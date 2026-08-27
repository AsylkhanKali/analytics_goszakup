import subprocess
import datetime
from dateutil.relativedelta import relativedelta

start = datetime.date(2024, 3, 1)
end = datetime.date(2026, 8, 31)

current = start
while current <= end:
    next_month = current + relativedelta(months=1)
    month_end = next_month - relativedelta(days=1)
    
    start_str = current.strftime("%Y-%m-%d")
    end_str = month_end.strftime("%Y-%m-%d")
    
    print(f"Running scraper for {start_str} to {end_str}")
    subprocess.run(["./venv/bin/python3", "scraper.py", "--start", start_str, "--end", end_str])
    
    current = next_month
