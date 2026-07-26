from __future__ import annotations

import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber


AMOUNT_PATTERN = re.compile(
    r"(?<!\S)(?:Rechnungsendbetrag|Rechnungsbetrag|Gesamtbetrag|Endbetrag)"
    r"\s*:?\s*(?:EUR\s*)?(-?[\d.]+,\d{2})",
    re.IGNORECASE,
)
NUMBER_PATTERN = re.compile(
    r"(?:Rechnungs?[\s-]?(?:nr\.?|nummer)|Dokumentnummer|Beleg(?:s)?nummer)"
    r"\s*:?\s*([0-9][A-Za-z0-9./-]{1,29})",
    re.IGNORECASE,
)
STREET_SUFFIX_PATTERN = re.compile(
    r"(straße|strasse|str\.?|weg|allee|platz|ring|gasse|damm)$", re.IGNORECASE
)
ADDRESS_WORD_X_TOLERANCE = 1.5
FILENAME_NUMBER_PATTERN = re.compile(r"((?:19|20)\d{2}-\d{2}-\d{3,6})")
DATE_PATTERN = re.compile(
    r"(?:Rechnungsdatum|Datum)\s*:?\s*(\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)
CUSTOMER_NUMBER_PATTERN = re.compile(
    r"(?:Kundennr\.?|Kundennummer)\s*:?\s*([A-Za-z0-9._/-]+)",
    re.IGNORECASE,
)
POSTAL_PATTERN = re.compile(r"^(\d{5})\s+(.+)$")
TAX_RATE_PATTERN = re.compile(
    r"(?:(\d{1,2}(?:,\d{1,2})?)\s*%\s*(?:USt|MwSt|Umsatzsteuer|Mehrwertsteuer)"
    r"|(?:USt|MwSt|Umsatzsteuer|Mehrwertsteuer)\.?\s*:?\s*(\d{1,2}(?:,\d{1,2})?)\s*%)",
    re.IGNORECASE,
)


def _iso_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return ""


def _money_cents(value: str) -> int:
    return int(round(float(value.replace(".", "").replace(",", ".")) * 100))


def _tax_rate_bp(value: str) -> int | None:
    try:
        return int((Decimal(value.replace(",", ".")) * 100).to_integral_value())
    except InvalidOperation:
        return None


def _group_left_words(page) -> list[list[str]]:
    words = [
        word for word in page.extract_words(x_tolerance=ADDRESS_WORD_X_TOLERANCE)
        if word["x0"] < page.width * 0.55 and 50 < word["top"] < page.height * 0.31
    ]
    grouped: list[list[dict]] = []
    for word in sorted(words, key=lambda item: (round(item["top"]), item["x0"])):
        if not grouped or abs(grouped[-1][0]["top"] - word["top"]) > 3:
            grouped.append([word])
        else:
            grouped[-1].append(word)
    return [
        [item["text"] for item in sorted(line, key=lambda item: item["x0"])]
        for line in grouped
    ]


def _group_left_address_lines(page) -> list[str]:
    return [" ".join(tokens).strip() for tokens in _group_left_words(page)]


def _split_company_and_street(tokens: list[str]) -> tuple[str, str]:
    if tokens and re.match(r"^\d+[a-zA-Z]?\.?$", tokens[-1]):
        split_at = len(tokens) - 1
        while split_at > 0 and not STREET_SUFFIX_PATTERN.search(tokens[split_at - 1]):
            split_at -= 1
        if split_at > 0:
            return " ".join(tokens[:split_at]), " ".join(tokens[split_at:])
    return " ".join(tokens), ""


def _extract_incoming_supplier(page) -> dict[str, str]:
    lines = _group_left_words(page)
    if not lines:
        return {}
    header = lines[0]
    # Some senders separate company/street/city with commas on one line
    # (e.g. "Company GmbH, c/o Coworking Space 4, 50672 City") rather than
    # plain whitespace columns; try that first since it's unambiguous
    # when present.
    segments = [segment.strip() for segment in " ".join(header).split(",") if segment.strip()]
    if len(segments) >= 2:
        postal = POSTAL_PATTERN.match(segments[-1])
        if postal:
            return {
                "customer_name": segments[0],
                "street": " ".join(segments[1:-1]),
                "postal_code": postal.group(1),
                "city": postal.group(2),
            }
    for index, token in enumerate(header):
        if not re.fullmatch(r"\d{5}", token):
            continue
        city = " ".join(header[index + 1:])
        if not city:
            break
        remainder = header[:index]
        lower = [item.lower() for item in remainder]
        if "postfach" in lower:
            box_index = lower.index("postfach")
            company = " ".join(remainder[:box_index])
            street = " ".join(remainder[box_index:])
        else:
            company, street = _split_company_and_street(remainder)
        if company:
            return {
                "customer_name": company,
                "street": street,
                "postal_code": token,
                "city": city,
            }
        break
    return {}


def _extract_customer(
    page,
    text: str,
    sender: dict[str, str] | None = None,
) -> dict[str, str]:
    lines = _group_left_address_lines(page)
    if not lines:
        lines = [line.strip() for line in text.splitlines()[:30] if line.strip()]

    sender = sender or {}
    sender_postal_code = sender.get("postal_code", "").strip()
    sender_fragments = [
        value.casefold()
        for value in (
            sender.get("company_name", ""),
            sender.get("owner_name", ""),
            sender.get("street", ""),
        )
        if value.strip()
    ]
    for index, line in enumerate(lines):
        postal = POSTAL_PATTERN.match(line)
        if not postal or postal.group(1) == sender_postal_code or index < 2:
            continue
        street = lines[index - 1]
        candidates = lines[: index - 1]
        candidates = [
            candidate for candidate in candidates
            if not any(fragment in candidate.casefold() for fragment in sender_fragments)
            and not re.match(r"^\d{5}\s", candidate)
        ]
        company = candidates[-1] if candidates else ""
        return {
            "customer_name": company,
            "street": street,
            "postal_code": postal.group(1),
            "city": postal.group(2),
        }
    return {"customer_name": "", "street": "", "postal_code": "", "city": ""}


def analyze_invoice_pdf(
    raw: bytes,
    filename: str = "",
    sender: dict[str, str] | None = None,
    direction: str = "outgoing",
) -> dict:
    result = {
        "text": "",
        "invoice_number": "",
        "issue_date": "",
        "amount_cents": None,
        "tax_rate_bp": None,
        "customer_name": "",
        "customer_number": "",
        "street": "",
        "postal_code": "",
        "city": "",
        "error": "",
    }
    try:
        with pdfplumber.open(io.BytesIO(raw)) as document:
            texts = [(page.extract_text() or "") for page in document.pages]
            text = "\n".join(texts).strip()
            result["text"] = text
            if not text:
                result["error"] = "Das PDF enthält keinen auslesbaren Text. Für diese Datei wäre OCR erforderlich."
                return result

            normalized_text = re.sub(r"\s+", " ", text)
            number = NUMBER_PATTERN.search(normalized_text)
            if number:
                result["invoice_number"] = re.sub(r"\s*-\s*", "-", number.group(1))
            else:
                fallback = FILENAME_NUMBER_PATTERN.search(Path(filename).name)
                if fallback:
                    result["invoice_number"] = fallback.group(1)

            issue_date = DATE_PATTERN.search(normalized_text)
            if issue_date:
                result["issue_date"] = _iso_date(issue_date.group(1))

            customer_number = CUSTOMER_NUMBER_PATTERN.search(normalized_text)
            if customer_number:
                result["customer_number"] = customer_number.group(1)

            amount = AMOUNT_PATTERN.search(normalized_text)
            if amount:
                result["amount_cents"] = _money_cents(amount.group(1))

            if direction == "incoming":
                tax_rate = TAX_RATE_PATTERN.search(normalized_text)
                if tax_rate:
                    result["tax_rate_bp"] = _tax_rate_bp(tax_rate.group(1) or tax_rate.group(2))

            if document.pages:
                page = document.pages[0]
                supplier = _extract_incoming_supplier(page) if direction == "incoming" else None
                result.update(supplier or _extract_customer(page, texts[0], sender))
    except Exception as exc:
        result["error"] = f"PDF konnte nicht analysiert werden: {exc}"
    return result
