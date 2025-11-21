from app import app, db, Usuario
# CORRECCIÓN 1: La función correcta es con _hash
from werkzeug.security import generate_password_hash 

with app.app_context():
    print("Iniciando reparación de usuario admin...")
    
    # 1. Buscar si ya existe el usuario admin
    admin = Usuario.query.filter_by(username='admin').first()
    
    # Generamos la contraseña encriptada
    clave_encriptada = generate_password_hash('1234')

    if admin:
        print("🔄 Usuario 'admin' encontrado. Actualizando contraseña...")
        
        # CORRECCIÓN 2: En tu app.py la columna se llama 'password', no 'password_hash'
        admin.password = clave_encriptada 
        
        # Nos aseguramos que tenga los otros datos obligatorios
        admin.rol = 'Administrador'
        if not admin.cedula: admin.cedula = "10001"
        if not admin.nombre: admin.nombre = "Administrador"
        if not admin.apellido: admin.apellido = "General"
        
    else:
        print("➕ El usuario no existe. Creando desde cero con todos los datos...")
        admin = Usuario(
            username='admin',
            nombre='Administrador',       # Obligatorio
            apellido='General',           # Obligatorio
            cedula='10001',               # Obligatorio (Debe ser única)
            rol='Administrador',
            # CORRECCIÓN 3: Aquí también usamos 'password'
            password=clave_encriptada 
        )
        db.session.add(admin)

    try:
        db.session.commit()
        print("\n✅ ¡LISTO! Usuario configurado correctamente.")
        print("👤 Usuario: admin")
        print("🔑 Clave: 1234")
    except Exception as e:
        print(f"\n❌ Error al guardar en base de datos: {e}")