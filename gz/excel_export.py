"""Модуль выгрузки индивидуального Excel-отчёта по БИН/ИИН.

Быстрая генерация с прямым запросом по плоской таблице contracts.
Включает колонку "Способ закупки" (ОК / ЗЦП).
"""

from __future__ import annotations

import io
import sqlite3

import pandas as pd


def generate_bin_excel(con: sqlite3.Connection, bin_code: str) -> io.BytesIO:
    """Генерирует мгновенный Excel файл с детализацией по БИН (включая Способ закупки)."""
    output = io.BytesIO()

    # Запрос по Поставщику
    query_supplier = """
    SELECT 
        c.contract_number as "Номер договора",
        c.contract_date as "Дата заключения",
        c.contract_status as "Статус",
        COALESCE(c.purchase_method, 'ОК') as "Способ закупки",
        c.customer_name as "Заказчик",
        c.customer_bin as "БИН Заказчика",
        c.supplier_name as "Поставщик",
        c.supplier_bin as "БИН Поставщика",
        c.lot_title as "Наименование лота",
        COALESCE(c.unit_price, 0) as "Цена за штуку (₸)",
        COALESCE(c.quantity, 1) as "Количество (шт)",
        c.contract_amount as "Сумма (₸)",
        COALESCE(NULLIF(c.brand_model, ''), 'Нет данных') as "Марка / Модель",
        COALESCE(NULLIF(c.country, ''), 'Нет данных') as "Страна происхождения",
        COALESCE(NULLIF(c.manufacturer, ''), 'Нет данных') as "Завод-изготовитель"
    FROM contracts_lots c
    WHERE c.supplier_bin = ?
      AND LOWER(c.contract_status) NOT LIKE '%расторгнут%'
      AND LOWER(c.contract_status) NOT LIKE '%не заключен%'
      AND LOWER(c.contract_status) NOT LIKE '%не исполнен%'
      AND LOWER(c.contract_status) NOT LIKE '%передан%'
    ORDER BY c.contract_amount DESC
    """

    df_sup = pd.read_sql_query(query_supplier, con, params=[bin_code])

    # Запрос по Заказчику
    query_customer = """
    SELECT 
        c.contract_number as "Номер договора",
        c.contract_date as "Дата заключения",
        c.contract_status as "Статус",
        COALESCE(c.purchase_method, 'ОК') as "Способ закупки",
        c.customer_name as "Заказчик",
        c.customer_bin as "БИН Заказчика",
        c.supplier_name as "Победитель Поставщик",
        c.supplier_bin as "БИН Поставщика",
        c.lot_title as "Наименование лота",
        COALESCE(c.unit_price, 0) as "Цена за штуку (₸)",
        COALESCE(c.quantity, 1) as "Количество (шт)",
        c.contract_amount as "Сумма (₸)",
        COALESCE(NULLIF(c.brand_model, ''), 'Нет данных') as "Марка / Модель",
        COALESCE(NULLIF(c.country, ''), 'Нет данных') as "Страна происхождения",
        COALESCE(NULLIF(c.manufacturer, ''), 'Нет данных') as "Завод-изготовитель"
    FROM contracts_lots c
    WHERE (c.customer_bin = ? OR c.contract_number LIKE ? || '/%')
      AND LOWER(c.contract_status) NOT LIKE '%расторгнут%'
      AND LOWER(c.contract_status) NOT LIKE '%не заключен%'
      AND LOWER(c.contract_status) NOT LIKE '%не исполнен%'
      AND LOWER(c.contract_status) NOT LIKE '%передан%'
    ORDER BY c.contract_amount DESC
    """

    df_cust = pd.read_sql_query(query_customer, con, params=[bin_code, bin_code])

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        if not df_sup.empty:
            df_sup.to_excel(writer, sheet_name="Поставщик (Договоры)", index=False)
        if not df_cust.empty:
            df_cust.to_excel(writer, sheet_name="Заказчик (Договоры)", index=False)
        if df_sup.empty and df_cust.empty:
            pd.DataFrame([{"Сообщение": "Нет данных по указанному БИН"}]).to_excel(
                writer, sheet_name="Пусто", index=False
            )

    output.seek(0)
    return output
