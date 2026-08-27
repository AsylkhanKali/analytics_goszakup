import os
import shutil
import subprocess
from pathlib import Path

db_path = Path("data/goszakup.db")
db_path_tmp = Path("data/goszakup.db.tmp")
db_gz_path = Path("db_parts/goszakup.db.gz")

parts = sorted(Path("db_parts").glob("goszakup.db.gz.part_*"))

if parts:
    print(f"Found {len(parts)} parts.")
    
    if db_path.exists() and db_path.stat().st_size > 4_000_000_000:
        print(f"Database {db_path} already exists (size: {db_path.stat().st_size} bytes). Skipping decompression.")
    else:
        print(f"Streaming decompression from parts directly to {db_path_tmp}...")
        Path("data").mkdir(parents=True, exist_ok=True)
        
        # We can use cat and zcat to stream it directly and save disk space
        # cat db_parts/goszakup.db.gz.part_* | zcat > data/goszakup.db.tmp
        parts_str = " ".join([f'"{p}"' for p in parts])
        cmd = f"cat {parts_str} | gzip -d -c > {db_path_tmp}"
        
        try:
            subprocess.run(cmd, shell=True, check=True)
            print("Decompression successful! Renaming to final db path...")
            
            # Atomic replace
            os.replace(db_path_tmp, db_path)
            
            # Clean up WAL/SHM just in case
            if Path(str(db_path) + "-wal").exists():
                Path(str(db_path) + "-wal").unlink()
            if Path(str(db_path) + "-shm").exists():
                Path(str(db_path) + "-shm").unlink()
                
            print("Database updated successfully.")
        except subprocess.CalledProcessError as e:
            print(f"Extraction failed: {e}")
            if db_path_tmp.exists():
                db_path_tmp.unlink()
            
elif db_gz_path.exists():
    if not db_path.exists() or db_gz_path.stat().st_mtime > db_path.stat().st_mtime:
        print(f"Decompressing {db_gz_path} to {db_path_tmp}...")
        try:
            cmd = f"gzip -d -c {db_gz_path} > {db_path_tmp}"
            subprocess.run(cmd, shell=True, check=True)
            os.replace(db_path_tmp, db_path)
            print("Database decompressed successfully.")
        except subprocess.CalledProcessError as e:
            print(f"Extraction failed: {e}")
            if db_path_tmp.exists():
                db_path_tmp.unlink()
    else:
        print("Database already exists and is up to date.")
else:
    print("Compressed files not found.")
