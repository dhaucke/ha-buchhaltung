from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "buchhaltung" / "app"
sys.path.insert(0, str(APP_DIR))

from db import Database
from pdfgen import create_document_pdf
from pdfimport import analyze_invoice_pdf


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(self.temp.name)
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_new_installation_is_unconfigured_and_neutral(self):
        settings = self.db.settings()
        self.assertEqual(settings["setup_complete"], "0")
        self.assertEqual(settings["company_name"], "")
        self.assertEqual(settings["invoice_counter"], "0")
        self.assertEqual(settings["customer_counter"], "0")

    def test_schema_prevents_duplicate_document_numbers(self):
        now = Database.now()
        with self.db.connect() as connection:
            customer = connection.execute(
                """
                INSERT INTO customers(customer_number, company, street, postal_code, city, created_at, updated_at)
                VALUES ('1003','Test GmbH','Teststraße 1','10115','Berlin',?,?)
                """,
                (now, now),
            ).lastrowid
            values = ("invoice", "2026-07-0133", customer, "2026-07-23", now, now)
            connection.execute(
                """
                INSERT INTO documents(document_type, document_number, customer_id, issue_date, created_at, updated_at)
                VALUES (?,?,?,?,?,?)
                """,
                values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO documents(document_type, document_number, customer_id, issue_date, created_at, updated_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    values,
                )

    def test_pdf_generation(self):
        settings = self.db.settings()
        settings.update({
            "company_name": "Musterbetrieb",
            "owner_name": "Max Mustermann",
            "street": "Beispielstraße 1",
            "postal_code": "10117",
            "city": "Berlin",
            "email": "rechnung@example.invalid",
        })
        document = {
            "document_type": "invoice",
            "document_number": "2026-07-0133",
            "issue_date": "2026-07-23",
            "due_date": "2026-07-30",
            "title": "Hosting Services",
            "introduction": "",
            "notes": "",
            "total_cents": 6000,
        }
        customer = {
            "customer_number": "1002",
            "company": "Muster GmbH",
            "contact_name": "",
            "street": "Musterweg 1",
            "postal_code": "12345",
            "city": "Berlin",
        }
        items = [{
            "position": 1,
            "category": "Hosting",
            "description": "Virtueller Webserver",
            "quantity_milli": 1000,
            "unit": "pauschal",
            "unit_price_cents": 6000,
            "total_cents": 6000,
            "service_period": "01.07.2026–30.09.2026",
        }]
        output = Path(self.temp.name) / "test.pdf"
        create_document_pdf(
            output, document, customer, items, settings, None,
        )
        self.assertTrue(output.read_bytes().startswith(b"%PDF"))
        self.assertGreater(output.stat().st_size, 2_500)

    def test_archive_schema_migration(self):
        with self.db.connect() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(archive_files)")
            }
        self.assertIn("detected_customer_name", columns)
        self.assertIn("detected_customer_number", columns)
        self.assertIn("detected_amount_cents", columns)
        self.assertIn("customer_id", columns)

    def test_recurring_invoice_schema_exists(self):
        with self.db.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("recurring_invoices", tables)
        self.assertIn("recurring_runs", tables)

    def test_generated_invoice_can_be_analyzed(self):
        settings = self.db.settings()
        settings.update({
            "company_name": "Musterbetrieb",
            "owner_name": "Max Mustermann",
            "street": "Beispielstraße 1",
            "postal_code": "10117",
            "city": "Berlin",
            "email": "rechnung@example.invalid",
        })
        output = Path(self.temp.name) / "Rechnung 2026-07-0133.pdf"
        create_document_pdf(
            output,
            {
                "document_type": "invoice", "document_number": "2026-07-0133",
                "issue_date": "2026-07-23", "due_date": "2026-07-30",
                "title": "Hosting", "introduction": "", "notes": "", "total_cents": 6000,
            },
            {
                "customer_number": "1003", "company": "Test GmbH", "contact_name": "",
                "street": "Testweg 1", "postal_code": "12345", "city": "Berlin",
            },
            [{
                "position": 1, "category": "Hosting", "description": "Webserver",
                "quantity_milli": 1000, "unit": "pauschal", "unit_price_cents": 6000,
                "total_cents": 6000, "service_period": "Juli 2026",
            }],
            settings, None,
        )
        result = analyze_invoice_pdf(output.read_bytes(), output.name, settings)
        self.assertEqual(result["invoice_number"], "2026-07-0133")
        self.assertEqual(result["issue_date"], "2026-07-23")
        self.assertEqual(result["amount_cents"], 6000)
        self.assertEqual(result["customer_name"], "Test GmbH")
        self.assertEqual(result["customer_number"], "1003")


if __name__ == "__main__":
    unittest.main()
