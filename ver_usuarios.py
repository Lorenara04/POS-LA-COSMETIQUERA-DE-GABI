from app import app, db, Usuario
import os

print(f"📂 Carpeta actual: {os.getcwd()}")

with app.app_context():
    # Imprimimos la ruta de la base de datos que Flask está usando
    print(f"🗄️ Base de datos configurada en: {app.config['SQLALCHEMY_DATABASE_URI']}")
    
    try:
        usuarios = Usuario.query.all()
        print(f"\n👥 TOTAL USUARIOS ENCONTRADOS: {len(usuarios)}")
        print("-" * 30)
        
        for u in usuarios:
            nombre = getattr(u, 'username', 'No tiene username')
            rol = getattr(u, 'rol', 'Sin rol')
            
            # --- CORRECCIÓN AQUÍ ---
            # Obtenemos el valor de forma segura (funciona si es 'password' o 'password_hash')
            clave_raw = getattr(u, 'password', None) or getattr(u, 'password_hash', None)
            
            print(f"🆔 ID: {u.id} | Usuario: '{nombre}' | Rol: {rol}")
            
            if clave_raw:
                print(f"🔑 Hash Clave (primeros 10): {clave_raw[:10]}...")
            else:
                print("⚠️ Hash Clave: [VACÍO / NULL]")
                
            print("-" * 30)

        if len(usuarios) == 0:
            print("⚠️ LA BASE DE DATOS ESTÁ VACÍA. Ejecuta crear_admin.py primero.")

    except Exception as e:
        print(f"❌ Error al leer la base de datos: {e}")
        print("Consejo: Si cambiaste el modelo, intenta borrar el archivo .db y reiniciar.")