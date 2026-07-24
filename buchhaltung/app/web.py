from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import sqlite3
import urllib.parse
import base64
import calendar
import unicodedata
import threading
import time
from io import BytesIO
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server
from socketserver import ThreadingMixIn
from PIL import Image as PillowImage, UnidentifiedImageError

from db import Database, suggested_payment_date
from einvoice import create_zugferd
from graph import GraphClient
from euer import EXPENSE_CATEGORIES, create_euer_csv, create_euer_pdf, euer_entries, euer_summary
from pdfgen import TYPE_LABELS, create_document_pdf, german_date, money
from pdfimport import analyze_invoice_pdf


APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HD_DATA_DIR", "/data"))
if not DATA_DIR.parent.exists() or not os.access(DATA_DIR.parent, os.W_OK):
    DATA_DIR = APP_ROOT.parent / "data"
DB = Database(DATA_DIR)
DB.initialize()
COMPANY_LOGO = DATA_DIR / "company-logo.png"
APP_CSS = (APP_ROOT / "static" / "app.css").read_text(encoding="utf-8")


DOCUMENT_STATUS = {
    "draft": "Entwurf",
    "final": "Fertiggestellt",
    "sent": "Versendet",
    "accepted": "Bestätigt",
    "paid": "Bezahlt",
    "settled": "Verrechnet",
    "refunded": "Ausgezahlt",
    "credited": "Vollständig gutgeschrieben",
    "cancelled": "Storniert",
}


def h(value) -> str:
    return html.escape(str(value or ""), quote=True)


def parse_form(environ) -> dict:
    content_type = environ.get("CONTENT_TYPE", "")
    size = int(environ.get("CONTENT_LENGTH") or 0)
    raw_bytes = environ["wsgi.input"].read(size)
    if content_type.startswith("multipart/form-data"):
        message = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\n"
            b"MIME-Version: 1.0\r\n\r\n" + raw_bytes
        )
        result = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                value = UploadedFile(filename, part.get_content_type(), payload)
            else:
                charset = part.get_content_charset() or "utf-8"
                value = payload.decode(charset, errors="replace")
            if name in result:
                if not isinstance(result[name], list):
                    result[name] = [result[name]]
                result[name].append(value)
            else:
                result[name] = value
        return result
    raw = raw_bytes.decode("utf-8")
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
    return {
        key: values if len(values) > 1 else values[-1]
        for key, values in parsed.items()
    }


class UploadedFile:
    def __init__(self, filename: str, content_type: str, data: bytes):
        self.filename = filename
        self.type = content_type
        self.data = data


def uploaded_files(value) -> list[UploadedFile]:
    values = value if isinstance(value, list) else [value]
    return [item for item in values if isinstance(item, UploadedFile) and item.filename]


def form_values(form: dict, key: str) -> list[str]:
    value = form.get(key, [])
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def document_items_from_form(form: dict) -> list[dict]:
    descriptions = form_values(form, "item_description")
    categories = form_values(form, "item_category")
    periods = form_values(form, "item_service_period")
    quantities = form_values(form, "item_quantity")
    units = form_values(form, "item_unit")
    prices = form_values(form, "item_unit_price")
    result = []
    for index, description in enumerate(descriptions):
        description = description.strip()
        if not description:
            continue
        qty = quantity_milli(quantities[index] if index < len(quantities) else "1")
        if qty <= 0:
            raise ValueError(f"Die Menge in Position {index + 1} muss größer als 0 sein.")
        unit_price = cents(prices[index] if index < len(prices) else "0")
        if unit_price < 0:
            raise ValueError(f"Der Einzelpreis in Position {index + 1} darf nicht negativ sein.")
        total = int(
            (Decimal(qty) / 1000 * unit_price).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        result.append({
            "position": len(result) + 1,
            "category": categories[index].strip() if index < len(categories) else "",
            "description": description,
            "service_period": periods[index].strip() if index < len(periods) else "",
            "quantity_milli": qty,
            "unit": (units[index].strip() if index < len(units) else "") or "pauschal",
            "unit_price_cents": unit_price,
            "total_cents": total,
        })
    if not result:
        raise ValueError("Bitte mindestens eine Rechnungsposition vollständig ausfüllen.")
    return result


def replace_document_items(connection, document_id: int, items: list[dict]) -> None:
    connection.execute("DELETE FROM document_items WHERE document_id=?", (document_id,))
    connection.executemany(
        """
        INSERT INTO document_items(
            document_id, position, category, description, quantity_milli, unit,
            unit_price_cents, total_cents, service_period
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                document_id, item["position"], item["category"], item["description"],
                item["quantity_milli"], item["unit"], item["unit_price_cents"],
                item["total_cents"], item["service_period"],
            )
            for item in items
        ],
    )


def archive_can_be_deleted(item) -> bool:
    """Ungebuchte Fehlimporte dürfen auch nach einer Kundenverknüpfung weg."""
    return (
        not item["document_id"]
        and not item["incoming_invoice_id"]
        and item["accounting_status"] == "unbooked"
    )


def cents(value: str) -> int:
    normalized = value.strip().replace(".", "").replace(",", ".")
    try:
        return int((Decimal(normalized) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0


def quantity_milli(value: str) -> int:
    normalized = value.strip().replace(",", ".")
    try:
        return int((Decimal(normalized) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 1000


def normalized_counter(value: str) -> str:
    try:
        return str(max(0, int(value.strip() or "0")))
    except (AttributeError, ValueError):
        raise ValueError("Nummernkreis-Zähler müssen ganze Zahlen ab 0 sein.")


def save_company_logo(upload) -> None:
    if not upload:
        return
    if not isinstance(upload, UploadedFile):
        raise ValueError("Das Logo konnte nicht verarbeitet werden.")
    if len(upload.data) > 5 * 1024 * 1024:
        raise ValueError("Das Logo darf höchstens 5 MB groß sein.")
    try:
        with PillowImage.open(BytesIO(upload.data)) as source:
            source.load()
            if source.width < 100 or source.height < 40:
                raise ValueError("Das Logo muss mindestens 100 × 40 Pixel groß sein.")
            image = source.convert("RGBA")
            image.thumbnail((1600, 600))
            image.save(COMPANY_LOGO, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Bitte ein gültiges PNG-, JPG- oder WebP-Logo hochladen.") from exc


def save_graph_credentials(certificate, private_key) -> None:
    uploads = [
        (certificate, DATA_DIR / "graph-certificate.pem", "Zertifikat"),
        (private_key, DATA_DIR / "graph-private-key.pem", "privater Schlüssel"),
    ]
    for upload, target, label in uploads:
        if not upload:
            continue
        if not isinstance(upload, UploadedFile):
            raise ValueError(f"{label} konnte nicht verarbeitet werden.")
        if len(upload.data) > 128 * 1024:
            raise ValueError(f"{label} ist ungewöhnlich groß.")
        if b"-----BEGIN" not in upload.data:
            raise ValueError(f"{label} muss im PEM-Format vorliegen.")
        target.write_bytes(upload.data)
        target.chmod(0o600)
    if certificate or private_key:
        cert_path = DATA_DIR / "graph-certificate.pem"
        key_path = DATA_DIR / "graph-private-key.pem"
        if cert_path.is_file() and key_path.is_file():
            try:
                from cryptography import x509
                from cryptography.hazmat.primitives import serialization
                cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
                key = serialization.load_pem_private_key(
                    key_path.read_bytes(), password=None
                )
                if cert.public_key().public_numbers() != key.public_key().public_numbers():
                    raise ValueError("Zertifikat und privater Schlüssel gehören nicht zusammen.")
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Graph-Zertifikatsdateien sind ungültig: {exc}"
                ) from exc


def generate_graph_certificate(settings: dict[str, str]) -> None:
    """Create a self-signed cert/key pair for Graph client-cert auth locally.

    Avoids requiring the user to run openssl themselves: only the public
    certificate needs to leave this machine (uploaded to Entra); the private
    key is written straight to the persistent data directory.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, settings.get("company_name") or "Buchhaltung"),
    ])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=730))
        .sign(private_key, hashes.SHA256())
    )
    key_path = DATA_DIR / "graph-private-key.pem"
    cert_path = DATA_DIR / "graph-certificate.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    cert_path.chmod(0o600)


def company_logo_data_uri() -> str:
    if not COMPANY_LOGO.is_file():
        return ""
    return "data:image/png;base64," + base64.b64encode(COMPANY_LOGO.read_bytes()).decode()


def app_name(settings: dict[str, str]) -> str:
    company = settings.get("company_name", "").strip()
    return f"{company} Buchhaltung" if company else "Buchhaltung"


def flash_cookie(message: str, level: str = "success") -> str:
    payload = urllib.parse.quote(json.dumps({"message": message, "level": level}))
    return f"hd_flash={payload}; Path=/; SameSite=Strict"


def get_flash(environ):
    cookies = SimpleCookie(environ.get("HTTP_COOKIE", ""))
    if "hd_flash" not in cookies:
        return "", []
    try:
        payload = json.loads(urllib.parse.unquote(cookies["hd_flash"].value))
        value = f'<div class="alert {h(payload["level"])}">{h(payload["message"])}</div>'
    except Exception:
        value = ""
    return value, [("Set-Cookie", "hd_flash=; Path=/; Max-Age=0; SameSite=Strict")]


def layout(title: str, body: str, active: str = "") -> str:
    settings = DB.settings()
    application_name = app_name(settings)
    company_name = settings.get("company_name", "") or "Buchhaltung"
    logo_data_uri = company_logo_data_uri()
    brand_visual = (
        f'<img src="{logo_data_uri}" alt="{h(company_name)}">'
        if logo_data_uri
        else '<div class="brand-placeholder">B</div>'
    )
    sidebar_note = (
        "Kleinunternehmer · § 19 UStG"
        if settings.get("small_business_enabled", "1") == "1"
        else "Rechnungsverwaltung"
    )
    nav = [
        ("/", "dashboard", "Übersicht"),
        ("/customers", "customers", "Kunden"),
        ("/documents?type=offer", "offer", "Angebote"),
        ("/documents?type=order", "order", "Aufträge"),
        ("/documents?type=invoice", "invoice", "Rechnungen"),
        ("/documents?type=credit", "credit", "Gutschriften"),
        ("/reminders", "reminders", "Zahlungserinnerungen"),
        ("/incoming", "incoming", "Eingangsrechnungen"),
        ("/archive", "archive", "Archiv"),
        ("/reports/euer", "reports", "EÜR"),
        ("/settings", "settings", "Einstellungen"),
    ]
    links = "".join(
        f'<a class="{"active" if key == active else ""}" href="{url}">{label}</a>'
        for url, key, label in nav
    )
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{h(title)} · {h(application_name)}</title>
  <style>{APP_CSS}</style>
</head>
<body>
  <aside class="sidebar">
    <div class="brand">{brand_visual}<strong>{h(application_name)}</strong></div>
    <nav>{links}</nav>
    <div class="sidebar-foot">{h(sidebar_note)}</div>
  </aside>
  <main>
    <header class="topbar"><div><span>{h(company_name)}</span><h1>{h(title)}</h1></div></header>
    <section class="content">{body}</section>
  </main>
  <script>
  (() => {{
    const marker = "/api/hassio_ingress/";
    const path = window.location.pathname;
    const markerPos = path.indexOf(marker);
    let root = "/";
    if (markerPos >= 0) {{
      const afterMarker = markerPos + marker.length;
      const tokenEnd = path.indexOf("/", afterMarker);
      root = tokenEnd >= 0 ? path.slice(0, tokenEnd + 1) : path + "/";
    }}
    const ingressUrl = value => {{
      if (!value || !value.startsWith("/")) return value;
      return root + value.slice(1);
    }};
    document.querySelectorAll("a[href]").forEach(link => {{
      link.setAttribute("href", ingressUrl(link.getAttribute("href")));
    }});
    document.querySelectorAll("form[action]").forEach(form => {{
      form.setAttribute("action", ingressUrl(form.getAttribute("action")));
    }});
    document.querySelectorAll("[formaction]").forEach(button => {{
      button.setAttribute("formaction", ingressUrl(button.getAttribute("formaction")));
    }});
  }})();
  </script>
</body>
</html>"""


def setup_page(settings: dict[str, str]) -> str:
    logo_data_uri = company_logo_data_uri()
    logo_preview = (
        f'<img class="setup-logo-preview" src="{logo_data_uri}" alt="Aktuelles Logo">'
        if logo_data_uri else
        '<div class="setup-logo-empty">Noch kein Logo hinterlegt</div>'
    )
    checked = "checked" if settings.get("small_business_enabled", "1") == "1" else ""
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Ersteinrichtung · Buchhaltung</title>
  <style>{APP_CSS}</style>
</head>
<body class="setup-body">
  <main class="setup-main">
    <section class="setup-intro">
      <div class="setup-mark">B</div>
      <span>Lokale Rechnungsverwaltung</span>
      <h1>Buchhaltung einrichten</h1>
      <p>Die Angaben werden ausschließlich im persistenten Datenverzeichnis dieser
      Installation gespeichert und für Oberfläche, PDFs und E-Mails verwendet.</p>
    </section>
    <form class="setup-card" method="post" enctype="multipart/form-data">
      <div class="setup-step"><span>1</span><div><strong>Unternehmen und Logo</strong>
      <small>Pflichtangaben für den Dokumentkopf</small></div></div>
      <div class="form-grid">
        <label class="wide"><span>Logo (PNG, JPG oder WebP; maximal 5 MB)</span>
          {logo_preview}
          <input type="file" name="logo" accept="image/png,image/jpeg,image/webp">
        </label>
        <label><span>Unternehmensname *</span><input required name="company_name" value="{h(settings.get('company_name'))}"></label>
        <label><span>Inhaber / Geschäftsführung *</span><input required name="owner_name" value="{h(settings.get('owner_name'))}"></label>
        <label class="wide"><span>Straße und Hausnummer *</span><input required name="street" value="{h(settings.get('street'))}"></label>
        <label><span>PLZ *</span><input required name="postal_code" value="{h(settings.get('postal_code'))}"></label>
        <label><span>Ort *</span><input required name="city" value="{h(settings.get('city'))}"></label>
        <label><span>Land</span><input name="country" value="{h(settings.get('country', 'Deutschland'))}"></label>
        <label><span>Telefon</span><input name="phone" value="{h(settings.get('phone'))}"></label>
        <label><span>E-Mail *</span><input required type="email" name="email" value="{h(settings.get('email'))}"></label>
        <label><span>Website</span><input name="website" value="{h(settings.get('website'))}"></label>
      </div>

      <div class="setup-step"><span>2</span><div><strong>Steuern, Zahlung und Nummernkreise</strong>
      <small>Die Zähler bezeichnen jeweils die zuletzt vergebene Nummer</small></div></div>
      <div class="form-grid">
        <label><span>Steuernummer</span><input name="tax_number" value="{h(settings.get('tax_number'))}"></label>
        <label><span>Zahlungsziel in Tagen *</span><input required type="number" min="0" max="365" name="payment_terms_days" value="{h(settings.get('payment_terms_days', '14'))}"></label>
        <label><span>Bank</span><input name="bank_name" value="{h(settings.get('bank_name'))}"></label>
        <label><span>IBAN</span><input name="iban" value="{h(settings.get('iban'))}"></label>
        <label><span>BIC</span><input name="bic" value="{h(settings.get('bic'))}"></label>
        <label><span>Letzte Rechnungs-/Gutschriftnummer *</span><input required type="number" min="0" name="invoice_counter" value="{h(settings.get('invoice_counter', '0'))}"></label>
        <label><span>Letzte Kundennummer *</span><input required type="number" min="0" name="customer_counter" value="{h(settings.get('customer_counter', '0'))}"></label>
        <label><span>Letzte Angebotsnummer *</span><input required type="number" min="0" name="offer_counter" value="{h(settings.get('offer_counter', '0'))}"></label>
        <label><span>Letzte Auftragsnummer *</span><input required type="number" min="0" name="order_counter" value="{h(settings.get('order_counter', '0'))}"></label>
        <label class="check wide"><input type="checkbox" name="small_business_enabled" {checked}>
          <span>Kleinunternehmerregelung nach § 19 UStG verwenden</span></label>
        <label class="wide"><span>Hinweis auf Rechnungen</span>
          <input name="small_business_notice" value="{h(settings.get('small_business_notice'))}"></label>
      </div>

      <div class="setup-step"><span>3</span><div><strong>Microsoft Graph (optional)</strong>
      <small>Kann später jederzeit in den Einstellungen ergänzt werden</small></div></div>
      <div class="form-grid">
        <label><span>Microsoft Tenant-ID</span><input name="graph_tenant_id" value="{h(settings.get('graph_tenant_id'))}"></label>
        <label><span>Microsoft Client-ID</span><input name="graph_client_id" value="{h(settings.get('graph_client_id'))}"></label>
        <label class="wide"><span>Absenderadresse</span><input type="email" name="graph_sender" value="{h(settings.get('graph_sender') or settings.get('email'))}"></label>
      </div>
      <div class="setup-submit">
        <p>Nach dem Speichern öffnet sich die leere Buchhaltungsoberfläche.</p>
        <button class="button primary" type="submit">Einrichtung abschließen</button>
      </div>
    </form>
  </main>
</body>
</html>"""


def response(start_response, body: str | bytes, status=200, headers=None, content_type="text/html; charset=utf-8"):
    raw = body.encode("utf-8") if isinstance(body, str) else body
    all_headers = [("Content-Type", content_type), ("Content-Length", str(len(raw)))]
    all_headers.extend(headers or [])
    start_response(f"{status} {HTTPStatus(status).phrase}", all_headers)
    return [raw]


def redirect(start_response, target: str, message: str = "", level: str = "success"):
    headers = []
    if message:
        headers.append(("Set-Cookie", flash_cookie(message, level)))
    safe_target = json.dumps(target.lstrip("/"))
    body = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Weiterleitung</title></head><body>
    <p>Die Anwendung wird geöffnet …</p>
    <script>
    (() => {{
      const marker = "/api/hassio_ingress/";
      const path = window.location.pathname;
      const markerPos = path.indexOf(marker);
      let root = "/";
      if (markerPos >= 0) {{
        const afterMarker = markerPos + marker.length;
        const tokenEnd = path.indexOf("/", afterMarker);
        root = tokenEnd >= 0 ? path.slice(0, tokenEnd + 1) : path + "/";
      }}
      window.location.replace(root + {safe_target});
    }})();
    </script></body></html>"""
    return response(start_response, body, 200, headers)


def rows(connection, query: str, params=()):
    return [dict(row) for row in connection.execute(query, params)]


def fetch_document(connection, document_id: int):
    row = connection.execute(
        """
        SELECT d.*, c.company, c.customer_number, c.email AS customer_email,
               source.document_number AS source_document_number
        FROM documents d
        JOIN customers c ON c.id=d.customer_id
        LEFT JOIN documents source ON source.id=d.source_document_id
        WHERE d.id=?
        """,
        (document_id,),
    ).fetchone()
    return dict(row) if row else None


def outstanding_cents(connection, document_id: int) -> int:
    row = connection.execute(
        """
        SELECT max(0, d.total_cents - COALESCE((
          SELECT sum(c.total_cents) FROM documents c
          WHERE c.document_type='credit' AND c.source_document_id=d.id
            AND c.status='settled'
        ),0))
        FROM documents d WHERE d.id=?
        """,
        (document_id,),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def dashboard(connection) -> str:
    stats = {
        "customers": connection.execute("SELECT count(*) FROM customers").fetchone()[0],
        "open": connection.execute(
            """
            SELECT count(*) FROM documents d
            WHERE d.document_type='invoice' AND d.status IN ('final','sent')
              AND d.total_cents > COALESCE((
                SELECT sum(c.total_cents) FROM documents c
                WHERE c.document_type='credit' AND c.source_document_id=d.id
                  AND c.status='settled'
              ),0)
            """
        ).fetchone()[0],
        "paid": connection.execute(
            "SELECT COALESCE(sum(total_cents),0) FROM documents WHERE document_type='invoice' AND status='paid' AND substr(paid_at,1,4)=?",
            (str(date.today().year),),
        ).fetchone()[0],
        "outstanding": connection.execute(
            """
            SELECT COALESCE(sum(max(0, d.total_cents - COALESCE((
              SELECT sum(c.total_cents) FROM documents c
              WHERE c.document_type='credit' AND c.source_document_id=d.id
                AND c.status='settled'
            ),0))),0)
            FROM documents d
            WHERE d.document_type='invoice' AND d.status IN ('final','sent')
            """
        ).fetchone()[0],
    }
    recent = rows(
        connection,
        """
        SELECT d.*, c.company FROM documents d JOIN customers c ON c.id=d.customer_id
        ORDER BY d.created_at DESC LIMIT 8
        """,
    )
    recent_rows = "".join(
        f"""<tr><td><a href="/document/{d['id']}">{h(d['document_number'] or 'Entwurf')}</a></td>
        <td>{h(TYPE_LABELS[d['document_type']])}</td><td>{h(d['company'])}</td>
        <td>{german_date(d['issue_date'])}</td><td class="money">{money(d['total_cents'])}</td>
        <td><span class="status {h(d['status'])}">{h(DOCUMENT_STATUS.get(d['status'], d['status']))}</span></td></tr>"""
        for d in recent
    ) or '<tr><td colspan="6" class="empty">Noch keine Dokumente vorhanden.</td></tr>'
    return f"""
    <div class="actions"><a class="button primary" href="/document/new?type=invoice">Neue Rechnung</a>
    <a class="button" href="/document/new?type=offer">Neues Angebot</a></div>
    <div class="stats">
      <article><span>Kunden</span><strong>{stats['customers']}</strong></article>
      <article><span>Offene Rechnungen</span><strong>{stats['open']}</strong></article>
      <article><span>Offener Betrag</span><strong>{money(stats['outstanding'])}</strong></article>
      <article><span>Bezahlt {date.today().year}</span><strong>{money(stats['paid'])}</strong></article>
    </div>
    <div class="card"><div class="card-head"><h2>Letzte Dokumente</h2></div>
    <div class="table-wrap"><table><thead><tr><th>Nummer</th><th>Art</th><th>Kunde</th>
    <th>Datum</th><th>Betrag</th><th>Status</th></tr></thead><tbody>{recent_rows}</tbody></table></div></div>
    """


def customers_page(connection) -> str:
    customers = rows(
        connection,
        """
        SELECT c.*,
          (SELECT count(*) FROM documents d WHERE d.customer_id=c.id) current_count,
          (SELECT count(*) FROM archive_files a WHERE a.customer_id=c.id) archive_count
        FROM customers c ORDER BY c.company
        """,
    )
    customer_rows = "".join(
        f"""<tr><td><a href="/customer/{c['id']}"><strong>{h(c['customer_number'])}</strong></a></td>
        <td><a href="/customer/{c['id']}">{h(c['company'])}</a></td>
        <td>{h(c['contact_name'])}</td><td>{h(c['email'])}</td>
        <td>{c['current_count'] + c['archive_count']}</td>
        <td><a class="button compact" href="/customer/{c['id']}">Kundenakte</a></td></tr>"""
        for c in customers
    ) or '<tr><td colspan="6" class="empty">Noch keine Kunden angelegt.</td></tr>'
    return f"""
    <div class="actions"><a class="button primary" href="/customer/new">Kunde anlegen</a></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>Kundennummer</th><th>Unternehmen</th>
    <th>Ansprechpartner</th><th>Rechnungs-E-Mail</th><th>Dokumente</th><th></th></tr></thead>
    <tbody>{customer_rows}</tbody></table></div></div>"""


def customer_form(customer=None) -> str:
    customer = dict(customer or {})
    editing = bool(customer)
    action = f"/customer/{customer['id']}/edit" if editing else "/customer/new"
    return f"""
    <form class="card form" method="post" action="{action}">
      <div class="form-grid">
        <label><span>Unternehmen *</span><input required name="company" value="{h(customer.get('company'))}"></label>
        <label><span>Ansprechpartner</span><input name="contact_name" value="{h(customer.get('contact_name'))}"></label>
        <label class="wide"><span>Straße *</span><input required name="street" value="{h(customer.get('street'))}"></label>
        <label><span>PLZ *</span><input required name="postal_code" value="{h(customer.get('postal_code'))}"></label>
        <label><span>Ort *</span><input required name="city" value="{h(customer.get('city'))}"></label>
        <label><span>Land</span><input name="country" value="{h(customer.get('country', 'Deutschland'))}"></label>
        <label><span>Rechnungs-E-Mail</span><input type="email" name="email" value="{h(customer.get('email'))}"></label>
        <label><span>Buyer Reference</span><input name="buyer_reference" value="{h(customer.get('buyer_reference'))}"></label>
        <label class="wide"><span>Notizen</span><textarea name="notes">{h(customer.get('notes'))}</textarea></label>
      </div>
      <div class="form-actions"><a class="button" href="/customers">Abbrechen</a>
      <button class="button primary">{'Änderungen speichern' if editing else 'Kunde speichern'}</button></div>
    </form>"""


def customer_detail(connection, customer_id: int) -> str:
    customer = connection.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        raise ValueError("Kunde wurde nicht gefunden.")
    documents = rows(
        connection,
        "SELECT * FROM documents WHERE customer_id=? ORDER BY issue_date DESC, id DESC",
        (customer_id,),
    )
    archive = rows(
        connection,
        """
        SELECT * FROM archive_files WHERE customer_id=?
        ORDER BY
          CASE WHEN trim(detected_invoice_number)='' THEN 1 ELSE 0 END,
          detected_invoice_number COLLATE NOCASE DESC,
          detected_issue_date DESC,
          id DESC
        """,
        (customer_id,),
    )
    document_rows = "".join(
        f"""<tr><td><a href="/document/{d['id']}">{h(d['document_number'] or 'Entwurf')}</a></td>
        <td>{h(TYPE_LABELS[d['document_type']])}</td><td>{german_date(d['issue_date'])}</td>
        <td class="money">{money(d['total_cents'])}</td>
        <td><span class="status {h(d['status'])}">{h(DOCUMENT_STATUS.get(d['status'], d['status']))}</span></td></tr>"""
        for d in documents
    )
    document_rows += "".join(
        f"""<tr><td><a href="/archive/{a['id']}">{h(a['detected_invoice_number'] or a['original_filename'])}</a></td>
        <td>Archiv-Rechnung</td><td>{german_date(a['detected_issue_date']) if a['detected_issue_date'] else '–'}</td>
        <td class="money">{money(a['detected_amount_cents']) if a['detected_amount_cents'] is not None else '–'}</td>
        <td><a class="button compact" target="_blank" href="/archive/{a['id']}/pdf">PDF öffnen</a></td></tr>"""
        for a in archive
    )
    if not document_rows:
        document_rows = '<tr><td colspan="5" class="empty">Noch keine Dokumente für diesen Kunden.</td></tr>'

    templates = rows(
        connection,
        """
        SELECT r.*, rr.period AS last_period, rr.status AS last_status, rr.error AS last_error
        FROM recurring_invoices r
        LEFT JOIN recurring_runs rr ON rr.id=(
          SELECT id FROM recurring_runs WHERE recurring_invoice_id=r.id ORDER BY period DESC LIMIT 1
        )
        WHERE r.customer_id=? ORDER BY r.id
        """,
        (customer_id,),
    )
    template_rows = "".join(
        f"""<tr><td>{h(r['description'])}</td><td class="money">{money(r['unit_price_cents'])}</td>
        <td>am {r['billing_day']}. des Monats</td>
        <td>{'Ja' if r['auto_send'] else 'Nein'}</td>
        <td><span class="status {'paid' if r['active'] else 'cancelled'}">{'Aktiv' if r['active'] else 'Pausiert'}</span></td>
        <td><div class="row-actions"><form method="post" action="/recurring/{r['id']}/run">
        <button class="button compact">Jetzt erzeugen</button></form>
        <form method="post" action="/recurring/{r['id']}/toggle">
        <button class="button compact">{'Pausieren' if r['active'] else 'Aktivieren'}</button></form></div></td></tr>"""
        for r in templates
    ) or '<tr><td colspan="6" class="empty">Noch keine monatliche Rechnung eingerichtet.</td></tr>'
    email_note = (
        f'<a href="mailto:{h(customer["email"])}">{h(customer["email"])}</a>'
        if customer["email"] else '<span class="status cancelled">Noch keine Rechnungs-E-Mail hinterlegt</span>'
    )
    return f"""
    <div class="actions"><a class="button" href="/customer/{customer_id}/edit">Kundendaten bearbeiten</a>
    <a class="button primary" href="/document/new?type=invoice&customer={customer_id}">Neue Rechnung</a></div>
    <div class="card customer-profile"><div><span>Kundennummer</span><strong>{h(customer['customer_number'])}</strong></div>
    <div><span>Unternehmen</span><strong>{h(customer['company'])}</strong></div>
    <div><span>Ansprechpartner</span><strong>{h(customer['contact_name'] or '–')}</strong></div>
    <div><span>Rechnungs-E-Mail</span><strong>{email_note}</strong></div>
    <div><span>Anschrift</span><strong>{h(customer['street'])}<br>{h(customer['postal_code'])} {h(customer['city'])}</strong></div></div>
    <div class="card"><div class="card-head"><h2>Dokumente und importierte Rechnungen</h2></div>
    <div class="table-wrap"><table><thead><tr><th>Nummer</th><th>Art</th><th>Datum</th><th>Betrag</th><th>Status</th></tr></thead>
    <tbody>{document_rows}</tbody></table></div></div>
    <div class="card"><div class="card-head split-head"><div><h2>Monatliche Rechnungen</h2>
    <p class="muted">Automatische Läufe erfolgen einmal pro Stunde und erzeugen je Vorlage höchstens eine Rechnung pro Monat.</p></div>
    <a class="button primary" href="/customer/{customer_id}/recurring/new">Dauerrechnung anlegen</a></div>
    <div class="table-wrap"><table><thead><tr><th>Leistung</th><th>Betrag</th><th>Lauf</th><th>Auto-Versand</th><th>Status</th><th></th></tr></thead>
    <tbody>{template_rows}</tbody></table></div></div>"""


def documents_page(connection, doc_type: str) -> str:
    if doc_type not in TYPE_LABELS:
        doc_type = "invoice"
    documents = rows(
        connection,
        """
        SELECT d.*, c.company FROM documents d JOIN customers c ON c.id=d.customer_id
        WHERE d.document_type=? ORDER BY d.issue_date DESC, d.id DESC
        """,
        (doc_type,),
    )
    document_rows = "".join(
        f"""<tr><td><a href="/document/{d['id']}">{h(d['document_number'] or 'Entwurf')}</a></td>
        <td>{german_date(d['issue_date'])}</td><td>{h(d['company'])}</td>
        <td>{h(d['title'])}</td><td class="money">{money(d['total_cents'])}</td>
        <td><span class="status {h(d['status'])}">{h(DOCUMENT_STATUS.get(d['status'], d['status']))}</span></td></tr>"""
        for d in documents
    ) or f'<tr><td colspan="6" class="empty">Noch keine {h(TYPE_LABELS[doc_type])}-Dokumente vorhanden.</td></tr>'
    create_action = (
        '<div class="alert">Eine Gutschrift wird direkt aus der zugehörigen Rechnung '
        'über „Gutschrift erstellen“ angelegt. Dadurch bleibt die Belegkette eindeutig.</div>'
        if doc_type == "credit"
        else f"""<div class="actions"><a class="button primary" href="/document/new?type={doc_type}">
        {h(TYPE_LABELS[doc_type])} erstellen</a></div>"""
    )
    return f"""
    {create_action}
    <div class="card"><div class="table-wrap"><table><thead><tr><th>Nummer</th><th>Datum</th>
    <th>Kunde</th><th>Betreff</th><th>Betrag</th><th>Status</th></tr></thead>
    <tbody>{document_rows}</tbody></table></div></div>"""


def reminders_page(connection) -> str:
    today = date.today().isoformat()
    invoices = rows(
        connection,
        """
        SELECT d.*, c.company, c.email AS customer_email,
          max(0, d.total_cents - COALESCE((
            SELECT sum(cn.total_cents) FROM documents cn
            WHERE cn.document_type='credit' AND cn.source_document_id=d.id
              AND cn.status='settled'
          ),0)) AS open_cents,
          (SELECT max(reminder_level) FROM payment_reminders r
           WHERE r.document_id=d.id AND r.status='sent') AS reminder_level,
          (SELECT max(reminder_date) FROM payment_reminders r
           WHERE r.document_id=d.id AND r.status='sent') AS last_reminder_date
        FROM documents d
        JOIN customers c ON c.id=d.customer_id
        WHERE d.document_type='invoice'
          AND d.status IN ('final','sent')
          AND d.due_date < ?
          AND d.total_cents > COALESCE((
            SELECT sum(cn.total_cents) FROM documents cn
            WHERE cn.document_type='credit' AND cn.source_document_id=d.id
              AND cn.status='settled'
          ),0)
        ORDER BY d.due_date, d.id
        """,
        (today,),
    )
    invoice_rows = "".join(
        f"""<tr><td><a href="/document/{item['id']}">{h(item['document_number'])}</a></td>
        <td>{h(item['company'])}</td><td>{german_date(item['due_date'])}</td>
        <td>{(date.today() - date.fromisoformat(item['due_date'])).days} Tage</td>
        <td class="money">{money(item['open_cents'])}</td>
        <td>{f"Stufe {item['reminder_level']} · {german_date(item['last_reminder_date'])}" if item['reminder_level'] else "Noch keine"}</td>
        <td>{f'<a class="button compact primary" href="/document/{item["id"]}/reminder">Vorbereiten</a>' if item['customer_email'] else '<span class="status cancelled">E-Mail fehlt</span>'}</td></tr>"""
        for item in invoices
    ) or '<tr><td colspan="7" class="empty">Aktuell sind keine unbezahlten Rechnungen überfällig.</td></tr>'
    return f"""
    <div class="card"><div class="card-head"><h2>Überfällige Rechnungen</h2>
    <p class="muted">Erinnerungen werden erst nach einer Vorschau ausdrücklich versendet.
    Bereits bezahlte oder stornierte Rechnungen erscheinen hier nicht.</p></div>
    <div class="table-wrap"><table><thead><tr><th>Rechnung</th><th>Kunde</th>
    <th>Fällig seit</th><th>Überfällig</th><th>Betrag</th><th>Letzte Erinnerung</th><th></th>
    </tr></thead><tbody>{invoice_rows}</tbody></table></div></div>"""


def reminder_form(connection, document_id: int) -> str:
    document = fetch_document(connection, document_id)
    if (
        not document or document["document_type"] != "invoice"
        or document["status"] not in ("final", "sent")
    ):
        raise ValueError("Für diese Rechnung kann keine Zahlungserinnerung erstellt werden.")
    if not document.get("due_date") or date.fromisoformat(document["due_date"]) >= date.today():
        raise ValueError("Die Rechnung ist noch nicht überfällig.")
    if not document["customer_email"]:
        raise ValueError("Beim Kunden ist keine Rechnungs-E-Mail hinterlegt.")
    previous = connection.execute(
        "SELECT max(reminder_level) FROM payment_reminders WHERE document_id=? AND status='sent'",
        (document_id,),
    ).fetchone()[0] or 0
    if previous >= 3:
        raise ValueError("Für diese Rechnung wurden bereits drei Zahlungserinnerungen versendet.")
    level = previous + 1
    overdue_days = (date.today() - date.fromisoformat(document["due_date"])).days
    open_amount = outstanding_cents(connection, document_id)
    if open_amount <= 0:
        raise ValueError("Für diese Rechnung besteht kein offener Betrag mehr.")
    labels = {
        1: "Freundliche Zahlungserinnerung",
        2: "Zweite Zahlungserinnerung",
        3: "Letzte Zahlungserinnerung",
    }
    subject = f"{labels[level]} zu Rechnung {document['document_number']}"
    message = (
        f"Guten Tag,\n\nbei der Durchsicht unserer Unterlagen ist uns aufgefallen, "
        f"dass die Rechnung {document['document_number']} über {money(open_amount)} "
        f"seit dem {german_date(document['due_date'])} fällig ist.\n\n"
        "Falls Sie den Betrag inzwischen überwiesen haben, betrachten Sie diese Nachricht "
        "bitte als gegenstandslos. Andernfalls bitten wir um zeitnahe Zahlung.\n\n"
        "Mit freundlichen Grüßen"
    )
    return f"""
    <form class="card form" method="post" action="/document/{document_id}/reminder">
      <div class="alert"><b>{labels[level]}</b> · {overdue_days} Tage überfällig ·
      Rechnung {h(document['document_number'])}</div>
      <div class="form-grid">
        <label><span>Empfänger</span><input readonly name="recipient" value="{h(document['customer_email'])}"></label>
        <label><span>Erinnerungsstufe</span><input readonly value="{level}"></label>
        <label class="wide"><span>Betreff *</span><input required name="subject" value="{h(subject)}"></label>
        <label class="wide"><span>Nachricht *</span><textarea class="reminder-message" required name="message">{h(message)}</textarea></label>
      </div>
      <input type="hidden" name="reminder_level" value="{level}">
      <div class="form-actions"><a class="button" href="/reminders">Abbrechen</a>
      <button class="button primary">Zahlungserinnerung jetzt senden</button></div>
    </form>"""


def send_payment_reminder(connection, document_id: int, form: dict) -> None:
    document = fetch_document(connection, document_id)
    if (
        not document or document["document_type"] != "invoice"
        or document["status"] not in ("final", "sent")
    ):
        raise ValueError("Für diese Rechnung kann keine Zahlungserinnerung versendet werden.")
    if not document["customer_email"]:
        raise ValueError("Beim Kunden ist keine Rechnungs-E-Mail hinterlegt.")
    level = int(form.get("reminder_level", "0"))
    expected = (connection.execute(
        "SELECT max(reminder_level) FROM payment_reminders WHERE document_id=? AND status='sent'",
        (document_id,),
    ).fetchone()[0] or 0) + 1
    if expected > 3:
        raise ValueError("Für diese Rechnung wurden bereits drei Zahlungserinnerungen versendet.")
    if level != expected:
        raise ValueError("Die Erinnerungsstufe ist nicht mehr aktuell. Bitte Vorschau neu öffnen.")
    subject = str(form.get("subject", "")).strip()
    message = str(form.get("message", "")).strip()
    if not subject or not message:
        raise ValueError("Betreff und Nachricht dürfen nicht leer sein.")
    body_html = "<p>" + h(message).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    pdf = preferred_document_pdf(connection, document_id)
    status = GraphClient(DB.settings()).send_pdf(
        document["customer_email"], subject, body_html, pdf.name, pdf.read_bytes()
    )
    now = Database.now()
    connection.execute(
        """
        INSERT INTO payment_reminders(
            document_id, reminder_level, reminder_date, recipient, subject,
            body_html, status, response_code, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'sent', ?, ?)
        """,
        (
            document_id, level, date.today().isoformat(), document["customer_email"],
            subject, body_html, str(status), now,
        ),
    )
    connection.execute(
        "UPDATE documents SET status='sent', sent_at=COALESCE(sent_at,?), updated_at=? WHERE id=?",
        (now, now, document_id),
    )
    Database.audit(connection, "document", document_id, "payment_reminder_sent", str(level))


def document_form(
    connection, doc_type: str, source_id: int | None = None,
    preferred_customer_id: int | None = None,
    document_id: int | None = None,
) -> str:
    customers = rows(connection, "SELECT * FROM customers ORDER BY company")
    if not customers:
        return '<div class="empty-state"><h2>Zuerst einen Kunden anlegen</h2><p>Für ein Dokument wird mindestens ein Kunde benötigt.</p><a class="button primary" href="/customer/new">Kunde anlegen</a></div>'
    editing = fetch_document(connection, document_id) if document_id else None
    if editing and editing["status"] != "draft":
        raise ValueError("Nur Entwürfe können bearbeitet werden.")
    source = editing or (fetch_document(connection, source_id) if source_id else None)
    if doc_type == "credit" and not editing:
        if (
            not source or source["document_type"] != "invoice"
            or source["status"] in ("draft", "cancelled")
        ):
            raise ValueError("Eine Gutschrift muss aus einer fertiggestellten Rechnung erstellt werden.")
    source_items = rows(
        connection, "SELECT * FROM document_items WHERE document_id=? ORDER BY position",
        ((document_id or source_id),),
    ) if source else []
    selected_customer_id = (
        source["customer_id"] if source else preferred_customer_id
    )
    customer_options = "".join(
        f'<option value="{c["id"]}" {"selected" if c["id"] == selected_customer_id else ""}>'
        f'{h(c["company"])} ({h(c["customer_number"])})</option>'
        for c in customers
    )
    if not source_items:
        source_items = [{
            "category": "", "description": "", "service_period": "",
            "quantity_milli": 1000, "unit": "pauschal", "unit_price_cents": 0,
        }]
    issue_date = editing["issue_date"] if editing else date.today().isoformat()
    title = source["title"] if source else ""
    notes = editing["notes"] if editing else ""
    credit_reason = editing["credit_reason"] if editing else ""
    if doc_type == "credit" and source and not editing:
        title = f"Gutschrift zu Rechnung {source['document_number'] or source['id']}"
    service_start = editing["service_start"] if editing else ""
    service_end = editing["service_end"] if editing else ""
    action = f"/document/{document_id}/edit" if editing else "/document/new"
    item_cards = "".join(
        document_item_editor(item, index)
        for index, item in enumerate(source_items, start=1)
    )
    return f"""
    <form class="card form" method="post" action="{action}" id="document-form">
      <input type="hidden" name="document_type" value="{h(doc_type)}">
      <input type="hidden" name="source_document_id" value="{h(source_id or '')}">
      <div class="form-grid">
        <label><span>Kunde *</span><select required name="customer_id">{customer_options}</select></label>
        <label><span>Dokumentdatum *</span><input required type="date" name="issue_date" value="{h(issue_date)}"></label>
        <label class="wide"><span>Betreff</span><input name="title" value="{h(title)}" placeholder="z. B. Hosting Services – Virtuelle Webserver"></label>
        <label><span>Leistungsbeginn</span><input type="date" name="service_start" value="{h(service_start)}"></label>
        <label><span>Leistungsende</span><input type="date" name="service_end" value="{h(service_end)}"></label>
      </div>
      <div class="position-section">
        <div class="split-head"><div><h3>Positionen</h3>
        <p class="muted">Positionen können hinzugefügt, entfernt und vor dem
        Fertigstellen jederzeit bearbeitet werden.</p></div>
        <button class="button" type="button" id="add-position">Position hinzufügen</button></div>
        <div id="position-list">{item_cards}</div>
        <div class="position-total"><span>Dokumentsumme</span><strong id="document-total">0,00 €</strong></div>
      </div>
      <div class="form-grid"><label class="wide"><span>Hinweise</span>
      <textarea name="notes">{h(notes)}</textarea></label>
      {f'''<label class="wide"><span>Grund der Gutschrift *</span>
      <textarea required name="credit_reason" placeholder="z. B. Leistungsstornierung oder Preiskorrektur">{h(credit_reason)}</textarea></label>'''
      if doc_type == "credit" else ''}</div>
      <div class="form-actions"><a class="button" href="/documents?type={h(doc_type)}">Abbrechen</a>
      <button class="button primary">{'Änderungen speichern' if editing else 'Als Entwurf speichern'}</button></div>
    </form>
    <template id="position-template">{document_item_editor({}, 0)}</template>
    <script>
    (() => {{
      const list = document.getElementById("position-list");
      const template = document.getElementById("position-template");
      const add = document.getElementById("add-position");
      const parseMoney = value => {{
        let normalized = String(value || "").trim().replace(/\\s/g, "");
        if (normalized.includes(",")) normalized = normalized.replace(/\\./g, "").replace(",", ".");
        return Number.parseFloat(normalized) || 0;
      }};
      const parseQuantity = value => Number.parseFloat(String(value || "").replace(",", ".")) || 0;
      const moneyText = value => value.toLocaleString("de-DE", {{minimumFractionDigits: 2, maximumFractionDigits: 2}}) + " €";
      const update = () => {{
        let sum = 0;
        const cards = [...list.querySelectorAll(".position-card")];
        cards.forEach((card, index) => {{
          card.querySelector(".position-number").textContent = `Position ${{index + 1}}`;
          const subtotal = parseQuantity(card.querySelector("[name=item_quantity]").value)
            * parseMoney(card.querySelector("[name=item_unit_price]").value);
          card.querySelector(".position-subtotal").textContent = moneyText(subtotal);
          card.querySelector(".remove-position").disabled = cards.length === 1;
          sum += subtotal;
        }});
        document.getElementById("document-total").textContent = moneyText(sum);
      }};
      const wire = card => {{
        card.querySelectorAll("input").forEach(input => input.addEventListener("input", update));
        card.querySelector(".remove-position").addEventListener("click", () => {{
          card.remove();
          update();
        }});
      }};
      [...list.querySelectorAll(".position-card")].forEach(wire);
      add.addEventListener("click", () => {{
        const fragment = template.content.cloneNode(true);
        const card = fragment.querySelector(".position-card");
        wire(card);
        list.appendChild(fragment);
        card.querySelector("[name=item_description]").focus();
        update();
      }});
      update();
    }})();
    </script>"""


def document_item_editor(item: dict, index: int) -> str:
    quantity = Decimal(item.get("quantity_milli", 1000)) / 1000
    price = Decimal(item.get("unit_price_cents", 0)) / 100
    quantity_value = format(quantity.normalize(), "f").replace(".", ",")
    price_value = (
        format(price, ".2f").replace(".", ",")
        if item.get("unit_price_cents") is not None else ""
    )
    return f"""
    <article class="position-card">
      <div class="position-card-head"><strong class="position-number">Position {index or ''}</strong>
      <button class="button compact danger remove-position" type="button">Entfernen</button></div>
      <div class="form-grid position-grid">
        <label><span>Kategorie</span><input name="item_category" value="{h(item.get('category', ''))}" placeholder="z. B. Hosting"></label>
        <label class="wide"><span>Beschreibung *</span><input required name="item_description" value="{h(item.get('description', ''))}"></label>
        <label><span>Leistungszeitraum</span><input name="item_service_period" value="{h(item.get('service_period', ''))}" placeholder="01.07.2026–30.09.2026"></label>
        <label><span>Menge *</span><input required name="item_quantity" inputmode="decimal" value="{h(quantity_value)}"></label>
        <label><span>Einheit *</span><input required name="item_unit" value="{h(item.get('unit', 'pauschal'))}"></label>
        <label><span>Einzelpreis in EUR *</span><input required name="item_unit_price" inputmode="decimal" value="{h(price_value)}"></label>
      </div>
      <div class="position-subtotal-row"><span>Positionssumme</span><strong class="position-subtotal">0,00 €</strong></div>
    </article>"""


def document_detail(connection, document_id: int) -> str:
    document = fetch_document(connection, document_id)
    if not document:
        return '<div class="alert error">Dokument nicht gefunden.</div>'
    items = rows(connection, "SELECT * FROM document_items WHERE document_id=? ORDER BY position", (document_id,))
    electronic = connection.execute(
        "SELECT * FROM e_invoice_files WHERE document_id=?", (document_id,)
    ).fetchone()
    item_rows = "".join(
        f"<tr><td>{i['position']}</td><td>{h(i['description'])}</td><td>{h(i['service_period'])}</td>"
        f"<td class='money'>{money(i['unit_price_cents'])}</td><td class='money'>{money(i['total_cents'])}</td></tr>"
        for i in items
    )
    actions = []
    if document["status"] == "draft":
        actions.append(f'<a class="button" href="/document/{document_id}/edit">Entwurf bearbeiten</a>')
        actions.append(f'<form method="post" action="/document/{document_id}/finalize"><button class="button primary">Fertigstellen & PDF erzeugen</button></form>')
        actions.append(
            f'<form method="post" action="/document/{document_id}/delete" '
            f'onsubmit="return confirm(\'Diesen Entwurf endgültig löschen?\')">'
            f'<button class="button danger">Entwurf löschen</button></form>'
        )
    if document["status"] != "draft":
        actions.append(f'<a class="button" href="/document/{document_id}/pdf">PDF öffnen</a>')
        if document["status"] in ("final", "sent") and document["customer_email"]:
            actions.append(f'<form method="post" action="/document/{document_id}/send"><button class="button">Per E-Mail senden</button></form>')
    if (
        document["document_type"] in ("invoice", "credit")
        and document["status"] not in ("draft", "cancelled")
    ):
        actions.append(
            f'<form method="post" action="/document/{document_id}/zugferd">'
            f'<button class="button">{"ZUGFeRD neu validieren" if electronic else "ZUGFeRD erzeugen"}</button></form>'
        )
        if electronic and electronic["xsd_valid"]:
            actions.append(
                f'<a class="button" href="/document/{document_id}/zugferd.pdf">ZUGFeRD-PDF</a>'
            )
            actions.append(
                f'<a class="button" href="/document/{document_id}/zugferd.xml">XML öffnen</a>'
            )
    if document["document_type"] == "offer" and document["status"] in ("final", "sent"):
        actions.append(f'<form method="post" action="/document/{document_id}/convert?to=order"><button class="button primary">Als Auftrag bestätigen</button></form>')
    if document["document_type"] == "order" and document["status"] in ("final", "sent", "accepted"):
        actions.append(f'<form method="post" action="/document/{document_id}/convert?to=invoice"><button class="button primary">Rechnung erstellen</button></form>')
    if document["document_type"] == "invoice" and document["status"] in ("final", "sent"):
        actions.append(
            f'<form class="payment-action" method="post" action="/document/{document_id}/paid">'
            f'<input aria-label="Zahlungsdatum" required type="date" name="payment_date" '
            f'value="{date.today().isoformat()}"><button class="button primary">Als bezahlt markieren</button></form>'
        )
        if document.get("due_date") and date.fromisoformat(document["due_date"]) < date.today():
            actions.append(
                f'<a class="button" href="/document/{document_id}/reminder">'
                "Zahlungserinnerung</a>"
            )
    if document["document_type"] == "invoice" and document["status"] in ("final", "sent", "paid"):
        actions.append(
            f'<a class="button" href="/document/new?type=credit&source={document_id}">'
            "Gutschrift erstellen</a>"
        )
    if document["document_type"] == "credit" and document["status"] in ("final", "sent"):
        actions.append(
            f'<form class="payment-action" method="post" action="/document/{document_id}/credit-settle">'
            f'<input aria-label="Datum" required type="date" name="settlement_date" '
            f'value="{date.today().isoformat()}">'
            '<button class="button primary" name="settlement_type" value="refund">Auszahlung verbuchen</button>'
            '<button class="button" name="settlement_type" value="offset">Mit Rechnung verrechnen</button></form>'
        )
    if document["status"] not in ("draft", "cancelled", "paid", "settled", "refunded"):
        actions.append(
            f'<form method="post" action="/document/{document_id}/cancel" '
            f'onsubmit="return confirm(\'Dokument stornieren? Der Beleg bleibt erhalten.\')">'
            f'<button class="button danger">Stornieren</button></form>'
        )
    settings = DB.settings()
    tax_notice = (
        f'<p class="notice">{h(settings.get("small_business_notice"))}</p>'
        if settings.get("small_business_enabled", "1") == "1"
        and settings.get("small_business_notice")
        else ""
    )
    electronic_notice = ""
    if electronic:
        state = "success" if electronic["xsd_valid"] else "error"
        electronic_notice = (
            f'<div class="alert {state}"><b>{h(electronic["profile"])}</b> · '
            f'{h(electronic["validation_message"])} · '
            f'{h(electronic["generated_at"])}</div>'
        )
    return f"""
    <div class="actions inline-actions">{''.join(actions)}</div>
    {electronic_notice}
    <div class="document-sheet">
      <div class="document-head"><div><span>{h(TYPE_LABELS[document['document_type']])}</span>
      <h2>{h(document['document_number'] or 'Entwurf')}</h2></div>
      <span class="status {h(document['status'])}">{h(DOCUMENT_STATUS.get(document['status']))}</span></div>
      <div class="document-meta"><div><span>Kunde</span><strong>{h(document['company'])}</strong></div>
      <div><span>Datum</span><strong>{german_date(document['issue_date'])}</strong></div>
      <div><span>Betreff</span><strong>{h(document['title'])}</strong></div></div>
      {f'<p class="notice"><b>Grund:</b> {h(document["credit_reason"])}</p>'
      if document["document_type"] == "credit" and document.get("credit_reason") else ""}
      <table><thead><tr><th>Pos.</th><th>Leistung</th><th>Zeitraum</th><th>Einzelpreis</th><th>Gesamt</th></tr></thead>
      <tbody>{item_rows}</tbody></table>
      <div class="grand-total"><span>Gesamtbetrag</span><strong>{money(document['total_cents'])}</strong></div>
      {tax_notice}
    </div>"""


def next_number(connection, document_type: str, issue_date: str) -> str:
    counter_key = {
        "invoice": "invoice_counter",
        "credit": "invoice_counter",
        "offer": "offer_counter",
        "order": "order_counter",
    }[document_type]
    current = int(connection.execute("SELECT value FROM settings WHERE key=?", (counter_key,)).fetchone()[0])
    value = current + 1
    connection.execute("UPDATE settings SET value=? WHERE key=?", (str(value), counter_key))
    prefix = {"invoice": "", "credit": "GS-", "offer": "AN-", "order": "AB-"}[document_type]
    return f"{prefix}{issue_date[:7]}-{value:04d}"


def finalize_document(connection, document_id: int):
    document = fetch_document(connection, document_id)
    if not document or document["status"] != "draft":
        raise ValueError("Nur Entwürfe können fertiggestellt werden.")
    if document["document_type"] == "credit":
        if not document.get("credit_reason"):
            raise ValueError("Für eine Gutschrift muss ein Grund angegeben werden.")
        source = fetch_document(connection, document.get("source_document_id"))
        if (
            not source or source["document_type"] != "invoice"
            or source["status"] in ("draft", "cancelled")
            or source["customer_id"] != document["customer_id"]
        ):
            raise ValueError("Die zugehörige Rechnung ist ungültig.")
        previous_credits = connection.execute(
            """
            SELECT COALESCE(sum(total_cents),0) FROM documents
            WHERE document_type='credit' AND source_document_id=?
              AND id<>? AND status NOT IN ('draft','cancelled')
            """,
            (source["id"], document_id),
        ).fetchone()[0]
        remaining = source["total_cents"] - previous_credits
        if document["total_cents"] > remaining:
            raise ValueError(
                f"Der Gutschriftsbetrag übersteigt den noch verfügbaren Betrag "
                f"von {money(max(0, remaining))}."
            )
    number = next_number(connection, document["document_type"], document["issue_date"])
    finalized_at = Database.now()
    connection.execute(
        "UPDATE documents SET document_number=?, status='final', finalized_at=?, updated_at=? WHERE id=?",
        (number, finalized_at, finalized_at, document_id),
    )
    Database.audit(connection, "document", document_id, "finalized", number)
    generate_pdf(connection, document_id)
    return number


def generate_pdf(connection, document_id: int) -> Path:
    document = fetch_document(connection, document_id)
    customer = dict(connection.execute("SELECT * FROM customers WHERE id=?", (document["customer_id"],)).fetchone())
    items = rows(connection, "SELECT * FROM document_items WHERE document_id=? ORDER BY position", (document_id,))
    settings = DB.settings()
    safe_number = re.sub(r"[^A-Za-z0-9._-]", "_", document["document_number"])
    output = DATA_DIR / "documents" / f"{safe_number}.pdf"
    create_document_pdf(
        output,
        document,
        customer,
        items,
        settings,
        COMPANY_LOGO if COMPANY_LOGO.is_file() else None,
    )
    return output


def generate_zugferd(connection, document_id: int) -> dict:
    document = fetch_document(connection, document_id)
    if (
        not document or document["document_type"] not in ("invoice", "credit")
        or document["status"] == "draft"
    ):
        raise ValueError(
            "ZUGFeRD kann nur für fertiggestellte Rechnungen und Gutschriften erzeugt werden."
        )
    customer = dict(connection.execute(
        "SELECT * FROM customers WHERE id=?", (document["customer_id"],)
    ).fetchone())
    items = rows(
        connection,
        "SELECT * FROM document_items WHERE document_id=? ORDER BY position",
        (document_id,),
    )
    regular_pdf = generate_pdf(connection, document_id)
    safe_number = re.sub(r"[^A-Za-z0-9._-]", "_", document["document_number"])
    pdf_filename = f"{safe_number}-zugferd.pdf"
    xml_filename = f"{safe_number}-factur-x.xml"
    output_pdf = DATA_DIR / "documents" / pdf_filename
    output_xml = DATA_DIR / "documents" / xml_filename
    try:
        result = create_zugferd(
            regular_pdf, output_pdf, output_xml, document, customer, items,
            DB.settings(),
        )
    except Exception as exc:
        now = Database.now()
        connection.execute(
            """
            INSERT INTO e_invoice_files(
                document_id, profile, pdf_filename, xml_filename, xsd_valid,
                validation_message, generated_at
            ) VALUES (?, 'ZUGFeRD / Factur-X EN 16931', ?, ?, 0, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
              xsd_valid=0, validation_message=excluded.validation_message,
              generated_at=excluded.generated_at
            """,
            (document_id, pdf_filename, xml_filename, str(exc)[:1000], now),
        )
        raise ValueError(f"ZUGFeRD-Validierung fehlgeschlagen: {exc}") from exc
    now = Database.now()
    validation_message = (
        "XML-Schema und EN-16931-Geschäftsregeln (Schematron) erfolgreich validiert."
        if result.get("schematron_checked")
        else "XML-Schema erfolgreich validiert. Schematron-Prüfung (Geschäftsregeln) "
        "nicht verfügbar – Java-Validator ist in dieser Installation nicht vorhanden."
    )
    connection.execute(
        """
        INSERT INTO e_invoice_files(
            document_id, profile, pdf_filename, xml_filename, xsd_valid,
            validation_message, generated_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
          profile=excluded.profile, pdf_filename=excluded.pdf_filename,
          xml_filename=excluded.xml_filename, xsd_valid=1,
          validation_message=excluded.validation_message,
          generated_at=excluded.generated_at
        """,
        (document_id, result["profile"], pdf_filename, xml_filename, validation_message, now),
    )
    Database.audit(
        connection, "document", document_id, "zugferd_generated", result["profile"]
    )
    return result


def electronic_invoice_path(connection, document_id: int, kind: str) -> Path:
    item = connection.execute(
        "SELECT * FROM e_invoice_files WHERE document_id=? AND xsd_valid=1",
        (document_id,),
    ).fetchone()
    if not item:
        raise ValueError("Für dieses Dokument wurde noch keine gültige E-Rechnung erzeugt.")
    filename = item["pdf_filename"] if kind == "pdf" else item["xml_filename"]
    root = (DATA_DIR / "documents").resolve()
    target = (root / filename).resolve()
    if target.parent != root or not target.is_file():
        raise ValueError("Die erzeugte E-Rechnungsdatei wurde nicht gefunden.")
    return target


def preferred_document_pdf(connection, document_id: int) -> Path:
    try:
        return electronic_invoice_path(connection, document_id, "pdf")
    except ValueError:
        return generate_pdf(connection, document_id)


MONTH_NAMES = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def recurring_form(connection, customer_id: int) -> str:
    customer = connection.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        raise ValueError("Kunde wurde nicht gefunden.")
    warning = (
        ""
        if customer["email"] else
        '<div class="alert error">Für automatischen Versand muss zuerst eine Rechnungs-E-Mail beim Kunden hinterlegt werden.</div>'
    )
    return f"""
    {warning}
    <form class="card form" method="post" action="/customer/{customer_id}/recurring/new">
      <h2>Monatliche Rechnung für {h(customer['company'])}</h2>
      <div class="form-grid">
        <label><span>Rechnungstag (1–28)</span><input required type="number" min="1" max="28" name="billing_day" value="1"></label>
        <label><span>Betreff</span><input name="title" placeholder="Hosting Services"></label>
        <label><span>Kategorie</span><input name="category" placeholder="Hosting"></label>
        <label class="wide"><span>Leistungsbeschreibung *</span><input required name="description" placeholder="Virtueller Webserver"></label>
        <label class="wide"><span>Leistungszeitraum</span><input name="service_period_template" value="{{monat}} {{jahr}}">
        <small class="muted">Platzhalter: {{monat}}, {{monat_nummer}} und {{jahr}}</small></label>
        <label><span>Menge</span><input name="quantity" value="1"></label>
        <label><span>Einheit</span><input name="unit" value="pauschal"></label>
        <label><span>Einzelpreis in EUR *</span><input required name="unit_price" inputmode="decimal"></label>
        <label class="check"><input type="checkbox" name="auto_finalize" checked><span>Automatisch fertigstellen und PDF erzeugen</span></label>
        <label class="check"><input type="checkbox" name="auto_send" {'disabled' if not customer['email'] else ''}>
        <span>PDF automatisch an die Rechnungs-E-Mail versenden</span></label>
      </div>
      <div class="form-actions"><a class="button" href="/customer/{customer_id}">Abbrechen</a>
      <button class="button primary">Dauerrechnung speichern</button></div>
    </form>"""


def send_document_email(connection, document_id: int):
    document = fetch_document(connection, document_id)
    if not document or document["status"] not in ("final", "sent"):
        raise ValueError("Nur fertiggestellte Dokumente können versendet werden.")
    if not document["customer_email"]:
        raise ValueError("Beim Kunden ist keine Rechnungs-E-Mail hinterlegt.")
    pdf = preferred_document_pdf(connection, document_id)
    settings = DB.settings()
    company_name = settings.get("company_name", "")
    closing_name = settings.get("owner_name") or company_name
    subject = (
        f"{TYPE_LABELS[document['document_type']]} "
        f"{document['document_number']} von {company_name}"
    )
    body_html = (
        f"<p>Guten Tag,</p><p>anbei erhalten Sie {TYPE_LABELS[document['document_type']].lower()} "
        f"{h(document['document_number'])}.</p><p>Mit freundlichen Grüßen<br>{h(closing_name)}</p>"
    )
    status = GraphClient(settings).send_pdf(
        document["customer_email"], subject, body_html, pdf.name, pdf.read_bytes()
    )
    now = Database.now()
    connection.execute(
        "UPDATE documents SET status='sent', sent_at=?, updated_at=? WHERE id=?",
        (now, now, document_id),
    )
    connection.execute(
        """
        INSERT INTO mail_log(document_id, recipient, subject, status, response_code, sent_at)
        VALUES (?, ?, ?, 'sent', ?, ?)
        """,
        (document_id, document["customer_email"], subject, str(status), now),
    )
    Database.audit(connection, "document", document_id, "sent", document["customer_email"])


def run_recurring_invoice(connection, recurring_id: int, run_date: date, manual: bool = False):
    template = connection.execute(
        """
        SELECT r.*, c.email AS customer_email FROM recurring_invoices r
        JOIN customers c ON c.id=r.customer_id WHERE r.id=?
        """,
        (recurring_id,),
    ).fetchone()
    if not template:
        raise ValueError("Dauerrechnung wurde nicht gefunden.")
    if not manual and (not template["active"] or run_date.day < template["billing_day"]):
        return None
    period = run_date.strftime("%Y-%m")
    now = Database.now()

    def create_run():
        return connection.execute(
            """
            INSERT INTO recurring_runs(recurring_invoice_id, period, status, created_at, updated_at)
            VALUES (?, ?, 'processing', ?, ?)
            """,
            (recurring_id, period, now, now),
        ).lastrowid

    try:
        run_id = create_run()
    except sqlite3.IntegrityError:
        # A prior run for this period exists. If its document was cancelled
        # (e.g. it had a wrong number), free up the period instead of
        # permanently blocking this recurring invoice from ever running again.
        existing = connection.execute(
            """
            SELECT rr.id, d.status document_status
            FROM recurring_runs rr LEFT JOIN documents d ON d.id=rr.document_id
            WHERE rr.recurring_invoice_id=? AND rr.period=?
            """,
            (recurring_id, period),
        ).fetchone()
        if not existing or existing["document_status"] != "cancelled":
            return None
        connection.execute("DELETE FROM recurring_runs WHERE id=?", (existing["id"],))
        run_id = create_run()

    last_day = calendar.monthrange(run_date.year, run_date.month)[1]
    service_start = date(run_date.year, run_date.month, 1)
    service_end = date(run_date.year, run_date.month, last_day)
    period_text = template["service_period_template"].format(
        monat=MONTH_NAMES[run_date.month],
        monat_nummer=f"{run_date.month:02d}",
        jahr=run_date.year,
    )
    terms = int(DB.settings()["payment_terms_days"])
    due_date = (run_date + timedelta(days=terms)).isoformat()
    qty = template["quantity_milli"]
    total = int((Decimal(qty) / 1000 * template["unit_price_cents"]).quantize(Decimal("1")))
    document_id = connection.execute(
        """
        INSERT INTO documents(document_type, status, customer_id, issue_date, service_start,
        service_end, due_date, payment_terms_days, title, total_cents, created_at, updated_at)
        VALUES ('invoice', 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            template["customer_id"], run_date.isoformat(), service_start.isoformat(),
            service_end.isoformat(), due_date, terms, template["title"], total, now, now,
        ),
    ).lastrowid
    connection.execute(
        """
        INSERT INTO document_items(document_id, position, category, description, quantity_milli,
        unit, unit_price_cents, total_cents, service_period)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id, template["category"], template["description"], qty, template["unit"],
            template["unit_price_cents"], total, period_text,
        ),
    )
    status = "draft_created"
    error = ""
    try:
        if template["auto_finalize"] or template["auto_send"]:
            finalize_document(connection, document_id)
            status = "finalized"
        if template["auto_send"]:
            send_document_email(connection, document_id)
            status = "sent"
    except Exception as exc:
        status = "send_failed"
        error = str(exc)
    connection.execute(
        """
        UPDATE recurring_runs SET document_id=?, status=?, error=?, updated_at=? WHERE id=?
        """,
        (document_id, status, error, Database.now(), run_id),
    )
    Database.audit(connection, "recurring_invoice", recurring_id, status, period)
    return document_id, status, error


def process_due_recurring():
    try:
        with DB.connect() as connection:
            ids = [
                row["id"] for row in connection.execute(
                    "SELECT id FROM recurring_invoices WHERE active=1 AND billing_day<=?",
                    (date.today().day,),
                )
            ]
            for recurring_id in ids:
                run_recurring_invoice(connection, recurring_id, date.today())
    except Exception as exc:
        print(f"Dauerrechnungsprüfung fehlgeschlagen: {exc}", flush=True)


def recurring_worker():
    time.sleep(30)
    while True:
        process_due_recurring()
        time.sleep(3600)


def archive_filters_from_query(query: dict) -> dict[str, str]:
    return {
        "customer": str(query.get("customer", [""])[0]).strip(),
        "customer_number": str(query.get("customer_number", [""])[0]).strip(),
    }


def archive_filter_suffix(filters: dict[str, str]) -> str:
    values = {key: value for key, value in filters.items() if value}
    return "?" + urllib.parse.urlencode(values) if values else ""


def archive_page(connection, filters: dict[str, str] | None = None) -> str:
    filters = filters or {"customer": "", "customer_number": ""}
    conditions, parameters = [], []
    if filters["customer"]:
        pattern = f"%{filters['customer'].lower()}%"
        conditions.append(
            "(lower(a.detected_customer_name) LIKE ? "
            "OR lower(COALESCE(c.company,'')) LIKE ?)"
        )
        parameters.extend((pattern, pattern))
    if filters["customer_number"]:
        pattern = f"%{filters['customer_number'].lower()}%"
        conditions.append(
            "(lower(a.detected_customer_number) LIKE ? "
            "OR lower(COALESCE(c.customer_number,'')) LIKE ?)"
        )
        parameters.extend((pattern, pattern))
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    files = rows(
        connection,
        f"""
        SELECT a.*, d.document_number, c.company AS imported_customer,
               i.id AS incoming_invoice_id
        FROM archive_files a
        LEFT JOIN customers c ON c.id=a.customer_id
        LEFT JOIN documents d ON d.id=a.document_id
        LEFT JOIN incoming_invoices i ON i.archive_file_id=a.id
        {where}
        ORDER BY
          CASE WHEN trim(a.detected_invoice_number)='' THEN 1 ELSE 0 END,
          a.detected_invoice_number COLLATE NOCASE DESC,
          a.detected_issue_date DESC,
          a.uploaded_at DESC,
          a.id DESC
        """,
        tuple(parameters),
    )
    filter_suffix = archive_filter_suffix(filters)
    escaped_suffix = h(filter_suffix)
    open_files = [file for file in files if not file["reviewed_at"]]
    continue_button = (
        f'<a class="button primary" href="/archive/{open_files[0]["id"]}{escaped_suffix}">'
        f'Prüfung fortsetzen · {len(open_files)} offen</a>'
        if open_files else
        '<span class="status paid">Alle importierten Belege geprüft</span>'
    )
    file_rows = "".join(
        f"<tr><td><strong>{h(f['original_filename'])}</strong></td>"
        f"<td><span class='status'>{'Eingang' if f['document_direction'] == 'incoming' else 'Ausgang'}</span><br>"
        f"{h(f['detected_invoice_number'] or f['document_number'] or 'Noch nicht erkannt')}</td>"
        f"<td>{h(f['detected_customer_name'] or '–')}"
        f"{f'<br><small>Kd.-Nr. {h(f['detected_customer_number'])}</small>' if f['detected_customer_number'] else ''}</td>"
        f"<td>{german_date(f['detected_issue_date']) if f['detected_issue_date'] else '–'}</td>"
        f"<td class='money'>{money(f['detected_amount_cents']) if f['detected_amount_cents'] is not None else '–'}</td>"
        f"<td><span class='status {'paid' if f['reviewed_at'] else ''}'>"
        f"{'Geprüft' if f['reviewed_at'] else 'Offen'}</span></td>"
        f"<td><div class='row-actions'><a class='button compact' href='/archive/{f['id']}{escaped_suffix}'>Prüfen</a>"
        f"<a class='button compact' target='_blank' href='/archive/{f['id']}/pdf'>Öffnen</a>"
        f"<form method='post' action='/archive/{f['id']}/analyze'><button class='button compact'>Neu analysieren</button></form>"
        + (
            f"<form method='post' action='/archive/{f['id']}/delete' "
            f"onsubmit=\"return confirm('Fehlimport und PDF endgültig löschen?')\">"
            f"<button class='button compact danger'>Löschen</button></form>"
            if archive_can_be_deleted(f)
            else ""
        )
        + f"</div></td></tr>"
        for f in files
    ) or (
        '<tr><td colspan="7" class="empty">Keine passenden Archivbelege gefunden.</td></tr>'
        if filters["customer"] or filters["customer_number"] else
        '<tr><td colspan="7" class="empty">Noch keine alten Rechnungen importiert.</td></tr>'
    )
    return f"""
    <div class="actions">{continue_button}</div>
    <form class="card form" method="get" action="/archive">
      <h2>Archiv filtern</h2>
      <div class="form-grid">
        <label><span>Kunde / Unternehmen</span>
          <input name="customer" value="{h(filters['customer'])}"
                 placeholder="z. B. Muster GmbH">
        </label>
        <label><span>Kundennummer</span>
          <input name="customer_number" value="{h(filters['customer_number'])}"
                 placeholder="z. B. 1002">
        </label>
      </div>
      <div class="form-actions">
        <a class="button" href="/archive">Filter zurücksetzen</a>
        <button class="button primary">Filtern · {len(files)} Treffer</button>
      </div>
    </form>
    <div class="card form">
      <h2>PDF-Belege importieren</h2>
      <p class="muted">Bis zu 50 PDFs gemeinsam auswählen. Die Originaldateien bleiben
      unverändert; erkannte Daten werden anschließend einzeln geprüft.</p>
      <form method="post" action="/archive/upload" enctype="multipart/form-data">
      <div class="form-grid">
      <label><span>Belegart *</span><select name="document_direction">
        <option value="outgoing">Ausgangsrechnungen</option>
        <option value="incoming">Eingangsrechnungen</option>
      </select></label>
      <label class="wide"><span>PDF-Dateien * (maximal 50)</span>
      <input required multiple type="file" name="pdf" accept="application/pdf"></label>
      <label><span>Rechnungsnummer</span><input name="document_number" placeholder="nur bei Einzelimport"></label>
      <label><span>Rechnungsdatum</span><input type="date" name="issue_date"></label>
      <label><span>Betrag in EUR</span><input name="amount" inputmode="decimal"></label></div>
      <div class="form-actions"><button class="button primary">Belege hochladen und analysieren</button></div></form>
    </div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>Datei</th><th>Nummer</th><th>Kunde</th><th>Datum</th><th>Betrag</th><th>Prüfung</th><th>Aktionen</th></tr></thead>
    <tbody>{file_rows}</tbody></table></div></div>"""


def archive_detail(
    connection,
    archive_id: int,
    filters: dict[str, str] | None = None,
) -> str:
    filters = filters or {"customer": "", "customer_number": ""}
    filter_suffix = archive_filter_suffix(filters)
    escaped_suffix = h(filter_suffix)
    item = connection.execute(
        """
        SELECT a.*, c.company AS imported_customer, c.customer_number,
               i.id AS incoming_invoice_id
        FROM archive_files a LEFT JOIN customers c ON c.id=a.customer_id
        LEFT JOIN incoming_invoices i ON i.archive_file_id=a.id
        WHERE a.id=?
        """,
        (archive_id,),
    ).fetchone()
    if not item:
        raise ValueError("Archivdatei wurde nicht gefunden.")
    next_id = Database.next_unreviewed_archive_id(
        connection,
        archive_id,
        item["document_direction"],
        filters["customer"],
        filters["customer_number"],
    )
    queue_state = (
        f'<span class="status paid">Geprüft</span>'
        if item["reviewed_at"] else
        '<span class="status">Offen</span>'
    )
    next_button = (
        f'<a class="button" href="/archive/{next_id}{escaped_suffix}">'
        f'Nächster offener Beleg</a>'
        if next_id else ""
    )
    amount_value = (
        f"{item['detected_amount_cents'] / 100:.2f}".replace(".", ",")
        if item["detected_amount_cents"] is not None else ""
    )
    state = (
        f'<div class="alert success">Als Kunde {h(item["customer_number"])} · '
        f'{h(item["imported_customer"])} übernommen.</div>'
        if item["customer_id"] else ""
    )
    warning = (
        f'<div class="alert error">{h(item["analysis_error"])}</div>'
        if item["analysis_error"] else ""
    )
    is_incoming = item["document_direction"] == "incoming"
    customer_button = (
        ""
        if item["customer_id"] or is_incoming else
        f"""<button class="button primary" type="submit"
        form="archive-customer-import">Kundendaten übernehmen</button>"""
    )
    incoming_button = (
        f'<form method="post" action="/archive/{archive_id}/incoming">'
        f'<button class="button primary">Als Eingangsrechnung erfassen</button></form>'
        if is_incoming and not item["incoming_invoice_id"] else
        (
            f'<a class="button primary" href="/incoming/{item["incoming_invoice_id"]}">'
            f'Eingangsrechnung öffnen</a>'
            if item["incoming_invoice_id"] else ""
        )
    )
    delete_button = (
        f'<form method="post" action="/archive/{archive_id}/delete" '
        f'onsubmit="return confirm(\'Fehlimport und PDF endgültig löschen?\')">'
        f'<button class="button danger">Fehlimport löschen</button></form>'
        if archive_can_be_deleted(item)
        else ""
    )
    payment_form = ""
    if not is_incoming:
        terms = DB.settings().get("payment_terms_days", "14")
        payment_value = item["payment_date"] or suggested_payment_date(
            item["detected_issue_date"], terms
        )
        payment_form = f"""
        <form class="card form" method="post"
              action="/archive/{archive_id}/payment{escaped_suffix}">
          <h2>Zahlung für EÜR</h2>
          <p class="muted">Historische Ausgangsrechnungen werden erst mit einem
          Zahlungsdatum als Betriebseinnahme berücksichtigt.</p>
          <div class="form-grid">
            <label><span>Status</span><select name="accounting_status">
              <option value="unbooked" {'selected' if item['accounting_status'] == 'unbooked' else ''}>Noch nicht bezahlt</option>
              <option value="paid" {'selected' if item['accounting_status'] == 'paid' else ''}>Bezahlt</option>
              <option value="cancelled" {'selected' if item['accounting_status'] == 'cancelled' else ''}>Storniert</option>
            </select></label>
            <label><span>Zahlungsdatum</span><input type="date" name="payment_date" value="{h(payment_value)}"></label>
          </div>
          <p class="muted">Vorschlag: Rechnungsdatum plus {h(terms)} Tage Zahlungsziel;
          Samstag und Sonntag werden auf Montag verschoben. Für die EÜR bitte
          abweichende tatsächliche Zahlungseingänge korrigieren.</p>
          <div class="form-actions">
            <button class="button">Zahlungsstatus speichern</button>
            <button class="button primary" name="continue" value="1">
              Speichern &amp; nächster Beleg
            </button>
            <button class="button primary" name="mark_paid" value="1">
              Als bezahlt &amp; nächster Beleg
            </button>
          </div>
        </form>"""
    return f"""
    {state}{warning}
    <form id="archive-customer-import" method="post" action="/archive/{archive_id}/customer"></form>
    <div class="actions">
      {queue_state}{next_button}
      <a class="button" target="_blank" href="/archive/{archive_id}/pdf">Original-PDF öffnen</a>
      <form method="post" action="/archive/{archive_id}/analyze"><button class="button">Neu analysieren</button></form>
      {incoming_button}{delete_button}
    </div>
    <form class="card form" method="post"
          action="/archive/{archive_id}/metadata{escaped_suffix}">
      <h2>Erkannte Daten prüfen</h2>
      <p class="muted">Korrigiere die Felder bei Bedarf, bevor du den Kunden übernimmst.</p>
      <div class="form-grid">
        <label><span>Rechnungsnummer</span><input name="invoice_number" value="{h(item['detected_invoice_number'])}"></label>
        <label><span>Rechnungsdatum</span><input type="date" name="issue_date" value="{h(item['detected_issue_date'])}"></label>
        <label><span>Rechnungsbetrag in EUR</span><input name="amount" value="{h(amount_value)}"></label>
        <label><span>{'Lieferant / Aussteller' if is_incoming else 'Unternehmen / Kunde *'}</span><input name="customer_name" value="{h(item['detected_customer_name'])}"></label>
        <label><span>Erkannte Kundennummer</span><input name="customer_number" value="{h(item['detected_customer_number'])}"></label>
        <label><span>Straße</span><input name="street" value="{h(item['detected_street'])}"></label>
        <label><span>PLZ</span><input name="postal_code" value="{h(item['detected_postal_code'])}"></label>
        <label><span>Ort</span><input name="city" value="{h(item['detected_city'])}"></label>
      </div>
      <div class="form-actions">
        <button class="button">Korrekturen speichern</button>
        <button class="button primary" name="continue" value="1">
          Geprüft &amp; nächster Beleg
        </button>
        {customer_button}
      </div>
    </form>
    {payment_form}
    <details class="card extracted"><summary>Erkannten PDF-Text anzeigen</summary>
      <pre>{h(item['extracted_text'])}</pre>
    </details>"""


def update_archive_analysis(connection, archive_id: int, result: dict):
    connection.execute(
        """
        UPDATE archive_files SET extracted_text=?, detected_invoice_number=?,
        detected_issue_date=?, detected_amount_cents=?, detected_customer_name=?,
        detected_customer_number=?, detected_street=?, detected_postal_code=?, detected_city=?, analyzed_at=?,
        analysis_error=? WHERE id=?
        """,
        (
            result["text"], result["invoice_number"], result["issue_date"],
            result["amount_cents"], result["customer_name"], result["customer_number"],
            result["street"], result["postal_code"],
            result["city"], Database.now(), result["error"],
            archive_id,
        ),
    )
    Database.link_archives_by_customer_number(connection, archive_id)


def link_or_create_customer(connection, archive_id: int) -> tuple[int, str]:
    """Match the recognized sender to an existing customer or create one.

    Shared by the manual "Kundendaten übernehmen" action and by automatic
    linking when a document is marked as paid.
    """
    item = connection.execute(
        "SELECT * FROM archive_files WHERE id=?", (archive_id,)
    ).fetchone()
    if not item or not item["detected_customer_name"]:
        raise ValueError("Bitte zuerst mindestens den Kundennamen prüfen und speichern.")
    existing_customer = connection.execute(
        """
        SELECT id, customer_number FROM customers
        WHERE (
            ? != '' AND customer_number=?
        ) OR (
            lower(trim(company))=lower(trim(?))
            AND (?='' OR postal_code=?)
        )
        ORDER BY id LIMIT 1
        """,
        (
            item["detected_customer_number"], item["detected_customer_number"],
            item["detected_customer_name"], item["detected_postal_code"],
            item["detected_postal_code"],
        ),
    ).fetchone()
    if existing_customer:
        customer_id = existing_customer["id"]
        customer_number = existing_customer["customer_number"]
        message = f"Vorhandener Kunde {customer_number} wurde mit dem PDF verknüpft."
    else:
        settings = DB.settings()
        detected_number = item["detected_customer_number"].strip()
        number_in_use = (
            connection.execute(
                "SELECT 1 FROM customers WHERE customer_number=?",
                (detected_number,),
            ).fetchone()
            if detected_number else None
        )
        customer_number = (
            detected_number
            if detected_number and not number_in_use
            else str(int(settings["customer_counter"]) + 1)
        )
        now = Database.now()
        customer_id = connection.execute(
            """
            INSERT INTO customers(customer_number, company, street, postal_code, city, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                customer_number, item["detected_customer_name"],
                item["detected_street"], item["detected_postal_code"],
                item["detected_city"], now, now,
            ),
        ).lastrowid
        connection.execute(
            "UPDATE settings SET value=? WHERE key='customer_counter'",
            (str(max(int(settings["customer_counter"]), int(customer_number)))
             if customer_number.isdigit() else settings["customer_counter"],),
        )
        Database.audit(connection, "customer", customer_id, "created_from_archive", str(archive_id))
        message = f"Kunde {customer_number} wurde aus dem PDF angelegt."
    connection.execute(
        "UPDATE archive_files SET customer_id=? WHERE id=?",
        (customer_id, archive_id),
    )
    Database.audit(connection, "archive", archive_id, "customer_linked", str(customer_id))
    return customer_id, message


def incoming_page(connection) -> str:
    invoices = rows(
        connection,
        """
        SELECT i.*, COALESCE(s.company,'Noch nicht zugeordnet') supplier,
               a.original_filename
        FROM incoming_invoices i
        LEFT JOIN suppliers s ON s.id=i.supplier_id
        LEFT JOIN archive_files a ON a.id=i.archive_file_id
        ORDER BY i.invoice_date DESC, i.id DESC
        """,
    )
    status_labels = {
        "draft": "Entwurf", "booked": "Gebucht", "paid": "Bezahlt", "cancelled": "Storniert"
    }
    invoice_rows = "".join(
        f"""<tr><td><a href="/incoming/{item['id']}"><strong>{h(item['invoice_number'] or 'Ohne Nummer')}</strong></a></td>
        <td>{h(item['supplier'])}</td><td>{german_date(item['invoice_date'])}</td>
        <td>{german_date(item['payment_date']) if item['payment_date'] else '–'}</td>
        <td>{h(item['eur_category'])}</td><td class="money">{money(item['deductible_cents'])}</td>
        <td><span class="status {h(item['status'])}">{h(status_labels[item['status']])}</span></td></tr>"""
        for item in invoices
    ) or '<tr><td colspan="7" class="empty">Noch keine Eingangsrechnungen erfasst.</td></tr>'
    return f"""
    <div class="actions"><a class="button primary" href="/archive">PDFs importieren</a></div>
    <div class="card">
      <div class="card-head"><h2>Eingangsrechnungen</h2>
      <p class="muted">Belege werden zuerst im Archiv importiert und anschließend hier
      als Ausgabe gebucht. Für die EÜR ist das Zahlungsdatum maßgeblich.</p></div>
      <div class="table-wrap"><table><thead><tr><th>Rechnungsnummer</th><th>Lieferant</th>
      <th>Rechnungsdatum</th><th>Bezahlt am</th><th>EÜR-Kategorie</th>
      <th>Abziehbarer Betrag</th><th>Status</th></tr></thead><tbody>{invoice_rows}</tbody></table></div>
    </div>"""


def incoming_detail(connection, incoming_id: int) -> str:
    item = connection.execute(
        """
        SELECT i.*, a.original_filename, a.id archive_id,
               COALESCE(NULLIF(s.company,''), a.detected_customer_name) supplier_company,
               COALESCE(s.contact_name,'') supplier_contact,
               COALESCE(NULLIF(s.street,''), a.detected_street) supplier_street,
               COALESCE(NULLIF(s.postal_code,''), a.detected_postal_code) supplier_postal_code,
               COALESCE(NULLIF(s.city,''), a.detected_city) supplier_city,
               COALESCE(s.email,'') supplier_email,
               s.payment_terms_days supplier_payment_terms_days
        FROM incoming_invoices i
        LEFT JOIN archive_files a ON a.id=i.archive_file_id
        LEFT JOIN suppliers s ON s.id=i.supplier_id
        WHERE i.id=?
        """,
        (incoming_id,),
    ).fetchone()
    if not item:
        raise ValueError("Eingangsrechnung wurde nicht gefunden.")
    suppliers = rows(
        connection,
        """
        SELECT company, contact_name, street, postal_code, city, email, payment_terms_days
        FROM suppliers ORDER BY company COLLATE NOCASE
        """,
    )
    supplier_options = "".join(
        f'<option value="{h(supplier["company"])}"></option>' for supplier in suppliers
    )
    supplier_map = json.dumps({
        supplier["company"].strip().lower(): {
            "contact": supplier["contact_name"], "street": supplier["street"],
            "postal_code": supplier["postal_code"], "city": supplier["city"],
            "email": supplier["email"],
            "terms": "" if supplier["payment_terms_days"] is None else supplier["payment_terms_days"],
        }
        for supplier in suppliers
    })
    supplier_terms = item["supplier_payment_terms_days"]
    if supplier_terms in (None, ""):
        supplier_terms = DB.settings().get("payment_terms_days", "14")
    payment_value = item["payment_date"] or suggested_payment_date(
        item["invoice_date"], supplier_terms
    )
    category_options = "".join(
        f'<option value="{h(category)}" {"selected" if category == item["eur_category"] else ""}>{h(category)}</option>'
        for category in EXPENSE_CATEGORIES
    )
    gross = f"{item['gross_cents'] / 100:.2f}".replace(".", ",")
    editable = item["status"] in ("draft", "booked")
    readonly = "" if editable else "disabled"
    delete = (
        f'<form method="post" action="/incoming/{incoming_id}/delete" '
        f'onsubmit="return confirm(\'Entwurf und zugehörige PDF endgültig löschen?\')">'
        f'<button class="button danger">Entwurf samt PDF löschen</button></form>'
        if item["status"] == "draft" else ""
    )
    cancel = (
        f'<form method="post" action="/incoming/{incoming_id}/cancel" '
        f'onsubmit="return confirm(\'Buchung stornieren? Der Beleg bleibt erhalten.\')">'
        f'<button class="button danger">Buchung stornieren</button></form>'
        if item["status"] in ("booked", "paid") else ""
    )
    return f"""
    <div class="actions inline-actions">
      <a class="button" target="_blank" href="/archive/{item['archive_id']}/pdf">Original-PDF öffnen</a>
      {delete}{cancel}
    </div>
    <form class="card form" method="post" action="/incoming/{incoming_id}">
      <h2>Lieferant</h2>
      <p class="muted">Bei Auswahl eines bereits bekannten Lieferanten werden Anschrift und
      E-Mail automatisch übernommen.</p>
      <div class="form-grid">
        <label><span>Unternehmen *</span><input {readonly} required list="supplier-options"
          id="supplier-company" name="supplier_company" value="{h(item['supplier_company'])}"></label>
        <datalist id="supplier-options">{supplier_options}</datalist>
        <label><span>Ansprechpartner</span><input {readonly} id="supplier-contact" name="supplier_contact" value="{h(item['supplier_contact'])}"></label>
        <label class="wide"><span>Straße</span><input {readonly} id="supplier-street" name="supplier_street" value="{h(item['supplier_street'])}"></label>
        <label><span>PLZ</span><input {readonly} id="supplier-postal-code" name="supplier_postal_code" value="{h(item['supplier_postal_code'])}"></label>
        <label><span>Ort</span><input {readonly} id="supplier-city" name="supplier_city" value="{h(item['supplier_city'])}"></label>
        <label><span>E-Mail</span><input {readonly} type="email" id="supplier-email" name="supplier_email" value="{h(item['supplier_email'])}"></label>
        <label><span>Zahlungsziel in Tagen</span><input {readonly} type="number" min="0" max="365"
          id="supplier-terms" name="supplier_terms"
          value="{h('' if item['supplier_payment_terms_days'] is None else str(item['supplier_payment_terms_days']))}"></label>
      </div>
      <h3>Buchungsdaten</h3>
      <div class="form-grid">
        <label><span>Rechnungsnummer</span><input {readonly} name="invoice_number" value="{h(item['invoice_number'])}"></label>
        <label><span>Rechnungsdatum *</span><input {readonly} required type="date" name="invoice_date" value="{h(item['invoice_date'])}"></label>
        <label><span>Fällig am</span><input {readonly} type="date" name="due_date" value="{h(item['due_date'] or payment_value)}"></label>
        <label><span>Zahlungsdatum</span><input {readonly} type="date" name="payment_date" value="{h(payment_value)}"></label>
        <label><span>Bruttobetrag in EUR *</span><input {readonly} required name="gross_amount" value="{h(gross)}"></label>
        <label><span>Betrieblicher Anteil in %</span><input {readonly} type="number" min="0" max="100" name="business_share_percent" value="{item['business_share_percent']}"></label>
        <label class="wide"><span>EÜR-Kategorie *</span><select {readonly} name="eur_category">{category_options}</select></label>
        <label class="wide"><span>Beschreibung</span><input {readonly} name="description" value="{h(item['description'])}"></label>
        <label class="wide"><span>Notizen / Prüfvermerk</span><textarea {readonly} name="notes">{h(item['notes'])}</textarea></label>
      </div>
      <p class="notice">Bei Kleinunternehmern wird grundsätzlich der Bruttobetrag als Ausgabe
      verwendet. Sonderfälle – insbesondere AfA, Bewirtung und private Anteile – müssen
      steuerlich geprüft werden.</p>
      {'<div class="form-actions"><button name="action" value="booked" class="button">Als gebucht speichern</button><button name="action" value="paid" class="button primary">Speichern und als bezahlt buchen</button></div>' if editable else ''}
    </form>
    {f'''<script>
    (() => {{
      const suppliers = {supplier_map};
      const company = document.getElementById("supplier-company");
      const fields = {{
        contact: document.getElementById("supplier-contact"),
        street: document.getElementById("supplier-street"),
        postal_code: document.getElementById("supplier-postal-code"),
        city: document.getElementById("supplier-city"),
        email: document.getElementById("supplier-email"),
        terms: document.getElementById("supplier-terms"),
      }};
      company.addEventListener("input", () => {{
        const match = suppliers[company.value.trim().toLowerCase()];
        if (!match) return;
        fields.contact.value = match.contact;
        fields.street.value = match.street;
        fields.postal_code.value = match.postal_code;
        fields.city.value = match.city;
        fields.email.value = match.email;
        fields.terms.value = match.terms;
      }});
    }})();
    </script>''' if editable else ''}"""


def euer_page(connection, year: int) -> str:
    entries = euer_entries(connection, year)
    summary = euer_summary(entries)
    years = range(date.today().year, date.today().year - 8, -1)
    options = "".join(
        f'<option value="{value}" {"selected" if value == year else ""}>{value}</option>'
        for value in years
    )
    category_rows = "".join(
        f"<tr><td>{h(category)}</td><td class='money'>{money(amount)}</td></tr>"
        for category, amount in summary["expense_categories"].items()
    ) or '<tr><td colspan="2" class="empty">Keine bezahlten Ausgaben erfasst.</td></tr>'
    entry_rows = "".join(
        f"<tr><td>{german_date(item['date'])}</td><td>{h(item['kind'])}</td>"
        f"<td>{h(item['number'] or '–')}</td><td>{h(item['party'] or '–')}</td>"
        f"<td>{h(item['category'])}</td><td class='money'>{money(item['amount_cents'] if item['kind'] == 'Einnahme' else -item['amount_cents'])}</td></tr>"
        for item in entries
    ) or '<tr><td colspan="6" class="empty">Für dieses Jahr wurden noch keine Zahlungen erfasst.</td></tr>'
    return f"""
    <form class="filter-bar" method="get" action="/reports/euer">
      <label><span>Auswertungsjahr</span><select name="year">{options}</select></label>
      <button class="button">Anzeigen</button>
      <a class="button" href="/reports/euer.csv?year={year}">CSV exportieren</a>
      <a class="button primary" href="/reports/euer.pdf?year={year}">PDF-Arbeitsunterlage</a>
    </form>
    <div class="stats">
      <article><span>Betriebseinnahmen</span><strong>{money(summary['income_cents'])}</strong></article>
      <article><span>Betriebsausgaben</span><strong>{money(summary['expense_cents'])}</strong></article>
      <article><span>Vorläufiger Überschuss</span><strong>{money(summary['profit_cents'])}</strong></article>
    </div>
    <div class="card"><div class="card-head"><h2>Ausgaben nach Kategorie</h2></div>
      <div class="table-wrap"><table><thead><tr><th>Kategorie</th><th>Betrag</th></tr></thead><tbody>{category_rows}</tbody></table></div>
    </div>
    <div class="card"><div class="card-head"><h2>Zahlungsjournal</h2>
      <p class="muted">Die Zuordnung erfolgt nach Zahlungsdatum (Zufluss/Abfluss).</p></div>
      <div class="table-wrap"><table><thead><tr><th>Datum</th><th>Art</th><th>Beleg</th>
      <th>Geschäftspartner</th><th>Kategorie</th><th>Betrag</th></tr></thead><tbody>{entry_rows}</tbody></table></div>
    </div>
    <div class="alert">Arbeitsunterlage – keine direkte ELSTER-Übermittlung. AfA, Einlagen,
    Entnahmen und steuerlich beschränkte Ausgaben sind gesondert zu prüfen.</div>"""


def settings_page(settings: dict[str, str]) -> str:
    fields = [
        ("company_name", "Unternehmensname"), ("owner_name", "Inhaber"), ("street", "Straße"),
        ("postal_code", "PLZ"), ("city", "Ort"), ("country", "Land"),
        ("phone", "Telefon"), ("email", "E-Mail"),
        ("website", "Website"), ("tax_number", "Steuernummer"), ("iban", "IBAN"), ("bic", "BIC"),
        ("bank_name", "Bank"), ("payment_terms_days", "Zahlungsziel in Tagen"),
    ]
    inputs = "".join(
        f'<label><span>{h(label)}</span><input name="{h(key)}" value="{h(settings.get(key, ""))}"></label>'
        for key, label in fields
    )
    graph_status = GraphClient(settings).configured()
    logo_data_uri = company_logo_data_uri()
    logo_preview = (
        f'<img class="setup-logo-preview" src="{logo_data_uri}" alt="Aktuelles Logo">'
        if logo_data_uri else
        '<div class="setup-logo-empty">Noch kein Logo hinterlegt</div>'
    )
    checked = "checked" if settings.get("small_business_enabled", "1") == "1" else ""
    return f"""
    <div class="card">
      <div class="settings-status"><span class="status {'paid' if graph_status else 'draft'}">
      Microsoft Graph: {'eingerichtet' if graph_status else 'noch nicht vollständig'}</span></div>
      <div class="form-actions"><a class="button" href="/settings/microsoft">Microsoft-Einrichtung öffnen</a></div>
    </div>
    <form class="card form" method="post" action="/settings" enctype="multipart/form-data">
      <div class="form-grid">
      <label class="wide"><span>Unternehmenslogo</span>{logo_preview}
      <input type="file" name="logo" accept="image/png,image/jpeg,image/webp"></label>
      {inputs}
      <label class="check wide"><input type="checkbox" name="small_business_enabled" {checked}>
      <span>Kleinunternehmerregelung nach § 19 UStG verwenden</span></label>
      <label class="wide"><span>Kleinunternehmer-Hinweis</span>
      <input name="small_business_notice" value="{h(settings.get('small_business_notice'))}"></label>
      </div>
      <div class="form-actions"><button class="button primary">Einstellungen speichern</button></div>
    </form>
    <form class="card form" method="post" action="/settings/counters">
      <h3>Nummernkreise korrigieren</h3>
      <p class="muted">Zählerstand vor dem nächsten neu erzeugten Dokument dieser Art.
      Beispiel: Wird bei Rechnungen 132 eingetragen, erhält die nächste Rechnung die
      laufende Nummer 0133. Nur ändern, wenn ein Zähler durch einen Fehler
      falsch gesetzt wurde – ansonsten drohen doppelte oder übersprungene Nummern.</p>
      <div class="form-grid">
      <label><span>Letzte Rechnungs-/Gutschriftnummer</span>
      <input required type="number" min="0" name="invoice_counter"
      value="{h(settings.get('invoice_counter', '0'))}"></label>
      <label><span>Letzte Kundennummer</span>
      <input required type="number" min="0" name="customer_counter"
      value="{h(settings.get('customer_counter', '0'))}"></label>
      <label><span>Letzte Angebotsnummer</span>
      <input required type="number" min="0" name="offer_counter"
      value="{h(settings.get('offer_counter', '0'))}"></label>
      <label><span>Letzte Auftragsnummer</span>
      <input required type="number" min="0" name="order_counter"
      value="{h(settings.get('order_counter', '0'))}"></label>
      </div>
      <div class="form-actions"><button class="button primary">Zähler speichern</button></div>
    </form>"""


def microsoft_setup_page(settings: dict[str, str]) -> str:
    client_id = settings.get("graph_client_id", "")
    object_id = settings.get("graph_service_principal_object_id", "")
    sender = settings.get("graph_sender", "")
    scope_name = "Buchhaltung-Mailbox"
    assignment_name = "Buchhaltung-Mail.Send"
    tenant_id = settings.get("graph_tenant_id", "")
    graph_status = GraphClient(settings).configured()
    missing = []
    if not tenant_id:
        missing.append("Tenant-ID")
    if not client_id:
        missing.append("Client-ID")
    if not object_id:
        missing.append("Dienstprinzipal-Objekt-ID")
    if not sender:
        missing.append("Absenderadresse")
    if not Path(settings.get("graph_certificate_path", "")).is_file():
        missing.append("Zertifikat")
    if not Path(settings.get("graph_private_key_path", "")).is_file():
        missing.append("privater Schlüssel")
    status_text = "vollständig eingerichtet" if not missing else "fehlt noch: " + ", ".join(missing)
    commands = f"""Connect-ExchangeOnline
New-ServicePrincipal -AppId "{client_id or '<CLIENT-ID>'}" -ObjectId "{object_id or '<DIENSTPRINZIPAL-OBJEKT-ID>'}" -DisplayName "Buchhaltung"
New-ManagementScope -Name "{scope_name}" -RecipientRestrictionFilter "PrimarySmtpAddress -eq '{sender or '<ABSENDER-POSTFACH>'}'"
New-ManagementRoleAssignment -Name "{assignment_name}" -Role "Application Mail.Send" -App "{object_id or '<DIENSTPRINZIPAL-OBJEKT-ID>'}" -CustomResourceScope "{scope_name}"
Test-ServicePrincipalAuthorization -Identity "{object_id or '<DIENSTPRINZIPAL-OBJEKT-ID>'}" -Resource "{sender or '<ABSENDER-POSTFACH>'}" | Format-Table"""
    app_registration_url = (
        f"https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/"
        f"ApplicationMenuBlade/Overview/appId/{urllib.parse.quote(client_id)}"
        if client_id else "https://entra.microsoft.com"
    )
    enterprise_apps_url = "https://entra.microsoft.com"
    fields = [
        ("graph_tenant_id", "Microsoft Tenant-ID"), ("graph_client_id", "Microsoft Client-ID"),
        ("graph_service_principal_object_id", "Dienstprinzipal-Objekt-ID"),
        ("graph_sender", "Absenderadresse"),
    ]
    inputs = "".join(
        f'<label><span>{h(label)}</span><input name="{h(key)}" value="{h(settings.get(key, ""))}"></label>'
        for key, label in fields
    )
    has_certificate = Path(settings.get("graph_certificate_path", "")).is_file()
    generate_confirm = (
        "Neues Zertifikat erstellen? Das alte Zertifikat in Entra funktioniert danach "
        "nicht mehr und muss durch die neue Datei ersetzt werden."
        if has_certificate else
        "Zertifikat und privaten Schlüssel jetzt erstellen?"
    )
    return f"""
    <div class="card form">
      <h2>Entra und Exchange Application RBAC</h2>
      <p>Diese Variante beschränkt die Anwendung in Exchange auf genau das eingetragene
      Absenderpostfach. Die Befehle müssen mit der Rolle Exchange-Administrator ausgeführt werden.</p>
      <ol class="setup-guide">
        <li><b>Entra-App registrieren.</b>
        <a class="button" target="_blank" href="{h(app_registration_url)}">
        {'App-Registrierung öffnen' if client_id else 'App registrieren'}</a>
        Tenant-ID und Client-ID unten eintragen.</li>
        <li><b>Zertifikat erstellen und hochladen.</b> Unten „Zertifikat erstellen“
        verwenden, die heruntergeladene Datei in der App-Registrierung unter
        „Zertifikate &amp; Geheimnisse“ hochladen. Der private Schlüssel verlässt dabei
        nie dieses Gerät – es muss nichts mit z. B. OpenSSL selbst erzeugt werden.</li>
        <li><b>Dienstprinzipal-Objekt-ID ermitteln.</b>
        <a class="button" target="_blank" href="{h(enterprise_apps_url)}">Unternehmensanwendungen öffnen</a>
        dort nach der Client-ID suchen und die Objekt-ID der Anwendung übernehmen
        (nicht die Objekt-ID der App-Registrierung).</li>
        <li><b>Exchange RBAC ausführen.</b> Die folgenden Befehle in Exchange Online PowerShell verwenden.</li>
      </ol>
      <pre class="command-block">{h(commands)}</pre>
    </div>
    <form class="card form" method="post" action="/settings" enctype="multipart/form-data">
      <input type="hidden" name="return_to" value="/settings/microsoft">
      <div class="settings-status"><span class="status {'paid' if not missing else 'draft'}">
      Microsoft Graph: {h(status_text)}</span></div>
      <div class="form-grid">
      {inputs}
      </div>
      <div class="form-actions"><button class="button primary">Speichern</button></div>
    </form>
    <div class="card form">
      <h3>Zertifikat</h3>
      <p class="muted">Empfohlen: Zertifikat und privaten Schlüssel hier erstellen lassen.
      Der private Schlüssel bleibt dabei auf diesem Gerät; nur die heruntergeladene
      Zertifikatsdatei wird bei Microsoft hochgeladen.</p>
      <div class="form-actions">
      <form method="post" action="/settings/microsoft/generate-certificate"
            onsubmit="return confirm('{h(generate_confirm)}')">
        <button class="button primary">Zertifikat erstellen</button>
      </form>
      {'<a class="button" href="/settings/microsoft/certificate.pem">Zertifikat herunterladen</a>' if has_certificate else ''}
      </div>
      <details>
        <summary>Stattdessen eigenes Zertifikat hochladen</summary>
        <form class="form" method="post" action="/settings" enctype="multipart/form-data">
          <input type="hidden" name="return_to" value="/settings/microsoft">
          <div class="form-grid">
          <label><span>Öffentliches Zertifikat (PEM)</span>
          <input type="file" name="graph_certificate" accept=".pem,.crt"></label>
          <label><span>Privater Schlüssel (PEM)</span>
          <input type="file" name="graph_private_key" accept=".pem,.key"></label>
          </div>
          <p class="notice">Der private Schlüssel wird nur im persistenten App-Datenverzeichnis
          mit eingeschränkten Dateirechten gespeichert und gehört in ein verschlüsseltes Backup.</p>
          <div class="form-actions"><button class="button">Hochladen</button></div>
        </form>
      </details>
    </div>
    <div class="card">
      <p class="notice"><b>Wichtig:</b> Wenn „Application Mail.Send“ zusätzlich als
      organisationsweite Graph-Anwendungsberechtigung mit Admin-Zustimmung in Entra vergeben
      wird, wirkt diese additiv und hebt den engen Exchange-RBAC-Bereich praktisch auf.</p>
      <div class="form-actions"><a class="button" href="/settings">Zurück zu Einstellungen</a>
      {'<form method="post" action="/settings/microsoft/test"><button class="button primary">Zertifikatsanmeldung testen</button></form>' if graph_status else ''}</div>
      {f'''<form class="form-actions" method="post" action="/settings/microsoft/send-test-email">
      <input type="email" required name="test_recipient" value="{h(sender)}" placeholder="empfaenger@beispiel.de">
      <button class="button primary">Testmail senden</button>
      </form>''' if graph_status else ''}
    </div>"""


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET").upper()
    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    flash, flash_headers = get_flash(environ)

    if path.startswith("/static/"):
        target = (APP_ROOT / path.lstrip("/")).resolve()
        static_root = (APP_ROOT / "static").resolve()
        if static_root not in target.parents or not target.is_file():
            return response(start_response, "Nicht gefunden", 404)
        return response(
            start_response,
            target.read_bytes(),
            content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        )
    if method == "GET" and path == "/health":
        return response(start_response, '{"status":"ok"}', content_type="application/json")

    try:
        with DB.connect() as connection:
            current_settings = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM settings")
            }
            if current_settings.get("setup_complete") != "1":
                if method == "GET" and path == "/setup":
                    return response(start_response, setup_page(current_settings))
                if method == "POST" and path == "/setup":
                    form = parse_form(environ)
                    required = {
                        "company_name": "Unternehmensname",
                        "owner_name": "Inhaber / Geschäftsführung",
                        "street": "Straße und Hausnummer",
                        "postal_code": "PLZ",
                        "city": "Ort",
                        "email": "E-Mail",
                        "payment_terms_days": "Zahlungsziel",
                    }
                    missing = [
                        label for key, label in required.items()
                        if not str(form.get(key, "")).strip()
                    ]
                    if missing:
                        raise ValueError(
                            "Bitte folgende Pflichtfelder ausfüllen: " + ", ".join(missing)
                        )
                    try:
                        terms = int(str(form["payment_terms_days"]).strip())
                    except ValueError as exc:
                        raise ValueError("Das Zahlungsziel muss eine ganze Zahl sein.") from exc
                    if not 0 <= terms <= 365:
                        raise ValueError("Das Zahlungsziel muss zwischen 0 und 365 Tagen liegen.")
                    save_company_logo(form.get("logo"))
                    setup_keys = {
                        "company_name", "owner_name", "street", "postal_code", "city",
                        "country", "phone", "email", "website", "tax_number", "iban", "bic",
                        "bank_name", "small_business_notice", "graph_tenant_id",
                        "graph_client_id", "graph_sender",
                    }
                    values = {
                        key: str(form.get(key, "")).strip()
                        for key in setup_keys
                    }
                    values.update({
                        "setup_complete": "1",
                        "small_business_enabled":
                            "1" if form.get("small_business_enabled") == "on" else "0",
                        "payment_terms_days": str(terms),
                        "invoice_counter": normalized_counter(form.get("invoice_counter", "0")),
                        "customer_counter": normalized_counter(form.get("customer_counter", "0")),
                        "offer_counter": normalized_counter(form.get("offer_counter", "0")),
                        "order_counter": normalized_counter(form.get("order_counter", "0")),
                    })
                    if not values["graph_sender"]:
                        values["graph_sender"] = values["email"]
                    connection.executemany(
                        """
                        INSERT INTO settings(key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        values.items(),
                    )
                    Database.audit(
                        connection, "settings", None, "setup_completed",
                        ", ".join(sorted(values)),
                    )
                    return redirect(
                        start_response, "/",
                        f"{values['company_name']} Buchhaltung wurde eingerichtet.",
                    )
                return redirect(start_response, "/setup")
            if path == "/setup":
                return redirect(start_response, "/")
            if method == "GET" and path == "/":
                body, title, active = dashboard(connection), "Übersicht", "dashboard"
            elif method == "GET" and path == "/customers":
                body, title, active = customers_page(connection), "Kunden", "customers"
            elif method == "GET" and path == "/customer/new":
                body, title, active = customer_form(), "Neuer Kunde", "customers"
            elif method == "POST" and path == "/customer/new":
                form = parse_form(environ)
                settings = DB.settings()
                number = int(settings["customer_counter"]) + 1
                now = Database.now()
                cursor = connection.execute(
                    """
                    INSERT INTO customers(customer_number, company, contact_name, street, postal_code, city, email, buyer_reference, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(number), form["company"], form.get("contact_name", ""), form["street"],
                     form["postal_code"], form["city"], form.get("email", ""),
                     form.get("buyer_reference", ""), form.get("notes", ""), now, now),
                )
                connection.execute("UPDATE settings SET value=? WHERE key='customer_counter'", (str(number),))
                Database.audit(connection, "customer", cursor.lastrowid, "created", str(number))
                Database.link_archives_by_customer_number(connection)
                return redirect(start_response, "/customers", f"Kunde {form['company']} wurde angelegt.")
            elif method == "GET" and re.fullmatch(r"/customer/\d+", path):
                customer_id = int(path.split("/")[2])
                customer = connection.execute("SELECT company FROM customers WHERE id=?", (customer_id,)).fetchone()
                body, title, active = customer_detail(connection, customer_id), customer["company"], "customers"
            elif method == "GET" and re.fullmatch(r"/customer/\d+/edit", path):
                customer_id = int(path.split("/")[2])
                customer = connection.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
                if not customer:
                    raise ValueError("Kunde wurde nicht gefunden.")
                body, title, active = customer_form(customer), "Kunde bearbeiten", "customers"
            elif method == "POST" and re.fullmatch(r"/customer/\d+/edit", path):
                customer_id = int(path.split("/")[2])
                form = parse_form(environ)
                now = Database.now()
                connection.execute(
                    """
                    UPDATE customers SET company=?, contact_name=?, street=?, postal_code=?, city=?,
                    country=?, email=?, buyer_reference=?, notes=?, updated_at=? WHERE id=?
                    """,
                    (
                        form["company"].strip(), form.get("contact_name", "").strip(),
                        form["street"].strip(), form["postal_code"].strip(), form["city"].strip(),
                        form.get("country", "Deutschland").strip(), form.get("email", "").strip(),
                        form.get("buyer_reference", "").strip(), form.get("notes", "").strip(),
                        now, customer_id,
                    ),
                )
                Database.audit(connection, "customer", customer_id, "updated")
                return redirect(start_response, f"/customer/{customer_id}", "Kundendaten wurden gespeichert.")
            elif method == "GET" and re.fullmatch(r"/customer/\d+/recurring/new", path):
                customer_id = int(path.split("/")[2])
                body, title, active = recurring_form(connection, customer_id), "Dauerrechnung anlegen", "customers"
            elif method == "POST" and re.fullmatch(r"/customer/\d+/recurring/new", path):
                customer_id = int(path.split("/")[2])
                form = parse_form(environ)
                customer = connection.execute("SELECT email FROM customers WHERE id=?", (customer_id,)).fetchone()
                auto_send = 1 if form.get("auto_send") == "on" else 0
                if auto_send and (not customer or not customer["email"]):
                    raise ValueError("Für automatischen Versand fehlt die Rechnungs-E-Mail.")
                now = Database.now()
                recurring_id = connection.execute(
                    """
                    INSERT INTO recurring_invoices(customer_id, active, title, category, description,
                    service_period_template, quantity_milli, unit, unit_price_cents, billing_day,
                    auto_finalize, auto_send, created_at, updated_at)
                    VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        customer_id, form.get("title", "").strip(), form.get("category", "").strip(),
                        form["description"].strip(), form.get("service_period_template", "{monat} {jahr}").strip(),
                        quantity_milli(form.get("quantity", "1")), form.get("unit", "pauschal").strip(),
                        cents(form["unit_price"]), int(form["billing_day"]),
                        1 if form.get("auto_finalize") == "on" or auto_send else 0, auto_send, now, now,
                    ),
                ).lastrowid
                Database.audit(connection, "recurring_invoice", recurring_id, "created")
                return redirect(start_response, f"/customer/{customer_id}", "Monatliche Rechnung wurde eingerichtet.")
            elif method == "POST" and re.fullmatch(r"/recurring/\d+/toggle", path):
                recurring_id = int(path.split("/")[2])
                template = connection.execute(
                    "SELECT customer_id, active FROM recurring_invoices WHERE id=?", (recurring_id,)
                ).fetchone()
                if not template:
                    raise ValueError("Dauerrechnung wurde nicht gefunden.")
                connection.execute(
                    "UPDATE recurring_invoices SET active=?, updated_at=? WHERE id=?",
                    (0 if template["active"] else 1, Database.now(), recurring_id),
                )
                return redirect(start_response, f"/customer/{template['customer_id']}", "Status wurde geändert.")
            elif method == "POST" and re.fullmatch(r"/recurring/\d+/run", path):
                recurring_id = int(path.split("/")[2])
                template = connection.execute(
                    "SELECT customer_id FROM recurring_invoices WHERE id=?", (recurring_id,)
                ).fetchone()
                if not template:
                    raise ValueError("Dauerrechnung wurde nicht gefunden.")
                result = run_recurring_invoice(connection, recurring_id, date.today(), manual=True)
                if not result:
                    return redirect(
                        start_response, f"/customer/{template['customer_id']}",
                        "Für diesen Monat wurde bereits eine Rechnung erzeugt.", "error",
                    )
                document_id, status, error = result
                message = f"Monatsrechnung wurde erzeugt ({status})."
                if error:
                    message += f" Versandfehler: {error}"
                return redirect(
                    start_response, f"/document/{document_id}", message,
                    "error" if error else "success",
                )
            elif method == "GET" and path == "/documents":
                doc_type = query.get("type", ["invoice"])[0]
                body, title, active = documents_page(connection, doc_type), TYPE_LABELS.get(doc_type, "Rechnungen"), doc_type
            elif method == "GET" and path == "/document/new":
                doc_type = query.get("type", ["invoice"])[0]
                source_id = int(query["source"][0]) if query.get("source") else None
                customer_id = int(query["customer"][0]) if query.get("customer") else None
                body, title, active = document_form(
                    connection, doc_type, source_id, customer_id
                ), f"Neue {TYPE_LABELS.get(doc_type, 'Rechnung')}", doc_type
            elif method == "POST" and path == "/document/new":
                form = parse_form(environ)
                doc_type = form.get("document_type", "invoice")
                issue_date = form["issue_date"]
                settings = DB.settings()
                terms = int(settings["payment_terms_days"])
                due_date = (date.fromisoformat(issue_date) + timedelta(days=terms)).isoformat() if doc_type == "invoice" else None
                items = document_items_from_form(form)
                total = sum(item["total_cents"] for item in items)
                now = Database.now()
                cursor = connection.execute(
                    """
                    INSERT INTO documents(document_type, status, customer_id, issue_date, service_start, service_end,
                    due_date, payment_terms_days, title, notes, source_document_id, credit_reason,
                    total_cents, created_at, updated_at)
                    VALUES (?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (doc_type, int(form["customer_id"]), issue_date, form.get("service_start") or None,
                     form.get("service_end") or None, due_date, terms, form.get("title", ""),
                     form.get("notes", ""), int(form["source_document_id"]) if form.get("source_document_id") else None,
                     form.get("credit_reason", ""), total, now, now),
                )
                document_id = cursor.lastrowid
                replace_document_items(connection, document_id, items)
                Database.audit(connection, "document", document_id, "created", doc_type)
                return redirect(start_response, f"/document/{document_id}", "Entwurf wurde gespeichert.")
            elif method == "GET" and re.fullmatch(r"/document/\d+/edit", path):
                document_id = int(path.split("/")[2])
                document = fetch_document(connection, document_id)
                if not document:
                    raise ValueError("Dokument wurde nicht gefunden.")
                body, title, active = (
                    document_form(
                        connection, document["document_type"], document_id=document_id
                    ),
                    f"{TYPE_LABELS[document['document_type']]} bearbeiten",
                    document["document_type"],
                )
            elif method == "POST" and re.fullmatch(r"/document/\d+/edit", path):
                document_id = int(path.split("/")[2])
                document = fetch_document(connection, document_id)
                if not document or document["status"] != "draft":
                    raise ValueError("Nur Entwürfe können bearbeitet werden.")
                form = parse_form(environ)
                issue_date = form["issue_date"]
                terms = int(DB.settings()["payment_terms_days"])
                due_date = (
                    date.fromisoformat(issue_date) + timedelta(days=terms)
                ).isoformat() if document["document_type"] == "invoice" else None
                items = document_items_from_form(form)
                total = sum(item["total_cents"] for item in items)
                now = Database.now()
                connection.execute(
                    """
                    UPDATE documents SET customer_id=?, issue_date=?, service_start=?,
                    service_end=?, due_date=?, payment_terms_days=?, title=?, notes=?,
                    credit_reason=?, total_cents=?, updated_at=? WHERE id=?
                    """,
                    (
                        int(form["customer_id"]), issue_date,
                        form.get("service_start") or None,
                        form.get("service_end") or None, due_date, terms,
                        form.get("title", ""), form.get("notes", ""),
                        form.get("credit_reason", ""), total, now, document_id,
                    ),
                )
                replace_document_items(connection, document_id, items)
                Database.audit(connection, "document", document_id, "updated")
                return redirect(
                    start_response, f"/document/{document_id}",
                    "Entwurf und Positionen wurden gespeichert.",
                )
            elif method == "GET" and re.fullmatch(r"/document/\d+", path):
                document_id = int(path.rsplit("/", 1)[1])
                document = fetch_document(connection, document_id)
                body, title, active = document_detail(connection, document_id), document["document_number"] or "Entwurf", document["document_type"]
            elif method == "POST" and re.fullmatch(r"/document/\d+/finalize", path):
                document_id = int(path.split("/")[2])
                number = finalize_document(connection, document_id)
                return redirect(start_response, f"/document/{document_id}", f"{number} wurde fertiggestellt.")
            elif method == "POST" and re.fullmatch(r"/document/\d+/delete", path):
                document_id = int(path.split("/")[2])
                document = fetch_document(connection, document_id)
                if not document or document["status"] != "draft":
                    raise ValueError("Nur Entwürfe dürfen endgültig gelöscht werden.")
                Database.audit(connection, "document", document_id, "draft_deleted")
                connection.execute("DELETE FROM documents WHERE id=?", (document_id,))
                return redirect(
                    start_response, f"/documents?type={document['document_type']}",
                    "Entwurf wurde gelöscht.",
                )
            elif method == "POST" and re.fullmatch(r"/document/\d+/cancel", path):
                document_id = int(path.split("/")[2])
                document = fetch_document(connection, document_id)
                if not document or document["status"] in (
                    "draft", "cancelled", "paid", "settled", "refunded", "credited"
                ):
                    raise ValueError("Dieses Dokument kann nicht storniert werden.")
                now = Database.now()
                connection.execute(
                    "UPDATE documents SET status='cancelled', cancelled_at=?, updated_at=? WHERE id=?",
                    (now, now, document_id),
                )
                Database.audit(connection, "document", document_id, "cancelled")
                return redirect(start_response, f"/document/{document_id}", "Dokument wurde storniert und bleibt im Archiv erhalten.")
            elif method == "GET" and re.fullmatch(r"/document/\d+/pdf", path):
                document_id = int(path.split("/")[2])
                document = fetch_document(connection, document_id)
                pdf = generate_pdf(connection, document_id)
                return response(
                    start_response, pdf.read_bytes(), content_type="application/pdf",
                    headers=[("Content-Disposition", f'inline; filename="{document["document_number"]}.pdf"')],
                )
            elif method == "POST" and re.fullmatch(r"/document/\d+/zugferd", path):
                document_id = int(path.split("/")[2])
                generate_zugferd(connection, document_id)
                return redirect(
                    start_response, f"/document/{document_id}",
                    "ZUGFeRD-PDF und XML wurden erzeugt und gegen das XML-Schema validiert.",
                )
            elif method == "GET" and re.fullmatch(r"/document/\d+/zugferd\.pdf", path):
                document_id = int(path.split("/")[2])
                target = electronic_invoice_path(connection, document_id, "pdf")
                return response(
                    start_response, target.read_bytes(), content_type="application/pdf",
                    headers=[("Content-Disposition", f'inline; filename="{target.name}"')],
                )
            elif method == "GET" and re.fullmatch(r"/document/\d+/zugferd\.xml", path):
                document_id = int(path.split("/")[2])
                target = electronic_invoice_path(connection, document_id, "xml")
                return response(
                    start_response, target.read_bytes(),
                    content_type="application/xml; charset=utf-8",
                    headers=[("Content-Disposition", f'inline; filename="{target.name}"')],
                )
            elif method == "POST" and re.fullmatch(r"/document/\d+/paid", path):
                document_id = int(path.split("/")[2])
                document = fetch_document(connection, document_id)
                if (
                    not document or document["document_type"] != "invoice"
                    or document["status"] not in ("final", "sent")
                ):
                    raise ValueError("Diese Rechnung kann nicht als bezahlt verbucht werden.")
                form = parse_form(environ)
                payment_date = str(form.get("payment_date", "")).strip()
                try:
                    date.fromisoformat(payment_date)
                except ValueError as exc:
                    raise ValueError("Bitte ein gültiges Zahlungsdatum angeben.") from exc
                now = Database.now()
                paid_at = f"{payment_date}T12:00:00"
                connection.execute(
                    "UPDATE documents SET status='paid', paid_at=?, updated_at=? WHERE id=?",
                    (paid_at, now, document_id),
                )
                Database.audit(connection, "document", document_id, "paid", payment_date)
                return redirect(start_response, f"/document/{document_id}", "Zahlung wurde verbucht.")
            elif method == "POST" and re.fullmatch(r"/document/\d+/credit-settle", path):
                document_id = int(path.split("/")[2])
                document = fetch_document(connection, document_id)
                if (
                    not document or document["document_type"] != "credit"
                    or document["status"] not in ("final", "sent")
                ):
                    raise ValueError("Diese Gutschrift kann nicht verbucht werden.")
                form = parse_form(environ)
                settlement_type = str(form.get("settlement_type", ""))
                if settlement_type not in ("refund", "offset"):
                    raise ValueError("Bitte eine gültige Verbuchungsart auswählen.")
                settlement_date = str(form.get("settlement_date", "")).strip()
                try:
                    date.fromisoformat(settlement_date)
                except ValueError as exc:
                    raise ValueError("Bitte ein gültiges Datum angeben.") from exc
                status = "refunded" if settlement_type == "refund" else "settled"
                now = Database.now()
                connection.execute(
                    """
                    UPDATE documents SET status=?, settlement_type=?, settled_at=?,
                    paid_at=?, updated_at=? WHERE id=?
                    """,
                    (
                        status, settlement_type, f"{settlement_date}T12:00:00",
                        f"{settlement_date}T12:00:00" if settlement_type == "refund" else None,
                        now, document_id,
                    ),
                )
                if settlement_type == "offset" and document.get("source_document_id"):
                    source = fetch_document(connection, document["source_document_id"])
                    credited_total = connection.execute(
                        """
                        SELECT COALESCE(sum(total_cents),0) FROM documents
                        WHERE document_type='credit' AND source_document_id=?
                          AND status='settled'
                        """,
                        (source["id"],),
                    ).fetchone()[0]
                    if (
                        source["status"] in ("final", "sent")
                        and credited_total >= source["total_cents"]
                    ):
                        connection.execute(
                            "UPDATE documents SET status='credited', updated_at=? WHERE id=?",
                            (now, source["id"]),
                        )
                Database.audit(
                    connection, "document", document_id, f"credit_{settlement_type}",
                    settlement_date,
                )
                message = (
                    "Auszahlung der Gutschrift wurde verbucht."
                    if settlement_type == "refund"
                    else "Gutschrift wurde mit der Rechnung verrechnet."
                )
                return redirect(start_response, f"/document/{document_id}", message)
            elif method == "POST" and re.fullmatch(r"/document/\d+/convert", path):
                source_id = int(path.split("/")[2])
                target = query.get("to", [""])[0]
                if target not in ("order", "invoice"):
                    raise ValueError("Ungültiger Dokumenttyp.")
                return redirect(start_response, f"/document/new?type={target}&source={source_id}")
            elif method == "POST" and re.fullmatch(r"/document/\d+/send", path):
                document_id = int(path.split("/")[2])
                send_document_email(connection, document_id)
                return redirect(start_response, f"/document/{document_id}", "E-Mail wurde über Microsoft Graph versendet.")
            elif method == "GET" and path == "/reminders":
                body, title, active = (
                    reminders_page(connection),
                    "Zahlungserinnerungen",
                    "reminders",
                )
            elif method == "GET" and re.fullmatch(r"/document/\d+/reminder", path):
                document_id = int(path.split("/")[2])
                body, title, active = (
                    reminder_form(connection, document_id),
                    "Zahlungserinnerung prüfen",
                    "reminders",
                )
            elif method == "POST" and re.fullmatch(r"/document/\d+/reminder", path):
                document_id = int(path.split("/")[2])
                send_payment_reminder(connection, document_id, parse_form(environ))
                return redirect(
                    start_response, f"/document/{document_id}",
                    "Zahlungserinnerung wurde über Microsoft Graph versendet.",
                )
            elif method == "GET" and path == "/archive":
                filters = archive_filters_from_query(query)
                body, title, active = (
                    archive_page(connection, filters),
                    "Dokumentenarchiv",
                    "archive",
                )
            elif method == "GET" and re.fullmatch(r"/archive/\d+", path):
                archive_id = int(path.split("/")[2])
                filters = archive_filters_from_query(query)
                body, title, active = (
                    archive_detail(connection, archive_id, filters),
                    "PDF-Import prüfen",
                    "archive",
                )
            elif method == "GET" and re.fullmatch(r"/archive/\d+/pdf", path):
                archive_id = int(path.split("/")[2])
                item = connection.execute(
                    "SELECT original_filename, stored_filename FROM archive_files WHERE id=?",
                    (archive_id,),
                ).fetchone()
                if not item:
                    raise ValueError("Archivdatei wurde nicht gefunden.")
                archive_root = (DATA_DIR / "archive").resolve()
                target = (archive_root / item["stored_filename"]).resolve()
                if target.parent != archive_root or not target.is_file():
                    raise ValueError("Die archivierte Originaldatei wurde nicht gefunden.")
                display_name = re.sub(r"[^A-Za-z0-9._-]", "_", item["original_filename"])
                return response(
                    start_response, target.read_bytes(), content_type="application/pdf",
                    headers=[("Content-Disposition", f'inline; filename="{display_name}"')],
                )
            elif method == "POST" and path == "/archive/upload":
                form = parse_form(environ)
                uploads = uploaded_files(form.get("pdf"))
                if not uploads:
                    raise ValueError("Bitte mindestens eine PDF-Datei auswählen.")
                if len(uploads) > 50:
                    raise ValueError("Pro Import können höchstens 50 PDF-Dateien verarbeitet werden.")
                if sum(len(upload.data) for upload in uploads) > 200 * 1024 * 1024:
                    raise ValueError("Der gesamte Import darf höchstens 200 MB groß sein.")
                direction = form.get("document_direction", "outgoing")
                if direction not in ("outgoing", "incoming"):
                    raise ValueError("Ungültige Belegart.")
                created_ids, file_duplicates, document_duplicates, invalid = [], 0, 0, 0
                duplicate_names = []
                for index, upload in enumerate(uploads):
                    raw = upload.data
                    if len(raw) > 20 * 1024 * 1024 or not raw.startswith(b"%PDF-"):
                        invalid += 1
                        continue
                    digest = hashlib.sha256(raw).hexdigest()
                    existing = connection.execute(
                        "SELECT id FROM archive_files WHERE sha256=? ORDER BY id LIMIT 1", (digest,)
                    ).fetchone()
                    if existing:
                        file_duplicates += 1
                        duplicate_names.append(Path(upload.filename).name)
                        continue
                    result = analyze_invoice_pdf(raw, upload.filename, DB.settings(), direction)
                    if len(uploads) == 1:
                        if form.get("document_number"):
                            result["invoice_number"] = str(form["document_number"]).strip()
                        if form.get("issue_date"):
                            result["issue_date"] = str(form["issue_date"])
                        if form.get("amount"):
                            result["amount_cents"] = cents(str(form["amount"]))
                    semantic_duplicate = Database.find_semantic_archive_duplicate(
                        connection,
                        direction,
                        result["invoice_number"],
                        result["customer_name"],
                        result["issue_date"],
                        result["amount_cents"],
                    )
                    if semantic_duplicate:
                        document_duplicates += 1
                        duplicate_names.append(Path(upload.filename).name)
                        Database.audit(
                            connection,
                            "archive_import",
                            None,
                            "semantic_duplicate_skipped",
                            f"{direction}: {upload.filename}; "
                            f"{semantic_duplicate['source']}:{semantic_duplicate['id']}",
                        )
                        continue
                    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(upload.filename).name)
                    stored_name = (
                        f"{datetime.now():%Y%m%d%H%M%S%f}-{index:02d}-"
                        f"{digest[:12]}-{safe_name}"
                    )
                    target = DATA_DIR / "archive" / stored_name
                    target.write_bytes(raw)
                    try:
                        archive_id = connection.execute(
                            """
                            INSERT INTO archive_files(
                                original_filename, stored_filename, sha256, mime_type,
                                file_size, uploaded_at, document_direction
                            ) VALUES (?, ?, ?, 'application/pdf', ?, ?, ?)
                            """,
                            (
                                Path(upload.filename).name, stored_name, digest, len(raw),
                                Database.now(), direction,
                            ),
                        ).lastrowid
                    except Exception:
                        target.unlink(missing_ok=True)
                        raise
                    update_archive_analysis(connection, archive_id, result)
                    Database.audit(
                        connection, "archive", archive_id, "uploaded_and_analyzed",
                        f"{direction}: {upload.filename}",
                    )
                    created_ids.append(archive_id)
                duplicate_total = file_duplicates + document_duplicates
                duplicate_detail = (
                    " Betroffen: "
                    + ", ".join(duplicate_names[:5])
                    + (" …" if len(duplicate_names) > 5 else "")
                    if duplicate_names else ""
                )
                if not created_ids:
                    raise ValueError(
                        f"Keine neue PDF importiert ({file_duplicates} identische Dateien, "
                        f"{document_duplicates} bereits vorhandene Rechnungen, "
                        f"{invalid} ungültige/zu große Dateien).{duplicate_detail}"
                    )
                if len(created_ids) == 1 and len(uploads) == 1:
                    return redirect(
                        start_response, f"/archive/{created_ids[0]}",
                        "PDF wurde archiviert und analysiert.",
                    )
                return redirect(
                    start_response, "/archive",
                    f"Massenimport abgeschlossen: {len(created_ids)} importiert, "
                    f"{duplicate_total} Dubletten übersprungen "
                    f"({file_duplicates} identische Dateien, "
                    f"{document_duplicates} gleiche Rechnungen), "
                    f"{invalid} ungültig/zu groß.{duplicate_detail}",
                    "error" if invalid else "success",
                )
            elif method == "POST" and re.fullmatch(r"/archive/\d+/analyze", path):
                archive_id = int(path.split("/")[2])
                item = connection.execute(
                    "SELECT original_filename, stored_filename, document_direction "
                    "FROM archive_files WHERE id=?",
                    (archive_id,),
                ).fetchone()
                if not item:
                    raise ValueError("Archivdatei wurde nicht gefunden.")
                target = DATA_DIR / "archive" / item["stored_filename"]
                result = analyze_invoice_pdf(
                    target.read_bytes(), item["original_filename"], DB.settings(),
                    item["document_direction"],
                )
                update_archive_analysis(connection, archive_id, result)
                Database.audit(connection, "archive", archive_id, "reanalyzed")
                message = "PDF wurde erneut analysiert." if not result["error"] else result["error"]
                return redirect(
                    start_response, f"/archive/{archive_id}", message,
                    "error" if result["error"] else "success",
                )
            elif method == "POST" and re.fullmatch(r"/archive/\d+/metadata", path):
                archive_id = int(path.split("/")[2])
                filters = archive_filters_from_query(query)
                filter_suffix = archive_filter_suffix(filters)
                form = parse_form(environ)
                connection.execute(
                    """
                    UPDATE archive_files SET detected_invoice_number=?, detected_issue_date=?,
                    detected_amount_cents=?, detected_customer_name=?, detected_street=?,
                    detected_postal_code=?, detected_city=?, detected_customer_number=? WHERE id=?
                    """,
                    (
                        form.get("invoice_number", "").strip(), form.get("issue_date", ""),
                        cents(form["amount"]) if form.get("amount") else None,
                        form.get("customer_name", "").strip(), form.get("street", "").strip(),
                        form.get("postal_code", "").strip(), form.get("city", "").strip(),
                        form.get("customer_number", "").strip(),
                        archive_id,
                    ),
                )
                Database.audit(connection, "archive", archive_id, "metadata_corrected")
                linked = Database.link_archives_by_customer_number(connection, archive_id)
                message = (
                    "Korrekturen wurden gespeichert und der Beleg wurde über die "
                    "Kundennummer mit dem bestehenden Kunden verknüpft."
                    if linked else "Korrekturen wurden gespeichert."
                )
                if form.get("continue") == "1":
                    connection.execute(
                        "UPDATE archive_files SET reviewed_at=? WHERE id=?",
                        (Database.now(), archive_id),
                    )
                    Database.audit(connection, "archive", archive_id, "reviewed")
                    next_id = Database.next_unreviewed_archive_id(
                        connection,
                        archive_id,
                        None,
                        filters["customer"],
                        filters["customer_number"],
                    )
                    if next_id:
                        return redirect(
                            start_response,
                            f"/archive/{next_id}{filter_suffix}",
                            f"{message} Nächster offener Beleg wurde geladen.",
                        )
                    return redirect(
                        start_response,
                        "/archive",
                        f"{message} Die Prüfliste ist vollständig abgearbeitet.",
                    )
                return redirect(start_response, f"/archive/{archive_id}", message)
            elif method == "POST" and re.fullmatch(r"/archive/\d+/payment", path):
                archive_id = int(path.split("/")[2])
                filters = archive_filters_from_query(query)
                filter_suffix = archive_filter_suffix(filters)
                item = connection.execute(
                    """
                    SELECT document_direction, detected_amount_cents, customer_id,
                    detected_customer_name FROM archive_files WHERE id=?
                    """,
                    (archive_id,),
                ).fetchone()
                if not item or item["document_direction"] != "outgoing":
                    raise ValueError("Zahlungen können hier nur für Ausgangsrechnungen erfasst werden.")
                form = parse_form(environ)
                mark_paid = form.get("mark_paid") == "1"
                status = (
                    "paid" if mark_paid
                    else str(form.get("accounting_status", "unbooked"))
                )
                if status not in ("unbooked", "paid", "cancelled"):
                    raise ValueError("Ungültiger Zahlungsstatus.")
                payment_date = str(form.get("payment_date", "")).strip() or None
                if status == "paid" and not payment_date:
                    raise ValueError("Bitte das tatsächliche Zahlungsdatum angeben.")
                if status == "paid" and item["detected_amount_cents"] is None:
                    raise ValueError("Bitte zuerst den Rechnungsbetrag erfassen.")
                link_prefix = ""
                if status == "paid" and not item["customer_id"] and item["detected_customer_name"]:
                    _, link_message = link_or_create_customer(connection, archive_id)
                    link_prefix = f"{link_message} "
                now = Database.now()
                connection.execute(
                    """
                    UPDATE archive_files SET accounting_status=?, payment_date=?,
                    cancelled_at=? WHERE id=?
                    """,
                    (
                        status, payment_date if status == "paid" else None,
                        now if status == "cancelled" else None, archive_id,
                    ),
                )
                Database.audit(connection, "archive", archive_id, f"accounting_{status}")
                if form.get("continue") == "1" or mark_paid:
                    connection.execute(
                        "UPDATE archive_files SET reviewed_at=? WHERE id=?",
                        (now, archive_id),
                    )
                    Database.audit(
                        connection, "archive", archive_id, "reviewed_after_accounting"
                    )
                    next_id = Database.next_unreviewed_archive_id(
                        connection,
                        archive_id,
                        None,
                        filters["customer"],
                        filters["customer_number"],
                    )
                    if next_id:
                        return redirect(
                            start_response,
                            f"/archive/{next_id}{filter_suffix}",
                            f"{link_prefix}Zahlungsstatus wurde gespeichert. "
                            "Nächster offener Beleg wurde geladen.",
                        )
                    return redirect(
                        start_response,
                        "/archive",
                        f"{link_prefix}Zahlungsstatus wurde gespeichert. "
                        "Die Prüfliste ist vollständig abgearbeitet.",
                    )
                return redirect(start_response, f"/archive/{archive_id}", f"{link_prefix}Zahlungsstatus wurde gespeichert.")
            elif method == "POST" and re.fullmatch(r"/archive/\d+/customer", path):
                archive_id = int(path.split("/")[2])
                _, message = link_or_create_customer(connection, archive_id)
                return redirect(start_response, f"/archive/{archive_id}", message)
            elif method == "POST" and re.fullmatch(r"/archive/\d+/delete", path):
                archive_id = int(path.split("/")[2])
                item = connection.execute(
                    """
                    SELECT a.*, i.id incoming_invoice_id
                    FROM archive_files a
                    LEFT JOIN incoming_invoices i ON i.archive_file_id=a.id
                    WHERE a.id=?
                    """,
                    (archive_id,),
                ).fetchone()
                if not item:
                    raise ValueError("Archivdatei wurde nicht gefunden.")
                if not archive_can_be_deleted(item):
                    raise ValueError(
                        "Der Beleg ist bereits gebucht oder einem Dokument zugeordnet "
                        "und darf deshalb nicht gelöscht werden."
                    )
                target = DATA_DIR / "archive" / item["stored_filename"]
                Database.audit(
                    connection, "archive", archive_id, "unbooked_import_deleted",
                    item["original_filename"],
                )
                connection.execute("DELETE FROM archive_files WHERE id=?", (archive_id,))
                target.unlink(missing_ok=True)
                return redirect(start_response, "/archive", "Fehlimport und PDF wurden gelöscht.")
            elif method == "POST" and re.fullmatch(r"/archive/\d+/incoming", path):
                archive_id = int(path.split("/")[2])
                item = connection.execute(
                    "SELECT * FROM archive_files WHERE id=?", (archive_id,)
                ).fetchone()
                if not item or item["document_direction"] != "incoming":
                    raise ValueError("Der Beleg ist nicht als Eingangsrechnung importiert.")
                existing = connection.execute(
                    "SELECT id FROM incoming_invoices WHERE archive_file_id=?", (archive_id,)
                ).fetchone()
                if existing:
                    return redirect(start_response, f"/incoming/{existing['id']}")
                now = Database.now()
                incoming_id = connection.execute(
                    """
                    INSERT INTO incoming_invoices(
                        archive_file_id, invoice_number, invoice_date, eur_category,
                        gross_cents, deductible_cents, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        archive_id, item["detected_invoice_number"],
                        item["detected_issue_date"] or date.today().isoformat(),
                        "Fremdleistungen",
                        item["detected_amount_cents"] or 0,
                        item["detected_amount_cents"] or 0,
                        now, now,
                    ),
                ).lastrowid
                connection.execute(
                    "UPDATE archive_files SET reviewed_at=COALESCE(reviewed_at,?) WHERE id=?",
                    (now, archive_id),
                )
                Database.audit(connection, "incoming_invoice", incoming_id, "draft_created_from_archive")
                return redirect(
                    start_response, f"/incoming/{incoming_id}",
                    "Eingangsrechnung wurde als Entwurf angelegt. Bitte Daten prüfen.",
                )
            elif method == "GET" and path == "/incoming":
                body, title, active = incoming_page(connection), "Eingangsrechnungen", "incoming"
            elif method == "GET" and re.fullmatch(r"/incoming/\d+", path):
                incoming_id = int(path.split("/")[2])
                body, title, active = incoming_detail(connection, incoming_id), "Eingangsrechnung prüfen", "incoming"
            elif method == "POST" and re.fullmatch(r"/incoming/\d+", path):
                incoming_id = int(path.split("/")[2])
                item = connection.execute(
                    "SELECT * FROM incoming_invoices WHERE id=?", (incoming_id,)
                ).fetchone()
                if not item or item["status"] not in ("draft", "booked"):
                    raise ValueError("Diese Eingangsrechnung kann nicht mehr geändert werden.")
                form = parse_form(environ)
                company = str(form.get("supplier_company", "")).strip()
                if not company:
                    raise ValueError("Bitte den Lieferanten angeben.")
                supplier = connection.execute(
                    "SELECT id FROM suppliers WHERE lower(trim(company))=lower(trim(?)) ORDER BY id LIMIT 1",
                    (company,),
                ).fetchone()
                now = Database.now()
                supplier_terms_raw = str(form.get("supplier_terms", "")).strip()
                supplier_terms = int(supplier_terms_raw) if supplier_terms_raw else None
                supplier_values = (
                    company, str(form.get("supplier_contact", "")).strip(),
                    str(form.get("supplier_street", "")).strip(),
                    str(form.get("supplier_postal_code", "")).strip(),
                    str(form.get("supplier_city", "")).strip(),
                    str(form.get("supplier_email", "")).strip(), supplier_terms, now,
                )
                if supplier:
                    supplier_id = supplier["id"]
                    connection.execute(
                        """
                        UPDATE suppliers SET company=?, contact_name=?, street=?, postal_code=?,
                        city=?, email=?, payment_terms_days=?, updated_at=? WHERE id=?
                        """,
                        (*supplier_values, supplier_id),
                    )
                else:
                    supplier_id = connection.execute(
                        """
                        INSERT INTO suppliers(
                            company, contact_name, street, postal_code, city, email,
                            payment_terms_days, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (*supplier_values[:-1], now, now),
                    ).lastrowid
                action = str(form.get("action", "booked"))
                if action not in ("booked", "paid"):
                    raise ValueError("Ungültiger Buchungsstatus.")
                payment_date = str(form.get("payment_date", "")).strip() or None
                if action == "paid" and not payment_date:
                    raise ValueError("Für eine bezahlte Eingangsrechnung ist das Zahlungsdatum erforderlich.")
                gross_cents = cents(str(form.get("gross_amount", "0")))
                share = max(0, min(100, int(str(form.get("business_share_percent", "100")))))
                deductible = int(
                    (Decimal(gross_cents) * Decimal(share) / 100).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                connection.execute(
                    """
                    UPDATE incoming_invoices SET supplier_id=?, invoice_number=?,
                    invoice_date=?, due_date=?, payment_date=?, status=?, description=?,
                    eur_category=?, gross_cents=?, business_share_percent=?,
                    deductible_cents=?, notes=?, booked_at=COALESCE(booked_at,?),
                    updated_at=? WHERE id=?
                    """,
                    (
                        supplier_id, str(form.get("invoice_number", "")).strip(),
                        str(form.get("invoice_date", "")), str(form.get("due_date", "")) or None,
                        payment_date, action, str(form.get("description", "")).strip(),
                        str(form.get("eur_category", "Fremdleistungen")),
                        gross_cents, share, deductible, str(form.get("notes", "")).strip(),
                        now, now, incoming_id,
                    ),
                )
                connection.execute(
                    "UPDATE archive_files SET accounting_status=? WHERE id=?",
                    (action, item["archive_file_id"]),
                )
                Database.audit(connection, "incoming_invoice", incoming_id, action)
                next_id = Database.next_unreviewed_archive_id(
                    connection, item["archive_file_id"], "incoming"
                )
                if next_id:
                    return redirect(
                        start_response, f"/archive/{next_id}",
                        "Eingangsrechnung wurde gespeichert. Nächster offener Beleg wurde geladen.",
                    )
                return redirect(
                    start_response, "/incoming",
                    "Eingangsrechnung wurde gespeichert. Die Prüfliste ist vollständig abgearbeitet.",
                )
            elif method == "POST" and re.fullmatch(r"/incoming/\d+/delete", path):
                incoming_id = int(path.split("/")[2])
                item = connection.execute(
                    """
                    SELECT i.status, i.archive_file_id, a.stored_filename
                    FROM incoming_invoices i JOIN archive_files a ON a.id=i.archive_file_id
                    WHERE i.id=?
                    """,
                    (incoming_id,),
                ).fetchone()
                if not item or item["status"] != "draft":
                    raise ValueError("Nur ein ungebuchter Entwurf darf gelöscht werden.")
                target = DATA_DIR / "archive" / item["stored_filename"]
                Database.audit(connection, "incoming_invoice", incoming_id, "draft_and_pdf_deleted")
                connection.execute("DELETE FROM incoming_invoices WHERE id=?", (incoming_id,))
                connection.execute("DELETE FROM archive_files WHERE id=?", (item["archive_file_id"],))
                target.unlink(missing_ok=True)
                return redirect(start_response, "/incoming", "Entwurf und PDF wurden gelöscht.")
            elif method == "POST" and re.fullmatch(r"/incoming/\d+/cancel", path):
                incoming_id = int(path.split("/")[2])
                item = connection.execute(
                    "SELECT status, archive_file_id FROM incoming_invoices WHERE id=?",
                    (incoming_id,),
                ).fetchone()
                if not item or item["status"] not in ("booked", "paid"):
                    raise ValueError("Diese Eingangsrechnung kann nicht storniert werden.")
                now = Database.now()
                connection.execute(
                    "UPDATE incoming_invoices SET status='cancelled', cancelled_at=?, updated_at=? WHERE id=?",
                    (now, now, incoming_id),
                )
                connection.execute(
                    "UPDATE archive_files SET accounting_status='cancelled', cancelled_at=? WHERE id=?",
                    (now, item["archive_file_id"]),
                )
                Database.audit(connection, "incoming_invoice", incoming_id, "cancelled")
                return redirect(start_response, f"/incoming/{incoming_id}", "Buchung wurde storniert; der Beleg bleibt erhalten.")
            elif method == "GET" and path == "/reports/euer":
                try:
                    year = int(query.get("year", [str(date.today().year)])[0])
                except ValueError as exc:
                    raise ValueError("Ungültiges Auswertungsjahr.") from exc
                body, title, active = euer_page(connection, year), f"EÜR {year}", "reports"
            elif method == "GET" and path == "/reports/euer.csv":
                year = int(query.get("year", [str(date.today().year)])[0])
                raw = create_euer_csv(euer_entries(connection, year))
                return response(
                    start_response, raw, content_type="text/csv; charset=utf-8",
                    headers=[("Content-Disposition", f'attachment; filename="EÜR-{year}.csv"')],
                )
            elif method == "GET" and path == "/reports/euer.pdf":
                year = int(query.get("year", [str(date.today().year)])[0])
                target = DATA_DIR / "reports" / f"euer-{year}.pdf"
                create_euer_pdf(target, year, euer_entries(connection, year), DB.settings())
                return response(
                    start_response, target.read_bytes(), content_type="application/pdf",
                    headers=[("Content-Disposition", f'inline; filename="EÜR-{year}.pdf"')],
                )
            elif method == "GET" and path == "/settings":
                body, title, active = settings_page(DB.settings()), "Einstellungen", "settings"
            elif method == "GET" and path == "/settings/microsoft":
                body, title, active = (
                    microsoft_setup_page(DB.settings()),
                    "Microsoft-Einrichtung",
                    "settings",
                )
            elif method == "POST" and path == "/settings/microsoft/test":
                GraphClient(DB.settings()).test_authentication()
                return redirect(
                    start_response, "/settings/microsoft",
                    "Zertifikatsanmeldung bei Microsoft war erfolgreich.",
                )
            elif method == "POST" and path == "/settings/microsoft/send-test-email":
                form = parse_form(environ)
                recipient = str(form.get("test_recipient", "")).strip()
                if not recipient:
                    raise ValueError("Bitte eine Empfängeradresse für die Testmail angeben.")
                GraphClient(DB.settings()).send_test_email(recipient)
                Database.audit(connection, "settings", None, "graph_test_email_sent", recipient)
                return redirect(
                    start_response, "/settings/microsoft",
                    f"Testmail wurde an {recipient} gesendet.",
                )
            elif method == "POST" and path == "/settings/microsoft/generate-certificate":
                generate_graph_certificate(DB.settings())
                Database.audit(connection, "settings", None, "graph_certificate_generated")
                return redirect(
                    start_response, "/settings/microsoft",
                    "Neues Zertifikat wurde erstellt. Jetzt herunterladen und bei "
                    "Microsoft unter „Zertifikate & Geheimnisse“ hochladen.",
                )
            elif method == "GET" and path == "/settings/microsoft/certificate.pem":
                cert_path = DATA_DIR / "graph-certificate.pem"
                if not cert_path.is_file():
                    raise ValueError("Es wurde noch kein Zertifikat erstellt.")
                return response(
                    start_response, cert_path.read_bytes(), content_type="application/x-pem-file",
                    headers=[
                        ("Content-Disposition", 'attachment; filename="buchhaltung-graph-certificate.pem"')
                    ],
                )
            elif method == "POST" and path == "/settings":
                form = parse_form(environ)
                save_company_logo(form.get("logo"))
                save_graph_credentials(
                    form.get("graph_certificate"), form.get("graph_private_key")
                )
                allowed = set(DB.settings()) - {
                    "setup_complete", "invoice_counter", "customer_counter",
                    "offer_counter", "order_counter",
                    "graph_certificate_path", "graph_private_key_path",
                }
                values = {
                    key: str(value).strip()
                    for key, value in form.items()
                    if key in allowed and not isinstance(value, UploadedFile)
                }
                values["small_business_enabled"] = (
                    "1" if form.get("small_business_enabled") == "on" else "0"
                )
                if "payment_terms_days" in values:
                    try:
                        terms = int(values["payment_terms_days"])
                    except ValueError as exc:
                        raise ValueError("Das Zahlungsziel muss eine ganze Zahl sein.") from exc
                    if not 0 <= terms <= 365:
                        raise ValueError("Das Zahlungsziel muss zwischen 0 und 365 Tagen liegen.")
                DB.update_settings(values)
                return_to = (
                    "/settings/microsoft" if form.get("return_to") == "/settings/microsoft"
                    else "/settings"
                )
                return redirect(start_response, return_to, "Einstellungen wurden gespeichert.")
            elif method == "POST" and path == "/settings/counters":
                form = parse_form(environ)
                counters = {
                    key: normalized_counter(form.get(key, "0"))
                    for key in (
                        "invoice_counter", "customer_counter",
                        "offer_counter", "order_counter",
                    )
                }
                DB.update_settings(counters)
                Database.audit(
                    connection, "settings", None, "counters_corrected",
                    ", ".join(f"{key}={value}" for key, value in counters.items()),
                )
                return redirect(start_response, "/settings", "Zähler wurden gespeichert.")
            else:
                return response(start_response, layout("Nicht gefunden", "<div class='alert error'>Seite nicht gefunden.</div>"), 404)
    except (ValueError, sqlite3.Error, RuntimeError) as exc:
        return response(
            start_response,
            layout("Fehler", f'<div class="alert error">{h(exc)}</div><a class="button" href="/">Zur Übersicht</a>'),
            400,
        )
    return response(start_response, layout(title, flash + body, active), headers=flash_headers)


if __name__ == "__main__":
    port = int(os.environ.get("HD_PORT", "8099"))
    host = os.environ.get("HD_HOST", "0.0.0.0")
    threading.Thread(target=recurring_worker, daemon=True, name="recurring-invoices").start()
    print(f"Buchhaltung läuft auf http://{host}:{port}", flush=True)
    with make_server(host, port, application, server_class=ThreadingWSGIServer, handler_class=WSGIRequestHandler) as server:
        server.serve_forever()
