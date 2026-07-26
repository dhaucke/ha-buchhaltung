from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from pdfgen import german_date, h_xml, money


EXPENSE_CATEGORIES = (
    "Wareneinsatz und Material",
    "Fremdleistungen",
    "Raumkosten",
    "Telefon und Internet",
    "Software und IT",
    "Fahrzeug- und Reisekosten",
    "Werbung",
    "Versicherungen und Beiträge",
    "Fortbildung",
    "Rechts- und Beratungskosten",
    "Bankgebühren",
    "Bewirtung",
    "Geringwertige Wirtschaftsgüter",
    "Anlagevermögen / AfA prüfen",
    "Sonstige Betriebsausgaben",
)


def euer_entries(connection, year: int) -> list[dict]:
    start, end = f"{year:04d}-01-01", f"{year:04d}-12-31"
    result: list[dict] = []

    for row in connection.execute(
        """
        SELECT d.id, d.document_number number, d.paid_at payment_timestamp,
               d.total_cents amount_cents, d.tax_cents, c.company party
        FROM documents d JOIN customers c ON c.id=d.customer_id
        WHERE d.document_type='invoice' AND d.status='paid'
          AND substr(d.paid_at,1,10) BETWEEN ? AND ?
        ORDER BY d.paid_at, d.id
        """,
        (start, end),
    ):
        result.append({
            "kind": "Einnahme",
            "source": "Ausgangsrechnung",
            "source_id": row["id"],
            "date": row["payment_timestamp"][:10],
            "number": row["number"] or "",
            "party": row["party"],
            "category": "Betriebseinnahmen",
            "amount_cents": row["amount_cents"],
            "tax_cents": row["tax_cents"],
        })

    for row in connection.execute(
        """
        SELECT d.id, d.document_number number, d.paid_at payment_timestamp,
               d.total_cents amount_cents, d.tax_cents, c.company party
        FROM documents d JOIN customers c ON c.id=d.customer_id
        WHERE d.document_type='credit' AND d.status='refunded'
          AND substr(d.paid_at,1,10) BETWEEN ? AND ?
        ORDER BY d.paid_at, d.id
        """,
        (start, end),
    ):
        result.append({
            "kind": "Einnahme",
            "source": "Ausgezahlte Gutschrift",
            "source_id": row["id"],
            "date": row["payment_timestamp"][:10],
            "number": row["number"] or "",
            "party": row["party"],
            "category": "Einnahmenkorrektur",
            "amount_cents": -row["amount_cents"],
            "tax_cents": -row["tax_cents"],
        })

    for row in connection.execute(
        """
        SELECT id, detected_invoice_number number, payment_date,
               detected_amount_cents amount_cents, detected_customer_name party
        FROM archive_files
        WHERE document_direction='outgoing' AND accounting_status='paid'
          AND document_id IS NULL AND cancelled_at IS NULL
          AND payment_date BETWEEN ? AND ?
        ORDER BY payment_date, id
        """,
        (start, end),
    ):
        result.append({
            "kind": "Einnahme",
            "source": "Archiv-Ausgangsrechnung",
            "source_id": row["id"],
            "date": row["payment_date"],
            "number": row["number"] or "",
            "party": row["party"] or "",
            "category": "Betriebseinnahmen",
            "amount_cents": row["amount_cents"] or 0,
        })

    for row in connection.execute(
        """
        SELECT i.id, i.invoice_number number, i.payment_date,
               i.deductible_cents amount_cents, i.vorsteuer_cents, COALESCE(s.company,'') party,
               i.eur_category
        FROM incoming_invoices i
        LEFT JOIN suppliers s ON s.id=i.supplier_id
        WHERE i.status='paid' AND i.payment_date BETWEEN ? AND ?
        ORDER BY i.payment_date, i.id
        """,
        (start, end),
    ):
        result.append({
            "kind": "Ausgabe",
            "source": "Eingangsrechnung",
            "source_id": row["id"],
            "date": row["payment_date"],
            "number": row["number"],
            "party": row["party"],
            "category": row["eur_category"],
            "amount_cents": row["amount_cents"],
            "vorsteuer_cents": row["vorsteuer_cents"],
        })
    return sorted(result, key=lambda item: (item["date"], item["kind"], item["source_id"]))


def euer_summary(entries: list[dict]) -> dict:
    income = sum(item["amount_cents"] for item in entries if item["kind"] == "Einnahme")
    expenses = sum(item["amount_cents"] for item in entries if item["kind"] == "Ausgabe")
    vorsteuer = sum(item.get("vorsteuer_cents", 0) for item in entries if item["kind"] == "Ausgabe")
    categories: dict[str, int] = defaultdict(int)
    for item in entries:
        if item["kind"] == "Ausgabe":
            categories[item["category"]] += item["amount_cents"]
    return {
        "income_cents": income,
        "expense_cents": expenses,
        "profit_cents": income - expenses,
        "vorsteuer_cents": vorsteuer,
        "expense_categories": dict(sorted(categories.items())),
    }


MONTH_NAMES_DE = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April", 5: "Mai", 6: "Juni",
    7: "Juli", 8: "August", 9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
}


def vat_liability_by_period(entries: list[dict], period: str = "month") -> list[dict]:
    """Group cash-basis USt/Vorsteuer into an informational Zahllast preview per
    Voranmeldezeitraum. This is not a UStVA submission – just an early-warning
    estimate of what a period's balance is likely to look like, with the
    statutory reporting deadline (10th of the following month) shown alongside."""
    groups: dict[str, dict[str, int]] = {}
    for item in entries:
        year, month = int(item["date"][:4]), int(item["date"][5:7])
        key = f"{year}-Q{(month - 1) // 3 + 1}" if period == "quarter" else f"{year}-{month:02d}"
        group = groups.setdefault(key, {"tax_collected_cents": 0, "vorsteuer_paid_cents": 0})
        if item["kind"] == "Einnahme":
            group["tax_collected_cents"] += item.get("tax_cents", 0)
        else:
            group["vorsteuer_paid_cents"] += item.get("vorsteuer_cents", 0)

    result = []
    for key in sorted(groups):
        group = groups[key]
        if period == "quarter":
            year_str, quarter_str = key.split("-Q")
            year, quarter = int(year_str), int(quarter_str)
            label = f"{quarter}. Quartal {year}"
            end_month = quarter * 3
        else:
            year_str, month_str = key.split("-")
            year, end_month = int(year_str), int(month_str)
            label = f"{MONTH_NAMES_DE[end_month]} {year}"
        due_year, due_month = (year, end_month + 1) if end_month < 12 else (year + 1, 1)
        result.append({
            "period_key": key,
            "period_label": label,
            "tax_collected_cents": group["tax_collected_cents"],
            "vorsteuer_paid_cents": group["vorsteuer_paid_cents"],
            "balance_cents": group["tax_collected_cents"] - group["vorsteuer_paid_cents"],
            "due_date": date(due_year, due_month, 10).isoformat(),
        })
    return result


def create_euer_csv(entries: list[dict]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(("Zahlungsdatum", "Art", "Belegtyp", "Belegnummer", "Geschäftspartner", "Kategorie", "Betrag EUR"))
    for item in entries:
        amount = item["amount_cents"] if item["kind"] == "Einnahme" else -item["amount_cents"]
        writer.writerow((
            item["date"], item["kind"], item["source"], item["number"], item["party"],
            item["category"], f"{amount / 100:.2f}".replace(".", ","),
        ))
    summary = euer_summary(entries)
    if summary["vorsteuer_cents"]:
        writer.writerow((
            "", "", "", "", "",
            "Gezahlte Vorsteuer (informativ \u2013 ersetzt keine Umsatzsteuervoranmeldung)",
            f"{summary['vorsteuer_cents'] / 100:.2f}".replace(".", ","),
        ))
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def create_euer_pdf(
    output_path: Path,
    year: int,
    entries: list[dict],
    settings: dict[str, str],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = euer_summary(entries)
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(
        str(output_path), pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"EÜR-Arbeitsunterlage {year}",
        author=settings.get("company_name", ""),
    )
    story = [
        Paragraph(f"Einnahmenüberschussrechnung {year}", styles["Title"]),
        Paragraph(
            f"<b>{h_xml(settings.get('company_name', ''))}</b> · "
            "Arbeitsunterlage auf Basis der erfassten Zahlungsdaten",
            styles["Normal"],
        ),
        Spacer(1, 5 * mm),
    ]
    totals = Table(
        [
            ["Betriebseinnahmen", "Betriebsausgaben", "Vorläufiger Überschuss"],
            [money(summary["income_cents"]), money(summary["expense_cents"]), money(summary["profit_cents"])],
        ],
        colWidths=[70 * mm] * 3,
    )
    totals.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#123d78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#94a3b8")),
        ("INNERGRID", (0, 0), (-1, -1), .25, colors.HexColor("#cbd5e1")),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(totals)
    if summary["vorsteuer_cents"]:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(
            f"Gezahlte Vorsteuer (informativ – ersetzt keine Umsatzsteuervoranmeldung): "
            f"<b>{money(summary['vorsteuer_cents'])}</b>",
            styles["Normal"],
        ))
    story.extend([Spacer(1, 6 * mm), Paragraph("Zahlungsjournal", styles["Heading2"])])

    rows = [["Datum", "Art", "Beleg", "Geschäftspartner", "Kategorie", "Betrag"]]
    for item in entries:
        signed = item["amount_cents"] if item["kind"] == "Einnahme" else -item["amount_cents"]
        rows.append([
            german_date(item["date"]), item["kind"], item["number"] or "–",
            Paragraph(h_xml(item["party"] or "–"), styles["Normal"]),
            Paragraph(h_xml(item["category"]), styles["Normal"]), money(signed),
        ])
    if len(rows) == 1:
        rows.append(["–", "–", "Keine Zahlungen erfasst", "", "", money(0)])
    journal = Table(rows, repeatRows=1, colWidths=[24 * mm, 22 * mm, 34 * mm, 58 * mm, 72 * mm, 32 * mm])
    journal.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f0fb")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), .25, colors.HexColor("#cbd5e1")),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([
        journal,
        Spacer(1, 6 * mm),
        Paragraph(
            "Hinweis: Diese Auswertung ist eine Arbeitsunterlage und keine elektronische "
            "Übermittlung der amtlichen Anlage EÜR. Steuerliche Sonderfälle wie AfA, "
            "Bewirtungsanteile, private Nutzungsanteile und Einlagen/Entnahmen sind zu prüfen.",
            styles["Italic"],
        ),
    ])
    document.build(story)
