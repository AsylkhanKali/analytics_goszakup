import asyncio
import io
import os
import sqlite3
import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from fastapi.requests import Request
from pydantic import BaseModel
from pathlib import Path

# Adjust path to import gz modules
import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))

from gz.auth import check_session_active, load_session, save_session
from gz.bin_analytics import get_bin_analytics, EXCLUDE_STATUSES_SQL
from gz.excel_export import generate_bin_excel
from gz.gap_filler import sync_bin_contracts_from_portal
from gz.store import connect

app = FastAPI(title="Goszakup Analytics Web")

# Mount static files
app.mount("/static", StaticFiles(directory="web/static"), name="static")

import subprocess
import sys
import threading
import traceback
from pathlib import Path

def run_db_setup():
    try:
        with open("setup_db.log", "w") as log_f:
            log_f.write("Running setup_db.py in background...\n")
            log_f.flush()
            result = subprocess.run([sys.executable, "setup_db.py"], capture_output=True, text=True, check=False)
            log_f.write(f"STDOUT:\n{result.stdout}\n")
            log_f.write(f"STDERR:\n{result.stderr}\n")
            log_f.write(f"Exit code: {result.returncode}\n")
            log_f.write("setup_db.py background setup finished.\n")
    except Exception as e:
        with open("setup_db.log", "a") as log_f:
            log_f.write(f"Failed to run setup_db.py: {e}\n{traceback.format_exc()}\n")

threading.Thread(target=run_db_setup, daemon=True).start()

def run_telegram_bot():
    lock_file = Path("/tmp/telegram_bot.lock")
    if lock_file.exists():
        return
    try:
        lock_file.touch()
        with open("bot.log", "w") as log_f:
            log_f.write("Starting telegram bot in background...\n")
            log_f.flush()
            subprocess.run([sys.executable, "bot.py"], stdout=log_f, stderr=subprocess.STDOUT)
            log_f.write("bot.py background process finished.\n")
    except Exception as e:
        with open("bot.log", "a") as log_f:
            log_f.write(f"Failed to run bot.py: {e}\n{traceback.format_exc()}\n")
    finally:
        if lock_file.exists():
            lock_file.unlink()

threading.Thread(target=run_telegram_bot, daemon=True).start()

STARTUP_TIME = datetime.datetime.now()

@app.get("/api/logs")
def get_logs():
    try:
        logs = ""
        if os.path.exists("setup_db.log"):
            with open("setup_db.log", "r") as f:
                logs += "=== setup_db.log ===\n" + f.read() + "\n"
        if os.path.exists("bot.log"):
            with open("bot.log", "r") as f:
                logs += "=== bot.log ===\n" + f.read() + "\n"
        return {"logs": logs or "No logs yet"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/health")
def health_check():
    uptime = datetime.datetime.now() - STARTUP_TIME
    return {
        "status": "ok",
        "uptime_seconds": uptime.total_seconds(),
        "uptime": str(uptime).split('.')[0],
        "server_time": datetime.datetime.now().isoformat()
    }

@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("web/templates/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), status_code=200)

@app.get("/api/analytics/{bin_code}")
def get_analytics(bin_code: str):
    if len(bin_code) != 12 or not bin_code.isdigit():
        raise HTTPException(status_code=400, detail="Invalid BIN/IIN")
    try:
        con = connect()
        try:
            analytics = get_bin_analytics(con, bin_code)
            return analytics
        finally:
            con.close()
    except Exception as e:
        import traceback
        raise HTTPException(status_code=500, detail=f"Database error: {e}\n{traceback.format_exc()}")

@app.get("/api/contracts/{bin_code}")
def get_contracts(bin_code: str):
    con = connect()
    try:
        cur = con.cursor()
        
        if len(bin_code) == 12 and bin_code.isdigit():
            where_clause = "(c.supplier_bin = ? OR c.customer_bin = ?)"
            params = (bin_code, bin_code)
        else:
            where_clause = "c.contract_number LIKE ? || '%'"
            params = (bin_code,)

        cur.execute(
            f"""
            SELECT 
                c.contract_number, c.contract_status, c.contract_amount, 
                c.supplier_name, c.supplier_bin, c.customer_name, c.customer_bin,
                c.lot_title, COALESCE(c.quantity, 1) as qty, COALESCE(c.unit_price, 0) as u_price,
                COALESCE(c.purchase_method, 'ОК') as p_meth
            FROM contracts_lots c
            WHERE {where_clause}
              AND {EXCLUDE_STATUSES_SQL}
            ORDER BY c.contract_amount DESC LIMIT 10
            """,
            params,
        )
        rows = cur.fetchall()
        
        contracts = []
        for r in rows:
            contracts.append({
                "contract_number": r[0],
                "status": r[1],
                "amount": r[2],
                "supplier_name": r[3],
                "supplier_bin": r[4],
                "customer_name": r[5],
                "customer_bin": r[6],
                "title": r[7],
                "qty": r[8],
                "unit_price": r[9],
                "method": r[10]
            })
        return {"contracts": contracts}
    finally:
        con.close()

@app.get("/api/specs/{bin_code}")
def get_specs(bin_code: str):
    con = connect()
    try:
        cur = con.cursor()

        if len(bin_code) == 12 and bin_code.isdigit():
            where_clause = "(c.supplier_bin = ? OR c.customer_bin = ?)"
            params = (bin_code, bin_code)
        else:
            where_clause = "c.contract_number LIKE ? || '%'"
            params = (bin_code,)

        cur.execute(
            f"""
            SELECT 
                c.lot_title as title,
                COALESCE(c.quantity, 1) as qty,
                c.contract_amount,
                c.brand_model as brand,
                c.country as country,
                c.manufacturer as manufacturer,
                c.customer_name, c.customer_bin, c.supplier_name, c.supplier_bin
            FROM contracts_lots c
            WHERE {where_clause}
              AND {EXCLUDE_STATUSES_SQL}
              AND (c.brand_model != '' OR c.country != '' OR c.manufacturer != '')
            ORDER BY c.contract_amount DESC LIMIT 20
            """,
            params,
        )
        rows = cur.fetchall()
        specs = []
        for r in rows:
            specs.append({
                "title": r[0],
                "qty": r[1],
                "amount": r[2],
                "brand": r[3],
                "country": r[4],
                "manufacturer": r[5],
                "customer_name": r[6],
                "customer_bin": r[7],
                "supplier_name": r[8],
                "supplier_bin": r[9],
            })
        return {"specs": specs}
    finally:
        con.close()

@app.post("/api/sync/{bin_code}")
async def sync_portal(bin_code: str):
    def run_sync():
        with connect() as con:
            return sync_bin_contracts_from_portal(con, bin_code)
    
    res = await asyncio.to_thread(run_sync)
    return res

@app.get("/api/excel/{bin_code}")
def download_excel(bin_code: str):
    con = connect()
    try:
        excel_stream = generate_bin_excel(con, bin_code)
        
        # Return as StreamingResponse
        return StreamingResponse(
            excel_stream, 
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=goszakup_{bin_code}.xlsx"}
        )
    finally:
        con.close()

@app.get("/api/status")
def get_status():
    try:
        active, status_msg = check_session_active()
        con = connect()
        try:
            try:
                specs_cnt = con.execute("SELECT COUNT(*) FROM supplier_specs").fetchone()[0]
            except sqlite3.OperationalError:
                specs_cnt = 0
            contracts_cnt = con.execute("SELECT COUNT(*) FROM contracts_lots").fetchone()[0]
        finally:
            con.close()
            
        return {
            "active": active,
            "message": status_msg,
            "contracts_count": contracts_cnt,
            "specs_count": specs_cnt
        }
    except Exception as e:
        return {
            "active": False,
            "message": f"Error: {e}\n{traceback.format_exc()}",
            "contracts_count": 0,
            "specs_count": 0
        }

class AuthRequest(BaseModel):
    ci_session: str

@app.post("/api/auth")
def authenticate(req: AuthRequest):
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/127.0.0.0 Safari/537.36"
    save_session(req.ci_session.strip(), ua)
    active, status_msg = check_session_active()
    return {"active": active, "message": status_msg}

# =========================================================================
# WEEKLY DATABASE AUTO-UPDATE ENDPOINTS (RENDER & CRON SUPPORT)
# =========================================================================
from weekly_updater import run_weekly_update, get_sync_status, resolve_default_db, get_latest_contract_date
from fastapi import BackgroundTasks, Query, Header

SYNC_TOKEN = os.getenv("SYNC_SECRET_TOKEN", "goszakup_secret_2026")
_CURRENT_SYNC_TASK = None

@app.get("/api/sync/status")
def sync_status_endpoint(db: str = Query(None, description="Custom database path")):
    target_db = db if db else resolve_default_db()
    return get_sync_status(target_db)

@app.post("/api/sync/weekly")
@app.get("/api/sync/weekly")
async def trigger_weekly_sync(
    background_tasks: BackgroundTasks,
    token: str = Query(None),
    x_sync_token: str = Header(None),
    concurrency: int = Query(30, ge=1, le=50),
    db: str = Query(None),
    force_start: str = Query(None),
    force_end: str = Query(None)
):
    # Optional security verification if token is configured
    auth_token = token or x_sync_token
    if os.getenv("REQUIRE_SYNC_TOKEN", "false").lower() in ("true", "1") and auth_token != SYNC_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid sync token")
        
    target_db = db if db else resolve_default_db()
    status = get_sync_status(target_db)
    
    if status["is_sync_running"]:
        return {
            "status": "already_running",
            "message": "A sync is already in progress",
            "latest_contract_date": status["latest_contract_date"]
        }
        
    latest_date = get_latest_contract_date(target_db)
    start_date_str = force_start if force_start else (str(latest_date) if latest_date else "2024-01-01")
    today_str = force_end if force_end else str(datetime.date.today())
    
    async def _async_sync_job():
        await run_weekly_update(
            db_path=target_db,
            concurrency=concurrency,
            force_start=force_start,
            force_end=force_end
        )
        
    background_tasks.add_task(_async_sync_job)
    
    return {
        "status": "started",
        "message": f"Weekly sync started in background for database '{target_db}'",
        "target_period": f"{start_date_str} -> {today_str}",
        "latest_contract_in_db": str(latest_date) if latest_date else None,
        "concurrency": concurrency
    }

