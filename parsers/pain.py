"""Parser for ISO 20022 pain.001 Customer Credit Transfer Initiation XML.

A pain.001 message (``CstmrCdtTrfInitn``) is a payment-initiation batch: one
debtor instructs one or more credit transfers to different creditors. Each
``CdtTrfTxInf`` transaction is an independent payment, so this parser returns a
*list* of UPN payment orders — one per transaction — which the caller turns into
one invoice and QR code each.

The schema is versioned (pain.001.001.03, .09, .12, …) and each version uses a
different XML namespace URI. To stay version-agnostic the parser matches on
element local names rather than fully-qualified tags, mirroring the approach in
parsers.icl_envelope.

Reference: ISO 20022 Payments Initiation message definitions
https://www.iso20022.org/iso-20022-message-definitions
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

import defusedxml.ElementTree as ET

from core.epc import EPCParseError, _validate_iban
from core.upn import UPN


class PainParseError(ValueError):
    """Raised when XML is not a supported pain.001 credit-transfer message."""


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


def _descend(parent: ET.Element | None, *names: str) -> ET.Element | None:
    node = parent
    for name in names:
        node = _child(node, name)
        if node is None:
            return None
    return node


def _text(parent: ET.Element | None, *names: str) -> str:
    node = _descend(parent, *names) if names else parent
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _parse_date(raw: str, label: str) -> date | None:
    if not raw:
        return None
    # ReqdExctnDt may be a plain date (.03) or carry a date-time; keep the date.
    raw = raw.split("T", 1)[0]
    try:
        year, month, day = raw.split("-")
        return date(int(year), int(month), int(day))
    except ValueError as exc:
        raise PainParseError(f"invalid {label}: {raw!r} (expected YYYY-MM-DD)") from exc


def _parse_amount_cents(raw: str) -> int:
    if not raw:
        return 0
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise PainParseError(f"invalid amount: {raw!r}") from exc
    return int((amount * 100).quantize(Decimal("1")))


def _parse_iban(raw: str, label: str) -> str:
    if not raw:
        return ""
    try:
        return _validate_iban(raw.replace(" ", "").upper())
    except EPCParseError as exc:
        raise PainParseError(f"invalid {label}: {raw!r}") from exc


def _postal_address(party: ET.Element | None) -> tuple[str, str]:
    """Return (street, city) from a PstlAdr, tolerating structured or free-form."""
    addr = _child(party, "PstlAdr")
    if addr is None:
        return ("", "")

    street_name = _text(addr, "StrtNm")
    building = _text(addr, "BldgNb")
    town = _text(addr, "TwnNm")
    post_code = _text(addr, "PstCd")

    street = " ".join(p for p in (street_name, building) if p)
    city = " ".join(p for p in (post_code, town) if p)

    if not street or not city:
        lines = [_text(line) for line in _children(addr, "AdrLine")]
        lines = [line for line in lines if line]
        if not street and lines:
            street = lines[0]
        if not city and len(lines) > 1:
            city = lines[1]

    return (street, city)


def _required_execution_date(payment_info: ET.Element) -> date | None:
    node = _child(payment_info, "ReqdExctnDt")
    if node is None:
        return None
    # pain.001.001.09+ nests the value under <Dt> or <DtTm>.
    raw = _text(node, "Dt") or _text(node, "DtTm") or _text(node)
    return _parse_date(raw, "requested execution date")


def _remittance(tx: ET.Element) -> tuple[str, str]:
    """Return (structured_reference, unstructured_purpose) from RmtInf."""
    rmt = _child(tx, "RmtInf")
    if rmt is None:
        return ("", "")
    reference = _text(rmt, "Strd", "CdtrRefInf", "Ref")
    purpose = _text(rmt, "Ustrd")
    return (reference, purpose)


def _transaction_to_upn(
    tx: ET.Element,
    *,
    payer_name: str,
    payer_street: str,
    payer_city: str,
    payer_iban: str,
    execution_date: date | None,
) -> UPN:
    creditor = _child(tx, "Cdtr")
    recipient_name = _text(creditor, "Nm")
    recipient_street, recipient_city = _postal_address(creditor)

    recipient_iban = _parse_iban(
        _text(tx, "CdtrAcct", "Id", "IBAN"), "creditor account"
    )
    if not recipient_iban:
        raise PainParseError("credit transfer transaction has no creditor IBAN")

    reference, purpose = _remittance(tx)
    purpose_code = _text(tx, "Purp", "Cd") or "OTHR"
    payment_purpose = purpose or _text(tx, "PmtId", "EndToEndId")

    return UPN(
        payer_iban=payer_iban[:34],
        deposit=False,
        withdrawal=False,
        payer_reference="",
        payer_name=payer_name[:33],
        payer_street=payer_street[:33],
        payer_city=payer_city[:33],
        amount_cents=_parse_amount_cents(_text(tx, "Amt", "InstdAmt")),
        payment_date=None,
        urgent=False,
        purpose_code=purpose_code,
        payment_purpose=payment_purpose[:42],
        payment_deadline=execution_date,
        recipient_iban=recipient_iban,
        recipient_reference=reference,
        recipient_name=recipient_name,
        recipient_street=recipient_street,
        recipient_city=recipient_city,
    )


def parse_pain_credit_transfers(
    xml_text: str | bytes, *, include_payer: bool = True
) -> list[UPN]:
    """Parse a pain.001 message into one UPN per credit-transfer transaction.

    Args:
        xml_text: Raw pain.001 XML.
        include_payer: When False, debtor (payer) fields are left empty.

    Returns:
        One UPN per ``CdtTrfTxInf`` across all ``PmtInf`` blocks, in document
        order.

    Raises:
        PainParseError: If the document is not a pain.001 credit-transfer
            initiation, or a transaction lacks a creditor IBAN.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise PainParseError(f"invalid XML: {exc}") from exc

    if _local_name(root.tag) != "Document":
        raise PainParseError("root element is not an ISO 20022 Document")

    initiation = _child(root, "CstmrCdtTrfInitn")
    if initiation is None:
        raise PainParseError("not a pain.001 CustomerCreditTransferInitiation")

    initiating_party_name = _text(_descend(initiation, "GrpHdr", "InitgPty"), "Nm")

    results: list[UPN] = []
    for payment_info in _children(initiation, "PmtInf"):
        execution_date = _required_execution_date(payment_info)

        payer_name = payer_street = payer_city = payer_iban = ""
        if include_payer:
            debtor = _child(payment_info, "Dbtr")
            payer_name = _text(debtor, "Nm") or initiating_party_name
            payer_street, payer_city = _postal_address(debtor)
            payer_iban = _parse_iban(
                _text(payment_info, "DbtrAcct", "Id", "IBAN"), "debtor account"
            )

        for tx in _children(payment_info, "CdtTrfTxInf"):
            results.append(
                _transaction_to_upn(
                    tx,
                    payer_name=payer_name,
                    payer_street=payer_street,
                    payer_city=payer_city,
                    payer_iban=payer_iban,
                    execution_date=execution_date,
                )
            )

    if not results:
        raise PainParseError("pain.001 message contains no credit transfers")

    return results
