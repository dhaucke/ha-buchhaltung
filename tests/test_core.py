from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pdfplumber


APP_DIR = Path(__file__).resolve().parents[1] / "buchhaltung" / "app"
sys.path.insert(0, str(APP_DIR))

from db import Database
from einvoice import create_zugferd
from euer import create_euer_csv, create_euer_pdf, euer_entries, euer_summary
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

    def test_pdf_generation_with_multiple_items(self):
        settings = self.db.settings()
        settings.update({
            "company_name": "Musterbetrieb",
            "owner_name": "Max Mustermann",
            "street": "Beispielstraße 1",
            "postal_code": "10117",
            "city": "Berlin",
            "email": "rechnung@example.invalid",
        })
        items = []
        for position, (description, quantity, unit_price) in enumerate([
            ("Webhosting", 1000, 6000),
            ("Datensicherung", 2500, 2000),
            ("Domainregistrierung", 1000, 1500),
        ], start=1):
            total = int(
                (Decimal(quantity) / 1000 * unit_price).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            items.append({
                "position": position,
                "category": "IT-Dienstleistung",
                "description": description,
                "quantity_milli": quantity,
                "unit": "pauschal" if quantity == 1000 else "Stunde",
                "unit_price_cents": unit_price,
                "total_cents": total,
                "service_period": "Juli 2026",
            })
        total_cents = sum(item["total_cents"] for item in items)
        output = Path(self.temp.name) / "multiple-items.pdf"
        create_document_pdf(
            output,
            {
                "document_type": "invoice",
                "document_number": "2026-07-0133",
                "issue_date": "2026-07-23",
                "due_date": "2026-08-06",
                "title": "Mehrere Leistungen",
                "introduction": "",
                "notes": "",
                "total_cents": total_cents,
            },
            {
                "customer_number": "1002",
                "company": "Muster GmbH",
                "contact_name": "",
                "street": "Musterweg 1",
                "postal_code": "12345",
                "city": "Berlin",
            },
            items,
            settings,
            None,
        )
        with pdfplumber.open(output) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        self.assertIn("Webhosting", text)
        self.assertIn("Datensicherung", text)
        self.assertIn("Domainregistrierung", text)
        self.assertIn("125,00 €", text)

    def test_archive_schema_migration(self):
        with self.db.connect() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(archive_files)")
            }
        self.assertIn("detected_customer_name", columns)
        self.assertIn("detected_customer_number", columns)
        self.assertIn("detected_amount_cents", columns)
        self.assertIn("customer_id", columns)

    def test_archive_links_by_customer_number_despite_historical_address(self):
        now = Database.now()
        with self.db.connect() as connection:
            customer_id = connection.execute(
                """
                INSERT INTO customers(
                    customer_number, company, street, postal_code, city,
                    created_at, updated_at
                ) VALUES ('1002','Beispiel Logistik GmbH','Neuer Weg 9','10115',
                          'Berlin',?,?)
                """,
                (now, now),
            ).lastrowid
            archive_id = connection.execute(
                """
                INSERT INTO archive_files(
                    original_filename, stored_filename, sha256, mime_type,
                    file_size, uploaded_at, document_direction,
                    detected_customer_name, detected_customer_number,
                    detected_street, detected_postal_code, detected_city
                ) VALUES ('rechnung-2017.pdf','rechnung-2017.pdf','hash-2017',
                          'application/pdf',123,?,'outgoing',
                          'Beispiel Logistik GmbH','1002','Alte Straße 1','12345',
                          'Musterstadt')
                """,
                (now,),
            ).lastrowid

            linked = Database.link_archives_by_customer_number(connection)
            archive = connection.execute(
                """
                SELECT customer_id, detected_street, detected_postal_code,
                       detected_city FROM archive_files WHERE id=?
                """,
                (archive_id,),
            ).fetchone()

        self.assertEqual(linked, 1)
        self.assertEqual(archive["customer_id"], customer_id)
        self.assertEqual(archive["detected_street"], "Alte Straße 1")
        self.assertEqual(archive["detected_postal_code"], "12345")
        self.assertEqual(archive["detected_city"], "Musterstadt")

    def test_archive_does_not_link_by_name_or_address(self):
        now = Database.now()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO customers(
                    customer_number, company, street, postal_code, city,
                    created_at, updated_at
                ) VALUES ('1002','Beispiel Logistik GmbH','Alte Straße 1',
                          '12345','Musterstadt',?,?)
                """,
                (now, now),
            )
            archive_id = connection.execute(
                """
                INSERT INTO archive_files(
                    original_filename, stored_filename, sha256, mime_type,
                    file_size, uploaded_at, document_direction,
                    detected_customer_name, detected_customer_number,
                    detected_street, detected_postal_code, detected_city
                ) VALUES ('ohne-kundennummer.pdf','ohne-kundennummer.pdf',
                          'hash-ohne-nummer','application/pdf',123,?,'outgoing',
                          'Beispiel Logistik GmbH','','Alte Straße 1','12345',
                          'Musterstadt')
                """,
                (now,),
            ).lastrowid

            linked = Database.link_archives_by_customer_number(connection)
            customer_id = connection.execute(
                "SELECT customer_id FROM archive_files WHERE id=?",
                (archive_id,),
            ).fetchone()["customer_id"]

        self.assertEqual(linked, 0)
        self.assertIsNone(customer_id)

    def test_outgoing_duplicate_is_found_by_invoice_number(self):
        now = Database.now()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO archive_files(
                    original_filename, stored_filename, sha256, mime_type,
                    file_size, uploaded_at, document_direction,
                    detected_invoice_number, detected_customer_name
                ) VALUES ('original.pdf','original.pdf','first-hash',
                          'application/pdf',123,?,'outgoing',
                          '2026-07-0133','Beispiel GmbH')
                """,
                (now,),
            )
            duplicate = Database.find_semantic_archive_duplicate(
                connection,
                "outgoing",
                " 2026-07-0133 ",
                "Anderer erkannter Name",
            )

        self.assertIsNotNone(duplicate)
        self.assertEqual(duplicate["source"], "archive")

    def test_incoming_duplicate_requires_matching_supplier(self):
        now = Database.now()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO archive_files(
                    original_filename, stored_filename, sha256, mime_type,
                    file_size, uploaded_at, document_direction,
                    detected_invoice_number, detected_customer_name
                ) VALUES ('lieferant-a.pdf','lieferant-a.pdf','supplier-a-hash',
                          'application/pdf',123,?,'incoming',
                          'RE-100','Lieferant A GmbH')
                """,
                (now,),
            )
            same_supplier = Database.find_semantic_archive_duplicate(
                connection, "incoming", "RE-100", "lieferant a gmbh"
            )
            other_supplier = Database.find_semantic_archive_duplicate(
                connection, "incoming", "RE-100", "Lieferant B GmbH"
            )

        self.assertIsNotNone(same_supplier)
        self.assertIsNone(other_supplier)

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

    def test_incoming_invoice_and_supplier_schema_exists(self):
        with self.db.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            archive_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(archive_files)")
            }
        self.assertIn("suppliers", tables)
        self.assertIn("incoming_invoices", tables)
        self.assertIn("document_direction", archive_columns)
        self.assertIn("accounting_status", archive_columns)
        self.assertIn("payment_date", archive_columns)

    def test_credit_reminder_and_einvoice_schema_exists(self):
        with self.db.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            document_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(documents)")
            }
        self.assertIn("payment_reminders", tables)
        self.assertIn("e_invoice_files", tables)
        self.assertIn("credit_reason", document_columns)
        self.assertIn("settlement_type", document_columns)
        self.assertIn("settled_at", document_columns)

    def test_euer_uses_payment_date_and_deductible_expense(self):
        now = Database.now()
        with self.db.connect() as connection:
            customer_id = connection.execute(
                """
                INSERT INTO customers(customer_number, company, street, postal_code, city, created_at, updated_at)
                VALUES ('1','Kunde GmbH','Weg 1','10115','Berlin',?,?)
                """,
                (now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO documents(
                    document_type, document_number, status, customer_id, issue_date,
                    total_cents, paid_at, created_at, updated_at
                ) VALUES ('invoice','2025-12-0001','paid',?,'2025-12-20',12000,'2026-01-03T12:00:00+01:00',?,?)
                """,
                (customer_id, now, now),
            )
            supplier_id = connection.execute(
                "INSERT INTO suppliers(company,created_at,updated_at) VALUES ('Hosting AG',?,?)",
                (now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO incoming_invoices(
                    supplier_id, invoice_number, invoice_date, payment_date, status,
                    eur_category, gross_cents, business_share_percent, deductible_cents,
                    created_at, updated_at
                ) VALUES (?, 'R-1', '2025-12-29', '2026-01-05', 'paid',
                          'Software und IT', 5000, 80, 4000, ?, ?)
                """,
                (supplier_id, now, now),
            )
            entries = euer_entries(connection, 2026)
        summary = euer_summary(entries)
        self.assertEqual(len(entries), 2)
        self.assertEqual(summary["income_cents"], 12000)
        self.assertEqual(summary["expense_cents"], 4000)
        self.assertEqual(summary["profit_cents"], 8000)
        csv_data = create_euer_csv(entries).decode("utf-8-sig")
        self.assertIn("2026-01-03;Einnahme", csv_data)
        self.assertIn("2026-01-05;Ausgabe", csv_data)

    def test_euer_pdf_generation(self):
        output = Path(self.temp.name) / "euer.pdf"
        entries = [{
            "kind": "Einnahme", "source": "Ausgangsrechnung", "source_id": 1,
            "date": "2026-01-03", "number": "2025-12-0001",
            "party": "Kunde GmbH", "category": "Betriebseinnahmen",
            "amount_cents": 12000,
        }, {
            "kind": "Ausgabe", "source": "Eingangsrechnung", "source_id": 1,
            "date": "2026-01-05", "number": "R-1", "party": "Hosting AG",
            "category": "Software und IT", "amount_cents": 4000,
        }]
        create_euer_pdf(output, 2026, entries, {"company_name": "Musterbetrieb"})
        self.assertTrue(output.read_bytes().startswith(b"%PDF"))
        self.assertGreater(output.stat().st_size, 2_000)

    def test_refunded_credit_reduces_cash_income(self):
        now = Database.now()
        with self.db.connect() as connection:
            customer_id = connection.execute(
                """
                INSERT INTO customers(customer_number, company, street, postal_code, city, created_at, updated_at)
                VALUES ('1','Kunde GmbH','Weg 1','10115','Berlin',?,?)
                """,
                (now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO documents(
                    document_type, document_number, status, customer_id, issue_date,
                    total_cents, paid_at, settlement_type, settled_at, created_at, updated_at
                ) VALUES ('credit','GS-2026-01-0002','refunded',?,'2026-01-10',
                          2000,'2026-01-12T12:00:00','refund','2026-01-12T12:00:00',?,?)
                """,
                (customer_id, now, now),
            )
            entries = euer_entries(connection, 2026)
        self.assertEqual(entries[0]["source"], "Ausgezahlte Gutschrift")
        self.assertEqual(entries[0]["amount_cents"], -2000)
        self.assertEqual(euer_summary(entries)["income_cents"], -2000)

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

    def test_zugferd_generation_and_embedded_xml_validation(self):
        try:
            from facturx import get_xml_from_pdf, xml_check_xsd
        except ImportError:
            self.skipTest("factur-x ist in der lokalen Testumgebung nicht installiert")
        settings = self.db.settings()
        settings.update({
            "company_name": "Musterbetrieb",
            "owner_name": "Max Mustermann",
            "street": "Beispielstraße 1",
            "postal_code": "10117",
            "city": "Berlin",
            "country": "Deutschland",
            "email": "rechnung@example.invalid",
            "tax_number": "12/345/67890",
            "iban": "DE02120300000000202051",
        })
        document = {
            "document_type": "invoice", "document_number": "2026-07-0133",
            "issue_date": "2026-07-23", "due_date": "2026-08-06",
            "service_start": None, "service_end": None, "payment_terms_days": 14,
            "title": "Hosting", "introduction": "", "notes": "",
            "credit_reason": "", "total_cents": 11000,
        }
        customer = {
            "customer_number": "1003", "company": "Test GmbH", "contact_name": "",
            "street": "Testweg 1", "postal_code": "12345", "city": "Berlin",
            "country": "Deutschland", "email": "kunde@example.invalid",
            "buyer_reference": "",
        }
        items = [{
            "position": 1, "category": "Hosting", "description": "Webserver",
            "quantity_milli": 1000, "unit": "pauschal", "unit_price_cents": 6000,
            "total_cents": 6000, "service_period": "Juli 2026",
        }, {
            "position": 2, "category": "Service", "description": "Datensicherung",
            "quantity_milli": 2500, "unit": "Stunde", "unit_price_cents": 2000,
            "total_cents": 5000, "service_period": "Juli 2026",
        }]
        regular = Path(self.temp.name) / "invoice.pdf"
        output = Path(self.temp.name) / "invoice-zugferd.pdf"
        xml_output = Path(self.temp.name) / "factur-x.xml"
        create_document_pdf(regular, document, customer, items, settings, None)
        result = create_zugferd(
            regular, output, xml_output, document, customer, items, settings
        )
        embedded_name, embedded_xml = get_xml_from_pdf(
            output.read_bytes(), check_xsd=True, check_schematron=False
        )
        self.assertEqual(embedded_name, "factur-x.xml")
        self.assertTrue(
            xml_check_xsd(embedded_xml, flavor="factur-x", level="en16931")
        )
        self.assertTrue(result["xsd_valid"])


if __name__ == "__main__":
    unittest.main()
