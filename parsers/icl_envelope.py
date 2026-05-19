"""Parser for Slovenian ICL e-račun envelope XML payment data."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import defusedxml.ElementTree as ET

from core.epc import EPCParseError, _validate_iban
from core.upn import UPN


class ICLEnvelopeParseError(ValueError):
    """Raised when XML is not a supported ICL e-račun envelope."""


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[1]
    return tag


def _child(parent: ET.Element | None, name: str) -> ET.Element | None:
    if parent is None:
        return None
    for item in parent:
        if _local_name(item.tag) == name:
            return item
    return None


def _children(parent: ET.Element | None, name: str) -> list[ET.Element]:
    if parent is None:
        return []
    return [item for item in parent if _local_name(item.tag) == name]


def _text(parent: ET.Element | None, name: str) -> str:
    item = _child(parent, name)
    if item is None or item.text is None:
        return ""
    return item.text.strip()


def _parse_date(raw: str, label: str) -> date | None:
    if not raw:
        return None
    try:
        year, month, day = raw.split("-")
        return date(int(year), int(month), int(day))
    except ValueError as exc:
        raise ICLEnvelopeParseError(f"invalid {label}: {raw!r} (expected YYYY-MM-DD)") from exc


def _parse_amount_cents(raw: str) -> int:
    if not raw:
        return 0
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ICLEnvelopeParseError(f"invalid amount: {raw!r}") from exc
    return int((amount * 100).quantize(Decimal("1")))


def _parse_iban(raw: str, label: str) -> str:
    if not raw:
        return ""
    try:
        return _validate_iban(raw.replace(" ", "").upper())
    except EPCParseError as exc:
        raise ICLEnvelopeParseError(f"invalid {label}: {raw!r}") from exc


def _party_address(party: ET.Element | None) -> tuple[str, str, str]:
    addresses = [_text_item(item) for item in _children(party, "address")]
    addresses = [item for item in addresses if item]
    street = addresses[0] if addresses else ""
    city = addresses[1] if len(addresses) > 1 else ""
    return (_text(party, "name"), street, city)


def _text_item(item: ET.Element) -> str:
    if item.text is None:
        return ""
    return item.text.strip()


def parse_icl_envelope(xml_text: str | bytes, *, include_payer: bool = True) -> UPN:
    """Parse an ICL e-račun envelope into a minimal UPN payment order."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ICLEnvelopeParseError(f"invalid XML: {exc}") from exc

    if _local_name(root.tag) != "envelope":
        raise ICLEnvelopeParseError("root element is not ICL envelope")

    payment_data = _child(root, "payment_data")
    if payment_data is None:
        raise ICLEnvelopeParseError("missing payment_data")

    creditor = _child(payment_data, "creditor")
    if creditor is None:
        raise ICLEnvelopeParseError("missing creditor")

    recipient_iban_raw = _text(creditor, "creditor_account")
    recipient_iban = _parse_iban(recipient_iban_raw, "creditor account")
    if not recipient_iban:
        raise ICLEnvelopeParseError("missing creditor account")

    recipient_name, recipient_street, recipient_city = _party_address(creditor)
    if not recipient_city:
        raise ICLEnvelopeParseError("missing creditor city")

    payer_iban = payer_name = payer_street = payer_city = ""
    if include_payer:
        debtor = _child(payment_data, "debtor")
        payer_name, payer_street, payer_city = _party_address(debtor)
        payer_iban = _parse_iban(_text(debtor, "debtor_account"), "debtor account")

    remittance = _child(payment_data, "remittance_information")
    payment_reference = _text(remittance, "creditor_structured_reference")
    payment_purpose = _text(remittance, "additional_remittance_information")

    return UPN(
        payer_iban=payer_iban[:34],
        deposit=False,
        withdrawal=False,
        payer_reference="",
        payer_name=payer_name[:33],
        payer_street=payer_street[:33],
        payer_city=payer_city[:33],
        amount_cents=_parse_amount_cents(_text(payment_data, "amount")),
        payment_date=None,
        urgent=False,
        purpose_code=_text(payment_data, "purpose") or "OTHR",
        payment_purpose=payment_purpose[:42],
        payment_deadline=_parse_date(
            _text(payment_data, "requested_execution_date"),
            "requested execution date",
        ),
        recipient_iban=recipient_iban,
        recipient_reference=payment_reference,
        recipient_name=recipient_name,
        recipient_street=recipient_street,
        recipient_city=recipient_city,
    )
