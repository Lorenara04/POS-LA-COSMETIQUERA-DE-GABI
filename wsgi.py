from app import app

# Este código se ejecuta una vez al iniciar gunicorn.
with app.app_context():
    from app import db, Usuario, Cliente # Importa los modelos y la DB

    # 1. Crea las tablas
    db.create_all()

    # 2. Lógica de creación de usuario admin/cliente genérico
    if Usuario.query.filter_by(username='admin').first() is None:
        admin = Usuario(nombre='Administrador Principal', username='admin', rol='Administrador', cedula='00000000')
        admin.set_password('1234')
        db.session.add(admin)

    if Cliente.query.filter_by(nombre='Contado / Genérico').first() is None:
        cliente_generico = Cliente(nombre='Contado / Genérico', telefono='N/A', direccion='N/A', email='contacto@local.com')
        db.session.add(cliente_generico)

    db.session.commit()

# Aquí puedes dejar la lógica del scheduler si quieres que inicie con Gunicorn
# PERO, APScheduler no es ideal para Render (ver nota abajo).

# Exporta la app para que gunicorn la encuentre
# El archivo wsgi.py ya existe y sólo debe exportar la app.
# El Procfile debe apuntar aquí.
# El comando gunicorn debe ser: gunicorn wsgi:app