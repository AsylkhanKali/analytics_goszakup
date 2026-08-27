"""Модуль вычисления ключевых бизнес-метрик по БИН/ИИН закупщика или поставщика.

Работает с плоской таблицей `contracts` (1 строка = 1 лот).
Собирает:
- Суммарные объёмы закупок / поставок
- Точную разбивку по способам закупки: Открытый конкурс (ОК) vs Запрос ценовых предложений (ЗЦП)
- Контрагентов (Заказчиков и Поставщиков)
- Ключевые поставляемые/закупаемые товары (Название, Количество, Цена за штуку, Сумма)
- Поставляемые марки, страны и заводы
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

EXCLUDE_STATUSES_SQL = (
    "LOWER(c.contract_status) NOT LIKE '%расторгнут%'"
    " AND LOWER(c.contract_status) NOT LIKE '%не заключен%'"
    " AND LOWER(c.contract_status) NOT LIKE '%не исполнен%'"
    " AND LOWER(c.contract_status) NOT LIKE '%передан%'"
)


def clean_bin(text: str) -> str:
    """Извлекает 12-значный БИН из текста."""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 12:
        return digits
    return ""


def format_currency(val: float) -> str:
    """Форматирует сумму в млн или тыс тенге."""
    if val >= 1_000_000:
        return f"{val / 1_000_000:,.2f} млн ₸".replace(",", " ")
    if val >= 1_000:
        return f"{val / 1_000:,.1f} тыс ₸".replace(",", " ")
    return f"{val:,.0f} ₸".replace(",", " ")


def format_unit_price(val: float) -> str:
    """Форматирует цену за штуку."""
    return f"{val:,.2f} ₸".replace(",", " ")


def format_qty(val: float) -> str:
    """Форматирует количество товаров."""
    if val.is_integer():
        return f"{int(val):,}".replace(",", " ")
    return f"{val:,.2f}".replace(",", " ")


def clean_company_name(name: Optional[str]) -> str:
    """Очищает наименование компании от лишних знаков препинания."""
    if not name:
        return "Нет данных"
    cleaned = re.sub(
        r"^(Государственное|Коммунальное|Товарищество|Индивидуальный|Акционерное)\s+",
        "",
        name,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^[;,.:\)\(\s]+", "", cleaned)
    return cleaned.strip(" .;:-") or "Нет данных"


def get_supplier_stats(con: sqlite3.Connection, bin_code: str) -> Optional[Dict[str, Any]]:
    """Собирает аналитику по поставщику с данным БИН."""
    cur = con.cursor()

    cur.execute(
        f"""
        SELECT 
            COUNT(DISTINCT c.contract_number) as unique_contracts,
            COUNT(*) as total_lots,
            COALESCE(SUM(c.contract_amount), 0) as total_amount,
            c.supplier_name
        FROM contracts_lots c
        WHERE c.supplier_bin = ? AND {EXCLUDE_STATUSES_SQL}
        """,
        (bin_code,),
    )
    row = cur.fetchone()
    unique_contracts, total_lots, total_amount, supplier_name = row if row else (0, 0, 0, None)

    if total_lots == 0:
        return None

    if not supplier_name:
        cur.execute(f"SELECT supplier_name FROM contracts_lots WHERE supplier_bin = ? AND {EXCLUDE_STATUSES_SQL} LIMIT 1", (bin_code,))
        r_name = cur.fetchone()
        supplier_name = r_name[0] if r_name else f"Поставщик БИН {bin_code}"

    # Разбивка по способам закупки (ОК vs ЗЦП)
    cur.execute(
        f"""
        SELECT 
            COUNT(DISTINCT CASE WHEN c.purchase_method LIKE '%Открытый конкурс%' THEN c.contract_number END),
            COUNT(DISTINCT CASE WHEN c.purchase_method LIKE '%Запрос ценовых предложений%' THEN c.contract_number END)
        FROM contracts_lots c
        WHERE c.supplier_bin = ? AND {EXCLUDE_STATUSES_SQL}
        """,
        (bin_code,),
    )
    r_meth = cur.fetchone()
    open_cnt, zcp_cnt = r_meth if r_meth else (0, 0)

    # ТОП поставляемых товаров (с ценой за штуку и количеством)
    cur.execute(
        f"""
        SELECT 
            c.lot_title, 
            COALESCE(SUM(c.quantity), COUNT(*)) as total_qty,
            COALESCE(AVG(c.unit_price), AVG(c.contract_amount)) as avg_unit_price,
            SUM(c.contract_amount) as total_sum,
            COUNT(*) as occurrences
        FROM contracts_lots c
        WHERE c.supplier_bin = ? AND {EXCLUDE_STATUSES_SQL} AND c.lot_title IS NOT NULL AND c.lot_title != ''
        GROUP BY c.lot_title
        ORDER BY total_sum DESC
        LIMIT 10
        """,
        (bin_code,),
    )
    top_products = [
        {
            "title": r[0],
            "quantity": float(r[1]),
            "unit_price": float(r[2]),
            "total_sum": float(r[3]),
            "count": r[4],
        }
        for r in cur.fetchall()
    ]

    # Контрагенты (Заказчики)
    cur.execute(
        f"""
        SELECT 
            c.customer_name, 
            c.customer_bin, 
            COUNT(DISTINCT c.contract_number) as contracts_cnt, 
            SUM(c.contract_amount) as total_sum
        FROM contracts_lots c
        WHERE c.supplier_bin = ? AND {EXCLUDE_STATUSES_SQL} AND c.customer_name IS NOT NULL
        GROUP BY c.customer_name, c.customer_bin
        ORDER BY total_sum DESC
        LIMIT 10
        """,
        (bin_code,),
    )
    counterparties = [
        {
            "name": r[0],
            "bin": r[1] or "",
            "contracts": r[2],
            "amount": float(r[3]),
        }
        for r in cur.fetchall()
    ]

    # Извлечённые техспецификации (марка, страна, завод)
    cur.execute(
        f"""
        SELECT 
            c.lot_title, 
            COALESCE(c.brand_model, 'Нет данных') as brand, 
            COALESCE(c.country, 'Нет данных') as country, 
            COALESCE(c.manufacturer, 'Нет данных') as mfr
        FROM contracts_lots c
        WHERE c.supplier_bin = ? AND {EXCLUDE_STATUSES_SQL} 
          AND (c.brand_model != '' OR c.country != '' OR c.manufacturer != '')
        LIMIT 15
        """,
        (bin_code,),
    )
    specs = [
        {"lot": r[0], "brand": r[1], "country": r[2], "mfr": r[3]} for r in cur.fetchall()
    ]

    if not specs:
        try:
            cur.execute(
                """
                SELECT lot_name, brand_model, country, manufacturer 
                FROM supplier_specs 
                WHERE supplier_bin = ? 
                LIMIT 15
                """,
                (bin_code,),
            )
            specs = [
                {"lot": r[0], "brand": r[1], "country": r[2], "mfr": r[3]} for r in cur.fetchall()
            ]
        except sqlite3.OperationalError:
            specs = []

    return {
        "bin": bin_code,
        "role": "supplier",
        "name": supplier_name,
        "total_contracts": unique_contracts,
        "total_lots": total_lots,
        "total_amount": float(total_amount),
        "open_tender_contracts": open_cnt,
        "zcp_contracts": zcp_cnt,
        "top_products": top_products,
        "counterparties": counterparties,
        "specs": specs,
    }


def get_customer_stats(con: sqlite3.Connection, bin_code: str) -> Optional[Dict[str, Any]]:
    """Собирает аналитику по заказчику с данным БИН."""
    cur = con.cursor()

    cur.execute(
        f"""
        SELECT 
            COUNT(DISTINCT c.contract_number) as unique_contracts,
            COUNT(*) as total_lots,
            COALESCE(SUM(c.contract_amount), 0) as total_amount,
            c.customer_name
        FROM contracts_lots c
        WHERE c.customer_bin = ? AND {EXCLUDE_STATUSES_SQL}
        """,
        (bin_code,),
    )
    row = cur.fetchone()
    unique_contracts, total_lots, total_amount, customer_name = row if row else (0, 0, 0, None)

    if total_lots == 0:
        return None

    if not customer_name:
        cur.execute(f"SELECT customer_name FROM contracts_lots WHERE customer_bin = ? AND {EXCLUDE_STATUSES_SQL} LIMIT 1", (bin_code,))
        r_name = cur.fetchone()
        customer_name = r_name[0] if r_name else f"Заказчик БИН {bin_code}"

    # Разбивка по способам закупки (ОК vs ЗЦП)
    cur.execute(
        f"""
        SELECT 
            COUNT(DISTINCT CASE WHEN c.purchase_method LIKE '%Открытый конкурс%' THEN c.contract_number END),
            COUNT(DISTINCT CASE WHEN c.purchase_method LIKE '%Запрос ценовых предложений%' THEN c.contract_number END)
        FROM contracts_lots c
        WHERE c.customer_bin = ? AND {EXCLUDE_STATUSES_SQL}
        """,
        (bin_code,),
    )
    r_meth = cur.fetchone()
    open_cnt, zcp_cnt = r_meth if r_meth else (0, 0)

    # ТОП закупаемых товаров
    cur.execute(
        f"""
        SELECT 
            c.lot_title, 
            COALESCE(SUM(c.quantity), COUNT(*)) as total_qty,
            COALESCE(AVG(c.unit_price), AVG(c.contract_amount)) as avg_unit_price,
            SUM(c.contract_amount) as total_sum,
            COUNT(*) as occurrences
        FROM contracts_lots c
        WHERE c.customer_bin = ? AND {EXCLUDE_STATUSES_SQL} AND c.lot_title IS NOT NULL AND c.lot_title != ''
        GROUP BY c.lot_title
        ORDER BY total_sum DESC
        LIMIT 10
        """,
        (bin_code,),
    )
    top_products = [
        {
            "title": r[0],
            "quantity": float(r[1]),
            "unit_price": float(r[2]),
            "total_sum": float(r[3]),
            "count": r[4],
        }
        for r in cur.fetchall()
    ]

    # Контрагенты (Поставщики-победители)
    cur.execute(
        f"""
        SELECT 
            c.supplier_name, 
            c.supplier_bin, 
            COUNT(DISTINCT c.contract_number) as contracts_cnt, 
            SUM(c.contract_amount) as total_sum
        FROM contracts_lots c
        WHERE c.customer_bin = ? AND {EXCLUDE_STATUSES_SQL} AND c.supplier_name IS NOT NULL
        GROUP BY c.supplier_name, c.supplier_bin
        ORDER BY total_sum DESC
        LIMIT 10
        """,
        (bin_code,),
    )
    counterparties = [
        {
            "name": r[0],
            "bin": r[1] or "",
            "contracts": r[2],
            "amount": float(r[3]),
        }
        for r in cur.fetchall()
    ]

    # Извлечённые техспецификации
    cur.execute(
        f"""
        SELECT 
            c.lot_title, 
            COALESCE(c.brand_model, 'Нет данных') as brand, 
            COALESCE(c.country, 'Нет данных') as country, 
            COALESCE(c.manufacturer, 'Нет данных') as mfr
        FROM contracts_lots c
        WHERE c.customer_bin = ? AND {EXCLUDE_STATUSES_SQL} 
          AND (c.brand_model != '' OR c.country != '' OR c.manufacturer != '')
        LIMIT 15
        """,
        (bin_code,),
    )
    specs = [
        {"lot": r[0], "brand": r[1], "country": r[2], "mfr": r[3]} for r in cur.fetchall()
    ]

    return {
        "bin": bin_code,
        "role": "customer",
        "name": customer_name,
        "total_contracts": unique_contracts,
        "total_lots": total_lots,
        "total_amount": float(total_amount),
        "open_tender_contracts": open_cnt,
        "zcp_contracts": zcp_cnt,
        "top_products": top_products,
        "counterparties": counterparties,
        "specs": specs,
    }


def get_bin_analytics(con: sqlite3.Connection, bin_code: str) -> Dict[str, Any]:
    """Универсальная функция получения аналитики по БИН."""
    bin_clean = clean_bin(bin_code)
    if not bin_clean:
        return {"error": "Некорректный БИН. Введите 12 цифр."}

    supplier_data = get_supplier_stats(con, bin_clean)
    customer_data = get_customer_stats(con, bin_clean)

    if not supplier_data and not customer_data:
        return {
            "error": f"По БИН `{bin_clean}` не найдено заключённых договоров в базе данных."
        }

    return {
        "bin": bin_clean,
        "supplier": supplier_data,
        "customer": customer_data,
        "role": "both" if (supplier_data and customer_data) else ("supplier" if supplier_data else "customer"),
    }


def format_telegram_report(data: Dict[str, Any]) -> str:
    """Формирует текстовый отчёт для Telegram бота."""
    if "error" in data:
        return f"⚠️ {data['error']}"

    bin_code = data["bin"]
    lines = []

    # Аналитика Поставщика
    if data.get("supplier"):
        sup = data["supplier"]
        name = clean_company_name(sup["name"])
        lines.append(f"🏢 *ПОСТАВЩИК:* `{name}`\n*БИН:* `{bin_code}`\n")
        lines.append(f"📜 *Всего Договоров:* `{sup['total_contracts']:,}` *(Лотов: {sup['total_lots']:,})*".replace(",", " "))
        lines.append(f"🏆 *Открытый конкурс (ОК):* `{sup['open_tender_contracts']:,}` Договоров".replace(",", " "))
        lines.append(f"⚡ *Запрос ценовых предложений (ЗЦП):* `{sup['zcp_contracts']:,}` Договоров".replace(",", " "))
        lines.append(f"💰 *Общая сумма поставок:* `{format_currency(sup['total_amount'])}`")

        if sup.get("top_products"):
            lines.append("\n📦 *ТОП поставляемых товаров:*")
            for idx, p in enumerate(sup["top_products"][:5], 1):
                t_title = p["title"][:45]
                p_price = format_unit_price(p["unit_price"])
                p_sum = format_currency(p["total_sum"])
                p_qty = format_qty(p["quantity"])
                lines.append(f"  {idx}. *{t_title}*\n     • Цена за шт: `{p_price}` | Кол-во: `{p_qty}` | Сумма: `{p_sum}`")

        if sup.get("counterparties"):
            lines.append("\n🏛️ *Основные Заказчики (Контрагенты):*")
            for idx, c in enumerate(sup["counterparties"][:5], 1):
                c_name = clean_company_name(c["name"])[:40]
                c_bin = f" (БИН: `{c['bin']}`)" if c.get("bin") else ""
                lines.append(f"  {idx}. *{c_name}*{c_bin}\n     • `{c['contracts']}` Договоров на сумму `{format_currency(c['amount'])}`")

    # Аналитика Заказчика
    if data.get("customer"):
        cust = data["customer"]
        if lines:
            lines.append("\n" + "—" * 30 + "\n")
        c_name = clean_company_name(cust["name"])
        lines.append(f"🏛️ *ЗАКАЗЧИК:* `{c_name}`\n*БИН:* `{bin_code}`\n")
        lines.append(f"📜 *Всего Договоров:* `{cust['total_contracts']:,}` *(Лотов: {cust['total_lots']:,})*".replace(",", " "))
        lines.append(f"🏆 *Открытый конкурс (ОК):* `{cust['open_tender_contracts']:,}` Договоров".replace(",", " "))
        lines.append(f"⚡ *Запрос ценовых предложений (ЗЦП):* `{cust['zcp_contracts']:,}` Договоров".replace(",", " "))
        lines.append(f"💰 *Общий бюджет закупок:* `{format_currency(cust['total_amount'])}`")

        if cust.get("top_products"):
            lines.append("\n📦 *ТОП закупаемых товаров:*")
            for idx, p in enumerate(cust["top_products"][:5], 1):
                t_title = p["title"][:45]
                p_price = format_unit_price(p["unit_price"])
                p_sum = format_currency(p["total_sum"])
                p_qty = format_qty(p["quantity"])
                lines.append(f"  {idx}. *{t_title}*\n     • Цена за шт: `{p_price}` | Кол-во: `{p_qty}` | Сумма: `{p_sum}`")

        if cust.get("counterparties"):
            lines.append("\n🤝 *Основные Поставщики-победители:*")
            for idx, s in enumerate(cust["counterparties"][:5], 1):
                s_name = clean_company_name(s["name"])[:40]
                s_bin = f" (БИН: `{s['bin']}`)" if s.get("bin") else ""
                lines.append(f"  {idx}. *{s_name}*{s_bin}\n     • `{s['contracts']}` Договоров на сумму `{format_currency(s['amount'])}`")

    return "\n".join(lines)
