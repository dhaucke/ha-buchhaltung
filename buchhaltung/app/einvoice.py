from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

MUSTANG_CLI_JAR = os.environ.get("MUSTANG_CLI_JAR", "/opt/mustang/Mustang-CLI.jar")


def decimal_text(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def country_code(value: str) -> str:
    normalized = (value or "").strip()
    aliases = {
        "deutschland": "DE",
        "germany": "DE",
        "österreich": "AT",
        "austria": "AT",
        "schweiz": "CH",
        "switzerland": "CH",
    }
    if normalized.lower() in aliases:
        return aliases[normalized.lower()]
    if len(normalized) == 2 and normalized.isalpha():
        return normalized.upper()
    raise ValueError(
        f"Für das Land „{normalized or 'leer'}“ fehlt ein zweistelliger ISO-Ländercode."
    )


def unit_code(value: str) -> str:
    normalized = (value or "").strip().lower()
    if "stund" in normalized:
        return "HUR"
    if "tag" in normalized:
        return "DAY"
    if "monat" in normalized:
        return "MON"
    return "C62"


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def build_en16931_data(
    document: dict,
    customer: dict,
    items: list[dict],
    settings: dict[str, str],
) -> dict:
    if document["document_type"] not in ("invoice", "credit"):
        raise ValueError("E-Rechnungen werden nur für Rechnungen und Gutschriften erzeugt.")
    required = {
        "Dokumentnummer": document.get("document_number"),
        "Unternehmensname": settings.get("company_name"),
        "Absenderstraße": settings.get("street"),
        "Absender-PLZ": settings.get("postal_code"),
        "Absenderort": settings.get("city"),
        "Absender-E-Mail": settings.get("email"),
        "Steuernummer": settings.get("tax_number"),
        "Kundenname": customer.get("company"),
        "Kundenstraße": customer.get("street"),
        "Kunden-PLZ": customer.get("postal_code"),
        "Kundenort": customer.get("city"),
        "Kunden-E-Mail": customer.get("email"),
    }
    missing = [label for label, value in required.items() if not str(value or "").strip()]
    if missing:
        raise ValueError(
            "Für ZUGFeRD fehlen Pflichtangaben: " + ", ".join(missing)
        )
    total = decimal_text(document["total_cents"])
    tax_enabled = settings.get("small_business_enabled", "1") != "1"
    if tax_enabled:
        rate_groups: dict[int, dict[str, int]] = {}
        for item in items:
            rate_bp = item.get("tax_rate_bp", 0)
            group = rate_groups.setdefault(rate_bp, {"net": 0, "tax": 0})
            item_tax = int(
                (Decimal(item["total_cents"]) * rate_bp / 10000).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            group["net"] += item["total_cents"]
            group["tax"] += item_tax
        net_total = sum(group["net"] for group in rate_groups.values())
        tax_total = sum(group["tax"] for group in rate_groups.values())
        bg_23 = [
            {
                "BT-116": decimal_text(amounts["net"]),
                "BT-116-1": "EUR",
                "BT-117": decimal_text(amounts["tax"]),
                "BT-117-1": "EUR",
                "BT-118": "S" if rate_bp > 0 else "Z",
                "BT-119": f"{Decimal(rate_bp) / 100:.2f}",
            }
            for rate_bp, amounts in sorted(rate_groups.items())
        ]
    else:
        notice = (
            settings.get("small_business_notice")
            or "Steuerbefreiung für Kleinunternehmer gemäß § 19 UStG."
        )
        net_total = document["total_cents"]
        tax_total = 0
        bg_23 = [{
            "BT-116": total,
            "BT-116-1": "EUR",
            "BT-117": "0.00",
            "BT-117-1": "EUR",
            "BT-118": "E",
            "BT-119": "0.00",
            "BT-120": notice,
        }]
    data = {
        "BT-1": document["document_number"],
        "BT-2": _date(document["issue_date"]),
        "BT-3": "381" if document["document_type"] == "credit" else "380",
        "BT-5": "EUR",
        "BT-10": customer.get("buyer_reference") or customer["customer_number"],
        "BT-20": (
            f"Zahlbar innerhalb {document.get('payment_terms_days', 0)} Tagen ohne Abzug."
        ),
        "BT-27": settings["company_name"],
        # BR-CO-26 requires BT-29/30/31; Kleinunternehmer usually only have a
        # Steuernummer (BT-32, schemeID FC), which doesn't satisfy that rule.
        "BT-29": {None: settings["tax_number"]},
        "BT-32": settings["tax_number"],
        "BT-34": settings["email"],
        "BT-34-1": "EM",
        "BT-35": settings["street"],
        "BT-37": settings["city"],
        "BT-38": settings["postal_code"],
        "BT-40": country_code(settings.get("country", "DE")),
        "BT-41": settings.get("owner_name", ""),
        "BT-42": settings.get("phone", ""),
        "BT-43": settings["email"],
        "BT-44": customer["company"],
        "BT-46": {None: customer["customer_number"]},
        "BT-49": customer["email"],
        "BT-49-1": "EM",
        "BT-50": customer["street"],
        "BT-52": customer["city"],
        "BT-53": customer["postal_code"],
        "BT-55": country_code(customer.get("country", "DE")),
        "BT-56": customer.get("contact_name", ""),
        "BT-58": customer["email"],
        "BG-23": bg_23,
        "BT-106": decimal_text(net_total),
        "BT-109": decimal_text(net_total),
        "BT-110": decimal_text(tax_total),
        "BT-110-1": "EUR",
        "BT-112": total,
        "BT-115": total,
        "BG-25": [],
    }
    if settings.get("vat_id"):
        data["BT-31"] = settings["vat_id"]
    if document.get("due_date") and document["document_type"] == "invoice":
        data["BT-9"] = _date(document["due_date"])
    # Always set BT-72: if only BT-73/BT-74 (service period) are present,
    # factur-x emits an empty ApplicableHeaderTradeDelivery as xsi:nil, which
    # fails XSD validation because that element isn't declared nillable.
    data["BT-72"] = _date(document["issue_date"])
    if document.get("service_start") and document.get("service_end"):
        data["BT-73"] = _date(document["service_start"])
        data["BT-74"] = _date(document["service_end"])
    if settings.get("iban"):
        data.update({
            "BT-81": "58",
            "BT-84": settings["iban"].replace(" ", ""),
            "BT-85": settings["company_name"],
        })
        if settings.get("bic"):
            data["BT-86"] = settings["bic"].replace(" ", "")
    notes = []
    if document.get("notes"):
        notes.append({"BT-22": document["notes"]})
    if document["document_type"] == "credit" and document.get("credit_reason"):
        notes.append({"BT-21": "AAI", "BT-22": document["credit_reason"]})
    if notes:
        data["BG-1"] = notes
    for item in items:
        description = item["description"]
        if item.get("service_period"):
            description += f" – Leistungszeitraum: {item['service_period']}"
        item_rate_bp = item.get("tax_rate_bp", 0) if tax_enabled else 0
        data["BG-25"].append({
            "BT-126": str(item["position"]),
            "BT-127": description,
            "BT-153": item["description"],
            "BT-146": decimal_text(item["unit_price_cents"]),
            "BT-129": str(Decimal(item["quantity_milli"]) / 1000),
            "BT-130": unit_code(item.get("unit", "")),
            "BT-151": ("S" if item_rate_bp > 0 else "Z") if tax_enabled else "E",
            "BT-152": f"{Decimal(item_rate_bp) / 100:.2f}",
            "BT-131": decimal_text(item["total_cents"]),
        })
    return data


def validate_schematron(xml: bytes) -> dict:
    """Validate EN 16931 business rules with the Mustang reference validator.

    XSD validation only checks structure, not business rules (e.g. totals
    matching the sum of line items). The Mustang-CLI jar runs both checks in
    one offline Java process, so it's used here for the business-rule half.
    Returns available=False when Java or the bundled jar aren't present
    (e.g. local dev outside the Docker image) instead of failing outright.
    """
    if not Path(MUSTANG_CLI_JAR).is_file():
        return {"available": False, "valid": None, "report": ""}
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as handle:
        handle.write(xml)
        xml_path = handle.name
    try:
        result = subprocess.run(
            ["java", "-jar", MUSTANG_CLI_JAR, "--action", "validate", "--source", xml_path],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "valid": None, "report": str(exc)}
    finally:
        Path(xml_path).unlink(missing_ok=True)
    return {
        "available": True,
        "valid": result.returncode == 0,
        "report": (result.stdout or result.stderr).strip(),
    }


def create_zugferd(
    regular_pdf: Path,
    output_pdf: Path,
    output_xml: Path,
    document: dict,
    customer: dict,
    items: list[dict],
    settings: dict[str, str],
) -> dict:
    try:
        from facturx import generate_cii_xml, generate_from_file, xml_check_xsd
    except ImportError as exc:
        raise RuntimeError("Das Python-Paket factur-x fehlt.") from exc
    data = build_en16931_data(document, customer, items, settings)
    xml = generate_cii_xml(
        data,
        level="en16931",
        check_xsd=True,
        check_schematron=False,
        prefixed_namespaces=True,
    )
    xml_check_xsd(xml, flavor="factur-x", level="en16931")
    schematron = validate_schematron(xml)
    if schematron["available"] and not schematron["valid"]:
        raise RuntimeError(
            "XML ist XSD-valide, verletzt aber EN-16931-Geschäftsregeln "
            "(Schematron-Prüfung durch Mustang):\n" + schematron["report"]
        )
    output_xml.write_bytes(xml)
    generate_from_file(
        str(regular_pdf),
        xml,
        flavor="factur-x",
        level="en16931",
        check_xsd=False,
        check_schematron=False,
        output_pdf_file=str(output_pdf),
    )
    return {
        "profile": "ZUGFeRD / Factur-X EN 16931",
        "xsd_valid": True,
        "schematron_checked": schematron["available"],
        "schematron_valid": schematron["valid"],
        "xml_size": len(xml),
    }
