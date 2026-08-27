import requests
from bs4 import BeautifulSoup
import json
import sqlite3

url = "https://goszakup.gov.kz/ru/registry/contract"
params = {
    "filter[ref_subject_type]": "1",
    "filter[method][]": "2",
    "count_record": "2000",
    "filter[sign_date_from]": "2024-01-01",
    "filter[sign_date_to]": "2024-01-31"
}

headers = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36'
}

response = requests.get(url, params=params, headers=headers)
print(f"Status Code: {response.status_code}")
print(f"URL: {response.url}")

if response.status_code == 200:
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Check if there is a table
    table = soup.find('table')
    if table:
        print("Table found!")
        rows = table.find('tbody').find_all('tr')
        print(f"Found {len(rows)} rows in the first page.")
        if len(rows) > 0:
            cols = rows[0].find_all('td')
            print("First row columns:")
            for i, col in enumerate(cols):
                print(f"[{i}]: {col.text.strip()}")
                a_tag = col.find('a')
                if a_tag:
                    print(f"    Link: {a_tag.get('href')}")
    else:
        print("No table found.")
        print(response.text[:1000])
