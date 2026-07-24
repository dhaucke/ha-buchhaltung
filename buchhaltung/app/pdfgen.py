from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


TYPE_LABELS = {
    "offer": "Angebot",
    "order": "Auftragsbestätigung",
    "invoice": "Rechnung",
    "credit": "Gutschrift",
}


def money(cents: int) -> str:
    value = Decimal(cents) / 100
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def german_date(value: str | None) -> str:
    if not value:
        return ""
    return date.fromisoformat(value).strftime("%d.%m.%Y")


def create_document_pdf(
    output_path: Path,
    document: dict,
    customer: dict,
    items: list[dict],
    settings: dict[str, str],
    logo_path: Path | None,
):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            "Small",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#334155"),
        )
    )
    styles.add(
        ParagraphStyle(
            "Body",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#172033"),
        )
    )
    styles.add(
        ParagraphStyle(
            "DocTitle",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=26,
            textColor=colors.HexColor("#123d78"),
            spaceAfter=5 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            "Right",
            parent=styles["Body"],
            alignment=TA_RIGHT,
        )
    )
    styles.add(
        ParagraphStyle(
            "TableCell",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9,
            textColor=colors.HexColor("#172033"),
        )
    )
    styles.add(
        ParagraphStyle(
            "TableCellCenter",
            parent=styles["TableCell"],
            alignment=1,
        )
    )

    doc = BaseDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=18 * mm,
        bottomMargin=24 * mm,
        title=f"{TYPE_LABELS[document['document_type']]} {document['document_number']}",
        author=settings.get("company_name", ""),
    )

    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")

    def page(canvas, _doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#cbd5e1"))
        canvas.line(16 * mm, 19 * mm, 194 * mm, 19 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#475569"))
        footer_parts = [
            settings.get("company_name", ""),
            settings.get("owner_name", ""),
            settings.get("street", ""),
            " ".join(
                part for part in (
                    settings.get("postal_code", ""),
                    settings.get("city", ""),
                ) if part
            ),
        ]
        if settings.get("tax_number"):
            footer_parts.append(f"St.-Nr. {settings['tax_number']}")
        footer = " · ".join(part for part in footer_parts if part)
        canvas.drawString(16 * mm, 14 * mm, footer)
        bank_parts = [settings.get("bank_name", "")]
        if settings.get("iban"):
            bank_parts.append(f"IBAN {settings['iban']}")
        if settings.get("bic"):
            bank_parts.append(f"BIC {settings['bic']}")
        bank = " · ".join(part for part in bank_parts if part)
        canvas.drawString(16 * mm, 10 * mm, bank)
        canvas.drawRightString(194 * mm, 10 * mm, f"Seite {_doc.page}")
        canvas.restoreState()

    doc.addPageTemplates(PageTemplate(id="invoice", frames=[frame], onPage=page))

    sender = " · ".join(
        part for part in (
            settings.get("company_name", ""),
            settings.get("street", ""),
            " ".join(
                part for part in (
                    settings.get("postal_code", ""),
                    settings.get("city", ""),
                ) if part
            ),
        ) if part
    )
    customer_lines = [
        customer["company"],
        customer.get("contact_name", ""),
        customer["street"],
        f"{customer['postal_code']} {customer['city']}",
    ]
    customer_html = "<br/>".join(line for line in customer_lines if line)
    address_line = " ".join(
        part for part in (
            settings.get("postal_code", ""),
            settings.get("city", ""),
        ) if part
    )
    company_lines = [
        f"<b>{h_xml(settings.get('company_name', ''))}</b>",
        h_xml(settings.get("owner_name", "")),
        h_xml(settings.get("street", "")),
        h_xml(address_line),
        "",
        h_xml(settings.get("phone", "")),
        h_xml(settings.get("email", "")),
    ]
    company_html = "<br/>".join(company_lines)
    if logo_path and logo_path.is_file():
        logo_cell = Image(str(logo_path), width=68 * mm, height=29 * mm, kind="proportional")
    else:
        logo_cell = Paragraph(
            f"<b>{h_xml(settings.get('company_name', ''))}</b>",
            styles["Right"],
        )
    header = Table(
        [
            [Paragraph(f"<u>{h_xml(sender)}</u>", styles["Small"]), logo_cell],
            [Paragraph(customer_html, styles["Body"]), Paragraph(company_html, styles["Right"])],
        ],
        colWidths=[100 * mm, 78 * mm],
    )
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 4 * mm),
            ]
        )
    )

    story = [header, Spacer(1, 10 * mm)]
    story.append(Paragraph(TYPE_LABELS[document["document_type"]], styles["DocTitle"]))
    meta = [
        ["Dokumentnummer", document["document_number"] or "Entwurf"],
        ["Kundennummer", customer["customer_number"]],
        ["Datum", german_date(document["issue_date"])],
    ]
    if document.get("due_date") and document["document_type"] == "invoice":
        meta.append(["Fällig am", german_date(document["due_date"])])
    meta_table = Table(meta, colWidths=[34 * mm, 55 * mm], hAlign="LEFT")
    meta_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#172033")),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#94a3b8")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )
    story.extend([meta_table, Spacer(1, 6 * mm)])

    if document.get("title"):
        story.append(Paragraph(f"<b>{document['title']}</b>", styles["Body"]))
        story.append(Spacer(1, 3 * mm))
    if document.get("introduction"):
        story.append(Paragraph(document["introduction"], styles["Body"]))
        story.append(Spacer(1, 4 * mm))

    item_rows = [["Pos.", "Leistung / Leistungszeitraum", "Menge", "Einzelpreis", "Gesamt"]]
    for item in items:
        quantity = Decimal(item["quantity_milli"]) / 1000
        quantity_text = f"{quantity.normalize()}<br/>{h_xml(item['unit'])}"
        description = item["description"]
        if item.get("category"):
            description = f"<b>{h_xml(item['category'])}</b><br/>{h_xml(description)}"
        else:
            description = h_xml(description)
        if item.get("service_period"):
            description += (
                f'<br/><font size="7" color="#64748b">'
                f'Leistungszeitraum: {h_xml(item["service_period"])}</font>'
            )
        item_rows.append(
            [
                str(item["position"]),
                Paragraph(description, styles["TableCell"]),
                Paragraph(quantity_text, styles["TableCellCenter"]),
                money(item["unit_price_cents"]),
                money(item["total_cents"]),
            ]
        )
    item_table = Table(
        item_rows,
        repeatRows=1,
        colWidths=[9 * mm, 88 * mm, 25 * mm, 28 * mm, 28 * mm],
    )
    item_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#123d78")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 7.5),
                ("FONTSIZE", (0, 1), (0, -1), 7.5),
                ("FONTSIZE", (3, 1), (-1, -1), 7.5),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 1), (0, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ]
        )
    )
    story.extend([item_table, Spacer(1, 6 * mm)])

    totals = Table(
        [
            [
                {
                    "invoice": "Rechnungsbetrag",
                    "credit": "Gutschriftsbetrag",
                }.get(document["document_type"], "Gesamtbetrag"),
                money(document["total_cents"]),
            ],
        ],
        colWidths=[45 * mm, 35 * mm],
        hAlign="RIGHT",
    )
    totals.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e8f0fb")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#123d78")),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 11),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#7aa3d8")),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )
    story.extend([totals, Spacer(1, 8 * mm)])

    if document["document_type"] == "invoice":
        story.append(
            Paragraph(
                f"Der Gesamtbetrag ist bis zum <b>{german_date(document.get('due_date'))}</b> "
                f"ohne Abzug unter Angabe der Rechnungsnummer zu zahlen.",
                styles["Body"],
            )
        )
        story.append(Spacer(1, 4 * mm))
        if (
            settings.get("small_business_enabled", "1") == "1"
            and settings.get("small_business_notice")
        ):
            story.append(
                Paragraph(
                    f"<b>Hinweis:</b> {h_xml(settings['small_business_notice'])}",
                    styles["Body"],
                )
            )
    elif document["document_type"] == "offer":
        story.append(Paragraph("Dieses Angebot ist 30 Tage ab Ausstellungsdatum gültig.", styles["Body"]))
    elif document["document_type"] == "order":
        story.append(Paragraph("Vielen Dank für Ihren Auftrag.", styles["Body"]))
    elif document["document_type"] == "credit":
        if document.get("source_document_id"):
            story.append(
                Paragraph(
                    f"Diese Gutschrift bezieht sich auf Rechnung "
                    f"<b>{h_xml(document.get('source_document_number', ''))}</b>.",
                    styles["Body"],
                )
            )
            story.append(Spacer(1, 3 * mm))
        if document.get("credit_reason"):
            story.append(
                Paragraph(
                    f"<b>Grund:</b> {h_xml(document['credit_reason'])}",
                    styles["Body"],
                )
            )

    if document.get("notes"):
        story.extend([Spacer(1, 4 * mm), Paragraph(document["notes"], styles["Small"])])

    doc.build(story)


def h_xml(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
