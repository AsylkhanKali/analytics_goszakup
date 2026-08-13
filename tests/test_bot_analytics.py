"""Тесты компонентов бота и аналитики по БИН."""

import io
import sqlite3
import unittest

from gz.bin_analytics import clean_bin, format_telegram_report, get_bin_analytics
from gz.excel_export import generate_bin_excel


class TestGoszakupBot(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect("data/goszakup.db")

    def tearDown(self):
        self.con.close()

    def test_clean_bin(self):
        self.assertEqual(clean_bin("920724302391"), "920724302391")
        self.assertEqual(clean_bin("БИН: 920-724-302-391"), "920724302391")

    def test_supplier_analytics(self):
        res = get_bin_analytics(self.con, "920724302391")
        self.assertIsNotNone(res["supplier"])
        self.assertEqual(res["supplier"]["name"], "ИПАйбек")
        self.assertGreaterEqual(res["supplier"]["total_contracts"], 1)

        report_md = format_telegram_report(res)
        self.assertIn("ИПАйбек", report_md)

    def test_customer_analytics(self):
        res = get_bin_analytics(self.con, "980140000491")
        self.assertIsNotNone(res["customer"])
        self.assertTrue(len(res["customer"]["name"]) > 0)

        report_md = format_telegram_report(res)
        self.assertIn("ЗАКАЗЧИК", report_md)

    def test_excel_export(self):
        buf = generate_bin_excel(self.con, "920724302391")
        self.assertIsInstance(buf, io.BytesIO)
        self.assertGreater(len(buf.getvalue()), 1000)


if __name__ == "__main__":
    unittest.main()
