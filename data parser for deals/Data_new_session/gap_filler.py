import asyncio
import aiohttp
from bs4 import BeautifulSoup
import sqlite3
import re

DB_PATH = 'data/goszakup.db'
ALLOWED_STATUSES = ['Исполнен', 'Действует', 'В работе', 'Подписан']

def save_lots(lots):
    if not lots: return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executemany('''
        INSERT INTO contracts (
            contract_id, contract_number, contract_date, contract_status,
            customer_name, customer_bin, supplier_name, supplier_bin,
            lot_title, unit_price, quantity, contract_amount,
            brand_model, country, manufacturer
        ) VALUES (
            :contract_id, :contract_number, :contract_date, :contract_status,
            :customer_name, :customer_bin, :supplier_name, :supplier_bin,
            :lot_title, :unit_price, :quantity, :contract_amount,
            :brand_model, :country, :manufacturer
        )
    ''', lots)
    conn.commit()
    conn.close()

async def fetch_customer_supplier(session, contract_id):
    url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/customer_n_supplier/{contract_id}"
    for _ in range(5):
        try:
            async with session.get(url, timeout=20) as response:
                if response.status != 200:
                    await asyncio.sleep(5)
                    continue
                html = await response.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    print(f"429 in details for {contract_id}, sleep 120")
                    await asyncio.sleep(120)
                    continue
                    
                soup = BeautifulSoup(html, 'html.parser')
                tables = soup.find_all('table')
                c_bin, c_name, s_bin, s_name = "", "", "", ""
                if len(tables) > 0:
                    for row in tables[0].find_all('tr'):
                        cols = row.find_all(['th', 'td'])
                        if len(cols) == 2:
                            if 'БИН' in cols[0].text: c_bin = cols[1].text.strip()
                            elif 'Наименование заказчика' in cols[0].text: c_name = cols[1].text.strip()
                    if len(tables) > 1:
                        for row in tables[1].find_all('tr'):
                            cols = row.find_all(['th', 'td'])
                            if len(cols) == 2:
                                if 'БИН' in cols[0].text or 'ИИН' in cols[0].text:
                                    if cols[1].text.strip(): s_bin = cols[1].text.strip()
                                elif 'Наименование поставщика' in cols[0].text: s_name = cols[1].text.strip()
                return c_name, c_bin, s_name, s_bin
        except Exception as e:
            await asyncio.sleep(5)
    return None, None, None, None

async def fetch_lots(session, contract_id):
    url = f"https://goszakup.gov.kz/ru/egzcontract/cpublic/units/{contract_id}"
    for _ in range(5):
        lots_data = []
        try:
            async with session.get(url, timeout=20) as response:
                if response.status != 200:
                    await asyncio.sleep(5)
                    continue
                html = await response.text()
                if "429 Too" in html or "g-recaptcha" in html:
                    print(f"429 in lots for {contract_id}, sleep 120")
                    await asyncio.sleep(120)
                    continue
                    
                soup = BeautifulSoup(html, 'html.parser')
                table = soup.find('table')
                if not table: return lots_data
                for row in table.find_all('tr'):
                    cols = row.find_all(['td', 'th'])
                    if len(cols) >= 9 and cols[0].name != 'th':
                        title = cols[4].text.strip()
                        if title == "Наименование": continue
                        try:
                            quantity = float(cols[5].text.strip().replace(' ', '').replace(',', '.'))
                            unit_price = float(cols[7].text.strip().replace(' ', '').replace(',', '.'))
                            amount = float(cols[8].text.strip().replace(' ', '').replace(',', '.'))
                        except:
                            quantity, unit_price, amount = 0.0, 0.0, 0.0
                        lots_data.append({
                            "lot_title": title, "unit_price": unit_price,
                            "quantity": quantity, "contract_amount": amount
                        })
                return lots_data
        except:
            await asyncio.sleep(5)
    return []

async def process_contract(session, contract):
    print(f"Processing missing contract {contract['contract_id']}...")
    c_name, c_bin, s_name, s_bin = await fetch_customer_supplier(session, contract['contract_id'])
    if not c_name: return
    lots = await fetch_lots(session, contract['contract_id'])
    results = []
    for lot in lots:
        results.append({
            "contract_id": contract['contract_id'],
            "contract_number": contract['contract_number'],
            "contract_date": contract['contract_date'],
            "contract_status": contract['contract_status'],
            "customer_name": c_name,
            "customer_bin": c_bin,
            "supplier_name": s_name,
            "supplier_bin": s_bin,
            "lot_title": lot['lot_title'],
            "unit_price": lot['unit_price'],
            "quantity": lot['quantity'],
            "contract_amount": lot['contract_amount'],
            "brand_model": "", "country": "", "manufacturer": ""
        })
    if results:
        save_lots(results)
        print(f"Saved {contract['contract_id']} with {len(results)} lots.")

async def main():
    missing = [
        {"contract_id": "20418375", "contract_number": "130740003184/240045/00", "contract_date": "2024-05-30", "contract_status": "Исполнен"},
        {"contract_id": "20399420", "contract_number": "130740003184/240044/00", "contract_date": "2024-05-29", "contract_status": "Исполнен"},
        {"contract_id": "20399394", "contract_number": "130740003184/240043/00", "contract_date": "2024-05-29", "contract_status": "Исполнен"},
        {"contract_id": "20396595", "contract_number": "061240007364/240095/00", "contract_date": "2024-05-28", "contract_status": "Исполнен"},
        {"contract_id": "20396583", "contract_number": "061240007364/240094/00", "contract_date": "2024-05-28", "contract_status": "Исполнен"},
        {"contract_id": "20359175", "contract_number": "160440012336/240113/00", "contract_date": "2024-05-23", "contract_status": "Исполнен"},
        {"contract_id": "20359171", "contract_number": "160440012336/240112/00", "contract_date": "2024-05-23", "contract_status": "Исполнен"},
        {"contract_id": "20327769", "contract_number": "041240007003/240114/00", "contract_date": "2024-05-20", "contract_status": "Исполнен"},
        {"contract_id": "20128625", "contract_number": "970340001504/240080/00", "contract_date": "2024-04-20", "contract_status": "Исполнен"},
        {"contract_id": "20119063", "contract_number": "990640003694/240072/00", "contract_date": "2024-04-19", "contract_status": "Исполнен"}
    ]
    
    headers = {'User-Agent': 'Mozilla/5.0'}
    conn = aiohttp.TCPConnector(limit=5)
    async with aiohttp.ClientSession(headers=headers, connector=conn) as session:
        tasks = [process_contract(session, c) for c in missing]
        await asyncio.gather(*tasks)
        print("Done!")

if __name__ == "__main__":
    asyncio.run(main())
