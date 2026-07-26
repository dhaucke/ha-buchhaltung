from __future__ import annotations

import io
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

from db import Database, suggested_payment_date
from einvoice import create_zugferd, validate_schematron
from euer import (
    create_euer_csv, create_euer_pdf, euer_entries, euer_summary, vat_liability_by_period,
)
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

    def test_payment_date_suggestion_uses_terms_and_skips_weekend(self):
        self.assertEqual(
            suggested_payment_date("2026-07-04", 14),
            "2026-07-20",
        )
        self.assertEqual(
            suggested_payment_date("2026-07-06", 14),
            "2026-07-20",
        )
        self.assertEqual(suggested_payment_date("", 14), "")

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

    def test_review_queue_returns_only_open_archives(self):
        now = Database.now()
        with self.db.connect() as connection:
            first_id = connection.execute(
                """
                INSERT INTO archive_files(
                    original_filename, stored_filename, sha256, mime_type,
                    file_size, uploaded_at, document_direction,
                    detected_invoice_number
                ) VALUES ('eins.pdf','eins.pdf','queue-one','application/pdf',
                          123,?,'outgoing','2025-12-0099')
                """,
                (now,),
            ).lastrowid
            newest_id = connection.execute(
                """
                INSERT INTO archive_files(
                    original_filename, stored_filename, sha256, mime_type,
                    file_size, uploaded_at, document_direction,
                    detected_invoice_number
                ) VALUES ('neu.pdf','neu.pdf','queue-new','application/pdf',
                          123,?,'outgoing','2026-07-0133')
                """,
                (now,),
            ).lastrowid
            reviewed_id = connection.execute(
                """
                INSERT INTO archive_files(
                    original_filename, stored_filename, sha256, mime_type,
                    file_size, uploaded_at, document_direction, reviewed_at
                ) VALUES ('zwei.pdf','zwei.pdf','queue-two','application/pdf',
                          123,?,'outgoing',?)
                """,
                (now, now),
            ).lastrowid
            incoming_id = connection.execute(
                """
                INSERT INTO archive_files(
                    original_filename, stored_filename, sha256, mime_type,
                    file_size, uploaded_at, document_direction
                ) VALUES ('drei.pdf','drei.pdf','queue-three','application/pdf',
                          123,?,'incoming')
                """,
                (now,),
            ).lastrowid

            outgoing_next = Database.next_unreviewed_archive_id(
                connection, reviewed_id, "outgoing"
            )
            incoming_next = Database.next_unreviewed_archive_id(
                connection, None, "incoming"
            )

        self.assertNotEqual(first_id, newest_id)
        self.assertEqual(outgoing_next, newest_id)
        self.assertEqual(incoming_next, incoming_id)

    def test_review_queue_can_filter_by_customer_and_number(self):
        now = Database.now()
        with self.db.connect() as connection:
            customer_id = connection.execute(
                """
                INSERT INTO customers(
                    customer_number, company, street, postal_code, city,
                    created_at, updated_at
                ) VALUES ('1002','Beispiel Logistik GmbH','','','',?,?)
                """,
                (now, now),
            ).lastrowid
            matching_id = connection.execute(
                """
                INSERT INTO archive_files(
                    customer_id, original_filename, stored_filename, sha256,
                    mime_type, file_size, uploaded_at, document_direction,
                    detected_invoice_number
                ) VALUES (?,'passend.pdf','passend.pdf','filter-match',
                          'application/pdf',123,?,'outgoing','2026-07-0133')
                """,
                (customer_id, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO archive_files(
                    original_filename, stored_filename, sha256, mime_type,
                    file_size, uploaded_at, document_direction,
                    detected_invoice_number, detected_customer_name,
                    detected_customer_number
                ) VALUES ('andere.pdf','andere.pdf','filter-other',
                          'application/pdf',123,?,'outgoing','2026-08-0134',
                          'Andere GmbH','2000')
                """,
                (now,),
            )
            result = Database.next_unreviewed_archive_id(
                connection,
                None,
                "outgoing",
                "logistik",
                "002",
            )

        self.assertEqual(result, matching_id)

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

    def test_pdf_generation_with_mixed_tax_rates(self):
        settings = self.db.settings()
        settings.update({
            "company_name": "Musterbetrieb",
            "owner_name": "Max Mustermann",
            "street": "Beispielstraße 1",
            "postal_code": "10117",
            "city": "Berlin",
            "email": "rechnung@example.invalid",
            "small_business_enabled": "0",
        })
        items = [{
            "position": 1, "category": "Hosting", "description": "Webserver",
            "quantity_milli": 1000, "unit": "pauschal", "unit_price_cents": 6000,
            "total_cents": 6000, "tax_rate_bp": 1900, "service_period": "Juli 2026",
        }, {
            "position": 2, "category": "Literatur", "description": "Fachbuch",
            "quantity_milli": 1000, "unit": "Stück", "unit_price_cents": 5000,
            "total_cents": 5000, "tax_rate_bp": 700, "service_period": "",
        }]
        net_total = sum(item["total_cents"] for item in items)
        tax_total = 1140 + 350
        output = Path(self.temp.name) / "mixed-tax.pdf"
        create_document_pdf(
            output,
            {
                "document_type": "invoice", "document_number": "2026-07-0133",
                "issue_date": "2026-07-23", "due_date": "2026-08-06",
                "title": "Gemischte Steuersätze", "introduction": "", "notes": "",
                "total_cents": net_total + tax_total, "tax_cents": tax_total,
            },
            {
                "customer_number": "1002", "company": "Muster GmbH", "contact_name": "",
                "street": "Musterweg 1", "postal_code": "12345", "city": "Berlin",
            },
            items, settings, None,
        )
        with pdfplumber.open(output) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        self.assertIn("Nettobetrag", text)
        self.assertIn("110,00 €", text)
        self.assertIn("zzgl. 19% USt", text)
        self.assertIn("11,40 €", text)
        self.assertIn("zzgl. 7% USt", text)
        self.assertIn("3,50 €", text)
        self.assertIn("Rechnungsbetrag", text)
        self.assertIn("124,90 €", text)

    def test_kleinunternehmer_pdf_unaffected_by_tax_columns(self):
        settings = self.db.settings()
        settings.update({
            "company_name": "Musterbetrieb",
            "owner_name": "Max Mustermann",
            "street": "Beispielstraße 1",
            "postal_code": "10117",
            "city": "Berlin",
            "email": "rechnung@example.invalid",
        })
        output = Path(self.temp.name) / "kleinunternehmer.pdf"
        create_document_pdf(
            output,
            {
                "document_type": "invoice", "document_number": "2026-07-0133",
                "issue_date": "2026-07-23", "due_date": "2026-07-30",
                "title": "Hosting", "introduction": "", "notes": "",
                "total_cents": 6000, "tax_cents": 0,
            },
            {
                "customer_number": "1002", "company": "Muster GmbH", "contact_name": "",
                "street": "Musterweg 1", "postal_code": "12345", "city": "Berlin",
            },
            [{
                "position": 1, "category": "Hosting", "description": "Virtueller Webserver",
                "quantity_milli": 1000, "unit": "pauschal", "unit_price_cents": 6000,
                "total_cents": 6000, "tax_rate_bp": 0, "service_period": "Juli 2026",
            }],
            settings, None,
        )
        with pdfplumber.open(output) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        self.assertNotIn("USt-Satz", text)
        self.assertNotIn("zzgl.", text)
        self.assertNotIn("Nettobetrag", text)
        self.assertIn("Rechnungsbetrag", text)
        self.assertIn("Steuerbefreiung für Kleinunternehmer", text)

    def test_incoming_deductible_split_computes_vorsteuer(self):
        os.environ.setdefault("HD_DATA_DIR", tempfile.mkdtemp())
        from web import incoming_deductible_split

        settings = self.db.settings()
        settings["small_business_enabled"] = "0"
        deductible, vorsteuer, stored_rate = incoming_deductible_split(
            11900, 80, 1900, settings
        )
        self.assertEqual(stored_rate, 1900)
        self.assertEqual(deductible, 8000)
        self.assertEqual(vorsteuer, 1520)

        settings["small_business_enabled"] = "1"
        deductible, vorsteuer, stored_rate = incoming_deductible_split(
            11900, 80, 1900, settings
        )
        self.assertEqual(stored_rate, 0)
        self.assertEqual(vorsteuer, 0)
        self.assertEqual(deductible, 9520)

    def test_euer_reports_vorsteuer_for_regelbesteuerung_incoming_invoice(self):
        now = Database.now()
        with self.db.connect() as connection:
            supplier_id = connection.execute(
                "INSERT INTO suppliers(company,created_at,updated_at) VALUES ('Hosting AG',?,?)",
                (now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO incoming_invoices(
                    supplier_id, invoice_number, invoice_date, payment_date, status,
                    eur_category, gross_cents, business_share_percent, deductible_cents,
                    tax_rate_bp, vorsteuer_cents, created_at, updated_at
                ) VALUES (?, 'R-2', '2026-01-02', '2026-01-05', 'paid',
                          'Software und IT', 11900, 100, 10000, 1900, 1900, ?, ?)
                """,
                (supplier_id, now, now),
            )
            entries = euer_entries(connection, 2026)
        summary = euer_summary(entries)
        self.assertEqual(summary["expense_cents"], 10000)
        self.assertEqual(summary["vorsteuer_cents"], 1900)
        csv_data = create_euer_csv(entries).decode("utf-8-sig")
        self.assertIn("Gezahlte Vorsteuer", csv_data)

    def _recurring_invoice_with_tax_rate(self, connection, customer_id, tax_rate_bp):
        now = Database.now()
        return connection.execute(
            """
            INSERT INTO recurring_invoices(
                customer_id, active, title, category, description, service_period_template,
                quantity_milli, unit, unit_price_cents, billing_day, auto_finalize, auto_send,
                send_format, tax_rate_bp, created_at, updated_at
            ) VALUES (?, 1, 'Hosting', 'Hosting', 'Webserver', '{monat} {jahr}', 1000, 'pauschal',
                      10000, 1, 0, 0, 'auto', ?, ?, ?)
            """,
            (customer_id, tax_rate_bp, now, now),
        ).lastrowid

    def test_run_recurring_invoice_charges_vat_in_regelbesteuerung(self):
        os.environ.setdefault("HD_DATA_DIR", tempfile.mkdtemp())
        import web as web_module
        from web import run_recurring_invoice

        self.db.update_settings({
            "company_name": "Musterbetrieb", "owner_name": "Max Mustermann",
            "street": "Beispielstraße 1", "postal_code": "10117", "city": "Berlin",
            "email": "rechnung@example.invalid", "small_business_enabled": "0",
        })
        # run_recurring_invoice reads settings via web.py's module-level DB singleton,
        # not the connection it's given – point it at this test's database.
        original_db = web_module.DB
        web_module.DB = self.db
        self.addCleanup(setattr, web_module, "DB", original_db)
        now = Database.now()
        with self.db.connect() as connection:
            customer_id = connection.execute(
                """
                INSERT INTO customers(customer_number, company, street, postal_code, city, email, created_at, updated_at)
                VALUES ('1','Kunde GmbH','Weg 1','10115','Berlin','kunde@example.invalid',?,?)
                """,
                (now, now),
            ).lastrowid
            recurring_id = self._recurring_invoice_with_tax_rate(connection, customer_id, 1900)
            document_id, status, error = run_recurring_invoice(
                connection, recurring_id, date(2026, 7, 1), manual=True
            )
            document = connection.execute(
                "SELECT * FROM documents WHERE id=?", (document_id,)
            ).fetchone()
            item = connection.execute(
                "SELECT * FROM document_items WHERE document_id=?", (document_id,)
            ).fetchone()
        self.assertEqual(status, "draft_created", error)
        self.assertEqual(document["total_cents"], 11900)
        self.assertEqual(document["tax_cents"], 1900)
        self.assertEqual(item["total_cents"], 10000)
        self.assertEqual(item["tax_rate_bp"], 1900)

    def test_run_recurring_invoice_forces_no_vat_in_kleinunternehmer_mode(self):
        os.environ.setdefault("HD_DATA_DIR", tempfile.mkdtemp())
        import web as web_module
        from web import run_recurring_invoice

        self.db.update_settings({
            "company_name": "Musterbetrieb", "owner_name": "Max Mustermann",
            "street": "Beispielstraße 1", "postal_code": "10117", "city": "Berlin",
            "email": "rechnung@example.invalid", "small_business_enabled": "1",
        })
        original_db = web_module.DB
        web_module.DB = self.db
        self.addCleanup(setattr, web_module, "DB", original_db)
        now = Database.now()
        with self.db.connect() as connection:
            customer_id = connection.execute(
                """
                INSERT INTO customers(customer_number, company, street, postal_code, city, email, created_at, updated_at)
                VALUES ('1','Kunde GmbH','Weg 1','10115','Berlin','kunde@example.invalid',?,?)
                """,
                (now, now),
            ).lastrowid
            # tax_rate_bp is still set on the template (e.g. left over from a prior
            # Regelbesteuerung period) – Kleinunternehmer mode must ignore it.
            recurring_id = self._recurring_invoice_with_tax_rate(connection, customer_id, 1900)
            document_id, status, error = run_recurring_invoice(
                connection, recurring_id, date(2026, 7, 1), manual=True
            )
            document = connection.execute(
                "SELECT * FROM documents WHERE id=?", (document_id,)
            ).fetchone()
            item = connection.execute(
                "SELECT * FROM document_items WHERE document_id=?", (document_id,)
            ).fetchone()
        self.assertEqual(status, "draft_created", error)
        self.assertEqual(document["total_cents"], 10000)
        self.assertEqual(document["tax_cents"], 0)
        self.assertEqual(item["tax_rate_bp"], 0)

    def test_kleinunternehmer_threshold_warning_levels(self):
        os.environ.setdefault("HD_DATA_DIR", tempfile.mkdtemp())
        from web import kleinunternehmer_threshold_warning

        now = Database.now()
        with self.db.connect() as connection:
            customer_id = connection.execute(
                """
                INSERT INTO customers(customer_number, company, street, postal_code, city, created_at, updated_at)
                VALUES ('1','Kunde GmbH','Weg 1','10115','Berlin',?,?)
                """,
                (now, now),
            ).lastrowid

            def paid_invoice(number, total_cents, paid_at):
                connection.execute(
                    """
                    INSERT INTO documents(document_type, document_number, status, customer_id,
                    issue_date, total_cents, paid_at, created_at, updated_at)
                    VALUES ('invoice', ?, 'paid', ?, ?, ?, ?, ?, ?)
                    """,
                    (number, customer_id, paid_at[:10], total_cents, paid_at, now, now),
                )

            settings = {"small_business_enabled": "1"}
            self.assertEqual(
                kleinunternehmer_threshold_warning(connection, settings, date(2026, 7, 1)), ""
            )

            paid_invoice("2026-1", 8_500_000, "2026-01-10T10:00:00")
            warning = kleinunternehmer_threshold_warning(connection, settings, date(2026, 7, 1))
            self.assertIn("nähert sich", warning)

            paid_invoice("2026-2", 2_000_000, "2026-02-10T10:00:00")
            warning = kleinunternehmer_threshold_warning(connection, settings, date(2026, 7, 1))
            self.assertIn("entfällt damit ab sofort", warning)

            paid_invoice("2025-1", 3_000_000, "2025-06-10T10:00:00")
            warning = kleinunternehmer_threshold_warning(connection, settings, date(2026, 7, 1))
            self.assertIn("Vorjahresumsatz", warning)

            settings["small_business_enabled"] = "0"
            self.assertEqual(
                kleinunternehmer_threshold_warning(connection, settings, date(2026, 7, 1)), ""
            )

    def test_current_year_vat_stats_computes_correctly(self):
        os.environ.setdefault("HD_DATA_DIR", tempfile.mkdtemp())
        from web import current_year_vat_stats

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
                INSERT INTO documents(document_type, document_number, status, customer_id,
                issue_date, total_cents, tax_cents, paid_at, created_at, updated_at)
                VALUES ('invoice','2026-1','paid',?,'2026-01-05',11900,1900,'2026-01-10T10:00:00',?,?)
                """,
                (customer_id, now, now),
            )
            supplier_id = connection.execute(
                "INSERT INTO suppliers(company,created_at,updated_at) VALUES ('Lieferant',?,?)",
                (now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO incoming_invoices(
                    supplier_id, invoice_number, invoice_date, payment_date, status,
                    eur_category, gross_cents, business_share_percent, deductible_cents,
                    tax_rate_bp, vorsteuer_cents, created_at, updated_at
                ) VALUES (?, 'R-1', '2026-01-01', '2026-01-05', 'paid',
                          'Software und IT', 5950, 100, 5000, 1900, 950, ?, ?)
                """,
                (supplier_id, now, now),
            )
            stats = current_year_vat_stats(connection, 2026)
        self.assertEqual(stats["tax_collected_cents"], 1900)
        self.assertEqual(stats["vorsteuer_paid_cents"], 950)

    def test_analyze_invoice_pdf_detects_tax_rate_for_incoming_only(self):
        from reportlab.pdfgen import canvas as pdf_canvas

        buffer = io.BytesIO()
        canvas_obj = pdf_canvas.Canvas(buffer)
        canvas_obj.drawString(50, 750, "Lieferant GmbH, Beispielweg 1, 12345 Musterstadt")
        canvas_obj.drawString(50, 700, "Rechnungsnummer: RE-2026-001")
        canvas_obj.drawString(50, 600, "zzgl. 19% USt: 19,00 EUR")
        canvas_obj.drawString(50, 560, "Rechnungsbetrag: 119,00 EUR")
        canvas_obj.save()
        raw = buffer.getvalue()

        incoming_result = analyze_invoice_pdf(raw, "beleg.pdf", {}, "incoming")
        outgoing_result = analyze_invoice_pdf(raw, "beleg.pdf", {}, "outgoing")

        self.assertEqual(incoming_result["tax_rate_bp"], 1900)
        self.assertIsNone(outgoing_result["tax_rate_bp"])

    def test_customer_reverse_charge_applies_requires_regelbesteuerung_foreign_and_vat_id(self):
        os.environ.setdefault("HD_DATA_DIR", tempfile.mkdtemp())
        from web import customer_reverse_charge_applies

        foreign_with_vat_id = {"country": "Niederlande", "vat_id": "NL123456789B01"}
        regelbesteuerung = {"small_business_enabled": "0"}
        kleinunternehmer = {"small_business_enabled": "1"}

        self.assertTrue(
            customer_reverse_charge_applies(foreign_with_vat_id, regelbesteuerung)
        )
        self.assertFalse(
            customer_reverse_charge_applies(foreign_with_vat_id, kleinunternehmer)
        )
        self.assertFalse(
            customer_reverse_charge_applies(
                {"country": "Deutschland", "vat_id": "DE123456789"}, regelbesteuerung
            )
        )
        self.assertFalse(
            customer_reverse_charge_applies(
                {"country": "Niederlande", "vat_id": ""}, regelbesteuerung
            )
        )

    def test_document_totals_forces_zero_tax_for_reverse_charge(self):
        os.environ.setdefault("HD_DATA_DIR", tempfile.mkdtemp())
        from web import document_totals

        items = [{"total_cents": 10000, "tax_rate_bp": 1900}]
        settings = {"small_business_enabled": "0"}
        net, tax, gross = document_totals(items, settings, reverse_charge=True)
        self.assertEqual((net, tax, gross), (10000, 0, 10000))
        net, tax, gross = document_totals(items, settings, reverse_charge=False)
        self.assertEqual((net, tax, gross), (10000, 1900, 11900))

    def test_reverse_charge_pdf_shows_notice_without_vat_breakdown(self):
        settings = {
            "company_name": "Musterbetrieb", "owner_name": "Max Mustermann",
            "street": "Beispielstraße 1", "postal_code": "10117", "city": "Berlin",
            "email": "rechnung@example.invalid", "small_business_enabled": "0",
        }
        output = Path(self.temp.name) / "reverse-charge.pdf"
        create_document_pdf(
            output,
            {
                "document_type": "invoice", "document_number": "2026-07-0200",
                "issue_date": "2026-07-23", "due_date": "2026-08-06",
                "title": "Consulting", "introduction": "", "notes": "",
                "total_cents": 10000, "tax_cents": 0, "reverse_charge": True,
            },
            {
                "customer_number": "2001", "company": "Foreign BV", "contact_name": "",
                "street": "Keizersgracht 1", "postal_code": "1015CJ", "city": "Amsterdam",
            },
            [{
                "position": 1, "category": "Beratung", "description": "Consulting",
                "quantity_milli": 1000, "unit": "pauschal", "unit_price_cents": 10000,
                "total_cents": 10000, "tax_rate_bp": 1900, "service_period": "",
            }],
            settings, None,
        )
        with pdfplumber.open(output) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        self.assertIn("Steuerschuldnerschaft des Leistungsempfängers", text)
        self.assertIn("§ 13b UStG", text)
        self.assertNotIn("Nettobetrag", text)
        self.assertNotIn("zzgl.", text)
        self.assertIn("Rechnungsbetrag", text)
        self.assertIn("100,00 €", text)

    def test_zugferd_reverse_charge_uses_ae_category_and_buyer_vat_id(self):
        try:
            from facturx import get_xml_from_pdf, xml_check_xsd
        except ImportError:
            self.skipTest("factur-x ist in der lokalen Testumgebung nicht installiert")
        settings = self.db.settings()
        settings.update({
            "company_name": "Musterbetrieb", "owner_name": "Max Mustermann",
            "street": "Beispielstraße 1", "postal_code": "10117", "city": "Berlin",
            "country": "Deutschland", "email": "rechnung@example.invalid",
            "tax_number": "12/345/67890", "vat_id": "DE123456789",
            "iban": "DE02120300000000202051", "small_business_enabled": "0",
        })
        document = {
            "document_type": "invoice", "document_number": "2026-07-0201",
            "issue_date": "2026-07-23", "due_date": "2026-08-06",
            "service_start": None, "service_end": None, "payment_terms_days": 14,
            "title": "Consulting", "introduction": "", "notes": "",
            "credit_reason": "", "total_cents": 10000, "tax_cents": 0,
            "reverse_charge": True,
        }
        customer = {
            "customer_number": "2001", "company": "Foreign BV", "contact_name": "",
            "street": "Keizersgracht 1", "postal_code": "1015CJ", "city": "Amsterdam",
            "country": "Niederlande", "email": "kunde@example.invalid",
            "buyer_reference": "", "vat_id": "NL123456789B01",
        }
        items = [{
            "position": 1, "category": "Beratung", "description": "Consulting",
            "quantity_milli": 1000, "unit": "pauschal", "unit_price_cents": 10000,
            "total_cents": 10000, "tax_rate_bp": 1900, "service_period": "",
        }]
        regular = Path(self.temp.name) / "reverse-charge.pdf"
        output = Path(self.temp.name) / "reverse-charge-zugferd.pdf"
        xml_output = Path(self.temp.name) / "factur-x-reverse-charge.xml"
        create_document_pdf(regular, document, customer, items, settings, None)
        result = create_zugferd(
            regular, output, xml_output, document, customer, items, settings
        )
        embedded_name, embedded_xml = get_xml_from_pdf(
            output.read_bytes(), check_xsd=True, check_schematron=False
        )
        self.assertEqual(embedded_name, "factur-x.xml")
        self.assertTrue(xml_check_xsd(embedded_xml, flavor="factur-x", level="en16931"))
        self.assertTrue(result["xsd_valid"])
        self.assertIn(b"NL123456789B01", xml_output.read_bytes())
        schematron = validate_schematron(xml_output.read_bytes())
        if schematron["available"]:
            self.assertTrue(result["schematron_checked"])
            self.assertTrue(
                result["schematron_valid"],
                f"Mustang-Schematron-Prüfung meldet EN-16931-Regelverstöße:\n{schematron['report']}",
            )
        else:
            self.assertFalse(result["schematron_checked"])
            self.assertIsNone(result["schematron_valid"])

    def test_vat_liability_by_period_groups_month_and_quarter(self):
        entries = [
            {"kind": "Einnahme", "date": "2026-01-10", "tax_cents": 1900},
            {"kind": "Einnahme", "date": "2026-02-10", "tax_cents": 3800},
            {"kind": "Ausgabe", "date": "2026-01-15", "vorsteuer_cents": 190},
        ]
        monthly = vat_liability_by_period(entries, "month")
        self.assertEqual(len(monthly), 2)
        self.assertEqual(monthly[0]["period_label"], "Januar 2026")
        self.assertEqual(monthly[0]["tax_collected_cents"], 1900)
        self.assertEqual(monthly[0]["vorsteuer_paid_cents"], 190)
        self.assertEqual(monthly[0]["balance_cents"], 1710)
        self.assertEqual(monthly[0]["due_date"], "2026-02-10")
        self.assertEqual(monthly[1]["period_label"], "Februar 2026")
        self.assertEqual(monthly[1]["due_date"], "2026-03-10")

        quarterly = vat_liability_by_period(entries, "quarter")
        self.assertEqual(len(quarterly), 1)
        self.assertEqual(quarterly[0]["period_label"], "1. Quartal 2026")
        self.assertEqual(quarterly[0]["tax_collected_cents"], 5700)
        self.assertEqual(quarterly[0]["balance_cents"], 5510)
        self.assertEqual(quarterly[0]["due_date"], "2026-04-10")

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
        schematron = validate_schematron(xml_output.read_bytes())
        if schematron["available"]:
            self.assertTrue(result["schematron_checked"])
            self.assertTrue(
                result["schematron_valid"],
                f"Mustang-Schematron-Prüfung meldet EN-16931-Regelverstöße:\n{schematron['report']}",
            )
        else:
            self.assertFalse(result["schematron_checked"])
            self.assertIsNone(result["schematron_valid"])

    def test_zugferd_generation_with_mixed_tax_rates(self):
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
            "vat_id": "DE123456789",
            "iban": "DE02120300000000202051",
            "small_business_enabled": "0",
        })
        document = {
            "document_type": "invoice", "document_number": "2026-07-0134",
            "issue_date": "2026-07-23", "due_date": "2026-08-06",
            "service_start": None, "service_end": None, "payment_terms_days": 14,
            "title": "Hosting und Fachliteratur", "introduction": "", "notes": "",
            "credit_reason": "", "total_cents": 12490, "tax_cents": 1490,
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
            "total_cents": 6000, "tax_rate_bp": 1900, "service_period": "Juli 2026",
        }, {
            "position": 2, "category": "Literatur", "description": "Fachbuch",
            "quantity_milli": 1000, "unit": "Stück", "unit_price_cents": 5000,
            "total_cents": 5000, "tax_rate_bp": 700, "service_period": "",
        }]
        regular = Path(self.temp.name) / "invoice-mixed.pdf"
        output = Path(self.temp.name) / "invoice-mixed-zugferd.pdf"
        xml_output = Path(self.temp.name) / "factur-x-mixed.xml"
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
        schematron = validate_schematron(xml_output.read_bytes())
        if schematron["available"]:
            self.assertTrue(result["schematron_checked"])
            self.assertTrue(
                result["schematron_valid"],
                f"Mustang-Schematron-Prüfung meldet EN-16931-Regelverstöße:\n{schematron['report']}",
            )
        else:
            self.assertFalse(result["schematron_checked"])
            self.assertIsNone(result["schematron_valid"])


if __name__ == "__main__":
    unittest.main()
