import sqlite3
conn = sqlite3.connect('data/goszakup.db')
c = conn.cursor()
c.execute("DELETE FROM contracts WHERE contract_date >= '2024-02-01'")
conn.commit()
print("Deleted rows:", c.rowcount)
conn.close()
