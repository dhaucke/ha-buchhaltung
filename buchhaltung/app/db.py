from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_number TEXT NOT NULL UNIQUE,
    company TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    street TEXT NOT NULL DEFAULT '',
    postal_code TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT 'Deutschland',
    email TEXT NOT NULL DEFAULT '',
    buyer_reference TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_type TEXT NOT NULL CHECK(document_type IN ('offer','order','invoice','credit')),
    document_number TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'draft',
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    issue_date TEXT NOT NULL,
    service_start TEXT,
    service_end TEXT,
    due_date TEXT,
    payment_terms_days INTEGER NOT NULL DEFAULT 7,
    title TEXT NOT NULL DEFAULT '',
    introduction TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    source_document_id INTEGER REFERENCES documents(id),
    credit_reason TEXT NOT NULL DEFAULT '',
    settlement_type TEXT NOT NULL DEFAULT '',
    settled_at TEXT,
    total_cents INTEGER NOT NULL DEFAULT 0,
    finalized_at TEXT,
    sent_at TEXT,
    paid_at TEXT,
    cancelled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL DEFAULT 1000,
    unit TEXT NOT NULL DEFAULT 'pauschal',
    unit_price_cents INTEGER NOT NULL DEFAULT 0,
    total_cents INTEGER NOT NULL DEFAULT 0,
    service_period TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS archive_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id),
    customer_id INTEGER REFERENCES customers(id),
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    extracted_text TEXT NOT NULL DEFAULT '',
    detected_invoice_number TEXT NOT NULL DEFAULT '',
    detected_issue_date TEXT NOT NULL DEFAULT '',
    detected_amount_cents INTEGER,
    detected_customer_name TEXT NOT NULL DEFAULT '',
    detected_customer_number TEXT NOT NULL DEFAULT '',
    detected_street TEXT NOT NULL DEFAULT '',
    detected_postal_code TEXT NOT NULL DEFAULT '',
    detected_city TEXT NOT NULL DEFAULT '',
    analyzed_at TEXT,
    reviewed_at TEXT,
    analysis_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    street TEXT NOT NULL DEFAULT '',
    postal_code TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT 'Deutschland',
    email TEXT NOT NULL DEFAULT '',
    tax_number TEXT NOT NULL DEFAULT '',
    iban TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incoming_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_file_id INTEGER UNIQUE REFERENCES archive_files(id),
    supplier_id INTEGER REFERENCES suppliers(id),
    invoice_number TEXT NOT NULL DEFAULT '',
    invoice_date TEXT NOT NULL,
    due_date TEXT,
    payment_date TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','booked','paid','cancelled')),
    description TEXT NOT NULL DEFAULT '',
    eur_category TEXT NOT NULL DEFAULT 'Sonstige Betriebsausgaben',
    gross_cents INTEGER NOT NULL DEFAULT 0,
    business_share_percent INTEGER NOT NULL DEFAULT 100
        CHECK(business_share_percent BETWEEN 0 AND 100),
    deductible_cents INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    booked_at TEXT,
    cancelled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    active INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    service_period_template TEXT NOT NULL DEFAULT '{monat} {jahr}',
    quantity_milli INTEGER NOT NULL DEFAULT 1000,
    unit TEXT NOT NULL DEFAULT 'pauschal',
    unit_price_cents INTEGER NOT NULL,
    billing_day INTEGER NOT NULL DEFAULT 1 CHECK(billing_day BETWEEN 1 AND 28),
    auto_finalize INTEGER NOT NULL DEFAULT 1,
    auto_send INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recurring_invoice_id INTEGER NOT NULL REFERENCES recurring_invoices(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    document_id INTEGER REFERENCES documents(id),
    status TEXT NOT NULL DEFAULT 'processing',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(recurring_invoice_id, period)
);

CREATE TABLE IF NOT EXISTS mail_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    response_code TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    reminder_level INTEGER NOT NULL,
    reminder_date TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_html TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent',
    response_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(document_id, reminder_level)
);

CREATE TABLE IF NOT EXISTS e_invoice_files (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    pdf_filename TEXT NOT NULL,
    xml_filename TEXT NOT NULL,
    xsd_valid INTEGER NOT NULL DEFAULT 0,
    validation_message TEXT NOT NULL DEFAULT '',
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    action TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_customer ON documents(customer_id);
CREATE INDEX IF NOT EXISTS idx_documents_type_status ON documents(document_type, status);
CREATE INDEX IF NOT EXISTS idx_documents_issue_date ON documents(issue_date);
CREATE INDEX IF NOT EXISTS idx_archive_customer ON archive_files(customer_id);
CREATE INDEX IF NOT EXISTS idx_incoming_status ON incoming_invoices(status);
CREATE INDEX IF NOT EXISTS idx_incoming_payment_date ON incoming_invoices(payment_date);
CREATE INDEX IF NOT EXISTS idx_incoming_supplier ON incoming_invoices(supplier_id);
CREATE INDEX IF NOT EXISTS idx_recurring_customer ON recurring_invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_recurring_runs_status ON recurring_runs(status);
CREATE INDEX IF NOT EXISTS idx_payment_reminders_document ON payment_reminders(document_id);
"""


DEFAULT_SETTINGS = {
    "setup_complete": "0",
    "company_name": "",
    "owner_name": "",
    "street": "",
    "postal_code": "",
    "city": "",
    "country": "Deutschland",
    "phone": "",
    "email": "",
    "website": "",
    "tax_number": "",
    "iban": "",
    "bic": "",
    "bank_name": "",
    "small_business_enabled": "1",
    "small_business_notice": "Steuerbefreiung für Kleinunternehmer gemäß § 19 UStG.",
    "payment_terms_days": "14",
    "reminder_grace_days": "3",
    "reminder_interval_days": "7",
    "invoice_counter": "0",
    "customer_counter": "0",
    "offer_counter": "0",
    "order_counter": "0",
    "graph_tenant_id": "",
    "graph_client_id": "",
    "graph_service_principal_object_id": "",
    "graph_sender": "",
    "graph_certificate_path": "/data/graph-certificate.pem",
    "graph_private_key_path": "/data/graph-private-key.pem",
}


def suggested_payment_date(issue_date: str, payment_terms_days: int | str) -> str:
    """Return an editable due-date-style payment suggestion on a weekday."""
    try:
        candidate = date.fromisoformat(str(issue_date).strip()) + timedelta(
            days=max(0, int(payment_terms_days))
        )
    except (TypeError, ValueError):
        return ""
    if candidate.weekday() == 5:
        candidate += timedelta(days=2)
    elif candidate.weekday() == 6:
        candidate += timedelta(days=1)
    return candidate.isoformat()


class Database:
    def __init__(self, data_dir: str | os.PathLike[str]):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "documents").mkdir(exist_ok=True)
        (self.data_dir / "archive").mkdir(exist_ok=True)
        (self.data_dir / "reports").mkdir(exist_ok=True)
        self.path = self.data_dir / "buchhaltung.sqlite3"

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            existing = {
                row["name"] for row in connection.execute("PRAGMA table_info(archive_files)")
            }
            migrations = {
                "customer_id": "INTEGER",
                "extracted_text": "TEXT NOT NULL DEFAULT ''",
                "detected_invoice_number": "TEXT NOT NULL DEFAULT ''",
                "detected_issue_date": "TEXT NOT NULL DEFAULT ''",
                "detected_amount_cents": "INTEGER",
                "detected_customer_name": "TEXT NOT NULL DEFAULT ''",
                "detected_customer_number": "TEXT NOT NULL DEFAULT ''",
                "detected_street": "TEXT NOT NULL DEFAULT ''",
                "detected_postal_code": "TEXT NOT NULL DEFAULT ''",
                "detected_city": "TEXT NOT NULL DEFAULT ''",
                "analyzed_at": "TEXT",
                "reviewed_at": "TEXT",
                "analysis_error": "TEXT NOT NULL DEFAULT ''",
                "document_direction": "TEXT NOT NULL DEFAULT 'outgoing'",
                "payment_date": "TEXT",
                "eur_category": "TEXT NOT NULL DEFAULT 'Betriebseinnahmen'",
                "accounting_status": "TEXT NOT NULL DEFAULT 'unbooked'",
                "cancelled_at": "TEXT",
            }
            for column, definition in migrations.items():
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE archive_files ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_archive_direction "
                "ON archive_files(document_direction)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_archive_review_queue "
                "ON archive_files(document_direction, reviewed_at, uploaded_at)"
            )
            connection.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                DEFAULT_SETTINGS.items(),
            )
            document_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(documents)")
            }
            document_migrations = {
                "credit_reason": "TEXT NOT NULL DEFAULT ''",
                "settlement_type": "TEXT NOT NULL DEFAULT ''",
                "settled_at": "TEXT",
            }
            for column, definition in document_migrations.items():
                if column not in document_columns:
                    connection.execute(
                        f"ALTER TABLE documents ADD COLUMN {column} {definition}"
                    )
            self.link_archives_by_customer_number(connection)

    def settings(self) -> dict[str, str]:
        with self.connect() as connection:
            return {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM settings")
            }

    def update_settings(self, values: dict[str, str]):
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                values.items(),
            )
            self.audit(connection, "settings", None, "updated", ", ".join(sorted(values)))

    @staticmethod
    def now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @classmethod
    def audit(
        cls,
        connection: sqlite3.Connection,
        entity_type: str,
        entity_id: int | None,
        action: str,
        details: str = "",
    ):
        connection.execute(
            """
            INSERT INTO audit_log(entity_type, entity_id, action, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entity_type, entity_id, action, details, cls.now()),
        )

    @classmethod
    def link_archives_by_customer_number(
        cls,
        connection: sqlite3.Connection,
        archive_id: int | None = None,
    ) -> int:
        """Link outgoing archive PDFs to one unambiguous customer number.

        Customer addresses deliberately do not participate in this lookup:
        an imported invoice keeps its historical address while the customer
        record may contain the current address.
        """
        parameters: tuple[object, ...] = ()
        archive_filter = ""
        if archive_id is not None:
            archive_filter = "AND a.id=?"
            parameters = (archive_id,)
        candidates = connection.execute(
            f"""
            SELECT a.id AS archive_id, MIN(c.id) AS customer_id
            FROM archive_files a
            JOIN customers c
              ON lower(trim(c.customer_number))
               = lower(trim(a.detected_customer_number))
            WHERE a.customer_id IS NULL
              AND a.document_direction='outgoing'
              AND trim(a.detected_customer_number)!=''
              {archive_filter}
            GROUP BY a.id
            HAVING count(c.id)=1
            """,
            parameters,
        ).fetchall()
        linked = 0
        for candidate in candidates:
            result = connection.execute(
                """
                UPDATE archive_files SET customer_id=?
                WHERE id=? AND customer_id IS NULL
                """,
                (candidate["customer_id"], candidate["archive_id"]),
            )
            if result.rowcount:
                linked += 1
                cls.audit(
                    connection,
                    "archive",
                    candidate["archive_id"],
                    "customer_auto_linked_by_number",
                    str(candidate["customer_id"]),
                )
        return linked

    @staticmethod
    def find_semantic_archive_duplicate(
        connection: sqlite3.Connection,
        direction: str,
        invoice_number: str,
        party_name: str = "",
        issue_date: str = "",
        amount_cents: int | None = None,
    ):
        """Find the same business document even when the PDF bytes differ."""
        invoice_number = invoice_number.strip()
        if not invoice_number:
            return None
        if direction == "outgoing":
            duplicate = connection.execute(
                """
                SELECT id, 'archive' AS source
                FROM archive_files
                WHERE document_direction='outgoing'
                  AND lower(trim(detected_invoice_number))=lower(trim(?))
                ORDER BY id LIMIT 1
                """,
                (invoice_number,),
            ).fetchone()
            if duplicate:
                return duplicate
            return connection.execute(
                """
                SELECT id, 'document' AS source
                FROM documents
                WHERE lower(trim(document_number))=lower(trim(?))
                ORDER BY id LIMIT 1
                """,
                (invoice_number,),
            ).fetchone()

        party_name = party_name.strip()
        if party_name:
            return connection.execute(
                """
                SELECT id, 'archive' AS source
                FROM archive_files
                WHERE document_direction='incoming'
                  AND lower(trim(detected_invoice_number))=lower(trim(?))
                  AND lower(trim(detected_customer_name))=lower(trim(?))
                ORDER BY id LIMIT 1
                """,
                (invoice_number, party_name),
            ).fetchone()
        if issue_date and amount_cents is not None:
            return connection.execute(
                """
                SELECT id, 'archive' AS source
                FROM archive_files
                WHERE document_direction='incoming'
                  AND lower(trim(detected_invoice_number))=lower(trim(?))
                  AND detected_issue_date=?
                  AND detected_amount_cents=?
                ORDER BY id LIMIT 1
                """,
                (invoice_number, issue_date, amount_cents),
            ).fetchone()
        return None

    @staticmethod
    def next_unreviewed_archive_id(
        connection: sqlite3.Connection,
        current_id: int | None = None,
        direction: str | None = None,
        customer_search: str = "",
        customer_number_search: str = "",
    ) -> int | None:
        if current_id is not None and direction is None:
            current = connection.execute(
                "SELECT document_direction FROM archive_files WHERE id=?",
                (current_id,),
            ).fetchone()
            direction = current["document_direction"] if current else None
        conditions = ["a.reviewed_at IS NULL"]
        parameters: list[object] = []
        if current_id is not None:
            conditions.append("a.id!=?")
            parameters.append(current_id)
        if direction in ("outgoing", "incoming"):
            conditions.append("a.document_direction=?")
            parameters.append(direction)
        if customer_search.strip():
            pattern = f"%{customer_search.strip().lower()}%"
            conditions.append(
                "(lower(a.detected_customer_name) LIKE ? "
                "OR lower(COALESCE(c.company,'')) LIKE ?)"
            )
            parameters.extend((pattern, pattern))
        if customer_number_search.strip():
            pattern = f"%{customer_number_search.strip().lower()}%"
            conditions.append(
                "(lower(a.detected_customer_number) LIKE ? "
                "OR lower(COALESCE(c.customer_number,'')) LIKE ?)"
            )
            parameters.extend((pattern, pattern))
        row = connection.execute(
            f"""
            SELECT a.id FROM archive_files a
            LEFT JOIN customers c ON c.id=a.customer_id
            WHERE {' AND '.join(conditions)}
            ORDER BY
              CASE WHEN trim(a.detected_invoice_number)='' THEN 1 ELSE 0 END,
              a.detected_invoice_number COLLATE NOCASE DESC,
              a.detected_issue_date DESC,
              a.uploaded_at DESC,
              a.id DESC
            LIMIT 1
            """,
            tuple(parameters),
        ).fetchone()
        return int(row["id"]) if row else None
