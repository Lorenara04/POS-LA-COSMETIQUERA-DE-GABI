# app.py
# =================================================================
# 1. IMPORTS Y CONFIGURACIÓN
# =================================================================
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
from flask_mail import Mail, Message
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, render_template

# =================================================================
# 2. APP CONFIG & DATABASE
# =================================================================
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pos_cosmetiqueria.db'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))

db = SQLAlchemy(app) # Define la instancia de DB aquí

# LOGIN MANAGER
login_manager = LoginManager(app)
login_manager.login_view = 'login'

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))

# ----------------------------------------
# CONFIGURACIÓN DE CORREO (AJUSTA ESTOS VALORES)
# ----------------------------------------
app.config['MAIL_SERVER'] = 'smtp.gmail.com' # Corregido: Eliminado el espacio oculto U+00A0
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'lacomestiqueradegabi@outlook.com' # Correo del que se envía
app.config['MAIL_PASSWORD'] = 'Gabi52830265' # Contraseña o clave de app
app.config['ADMIN_EMAIL'] = 'johanna.chacon@outlook.es' # Correo de la administradora (DESTINATARIO)
# ----------------------------------------

mail = Mail(app)
# =================================================================
# AÑADIDO: CONFIGURACIÓN DE LOCALE Y FILTRO DE JINJA (para 'format_number')
# =================================================================
import locale

# Configurar el locale a español para el formato de moneda
try:
    locale.setlocale(locale.LC_ALL, 'es_ES.utf8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_ALL, 'es_ES')
    except locale.Error:
        pass # Ignorar si no se puede configurar

@app.template_filter('format_number')
def format_number_filter(value):
    """Formatea un número con separador de miles y dos decimales."""
    try:
        # Convierte el valor a float y luego formatea
        return locale.format_string("%.2f", float(value), grouping=True)
    except Exception:
        return value
        
# =================================================================
# 3. MODELOS (DEFINIDOS AQUÍ PARA EVITAR EL IMPORTERROR)
# ==============================================================
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

# =================================================================
# 4. FUNCIONES DE UTILIDAD E INYECCIÓN DE CONTEXTO
# =================================================================
@app.context_processor
def inject_global_data():
    from datetime import timedelta
    return dict(timedelta=timedelta)

def enviar_informe_ventas(periodo):
    with app.app_context():
        # 1. Obtener el rango de fechas y datos
        hoy = date.today()
        
        if periodo == 'semanal':
            # Informe Semanal: Últimos 7 días
            inicio = hoy - timedelta(days=7)
            asunto = f"📊 Informe Semanal de Ventas - {hoy.strftime('%d/%m/%Y')}"
            total = db.session.query(func.sum(Venta.total)).filter(Venta.fecha >= inicio).scalar() or 0
        
        elif periodo == 'mensual':
            # Informe Mensual: Mes actual
            inicio = hoy.replace(day=1)
            asunto = f"💰 Informe Mensual de Ventas - {hoy.strftime('%B %Y')}"
            total = db.session.query(func.sum(Venta.total)).filter(Venta.fecha >= inicio).scalar() or 0
        else:
            return

        # 2. Obtener más detalles (ej: total por vendedor para el periodo)
        ventas_vendedor = db.session.query(
            Usuario.username,
            func.sum(Venta.total)
        ).join(Venta, Usuario.id == Venta.usuario_id
        ).filter(Venta.fecha >= inicio
        ).group_by(Usuario.username).order_by(func.sum(Venta.total).desc()).all()

        # 3. Construir el cuerpo del mensaje
        cuerpo = f"""
        Hola Administradora,

        Adjunto los datos resumidos del {periodo} de ventas:
        
        TOTAL DE VENTAS {periodo.upper()}: ${"{:,.0f}".format(total)}

        ---
        Ventas por Vendedor:
        
        """
        for v, t in ventas_vendedor:
            cuerpo += f"- {v}: ${"{:,.0f}".format(t or 0)}\n"
        
        cuerpo += """
        ---
        Para ver el informe detallado y gráficos, por favor ingresa al sistema.
        """
        
        # 4. Enviar el correo
        msg = Message(asunto, sender=app.config['MAIL_USERNAME'], recipients=[app.config['ADMIN_EMAIL']])
        msg.body = cuerpo
        try:
            mail.send(msg)
            print(f"Correo de informe {periodo} enviado exitosamente.")
        except Exception as e:
            print(f"ERROR al enviar correo {periodo}: {e}")

# Inyecta la función generar_barcode_base64 en el contexto (la moví de antes)
def generar_barcode_base64(codigo):
    # ... (cuerpo de tu función de código de barras) ...
    pass

# =================================================================
#  FUNCIONES DE UTILIDAD (NO TIENE QUE VER CON EL CONTEXT_PROCESSOR)
# =================================================================
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

# =================================================================
# 5. RUTAS DE AUTENTICACIÓN Y DASHBOARD
# =================================================================
@login_manager.user_loader
def load_user(user_id):
    return Usuario.query.get(int(user_id))

@app.route('/')
def inicio():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard')) # Corregido: Eliminado el espacio oculto U+00A0
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard')) # Corregido: Eliminado el espacio oculto U+00A0
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = Usuario.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            # flash(f'¡Bienvenido, {user.username}!', 'success') # ELIMINADO para quitar el cuadro rosado en la esquina.
            return redirect(url_for('dashboard')) # Corregido: Eliminado el espacio oculto U+00A0
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
    """
    Renderiza el dashboard principal.
    AÑADIDO: Lógica para calcular y pasar las estadísticas al template.
    """
    # -----------------------------------------------------
    # LÓGICA AGREGADA PARA ESTADÍSTICAS DEL DASHBOARD
    # -----------------------------------------------------
    
    # 1. Productos con Stock Bajo
    try:
        # Consulta: Productos donde la cantidad actual es menor o igual al stock_minimo (default 5)
        productos_bajos = Producto.query.filter(Producto.cantidad <= Producto.stock_minimo).count()
    except Exception:
        productos_bajos = 0 # Valor por defecto en caso de error

    # 2. Ventas de Hoy
    try:
        hoy = datetime.now().date()
        # Suma los totales de las ventas de hoy (comparando solo la fecha)
        ventas_hoy_query = db.session.query(func.sum(Venta.total)).filter(func.date(Venta.fecha) == hoy).scalar()
        ventas_hoy = ventas_hoy_query if ventas_hoy_query is not None else 0.00
    except Exception:
        ventas_hoy = 0.00
        
    # 3. Clientes Nuevos del Mes
    try:
        inicio_mes = datetime.now().replace(day=1).date()
        # Cuenta los clientes registrados desde el inicio del mes
        clientes_nuevos_mes = Cliente.query.filter(func.date(Cliente.fecha_registro) >= inicio_mes).count()
    except Exception:
        clientes_nuevos_mes = 0

    # -----------------------------------------------------
    
    return render_template(
        'dashboard.html', 
        current_user=current_user,
        productos_stock_bajo=productos_bajos,
        ventas_hoy=ventas_hoy,
        clientes_nuevos_mes=clientes_nuevos_mes
    )
# =================================================================
# 6. RUTAS DE CLIENTES (CRUD y BÚSQUEDA)
# =================================================================
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

# =================================================================
# 7. RUTAS DE INVENTARIO (CRUD y BÚSQUEDA)
# =================================================================
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
        productos = Producto.query.all()
    return render_template('productos.html', productos=productos)

@app.route('/inventario/agregar', methods=['POST'])
@login_required
def agregar_producto():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden agregar productos.', 'danger')
        return redirect(url_for('inventario'))
    try:
        # CORRECCIÓN: Se usa 'or 0' para evitar error 'NoneType' si el campo está vacío
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

# =================================================================
# 8. RUTAS DE VENTAS Y COMPROBANTES (CON TRANSFERENCIA)
# =================================================================
@app.route('/ventas/nueva', methods=['GET', 'POST'])
@login_required
def nueva_venta():
    productos = Producto.query.all()
    clientes = Cliente.query.all()

    if request.method == 'GET':
        return render_template('nueva_venta.html', productos=productos, clientes=clientes)

    if request.method == 'POST':
        try:
            # 1. Conversión de valores monetarios (Robusto contra strings vacíos o None)
            total_venta = float(request.form.get('total_venta', 0) or 0)
            pago_efectivo = float(request.form.get('pago_efectivo', 0) or 0)
            pago_nequi = float(request.form.get('pago_nequi', 0) or 0)
            pago_transferencia = float(request.form.get('pago_transferencia', 0) or 0) 
            pago_daviplata = float(request.form.get('pago_daviplata', 0) or 0)
            pago_tarjeta = float(request.form.get('pago_tarjeta', 0) or 0)
            
            cod_transaccion = request.form.get('codigo_transaccion', '').strip()
            fecha_transaccion = request.form.get('fecha_transaccion', '')
            
            total_pagado = pago_efectivo + pago_nequi + pago_transferencia + pago_daviplata + pago_tarjeta

            # 2. Validación de Pago (uso de round() para precisión decimal)
            if round(total_pagado, 2) < round(total_venta, 2):
                flash('Error: El total pagado es menor al total de la venta.', 'danger')
                return redirect(url_for('nueva_venta'))

            # 3. Creación del diccionario de detalle de pagos
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

            # 4. Creación de la Venta Principal
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

            # 5. Procesamiento de los detalles de la venta
            productos_vendidos_json = request.form.get('productos_vendidos_json', '[]')
            try:
                productos_vendidos = json.loads(productos_vendidos_json)
            except json.JSONDecodeError as e:
                flash(f'Error de formato JSON en productos vendidos: {e}', 'danger')
                db.session.rollback()
                return redirect(url_for('nueva_venta'))
                
            for item in productos_vendidos:
                # Conversión segura a números
                item_id = int(item.get('id', 0))
                cantidad_vendida = int(item.get('cantidad', 0))
                precio_unitario = float(item.get('precio', 0))
                subtotal = float(item.get('subtotal', 0))
                producto = Producto.query.get(item_id)
                
                # 6. Validación de Stock y Decremento (Uso de INT() para la comparación)
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
                    # Manejo de error de stock
                    flash(f"Stock insuficiente para {producto.nombre if producto else 'desconocido'}. Cantidad solicitada: {cantidad_vendida}, disponible: {producto.cantidad if producto else 0}.", 'danger')
                    db.session.rollback()
                    return redirect(url_for('nueva_venta'))

            # 7. Commit Final
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
    
    # Agrupar productos (sin cambios)
    detalles_agrupados = defaultdict(lambda: {'cantidad': 0, 'subtotal': 0.0, 'precio_unitario': 0.0, 'producto': None})
    for d in detalles:
        pid = d.producto_id
        detalles_agrupados[pid]['cantidad'] += d.cantidad
        detalles_agrupados[pid]['subtotal'] += d.subtotal
        if detalles_agrupados[pid]['producto'] is None:
            detalles_agrupados[pid]['producto'] = d.producto
            detalles_agrupados[pid]['precio_unitario'] = d.precio_unitario
    detalles_finales = list(detalles_agrupados.values())
    
    # Cargar y normalizar el JSON de detalle de pago
    pagos_normalizados = {}
    try:
        detalle_pago_dict = json.loads(venta.detalle_pago) if venta.detalle_pago else {}
        
        first_value = next(iter(detalle_pago_dict.values()), None)

        if isinstance(first_value, dict):
            pagos_normalizados = {k: v for k, v in detalle_pago_dict.items() if isinstance(v, dict) and v.get('monto', 0) > 0}
        
        elif first_value is not None:
            ref_cod = detalle_pago_dict.get('Ref_Codigo', '')
            ref_fecha = detalle_pago_dict.get('Ref_Fecha', '')
            
            for k, v in detalle_pago_dict.items():
                if k not in ['Ref_Codigo', 'Ref_Fecha'] and (isinstance(v, (int, float)) and v > 0):
                    pagos_normalizados[k] = {'monto': v, 'cod': ref_cod, 'fecha': ref_fecha}

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
# =================================================================
# 9. RUTAS DE USUARIOS (CRUD SOLO ADMINISTRADOR)
# =================================================================
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
        flash('Acceso denegado. Solo administradores pueden eliminar usuarios.', 'danger')
        return redirect(url_for('usuarios'))

    usuario_a_eliminar = Usuario.query.get_or_404(usuario_id)

    # Evitar eliminar al único administrador
    if usuario_a_eliminar.rol.lower() == 'administrador' and \
       Usuario.query.filter(Usuario.rol.ilike('administrador')).count() <= 1:
        flash('Debe haber al menos un administrador en el sistema.', 'danger')
        return redirect(url_for('usuarios'))

    db.session.delete(usuario_a_eliminar)
    db.session.commit()
    flash(f'Usuario {usuario_a_eliminar.username} eliminado correctamente.', 'success')
    return redirect(url_for('usuarios'))

# =================================================================
## =================================================================
# 10. RUTAS DE REPORTES (Vista Consolidada y Gráficos)
# =================================================================
@app.route('/reportes')
@login_required
def reportes():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('clientes'))
    
    hoy = date.today()
    inicio_mes = hoy.replace(day=1)

    # 1. Totales Consolidados para Tarjetas
    total_diario = db.session.query(func.sum(Venta.total)).filter(func.date(Venta.fecha) == hoy).scalar() or 0
    
    inicio_semana = hoy - timedelta(days=hoy.weekday())
    total_semanal = db.session.query(func.sum(Venta.total)).filter(Venta.fecha >= inicio_semana).scalar() or 0
    
    total_mensual = db.session.query(func.sum(Venta.total)).filter(Venta.fecha >= inicio_mes).scalar() or 0

    # 2. Informe Diario Detallado (Para la tabla con scroll)
    informe_diario = db.session.query(
        Usuario.username.label('vendedor'),      
        Venta.tipo_pago, # Corregido: Eliminado el espacio oculto U+00A0
        func.sum(Venta.total).label('total_vendido')
    ).join(Usuario, Usuario.id == Venta.usuario_id
    ).filter(func.date(Venta.fecha) == hoy
    ).group_by(Usuario.username, Venta.tipo_pago).all()
    
    # 3. Gráfico 1: Ventas por Vendedor (Mensual)
    ventas_por_vendedor = db.session.query(
        Usuario.username.label('vendedor'),
        func.sum(Venta.total).label('total_vendido')
    ).join(Venta, Usuario.id == Venta.usuario_id
    ).filter(Venta.fecha >= inicio_mes
    ).group_by(Usuario.username
    ).order_by(func.sum(Venta.total).desc()).all()
    
    datos_vendedores = {'labels': [r.vendedor for r in ventas_por_vendedor],
                        'data': [float(r.total_vendido or 0) for r in ventas_por_vendedor]}

    # 4. Gráfico 2: Tendencia Semanal (Agrupado por Semana del Mes)
    # CRÍTICO: Usamos strftime('%Y-%W') para agrupar por semana
    informe_tendencia_semanal = db.session.query(
        func.strftime('%Y-%W', Venta.fecha).label('semana'),
        func.sum(Venta.total).label('total_vendido')
    ).filter(Venta.fecha >= inicio_mes
    ).group_by('semana'
    ).order_by('semana').all()

    def formatear_semana(semana_str):
        # Transforma 'YYYY-WW' a 'Semana WW' (Ej: 2025-45 -> Semana 45)
        return 'Semana ' + semana_str.split('-')[1]

    datos_tendencia = {'labels': [formatear_semana(r.semana) for r in informe_tendencia_semanal],
                       'data': [float(r.total_vendido or 0) for r in informe_tendencia_semanal]}
    
    return render_template(
        'reportes.html',
        hoy=hoy,
        informe_diario=informe_diario,
        total_diario=total_diario,
        total_semanal=total_semanal,
        total_mensual=total_mensual,
        datos_tendencia=json.dumps(datos_tendencia),
        datos_vendedores=json.dumps(datos_vendedores)
    )

# =================================================================
# 10.1 RUTA DEDICADA PARA LA GESTIÓN DE VENTAS (ANULAR/REVERSAR)
# =================================================================
@app.route('/gestion_ventas')
@login_required
def gestion_ventas():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('clientes'))
    
    # Consulta de Ventas Recientes (Para Anular/Reversar)
    ventas_recientes = Venta.query.order_by(Venta.id.desc()).limit(30).all() 
    
    # Necesitas el archivo templates/gestion_ventas.html
    return render_template('gestion_ventas.html', VentasRecientes=ventas_recientes)


# =================================================================
# 10.2 RUTA PARA ANULAR/REVERSAR VENTA (FUNCIÓN)
# =================================================================
@app.route('/ventas/eliminar/<int:venta_id>')
@login_required
def eliminar_venta(venta_id):
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden anular ventas.', 'danger')
        return redirect(url_for('gestion_ventas')) # CORRECCIÓN: Redirige a la nueva ruta

    venta = Venta.query.get_or_404(venta_id)
    
    try:
        # 1. Devolver el stock al inventario
        detalles = VentaDetalle.query.filter_by(venta_id=venta.id).all()
        for detalle in detalles:
            producto = Producto.query.get(detalle.producto_id)
            if producto:
                producto.cantidad += detalle.cantidad 

        # 2. Eliminar los detalles de la venta y la venta principal
        VentaDetalle.query.filter_by(venta_id=venta.id).delete()
        db.session.delete(venta)
        
        db.session.commit()
        
        flash(f'Venta N° {venta_id} anulada correctamente. Stock devuelto al inventario.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error al anular la venta: {e}', 'danger')

    return redirect(url_for('gestion_ventas')) # CORRECCIÓN: Redirige a la nueva ruta

# =================================================================
# =================================================================
# =================================================================
# 11. INICIALIZACIÓN DE LA APLICACIÓN
# =================================================================

# IMPORTANTE: Toda la lógica de creación de la base de datos y tareas programadas
# ha sido movida al archivo 'init_db.py' para el correcto despliegue con Gunicorn.

if __name__ == '__main__':
    # Esta línea SÓLO se ejecuta cuando corres 'python app.py' localmente.
    # Gunicorn la ignora, resolviendo el conflicto del servidor.
    app.run(debug=True)