"""
Run ONCE to add receipt storage columns to the subscriptions table.

Usage (from pcil-backend folder with venv active):
    python add_receipt_columns.py
"""
import os, sys

# Load .env manually
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

db_url = os.environ.get('DATABASE_URL', '')
if not db_url:
    print("ERROR: DATABASE_URL not found in .env")
    sys.exit(1)

# Convert asyncpg URL to psycopg2 URL
db_url = db_url.replace('postgresql+asyncpg://', 'postgresql://')
db_url = db_url.replace('postgresql+psycopg2://', 'postgresql://')

try:
    import psycopg2
except ImportError:
    print("psycopg2 not found — trying psycopg2-binary...")
    os.system(f"{sys.executable} -m pip install psycopg2-binary --quiet")
    import psycopg2

conn = psycopg2.connect(db_url)
conn.autocommit = True
cur = conn.cursor()

columns = [
    ("receipt_data",      "TEXT"),
    ("receipt_filename",  "VARCHAR(255)"),
    ("receipt_mime_type", "VARCHAR(100)"),
]

for col, coltype in columns:
    sql = f"ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS {col} {coltype}"
    cur.execute(sql)
    print(f"  ✓ {sql}")

cur.close()
conn.close()
print("\nMigration complete. Restart the backend.")
