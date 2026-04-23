"""EPC QR code (GiroCode / SEPA Credit Transfer QR) parser.

Spec: EPC069-12 "Quick Response Code – Guidelines to Enable the Data Capture
for the Initiation of a SCT" v2.1, European Payments Council.
https://www.europeanpaymentscouncil.eu/document-library/guidance-documents/
quick-response-code-guidelines-enable-data-capture-initiation

Structure: 12 LF-delimited fields, max 331 chars total.

Field overview:
  1  Service tag        "BCD" (constant)
  2  Version            "001" or "002"
  3  Character set      "1"–"8" (see CHARSETS below)
  4  Identification     "SCT" (SEPA Credit Transfer, constant)
  5  BIC                8 or 11 chars; required in v001, optional in v002
  6  Beneficiary name   max 70 chars, required
  7  Beneficiary IBAN   max 34 chars, required (SEPA IBAN)
  8  Amount             "EUR" + decimal, or empty; max EUR999999999.99
  9  Purpose code       max 4 chars, ISO 20022 purpose code, optional
  10 Structured ref     max 35 chars (ISO 11649 creditor ref); exclusive with field 11
  11 Unstructured ref   max 140 chars (free text); exclusive with field 10
  12 Originator info    max 70 chars, optional; NOT forwarded with the payment
"""
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional


SERVICE_TAG = "BCD"
IDENTIFICATION = "SCT"
VERSIONS = {"001", "002"}

CHARSETS = {
    "1": "utf-8",
    "2": "iso-8859-2",
    "3": "iso-8859-4",
    "4": "iso-8859-5",
    "5": "iso-8859-7",
    "6": "iso-8859-10",
    "7": "iso-8859-15",
}

FIELD_COUNT = 12


class EPCParseError(ValueError):
    """Raised when a string cannot be parsed as a valid EPC QR code."""


def _mod97(number: str) -> int:
    """Compute number % 97 for an arbitrarily long decimal string."""
    rem = 0
    for ch in number:
        rem = (rem * 10 + (ord(ch) - 48)) % 97
    return rem


def _validate_iban(raw: str) -> str:
    """Validate IBAN (ISO 13616) and return normalized uppercase without spaces."""
    iban = "".join(raw.split()).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{0,30}", iban):
        raise EPCParseError(f"invalid IBAN format: {raw!r}")
    if len(iban) > 34:
        raise EPCParseError(f"beneficiary IBAN too long: {len(iban)} > 34")

    rearranged = iban[4:] + iban[:4]
    digits = []
    for ch in rearranged:
        if "0" <= ch <= "9":
            digits.append(ch)
        else:
            digits.append(str(ord(ch) - 55))  # A=10 .. Z=35
    if _mod97("".join(digits)) != 1:
        raise EPCParseError(f"invalid IBAN checksum: {raw!r}")
    return iban

@dataclass(frozen=True)
class EPC:
    """Parsed EPC QR SEPA Credit Transfer payload."""

    version: str               # field 2 — "001" or "002"
    charset: str               # field 3 — encoding label (e.g. "utf-8")
    bic: str                   # field 5 — beneficiary BIC (may be empty in v002)
    beneficiary_name: str      # field 6 — required
    beneficiary_iban: str      # field 7 — required
    amount: Optional[Decimal]  # field 8 — EUR amount, or None if not specified
    purpose_code: str          # field 9 — ISO 20022 purpose code, may be empty
    structured_ref: str        # field 10 — ISO 11649 creditor reference, may be empty
    unstructured_ref: str      # field 11 — free text remittance info, may be empty
    originator_info: str       # field 12 — display-only info, not forwarded


def _parse_amount(raw: str) -> Optional[Decimal]:
    """Parse the amount field, e.g. 'EUR12.50' → Decimal('12.50'), '' → None."""
    if not raw:
        return None
    if not raw.startswith("EUR"):
        raise EPCParseError(f"amount must start with 'EUR', got {raw!r}")
    number = raw[3:]
    if not number:
        raise EPCParseError(f"amount has no value after currency: {raw!r}")
    try:
        value = Decimal(number)
    except InvalidOperation:
        raise EPCParseError(f"invalid amount value: {raw!r}")
    if value < 0:
        raise EPCParseError(f"amount must not be negative: {raw!r}")
    if value > Decimal("999999999.99"):
        raise EPCParseError(f"amount exceeds maximum EUR999999999.99: {raw!r}")
    # Max 2 decimal places
    if value != value.quantize(Decimal("0.01")):
        raise EPCParseError(f"amount has more than 2 decimal places: {raw!r}")
    return value


def parse_epc(text: str) -> EPC:
    """Parse a QR code string as an EPC SEPA Credit Transfer payload.

    Raises EPCParseError if the string is not a valid EPC QR code.
    """
    # Normalise line endings — spec uses LF, but be tolerant of CRLF
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fields = text.split("\n")

    # Canonical EPC payloads may omit trailing empty optional fields.
    if len(fields) < FIELD_COUNT:
        fields.extend([""] * (FIELD_COUNT - len(fields)))

    if fields[0] != SERVICE_TAG:
        raise EPCParseError(f"service tag must be 'BCD', got {fields[0]!r}")

    version = fields[1]
    if version not in VERSIONS:
        raise EPCParseError(f"unsupported version {version!r}; expected one of {VERSIONS}")

    charset_key = fields[2]
    if charset_key not in CHARSETS:
        raise EPCParseError(
            f"unknown character set {charset_key!r}; expected 1–{max(CHARSETS)}"
        )
    charset = CHARSETS[charset_key]

    if fields[3] != IDENTIFICATION:
        raise EPCParseError(f"identification must be 'SCT', got {fields[3]!r}")

    bic = fields[4].upper()
    if version == "001" and not bic:
        raise EPCParseError("BIC is required in version 001")
    if bic and not re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?", bic):
        raise EPCParseError(f"invalid BIC: {bic!r}")

    beneficiary_name = fields[5]
    if not beneficiary_name:
        raise EPCParseError("beneficiary name (field 6) is required")
    if len(beneficiary_name) > 70:
        raise EPCParseError(f"beneficiary name too long: {len(beneficiary_name)} > 70")

    beneficiary_iban_raw = fields[6]
    if not beneficiary_iban_raw:
        raise EPCParseError("beneficiary IBAN (field 7) is required")
    beneficiary_iban = _validate_iban(beneficiary_iban_raw)

    amount = _parse_amount(fields[7])

    purpose_code = fields[8]
    if len(purpose_code) > 4:
        raise EPCParseError(f"purpose code too long: {purpose_code!r}")

    structured_ref = fields[9]
    unstructured_ref = fields[10]
    if structured_ref and unstructured_ref:
        raise EPCParseError(
            "structured (field 10) and unstructured (field 11) remittance info "
            "are mutually exclusive"
        )
    if len(structured_ref) > 35:
        raise EPCParseError(f"structured reference too long: {len(structured_ref)} > 35")
    if len(unstructured_ref) > 140:
        raise EPCParseError(f"unstructured reference too long: {len(unstructured_ref)} > 140")

    originator_info = fields[11] if len(fields) > 11 else ""
    if len(originator_info) > 70:
        raise EPCParseError(f"originator info too long: {len(originator_info)} > 70")

    return EPC(
        version=version,
        charset=charset,
        bic=bic,
        beneficiary_name=beneficiary_name,
        beneficiary_iban=beneficiary_iban,
        amount=amount,
        purpose_code=purpose_code,
        structured_ref=structured_ref,
        unstructured_ref=unstructured_ref,
        originator_info=originator_info,
    )


def format_epc(epc: EPC) -> str:
    """Format a parsed EPC payload as a human-readable debug string."""
    amount_str = f"EUR {epc.amount:.2f}" if epc.amount is not None else "(not specified)"
    lines = [
        "=== EPC QR SEPA Credit Transfer ===",
        f"  Version:          {epc.version}  (charset: {epc.charset})",
        f"  Amount:           {amount_str}",
        f"  Purpose code:     {epc.purpose_code or '(none)'}",
        "",
        "  Beneficiary:",
        f"    Name: {epc.beneficiary_name}",
        f"    IBAN: {epc.beneficiary_iban}",
        f"    BIC:  {epc.bic or '(not provided)'}",
    ]

    if epc.structured_ref:
        lines.append(f"  Remittance (structured):   {epc.structured_ref}")
    if epc.unstructured_ref:
        lines.append(f"  Remittance (unstructured): {epc.unstructured_ref}")
    if epc.originator_info:
        lines.append(f"  Info (display only):       {epc.originator_info}")

    return "\n".join(lines)
