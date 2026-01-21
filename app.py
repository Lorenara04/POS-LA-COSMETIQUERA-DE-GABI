from flask import Flask, render_template, redirect, url_for, request, flash, abort, jsonify, send_file
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from flask_sqlalchemy import SQLAlchemy
import sqlalchemy
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
import pandas as pd  # Importado para manejo de Excel
from dotenv import load_dotenv
from openpyxl import Workbook

load_dotenv()  # carga .env automáticamente

# ✅ Envío de correo (usa el archivo enviar_correo.py / enviar_Correo.py)
#    IMPORTANTE: NO definas funciones SMTP dentro del template .html
from enviar_correo import enviar_correo_html

CORREO_INFORMES = os.getenv("CORREO_INFORMES", "johanna.chacon@outlook.es")

# =================================================================
# CONFIGURACIÓN Y BASE DE DATOS
# =================================================================
app = Flask(__name__)

# Configuración de Base de Datos
# -----------------------------------------------------------------
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "una_clave_secreta_por_defecto")

app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD")
app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", "587"))
app.config["MAIL_USE_TLS"] = os.getenv("MAIL_USE_TLS", "true").lower() == "true"

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'postgresql://la_cosmetiquera_de_gabi_user:z8vuwVK8rfm5S8CpZHZ3RITphvEolaqK@dpg-d48vb0i4d50c7391iap0-a.oregon-postgres.render.com/la_cosmetiquera_de_gabi'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

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


def rango_fechas_local_a_utc(fecha_inicio_str: str, fecha_fin_str: str):
    """
    Convierte un rango YYYY-MM-DD (seleccionado en UI) a un rango UTC.
    Interpreta el rango como días completos en Colombia (00:00:00 a 23:59:59).
    """
    fi_local = TIMEZONE_CO.localize(
        datetime.strptime(fecha_inicio_str, "%Y-%m-%d").replace(hour=0, minute=0, second=0)
    )
    ff_local = TIMEZONE_CO.localize(
        datetime.strptime(fecha_fin_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    )
    return fi_local.astimezone(pytz.UTC), ff_local.astimezone(pytz.UTC)

# =================================================================
def obtener_rango_turno_por_fecha_comercial(fecha_comercial: date):
    """
    Para una fecha comercial X:
    - inicia: X 06:00 Colombia
    - termina: (X+1) 05:59:59 Colombia
    """
    inicio_local = TIMEZONE_CO.localize(
        datetime.combine(fecha_comercial, time(6, 0, 0))
    )
    fin_local = inicio_local + timedelta(days=1) - timedelta(seconds=1)

    return (
        inicio_local.astimezone(pytz.UTC),
        fin_local.astimezone(pytz.UTC)
    )


def generar_cierre_para_fecha_si_no_existe(fecha_comercial: date, usuario_id: int):
    cierre_existente = CierreCaja.query.filter_by(
        fecha_cierre=fecha_comercial
    ).first()

    if cierre_existente:
        return True, f"Ya existía cierre para {fecha_comercial}"

    inicio_utc, fin_utc = obtener_rango_turno_por_fecha_comercial(fecha_comercial)

    ventas_turno = Venta.query.filter(
        and_(Venta.fecha >= inicio_utc, Venta.fecha <= fin_utc)
    ).all()

    total_venta = 0.0
    total_efectivo = 0.0
    detalle_metodos = defaultdict(float)
    detalle_vendedor = defaultdict(lambda: {"total": 0.0, "efectivo": 0.0})

    for v in ventas_turno:
        total_venta += float(v.total or 0)

        try:
            pagos = json.loads(v.detalle_pago or "{}")
            efectivo_v = float(pagos.get("Efectivo", 0) or 0)
            total_efectivo += efectivo_v

            for metodo, monto in pagos.items():
                if metodo not in [
                    "Ref_Codigo",
                    "Ref_Fecha",
                    "Efectivo_Recibido",
                    "Vuelto"
                ] and isinstance(monto, (int, float)):
                    detalle_metodos[metodo] += float(monto or 0)

            vendedor = (
                v.vendedor.username
                if getattr(v, "vendedor", None)
                else "N/A"
            )

            detalle_vendedor[vendedor]["total"] += float(v.total or 0)
            detalle_vendedor[vendedor]["efectivo"] += efectivo_v

        except Exception:
            pass

    total_electronico = total_venta - total_efectivo

    snapshot = {
        "metodos": dict(detalle_metodos),
        "vendedores": dict(detalle_vendedor),
        "hora_cierre_real": obtener_hora_colombia().strftime("%I:%M %p"),
        "auto": True,
        "motivo": "Cierre automático por turno pendiente"
    }

    nuevo = CierreCaja(
        fecha_cierre=fecha_comercial,
        hora_ejecucion=datetime.utcnow(),
        usuario_id=usuario_id,
        total_venta=total_venta,
        total_efectivo=total_efectivo,
        total_electronico=total_electronico,
        detalles_json=json.dumps(snapshot)
    )

    db.session.add(nuevo)
    db.session.commit()

    return True, f"Cierre automático creado para {fecha_comercial}"


def cerrar_turno_anterior_si_pendiente(usuario_id: int):
    ahora_co = obtener_hora_colombia()

    # Antes de las 6am NO se cierra nada
    if ahora_co.hour < 6:
        return True, "Turno aún activo (antes de las 6am)"

    fecha_comercial_hoy = ahora_co.date()
    fecha_comercial_ayer = fecha_comercial_hoy - timedelta(days=1)

    try:
        return generar_cierre_para_fecha_si_no_existe(
            fecha_comercial_ayer,
            usuario_id
        )
    except Exception as e:
        db.session.rollback()
        return False, str(e)

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
        locale.setlocale(locale.LC_ALL, 'es_CO.UTF-8')
        return "{:,.0f}".format(float(value)).replace(',', '.')
    except Exception:
        try:
            return f"{float(value):,.0f}".replace(',', '_').replace('.', ',').replace('_', '.')
        except Exception:
            return str(value)


@app.template_filter('from_json')
def from_json_filter(value):
    try:
        return json.loads(value)
    except Exception:
        return {}


@app.template_filter('fecha_co')
def fecha_colombia_filter(value):
    """Convierte UTC a Hora Colombia para mostrar en vistas"""
    if not value:
        return ""
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min)

    if isinstance(value, str):
        return value

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        value = pytz.utc.localize(value)

    return value.astimezone(TIMEZONE_CO).strftime('%d/%m/%Y %I:%M %p')


# =================================================================
# UTILIDADES
# =================================================================
def generar_barcode_base64(codigo: str | None) -> str:
    """
    Genera un barcode (Code128) como PNG en base64 para mostrar en templates.
    Retorna "" si no hay código o si ocurre un error.
    """
    if not codigo:
        return ""
    try:
        code128 = barcode.get_barcode_class('code128')
        instance = code128(str(codigo), writer=ImageWriter())
        buffer = BytesIO()
        instance.write(buffer)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as e:
        print("❌ Error generando barcode base64:", e)
        return ""


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

class AcumuladoMensual(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)  # 1-12

    total_venta = db.Column(db.Float, default=0)
    total_efectivo = db.Column(db.Float, default=0)
    total_electronico = db.Column(db.Float, default=0)

    fuente = db.Column(db.String(50), default="import_excel")  # opcional
    detalles_json = db.Column(db.Text)  # opcional

    __table_args__ = (
        db.UniqueConstraint('year', 'month', name='uq_acumulado_year_month'),
    )

# ===================== PROVEEDORES =====================

class Factura(db.Model):
    __tablename__ = "facturas"

    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(100), nullable=False)
    proveedor = db.Column(db.String(150), nullable=False)
    total = db.Column(db.Float, nullable=False)
    fecha = db.Column(db.Date, nullable=False, default=date.today)

    abonos = db.relationship(
        "Abono",
        backref="factura",
        cascade="all, delete-orphan",
        lazy=True
    )


class Abono(db.Model):
    __tablename__ = "abonos"

    id = db.Column(db.Integer, primary_key=True)
    factura_id = db.Column(db.Integer, db.ForeignKey("facturas.id", ondelete="CASCADE"), nullable=False)
    monto = db.Column(db.Float, nullable=False)
    medio_pago = db.Column(db.String(50), nullable=False)
    fecha = db.Column(db.Date, nullable=False, default=date.today)


# ===================== GASTOS =====================

class Gasto(db.Model):
    __tablename__ = "gastos"

    id = db.Column(db.Integer, primary_key=True)
    categoria = db.Column(db.String(100), nullable=False)
    concepto = db.Column(db.String(200), nullable=False)
    total = db.Column(db.Float, nullable=False)
    fecha = db.Column(db.Date, nullable=False, default=date.today)

    abonos = db.relationship(
        "AbonoGasto",
        backref="gasto",
        cascade="all, delete-orphan",
        lazy=True
    )


class AbonoGasto(db.Model):
    __tablename__ = "abonos_gastos"

    id = db.Column(db.Integer, primary_key=True)
    gasto_id = db.Column(db.Integer, db.ForeignKey("gastos.id", ondelete="CASCADE"), nullable=False)
    monto = db.Column(db.Float, nullable=False)
    medio_pago = db.Column(db.String(50), nullable=False)
    fecha = db.Column(db.Date, nullable=False, default=date.today)


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
    except OperationalError:
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

    try:
        productos_lista = Producto.query.all()
        valor_interno_total = sum((p.valor_interno or 0) * (p.cantidad or 0) for p in productos_lista)
        valor_venta_total = sum((p.valor_venta or 0) * (p.cantidad or 0) for p in productos_lista)
    except Exception:
        valor_interno_total = 0
        valor_venta_total = 0

    return render_template(
        'dashboard.html',
        current_user=current_user,
        productos_stock_bajo=productos_bajos,
        total_inventario=total_inventario,
        ventas_hoy=ventas_hoy,
        clientes_nuevos_mes=clientes_nuevos_mes,
        valor_interno_total=valor_interno_total,
        valor_venta_total=valor_venta_total
    )

#==================================================================
# -------------------- RUTAS CLIENTES --------------------
#==================================================================
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

#==================================================================
# -------------------- RUTAS INVENTARIO --------------------
#==================================================================
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

    try:
        productos_paginados = query.paginate(page=page, per_page=per_page, error_out=False)
    except OperationalError as e:
        flash(f'❌ Error de Base de Datos: La tabla de productos es inaccesible. Detalle: {e}', 'danger')
        productos_paginados = EmptyPagination()
    except Exception as e:
        flash(f'❌ Error de paginación o consulta de inventario: {e}', 'danger')
        productos_paginados = EmptyPagination()

    return render_template(
        'productos.html',
        productos_paginados=productos_paginados,
        search_query=search_query
    )

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

        if not request.form.get('nombre') or float(valor_venta_val) <= 0:
            flash('Error: El nombre y el valor de venta son obligatorios y deben ser positivos.', 'danger')
            return redirect(url_for('inventario'))

        nuevo_producto = Producto(
            codigo=codigo_producto,
            nombre=request.form.get('nombre'),
            descripcion=request.form.get('descripcion'),
            marca=request.form.get('marca', '').strip() or None,
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

        total_productos = Producto.query.count()
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


@app.route('/barcode/<int:producto_id>')
@login_required
def generar_barcode_api(producto_id):
    try:
        producto = Producto.query.get(producto_id)
        if not producto:
            return jsonify({"error": "Producto no encontrado"}), 404

        codigo = producto.codigo or str(producto.id)

        code128 = barcode.get_barcode_class('code128')
        instance = code128(codigo, writer=ImageWriter())

        buffer = BytesIO()
        instance.write(buffer)
        img64 = base64.b64encode(buffer.getvalue()).decode()

        return jsonify({
            "id": producto.id,
            "nombre": producto.nombre,
            "marca": producto.marca or "Sin marca",
            "codigo": codigo,
            "precio": producto.valor_venta,
            "barcode": img64
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------------- RUTAS DE CIERRE Y REPORTES --------------------

@app.route('/ejecutar_cierre_caja', methods=['POST'])
@login_required
def ejecutar_cierre_caja():
    if current_user.rol.lower() not in ['administrador', 'vendedora', 'administradora']:
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('reportes'))

    fecha_comercial, inicio_utc, fin_utc = obtener_rango_turno_colombia()
    cierre_existente = CierreCaja.query.filter_by(fecha_cierre=fecha_comercial).first()

    # 1. PROCESAR INGRESOS (VENTAS)
    ventas_turno = Venta.query.filter(and_(Venta.fecha >= inicio_utc, Venta.fecha <= fin_utc)).all()
    
    total_venta = 0.0
    total_efectivo = 0.0
    # Diccionario con nombres exactos para que el HTML los lea (Nequi, Daviplata, etc.)
    gen = {'Nequi':0.0, 'Daviplata':0.0, 'Transferencia':0.0, 'Tarjeta/Bold':0.0, 'Efectivo':0.0}
    vend_data = {}

    for v in ventas_turno:
        total_venta += v.total
        try:
            pagos = json.loads(v.detalle_pago)
            ef_v = float(pagos.get('Efectivo', 0) or 0)
            total_efectivo += ef_v
            
            for m in gen.keys():
                if m in pagos:
                    gen[m] += float(pagos[m] or 0)
            
            nom_v = v.vendedor.username if v.vendedor else "Sistema"
            if nom_v not in vend_data:
                vend_data[nom_v] = {'total_venta': 0.0, 'Efectivo': 0.0}
            
            vend_data[nom_v]['total_venta'] += float(v.total)
            vend_data[nom_v]['Efectivo'] += ef_v
        except:
            continue

    # 2. PROCESAR EGRESOS (GASTOS) - Para que dejen de salir en $0
    # Asegúrate que el modelo se llame Gasto en tu proyecto
    egresos_hoy = Gasto.query.filter(and_(Gasto.fecha >= inicio_utc, Gasto.fecha <= fin_utc)).all()
    tot_egresos = sum(float(e.total) for e in egresos_hoy)
    lista_egresos = [{'concepto':e.concepto, 'medio':e.medio_pago, 'monto':float(e.total)} for e in egresos_hoy]

    # 3. CONSOLIDAR SNAPSHOT (La clave para la imagen b6e2a0.png)
    snapshot = {
        'GENERAL': gen,
        'EGRESOS_TOTAL': tot_egresos,
        'EGRESOS_LISTA': lista_egresos,
        'hora_cierre_real': obtener_hora_colombia().strftime('%I:%M %p')
    }
    
    # Añadir vendedores al mismo nivel para evitar la "sopa" en la tabla
    for nom, datos in vend_data.items():
        snapshot[nom] = datos

    try:
        if cierre_existente:
            c = cierre_existente
            c.total_venta = total_venta
            c.total_efectivo = total_efectivo
            c.total_electronico = total_venta - total_efectivo
            c.detalles_json = json.dumps(snapshot)
            c.usuario_id = current_user.id
        else:
            nuevo = CierreCaja(
                fecha_cierre=fecha_comercial,
                total_venta=total_venta,
                total_efectivo=total_efectivo,
                total_electronico=total_venta - total_efectivo,
                usuario_id=current_user.id,
                detalles_json=json.dumps(snapshot),
                hora_ejecucion=datetime.utcnow()
            )
            db.session.add(nuevo)
        
        db.session.commit()
        flash('✅ Cierre de caja procesado correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {str(e)}', 'danger')

    return redirect(url_for('reportes'))

# --- RUTA DEL HISTORIAL ---
@app.route('/cierre_caja/historial')
@login_required
def historial_cierres():
    if current_user.rol.lower() not in ['administrador', 'vendedora', 'administradora']:
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('dashboard'))

    cierres = CierreCaja.query.order_by(CierreCaja.fecha_cierre.desc()).all()
    # Verifica que el archivo en templates sea exactamente historial_cierres.html
    return render_template('historial_cierres.html', cierres=cierres)

#==================================================================
# -------------------- RUTAS VENTAS --------------------
#==================================================================
@app.route('/ventas/nueva', methods=['GET', 'POST'])
@login_required
def nueva_venta():

    # 🔒 Cierre automático si ayer no se cerró
    ok, msg = cerrar_turno_anterior_si_pendiente(current_user.id)
    if not ok:
        flash(msg, 'warning')

    productos = Producto.query.filter(Producto.cantidad > 0).all()
    clientes = Cliente.query.all()

    if request.method == 'GET':
        return render_template('nueva_venta.html', productos=productos, clientes=clientes)

    if request.method == 'POST':
        try:
            total_venta = float(request.form.get('total_venta', 0) or 0)

            pago_efectivo_recibido = float(request.form.get('pago_efectivo', 0) or 0)
            pago_nequi = float(request.form.get('pago_nequi', 0) or 0)
            pago_transferencia = float(request.form.get('pago_transferencia', 0) or 0)
            pago_daviplata = float(request.form.get('pago_daviplata', 0) or 0)
            pago_tarjeta = float(request.form.get('pago_tarjeta', 0) or 0)

            total_electronico_pagado = pago_nequi + pago_transferencia + pago_daviplata + pago_tarjeta
            total_pagado = pago_efectivo_recibido + total_electronico_pagado

            if total_pagado + 0.0001 < total_venta:
                flash(
                    f'Error: El total pagado (${total_pagado:,.0f}) es menor al total de la venta (${total_venta:,.0f}).',
                    'danger'
                )
                return redirect(url_for('nueva_venta'))

            vuelto = max(0.0, total_pagado - total_venta)
            efectivo_neto = max(0.0, pago_efectivo_recibido - vuelto)

            detalle_pago_dict = {
                'Efectivo': efectivo_neto,
                'Efectivo_Recibido': pago_efectivo_recibido,
                'Vuelto': vuelto,
                'Nequi': pago_nequi,
                'Transferencia': pago_transferencia,
                'Daviplata': pago_daviplata,
                'Tarjeta/Bold': pago_tarjeta,
                'Ref_Codigo': request.form.get('codigo_transaccion', '').strip(),
                'Ref_Fecha': request.form.get('fecha_transaccion', '')
            }

            tipos_pagos = [
                k for k, v in detalle_pago_dict.items()
                if k not in ['Ref_Codigo', 'Ref_Fecha', 'Efectivo_Recibido', 'Vuelto']
                and float(v or 0) > 0
            ]
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
                    raise Exception(
                        f"Stock insuficiente para {producto.nombre}. Disponible: {producto.cantidad}, Solicitado: {cantidad_vendida}"
                    )

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
            if k not in ['Ref_Codigo', 'Ref_Fecha', 'Efectivo_Recibido', 'Vuelto'] and (isinstance(v, (int, float)) and v > 0):
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


#=========================REPORTES========================
@app.route('/reportes')
@login_required
def reportes():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('dashboard'))

    # Día comercial (6am CO -> 5:59:59am CO)
    fecha_comercial, inicio_utc, fin_utc = obtener_rango_turno_colombia()

    # ----------------------------
    # 1) Ventas de HOY (comercial)
    # ----------------------------
    ventas_hoy = Venta.query.filter(
        and_(Venta.fecha >= inicio_utc, Venta.fecha <= fin_utc)
    ).all()

    total_diario = sum(float(v.total or 0) for v in ventas_hoy)

    # ----------------------------
    # 2) Desglose pagos (tabla)
    # ----------------------------
    desglose_temp = defaultdict(float)
    for v in ventas_hoy:
        try:
            pagos = json.loads(v.detalle_pago or "{}")
            for metodo, valor in pagos.items():
                if metodo not in ['Ref_Codigo', 'Ref_Fecha', 'change', 'Efectivo_Recibido', 'Vuelto'] and isinstance(valor, (int, float)):
                    desglose_temp[metodo] += float(valor or 0)
        except Exception:
            pass

    informe_diario_list = [("General", metodo, total) for metodo, total in desglose_temp.items()]

    # ----------------------------
    # 3) Total mensual (mes calendario CO)
    # ----------------------------
    ahora_co = obtener_hora_colombia()
    inicio_mes_co = TIMEZONE_CO.localize(datetime(ahora_co.year, ahora_co.month, 1, 0, 0, 0))
    inicio_mes_utc = inicio_mes_co.astimezone(pytz.UTC)

    total_mensual = db.session.query(func.sum(Venta.total)).filter(
        Venta.fecha >= inicio_mes_utc
    ).scalar() or 0

    # ----------------------------
    # 4) Total semanal (semana CO desde lunes 00:00)
    # ----------------------------
    lunes_co = (ahora_co - timedelta(days=ahora_co.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if lunes_co.tzinfo is None:
        lunes_co = TIMEZONE_CO.localize(lunes_co)
    lunes_utc = lunes_co.astimezone(pytz.UTC)

    total_semanal = db.session.query(func.sum(Venta.total)).filter(
        Venta.fecha >= lunes_utc
    ).scalar() or 0

    # ----------------------------
    # 5) Datos gráficos: Ventas por vendedor (HOY)
    # ----------------------------
    rows_vend = db.session.query(
        Usuario.username,
        func.coalesce(func.sum(Venta.total), 0)
    ).join(Venta, Venta.usuario_id == Usuario.id).filter(
        and_(Venta.fecha >= inicio_utc, Venta.fecha <= fin_utc)
    ).group_by(Usuario.username).order_by(func.sum(Venta.total).desc()).all()

    datos_vendedores = {
        "labels": [u for (u, _) in rows_vend],
        "data": [float(t or 0) for (_, t) in rows_vend]
    }

    # ----------------------------
    # 6) Datos gráficos: Tendencia últimos 7 días comerciales
    # ----------------------------
    labels = []
    data = []
    # últimos 7 días comerciales (incluye hoy)
    for i in range(6, -1, -1):
        d = fecha_comercial - timedelta(days=i)
        di_utc, df_utc = obtener_rango_turno_por_fecha_comercial(d)

        total_dia = db.session.query(func.coalesce(func.sum(Venta.total), 0)).filter(
            and_(Venta.fecha >= di_utc, Venta.fecha <= df_utc)
        ).scalar() or 0

        labels.append(d.strftime("%d/%m"))
        data.append(float(total_dia))

    datos_tendencia = {"labels": labels, "data": data}

    # ----------------------------
    # 7) Caja cerrada hoy
    # ----------------------------
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

#================================================================

@app.route("/enviar-informe-rango", methods=["POST"])
@login_required
def enviar_informe_rango():
    if current_user.rol.lower() != 'administrador':
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))

    fecha_inicio = request.form.get("fecha_inicio")
    fecha_fin = request.form.get("fecha_fin")

    if not fecha_inicio or not fecha_fin:
        flash("Debes seleccionar ambas fechas.", "warning")
        return redirect(url_for("reportes"))

    # =========================
    # 1) RANGO SELECCIONADO (Colombia UI -> UTC)
    # =========================
    fi_utc, ff_utc = rango_fechas_local_a_utc(fecha_inicio, fecha_fin)

    ventas = Venta.query.filter(
        and_(Venta.fecha >= fi_utc, Venta.fecha <= ff_utc)
    ).all()

    # =========================
    # 2) MÉTRICAS BASE (PERIODO)
    # =========================
    total_ingresos = sum(v.total or 0 for v in ventas)
    total_dias = len(set(v.fecha.date() for v in ventas)) if ventas else 0
    promedio = (total_ingresos / total_dias) if total_dias else 0

    # Mejor día (por fecha)
    por_dia = {}
    for v in ventas:
        d = v.fecha.date()
        por_dia[d] = por_dia.get(d, 0) + (v.total or 0)

    if por_dia:
        mejor_dia_fecha = max(por_dia, key=por_dia.get).isoformat()
        mejor_dia_valor = por_dia[max(por_dia, key=por_dia.get)]
    else:
        mejor_dia_fecha = None
        mejor_dia_valor = 0

    # =========================
    # 3) TOTAL HOY (turno Colombia)
    # =========================
    _, inicio_hoy_utc, fin_hoy_utc = obtener_rango_turno_colombia()
    total_hoy = db.session.query(func.sum(Venta.total)).filter(
        and_(Venta.fecha >= inicio_hoy_utc, Venta.fecha <= fin_hoy_utc)
    ).scalar() or 0

    # =========================
    # 4) TOTAL SEMANA (semana actual Colombia: lunes 00:00 -> hoy)
    # =========================
    ahora_co = obtener_hora_colombia()
    lunes_co = (ahora_co - timedelta(days=ahora_co.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # ✅ Evita error tzinfo ya configurado
    if lunes_co.tzinfo is None:
        lunes_co = TIMEZONE_CO.localize(lunes_co)

    lunes_utc = lunes_co.astimezone(pytz.UTC)

    total_semana = db.session.query(func.sum(Venta.total)).filter(
        Venta.fecha >= lunes_utc
    ).scalar() or 0

    # =========================
    # 5) TOTAL POR VENDEDOR (del periodo)
    # =========================
    por_vendedor = db.session.query(
        Usuario.username,
        func.sum(Venta.total)
    ).join(Venta, Venta.usuario_id == Usuario.id).filter(
        and_(Venta.fecha >= fi_utc, Venta.fecha <= ff_utc)
    ).group_by(Usuario.username).order_by(func.sum(Venta.total).desc()).all()

    ventas_por_vendedor = [{"vendedor": u, "total": float(t or 0)} for (u, t) in por_vendedor]

    # =========================
    # 6) EFECTIVO vs ELECTRÓNICO + DESGLOSE (del periodo)
    # =========================
    pago_metodos = defaultdict(float)

    for v in ventas:
        try:
            pagos = json.loads(v.detalle_pago) if v.detalle_pago else {}
        except Exception:
            pagos = {}

        # Normalizamos posibles nombres
        # (tú puedes ajustar si tus llaves son distintas)
        efectivo = float(pagos.get("Efectivo", 0) or 0)

        nequi = float(pagos.get("Nequi", 0) or 0)
        daviplata = float(pagos.get("Daviplata", 0) or 0)
        transferencia = float(pagos.get("Transferencia", 0) or 0)

        tarjeta = 0.0
        if "Tarjeta/Bold" in pagos:
            tarjeta = float(pagos.get("Tarjeta/Bold", 0) or 0)
        elif "Tarjeta" in pagos:
            tarjeta = float(pagos.get("Tarjeta", 0) or 0)
        elif "Bold" in pagos:
            tarjeta = float(pagos.get("Bold", 0) or 0)

        pago_metodos["Efectivo"] += efectivo
        pago_metodos["Nequi"] += nequi
        pago_metodos["Daviplata"] += daviplata
        pago_metodos["Transferencia"] += transferencia
        pago_metodos["Tarjeta"] += tarjeta

    total_efectivo = pago_metodos["Efectivo"]
    total_electronico = pago_metodos["Nequi"] + pago_metodos["Daviplata"] + pago_metodos["Transferencia"] + pago_metodos["Tarjeta"]

    # =========================
    # =========================
    # 7) MES(ES) DENTRO DEL PERIODO + ACUMULADO DEL MES COMPLETO
    #    (Aunque el rango sea solo una semana, total_mes será del mes entero)
    # =========================
    meses = defaultdict(float)

    for v in ventas:
        dt = v.fecha
        # v.fecha normally está en UTC (a veces naive). Normalizamos:
        if dt and (dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None):
            dt = pytz.UTC.localize(dt)

        dt_co = dt.astimezone(TIMEZONE_CO)
        key = (dt_co.year, dt_co.month)
        meses[key] += float(v.total or 0)

    # Ordenados (detalle del rango, por si lo usas en template)
    meses_detalle = []
    for (y, m) in sorted(meses.keys()):
        meses_detalle.append({
            "year": int(y),
            "month": int(m),
            "total": float(meses[(y, m)] or 0)
        })

    meses_es = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
        7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
    }

    # Mes principal: el último mes presente en el periodo (más intuitivo)
    if meses_detalle:
        last = meses_detalle[-1]
        y = int(last["year"])
        m = int(last["month"])

        mes_label = f"{meses_es.get(m, m)} {y}"

        # ✅ ACUMULADO DEL MES COMPLETO (mes calendario en hora Colombia)
        inicio_mes_local = datetime(y, m, 1, 0, 0, 0)
        if inicio_mes_local.tzinfo is None:
            inicio_mes_local = TIMEZONE_CO.localize(inicio_mes_local)

        if m == 12:
            inicio_sig_mes_local = datetime(y + 1, 1, 1, 0, 0, 0)
        else:
            inicio_sig_mes_local = datetime(y, m + 1, 1, 0, 0, 0)

        if inicio_sig_mes_local.tzinfo is None:
            inicio_sig_mes_local = TIMEZONE_CO.localize(inicio_sig_mes_local)

        inicio_mes_utc = inicio_mes_local.astimezone(pytz.UTC)
        inicio_sig_mes_utc = inicio_sig_mes_local.astimezone(pytz.UTC)

        total_mes = db.session.query(func.sum(Venta.total)).filter(
            and_(Venta.fecha >= inicio_mes_utc, Venta.fecha < inicio_sig_mes_utc)
        ).scalar() or 0

    else:
        mes_label = "—"
        total_mes = 0


    # =========================
    # 8) Render de correo
    # =========================
    html = render_template(
        "informe_correo.html",

        # fechas
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,

        # totales
        total_hoy=total_hoy,
        total_semana=total_semana,
        total_ingresos=total_ingresos,  # total del periodo
        mes_label=mes_label,
        total_mes=total_mes,
        meses_detalle=meses_detalle,

        # pagos
        total_efectivo=total_efectivo,
        total_electronico=total_electronico,
        nequi=pago_metodos["Nequi"],
        daviplata=pago_metodos["Daviplata"],
        transferencia=pago_metodos["Transferencia"],
        tarjeta=pago_metodos["Tarjeta"],

        # performance
        promedio=promedio,
        mejor_dia_valor=mejor_dia_valor,
        mejor_dia_fecha=mejor_dia_fecha,
        total_dias=total_dias,

        # vendedor
        ventas_por_vendedor=ventas_por_vendedor
    )

    ok, err = enviar_correo_html(
        destinatarios=[CORREO_INFORMES],
        asunto=f"Informe de ventas ({fecha_inicio} a {fecha_fin})",
        html=html
    )

    if ok:
        flash("✅ Informe enviado correctamente.", "success")
    else:
        flash(f"⚠️ No se pudo enviar el correo. {err}", "warning")

    return redirect(url_for("reportes"))


# =================================================================
# EJECUCIÓN E INICIALIZACIÓN PARA PRODUCCIÓN (RENDER)
# =================================================================

# -------------------- RUTAS USUARIOS --------------------
@app.route('/usuarios')
@login_required
def usuarios():
    if current_user.rol.lower() != 'administrador':
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('dashboard'))
    try:
        usuarios_list = Usuario.query.all()
    except OperationalError as e:
        flash(f'Error de base de datos al cargar usuarios: {e}', 'danger')
        usuarios_list = []
    return render_template('usuarios.html', usuarios=usuarios_list)

@app.route('/usuarios/agregar', methods=['POST'])
@login_required
def agregar_usuario():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('usuarios'))
    try:
        username = (request.form.get('username') or '').strip()
        if not username:
            flash('El username es obligatorio.', 'danger')
            return redirect(url_for('usuarios'))

        if Usuario.query.filter_by(username=username).first():
            flash(f'Error: El usuario "{username}" ya existe.', 'danger')
            return redirect(url_for('usuarios'))

        nuevo_usuario = Usuario(
            username=username,
            nombre=(request.form.get('nombre') or '').strip(),
            apellido=(request.form.get('apellido') or '').strip(),
            cedula=(request.form.get('cedula') or '').strip(),
            rol=(request.form.get('rol') or 'Vendedora').strip()
        )
        nuevo_usuario.set_password(request.form.get('password') or '1234')
        db.session.add(nuevo_usuario)
        db.session.commit()
        flash(f'Usuario {username} creado.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Error: Cédula o nombre de usuario duplicado.', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al agregar usuario: {e}', 'danger')
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
            if pw:
                usuario.set_password(pw)
            db.session.commit()
            flash('Usuario actualizado.', 'success')
            return redirect(url_for('usuarios'))
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
        flash('Usuario eliminado.', 'success')
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
        return redirect(url_for('dashboard'))

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
        detalles = VentaDetalle.query.filter_by(venta_id=venta.id).all()
        for d in detalles:
            p = Producto.query.get(d.producto_id)
            if p:
                p.cantidad += d.cantidad
        VentaDetalle.query.filter_by(venta_id=venta.id).delete(synchronize_session='fetch')
        db.session.delete(venta)
        db.session.commit()
        flash(f'Venta {venta_id} anulada y stock recuperado.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al anular venta: {e}', 'danger')
    return redirect(url_for('gestion_ventas'))

# -------------------- IMPORTAR EXCEL (ADMIN) --------------------
@app.route('/importar')
@login_required
def vista_importar():
    if current_user.rol.lower() != 'administrador':
        flash('Acceso denegado.', 'danger')
        return redirect(url_for('dashboard'))
    return render_template('importar_datos.html')

@app.route('/admin/importar_productos', methods=['POST'])
@login_required
def importar_productos_excel():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado. Solo administradores pueden importar datos.', 'danger')
        return redirect(url_for('inventario'))

    if 'excel_file' not in request.files:
        flash('Error: No se encontró el archivo en la solicitud.', 'danger')
        return redirect(url_for('vista_importar'))

    file = request.files['excel_file']
    if not file or file.filename == '':
        flash('Error: Archivo no seleccionado.', 'danger')
        return redirect(url_for('vista_importar'))

    if not file.filename.endswith('.xlsx'):
        flash('Error: El archivo debe ser un Excel (.xlsx).', 'danger')
        return redirect(url_for('vista_importar'))

    try:
        excel_data = BytesIO(file.read())
        df_productos = pd.read_excel(excel_data, sheet_name='Producto')

        db.session.begin_nested()
        db.session.query(VentaDetalle).delete()
        db.session.query(Venta).delete()
        db.session.query(CierreCaja).delete()
        db.session.query(Producto).delete()
        db.session.commit()

        filas_importadas = 0
        for _, row in df_productos.iterrows():
            row_lower = {str(k).lower(): v for k, v in row.items()}
            if pd.isna(row_lower.get('nombre')) or pd.isna(row_lower.get('valor_venta')):
                continue

            def to_float(x, default=0.0):
                try:
                    return float(x)
                except Exception:
                    return default

            def to_int(x, default=0):
                try:
                    return int(x)
                except Exception:
                    return default

            nuevo_producto = Producto(
                codigo=str(row_lower.get('codigo')) if pd.notna(row_lower.get('codigo')) else None,
                nombre=str(row_lower.get('nombre')),
                descripcion=str(row_lower.get('descripcion')) if pd.notna(row_lower.get('descripcion')) else None,
                marca=str(row_lower.get('marca')) if pd.notna(row_lower.get('marca')) else None,
                cantidad=to_int(row_lower.get('cantidad'), 0),
                valor_venta=to_float(row_lower.get('valor_venta'), 0.0),
                valor_interno=to_float(row_lower.get('valor_interno'), 0.0),
                stock_minimo=to_int(row_lower.get('stock_minimo'), 5)
            )
            db.session.add(nuevo_producto)
            filas_importadas += 1

        db.session.commit()
        flash(f'✅ ¡Éxito! {filas_importadas} productos importados desde Excel (Hoja Producto).', 'success')
    except KeyError as e:
        db.session.rollback()
        flash(f'Error en el Excel: Columna/hoja no encontrada: {e}', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error grave al procesar el Excel: {e}', 'danger')

    return redirect(url_for('inventario'))


with app.app_context():
    try:
        locale.setlocale(locale.LC_ALL, 'es_CO.UTF-8')
    except locale.Error as le:
        try:
            locale.setlocale(locale.LC_ALL, 'C.UTF-8')
        except locale.Error:
            print(f"❌ Advertencia: Fallo al establecer el locale, usando el predeterminado. Error: {le}")

    try:
        db.create_all()
        print("✅ Tablas creadas (o verificadas) correctamente en PostgreSQL de Render.")

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
            db.session.flush()
            print("✅ Usuario admin creado: admin / admin123")

        generico = Cliente.query.get(1)
        if not generico:
            generico = Cliente(
                id=1,
                nombre='Contado / Genérico',
                telefono='N/A',
                direccion='N/A',
                email='N/A'
            )
            db.session.add(generico)
            db.session.flush()
            print("✅ Cliente genérico creado.")

        db.session.commit()

    except Exception as e:
        print(f"❌ ¡ERROR CRÍTICO DURANTE LA INICIALIZACIÓN DE DB!: {e}")
        print("Asegúrese de que la URL de la base de datos sea accesible.")
        db.session.rollback()
# ======================IMPORTAR CIERRE DE CAJA ===========================================
@app.route("/admin/importar_cierres_excel", methods=["POST"])
@login_required
def importar_cierres_excel():
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("reportes"))

    if "excel_file" not in request.files:
        flash("No se encontró el archivo en la solicitud.", "danger")
        return redirect(url_for("reportes"))

    file = request.files["excel_file"]
    if not file or file.filename == "":
        flash("Archivo no seleccionado.", "danger")
        return redirect(url_for("reportes"))

    if not file.filename.endswith(".xlsx"):
        flash("El archivo debe ser .xlsx", "danger")
        return redirect(url_for("reportes"))

    try:
        excel_data = BytesIO(file.read())
        df = pd.read_excel(excel_data, sheet_name="CierresCaja")

        requeridas = {"Fecha Cierre", "Total Venta", "Total Efectivo", "Total Electrónico"}
        if not requeridas.issubset(set(df.columns)):
            faltan = requeridas - set(df.columns)
            flash(f"Faltan columnas en CierresCaja: {', '.join(faltan)}", "danger")
            return redirect(url_for("reportes"))

        importados = 0

        for _, row in df.iterrows():
            fecha_raw = row.get("Fecha Cierre")

            # Fecha puede venir como datetime o string
            if pd.isna(fecha_raw):
                continue
            if isinstance(fecha_raw, datetime):
                fecha_cierre = fecha_raw.date()
            else:
                fecha_cierre = datetime.strptime(str(fecha_raw)[:10], "%Y-%m-%d").date()

            total_venta = float(row.get("Total Venta") or 0)
            total_efectivo = float(row.get("Total Efectivo") or 0)
            total_electronico = float(row.get("Total Electrónico") or 0)

            detalles = row.get("Detalles (JSON)")
            detalles = "" if pd.isna(detalles) else str(detalles)

            # Si ya existe cierre para esa fecha, actualiza; si no, crea
            cierre = CierreCaja.query.filter_by(fecha_cierre=fecha_cierre).first()
            if cierre:
                cierre.usuario_id = current_user.id
                cierre.total_venta = total_venta
                cierre.total_efectivo = total_efectivo
                cierre.total_electronico = total_electronico
                cierre.detalles_json = detalles
                cierre.hora_ejecucion = datetime.utcnow()
            else:
                cierre = CierreCaja(
                    fecha_cierre=fecha_cierre,
                    hora_ejecucion=datetime.utcnow(),
                    usuario_id=current_user.id,
                    total_venta=total_venta,
                    total_efectivo=total_efectivo,
                    total_electronico=total_electronico,
                    detalles_json=detalles
                )
                db.session.add(cierre)

            importados += 1

        db.session.commit()
        flash(f"✅ Cierres importados/actualizados: {importados}", "success")
        return redirect(url_for("historial_cierres"))

    except Exception as e:
        db.session.rollback()
        flash(f"Error importando cierres: {e}", "danger")
        return redirect(url_for("reportes"))
#=============================IMPORTAR ACUMULADOS DEL MES  ==================================
@app.route("/admin/importar_acumulado_mes_excel", methods=["POST"])
@login_required
def importar_acumulado_mes_excel():
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("reportes"))

    if "excel_file" not in request.files:
        flash("No se encontró el archivo en la solicitud.", "danger")
        return redirect(url_for("reportes"))

    file = request.files["excel_file"]
    if not file or file.filename == "":
        flash("Archivo no seleccionado.", "danger")
        return redirect(url_for("reportes"))

    if not file.filename.endswith(".xlsx"):
        flash("El archivo debe ser .xlsx", "danger")
        return redirect(url_for("reportes"))

    try:
        excel_data = BytesIO(file.read())
        df = pd.read_excel(excel_data, sheet_name="AcumuladosMes")

        requeridas = {"Año", "Mes", "Total Venta", "Total Efectivo", "Total Electrónico"}
        if not requeridas.issubset(set(df.columns)):
            faltan = requeridas - set(df.columns)
            flash(f"Faltan columnas en AcumuladosMes: {', '.join(faltan)}", "danger")
            return redirect(url_for("reportes"))

        importados = 0

        for _, row in df.iterrows():
            if pd.isna(row.get("Año")) or pd.isna(row.get("Mes")):
                continue

            year = int(row.get("Año"))
            month = int(row.get("Mes"))

            total_venta = float(row.get("Total Venta") or 0)
            total_efectivo = float(row.get("Total Efectivo") or 0)
            total_electronico = float(row.get("Total Electrónico") or 0)

            detalles = row.get("Detalles (JSON)")
            detalles = "" if pd.isna(detalles) else str(detalles)

            reg = AcumuladoMensual.query.filter_by(year=year, month=month).first()
            if reg:
                reg.total_venta = total_venta
                reg.total_efectivo = total_efectivo
                reg.total_electronico = total_electronico
                reg.detalles_json = detalles
            else:
                reg = AcumuladoMensual(
                    year=year,
                    month=month,
                    total_venta=total_venta,
                    total_efectivo=total_efectivo,
                    total_electronico=total_electronico,
                    detalles_json=detalles
                )
                db.session.add(reg)

            importados += 1

        db.session.commit()
        flash(f"✅ Acumulados mensuales importados/actualizados: {importados}", "success")
        return redirect(url_for("reportes"))

    except Exception as e:
        db.session.rollback()
        flash(f"Error importando acumulado mensual: {e}", "danger")
        return redirect(url_for("reportes"))

# -------------------- EXPORTAR EXCEL (ADMIN) --------------------

@app.route('/exportar_productos_excel')
@login_required
def exportar_productos_excel():
    if current_user.rol.lower() != 'administrador':
        flash('Permiso denegado.', 'danger')
        return redirect(url_for('inventario'))

    try:
        productos = Producto.query.order_by(Producto.id.asc()).all()

        data = []
        for p in productos:
            data.append({
                "ID": p.id,
                "Código": p.codigo,
                "Nombre": p.nombre,
                "Descripción": p.descripcion,
                "Marca": p.marca,
                "Cantidad": p.cantidad,
                "Valor Venta": p.valor_venta,
                "Valor Interno": p.valor_interno,
                "Stock Mínimo": p.stock_minimo
            })

        df = pd.DataFrame(data)

        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Productos')

        output.seek(0)

        nombre_archivo = f"productos_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        return send_file(
            output,
            as_attachment=True,
            download_name=nombre_archivo,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    except Exception as e:
        flash(f'Error al exportar productos: {e}', 'danger')
        return redirect(url_for('inventario'))
#= =============================EXPORTAR CIERRE DE CAJA ===================================
@app.route("/exportar_cierres_excel")
@login_required
def exportar_cierres_excel():
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("reportes"))

    # opcional: filtro por fechas (YYYY-MM-DD) desde query params
    fecha_inicio = request.args.get("fecha_inicio")  # "2026-01-01"
    fecha_fin = request.args.get("fecha_fin")        # "2026-01-31"

    q = CierreCaja.query.order_by(CierreCaja.fecha_cierre.asc())

    if fecha_inicio and fecha_fin:
        try:
            fi = datetime.strptime(fecha_inicio, "%Y-%m-%d").date()
            ff = datetime.strptime(fecha_fin, "%Y-%m-%d").date()
            q = q.filter(CierreCaja.fecha_cierre >= fi, CierreCaja.fecha_cierre <= ff)
        except Exception:
            flash("Fechas inválidas. Usa formato YYYY-MM-DD.", "warning")
            return redirect(url_for("reportes"))

    cierres = q.all()

    data = []
    for c in cierres:
        usuario_nombre = c.usuario.username if c.usuario else ""
        data.append({
            "ID": c.id,
            "Fecha Cierre": c.fecha_cierre.isoformat() if c.fecha_cierre else "",
            "Hora Ejecución (UTC)": c.hora_ejecucion.isoformat() if c.hora_ejecucion else "",
            "Usuario": usuario_nombre,
            "Total Venta": float(c.total_venta or 0),
            "Total Efectivo": float(c.total_efectivo or 0),
            "Total Electrónico": float(c.total_electronico or 0),
            "Detalles (JSON)": c.detalles_json or ""
        })

    df = pd.DataFrame(data)

    output = BytesIO()
    df.to_excel(output, index=False, sheet_name="CierresCaja")
    output.seek(0)

    return send_file(
        output,
        download_name="cierres_caja.xlsx",
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
#=================================EXPORTAR ACUMULADO ==================================
@app.route("/exportar_acumulados_excel")
@login_required
def exportar_acumulados_excel():
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("reportes"))

    fecha_inicio = request.args.get("fecha_inicio")
    fecha_fin = request.args.get("fecha_fin")

    if not fecha_inicio or not fecha_fin:
        flash("Debes enviar fecha_inicio y fecha_fin en la URL (YYYY-MM-DD).", "warning")
        return redirect(url_for("reportes"))

    try:
        fi_date = datetime.strptime(fecha_inicio, "%Y-%m-%d").date()
        ff_date = datetime.strptime(fecha_fin, "%Y-%m-%d").date()
    except Exception:
        flash("Fechas inválidas. Usa formato YYYY-MM-DD.", "warning")
        return redirect(url_for("reportes"))

    # Convertimos cada día comercial a su rango UTC (06:00 CO a 05:59 CO)
    rows = []
    dia = fi_date
    while dia <= ff_date:
        inicio_utc, fin_utc = obtener_rango_turno_por_fecha_comercial(dia)

        ventas = Venta.query.filter(and_(Venta.fecha >= inicio_utc, Venta.fecha <= fin_utc)).all()

        total_venta = 0.0
        total_efectivo = 0.0

        for v in ventas:
            total_venta += float(v.total or 0)
            try:
                pagos = json.loads(v.detalle_pago or "{}")
                total_efectivo += float(pagos.get("Efectivo", 0) or 0)
            except Exception:
                pass

        total_electronico = total_venta - total_efectivo

        rows.append({
            "Fecha Comercial": dia.isoformat(),
            "Total Venta": total_venta,
            "Total Efectivo": total_efectivo,
            "Total Electrónico": total_electronico,
            "Cantidad Ventas": len(ventas),
        })

        dia += timedelta(days=1)

    df = pd.DataFrame(rows)

    output = BytesIO()
    df.to_excel(output, index=False, sheet_name="Acumulados")
    output.seek(0)

    nombre_archivo = f"acumulados_{fecha_inicio}_a_{fecha_fin}.xlsx"

    return send_file(
        output,
        download_name=nombre_archivo,
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# =================================================================
# SECCIÓN MEJORADA: PROVEEDORES (CON MENSAJES FLASH) ✨
# =================================================================
@app.route("/proveedores", methods=["GET", "POST"])
@login_required
def proveedores():
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        numero = (request.form.get("numero") or "").strip()
        proveedor = (request.form.get("proveedor") or "").strip()
        total_raw = (request.form.get("total") or "0").strip()
        fecha_str = (request.form.get("fecha") or "").strip()

        if not numero or not proveedor:
            # MEJORA: En vez de abort(400), usamos flash para no romper la pagina
            flash("Error: Número de factura y Proveedor son obligatorios.", "danger")
            return redirect(url_for("proveedores"))

        try:
            total = float(total_raw)
        except ValueError:
            flash("Error: El total debe ser un número válido.", "danger")
            return redirect(url_for("proveedores"))

        try:
            fecha_factura = datetime.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else date.today()
        except Exception:
            fecha_factura = date.today()

        f = Factura(numero=numero, proveedor=proveedor, total=total, fecha=fecha_factura)
        db.session.add(f)
        db.session.commit()
        flash("Proveedor agregado exitosamente.", "success")
        return redirect(url_for("proveedores"))

    rows = db.session.query(
        Factura,
        func.coalesce(func.sum(Abono.monto), 0).label("abonado"),
        (Factura.total - func.coalesce(func.sum(Abono.monto), 0)).label("saldo"),
        func.max(Abono.fecha).label("fecha_ultimo_abono")
    ).outerjoin(Abono, Abono.factura_id == Factura.id) \
     .group_by(Factura.id) \
     .order_by(Factura.fecha.desc(), Factura.id.desc()) \
     .all()

    facturas = []
    for f, abonado, saldo, fecha_ultimo_abono in rows:
        facturas.append({
            "id": f.id,
            "numero": f.numero,
            "proveedor": f.proveedor,
            "total": float(f.total or 0),
            "abonado": float(abonado or 0),
            "saldo": float(saldo or 0),
            "fecha_factura": f.fecha,
            "fecha_ultimo_abono": fecha_ultimo_abono
        })

    abonos = db.session.query(Abono).order_by(Abono.fecha.asc(), Abono.id.asc()).all()
    return render_template("proveedores.html", facturas=facturas, abonos=abonos)

@app.route("/abonar/<int:factura_id>", methods=["POST"])
@login_required
def abonar(factura_id):
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))

    monto_raw = (request.form.get("monto") or "0").strip()
    medio = (request.form.get("medio") or "").strip()

    try:
        monto = float(monto_raw)
    except ValueError:
        flash("Monto inválido.", "danger")
        return redirect(url_for("proveedores"))

    if monto <= 0 or not medio:
        flash("El monto debe ser positivo y el medio de pago es obligatorio.", "danger")
        return redirect(url_for("proveedores"))

    factura = Factura.query.get(factura_id)
    if not factura:
        flash("Factura no encontrada.", "danger")
        return redirect(url_for("proveedores"))

    abonado = db.session.query(func.coalesce(func.sum(Abono.monto), 0)) \
        .filter(Abono.factura_id == factura_id).scalar() or 0

    saldo = float(factura.total or 0) - float(abonado or 0)
    
    # Tolerancia pequeña para errores de flotante
    if saldo <= 0.01:
        flash("Esta factura ya está pagada.", "warning")
        return redirect(url_for("proveedores"))

    if monto > saldo:
        monto = saldo

    db.session.add(Abono(factura_id=factura_id, monto=monto, medio_pago=medio, fecha=date.today()))
    db.session.commit()
    flash(f"Abono de ${monto:,.0f} registrado.", "success")
    return redirect(url_for("proveedores"))

@app.route("/eliminar_factura/<int:factura_id>", methods=["POST"])
@login_required
def eliminar_factura(factura_id):
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))
    factura = Factura.query.get_or_404(factura_id)
    db.session.delete(factura)
    db.session.commit()
    flash("Factura eliminada.", "success")
    return redirect(url_for("proveedores"))

@app.route("/editar_factura/<int:factura_id>", methods=["GET", "POST"])
@login_required
def editar_factura(factura_id):
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))

    factura = Factura.query.get_or_404(factura_id)

    if request.method == "POST":
        numero = (request.form.get("numero") or "").strip()
        proveedor = (request.form.get("proveedor") or "").strip()
        total_raw = (request.form.get("total") or "0").strip()
        fecha_str = (request.form.get("fecha") or "").strip()

        if not numero or not proveedor:
            flash("Número y proveedor son obligatorios.", "danger")
            return redirect(url_for("editar_factura", factura_id=factura_id))
            
        try:
            total = float(total_raw)
        except ValueError:
             flash("Total inválido.", "danger")
             return redirect(url_for("editar_factura", factura_id=factura_id))
        
        try:
            fecha_factura = datetime.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else date.today()
        except Exception:
            fecha_factura = date.today()

        abonado = db.session.query(func.coalesce(func.sum(Abono.monto), 0)) \
            .filter(Abono.factura_id == factura_id).scalar() or 0

        if total < float(abonado or 0):
            flash(f"Error: El nuevo total no puede ser menor que lo ya abonado (${float(abonado):,.0f}).", "danger")
            return redirect(url_for("editar_factura", factura_id=factura_id))

        factura.numero = numero
        factura.proveedor = proveedor
        factura.total = total
        factura.fecha = fecha_factura

        db.session.commit()
        flash("Factura actualizada correctamente.", "success")
        return redirect(url_for("proveedores"))

    return render_template("editar_factura.html", factura=factura)

#=================================================================
# SECCIÓN MEJORADA: GASTOS (CON MENSAJES FLASH) ✨
#=================================================================
@app.route("/gastos", methods=["GET", "POST"])
@login_required
def gastos():
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        categoria = (request.form.get("categoria") or "").strip()
        concepto = (request.form.get("concepto") or "").strip()
        total_raw = (request.form.get("total") or "0").strip()
        fecha_str = (request.form.get("fecha") or "").strip()

        if not categoria or not concepto:
            flash("Categoría y concepto son obligatorios.", "danger")
            return redirect(url_for("gastos"))

        try:
            total = float(total_raw)
        except ValueError:
            flash("Total inválido.", "danger")
            return redirect(url_for("gastos"))

        try:
            fecha_gasto = datetime.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else date.today()
        except Exception:
            fecha_gasto = date.today()

        g = Gasto(categoria=categoria, concepto=concepto, total=total, fecha=fecha_gasto)
        db.session.add(g)
        db.session.commit()
        flash("Gasto registrado exitosamente.", "success")
        return redirect(url_for("gastos"))

    rows = db.session.query(
        Gasto,
        func.coalesce(func.sum(AbonoGasto.monto), 0).label("abonado"),
        (Gasto.total - func.coalesce(func.sum(AbonoGasto.monto), 0)).label("saldo"),
        func.max(AbonoGasto.fecha).label("fecha_ultimo_abono")
    ).outerjoin(AbonoGasto, AbonoGasto.gasto_id == Gasto.id) \
     .group_by(Gasto.id) \
     .order_by(Gasto.fecha.desc(), Gasto.id.desc()) \
     .all()

    gastos_list = []
    for g, abonado, saldo, fecha_ultimo_abono in rows:
        gastos_list.append({
            "id": g.id,
            "categoria": g.categoria,
            "concepto": g.concepto,
            "total": float(g.total or 0),
            "abonado": float(abonado or 0),
            "saldo": float(saldo or 0),
            "fecha_gasto": g.fecha,
            "fecha_ultimo_abono": fecha_ultimo_abono
        })

    abonos_gastos = db.session.query(AbonoGasto).order_by(AbonoGasto.fecha.asc(), AbonoGasto.id.asc()).all()
    return render_template("modulo_gastos.html", gastos=gastos_list, abonos_gastos=abonos_gastos)


@app.route("/abonar_gasto/<int:gasto_id>", methods=["POST"])
@login_required
def abonar_gasto(gasto_id):
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))

    monto_raw = (request.form.get("monto") or "0").strip()
    medio = (request.form.get("medio") or "").strip()

    try:
        monto = float(monto_raw)
    except ValueError:
        flash("Monto inválido.", "danger")
        return redirect(url_for("gastos"))

    if monto <= 0 or not medio:
        flash("Monto positivo y medio de pago requeridos.", "danger")
        return redirect(url_for("gastos"))

    gasto = Gasto.query.get(gasto_id)
    if not gasto:
        flash("Gasto no encontrado.", "danger")
        return redirect(url_for("gastos"))

    abonado = db.session.query(func.coalesce(func.sum(AbonoGasto.monto), 0)) \
        .filter(AbonoGasto.gasto_id == gasto_id).scalar() or 0

    saldo = float(gasto.total or 0) - float(abonado or 0)
    
    if saldo <= 0.01:
        flash("Este gasto ya está pagado.", "warning")
        return redirect(url_for("gastos"))

    if monto > saldo:
        monto = saldo

    db.session.add(AbonoGasto(gasto_id=gasto_id, monto=monto, medio_pago=medio, fecha=date.today()))
    db.session.commit()
    flash(f"Abono de ${monto:,.0f} registrado.", "success")
    return redirect(url_for("gastos"))

@app.route("/eliminar_gasto/<int:gasto_id>", methods=["POST"])
@login_required
def eliminar_gasto(gasto_id):
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))
    gasto = Gasto.query.get_or_404(gasto_id)
    db.session.delete(gasto)
    db.session.commit()
    flash("Gasto eliminado.", "success")
    return redirect(url_for("gastos"))

@app.route("/editar_gasto/<int:gasto_id>", methods=["GET", "POST"])
@login_required
def editar_gasto(gasto_id):
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))

    gasto = Gasto.query.get_or_404(gasto_id)

    if request.method == "POST":
        categoria = (request.form.get("categoria") or "").strip()
        concepto = (request.form.get("concepto") or "").strip()
        total_raw = (request.form.get("total") or "0").strip()
        fecha_str = (request.form.get("fecha") or "").strip()

        if not categoria or not concepto:
            flash("Categoría y concepto son obligatorios.", "danger")
            return redirect(url_for("editar_gasto", gasto_id=gasto_id))

        try:
            total = float(total_raw)
        except ValueError:
            flash("Total inválido.", "danger")
            return redirect(url_for("editar_gasto", gasto_id=gasto_id))

        try:
            fecha_gasto = datetime.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else date.today()
        except Exception:
            fecha_gasto = date.today()

        abonado = db.session.query(func.coalesce(func.sum(AbonoGasto.monto), 0)) \
            .filter(AbonoGasto.gasto_id == gasto_id).scalar() or 0

        if total < float(abonado or 0):
             flash(f"Error: El nuevo total no puede ser menor que lo ya abonado (${float(abonado):,.0f}).", "danger")
             return redirect(url_for("editar_gasto", gasto_id=gasto_id))

        gasto.categoria = categoria
        gasto.concepto = concepto
        gasto.total = total
        gasto.fecha = fecha_gasto

        db.session.commit()
        flash("Gasto actualizado correctamente.", "success")
        return redirect(url_for("gastos"))

    return render_template("editar_gasto.html", gasto=gasto)

@app.route("/exportar_proveedores")
@login_required
def exportar_proveedores():
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))

    rows = db.session.query(
        Factura,
        func.coalesce(func.sum(Abono.monto), 0).label("abonado"),
        (Factura.total - func.coalesce(func.sum(Abono.monto), 0)).label("saldo"),
        func.max(Abono.fecha).label("fecha_ultimo_abono")
    ).outerjoin(Abono, Abono.factura_id == Factura.id) \
     .group_by(Factura.id) \
     .order_by(Factura.fecha.desc(), Factura.id.desc()) \
     .all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Proveedores"
    ws.append(["ID", "Factura", "Proveedor", "Total", "Abonado", "Saldo", "Fecha Factura", "Último Abono"])

    for f, abonado, saldo, ult in rows:
        ws.append([
            f.id,
            f.numero,
            f.proveedor,
            float(f.total or 0),
            float(abonado or 0),
            float(saldo or 0),
            f.fecha.isoformat() if f.fecha else "",
            ult.isoformat() if ult else ""
        ])

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="proveedores.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.route("/exportar_gastos")
@login_required
def exportar_gastos():
    if current_user.rol.lower() != "administrador":
        flash("Permiso denegado.", "danger")
        return redirect(url_for("dashboard"))

    rows = db.session.query(
        Gasto,
        func.coalesce(func.sum(AbonoGasto.monto), 0).label("abonado"),
        (Gasto.total - func.coalesce(func.sum(AbonoGasto.monto), 0)).label("saldo"),
        func.max(AbonoGasto.fecha).label("fecha_ultimo_abono")
    ).outerjoin(AbonoGasto, AbonoGasto.gasto_id == Gasto.id) \
     .group_by(Gasto.id) \
     .order_by(Gasto.fecha.desc(), Gasto.id.desc()) \
     .all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Gastos"
    ws.append(["ID", "Categoría", "Concepto", "Total", "Abonado", "Saldo", "Fecha Gasto", "Último Abono"])

    for g, abonado, saldo, ult in rows:
        ws.append([
            g.id,
            g.categoria,
            g.concepto,
            float(g.total or 0),
            float(abonado or 0),
            float(saldo or 0),
            g.fecha.isoformat() if g.fecha else "",
            ult.isoformat() if ult else ""
        ])

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="gastos.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.route('/api/venta/<int:venta_id>', methods=['GET'])
@login_required
def obtener_detalle_venta_api(venta_id):
    if current_user.rol.lower() != 'administrador':
        return jsonify({'error': 'No autorizado'}), 403

    venta = Venta.query.get_or_404(venta_id)
    detalles = VentaDetalle.query.filter_by(venta_id=venta.id).all()

    items_list = []
    for d in detalles:
        items_list.append({
            'producto_id': d.producto_id,
            'producto_nombre': d.producto.nombre if d.producto else 'Producto Eliminado',
            'cantidad': d.cantidad,
            'descripcion': d.producto.descripcion if d.producto else '',
            'precio_unitario': d.precio_unitario,
            'subtotal': d.subtotal
        })

    pagos = {}
    try:
        pagos = json.loads(venta.detalle_pago or '{}')
    except:
        pass

    data = {
        'id': venta.id,
        'fecha': venta.fecha.isoformat(),
        'vendedor_id': venta.usuario_id,
        'cliente_id': venta.cliente_id,
        'total': venta.total,
        'items': items_list,
        'pagos': pagos
    }
    return jsonify(data)

@app.route('/api/venta/editar_items/<int:venta_id>', methods=['POST'])
@login_required
def editar_items_venta(venta_id):
    if current_user.rol.lower() != 'administrador':
        return jsonify({'error': 'No autorizado'}), 403

    venta = Venta.query.get_or_404(venta_id)
    data = request.json
    nuevos_items = data.get('items', [])

    try:
        detalles_actuales = VentaDetalle.query.filter_by(venta_id=venta.id).all()
        for d in detalles_actuales:
            prod = Producto.query.get(d.producto_id)
            if prod:
                prod.cantidad += d.cantidad
        
        VentaDetalle.query.filter_by(venta_id=venta.id).delete()

        nuevo_total = 0.0
        for item in nuevos_items:
            pid = int(item['id'])
            cant = int(item['cantidad'])
            precio = float(item['precio'])
            sub = float(item['subtotal'])

            prod = Producto.query.get(pid)
            if not prod:
                raise Exception(f"Producto ID {pid} no existe")
            
            if prod.cantidad < cant:
                raise Exception(f"Stock insuficiente para {prod.nombre} (Hay {prod.cantidad})")
            
            prod.cantidad -= cant
            
            nuevo_det = VentaDetalle(
                venta_id=venta.id,
                producto_id=pid,
                cantidad=cant,
                precio_unitario=precio,
                subtotal=sub
            )
            db.session.add(nuevo_det)
            nuevo_total += sub

        venta.total = nuevo_total
        db.session.commit()
        return jsonify({'ok': True, 'nuevo_total': nuevo_total})

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/venta/editar_info/<int:venta_id>', methods=['POST'])
@login_required
def editar_info_venta(venta_id):
    if current_user.rol.lower() != 'administrador':
        return jsonify({'error': 'No autorizado'}), 403

    venta = Venta.query.get_or_404(venta_id)
    data = request.json

    try:
        venta.usuario_id = int(data.get('vendedor_id'))
        venta.cliente_id = int(data.get('cliente_id'))
        venta.detalle_pago = json.dumps(data.get('pagos', {}))
        
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/productos/buscar')
@login_required
def buscar_productos_api():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])

    productos = Producto.query.filter(
        (Producto.nombre.ilike(f'%{query}%')) |
        (Producto.codigo.ilike(f'{query}%'))
    ).limit(10).all()

    resultados = []
    for p in productos:
        resultados.append({
            'id': p.id,
            'codigo': p.codigo,
            'nombre': p.nombre,
            'descripcion': p.descripcion or '',
            'precio': p.valor_venta,
            'stock': p.cantidad
        })
    
    return jsonify(resultados)

if __name__ == "__main__":
    app.run(debug=True, port=5000)