"""Техническая спецификация поставщика — марка, страна, завод-изготовитель.

Поддерживает все форматы файлов Приложения 17:
- DOCX (Microsoft Word документов)
- PDF текстовые документы
- PDF скан-картинки (автоматический переход на Tesseract OCR с русским и казахским языком)
- HTML таблицы

Автоматический алгоритм распознавания графических сканов (OCR):
1. Пробует моментальное извлечение текста через pdftotext
2. Если текст отсутствует (менее 50 символов), автоматически переходит на OCR:
   - Преобразует страницы PDF в высокочеткие изображения PNG (pdftoppm)
   - Распознает текст через Tesseract OCR (модели rus + kaz + eng)
   - Извлекает марки, бренды, страны и заводы из распознанного изображения!
"""

from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict

from selectolax.parser import HTMLParser

from .store import upsert

log = logging.getLogger(__name__)

COMMON_COUNTRIES = (
    "КИТАЙСКАЯ РЕСПУБЛИКА", "КИТАЙСКАЯ НАРОДНАЯ РЕСПУБЛИКА", "КНР",
    "ГЕРМАНИЯ", "ҚАЗАҚСТАН", "КАЗАХСТАН", "ҚЫТАЙ", "КИТАЙ", "РЕСЕЙ", "РОССИЯ", 
    "ТАЙЛАНД", "ТАИЛАНД", "АМЕРИКА ҚҰРАМА ШТАТТАРЫ", "США", "ФРАНЦИЯ", "ИТАЛИЯ", 
    "ШВЕЦИЯ", "ЯПОНИЯ", "ТҮРКИЯ", "ТУРЦИЯ", "ОҢТҮСТІК КОРЕЯ", "КОРЕЯ", "ИНДИЯ", "ҮНДІСТАН", "КОСТА-РИКА", "МАЛАЙЗИЯ"
)

# Подписи сопоставляются от самых длинных и точных к коротким
LABELS = (
    ("№ конкурса", "announce_ref"),
    ("Конкурстың №", "announce_ref"),
    ("№ лота", "lot_ref"),
    ("Лоттың №", "lot_ref"),
    ("Наименование лота", "lot_name"),
    ("Наименование заказчика", "customer"),
    ("Наименование поставщика", "supplier_name"),
    ("Бизнес-идентификационный номер поставщика", "supplier_bin"),
    ("Өнім берушінің бизнес- сәйкестендіру нөмірі", "supplier_bin"),
    ("Өнім берушінің БСН", "supplier_bin"),
    # Марки, бренды и модели
    ("Наименование товара с указанием марки", "brand_model"),
    ("Маркасы және/немесе тауар белгісі", "brand_model"),
    ("Маркасы және / немесе тауар белгісі", "brand_model"),
    ("Торговая марка", "brand_model"),
    ("Товарный знак", "brand_model"),
    ("Тауарлық белгісі", "brand_model"),
    ("Тауар белгісі", "brand_model"),
    ("Бренд", "brand_model"),
    ("Бренді", "brand_model"),
    ("Марка", "brand_model"),
    ("Модель", "brand_model"),
    ("Тауардың маркасы", "brand_model"),
    # Страны
    ("Страна происхождения", "country"),
    ("Страна изготовитель", "country"),
    ("Страна-изготовитель", "country"),
    ("Шыққан елі", "country"),
    ("Өндіруші ел", "country"),
    ("Дайындаушы ел", "country"),
    ("Страна", "country"),
    # Заводы и производители
    ("Завод-изготовитель", "manufacturer"),
    ("Дайындаушы зауыт", "manufacturer"),
    ("Өндіруші зауыт", "manufacturer"),
    ("Өндіруші", "manufacturer"),
    ("Производитель", "manufacturer"),
    ("Изготовитель", "manufacturer"),
    # Прочее
    ("Год выпуска", "year_made"),
    ("Шығарылған жылы", "year_made"),
    ("Гарантийный срок", "warranty"),
    ("Кепілдік мерзімі", "warranty"),
    ("Наименование национальных стандартов", "standard"),
)

ANNOUNCE_RE = re.compile(r"(\d{6,})")


def _scan_free_text(text: str, out: Dict[str, str]) -> None:
    """Извлекает марку, страну и завод из любого текста ячеек или результатов OCR."""
    if not text:
        return

    if not out.get("country"):
        for cntry in COMMON_COUNTRIES:
            if re.search(r"\b" + re.escape(cntry) + r"\b", text, re.I):
                out["country"] = cntry
                break

    if not out.get("brand_model"):
        m_b = re.search(r"(?:марка|модель|бренд|бренді|товарный\s+знак|торговая\s+марка|тауарлық\s+белгісі)\s*[:：]?\s*([^\n;]{2,80})", text, re.I)
        if m_b:
            cleaned = m_b.group(1).strip(" .;:-*")
            if cleaned and cleaned.lower() != "нет данных":
                out["brand_model"] = cleaned

    if not out.get("manufacturer"):
        m_m = re.search(r"(?:завод|изготовитель|производитель|өндіруші|дайындаушы)\s*(?:\([^)]*\))?\s*[:：]?\s*([^\n;]{2,120})", text, re.I)
        if m_m:
            cleaned_m = re.sub(r"^\s*\(?\s*(?:дайындаушы|изготовитель|өндіруші)[^\)]*\)?\s*", "", m_m.group(1), flags=re.I).strip(" .;:-*")
            if cleaned_m and cleaned_m.lower() != "нет данных":
                out["manufacturer"] = cleaned_m


def parse_docx(content_bytes: bytes) -> Dict[str, str]:
    """Разбирает DOCX файл (Zip архив с word/document.xml)."""
    out: Dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
            doc_xml = z.read("word/document.xml")
        tree = ET.fromstring(doc_xml)
        texts = [node.text for node in tree.iter() if node.tag.endswith("}t") and node.text]
        text = " ".join(texts)
        text = re.sub(r"\s+", " ", text)

        m_bin = re.search(r"(?:сәйкестендіру\s+нөмірі|БИН\s+поставщика|БСН)\s*[:：]?\s*(\d{12})", text, re.I)
        if m_bin:
            out["supplier_bin"] = m_bin.group(1)

        m_ann = re.search(r"(?:Конкурстың\s*№?|№?\s*конкурса)\s*[:：]?\s*(\d{6,})", text, re.I)
        if m_ann:
            out["announce_id"] = m_ann.group(1)

        m_lot = re.search(r"(?:Лоттың|№?\s*лота)\s*№?\s*[:：]?\s*№?\s*([^\s]{3,40})", text, re.I)
        if m_lot:
            out["lot_number"] = m_lot.group(1)

        m_brand = re.search(
            r"(?:Маркасы|Марка|Бренд|Бренді|Торговая\s+марка|Товарный\s+знак|Тауарлық\s+белгісі|Модель|Тауардың\s+маркасы|указанием\s+марки)\s*(?:\/|\s|және)?\s*(?:моделі)?\s*[:：]?\s*([^\n]{2,120}?)(?:Шыққан|Страна|Дайындаушы|Завод|Өндіруші|$)",
            text,
            re.I,
        )
        if m_brand:
            out["brand_model"] = m_brand.group(1).strip(" .;:-*")

        m_country = re.search(
            r"(?:Шыққан\s+елі|Страна\s+происхождения|Өндіруші\s+ел|Страна-изготовитель|Страна)\s*[:：]?\s*([^\n]{2,60}?)(?:Дайындаушы|Завод|Өндіруші|Изготовитель|Год|Шығарылған|$)",
            text,
            re.I,
        )
        if m_country:
            out["country"] = m_country.group(1).strip(" .;:-*")

        m_mfr = re.search(
            r"(?:Дайындаушы\s+зауыт|Завод-изготовитель|Өндіруші\s+зауыт|Өндіруші|Производитель|Изготовитель)\s*(?:\([^)]*\))?\s*[:：]?\s*([^\n]{2,180}?)(?:Шығарылған|Год|Кепілдік|Гарантия|$)",
            text,
            re.I,
        )
        if m_mfr:
            cleaned_mfr = re.sub(r"^\s*\(?\s*(?:дайындаушы|изготовитель|өндіруші)[^\)]*\)?\s*", "", m_mfr.group(1), flags=re.I).strip(" .;:-*")
            out["manufacturer"] = cleaned_mfr

        _scan_free_text(text, out)
        return out
    except Exception as e:
        log.warning("Ошибка разбора DOCX: %s", e)
        return out


def parse_pdf(content_bytes: bytes) -> str:
    """PDF -> текст (pdftotext) с автоматическим фолбэком на Tesseract OCR при фотосканах."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as f:
        f.write(content_bytes)
        f.flush()
        out_file = Path(f.name).with_suffix(".txt")
        text = ""
        try:
            subprocess.run(
                ["pdftotext", "-layout", f.name, str(out_file)],
                check=True,
                capture_output=True,
                timeout=40,
            )
            text = out_file.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            pass
        finally:
            out_file.unlink(missing_ok=True)

        # Если текстовый слой отсутствует (сканированная скан-картинка) -> запускаем OCR
        if len(text) < 50:
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    subprocess.run(
                        ["pdftoppm", "-png", "-r", "150", f.name, os.path.join(tmpdir, "page")],
                        check=True,
                        capture_output=True,
                        timeout=40,
                    )
                    pages = sorted([os.path.join(tmpdir, fn) for fn in os.listdir(tmpdir) if fn.startswith("page-")])
                    ocr_parts = []
                    tessdata_path = os.path.abspath("data/tessdata")
                    for pfile in pages[:4]:  # Сканируем первые 4 страницы
                        res = subprocess.run(
                            [
                                "tesseract", pfile, "stdout",
                                "--tessdata-dir", tessdata_path,
                                "-l", "rus+kaz+eng"
                            ],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        if res.stdout:
                            ocr_parts.append(res.stdout)
                    if ocr_parts:
                        text = "\n".join(ocr_parts)
                        log.info("Успешный OCR скана PDF (символов: %d)", len(text))
                except Exception as e:
                    log.warning("Ошибка OCR скана: %s", e)

    return text


def parse_html_spec(html: str) -> Dict[str, str]:
    """Разбирает HTML техспецификации."""
    out: Dict[str, str] = {}
    full_text = ""

    for tr in HTMLParser(html).css("tr"):
        cells = [re.sub(r"\s+", " ", c.text(strip=True)) for c in tr.css("td,th")]
        cells = [c for c in cells if c]
        if len(cells) >= 1:
            row_text = " ".join(cells)
            full_text += " " + row_text

        if len(cells) < 2:
            continue

        label = cells[0].lstrip("№ ").strip()
        val = cells[1].strip()

        for prefix, key in LABELS:
            probe = prefix.lstrip("№ ") if prefix.startswith("№") else prefix
            if key not in out and label.lower().startswith(probe.lower()):
                out[key] = val
                break

    if "announce_ref" in out:
        m = ANNOUNCE_RE.search(out["announce_ref"])
        if m:
            out["announce_id"] = m.group(1)
    if "lot_ref" in out:
        out["lot_number"] = out["lot_ref"].lstrip("№ ").strip()

    _scan_free_text(full_text, out)
    return out


def parse_spec(content: bytes | str) -> Dict[str, str]:
    """Универсальный парсер Приложения 17 (DOCX, PDF, OCR или HTML)."""
    if isinstance(content, str):
        content_bytes = content.encode("utf-8", errors="ignore")
    else:
        content_bytes = content

    if content_bytes[:4] == b"PK\x03\x04":
        return parse_docx(content_bytes)

    if content_bytes[:4] == b"%PDF":
        pdf_text = parse_pdf(content_bytes)
        return parse_html_spec(pdf_text) if pdf_text else {}

    html_str = content if isinstance(content, str) else content_bytes.decode("utf-8", errors="ignore")
    return parse_html_spec(html_str)
