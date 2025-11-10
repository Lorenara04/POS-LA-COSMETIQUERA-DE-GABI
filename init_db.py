# init_db.py

from app import app, db, Usuario, Producto, Cliente, Venta, VentaDetalle
import os # Asegura que esta importación exista

# --- LÓGICA DE DETECCIÓN DE BASE DE DATOS EXISTENTE ---
DB_PATH = os.path.join('/data', 'pos_cosmetiqueria.db')
DB_DIR = '/data'

def inicializar_base_datos():
    # Solo inicializa si el archivo NO existe o el disco está vacío
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
        print("--- BASE DE DATOS EXISTENTE DETECTADA. OMITIENDO INICIALIZACIÓN ---")
        return # Si existe y tiene datos, NO HACER NADA

    # Si llega aquí, es porque el archivo no existe o está vacío.

    print("--- INICIALIZACIÓN DE BASE DE DATOS ---")
    
    # Crear todas las tablas
    print("Creando tablas de la base de datos...")
    db.create_all()
    print("Tablas creadas.")

    # ... (El resto de tu código para crear usuario admin y cliente genérico) ...

    print("Base de datos inicializada correctamente.")

if __name__ == '__main__':
    # 1. Asegura que el directorio exista (esto es redundante, pero seguro)
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR) 

    # 2. Ejecuta la lógica de inicialización
    with app.app_context():
        inicializar_base_datos()