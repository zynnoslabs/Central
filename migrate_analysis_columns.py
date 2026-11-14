"""
Migración: agrega estilo_json y web_json a chat_conversaciones.
Ejecutar una sola vez en Render via shell.
"""
import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL no encontrada")
    exit(1)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True
cur = conn.cursor()

for col in ["estilo_json", "web_json"]:
    cur.execute(f"""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'chat_conversaciones' AND column_name = '{col}'
    """)
    if cur.fetchone():
        print(f"✓ Columna {col} ya existe.")
    else:
        cur.execute(f"ALTER TABLE chat_conversaciones ADD COLUMN {col} TEXT")
        print(f"✓ Columna {col} agregada.")

cur.close()
conn.close()
