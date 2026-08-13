"""Этап 4 — аналитика и выгрузка в Excel.

Поставщики и заказчики считаются по БИН/ИИН, а не по строке названия:
одно и то же ТОО в разных закупках пишется по-разному, и группировка
по тексту дала бы завышенное число «разных» участников рынка.

Где данных нет — в отчёте стоит «Нет данных». Ничего не достраивается
правдоподобными догадками: это аналитика под денежные решения.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd

from .store import NO_DATA

log = logging.getLogger(__name__)

OUT = Path(__file__).resolve().parent.parent / "out"

SQL = """
SELECT
    l.category, l.lot_name, l.lot_desc, l.qty, l.year, l.method, l.announce_id,
    ct.lot_number,
    ct.contract_number, ct.contract_id, ct.plan_amount, ct.contract_amount,
    ct.executed_amount, ct.supplier_name, ct.supplier_bin, ct.contract_status,
    ct.winner_status,
    cc.signed_date, cc.description, cc.subject_type,
    p.participants, p.admitted, p.min_price, p.max_price,
    ts.brand, ts.model, ts.country, ts.manufacturer, ts.file_url
FROM lots l
JOIN contracts ct
     ON ct.announce_id = l.announce_id AND ct.lot_number = l.lot_number
LEFT JOIN contract_cards cc ON cc.contract_id = ct.contract_id
LEFT JOIN protocols  p  ON p.announce_id = l.announce_id AND p.lot_number = l.lot_number
LEFT JOIN techspecs  ts ON ts.announce_id = l.announce_id
WHERE l.relevant = 1
"""

# Договор состоялся и живёт: отменённые и незаключённые в аналитику не идут.
BAD_STATUS = re.compile(r"не заключен|расторгнут|отмен", re.I)

CUSTOMER_RE = re.compile(r"Заказчик:\s*(.+)", re.S)


def _ensure_tables(con):
    for ddl in (
        "CREATE TABLE IF NOT EXISTS contract_cards (contract_id TEXT PRIMARY KEY,"
        " signed_date TEXT, description TEXT, subject_type TEXT, method_fact TEXT,"
        " status TEXT, fin_year TEXT, plan_total REAL, result_total REAL)",
        "CREATE TABLE IF NOT EXISTS protocols (announce_id TEXT, lot_number TEXT,"
        " participants INTEGER, admitted INTEGER, planned_price REAL,"
        " min_price REAL, max_price REAL,"
        " file_url TEXT, PRIMARY KEY (announce_id, lot_number))",
        # Техспецификация поставщика из-под авторизации — уровень лота.
        "CREATE TABLE IF NOT EXISTS supplier_specs (announce_id TEXT, lot_number TEXT,"
        " supplier_name TEXT, supplier_bin TEXT, brand_model TEXT, country TEXT,"
        " manufacturer TEXT, year_made TEXT, warranty TEXT, standard TEXT,"
        " lot_name TEXT, source_file TEXT, PRIMARY KEY (announce_id, lot_number))",
        "CREATE TABLE IF NOT EXISTS contract_units (unit_id TEXT PRIMARY KEY,"
        " contract_id TEXT, ktru TEXT, name TEXT, qty REAL, unit TEXT,"
        " price_per_unit REAL, price_no_vat REAL, amount REAL, short_char TEXT,"
        " extra_char TEXT, customer TEXT, delivery TEXT, doc_url TEXT)",
    ):
        con.execute(ddl)
    con.commit()


def build(con) -> pd.DataFrame:
    _ensure_tables(con)
    df = pd.read_sql_query(SQL, con)
    log.info("строк договор×лот: %s", len(df))

    df = df[~df["contract_status"].fillna("").str.contains(BAD_STATUS)]

    df["Заказчик"] = (
        df["lot_desc"].fillna("").str.extract(CUSTOMER_RE, expand=False)
        .str.replace(r"\s+", " ", regex=True).str.strip().replace("", NO_DATA)
    )
    df["Товар"] = df["lot_name"].fillna("").str.strip()
    df["товар_ключ"] = df["Товар"].str.lower()

    qty = df["qty"].where(df["qty"] > 0)
    df["Цена за единицу"] = (df["contract_amount"] / qty).round(2)
    df["Снижение, %"] = (
        100 * (df["plan_amount"] - df["contract_amount"]) / df["plan_amount"].where(df["plan_amount"] > 0)
    ).round(1)

    for col in ("brand", "model", "country", "manufacturer", "signed_date", "description"):
        df[col] = df[col].fillna(NO_DATA).replace("", NO_DATA)
    return df


def detail(df: pd.DataFrame) -> pd.DataFrame:
    """Детальная таблица в колонках из технического задания."""
    out = pd.DataFrame({
        "Категория": df["category"],
        "Наименование товара": df["Товар"],
        "Краткое содержание договора": df["description"],
        "Количество": df["qty"],
        "Цена за единицу": df["Цена за единицу"],
        "Общая сумма": df["contract_amount"],
        "Первоначальная цена": df["plan_amount"],
        "Снижение, %": df["Снижение, %"],
        "Заказчик": df["Заказчик"],
        "Поставщик": df["supplier_name"],
        "БИН/ИИН поставщика": df["supplier_bin"],
        "Дата": df["signed_date"],
        "Способ закупки": df["method"],
        "Номер договора": df["contract_number"],
        "Заявок подано": df["participants"],
        "Заявок допущено": df["admitted"],
        "Марка/модель": (df["brand"] + " / " + df["model"]).str.replace(
            f"{NO_DATA} / {NO_DATA}", NO_DATA, regex=False),
        "Страна происхождения": df["country"],
        "Завод-изготовитель": df["manufacturer"],
        "Ссылка на техспецификацию": df["file_url"].fillna(NO_DATA),
    })
    return out.sort_values(["Категория", "Общая сумма"], ascending=[True, False])


OPEN_TENDER = "Открытый конкурс"


def open_tender(con, df):
    """Раздел по открытому конкурсу — на уровне позиции договора.

    Узкая полоса рынка (358 договоров), но на неё приходится больше половины
    всех денег выборки, и только здесь есть смысл разбирать техспецификации.
    Цена берётся без НДС: в таблице позиций она у плательщиков НДС на 12 % выше,
    и сравнивать поставщиков по ней нельзя.
    """
    ot = df[df["method"] == OPEN_TENDER]
    if ot.empty:
        return None, None

    # Договор может покрывать несколько лотов — схлопываем до одного договора.
    head = ot.sort_values("contract_amount", ascending=False).groupby(
        "contract_id", as_index=False
    ).agg({
        "category": "first", "Заказчик": "first", "signed_date": "first",
        "participants": "max", "admitted": "max",
        "year": "first", "supplier_name": "first", "supplier_bin": "first",
        "contract_number": "first", "plan_amount": "sum",
        "contract_amount": "sum", "Снижение, %": "mean",
    })

    units = pd.read_sql_query("SELECT * FROM contract_units", con)
    rows = head.merge(units, on="contract_id", how="left")

    # Спецификацию заполняет поставщик по лоту, а договор может покрывать
    # несколько лотов — тогда неизвестно, какой позиции она соответствует.
    # Привязываем только однолотовые договоры, иначе приписали бы марку наугад.
    nlots = ot.groupby("contract_id")["lot_number"].nunique()
    single = ot[ot["contract_id"].isin(nlots[nlots == 1].index)]
    specs = pd.read_sql_query("SELECT * FROM supplier_specs", con)
    spec_cols = ["brand_model", "country", "manufacturer", "year_made", "source_file"]
    if not specs.empty:
        link = single[["contract_id", "announce_id", "lot_number"]].drop_duplicates().merge(
            specs, on=["announce_id", "lot_number"], how="inner"
        )
        rows = rows.merge(link[["contract_id", *spec_cols]], on="contract_id", how="left")
    for col in spec_cols:
        rows[col] = (
            NO_DATA if col not in rows
            else rows[col].fillna(NO_DATA).replace("", NO_DATA)
        )

    detail_ot = pd.DataFrame({
        "Дата заключения": rows["signed_date"],
        "Категория": rows["category"],
        "Товар (позиция договора)": rows["name"].fillna(rows["category"]),
        "Характеристика": rows["extra_char"].fillna(rows["short_char"]).fillna(NO_DATA),
        "КТРУ": rows["ktru"].fillna(NO_DATA),
        "Количество": rows["qty"],
        "Ед.": rows["unit"],
        "Цена за единицу без НДС": rows["price_no_vat"],
        "Сумма позиции": rows["amount"],
        "Начальная сумма договора": rows["plan_amount"],
        "Итоговая сумма договора": rows["contract_amount"],
        "Снижение, %": rows["Снижение, %"],
        "Заявок подано": rows["participants"],
        "Заявок допущено": rows["admitted"],
        "Заказчик": rows["Заказчик"],
        "Место поставки": rows["delivery"].fillna(NO_DATA),
        "Поставщик": rows["supplier_name"],
        "БИН/ИИН поставщика": rows["supplier_bin"],
        "Номер договора": rows["contract_number"],
        "Марка / модель / тип": rows["brand_model"],
        "Страна происхождения": rows["country"],
        "Завод-изготовитель": rows["manufacturer"],
        "Год выпуска": rows["year_made"],
        "Источник техспецификации": rows["source_file"],
    }).sort_values("Сумма позиции", ascending=False)

    summary = head.groupby("category", as_index=False).agg({
        "contract_id": "count", "contract_amount": "sum",
        "plan_amount": "sum", "supplier_bin": "nunique",
        "Заказчик": "nunique", "Снижение, %": "mean",
    }).rename(columns={
        "category": "Категория", "contract_id": "Договоров",
        "contract_amount": "Итоговая сумма", "plan_amount": "Начальная сумма",
        "supplier_bin": "Поставщиков", "Заказчик": "Заказчиков",
    })
    summary["Средний чек"] = (summary["Итоговая сумма"] / summary["Договоров"]).round(0)
    top3 = head.groupby(["category", "supplier_bin"])["contract_amount"].sum().reset_index()
    share = top3.groupby("category").apply(
        lambda g: 100 * g.nlargest(3, "contract_amount")["contract_amount"].sum()
        / max(g["contract_amount"].sum(), 1),
        include_groups=False,
    ).round(1)
    summary["Доля топ-3 поставщиков, %"] = summary["Категория"].map(share)
    return detail_ot, summary.round(2)


def _agg_products(df):
    g = df.groupby(["category", "товар_ключ"]).agg(
        Договоров=("contract_number", "count"),
        Поставщиков=("supplier_bin", "nunique"),
        Заказчиков=("Заказчик", "nunique"),
        Сумма=("contract_amount", "sum"),
        Средняя_цена_ед=("Цена за единицу", "median"),
        Мин_цена_ед=("Цена за единицу", "min"),
        Макс_цена_ед=("Цена за единицу", "max"),
        Среднее_снижение=("Снижение, %", "mean"),
        Лет=("year", "nunique"),
    ).reset_index()
    g["Конкурентов на договор"] = (g["Поставщиков"] / g["Договоров"]).round(2)
    return g.round(2)


def report(con, path: Path | None = None) -> Path:
    df = build(con)
    if df.empty:
        raise SystemExit("нет данных: сначала пройдите этапы discover и enrich")

    OUT.mkdir(parents=True, exist_ok=True)
    path = path or OUT / "goszakup_analytics.xlsx"

    prod = _agg_products(df)
    # Перспективность: часто покупают, много заказчиков, но мало поставщиков.
    prod["Индекс входа"] = (
        prod["Договоров"].rank(pct=True) * 0.4
        + prod["Заказчиков"].rank(pct=True) * 0.3
        + (1 - prod["Поставщиков"].rank(pct=True)) * 0.3
    ).round(3)

    suppliers = df.groupby(["supplier_bin", "supplier_name"]).agg(
        Договоров=("contract_number", "count"),
        Сумма=("contract_amount", "sum"),
        Категорий=("category", "nunique"),
        Заказчиков=("Заказчик", "nunique"),
        Товаров=("товар_ключ", "nunique"),
    ).reset_index().sort_values("Сумма", ascending=False).round(2)

    customers = df.groupby("Заказчик").agg(
        Договоров=("contract_number", "count"),
        Сумма=("contract_amount", "sum"),
        Поставщиков=("supplier_bin", "nunique"),
        Товаров=("товар_ключ", "nunique"),
    ).reset_index().sort_values("Сумма", ascending=False).round(2)

    yearly = df.pivot_table(
        index=["category", "товар_ключ"], columns="year",
        values="contract_number", aggfunc="count", fill_value=0,
    ).reset_index()
    year_cols = [c for c in yearly.columns if str(c).isdigit()]
    yearly["Лет подряд"] = (yearly[year_cols] > 0).sum(axis=1)
    yearly = yearly.sort_values(["Лет подряд", *year_cols], ascending=False)

    have_part = df[df["participants"].notna()]
    low_comp = have_part.sort_values(["participants", "contract_amount"], ascending=[True, False])[
        ["category", "Товар", "participants", "contract_amount", "plan_amount",
         "Снижение, %", "supplier_name", "Заказчик", "contract_number"]
    ]
    big_low = have_part[have_part["participants"] <= 2].sort_values("contract_amount", ascending=False)[
        ["category", "Товар", "participants", "contract_amount", "supplier_name",
         "Заказчик", "year", "contract_number"]
    ]

    cats = df.groupby("category").agg(
        Договоров=("contract_number", "count"),
        Сумма=("contract_amount", "sum"),
        Поставщиков=("supplier_bin", "nunique"),
        Заказчиков=("Заказчик", "nunique"),
        Товаров=("товар_ключ", "nunique"),
        Среднее_снижение=("Снижение, %", "mean"),
        Средних_участников=("participants", "mean"),
    ).reset_index().round(2)
    # Доля рынка топ-3 поставщиков — индикатор монополизации.
    top3 = df.groupby(["category", "supplier_bin"])["contract_amount"].sum().reset_index()
    share = top3.groupby("category").apply(
        lambda g: 100 * g.nlargest(3, "contract_amount")["contract_amount"].sum()
        / max(g["contract_amount"].sum(), 1),
        include_groups=False,
    ).round(1)
    cats["Доля топ-3 поставщиков, %"] = cats["category"].map(share)

    sheets = {
        "Договоры": detail(df),
        "ТОП-20 товаров": prod.sort_values("Индекс входа", ascending=False).head(20),
        "Все товары": prod.sort_values("Сумма", ascending=False),
        "ТОП-20 поставщиков": suppliers.head(20),
        "ТОП-20 заказчиков": customers.head(20),
        "Повторяемость по годам": yearly.head(300),
        "Наименьшая конкуренция": low_comp.head(500),
        "Дорого и мало участников": big_low.head(300),
        "Категории": cats,
    }

    ot_detail, ot_summary = open_tender(con, df)
    if ot_detail is not None:
        sheets["Открытый конкурс"] = ot_detail
        sheets["Открытый конкурс — сводка"] = ot_summary

    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        for name, sheet in sheets.items():
            sheet.to_excel(xl, sheet_name=name[:31], index=False)

    log.info("отчёт: %s (%s листов, %s строк в детализации)", path, len(sheets), len(df))
    return path
