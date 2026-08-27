import os
import gzip
import shutil
from pathlib import Path

db_path = Path("data/goszakup.db")
db_gz_path = Path("data/goszakup.db.gz")

if not db_path.exists():
    parts = sorted(Path("data").glob("goszakup.db.gz.part_*"))
    if parts:
        print(f"Found {len(parts)} parts. Reassembling and decompressing to {db_path}...")
        
        # We can stream directly from the parts through gzip to the output file
        # But gzip module doesn't natively support multiple files as one stream easily
        # So let's reassemble them into a temporary file first
        temp_gz = Path("data/temp.gz")
        with open(temp_gz, 'wb') as f_out:
            for part in parts:
                with open(part, 'rb') as f_in:
                    shutil.copyfileobj(f_in, f_out)
        
        print("Reassembly complete. Decompressing...")
        with gzip.open(temp_gz, 'rb') as f_in:
            with open(db_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
                
        temp_gz.unlink() # cleanup
        print("Database decompressed successfully.")
    elif db_gz_path.exists():
        print(f"Decompressing {db_gz_path} to {db_path}...")
        with gzip.open(db_gz_path, 'rb') as f_in:
            with open(db_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        print("Database decompressed successfully.")
    else:
        print("Compressed files not found.")
else:
    print("Database already exists or compressed file not found.")
