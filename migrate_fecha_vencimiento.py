"""
Migración: agrega fecha_vencimiento a la tabla avisos en PostgreSQL.
Ejecutar una sola vez en Render via shell o como script de startup.
"""
import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL no encontrada")
    exit(1)

# Render usa postgres:// pero psycopg2 necesita postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True
cur = conn.cursor()

# Verificar si la columna ya existe
cur.execute("""
    SELECT column_name FROM information_schema.columns
    WHERE table_name = 'avisos' AND column_name = 'fecha_vencimiento'
""")
exists = cur.fetchone()

if exists:
    print("✓ Columna fecha_vencimiento ya existe, nada que hacer.")
else:
    cur.execute("ALTER TABLE avisos ADD COLUMN fecha_vencimiento DATE")
    print("✓ Columna fecha_vencimiento agregada correctamente.")

cur.close()
conn.close()
