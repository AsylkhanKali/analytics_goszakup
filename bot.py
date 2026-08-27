"""Telegram Bot для аналитики Госзакупок Казахстана (goszakup.gov.kz).

Запуск:
    .venv/bin/python bot.py
Или:
    .venv/bin/python cli.py bot
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from gz.auth import check_session_active, load_session, save_session
from gz.bin_analytics import (
    EXCLUDE_STATUSES_SQL,
    clean_bin,
    format_currency,
    format_qty,
    format_telegram_report,
    format_unit_price,
    get_bin_analytics,
)
from gz.client import Portal
from gz.excel_export import generate_bin_excel
from gz.live_fetch import fetch_and_parse_contract_units
from gz.store import connect, upsert
from gz.techspec import collect as collect_public_techspecs

# Загружаем переменные из .env
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("gz_bot")


def get_keyboard_for_bin(bin_code: str) -> InlineKeyboardMarkup:
    """Формирует инлайн-клавиатуру."""
    keyboard = [
        [
            InlineKeyboardButton("📊 Договоры", callback_data=f"contracts_{bin_code}"),
            InlineKeyboardButton("🏷️ Марки и Заводы", callback_data=f"specs_{bin_code}"),
        ],
        [
            InlineKeyboardButton("🔄 Допрогрузить свежие договоры", callback_data=f"sync_{bin_code}"),
            InlineKeyboardButton("📥 Скачать Excel", callback_data=f"excel_{bin_code}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    welcome_text = (
        "👋 *Привет! Я бот аналитики Госзакупок Казахстана (`goszakup.gov.kz`)*\n\n"
        "🔍 *Отправьте мне 12-значный БИН или ИИН компании*, чтобы мгновенно получить аналитику:\n"
        "• Объёмы договоров и бюджет закупок (с выделением *Открытого конкурса*)\n"
        "• Контрагентов (Заказчиков и Поставщиков-победителей)\n"
        "• Ключевые поставляемые и закупаемые товары (Количество, Цена за штуку, Общая сумма)\n"
        "• Марки, страны происхождения и заводы-изготовители\n\n"
        "📌 *Команды*\n"
        "/auth <cookie> — сохранить авторизованную сессию v3bl\n"
        "/status — проверить статус авторизованной сессии\n"
        "/help — справка по использованию"
    )
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def auth_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /auth <ci_session>."""
    if not update.message:
        return

    if not context.args:
        help_msg = (
            "🔑 *Как передать авторизованную сессию Goszakup:*\n\n"
            "1. Войдите на `v3bl.goszakup.gov.kz` в браузере под вашей учетной записью/ЭЦП.\n"
            "2. Нажмите F12 -> вкладка Application (Приложение) -> Cookies -> `https://v3bl.goszakup.gov.kz`.\n"
            "3. Скопируйте значение `ci_session`.\n"
            "4. Отправьте команду прямо сюда:\n"
            "`/auth ваш_код_ci_session`"
        )
        await update.message.reply_text(help_msg, parse_mode="Markdown")
        return

    ci_session = context.args[0].strip()
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    )
    save_session(ci_session, ua)

    active, status_msg = check_session_active()
    if active:
        msg = "✅ *Сессия v3bl.goszakup.gov.kz успешно сохранена и АКТИВНА!*\nТеперь бот может скачивать закрытые Приложения 17 (техспецификации поставщика)."
    else:
        msg = f"⚠️ *Сессия сохранена, но сервер вернул статус:* {status_msg}\nПроверьте правильность скопированного `ci_session`."

    await update.message.reply_text(msg, parse_mode="Markdown")


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /status."""
    if not update.message:
        return
    active, status_msg = check_session_active()
    con = connect()
    specs_cnt = con.execute("SELECT COUNT(*) FROM supplier_specs").fetchone()[0]
    contracts_cnt = con.execute(f"SELECT COUNT(*) FROM contracts_lots c WHERE {EXCLUDE_STATUSES_SQL}").fetchone()[0]
    con.close()

    status_icon = "🟢" if active else "🔴"
    text = (
        f"📊 *Статус системы analytics_goszakup:*\n\n"
        f"🔐 *Авторизация v3bl:* {status_icon} {status_msg}\n"
        f"📜 *Действующих договоров в базе:* `{contracts_cnt:,}`\n"
        f"🏷️ *Извлечено спецификаций (марки/заводы):* `{specs_cnt:,}`\n\n"
        f"💡 _Отправьте 12-значный БИН/ИИН для получения аналитики._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка текстового сообщения с БИН/ИИН."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    digits = clean_bin(text)

    if len(digits) != 12:
        await update.message.reply_text(
            "⚠️ Пожалуйста, введите корректный 12-значный БИН или ИИН (только 12 цифр).",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(f"⏳ Формирую аналитический отчёт по БИН `{digits}`...", parse_mode="Markdown")

    con = connect()
    try:
        analytics = get_bin_analytics(con, digits)
        report_md = format_telegram_report(analytics)
        reply_markup = get_keyboard_for_bin(digits)
        await update.message.reply_text(report_md, parse_mode="Markdown", reply_markup=reply_markup)
    finally:
        con.close()


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатий на инлайн-кнопки."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    data = query.data

    con = connect()
    try:
        if data.startswith("contracts_"):
            bin_code = data.replace("contracts_", "")
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
            if not rows:
                await query.message.reply_text(
                    f"ℹ️ По БИН `{bin_code}` нет сохранённых действующих договоров.\nНажмите *🔄 Допрогрузить свежие договоры*, чтобы проверить их на портале.",
                    parse_mode="Markdown",
                    reply_markup=get_keyboard_for_bin(bin_code),
                )
                return

            msg_lines = [f"📜 *Топ-10 крупных договоров по БИН* `{bin_code}`:\n"]
            for r in rows:
                c_num, c_stat, c_amt, s_name, s_bin, c_name, c_bin, title, qty, u_price, p_meth = r
                q_str = format_qty(qty)
                u_str = format_unit_price(u_price if u_price > 0 else (c_amt / qty if qty > 0 else 0))

                if bin_code == c_bin:
                    counterparty_str = f"  🤝 *Поставщик:* {s_name} (`{s_bin}`)" if (s_bin and s_name) else (f"  🤝 *Поставщик:* `{s_bin}`" if s_bin else "")
                elif bin_code == s_bin:
                    counterparty_str = f"  🏛️ *Заказчик:* {c_name} (`{c_bin}`)" if (c_bin and c_name) else (f"  🏛️ *Заказчик:* `{c_bin}`" if c_bin else "")
                else:
                    cust_str = f"{c_name} (`{c_bin}`)" if (c_bin and c_name) else (f"`{c_bin}`" if c_bin else "Нет данных")
                    supp_str = f"{s_name} (`{s_bin}`)" if (s_bin and s_name) else (f"`{s_bin}`" if s_bin else "Нет данных")
                    counterparty_str = f"  🏛️ *Заказчик:* {cust_str}\n  🤝 *Поставщик:* {supp_str}"

                msg_line = (
                    f"• № `{c_num}` ({c_stat} | *{p_meth}*)\n"
                    f"  Товар: {title}\n"
                )
                if counterparty_str:
                    msg_line += f"{counterparty_str}\n"
                msg_line += f"  Количество: *{q_str}* × *{u_str}/шт* (Всего: *{format_currency(c_amt or 0)}*)\n"
                msg_lines.append(msg_line)

            reply_markup = get_keyboard_for_bin(bin_code)
            await query.message.reply_text("\n".join(msg_lines), parse_mode="Markdown", reply_markup=reply_markup)

        elif data.startswith("specs_"):
            bin_code = data.replace("specs_", "")
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
                LIMIT 10
                """,
                params,
            )
            rows = cur.fetchall()
            if not rows:
                await query.message.reply_text(
                    f"ℹ️ В базе пока нет сохранённых марок и заводов по БИН `{bin_code}`.\n"
                    f"Вы можете нажать кнопку *🔄 Допрогрузить свежие договоры*, чтобы проверить обновлённые данные на портале.",
                    parse_mode="Markdown",
                    reply_markup=get_keyboard_for_bin(bin_code),
                )
                return

            msg_lines = [f"🏷️ *Спецификации, марки и заводы по БИН* `{bin_code}`:\n"]
            for r in rows:
                title, qty, amt, bm, country, mfr, c_name, c_bin, s_name, s_bin = r
                q_str = format_qty(qty)
                u_str = format_unit_price(amt or 0, qty)

                if bin_code == c_bin:
                    counterparty_str = f"  🤝 *Поставщик:* {s_name} (`{s_bin}`)" if (s_bin and s_name) else (f"  🤝 *Поставщик:* `{s_bin}`" if s_bin else "")
                elif bin_code == s_bin:
                    counterparty_str = f"  🏛️ *Заказчик:* {c_name} (`{c_bin}`)" if (c_bin and c_name) else (f"  🏛️ *Заказчик:* `{c_bin}`" if c_bin else "")
                else:
                    cust_str = f"{c_name} (`{c_bin}`)" if (c_bin and c_name) else (f"`{c_bin}`" if c_bin else "Нет данных")
                    supp_str = f"{s_name} (`{s_bin}`)" if (s_bin and s_name) else (f"`{s_bin}`" if s_bin else "Нет данных")
                    counterparty_str = f"  🏛️ *Заказчик:* {cust_str}\n  🤝 *Поставщик:* {supp_str}"

                msg_line = (
                    f"• *{title}*\n"
                )
                if counterparty_str:
                    msg_line += f"{counterparty_str}\n"
                msg_line += (
                    f"  Количество: *{q_str}* × *{u_str}/шт* (Всего: *{format_currency(amt or 0)}*)\n"
                    f"  Марка / Модель: `{bm or 'Нет данных'}`\n"
                    f"  Страна: `{country or 'Нет данных'}`\n"
                    f"  Завод: `{mfr or 'Нет данных'}`\n"
                )
                msg_lines.append(msg_line)

            await query.message.reply_text(
                "\n".join(msg_lines),
                parse_mode="Markdown",
                reply_markup=get_keyboard_for_bin(bin_code),
            )

        elif data.startswith("fetch_"):
            bin_code = data.replace("fetch_", "")
            await query.message.reply_text(
                f"⏳ Запускаю живую прогрузку с портала Госзакупок по БИН `{bin_code}`...\nПожалуйста, подождите 15-30 секунд.",
                parse_mode="Markdown",
            )

            cur = con.cursor()
            if len(bin_code) == 12 and bin_code.isdigit():
                where_clause = "(supplier_bin = ? OR customer_bin = ?)"
                params = (bin_code, bin_code)
            else:
                where_clause = "contract_number LIKE ? || '%'"
                params = (bin_code,)

            cur.execute(
                f"SELECT DISTINCT contract_id FROM contracts_lots WHERE {where_clause} AND {EXCLUDE_STATUSES_SQL} AND contract_id IS NOT NULL LIMIT 15",
                params,
            )
            cids = [r[0] for r in cur.fetchall()]

            auth_count = 0
            session_data = load_session()
            if session_data and cids:
                for cid in cids:
                    try:
                        specs = fetch_and_parse_contract_units(con, cid)
                        auth_count += len(specs)
                    except Exception as e:
                        log.warning("Ошибка живой выкачки по CID %s: %s", cid, e)

            analytics = get_bin_analytics(con, bin_code)
            report_md = format_telegram_report(analytics)
            res_msg = f"✅ *Живая прогрузка по БИН завершена!*\n• Извлечено Приложений 17: *{auth_count}*\n\n" + report_md

        elif data.startswith("sync_"):
            bin_code = data.replace("sync_", "")
            status_msg = await query.message.reply_text(
                f"⏳ *Синхронизирую свежие договоры с портала по БИН* `{bin_code}`...\nПожалуйста, подождите несколько секунд.",
                parse_mode="Markdown",
            )

            def run_gap_sync():
                with sqlite3.connect("data/goszakup.db", timeout=60.0) as con_gap:
                    from gz.gap_filler import sync_bin_contracts_from_portal
                    return sync_bin_contracts_from_portal(con_gap, bin_code)

            sync_res = await asyncio.to_thread(run_gap_sync)
            added_c = sync_res.get("added_contracts", 0)

            analytics = get_bin_analytics(con, bin_code)
            report_md = format_telegram_report(analytics)
            res_msg = f"✅ *Синхронизация с порталом завершена!*\n• Добавлено новых договоров с портала: *{added_c}*\n\n" + report_md

            await status_msg.edit_text(
                res_msg,
                parse_mode="Markdown",
                reply_markup=get_keyboard_for_bin(bin_code),
            )

        elif data.startswith("excel_"):
            bin_code = data.replace("excel_", "")
            await query.message.reply_text(f"⏳ Генерирую Excel-отчёт по БИН `{bin_code}`...", parse_mode="Markdown")

            excel_stream = generate_bin_excel(con, bin_code)
            await query.message.reply_document(
                document=excel_stream,
                filename=f"goszakup_analytics_{bin_code}.xlsx",
                caption=f"📊 Полная выгрузка договоров по БИН {bin_code}",
            )

    finally:
        con.close()


def run_bot() -> None:
    """Запуск телеграм бота."""
    if not TOKEN:
        print("❌ ОШИБКА: TELEGRAM_BOT_TOKEN не задан!")
        print("Создайте файл .env со строкой: TELEGRAM_BOT_TOKEN=ваш_токен")
        sys.exit(1)

    print("🤖 Запуск Telegram бота Goszakup Analytics...")
    from telegram.request import HTTPXRequest

    req = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = ApplicationBuilder().token(TOKEN).request(req).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", start_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("auth", auth_command_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_message_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.run_polling(bootstrap_retries=-1, poll_interval=1.0)


if __name__ == "__main__":
    run_bot()
