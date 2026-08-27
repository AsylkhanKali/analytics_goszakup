import subprocess
import datetime
import calendar
import argparse
import sys
import os
from dateutil.relativedelta import relativedelta

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, default="2024-03-01")
    parser.add_argument("--end", type=str, default="2026-08-31")
    args = parser.parse_args()
    
    start_date = datetime.datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.datetime.strptime(args.end, "%Y-%m-%d").date()
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    python_bin = os.path.join(script_dir, "venv", "bin", "python3")
    scraper_bin = os.path.join(script_dir, "scraper_zcp.py")
    if not os.path.exists(python_bin):
        python_bin = sys.executable

    current = start_date
    while current <= end_date:
        next_month = current + relativedelta(months=1)
        month_end = next_month - relativedelta(days=1)
        
        start_str = current.strftime("%Y-%m-%d")
        end_str = month_end.strftime("%Y-%m-%d")
        
        print(f"\n=======================================================")
        print(f"Running ZCP Scraper for FULL MONTH: {start_str} to {end_str}")
        print(f"=======================================================\n", flush=True)
        
        subprocess.run([python_bin, "-u", scraper_bin, "--start", start_str, "--end", end_str], check=True, cwd=script_dir)
        
        current = next_month

if __name__ == "__main__":
    main()
