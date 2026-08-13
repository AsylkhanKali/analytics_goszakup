import os
import gzip
import shutil
from pathlib import Path

db_path = Path("data/goszakup.db")
db_gz_path = Path("data/goszakup.db.gz")

if not db_path.exists() and db_gz_path.exists():
    print(f"Decompressing {db_gz_path} to {db_path}...")
    with gzip.open(db_gz_path, 'rb') as f_in:
        with open(db_path, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
    print("Database decompressed successfully.")
else:
    print("Database already exists or compressed file not found.")
