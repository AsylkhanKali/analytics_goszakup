#!/usr/bin/env python3
"""
Goszakup Autonomous Weekly Scheduler Daemon
-------------------------------------------
Runs continuously as a background daemon process.
Automatically triggers the weekly database update every Sunday at 23:00 (11:00 PM),
or on a custom configurable schedule.
"""

import os
import sys
import time
import datetime
import schedule
import argparse
import asyncio
from weekly_updater import run_weekly_update, get_sync_status, resolve_default_db

def job(db_path: str, concurrency: int):
    print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ⏰ Scheduled weekly update triggered!", flush=True)
    asyncio.run(run_weekly_update(db_path=db_path, concurrency=concurrency))

def main():
    parser = argparse.ArgumentParser(description="Goszakup Weekly Update Daemon")
    parser.add_argument("--day", type=str, default="sunday", help="Day of week to run (default: sunday)")
    parser.add_argument("--time", type=str, default="23:00", help="Time of day to run in HH:MM (default: 23:00 / 11 PM)")
    parser.add_argument("--concurrency", type=int, default=30, help="Concurrency level (default: 30)")
    parser.add_argument("--db", type=str, default=None, help="Custom database path")
    parser.add_argument("--run-now", action="store_true", help="Run an immediate update now before entering schedule loop")
    args = parser.parse_args()

    db_path = args.db if args.db else resolve_default_db()
    
    print("=" * 60)
    print("🚀 GOSZAKUP AUTONOMOUS WEEKLY DAEMON STARTED")
    print(f"Target Database: {db_path}")
    print(f"Schedule: Every {args.day.capitalize()} at {args.time}")
    print(f"Concurrency: {args.concurrency} workers")
    
    status = get_sync_status(db_path)
    print(f"Current Database State: {status['total_contracts']:,} contracts, {status['total_lots']:,} lots")
    print(f"Latest Contract in DB: {status['latest_contract_date']}")
    print("=" * 60, flush=True)

    if args.run_now:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Executing immediate run as requested...", flush=True)
        job(db_path, args.concurrency)

    # Configure schedule
    day_lower = args.day.lower()
    sched_obj = getattr(schedule.every(), day_lower, schedule.every().sunday)
    sched_obj.at(args.time).do(job, db_path=db_path, concurrency=args.concurrency)
    
    print(f"\n[DAEMON] Waiting for next scheduled run (Every {args.day.capitalize()} at {args.time})...\n", flush=True)
    
    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            print("\n[DAEMON] Stopped by user.")
            break
        except Exception as e:
            print(f"[DAEMON ERROR] {e}", flush=True)
            time.sleep(30)

if __name__ == "__main__":
    main()
