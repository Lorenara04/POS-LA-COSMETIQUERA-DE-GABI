from flask import Flask, render_template, redirect, url_for, request, flash, abort, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func
from datetime import datetime, date, timedelta
import os
import json
import barcode
from barcode.writer import ImageWriter
from io import BytesIO
from collections import defaultdict
import base64
import locale
import sys
from sqlalchemy.exc import OperationalError

# =================================================================
# APP CONFIG & DATABASE
# =================================================================
DB_FILENAME = 'pos_cosmetiqueria.db'
DB_PATH = os.path.join('/data', DB_FILENAME)  # /data/pos_cosmetiqueria.db

# Asegurar la existencia del directorio /data
DB_DIR = '/data'
if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR) 

app = Flask(__name__)

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))

db = SQLAlchemy(app)

# ===================== CHEQUEO DE BASE =====================
with app.app_context():
    try:
        # Intentar un simple query para ver si la tabla 'usuario' existe
        db.session.execute("SELECT 1 FROM usuario LIMIT 1")
    except OperationalError:
        # Si falla, la tabla no existe → crear todas las tablas
        print("Tablas no detectadas. Creando estructura...")
        db.create_all()
    else:
        print("--- BASE DE DATOS RESTAURADA DETECTADA Y CARGADA. OMITIENDO INICIALIZACIÓN. ---")

# =================================================================
# MODELOS (DEBEN ESTAR DEFINIDOS ANTES DEL BLOQUE DE INICIALIZACIÓN)
# =================================================================
class Usuario(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    apellido = db.Column(db.String(100), nullable=False)
    cedula = db.Column(db.String(20), unique=True, nullable=False)
    rol = db.Column(db.String(50), default='Vendedora')
    password_hash = db.Column(db.String(200))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Cliente(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    telefono = db.Column(db.String(20))
    direccion = db.Column(db.String(200))
    email = db.Column(db.String(100))
    fecha_registro = db.Column(db.DateTime, default=datetime.utcnow)
    ventas = db.relationship('Venta', backref='comprador', lazy=True)


class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(100), unique=True, nullable=True)
    nombre = db.Column(db.String(100), nullable=False)
    descripcion = db.Column(db.String(255))
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


class CierreCaja(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha_cierre = db.Column(db.DateTime, default=datetime.utcnow)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    total_venta = db.Column(db.Float)
    total_efectivo = db.Column(db.Float)
    total_electronico = db.Column(db.Float)
    detalles_json = db.Column(db.Text)
    usuario = db.relationship('Usuario', backref='cierres_caja', lazy=True)


# =================================================================
# LÓGICA DE INICIALIZACIÓN SEGURA (Chequeo por Tamaño de Archivo)
# =================================================================
DB_FILE_PATH_FULL = os.path.join('/data', 'pos_cosmetiqueria.db')
MIN_DB_SIZE = 1000 # 1KB es suficiente para indicar que tiene datos reales

with app.app_context():
    try:
        # 1. Chequeo CLAVE: Si el archivo existe y tiene un tamaño significativo
        if os.path.exists(DB_FILE_PATH_FULL) and os.path.getsize(DB_FILE_PATH_FULL) > MIN_DB_SIZE:
            
            # Intentamos leer la tabla Usuario; si falla, el archivo está dañado.
            Usuario.query.first() 
            print("--- BASE DE DATOS RESTAURADA DETECTADA Y CARGADA. OMITIENDO INICIALIZACIÓN. ---")
            
        else:
            # 2. Si el archivo no existe o está vacío (o si el chequeo de arriba falló)
            print("--- INICIALIZACIÓN DE ESTRUCTURA Y DATOS POR DEFECTO ---")
            db.create_all()
            
            # Crear usuario administrador inicial
            admin = Usuario(
                username='admin', nombre='Admin', apellido='Principal', cedula='0000', rol='Administrador'
            )
            admin.set_password('1234')
            db.session.add(admin)
            
            # Crear cliente genérico
            cliente_gen = Cliente(
                nombre='Contado / Genérico', telefono='', direccion='', email=''
            )
            db.session.add(cliente_gen)
            
            db.session.commit()
            print("Base de datos inicializada correctamente con datos por defecto.")
            
    except Exception as e:
        # Esto atrapa errores como 'no such table' si la DB restaurada está dañada.
        # Si llega aquí, significa que el archivo existe pero no es válido, así que lo sobreescribimos.
        print(f"ATENCIÓN: Archivo DB encontrado pero dañado ({e}). Creando ESTRUCTURA DE TABLAS nueva...")
        db.create_all()
        db.session.commit()
        # NOTA: Si el error persiste, deberás borrar manualmente el archivo .db en el Web Shell de Render.


# LOGIN MANAGER
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ----------------------------------------
# CONFIGURACIÓN DE CORREO (Opcional - Mantener comentada si no se usa)
# ----------------------------------------
#app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
#app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
#app.config['MAIL_USE_TLS'] = bool(os.environ.get('MAIL_USE_TLS', True))
#app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
#app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
#app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', '')
#mail = Mail(app)

# =================================================================
# CONTEXT PROCESSOR (Soluciona el error 'now' no definido en plantillas)
# =================================================================
@app.context_processor
def inject_now():
    """Hace que 'now' esté disponible en todas las plantillas automáticamente."""
    return {'now': datetime.now()}

# =================================================================
# LOCALE Y FILTRO JINJA (format_number y from_json)
# =================================================================
# Intento de configurar locale español
try:
    locale.setlocale(locale.LC_ALL, 'es_ES.utf8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_ALL, 'es_ES')
    except locale.Error:
        pass

@app.template_filter('format_number')
def format_number_filter(value):
    """Formatea un número con separador de miles y dos decimales."""
    try:
        # Intenta usar la configuración regional para un formato más limpio
        return locale.format_string("%.0f", float(value), grouping=True)
    except Exception:
        try:
            # Fallback a un formato de Python estándar si locale falla
            return f"{float(value):,.0f}"
        except Exception:
            return value

@app.template_filter('from_json')
def from_json_filter(value):
    """Convierte una cadena JSON a un objeto Python. Necesario para CierreCaja."""
    try:
        return json.loads(value)
    except Exception:
        return {}


# =================================================================
# FUNCIONES DE UTILIDAD
# =================================================================
@app.context_processor
def inject_global_data():
    return dict(timedelta=timedelta)


def generar_barcode_base64(codigo):
    """Genera un codigo de barras (Code128) y lo devuelve como imagen Base64."""
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
    """
    Función para generar y (opcionalmente) enviar informes por correo.
    """
    with app.app_context():
        hoy = date.today()

        if periodo == 'semanal':
            inicio = hoy - timedelta(days=7)
            asunto = f"📊 Informe Semanal de Ventas - {hoy.strftime('%d/%m/%Y')}"
            total = db.session.query(func.sum(Venta.total)).filter(Venta.fecha >= inicio).scalar() or 0

        elif periodo == 'mensual':
            inicio = hoy.replace(day=1)
            asunto = f"💰 Informe Mensual de Ventas - {hoy.strftime('%B %Y')}"
            total = db.session.query(func.sum(Venta.total)).filter(Venta.fecha >= inicio).scalar() or 0
        else:
            return

        ventas_vendedor = db.session.query(
            Usuario.username,
            func.sum(Venta.total)
        ).join(Venta, Usuario.id == Venta.usuario_id
        ).filter(Venta.fecha >= inicio
        ).group_by(Usuario.username
        ).order_by(func.sum(Venta.total).desc()).all()

        cuerpo = f"Hola Administradora,\n\nAdjunto los datos resumidos del {periodo} de ventas:\n\n"
        cuerpo += f"TOTAL DE VENTAS {periodo.upper()}: ${total:,.2f}\n\nVentas por Vendedor:\n"
        for v, t in ventas_vendedor:
            cuerpo += f"- {v}: ${t or 0:,.2f}\n"

        cuerpo += "\nPara ver el informe detallado y gráficos, por favor ingresa al sistema.\n"
        
        # (Aquí iría la lógica de envío de correo)
        print(asunto)
        print(cuerpo)


# =================================================================
# RUTAS Y LÓGICA
# =================================================================
@login_manager.user_loader
def load_user(user_id):
    return Usuario.query.get(int(user_id))


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
        user = Usuario.query.filter_by(username=username).first()
        
        if user and user.check_password(password): 
            login_user(user)
            return redirect(url_for('dashboard'))
            
        flash('Usuario o contraseña incorrectos.', 'danger')
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
    # -------------------------------------------------------------
    # 1. CÁLCULO DE STOCK BAJO (se mantiene para la otra tarjeta)
    # -------------------------------------------------------------
    try:
        productos_bajos = Producto.query.filter(Producto.cantidad <= Producto.stock_minimo).count()
    except Exception:
        productos_bajos = 0

    # -------------------------------------------------------------
    # 2. CÁLCULO DEL TOTAL DE INVENTARIO (CORREGIDO)
    # -------------------------------------------------------------
    try:
        # Suma la columna 'cantidad' de todos los productos
        total_inventario_query = db.session.query(func.sum(Producto.cantidad)).scalar()
        # Asegura que sea 0 si es None, y lo pasa a entero
        total_inventario = int(total_inventario_query) if total_inventario_query is not None else 0
    except Exception:
        total_inventario = 0

    # -------------------------------------------------------------
    # 3. CÁLCULO DE VENTAS Y CLIENTES (se mantiene)
    # -------------------------------------------------------------
    try:
        hoy = datetime.now().date()
        ventas_hoy_query = db.session.query(func.sum(Venta.total)).filter(func.date(Venta.fecha) == hoy).scalar()
        ventas_hoy = ventas_hoy_query if ventas_hoy_query is not None else 0.00
    except Exception:
        ventas_hoy = 0.00

    try:
        inicio_mes = datetime.now().replace(day=1).date()
        clientes_nuevos_mes = Cliente.query.filter(func.date(Cliente.fecha_registro) >= inicio_mes).count()
    except Exception:
        clientes_nuevos_mes = 0

    # -------------------------------------------------------------
    # 4. PASAR TODAS LAS VARIABLES A LA PLANTILLA
    # -------------------------------------------------------------
    return render_template(
        'dashboard.html',
        current_user=current_user,
        productos_stock_bajo=productos_bajos,
        total_inventario=total_inventario, # <<< VARIABLE CORRECTA
        ventas_hoy=ventas_hoy,
        clientes_nuevos_mes=clientes_nuevos_mes
    )


# ===================== RUTAS CLIENTES =====================
@app.route('/clientes')
@login_required
def clientes():
    search_query = request.args.get('search', '').strip()
    if search_query:
        clientes = Cliente.query.filter(
            (Cliente.nombre.ilike(f'%{search_query}%')) |
            (Cliente.telefono.ilike(f'%{search_query}%'))
        ).all()
    else:
        clientes = Cliente.query.all()
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
    cliente = Cliente.query.get_or_404(cliente_id)
    db.session.delete(cliente)
    db.session.commit()
    flash('Cliente eliminado correctamente.', 'success')
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
    except Exception as e:
        db.session.rollback()
        flash(f'Error al editar cliente: {e}', 'danger')
    return redirect(url_for('clientes'))


# ===================== RUTAS INVENTARIO =====================
@app.route('/inventario')
@login_required
def inventario():
    search_query = request.args.get('search', '').strip()
    if search_query:
        productos = Producto.query.filter(
            (Producto.nombre.ilike(f'%{search_query}%')) |
            (Producto.codigo.ilike(f'%{search_query}%')) |
            (Producto.descripcion.ilike(f'%{search_query}%'))
        ).all()
    else:
        productos = Producto.query.order_by(Producto.id.desc()).all()
    return render_template('productos.html', productos=productos)


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

        nuevo_producto = Producto(
            codigo=codigo_producto,
            nombre=request.form.get('nombre'),
            descripcion=request.form.get('descripcion'),
            cantidad=int(cantidad_val),
            valor_venta=float(valor_venta_val),
            valor_interno=float(valor_interno_val)
        )
        db.session.add(nuevo_producto)
        db.session.flush()
        if nuevo_producto.codigo is None:
            nuevo_producto.codigo = str(nuevo_producto.id).zfill(12)
        db.session.commit()
        flash('Producto agregado exitosamente!', 'success')
    except ValueError as ve:
        db.session.rollback()
        flash(f'Error al agregar producto: valor inválido. {ve}', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error general al agregar producto: {e}', 'danger')
    return redirect(url_for('inventario'))


@app.route('/inventario/eliminar/<int:producto_id>')
@login_required
def eliminar_producto(producto_id):
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden eliminar productos.', 'danger')
        return redirect(url_for('inventario'))
    producto = Producto.query.get_or_404(producto_id)
    db.session.delete(producto)
    db.session.commit()
    flash('Producto eliminado correctamente.', 'success')
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
            producto.codigo = request.form.get('codigo')
            producto.nombre = request.form.get('nombre')
            producto.descripcion = request.form.get('descripcion')
            producto.cantidad = int(request.form.get('cantidad') or 0)
            producto.valor_venta = float(request.form.get('valor_venta') or 0)
            producto.valor_interno = float(request.form.get('valor_interno') or 0)
            db.session.commit()
            flash('Producto actualizado exitosamente.', 'success')
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

    codigo = request.form.get('codigo_scanner')
    cantidad = request.form.get('cantidad_scanner')

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
        flash('Error: La cantidad o el código no son válidos.', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al agregar stock: {e}', 'danger')

    return redirect(url_for('inventario'))


# ===================== RUTAS VENTAS =====================
@app.route('/ventas/nueva', methods=['GET', 'POST'])
@login_required
def nueva_venta():
    productos = Producto.query.all()
    clientes = Cliente.query.all()

    if request.method == 'GET':
        return render_template('nueva_venta.html', 
                               productos=productos, 
                               clientes=clientes)

    if request.method == 'POST':
        try:
            total_venta = float(request.form.get('total_venta', 0) or 0)
            pago_efectivo = float(request.form.get('pago_efectivo', 0) or 0)
            pago_nequi = float(request.form.get('pago_nequi', 0) or 0)
            pago_transferencia = float(request.form.get('pago_transferencia', 0) or 0)
            pago_daviplata = float(request.form.get('pago_daviplata', 0) or 0)
            pago_tarjeta = float(request.form.get('pago_tarjeta', 0) or 0)

            cod_transaccion = request.form.get('codigo_transaccion', '').strip()
            fecha_transaccion = request.form.get('fecha_transaccion', '')

            total_pagado = pago_efectivo + pago_nequi + pago_transferencia + pago_daviplata + pago_tarjeta

            if round(total_pagado, 2) < round(total_venta, 2):
                flash('Error: El total pagado es menor al total de la venta.', 'danger')
                return redirect(url_for('nueva_venta'))

            detalle_pago_dict = {
                'Efectivo': pago_efectivo,
                'Nequi': pago_nequi,
                'Transferencia': pago_transferencia,
                'Daviplata': pago_daviplata,
                'Tarjeta/Bold': pago_tarjeta,
                'Ref_Codigo': cod_transaccion,
                'Ref_Fecha': fecha_transaccion
            }

            tipos_pagos = [k for k, v in detalle_pago_dict.items()
                             if k not in ['Ref_Codigo', 'Ref_Fecha'] and float(v or 0) > 0]
            tipo_pago_general = "Mixto" if len(tipos_pagos) > 1 else tipos_pagos[0] if tipos_pagos else "Sin Pago"

            cliente_id = int(request.form.get('cliente_id') or 1)

            nueva_venta = Venta(
                fecha=datetime.utcnow(),
                total=total_venta,
                usuario_id=current_user.id,
                cliente_id=cliente_id,
                tipo_pago=tipo_pago_general,
                detalle_pago=json.dumps(detalle_pago_dict)
            )
            db.session.add(nueva_venta)
            db.session.flush()

            productos_vendidos_json = request.form.get('productos_vendidos_json', '[]')
            try:
                productos_vendidos = json.loads(productos_vendidos_json)
            except json.JSONDecodeError as e:
                flash(f'Error de formato JSON en productos vendidos: {e}', 'danger')
                db.session.rollback()
                return redirect(url_for('nueva_venta'))

            for item in productos_vendidos:
                item_id = int(item.get('id', 0))
                cantidad_vendida = int(item.get('cantidad', 0))
                precio_unitario = float(item.get('precio', 0))
                subtotal = float(item.get('subtotal', 0))
                producto = Producto.query.get(item_id)

                if producto and int(producto.cantidad) >= cantidad_vendida:
                    detalle = VentaDetalle(
                        venta_id=nueva_venta.id,
                        producto_id=item_id,
                        cantidad=cantidad_vendida,
                        precio_unitario=precio_unitario,
                        subtotal=subtotal
                    )
                    db.session.add(detalle)
                    producto.cantidad -= cantidad_vendida
                else:
                    flash(f"Stock insuficiente para {producto.nombre if producto else 'desconocido'}. Cantidad solicitada: {cantidad_vendida}, disponible: {producto.cantidad if producto else 0}.", 'danger')
                    db.session.rollback()
                    return redirect(url_for('nueva_venta'))

            db.session.commit()
            flash('Venta registrada exitosamente!', 'success')
            return redirect(url_for('imprimir_comprobante', venta_id=nueva_venta.id))
        except Exception as e:
            db.session.rollback()
            flash(f'Ocurrió un error general al procesar la venta: {e}', 'danger')
            return redirect(url_for('nueva_venta'))


@app.route('/ventas/comprobante/<int:venta_id>')
@login_required
def imprimir_comprobante(venta_id):
    venta = Venta.query.get_or_404(venta_id)
    fecha_local = venta.fecha - timedelta(hours=5)
    detalles = VentaDetalle.query.filter_by(venta_id=venta_id).all()

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

    except Exception as e:
        print(f"Error normalizando detalle_pago para Venta ID {venta.id}: {e}")
        pagos_normalizados = {}

    return render_template(
        'comprobante.html',
        venta=venta,
        detalles=detalles_finales,
        pagos=pagos_normalizados,
        fecha_local=fecha_local
    )


# ===================== RUTAS CIERRE DE CAJA =====================
@app.route('/cierre_caja/ejecutar', methods=['POST'])
@login_required
def ejecutar_cierre_caja():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden realizar el cierre de caja.', 'danger')
        return redirect(url_for('reportes'))

    hoy = date.today()

    # 1. Verificar si ya hay un cierre para hoy
    cierre_existente = CierreCaja.query.filter(func.date(CierreCaja.fecha_cierre) == hoy).first()
    if cierre_existente:
        flash('⚠️ El cierre de caja para hoy ya fue registrado. Puedes consultarlo en el historial.', 'warning')
        return redirect(url_for('reportes'))

    # 2. Recalcular los datos del día
    ventas_del_dia = Venta.query.filter(func.date(Venta.fecha) == hoy).all()
    if not ventas_del_dia:
        flash('ℹ️ No se registraron ventas hoy. El cierre se registra con total $0.', 'info')

    total_diario = 0.0
    total_efectivo = 0.0
    
    total_pagos_electronicos = defaultdict(float)
    
    informe_diario_detallado = defaultdict(lambda: {
        'total_venta': 0.0,
        'Efectivo': 0.0,
        'Nequi': 0.0,
        'Daviplata': 0.0,
        'Transferencia': 0.0,
        'Tarjeta/Bold': 0.0
    })

    for venta in ventas_del_dia:
        total_diario += venta.total
        try:
            pagos = json.loads(venta.detalle_pago)
            efectivo = pagos.get('Efectivo', 0.0)
            nequi = pagos.get('Nequi', 0.0)
            daviplata = pagos.get('Daviplata', 0.0)
            transferencia = pagos.get('Transferencia', 0.0)
            tarjeta = pagos.get('Tarjeta/Bold', 0.0)

            total_efectivo += efectivo
            
            total_pagos_electronicos['Nequi'] += nequi
            total_pagos_electronicos['Daviplata'] += daviplata
            total_pagos_electronicos['Transferencia'] += transferencia
            total_pagos_electronicos['Tarjeta/Bold'] += tarjeta

            vendedor_username = venta.vendedor.username if venta.vendedor else "N/A"
            
            informe_diario_detallado[vendedor_username]['total_venta'] += venta.total
            informe_diario_detallado[vendedor_username]['Efectivo'] += efectivo
            informe_diario_detallado[vendedor_username]['Nequi'] += nequi
            informe_diario_detallado[vendedor_username]['Daviplata'] += daviplata
            informe_diario_detallado[vendedor_username]['Transferencia'] += transferencia
            informe_diario_detallado[vendedor_username]['Tarjeta/Bold'] += tarjeta

        except Exception:
            pass 

    # 3. Crear el JSON de detalles que incluye el desglose general
    detalles_para_guardar = dict(informe_diario_detallado)
    detalles_para_guardar['GENERAL'] = {
        'Total_Efectivo': total_efectivo,
        'Nequi': total_pagos_electronicos['Nequi'],
        'Daviplata': total_pagos_electronicos['Daviplata'],
        'Transferencia': total_pagos_electronicos['Transferencia'],
        'Tarjeta/Bold': total_pagos_electronicos['Tarjeta/Bold']
    }
    
    total_electronico_sum = sum(total_pagos_electronicos.values())

    # 4. Crear y guardar el registro de cierre
    nuevo_cierre = CierreCaja(
        usuario_id=current_user.id,
        total_venta=total_diario,
        total_efectivo=total_efectivo,
        total_electronico=total_electronico_sum,
        detalles_json=json.dumps(detalles_para_guardar)
    )
    
    db.session.add(nuevo_cierre)
    db.session.commit()
    
    flash(f'✅ Cierre de Caja registrado exitosamente para el día {hoy.strftime("%d/%m/%Y")}. Total vendido: ${total_diario:,.0f}', 'success')
    return redirect(url_for('reportes'))


@app.route('/cierre_caja/historial')
@login_required
def historial_cierres():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('clientes'))
    
    cierres = CierreCaja.query.order_by(CierreCaja.fecha_cierre.desc()).all()
    return render_template('historial_cierres.html', cierres=cierres, timedelta=timedelta)


# ===================== RUTAS REPORTES =====================
@app.route('/reportes')
@login_required
def reportes():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('clientes'))

    hoy = date.today()
    inicio_mes = hoy.replace(day=1)

    total_diario = db.session.query(func.sum(Venta.total)).filter(func.date(Venta.fecha) == hoy).scalar() or 0

    inicio_semana = hoy - timedelta(days=hoy.weekday())
    total_semanal = db.session.query(func.sum(Venta.total)).filter(Venta.fecha >= inicio_semana).scalar() or 0

    total_mensual = db.session.query(func.sum(Venta.total)).filter(Venta.fecha >= inicio_mes).scalar() or 0

    informe_diario = db.session.query(
        Usuario.username.label('vendedor'),
        Venta.tipo_pago,
        func.sum(Venta.total).label('total_vendido')
    ).join(Usuario, Usuario.id == Venta.usuario_id
    ).filter(func.date(Venta.fecha) == hoy
    ).group_by(Usuario.username, Venta.tipo_pago).all()

    ventas_por_vendedor = db.session.query(
        Usuario.username.label('vendedor'),
        func.sum(Venta.total).label('total_vendido')
    ).join(Venta, Usuario.id == Venta.usuario_id
    ).filter(Venta.fecha >= inicio_mes
    ).group_by(Usuario.username
    ).order_by(func.sum(Venta.total).desc()).all()

    datos_vendedores = {'labels': [r.vendedor for r in ventas_por_vendedor],
                        'data': [float(r.total_vendido or 0) for r in ventas_por_vendedor]}

    informe_tendencia_semanal = db.session.query(
        func.strftime('%Y-%W', Venta.fecha).label('semana'),
        func.sum(Venta.total).label('total_vendido')
    ).filter(Venta.fecha >= inicio_mes
    ).group_by('semana'
    ).order_by('semana').all()

    def formatear_semana(semana_str):
        return 'Semana ' + semana_str.split('-')[1] if '-' in semana_str else semana_str

    datos_tendencia = {'labels': [formatear_semana(r.semana) for r in informe_tendencia_semanal],
                        'data': [float(r.total_vendido or 0) for r in informe_tendencia_semanal]}
                        
    caja_cerrada_hoy = CierreCaja.query.filter(func.date(CierreCaja.fecha_cierre) == hoy).first() is not None

    return render_template(
        'reportes.html',
        hoy=hoy,
        informe_diario=informe_diario,
        total_diario=total_diario,
        total_semanal=total_semanal,
        total_mensual=total_mensual,
        datos_tendencia=json.dumps(datos_tendencia),
        datos_vendedores=json.dumps(datos_vendedores),
        caja_cerrada_hoy=caja_cerrada_hoy
    )


# ===================== RUTAS USUARIOS =====================
@app.route('/usuarios')
@login_required
def usuarios():
    if current_user.rol.lower() != 'administrador':
        flash('Acceso denegado. Solo administradores pueden gestionar usuarios.', 'danger')
        return redirect(url_for('clientes'))
    usuarios_list = Usuario.query.all()
    return render_template('usuarios.html', usuarios=usuarios_list)


@app.route('/usuarios/agregar', methods=['POST'])
@login_required
def agregar_usuario():
    if current_user.rol.lower() != 'administrador':
        flash('Acceso denegado. Solo administradores pueden crear usuarios.', 'danger')
        return redirect(url_for('usuarios'))

    try:
        username = request.form.get('username').strip()
        nombre = request.form.get('nombre').strip() or 'Usuario Nuevo'
        apellido = request.form.get('apellido').strip() or 'Usuario Nuevo'
        cedula = request.form.get('cedula').strip() or '00000000'
        rol = request.form.get('rol').strip() or 'Vendedora'
        password = request.form.get('password')

        if not username or not password:
            raise ValueError("El usuario y la contraseña son obligatorios.")

        if Usuario.query.filter_by(username=username).first():
            raise ValueError("Ya existe un usuario con ese username.")
        if Usuario.query.filter_by(cedula=cedula).first():
            raise ValueError("Ya existe un usuario con esa cédula.")

        nuevo_usuario = Usuario(
            username=username,
            nombre=nombre,
            apellido=apellido,
            cedula=cedula,
            rol=rol
        )
        nuevo_usuario.set_password(password)
        db.session.add(nuevo_usuario)
        db.session.commit()
        flash(f'Usuario {nuevo_usuario.username} ({nuevo_usuario.rol}) creado correctamente.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error al crear usuario: {e}', 'danger')

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

            password = request.form.get('password')
            if password:
                usuario.set_password(password)

            db.session.commit()
            flash(f'Usuario {usuario.username} actualizado correctamente.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error al actualizar usuario: {e}', 'danger')

        return redirect(url_for('usuarios'))

    return render_template('editar_usuario.html', usuario=usuario)


@app.route('/usuarios/eliminar/<int:usuario_id>')
@login_required
def eliminar_usuario(usuario_id):
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden eliminar usuarios.', 'danger')
        return redirect(url_for('usuarios'))

    usuario_a_eliminar = Usuario.query.get_or_404(usuario_id)

    if usuario_a_eliminar.rol.lower() == 'administrador' and \
       Usuario.query.filter(Usuario.rol.ilike('administrador')).count() <= 1:
        flash('Debe haber al menos un administrador en el sistema.', 'danger')
        return redirect(url_for('usuarios'))

    db.session.delete(usuario_a_eliminar)
    db.session.commit()
    flash(f'Usuario {usuario_a_eliminar.username} eliminado correctamente.', 'success')
    return redirect(url_for('usuarios'))


# ===================== GESTIÓN DE VENTAS =====================
@app.route('/gestion_ventas')
@login_required
def gestion_ventas():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('clientes'))

    ventas_recientes = Venta.query.order_by(Venta.id.desc()).limit(30).all()
    
    # NUEVAS VARIABLES A PASAR (para el modal de edición):
    todos_los_clientes = Cliente.query.all()
    todos_los_vendedores = Usuario.query.all() # Todos los usuarios pueden ser vendedores aquí

    return render_template('gestion_ventas.html', 
                           VentasRecientes=ventas_recientes,
                           clientes_full=todos_los_clientes,
                           vendedores_full=todos_los_vendedores)


@app.route('/ventas/eliminar/<int:venta_id>')
@login_required
def eliminar_venta(venta_id):
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden anular ventas.', 'danger')
        return redirect(url_for('gestion_ventas'))

    venta = Venta.query.get_or_404(venta_id)

    try:
        detalles = VentaDetalle.query.filter_by(venta_id=venta.id).all()
        for detalle in detalles:
            producto = Producto.query.get(detalle.producto_id)
            if producto:
                producto.cantidad += detalle.cantidad

        VentaDetalle.query.filter_by(venta_id=venta.id).delete()
        db.session.delete(venta)
        db.session.commit()
        flash(f'Venta N° {venta_id} anulada correctamente. Stock devuelto al inventario.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error al anular la venta: {e}', 'danger')

    return redirect(url_for('gestion_ventas'))


# RUTA API: para obtener detalles de una venta por ID (usada por el modal)
@app.route('/api/ventas/detalle/<int:venta_id>', methods=['GET'])
@login_required
def api_detalle_venta(venta_id):
    if current_user.rol.lower() != 'administrador':
        return jsonify({'error': 'Permiso denegado'}), 403

    venta = Venta.query.get(venta_id)
    if not venta:
        return jsonify({'error': 'Venta no encontrada'}), 404

    detalles = VentaDetalle.query.filter_by(venta_id=venta_id).all()
    
    productos_vendidos = []
    for detalle in detalles:
        producto = Producto.query.get(detalle.producto_id)
        if producto:
            productos_vendidos.append({
                'id': producto.id, 
                'nombre': producto.nombre,
                'descripcion': producto.descripcion,
                'cantidad': detalle.cantidad,
                'precio_unitario': detalle.precio_unitario,
                'subtotal': detalle.subtotal
            })
            
    # Parseamos el detalle de pago para mostrarlo
    try:
        detalle_pago_json = json.loads(venta.detalle_pago)
    except:
        detalle_pago_json = {}

    return jsonify({
        'venta_id': venta.id,
        'fecha': (venta.fecha - timedelta(hours=5)).strftime('%d/%m/%Y %I:%M %p'),
        'total': venta.total,
        # IDs para preseleccionar en el modal:
        'cliente_id': venta.cliente_id, 
        'vendedor_id': venta.usuario_id, 
        'vendedor': venta.vendedor.username,
        'cliente': venta.comprador.nombre if venta.comprador else 'Contado / Genérico',
        'productos': productos_vendidos,
        'pagos': detalle_pago_json
    })


# RUTA: Edición de una venta (Solo Cliente/Vendedor)
@app.route('/ventas/editar_info/<int:venta_id>', methods=['POST'])
@login_required
def editar_informacion_venta(venta_id):
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('gestion_ventas'))
    
    venta = Venta.query.get_or_404(venta_id)
    
    try:
        cliente_id_form = request.form.get('cliente_id')
        vendedor_id_form = request.form.get('vendedor_id')
        
        venta.cliente_id = int(cliente_id_form)
        venta.usuario_id = int(vendedor_id_form)
        
        db.session.commit()
        flash(f'Información básica de la Venta N° {venta_id} actualizada.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al editar información de la venta: {e}', 'danger')
        
    return redirect(url_for('gestion_ventas'))


# RUTA API: Para obtener todos los productos (se usará en el modal para agregar)
@app.route('/api/productos/todos', methods=['GET'])
@login_required
def api_todos_los_productos():
    productos = Producto.query.all()
    lista_productos = []
    for p in productos:
        lista_productos.append({
            'id': p.id,
            'nombre': p.nombre,
            'descripcion': p.descripcion,
            'valor_venta': p.valor_venta,
            'cantidad_stock': p.cantidad 
        })
    return jsonify({'productos': lista_productos})


# RUTA API: Edición detallada de productos en una venta (Anulación/Adición)
@app.route('/api/ventas/detalle/editar/<int:venta_id>', methods=['POST'])
@login_required
def api_editar_detalle_venta(venta_id):
    if current_user.rol.lower() != 'administrador':
        return jsonify({'success': False, 'message': 'Permiso denegado.'}), 403

    venta = Venta.query.get(venta_id)
    if not venta:
        return jsonify({'success': False, 'message': 'Venta no encontrada.'}), 404

    try:
        data = request.get_json()
        productos_actualizados = data.get('productos', [])

        # 1. Recuperar los detalles de venta existentes para compararlos
        detalles_existentes = VentaDetalle.query.filter_by(venta_id=venta_id).all()
        productos_originales = {detalle.producto_id: detalle.cantidad for detalle in detalles_existentes}

        # 2. Eliminar todos los detalles existentes para re-insertar los nuevos
        VentaDetalle.query.filter_by(venta_id=venta_id).delete()
        
        nuevo_total_venta = 0.0
        productos_procesados = {}
        
        # 3. Procesar los detalles actualizados, calculando el nuevo total e insertando detalles
        for item in productos_actualizados:
            prod_id = int(item.get('id', 0))
            cantidad_nueva = int(item.get('cantidad', 0))
            precio_unitario = float(item.get('precio_unitario', 0))
            
            if cantidad_nueva <= 0:
                continue # Producto anulado (eliminado del detalle)

            producto = Producto.query.get(prod_id)
            if not producto:
                raise Exception(f"Producto con ID {prod_id} no encontrado.")

            subtotal = round(cantidad_nueva * precio_unitario, 2)
            nuevo_total_venta += subtotal
            
            # Crear y agregar el nuevo detalle
            nuevo_detalle = VentaDetalle(
                venta_id=venta_id,
                producto_id=prod_id,
                cantidad=cantidad_nueva,
                precio_unitario=precio_unitario,
                subtotal=subtotal
            )
            db.session.add(nuevo_detalle)
            
            productos_procesados[prod_id] = cantidad_nueva

        # 4. Ajustar el inventario (devoluciones y adiciones)
        productos_a_ajustar = set(productos_originales.keys()) | set(productos_procesados.keys())

        for prod_id in productos_a_ajustar:
            cantidad_original = productos_originales.get(prod_id, 0)
            cantidad_nueva = productos_procesados.get(prod_id, 0)
            
            # Positivo = Devuelto a stock (Anulación), Negativo = Sacado de stock (Adición)
            diferencia = cantidad_original - cantidad_nueva 
            
            producto = Producto.query.get(prod_id)
            if producto:
                producto.cantidad += diferencia
                
                # Verificación de stock para nuevas salidas/adiciones
                if diferencia < 0 and producto.cantidad < 0:
                    db.session.rollback()
                    return jsonify({'success': False, 'message': f"Stock insuficiente para la adición de {producto.nombre}. Cantidad final en stock: {producto.cantidad}"}), 400

        # 5. Actualizar el total de la Venta
        venta.total = nuevo_total_venta
        
        db.session.commit()
        return jsonify({'success': True, 'message': 'Detalle de venta actualizado correctamente. Inventario ajustado.', 'nuevo_total': nuevo_total_venta})

    except Exception as e:
        db.session.rollback()
        print(f"Error en edición detallada de venta: {e}")
        return jsonify({'success': False, 'message': f'Error al procesar la edición: {e}'}), 500


# =================================================================
# INICIALIZACIÓN (SOLO PARA DESARROLLO LOCAL)
# =================================================================
if __name__ == '__main__':
    # Esta sección solo se ejecuta cuando se corre `python app.py` localmente
    with app.app_context():
        # Puedes decidir si quieres que cree las tablas automáticamente en local:
        # db.create_all() 
        pass 
    app.run(debug=True)