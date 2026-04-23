"""Parser for Slovenian eSLOG 2.0 invoice XML into a minimal UPN model.

eSLOG 2.0 invoice XML uses the ``urn:eslog:2.00`` namespace and an
EDIFACT-inspired segment tree. This parser extracts the payment-relevant
fields needed to construct a UPN payment order:

- seller/payee name and address
- seller receiving account (RB)
- payment reference (PQ)
- payment due date (DTM code 13)
- invoice total amount with VAT (MOA code 388)
- purpose code (FTX code ALQ)

Official source used for field mapping:
https://epos.si/assets/docs/e-SLOG-2.0-08-2020-EN.zip
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET

from core.epc import EPCParseError, _validate_iban
from core.upn import UPN

NS_URI = "urn:eslog:2.00"
NS = {"e": NS_URI}


class ESlogParseError(ValueError):
    """Raised when XML cannot be parsed as a supported eSLOG 2.0 invoice."""


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _find_first(parent: ET.Element, path: str) -> str:
    return _text(parent.find(path, NS))


def _find_group(parent: ET.Element, group_tag: str, qualifier_path: str, qualifier: str) -> ET.Element | None:
    for group in parent.findall(group_tag, NS):
        if _find_first(group, qualifier_path) == qualifier:
            return group
    return None


def _parse_date(raw: str, label: str) -> date | None:
    if not raw:
        return None
    try:
        year, month, day = raw.split("-")
        return date(int(year), int(month), int(day))
    except ValueError as exc:
        raise ESlogParseError(f"invalid {label}: {raw!r} (expected YYYY-MM-DD)") from exc


def _parse_amount_cents(raw: str) -> int:
    if not raw:
        return 0
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ESlogParseError(f"invalid amount: {raw!r}") from exc
    return int((amount * 100).quantize(Decimal("1")))


def _parse_address(group: ET.Element | None) -> tuple[str, str, str]:
    if group is None:
        return ("", "", "")
    name = _find_first(group, "e:S_NAD/e:C_C080/e:D_3036")
    street = _find_first(group, "e:S_NAD/e:C_C059/e:D_3042")
    city = _find_first(group, "e:S_NAD/e:D_3164")
    return (name, street, city)


def parse_eslog_invoice(xml_text: str | bytes) -> UPN:
    """Parse an eSLOG 2.0 invoice XML document into a minimal UPN."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ESlogParseError(f"invalid XML: {exc}") from exc

    if root.tag != f"{{{NS_URI}}}Invoice":
        raise ESlogParseError("root element is not eSLOG Invoice")

    message = root.find("e:M_INVOIC", NS)
    if message is None:
        raise ESlogParseError("missing M_INVOIC")

    seller_group = _find_group(message, "e:G_SG2", "e:S_NAD/e:D_3035", "SE")
    if seller_group is None:
        raise ESlogParseError("missing seller party (SE)")

    payee_group = _find_group(message, "e:G_SG2", "e:S_NAD/e:D_3035", "PE")
    recipient_name, recipient_street, recipient_city = _parse_address(payee_group)
    if not recipient_name or not recipient_city:
        recipient_name, recipient_street, recipient_city = _parse_address(seller_group)

    recipient_iban_raw = _find_first(
        seller_group,
        "e:S_FII[e:D_3035='RB']/e:C_C078/e:D_3194",
    )
    if not recipient_iban_raw:
        raise ESlogParseError("missing payment account identifier (RB)")
    recipient_iban = _validate_iban(recipient_iban_raw.replace(" ", "").upper())

    payment_reference = ""
    for group in message.findall("e:G_SG1", NS):
        if _find_first(group, "e:S_RFF/e:C_C506/e:D_1153") == "PQ":
            payment_reference = _find_first(group, "e:S_RFF/e:C_C506/e:D_1154")
            break

    payment_deadline = None
    for group in message.findall("e:G_SG8", NS):
        if _find_first(group, "e:S_PAT/e:D_4279") != "1":
            continue
        for dtm in group.findall("e:S_DTM", NS):
            if _find_first(dtm, "e:C_C507/e:D_2005") == "13":
                payment_deadline = _parse_date(
                    _find_first(dtm, "e:C_C507/e:D_2380"),
                    "payment due date",
                )
                break
        if payment_deadline is not None:
            break

    amount_cents = 0
    for group in message.findall("e:G_SG50", NS):
        if _find_first(group, "e:S_MOA/e:C_C516/e:D_5025") == "388":
            amount_cents = _parse_amount_cents(
                _find_first(group, "e:S_MOA/e:C_C516/e:D_5004")
            )
            break

    purpose_code = _find_first(message, "e:S_FTX[e:D_4451='ALQ']/e:C_C108/e:D_4440") or "OTHR"
    payment_means_text = _find_first(message, "e:S_FTX[e:D_4451='AAT']/e:C_C108/e:D_4440")
    invoice_number = _find_first(message, "e:S_BGM/e:C_C106/e:D_1004")
    payment_purpose = (payment_means_text or invoice_number)[:42]

    try:
        recipient_iban = _validate_iban(recipient_iban)
    except EPCParseError as exc:
        raise ESlogParseError(f"invalid payment account identifier: {recipient_iban_raw!r}") from exc

    if not recipient_city:
        raise ESlogParseError("missing recipient city")

    return UPN(
        payer_iban="",
        deposit=False,
        withdrawal=False,
        payer_reference="",
        payer_name="",
        payer_street="",
        payer_city="",
        amount_cents=amount_cents,
        payment_date=None,
        urgent=False,
        purpose_code=purpose_code,
        payment_purpose=payment_purpose,
        payment_deadline=payment_deadline,
        recipient_iban=recipient_iban,
        recipient_reference=payment_reference,
        recipient_name=recipient_name,
        recipient_street=recipient_street,
        recipient_city=recipient_city,
    )
