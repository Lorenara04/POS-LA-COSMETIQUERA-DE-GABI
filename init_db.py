# init_db.py

from app import app, db, Usuario, Producto, Cliente, Venta, VentaDetalle

def inicializar_base_datos():
    print("--- INICIALIZACIÓN DE BASE DE DATOS ---")
    
    # Crear todas las tablas
    print("Creando tablas de la base de datos...")
    db.create_all()
    print("Tablas creadas.")

    # Crear usuario administrador inicial si no existe
    if not Usuario.query.filter_by(username='admin').first():
        print("Creando usuario administrador inicial...")
        admin = Usuario(
            username='admin',
            nombre='Admin',
            apellido='Principal',
            cedula='0000',
            rol='Administrador'
        )
        admin.set_password('1234')
        db.session.add(admin)
        db.session.commit()
        print("Administrador 'admin' creado con contraseña '1234'.")
    
    # Crear cliente genérico si no existe
    if not Cliente.query.filter_by(nombre='Contado / Genérico').first():
        cliente_gen = Cliente(
            nombre='Contado / Genérico',
            telefono='',
            direccion='',
            email=''
        )
        db.session.add(cliente_gen)
        db.session.commit()
        print("Cliente genérico creado.")

    print("Base de datos inicializada correctamente.")

if __name__ == '__main__':
    with app.app_context():
        inicializar_base_datos()
