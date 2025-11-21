from flask import Flask, render_template, redirect, url_for, request, flash, abort, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, and_
# Importaciones añadidas para manejo de errores de DB
from sqlalchemy.exc import OperationalError, IntegrityError 
from datetime import datetime, date, timedelta, time
import os
import json
import barcode
from barcode.writer import ImageWriter
from io import BytesIO
from collections import defaultdict
import base64
import locale
import pytz
import traceback 
import pandas as pd # Importado para manejo de Excel

# =================================================================
# CONFIGURACIÓN Y BASE DE DATOS
# =================================================================
app = Flask(__name__)

# Configuración de Base de Datos
# -----------------------------------------------------------------
# Toma la URL de la base de datos de Render desde las variables de entorno
# IMPORTANTE: Asegúrese de que esta URL sea correcta en Render
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'postgresql://la_cosmetiquera_de_gabi_user:z8vuwVK8rfm5S8CpZHZ3RITphvEolaqK@dpg-d48vb0i4d50c7391iap0-a.oregon-postgres.render.com/la_cosmetiquera_de_gabi'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.environ.get('SECRET_KEY', 'una_clave_secreta_por_defecto') # Añadir clave secreta

db = SQLAlchemy(app)

# Login Manager
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = "Por favor inicia sesión para acceder."
login_manager.login_message_category = "warning"

# =================================================================
# LÓGICA DE TIEMPO (COLOMBIA 6:00 AM - MEDIANOCHE)
# =================================================================
TIMEZONE_CO = pytz.timezone('America/Bogota')

def obtener_hora_colombia():
    """Devuelve la fecha y hora actual en Colombia."""
    return datetime.now(TIMEZONE_CO)

def obtener_rango_turno_colombia():
    """
    Calcula el inicio y fin del turno actual basado en la regla de las 6:00 AM.
    """
    ahora_co = obtener_hora_colombia()
    
    if ahora_co.hour < 6:
        fecha_comercial = ahora_co.date() - timedelta(days=1)
    else:
        fecha_comercial = ahora_co.date()

    inicio_turno_local = TIMEZONE_CO.localize(datetime.combine(fecha_comercial, time(6, 0, 0)))
    fin_turno_local = inicio_turno_local + timedelta(days=1) - timedelta(seconds=1)

    inicio_utc = inicio_turno_local.astimezone(pytz.UTC)
    fin_utc = fin_turno_local.astimezone(pytz.UTC)

    return fecha_comercial, inicio_utc, fin_utc

# =================================================================
# CLASE AUXILIAR PARA MANEJO DE ERRORES DE DB
# =================================================================
class EmptyPagination: 
    """Objeto dummy para evitar errores de Jinja2 cuando la paginación falla por DB."""
    items = []
    has_prev = False
    has_next = False
    prev_num = None
    next_num = None
    page = 1
    pages = 1
    total = 0
    
    # Esta función se añade para satisfacer el llamado `ventas_paginadas.iter_pages()`
    def iter_pages(self, left_edge=1, right_edge=1, left_current=2, right_current=2):
        yield 1

# =================================================================
# FILTROS Y CONTEXTO
# =================================================================
@app.context_processor
def inject_global_data():
    return dict(
        now=obtener_hora_colombia(),
        timedelta=timedelta
    )

@app.template_filter('format_number')
def format_number_filter(value):
    try:
        # Intenta usar la configuración local de Colombia
        locale.setlocale(locale.LC_ALL, 'es_CO.UTF-8')
        return "{:,.0f}".format(float(value)).replace(',', '.')
    except Exception:
        # Fallback si no encuentra la configuración regional
        try:
            return f"{float(value):,.0f}".replace(',', '_').replace('.', ',').replace('_', '.')
        except:
            return str(value)

@app.template_filter('from_json')
def from_json_filter(value):
    try:
        return json.loads(value)
    except:
        return {}

@app.template_filter('fecha_co')
def fecha_colombia_filter(value):
    """Convierte UTC a Hora Colombia para mostrar en vistas"""
    if not value: return ""
    # Si es solo una fecha (date), la convertimos a datetime con hora inicial
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
        
    if isinstance(value, str): return value
    
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        value = pytz.utc.localize(value)
        
    return value.astimezone(TIMEZONE_CO).strftime('%d/%m/%Y %I:%M %p')

# =================================================================
# MODELOS
# =================================================================
class Usuario(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    apellido = db.Column(db.String(100), nullable=False)
    cedula = db.Column(db.String(20), unique=True, nullable=False)
    rol = db.Column(db.String(50), default='Vendedora')
    password = db.Column(db.String(200))

    def set_password(self, password_texto):
        self.password = generate_password_hash(password_texto)

    def check_password(self, password_texto):
        return check_password_hash(self.password, password_texto)

class Cliente(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    telefono = db.Column(db.String(20))
    direccion = db.Column(db.String(200))
    email = db.Column(db.String(100))
    fecha_registro = db.Column(db.DateTime, default=datetime.utcnow)
    ventas = db.relationship('Venta', backref='comprador', lazy=True)

class Producto(db.Model):
    # La columna 'marca' está correctamente definida aquí.
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(100), unique=True, nullable=True)
    nombre = db.Column(db.String(100), nullable=False)
    descripcion = db.Column(db.String(255))
    marca = db.Column(db.String(100))
    cantidad = db.Column(db.Integer, default=0)
    valor_venta = db.Column(db.Float)
    valor_interno = db.Column(db.Float)
    stock_minimo = db.Column(db.Integer, default=5)

class Venta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    total = db.Column(db.Float)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente.id'))
    tipo_pago = db.Column(db.String(50))
    detalle_pago = db.Column(db.Text)
    vendedor = db.relationship('Usuario', backref='ventas_realizadas', lazy=True)

class VentaDetalle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    venta_id = db.Column(db.Integer, db.ForeignKey('venta.id'))
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'))
    cantidad = db.Column(db.Integer)
    precio_unitario = db.Column(db.Float)
    subtotal = db.Column(db.Float)
    producto = db.relationship('Producto', backref='detalles_venta', lazy=True)
    venta = db.relationship('Venta', backref='detalle_venta', lazy=True)

class CierreCaja(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha_cierre = db.Column(db.Date)
    hora_ejecucion = db.Column(db.DateTime, default=datetime.utcnow, name='hora_cierre') 
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    total_venta = db.Column(db.Float)
    total_efectivo = db.Column(db.Float)
    total_electronico = db.Column(db.Float)
    detalles_json = db.Column(db.Text)
    usuario = db.relationship('Usuario', backref='cierres_caja', lazy=True)

# =================================================================
# FUNCIONES DE UTILIDAD
# =================================================================
def generar_barcode_base64(codigo):
    try:
        codigo_str = str(codigo)
        code128 = barcode.get_barcode_class('code128')
        instance = code128(codigo_str, writer=ImageWriter())
        buffer = BytesIO()
        instance.write(buffer)
        base64_img = base64.b64encode(buffer.getvalue()).decode('utf-8')
        return f"data:image/png;base64,{base64_img}"
    except Exception as e:
        print(f"Error al generar barcode: {e}")
        return None

def enviar_informe_ventas(periodo):
    """Función placeholder para informes por correo"""
    with app.app_context():
        hoy = date.today()
        pass

# =================================================================
# RUTAS Y LÓGICA
# =================================================================
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Usuario, int(user_id))

@app.route('/')
def inicio():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        try:
            # Esta línea fue la que causó el error UndefinedTable
            user = Usuario.query.filter_by(username=username).first()
            
            if user and user.check_password(password): 
                login_user(user)
                return redirect(url_for('dashboard'))
                
            flash('Usuario o contraseña incorrectos.', 'danger')
        
        except OperationalError as e:
            flash(f'Error de conexión a la base de datos o tabla faltante. Detalle: {e}', 'danger')
        except Exception as e:
            flash(f'Error inesperado al intentar iniciar sesión: {e}', 'danger')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada correctamente.', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    fecha_comercial, inicio_utc, fin_utc = obtener_rango_turno_colombia()

    try:
        productos_bajos = Producto.query.filter(Producto.cantidad <= Producto.stock_minimo).count()
    except OperationalError: # Manejo específico de error de DB
        productos_bajos = 0
        flash('Advertencia: Problema de conexión/tabla de base de datos.', 'warning')
    except Exception:
        productos_bajos = 0

    try:
        total_inventario_query = db.session.query(func.sum(Producto.cantidad)).scalar()
        total_inventario = int(total_inventario_query) if total_inventario_query is not None else 0
    except Exception:
        total_inventario = 0

    try:
        ventas_hoy_query = db.session.query(func.sum(Venta.total)).filter(
            and_(Venta.fecha >= inicio_utc, Venta.fecha <= fin_utc)
        ).scalar()
        ventas_hoy = ventas_hoy_query if ventas_hoy_query is not None else 0.00
    except Exception:
        ventas_hoy = 0.00

    try:
        inicio_mes = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)
        clientes_nuevos_mes = Cliente.query.filter(Cliente.fecha_registro >= inicio_mes).count()
    except Exception:
        clientes_nuevos_mes = 0

    return render_template(
        'dashboard.html',
        current_user=current_user,
        productos_stock_bajo=productos_bajos,
        total_inventario=total_inventario,
        ventas_hoy=ventas_hoy,
        clientes_nuevos_mes=clientes_nuevos_mes
    )

# -------------------- RUTAS CLIENTES --------------------
@app.route('/clientes')
@login_required
def clientes():
    search_query = request.args.get('search', '').strip()
    try:
        if search_query:
            clientes = Cliente.query.filter(
                (Cliente.nombre.ilike(f'%{search_query}%')) |
                (Cliente.telefono.ilike(f'%{search_query}%'))
            ).all()
        else:
            clientes = Cliente.query.all()
    except OperationalError as e:
        flash(f'Error de Base de Datos al cargar clientes: {e}', 'danger')
        clientes = []
    except Exception as e:
        flash(f'Error al cargar clientes: {e}', 'danger')
        clientes = []
        
    return render_template('clientes.html', clientes=clientes)

@app.route('/clientes/agregar', methods=['POST'])
@login_required
def agregar_cliente():
    try:
        nuevo_cliente = Cliente(
            nombre=request.form.get('nombre', '').strip(),
            telefono=request.form.get('telefono', '').strip(),
            direccion=request.form.get('direccion', '').strip(),
            email=request.form.get('email', '').strip()
        )
        db.session.add(nuevo_cliente)
        db.session.commit()
        flash('Cliente agregado exitosamente!', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Error: El cliente ya existe (nombre o email duplicado).', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al agregar cliente: {e}', 'danger')
    return redirect(url_for('clientes'))

@app.route('/clientes/eliminar/<int:cliente_id>')
@login_required
def eliminar_cliente(cliente_id):
    if cliente_id == 1:
        flash('No se puede eliminar el cliente genérico.', 'danger')
        return redirect(url_for('clientes'))
    try:
        cliente = Cliente.query.get_or_404(cliente_id)
        db.session.delete(cliente)
        db.session.commit()
        flash('Cliente eliminado correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al eliminar cliente: {e}', 'danger')
    return redirect(url_for('clientes'))

@app.route('/clientes/editar/<int:cliente_id>', methods=['POST'])
@login_required
def editar_cliente(cliente_id):
    cliente = Cliente.query.get_or_404(cliente_id)
    try:
        cliente.nombre = request.form.get('nombre', '')
        cliente.telefono = request.form.get('telefono', '')
        cliente.direccion = request.form.get('direccion', '')
        cliente.email = request.form.get('email', '')
        db.session.commit()
        flash('Cliente actualizado exitosamente.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Error: El cliente ya existe (nombre o email duplicado).', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al editar cliente: {e}', 'danger')
    return redirect(url_for('clientes'))

# -------------------- RUTAS INVENTARIO --------------------
@app.route('/inventario')
@login_required
def inventario():
    page = request.args.get('page', 1, type=int)
    per_page = 50
    
    search_query = request.args.get('search', '').strip()
    query = Producto.query.order_by(Producto.id.desc())
    
    if search_query:
        query = query.filter(
            (Producto.nombre.ilike(f'%{search_query}%')) |
            (Producto.codigo.ilike(f'%{search_query}%')) |
            (Producto.descripcion.ilike(f'%{search_query}%'))
        )
    
    # Se eliminó la definición interna de EmptyPagination y se usa la global.

    try:
        productos_paginados = query.paginate(page=page, per_page=per_page, error_out=False)
    except OperationalError as e:
        # Mensaje de error si la tabla no existe o es inaccesible
        flash(f'❌ Error de Base de Datos: La tabla de productos es inaccesible. Detalle: {e}', 'danger')
        productos_paginados = EmptyPagination()
    except Exception as e:
        flash(f'❌ Error de paginación o consulta de inventario: {e}', 'danger')
        productos_paginados = EmptyPagination()


    return render_template('productos.html', 
                            productos_paginados=productos_paginados,
                            search_query=search_query)

@app.route('/inventario/agregar', methods=['POST'])
@login_required
def agregar_producto():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden agregar productos.', 'danger')
        return redirect(url_for('inventario'))
    try:
        cantidad_val = request.form.get('cantidad') or 0
        valor_venta_val = request.form.get('valor_venta') or 0
        valor_interno_val = request.form.get('valor_interno') or 0
        codigo_producto = request.form.get('codigo', '').strip() or None

        # Validación básica de datos
        if not request.form.get('nombre') or float(valor_venta_val) <= 0:
             flash('Error: El nombre y el valor de venta son obligatorios y deben ser positivos.', 'danger')
             return redirect(url_for('inventario'))

        # Llama al constructor de Producto(..., marca=...)
        nuevo_producto = Producto(
            codigo=codigo_producto,
            nombre=request.form.get('nombre'),
            descripcion=request.form.get('descripcion'),
            # Esta línea es donde SQLAlchemy falla si la DB está desactualizada
            marca=request.form.get('marca', '').strip() or None, 
            cantidad=int(cantidad_val),
            valor_venta=float(valor_venta_val),
            valor_interno=float(valor_interno_val)
        )
        db.session.add(nuevo_producto)
        db.session.flush() # Obtiene el ID antes del commit

        if nuevo_producto.codigo is None:
            # Genera un código basado en el ID, asegurando un formato de 12 dígitos
            nuevo_producto.codigo = str(nuevo_producto.id).zfill(12) 
            
        db.session.commit()
        flash('Producto agregado exitosamente!', 'success')
        
        # Redirige a la última página donde estará el producto nuevo
        total_productos = Producto.query.count()
        per_page = 50
        last_page = (total_productos + per_page - 1) // per_page if per_page > 0 else 1
        return redirect(url_for('inventario', page=last_page))

    except IntegrityError:
        db.session.rollback()
        flash('Error: Ya existe un producto con ese código.', 'danger')
        return redirect(url_for('inventario'))
    except Exception as e:
        db.session.rollback()
        flash(f'Error al agregar producto: {e}', 'danger')
    return redirect(url_for('inventario'))

@app.route('/inventario/eliminar/<int:producto_id>')
@login_required
def eliminar_producto(producto_id):
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden eliminar productos.', 'danger')
        return redirect(url_for('inventario'))
    try:
        producto = Producto.query.get_or_404(producto_id)
        db.session.delete(producto)
        db.session.commit()
        flash('Producto eliminado correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al eliminar producto: {e}', 'danger')
    return redirect(url_for('inventario'))

@app.route('/inventario/editar/<int:producto_id>', methods=['GET', 'POST'])
@login_required
def editar_producto(producto_id):
    producto = Producto.query.get_or_404(producto_id)
    if request.method == 'POST':
        if current_user.rol.lower() != 'administrador':
            flash('Permiso denegado.', 'danger')
            return redirect(url_for('inventario'))

        try:
            # Validación de código no vacío si no es None
            codigo_editado = request.form.get('codigo', '').strip() or None 
            if codigo_editado is None and producto.codigo:
                 flash('Error: No puedes dejar el código de barras en blanco si ya tiene uno.', 'danger')
                 return redirect(url_for('editar_producto', producto_id=producto_id))

            producto.codigo = codigo_editado
            producto.nombre = request.form.get('nombre')
            producto.descripcion = request.form.get('descripcion')
            producto.marca = request.form.get('marca', '').strip() or None
            producto.cantidad = int(request.form.get('cantidad') or 0)
            producto.valor_venta = float(request.form.get('valor_venta') or 0)
            producto.valor_interno = float(request.form.get('valor_interno') or 0)
            db.session.commit()
            flash('Producto actualizado exitosamente.', 'success')
        except IntegrityError:
            flash('Error: Ya existe un producto con ese código.', 'danger')
            db.session.rollback()
        except Exception as e:
            flash(f'Error al actualizar producto: {e}', 'danger')
            db.session.rollback()
        return redirect(url_for('inventario'))

    barcode_img = generar_barcode_base64(producto.codigo)
    return render_template('editar_producto.html', producto=producto, barcode_img=barcode_img)

@app.route('/inventario/agregar_stock', methods=['POST'])
@login_required
def agregar_stock_por_codigo():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden modificar inventario.', 'danger')
        return redirect(url_for('inventario'))

    codigo = request.form.get('codigo_scanner', '').strip()
    cantidad = request.form.get('cantidad_scanner', '')

    if not codigo or not cantidad:
        flash('Error: Debes ingresar el código y la cantidad.', 'danger')
        return redirect(url_for('inventario'))

    try:
        cantidad_a_agregar = int(cantidad or 0)
        if cantidad_a_agregar <= 0:
            flash('Error: La cantidad a agregar debe ser positiva.', 'danger')
            return redirect(url_for('inventario'))

        producto = Producto.query.filter_by(codigo=codigo).first()
        if not producto:
            flash(f'Error: Producto con código {codigo} no encontrado.', 'danger')
            return redirect(url_for('inventario'))

        producto.cantidad += cantidad_a_agregar
        db.session.commit()
        flash(f'Stock de {producto.nombre} actualizado (+{cantidad_a_agregar}).', 'success')

    except ValueError:
        flash('Error: La cantidad debe ser un número entero válido.', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al agregar stock: {e}', 'danger')

    return redirect(url_for('inventario'))

# -------------------- RUTAS VENTAS --------------------
@app.route('/ventas/nueva', methods=['GET', 'POST'])
@login_required
def nueva_venta():
    productos = Producto.query.filter(Producto.cantidad > 0).all()
    clientes = Cliente.query.all()

    if request.method == 'GET':
        return render_template('nueva_venta.html', productos=productos, clientes=clientes)

    if request.method == 'POST':
        try:
            total_venta = float(request.form.get('total_venta', 0) or 0)
            
            # --- Proceso de Pago ---
            detalle_pago_dict = {
                'Efectivo': float(request.form.get('pago_efectivo', 0) or 0),
                'Nequi': float(request.form.get('pago_nequi', 0) or 0),
                'Transferencia': float(request.form.get('pago_transferencia', 0) or 0),
                'Daviplata': float(request.form.get('pago_daviplata', 0) or 0),
                'Tarjeta/Bold': float(request.form.get('pago_tarjeta', 0) or 0),
                'Ref_Codigo': request.form.get('codigo_transaccion', '').strip(),
                'Ref_Fecha': request.form.get('fecha_transaccion', '')
            }

            total_pagado = sum([v for k,v in detalle_pago_dict.items() if k not in ['Ref_Codigo', 'Ref_Fecha'] and isinstance(v, (int, float))])

            if abs(total_venta - total_pagado) > 0.01:
                flash(f'Error: El total de la venta (${total_venta:,.0f}) no coincide con el total pagado (${total_pagado:,.0f}).', 'danger')
                return redirect(url_for('nueva_venta'))
                
            tipos_pagos = [k for k, v in detalle_pago_dict.items() if k not in ['Ref_Codigo', 'Ref_Fecha'] and float(v or 0) > 0]
            tipo_pago_general = "Mixto" if len(tipos_pagos) > 1 else (tipos_pagos[0] if tipos_pagos else "Sin Pago")

            nueva_venta = Venta(
                fecha=datetime.utcnow(),
                total=total_venta,
                usuario_id=current_user.id,
                cliente_id=int(request.form.get('cliente_id') or 1),
                tipo_pago=tipo_pago_general,
                detalle_pago=json.dumps(detalle_pago_dict)
            )
            db.session.add(nueva_venta)
            db.session.flush()

            productos_vendidos_json = request.form.get('productos_vendidos_json', '[]')
            productos_vendidos = json.loads(productos_vendidos_json)
            
            if not productos_vendidos:
                raise Exception("No se especificaron productos para la venta.")

            # --- Proceso de Detalle y Stock ---
            for item in productos_vendidos:
                item_id = int(item.get('id', 0))
                cantidad_vendida = int(item.get('cantidad', 0))
                precio_unitario = float(item.get('precio', 0))
                subtotal = float(item.get('subtotal', 0))
                
                if cantidad_vendida <= 0 or precio_unitario < 0:
                    continue 
                
                producto = db.session.get(Producto, item_id)
                
                if not producto:
                    raise Exception(f"Producto con ID {item_id} no encontrado.")
                
                if producto.cantidad < cantidad_vendida:
                    # Esto debería ser prevenido por el JS, pero es la validación final
                    raise Exception(f"Stock insuficiente para {producto.nombre}. Disponible: {producto.cantidad}, Solicitado: {cantidad_vendida}")
                
                detalle = VentaDetalle(
                    venta_id=nueva_venta.id,
                    producto_id=item_id,
                    cantidad=cantidad_vendida,
                    precio_unitario=precio_unitario,
                    subtotal=subtotal
                )
                db.session.add(detalle)
                
                producto.cantidad -= cantidad_vendida
            
            db.session.commit()
            flash('Venta registrada exitosamente!', 'success')
            return redirect(url_for('imprimir_comprobante', venta_id=nueva_venta.id))
        except Exception as e:
            db.session.rollback()
            flash(f'Ocurrió un error al procesar la venta: {e}', 'danger')
            return redirect(url_for('nueva_venta'))

@app.route('/ventas/comprobante/<int:venta_id>')
@login_required
def imprimir_comprobante(venta_id):
    venta = Venta.query.get_or_404(venta_id)
    fecha_local = pytz.utc.localize(venta.fecha).astimezone(TIMEZONE_CO)
    
    detalles = VentaDetalle.query.filter_by(venta_id=venta_id).all()

    # Agrupar detalles (si un producto aparece varias veces, ej: por diferentes precios)
    detalles_agrupados = defaultdict(lambda: {'cantidad': 0, 'subtotal': 0.0, 'precio_unitario': 0.0, 'producto': None})
    for d in detalles:
        pid = d.producto_id
        detalles_agrupados[pid]['cantidad'] += d.cantidad
        detalles_agrupados[pid]['subtotal'] += d.subtotal
        if detalles_agrupados[pid]['producto'] is None:
            detalles_agrupados[pid]['producto'] = d.producto
            detalles_agrupados[pid]['precio_unitario'] = d.precio_unitario
    detalles_finales = list(detalles_agrupados.values())

    pagos_normalizados = {}
    try:
        detalle_pago_dict = json.loads(venta.detalle_pago) if venta.detalle_pago else {}
        ref_cod = detalle_pago_dict.get('Ref_Codigo', '')
        ref_fecha = detalle_pago_dict.get('Ref_Fecha', '')

        for k, v in detalle_pago_dict.items():
            if k not in ['Ref_Codigo', 'Ref_Fecha'] and (isinstance(v, (int, float)) and v > 0):
                if k in ['Nequi', 'Transferencia', 'Daviplata', 'Tarjeta/Bold']:
                    pagos_normalizados[k] = {'monto': v, 'cod': ref_cod, 'fecha': ref_fecha}
                else:
                    pagos_normalizados[k] = {'monto': v, 'cod': '', 'fecha': ''}
    except Exception:
        pagos_normalizados = {}

    return render_template(
        'comprobante.html',
        venta=venta,
        detalles=detalles_finales,
        pagos=pagos_normalizados,
        fecha_local=fecha_local
    )

# -------------------- RUTAS CIERRE DE CAJA --------------------
@app.route('/ejecutar_cierre_caja', methods=['GET', 'POST'])
@login_required
def ejecutar_cierre_caja():
    if current_user.rol.lower() not in ['administrador', 'vendedora']:
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('reportes'))

    if request.method == 'POST':
        fecha_comercial, inicio_utc, fin_utc = obtener_rango_turno_colombia()

        cierre_existente = CierreCaja.query.filter_by(fecha_cierre=fecha_comercial).first()
        
        # Corrección: Permitir al admin re-cerrar, solo limitar a la vendedora
        if cierre_existente and current_user.rol.lower() == 'vendedora' and not current_user.rol.lower() == 'administrador':
            flash(f'La caja del día {fecha_comercial} ya fue cerrada. No puedes modificarla.', 'warning')
            return redirect(url_for('reportes'))
        
        ventas_turno = Venta.query.filter(
            and_(Venta.fecha >= inicio_utc, Venta.fecha <= fin_utc)
        ).all()

        total_venta = 0.0
        total_efectivo = 0.0
        detalle_metodos = defaultdict(float)
        detalle_vendedor = defaultdict(lambda: {'total': 0.0, 'efectivo': 0.0})

        for v in ventas_turno:
            total_venta += v.total
            try:
                pagos = json.loads(v.detalle_pago)
                # Cálculo de efectivo
                efectivo_v = float(pagos.get('Efectivo', 0) or 0)
                total_efectivo += efectivo_v
                
                # Desglose de todos los métodos
                for metodo, monto in pagos.items():
                    if metodo not in ['Ref_Codigo', 'Ref_Fecha'] and isinstance(monto, (int, float)):
                        detalle_metodos[metodo] += float(monto or 0)
                
                # Desglose por vendedor
                v_user = v.vendedor.username if v.vendedor else "N/A"
                detalle_vendedor[v_user]['total'] += v.total
                detalle_vendedor[v_user]['efectivo'] += efectivo_v
            except:
                pass

        total_electronico = total_venta - total_efectivo
        
        snapshot = {
            'metodos': dict(detalle_metodos),
            'vendedores': dict(detalle_vendedor),
            'hora_cierre_real': obtener_hora_colombia().strftime('%I:%M %p')
        }

        try:
            if cierre_existente:
                cierre = cierre_existente
                cierre.usuario_id = current_user.id
                cierre.total_venta = total_venta
                cierre.total_efectivo = total_efectivo
                cierre.total_electronico = total_electronico
                cierre.detalles_json = json.dumps(snapshot)
                cierre.hora_ejecucion = datetime.utcnow() 
            else:
                nuevo = CierreCaja(
                    fecha_cierre=fecha_comercial,
                    hora_ejecucion=datetime.utcnow(), 
                    usuario_id=current_user.id,
                    total_venta=total_venta,
                    total_efectivo=total_efectivo,
                    total_electronico=total_electronico,
                    detalles_json=json.dumps(snapshot)
                )
                db.session.add(nuevo)

            db.session.commit()
            flash(f'✅ Cierre de Caja registrado ({fecha_comercial}). Total: ${total_venta:,.0f}', 'success')
            return redirect(url_for('reportes'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al procesar el cierre de caja: {e}', 'danger')
            return redirect(url_for('reportes'))

    flash('Acción inválida. Usa el botón de la página de reportes.', 'warning')
    return redirect(url_for('reportes'))

@app.route('/cierre_caja/historial')
@login_required
def historial_cierres():
    if current_user.rol.lower() not in ['administrador', 'vendedora']:
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('dashboard'))
        
    cierres = CierreCaja.query.order_by(CierreCaja.fecha_cierre.desc()).all()
    return render_template('historial_cierres.html', cierres=cierres)

@app.route('/reportes')
@login_required
def reportes():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('dashboard'))

    fecha_comercial, inicio_utc, fin_utc = obtener_rango_turno_colombia()
    
    ventas_hoy = Venta.query.filter(and_(Venta.fecha >= inicio_utc, Venta.fecha <= fin_utc)).all()
    total_diario = sum(v.total for v in ventas_hoy)

    desglose_temp = defaultdict(float)
    
    for v in ventas_hoy:
        try:
            pagos = json.loads(v.detalle_pago)
            for metodo, valor in pagos.items():
                if metodo not in ['Ref_Codigo', 'Ref_Fecha', 'change'] and isinstance(valor, (int, float)):
                    desglose_temp[metodo] += float(valor or 0)
        except:
            pass
    
    informe_diario_list = []
    for metodo, total in desglose_temp.items():
        informe_diario_list.append(("General", metodo, total))

    inicio_mes = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)
    total_mensual = db.session.query(func.sum(Venta.total)).filter(Venta.fecha >= inicio_mes).scalar() or 0
    
    # Asegurando el inicio de semana (Lunes)
    inicio_semana_utc = datetime.utcnow() - timedelta(days=datetime.utcnow().weekday())
    inicio_semana_utc = inicio_semana_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    total_semanal = db.session.query(func.sum(Venta.total)).filter(Venta.fecha >= inicio_semana_utc).scalar() or 0

    datos_vendedores_query = db.session.query(
        Usuario.username, 
        func.sum(Venta.total)
    ).join(Venta).filter(
        and_(Venta.fecha >= inicio_utc, Venta.fecha <= fin_utc)
    ).group_by(Usuario.username).order_by(func.sum(Venta.total).desc()).all()

    labels_vendedores = [row[0] for row in datos_vendedores_query]
    data_vendedores = [float(row[1]) for row in datos_vendedores_query]

    if not labels_vendedores:
        labels_vendedores = ["Tienda (Sin Ventas)"]
        data_vendedores = [0]

    datos_vendedores = {
        "labels": labels_vendedores, 
        "data": data_vendedores
    }

    datos_tendencia = {
        "labels": ["Semana Pasada", "Hoy"], 
        "data": [0, total_diario] 
    }

    caja_cerrada_hoy = CierreCaja.query.filter_by(fecha_cierre=fecha_comercial).first() is not None

    return render_template(
        'reportes.html',
        hoy=fecha_comercial,
        informe_diario=informe_diario_list,
        total_diario=total_diario,
        total_semanal=total_semanal,
        total_mensual=total_mensual,
        caja_cerrada_hoy=caja_cerrada_hoy,
        datos_tendencia=datos_tendencia,
        datos_vendedores=datos_vendedores
    )

# -------------------- RUTAS USUARIOS --------------------
@app.route('/usuarios')
@login_required
def usuarios():
    if current_user.rol.lower() != 'administrador':
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('clientes'))
    try:
        usuarios_list = Usuario.query.all()
    except OperationalError:
        flash('Error de base de datos al cargar usuarios.', 'danger')
        usuarios_list = []
    return render_template('usuarios.html', usuarios=usuarios_list)

@app.route('/usuarios/agregar', methods=['POST'])
@login_required
def agregar_usuario():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('usuarios'))
    try:
        username = request.form.get('username').strip()
        
        if Usuario.query.filter_by(username=username).first():
            flash(f'Error: El usuario "{username}" ya existe.', 'danger')
            return redirect(url_for('usuarios'))
            
        nuevo_usuario = Usuario(
            username=username,
            nombre=request.form.get('nombre').strip(),
            apellido=request.form.get('apellido').strip(),
            cedula=request.form.get('cedula').strip(),
            rol=request.form.get('rol').strip()
        )
        nuevo_usuario.set_password(request.form.get('password'))
        db.session.add(nuevo_usuario)
        db.session.commit()
        flash(f'Usuario {username} creado.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Error: Cédula o nombre de usuario duplicado.', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al agregar producto: {e}', 'danger')
    return redirect(url_for('usuarios'))

@app.route('/usuarios/editar/<int:usuario_id>', methods=['GET', 'POST'])
@login_required
def editar_usuario(usuario_id):
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('usuarios'))
    usuario = Usuario.query.get_or_404(usuario_id)
    if request.method == 'POST':
        try:
            usuario.username = request.form.get('username', usuario.username)
            usuario.nombre = request.form.get('nombre', usuario.nombre)
            usuario.apellido = request.form.get('apellido', usuario.apellido)
            usuario.rol = request.form.get('rol', usuario.rol)
            pw = request.form.get('password')
            if pw: usuario.set_password(pw)
            db.session.commit()
            flash('Actualizado.', 'success')
        except IntegrityError:
            db.session.rollback()
            flash('Error: Nombre de usuario o cédula duplicado.', 'danger')
        except Exception as e:
            db.session.rollback()
            flash(f'Error: {e}', 'danger')
        return redirect(url_for('usuarios'))
    return render_template('editar_usuario.html', usuario=usuario)

@app.route('/usuarios/eliminar/<int:usuario_id>')
@login_required
def eliminar_usuario(usuario_id):
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('usuarios'))
    try:
        u = Usuario.query.get_or_404(usuario_id)
        db.session.delete(u)
        db.session.commit()
        flash('Eliminado.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al eliminar: {e}', 'danger')
    return redirect(url_for('usuarios'))

# -------------------- GESTIÓN DE VENTAS (ADMIN) --------------------
@app.route('/gestion_ventas')
@login_required
def gestion_ventas():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('clientes'))
    
    page = request.args.get('page', 1, type=int)
    per_page = 50

    try:
        ventas_paginadas = Venta.query.order_by(Venta.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
        clientes_full = Cliente.query.all()
        vendedores_full = Usuario.query.all()
    except OperationalError as e:
        flash(f'Error de Base de Datos al cargar ventas: {e}', 'danger')
        ventas_paginadas = EmptyPagination()
        clientes_full = []
        vendedores_full = []
    
    return render_template('gestion_ventas.html', 
                            ventas_paginadas=ventas_paginadas,
                            clientes_full=clientes_full,
                            vendedores_full=vendedores_full)

@app.route('/ventas/eliminar/<int:venta_id>')
@login_required
def eliminar_venta(venta_id):
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('gestion_ventas'))
    venta = Venta.query.get_or_404(venta_id)
    try:
        # Revertir el stock antes de eliminar los detalles
        detalles = VentaDetalle.query.filter_by(venta_id=venta.id).all()
        for d in detalles:
            p = Producto.query.get(d.producto_id)
            if p: p.cantidad += d.cantidad
        
        # Eliminar detalles y luego la venta
        VentaDetalle.query.filter_by(venta_id=venta.id).delete(synchronize_session='fetch')
        db.session.delete(venta)
        db.session.commit()
        flash(f'Venta {venta_id} anulada y stock recuperado.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al anular venta: {e}', 'danger')
    return redirect(url_for('gestion_ventas'))

@app.route('/ventas/editar_info/<int:venta_id>', methods=['POST'])
@login_required
def editar_informacion_venta(venta_id):
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('gestion_ventas'))
    
    v = Venta.query.get_or_404(venta_id)
    
    try:
        v.cliente_id = int(request.form.get('cliente_id')) if request.form.get('cliente_id') else None
        v.usuario_id = int(request.form.get('vendedor_id'))
        
        # El total de venta (v.total) se actualiza en la API de edición de detalle (api_editar_detalle_venta). 
        # Esta ruta solo actualiza los datos del cliente, vendedor y pagos.

        detalle_pago_dict = {
            'Efectivo': float(request.form.get('pago_efectivo', 0) or 0),
            'Nequi': float(request.form.get('pago_nequi', 0) or 0),
            'Transferencia': float(request.form.get('pago_transferencia', 0) or 0),
            'Daviplata': float(request.form.get('pago_daviplata', 0) or 0),
            'Tarjeta/Bold': float(request.form.get('pago_tarjeta', 0) or 0),
            'Ref_Codigo': request.form.get('codigo_transaccion', '').strip(),
            'Ref_Fecha': request.form.get('fecha_transaccion', '')
        }
        
        tipos_pagos = [k for k, v in detalle_pago_dict.items() 
                        if k not in ['Ref_Codigo', 'Ref_Fecha'] and float(v or 0) > 0]
        tipo_pago_general = "Mixto" if len(tipos_pagos) > 1 else (tipos_pagos[0] if tipos_pagos else "Sin Pago")
        
        v.tipo_pago = tipo_pago_general
        v.detalle_pago = json.dumps(detalle_pago_dict)
        
        db.session.commit()
        flash('✅ Información de venta actualizada correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'❌ Error al actualizar: {e}', 'danger')
    
    return redirect(url_for('gestion_ventas'))

# -------------------- APIS PARA MODALES --------------------
@app.route('/api/ventas/detalle/<int:venta_id>', methods=['GET'])
@login_required
def api_detalle_venta(venta_id):
    if current_user.rol.lower() != 'administrador': 
        return jsonify({'error':'403'}), 403
    
    venta = Venta.query.get(venta_id)
    if not venta: 
        return jsonify({'error':'404'}), 404
    
    prods = []
    
    try:
        # Consulta explícita a VentaDetalle, no implícita a la relación, para evitar lazy loading errors
        detalles = VentaDetalle.query.filter_by(venta_id=venta_id).all() 

        for d in detalles:
            # Producto.query.get(d.producto_id) puede fallar si el producto fue eliminado
            # Usaremos una consulta más simple para obtener el producto.
            p = db.session.get(Producto, d.producto_id)
            
            prod_data = {
                'id': d.producto_id,
                'nombre': p.nombre if p else f'Producto #{d.producto_id} (eliminado)',
                'descripcion': p.descripcion if p else '',
                'marca': p.marca if p and p.marca else 'Sin marca',
                'cantidad': d.cantidad,
                'precio_unitario': d.precio_unitario,
                'subtotal': d.subtotal,
                'precio_stock': p.valor_venta if p else 0 
            }
            prods.append(prod_data)
        
        try: 
            # CLAVE DE CORRECCIÓN: Si detalle_pago es None, json.loads() fallará. Usamos {} como default.
            pagos = json.loads(venta.detalle_pago or '{}') 
        except: 
            pagos = {}

        fecha_utc = pytz.utc.localize(venta.fecha).astimezone(pytz.UTC)
        fecha_formateada = fecha_utc.astimezone(TIMEZONE_CO).strftime('%d/%m/%Y %I:%M %p')

        return jsonify({
            'venta_id': venta.id,
            'total': venta.total,
            'cliente_id': venta.cliente_id,
            'vendedor_id': venta.usuario_id,
            'tipo_pago': venta.tipo_pago,
            'productos': prods,
            'pagos': pagos,
            'fecha': fecha_formateada 
        })
        
    except OperationalError as e:
        # Manejo específico para errores de DB, como tabla inexistente o columna
        print(f"Operational Error en api_detalle_venta: {e}")
        return jsonify({'error': 'Error de Base de Datos al cargar el detalle.', 'detail': str(e), 'productos': []}), 500
    except Exception as e:
        # Capturamos cualquier otro error inesperado, incluimos el traceback para depurar
        error_detail = traceback.format_exc()
        print(f"Error INESPERADO en api_detalle_venta: {e}\n{error_detail}")
        return jsonify({'error': 'Error inesperado al cargar el detalle.', 'detail': str(e), 'productos': [], 'trace': error_detail}), 500


@app.route('/api/productos/todos', methods=['GET'])
@login_required
def api_todos_los_productos():
    try:
        productos_list = [{
            'id': p.id, 
            'nombre': p.nombre, 
            'marca': p.marca or 'Sin marca',
            'valor_venta': p.valor_venta, 
            'cantidad_stock': p.cantidad, 
            'descripcion': p.descripcion or ''
        } for p in Producto.query.all()]
        return jsonify({'productos': productos_list})
    except OperationalError:
        return jsonify({'productos': [], 'error': 'Error de base de datos.'}), 500

@app.route('/api/ventas/detalle/editar/<int:venta_id>', methods=['POST'])
@login_required
def api_editar_detalle_venta(venta_id):
    """
    API para editar el detalle de productos de una venta, revirtiendo el stock anterior
    y actualizando el total de la venta (v.total).
    """
    if current_user.rol.lower() != 'administrador': 
        return jsonify({'success':False}), 403
    
    venta = Venta.query.get(venta_id)
    if not venta:
        return jsonify({'success': False, 'message': 'Venta no encontrada'}), 404
    
    data = request.get_json()
    productos_nuevos = data.get('productos', [])
    
    try:
        # 1. Revertir el stock de los detalles viejos
        detalles_viejos = VentaDetalle.query.filter_by(venta_id=venta_id).all()
        for d in detalles_viejos:
            p = Producto.query.get(d.producto_id)
            if p: 
                p.cantidad += d.cantidad
        
        # 2. Eliminar los detalles viejos
        VentaDetalle.query.filter_by(venta_id=venta.id).delete(synchronize_session='fetch')

        # 3. Procesar y agregar los nuevos detalles
        nuevo_total = 0.0
        for item in productos_nuevos:
            pid = int(item.get('id'))
            cant = int(item.get('cantidad'))
            precio = float(item.get('precio_unitario')) 
            
            if cant > 0:
                sub = cant * precio
                nuevo_total += sub
                p = Producto.query.get(pid)
                
                if p:
                    if p.cantidad < cant:
                        # Si esto falla, el stock ya está revertido, pero se debe revertir el total.
                        raise Exception(f'Stock insuficiente para: {p.nombre}. Disponible: {p.cantidad}, Solicitado: {cant}')
                    
                    p.cantidad -= cant
                    db.session.add(VentaDetalle(
                        venta_id=venta.id, 
                        producto_id=pid, 
                        cantidad=cant, 
                        precio_unitario=precio, 
                        subtotal=sub
                    ))
        
        # 4. Actualizar el total de la venta (CLAVE para sincronizar con la edición de pagos)
        venta.total = nuevo_total
        
        if nuevo_total == 0:
            venta.tipo_pago = "Anulada/Sin Productos"
            
        db.session.commit()
        return jsonify({'success': True, 'nuevo_total': nuevo_total})
        
    except Exception as e:
        db.session.rollback() 
        return jsonify({'success': False, 'message': str(e)}), 500

# =================================================================
# LÓGICA DE IMPORTACIÓN DESDE EXCEL (NUEVA RUTA ADMINISTRATIVA)
# =================================================================

@app.route('/importar')
@login_required
def vista_importar():
    """Ruta para mostrar la interfaz de subida de Excel."""
    if current_user.rol.lower() != 'administrador':
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('dashboard'))
    return render_template('importar_datos.html')


@app.route('/admin/importar_productos', methods=['POST'])
@login_required
def importar_productos_excel():
    """
    Ruta que permite a los administradores subir un archivo Excel (.xlsx) 
    y volcar los datos de la hoja 'Producto' a la base de datos, 
    sobrescribiendo los datos existentes para una limpieza masiva.
    """
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden importar datos.', 'danger')
        return redirect(url_for('inventario'))

    if 'excel_file' not in request.files:
        flash('Error: No se encontró el archivo en la solicitud.', 'danger')
        return redirect(url_for('vista_importar'))

    file = request.files['excel_file']
    if file.filename == '':
        flash('Error: Archivo no seleccionado.', 'danger')
        return redirect(url_for('vista_importar'))

    if file and file.filename.endswith('.xlsx'):
        try:
            # Leer el archivo Excel directamente desde la memoria (BytesIO)
            excel_data = BytesIO(file.read())
            
            # Usar pandas para leer la hoja 'Producto'
            df_productos = pd.read_excel(excel_data, sheet_name='Producto')
            
            # Comienza la transacción de base de datos
            db.session.begin_nested() 
            
            # OPCIONAL: Eliminar productos existentes para evitar IDs duplicados
            db.session.query(Producto).delete() 
            db.session.commit() # Commit para el DELETE

            filas_importadas = 0
            for index, row in df_productos.iterrows():
                # Validación básica de campos obligatorios
                if pd.isna(row['nombre']) or pd.isna(row['valor_venta']):
                    continue
                
                nuevo_producto = Producto(
                    codigo=str(row['codigo']) if pd.notna(row['codigo']) else None,
                    nombre=str(row['nombre']),
                    descripcion=str(row['descripcion']) if pd.notna(row['descripcion']) else None,
                    marca=str(row['marca']) if pd.notna(row['marca']) else None,
                    cantidad=int(row['cantidad'] if pd.notna(row['cantidad']) else 0),
                    valor_venta=float(row['valor_venta']),
                    valor_interno=float(row['valor_interno'] if pd.notna(row['valor_interno']) else 0),
                    stock_minimo=int(row['stock_minimo'] if pd.notna(row['stock_minimo']) else 5)
                )
                db.session.add(nuevo_producto)
                filas_importadas += 1
            
            db.session.commit()
            flash(f'✅ ¡Éxito! {filas_importadas} productos importados desde Excel (Hoja Producto).', 'success')
            
        except KeyError:
            db.session.rollback()
            flash('Error: El Excel debe contener una hoja llamada "Producto" con las columnas correctas.', 'danger')
        except Exception as e:
            db.session.rollback()
            flash(f'Error grave al procesar el Excel: {e}', 'danger')
    else:
        flash('Error: El archivo debe ser un Excel (.xlsx).', 'danger')

    return redirect(url_for('inventario'))

# =================================================================
# EJECUCIÓN E INICIALIZACIÓN PARA PRODUCCIÓN (RENDER)
# =================================================================

# Este bloque es CRUCIAL para que Render cree las tablas usando la URL de PostgreSQL.
with app.app_context():
    try:
        # 1. Crear todas las tablas: SOLUCIÓN AL ERROR UndefinedTable
        db.create_all() 
        print("✅ Tablas creadas (o verificadas) correctamente en PostgreSQL de Render.")
        
        # 2. Inicialización de Usuario Admin
        admin = Usuario.query.filter_by(username='admin').first()
        if not admin:
            admin = Usuario(
                username='admin', 
                nombre='Admin', 
                apellido='G', 
                cedula='123', 
                rol='Administrador'
            )
            admin.set_password('admin123')
            db.session.add(admin)
            print("✅ Usuario admin creado: admin / admin123")
        
        # 3. Inicialización de Cliente Genérico
        generico = Cliente.query.get(1)
        if not generico:
            # Creamos el cliente genérico con ID=1
            generico = Cliente(
                id=1, 
                nombre='Contado / Genérico', 
                telefono='N/A', 
                direccion='N/A', 
                email='N/A'
            )
            db.session.add(generico)
            print("✅ Cliente genérico creado.")

        # 4. Inicialización de Datos de Prueba (Opcional)
        if Producto.query.count() == 0:
            # Para evitar errores si el usuario va a importar datos desde Excel.
            prod_labial = Producto(
                codigo='LBL001', 
                nombre='Labial Rojo Mate', 
                descripcion='Larga duración, tono 45', 
                marca='Macareana',
                cantidad=20, 
                valor_venta=35000.00, 
                valor_interno=15000.00
            )
            prod_polvo = Producto(
                codigo='PLV002', 
                nombre='Polvo Compacto', 
                descripcion='Tono claro, protector solar', 
                marca='Bella Piel',
                cantidad=10, 
                valor_venta=50000.00, 
                valor_interno=25000.00
            )
            db.session.add_all([prod_labial, prod_polvo])
            print("✅ Productos de prueba creados.")

            # Crear una venta de prueba
            venta_prueba = Venta(
                fecha=datetime.utcnow(),
                total=85000.00, 
                usuario_id=admin.id,
                cliente_id=generico.id,
                detalle_pago=json.dumps({'Efectivo': 85000.00, 'Nequi': 0, 'Transferencia': 0, 'Daviplata': 0, 'Tarjeta/Bold': 0, 'Ref_Codigo': '', 'Ref_Fecha': ''})
            )
            db.session.add(venta_prueba)
            db.session.flush()

            db.session.add(VentaDetalle(
                venta_id=venta_prueba.id, 
                producto_id=prod_labial.id, 
                cantidad=1, 
                precio_unitario=35000.00, 
                subtotal=35000.00
            ))
            db.session.add(VentaDetalle(
                venta_id=venta_prueba.id, 
                producto_id=prod_polvo.id, 
                cantidad=1, 
                precio_unitario=50000.00, 
                subtotal=50000.00
            ))
            
            prod_labial.cantidad -= 1
            prod_polvo.cantidad -= 1
            print(f"✅ Venta de prueba N° {venta_prueba.id} creada para depuración.")

        # Commit final para guardar todas las inicializaciones
        db.session.commit()
        
    except Exception as e:
        # Este bloque te ayudará a diagnosticar si Render no puede conectar con la DB
        print(f"❌ ¡ERROR CRÍTICO DURANTE LA INICIALIZACIÓN DE DB!: {e}")
        print("Asegúrese de que la URL de la base de datos sea accesible.")
        db.session.rollback()


# Bloque para ejecución local de desarrollo (opcional, puede quedar vacío)
if __name__ == "__main__":
    # La indentación AQUÍ ha sido corregida.
    app.run(debug=True, port=5000)