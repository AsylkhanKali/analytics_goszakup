import asyncio
import io
import os
import sqlite3
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

@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("web/templates/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), status_code=200)

@app.get("/api/analytics/{bin_code}")
def get_analytics(bin_code: str):
    if len(bin_code) != 12 or not bin_code.isdigit():
        raise HTTPException(status_code=400, detail="Invalid BIN/IIN")
    con = connect()
    try:
        analytics = get_bin_analytics(con, bin_code)
        return analytics
    finally:
        con.close()

@app.get("/api/contracts/{bin_code}")
def get_contracts(bin_code: str):
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            f"""
            SELECT 
                c.contract_number, c.contract_status, c.contract_amount, 
                c.supplier_name, c.supplier_bin, c.customer_name, c.customer_bin,
                c.lot_title, COALESCE(c.quantity, 1) as qty, COALESCE(c.unit_price, 0) as u_price,
                COALESCE(c.purchase_method, 'ОК') as p_meth
            FROM contracts c
            WHERE (c.supplier_bin = ? OR c.customer_bin = ? OR c.contract_number LIKE ? || '%')
              AND {EXCLUDE_STATUSES_SQL}
            ORDER BY c.contract_amount DESC LIMIT 10
            """,
            (bin_code, bin_code, bin_code),
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
            FROM contracts c
            WHERE (c.supplier_bin = ? OR c.customer_bin = ? OR c.contract_number LIKE ? || '%')
              AND {EXCLUDE_STATUSES_SQL}
              AND (c.brand_model != '' OR c.country != '' OR c.manufacturer != '')
            ORDER BY c.contract_amount DESC LIMIT 10
            """,
            (bin_code, bin_code, bin_code),
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
    active, status_msg = check_session_active()
    con = connect()
    try:
        try:
            specs_cnt = con.execute("SELECT COUNT(*) FROM supplier_specs").fetchone()[0]
        except sqlite3.OperationalError:
            specs_cnt = 0
        contracts_cnt = con.execute(f"SELECT COUNT(*) FROM contracts WHERE {EXCLUDE_STATUSES_SQL}").fetchone()[0]
    finally:
        con.close()
        
    return {
        "active": active,
        "message": status_msg,
        "contracts_count": contracts_cnt,
        "specs_count": specs_cnt
    }

class AuthRequest(BaseModel):
    ci_session: str

@app.post("/api/auth")
def authenticate(req: AuthRequest):
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/127.0.0.0 Safari/537.36"
    save_session(req.ci_session.strip(), ua)
    active, status_msg = check_session_active()
    return {"active": active, "message": status_msg}
