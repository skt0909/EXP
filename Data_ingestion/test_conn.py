"""
test_connection.py — run this first to confirm Python can reach Postgres
"""
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL not found — check your .env file exists and is in the same folder.")

print(f"Connecting to: {DATABASE_URL.split('@')[-1]}")  # hides password in output

try:
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'ml' ORDER BY table_name;
        """))
        tables = [row[0] for row in result]

    print("Connection successful.")
    print(f"Found {len(tables)} tables in 'ml' schema:")
    for t in tables:
        print(f"  - {t}")

    if len(tables) < 7:
        print("\nWarning: expected 7 tables. Did the DDL script run fully?")

except Exception as e:
    print(f"Connection FAILED: {e}")