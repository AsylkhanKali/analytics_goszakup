import os
import gzip
import shutil
from pathlib import Path

db_path = Path("data/goszakup.db")
db_gz_path = Path("db_parts/goszakup.db.gz")

parts = sorted(Path("db_parts").glob("goszakup.db.gz.part_*"))

if parts:
    print(f"Found {len(parts)} parts.")
    
    if db_path.exists() and db_path.stat().st_size > 100_000_000:
        print(f"Database {db_path} already exists (size: {db_path.stat().st_size} bytes). Skipping decompression.")
    else:
        print(f"Reassembling and decompressing to {db_path}...")
        
        Path("data").mkdir(parents=True, exist_ok=True)
        temp_gz = Path("data/temp.gz")
        
        with open(temp_gz, 'wb') as f_out:
            for part in parts:
                with open(part, 'rb') as f_in:
                    shutil.copyfileobj(f_in, f_out)
                    
        print("Reassembly complete. Decompressing...")
        
        if db_path.exists():
            db_path.unlink()
        if Path(str(db_path) + "-wal").exists():
            Path(str(db_path) + "-wal").unlink()
        if Path(str(db_path) + "-shm").exists():
            Path(str(db_path) + "-shm").unlink()
            
        with gzip.open(temp_gz, 'rb') as f_in:
            with open(db_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
                
        temp_gz.unlink()
        print("Database forcefully decompressed and updated successfully.")
elif db_gz_path.exists():
    if not db_path.exists() or db_gz_path.stat().st_mtime > db_path.stat().st_mtime:
        print(f"Decompressing {db_gz_path} to {db_path}...")
        with gzip.open(db_gz_path, 'rb') as f_in:
            with open(db_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        print("Database decompressed successfully.")
    else:
        print("Database already exists and is up to date.")
else:
    print("Compressed files not found.")
