"""Slovenian UPN QR payment order parser.

Spec: ZBS Technical Standard UPN QR Form v1.1, October 2016
https://www.zbs-giz.si/wp-content/uploads/2021/10/EN_Tehnicni_standard_UPN_QR.pdf
Section 5.2: QR code record structure.

QR string structure: 20 fields separated by LF (0x0A).
Field 20 is a 3-digit checksum = sum of (len(field) + 1) for fields 1-19.
Encoding: ISO 8859-2 (ECI 000004).
"""
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional


LEADING_STYLE = "UPNQR"
FIELD_COUNT = 20  # indices 0-19


class UPNParseError(ValueError):
    """Raised when a string cannot be parsed as a valid UPN QR code."""


class UPNReferenceError(ValueError):
    """Raised when a UPN SI/RF reference is present but invalid."""


@dataclass(frozen=True)
class UPN:
    """Parsed Slovenian UPN QR payment order (section 5.2)."""

    # Payer section (fields 2-8)
    payer_iban: str            # field 2 — SI IBAN or empty
    deposit: bool              # field 3 — "X" or blank
    withdrawal: bool           # field 4 — "X" or blank
    payer_reference: str       # field 5 — SI/RF model + reference, or empty
    payer_name: str            # field 6
    payer_street: str          # field 7
    payer_city: str            # field 8

    # Transaction (fields 9-14)
    amount_cents: int          # field 9 — EUR cents (raw 11-digit string / 100)
    payment_date: Optional[date]  # field 10 — DD.MM.YYYY or empty
    urgent: bool               # field 11 — "X" or blank
    purpose_code: str          # field 12 — 4-char ISO purpose code
    payment_purpose: str       # field 13 — free text
    payment_deadline: Optional[date]  # field 14 — DD.MM.YYYY or empty

    # Recipient section (fields 15-19)
    recipient_iban: str        # field 15 — required; SEPA IBAN
    recipient_reference: str   # field 16 — SI/RF model + reference, or empty
    recipient_name: str        # field 17
    recipient_street: str      # field 18
    recipient_city: str        # field 19 — required

    @property
    def amount(self) -> Decimal:
        """Payment amount in EUR."""
        return Decimal(self.amount_cents) / 100


def _parse_date(raw: str, label: str) -> Optional[date]:
    if not raw:
        return None
    try:
        day, month, year = raw.split(".")
        return date(int(year), int(month), int(day))
    except ValueError:
        raise UPNParseError(f"invalid {label}: {raw!r} — expected DD.MM.YYYY")


def parse_upn(text: str) -> UPN:
    """Parse a QR code string as a UPN payment order.

    Raises UPNParseError if the string is not a valid UPN QR code.
    """
    fields = text.split("\n")

    if len(fields) < FIELD_COUNT:
        raise UPNParseError(f"too few fields: need {FIELD_COUNT}, got {len(fields)}")

    if fields[0] != LEADING_STYLE:
        raise UPNParseError(f"leading style must be {LEADING_STYLE!r}, got {fields[0]!r}")

    # Checksum (field 20, index 19): sum of len(field + '\n') for fields 1-19
    expected_checksum = sum(len(f) + 1 for f in fields[:19])
    checksum_raw = fields[19]
    if not re.fullmatch(r"\d{1,3}", checksum_raw):
        raise UPNParseError(f"invalid checksum field: {checksum_raw!r}")
    if int(checksum_raw) != expected_checksum:
        raise UPNParseError(
            f"checksum mismatch: field says {checksum_raw}, calculated {expected_checksum}"
        )

    if not fields[14]:
        raise UPNParseError("recipient IBAN (field 15) is required")
    if not fields[18]:
        raise UPNParseError("recipient city (field 19) is required")

    amount_raw = fields[8]
    if amount_raw and not re.fullmatch(r"\d{11}", amount_raw):
        raise UPNParseError(f"invalid amount field: {amount_raw!r}")

    return UPN(
        payer_iban=fields[1],
        deposit=fields[2] == "X",
        withdrawal=fields[3] == "X",
        payer_reference=fields[4],
        payer_name=fields[5],
        payer_street=fields[6],
        payer_city=fields[7],
        amount_cents=int(amount_raw) if amount_raw else 0,
        payment_date=_parse_date(fields[9], "payment date"),
        urgent=fields[10] == "X",
        purpose_code=fields[11],
        payment_purpose=fields[12],
        payment_deadline=_parse_date(fields[13], "payment deadline"),
        recipient_iban=fields[14],
        recipient_reference=fields[15],
        recipient_name=fields[16],
        recipient_street=fields[17],
        recipient_city=fields[18],
    )


def format_upn(upn: UPN) -> str:
    """Format a parsed UPN as a human-readable debug string."""
    lines = [
        "=== UPN QR Payment Order ===",
        f"  Amount:           EUR {upn.amount:.2f}",
        f"  Purpose code:     {upn.purpose_code or '(none)'}",
        f"  Payment purpose:  {upn.payment_purpose or '(none)'}",
        f"  Payment date:     {upn.payment_date.strftime('%d.%m.%Y') if upn.payment_date else '(not set)'}",
        f"  Payment deadline: {upn.payment_deadline.strftime('%d.%m.%Y') if upn.payment_deadline else '(not set)'}",
        f"  Urgent:           {'yes' if upn.urgent else 'no'}",
        "",
        "  Recipient:",
        f"    IBAN:      {upn.recipient_iban}",
        f"    Reference: {upn.recipient_reference or '(none)'}",
        f"    Name:      {upn.recipient_name or '(none)'}",
        f"    Street:    {upn.recipient_street or '(none)'}",
        f"    City:      {upn.recipient_city}",
    ]

    if upn.payer_iban or upn.payer_name or upn.payer_reference:
        lines += ["", "  Payer:"]
        if upn.payer_iban:
            lines.append(f"    IBAN:      {upn.payer_iban}")
        if upn.payer_reference:
            lines.append(f"    Reference: {upn.payer_reference}")
        if upn.payer_name:
            lines.append(f"    Name:      {upn.payer_name}")
        if upn.payer_street or upn.payer_city:
            addr = ", ".join(p for p in [upn.payer_street, upn.payer_city] if p)
            lines.append(f"    Address:   {addr}")

    flags = [f for f, v in [("DEPOSIT", upn.deposit), ("WITHDRAWAL", upn.withdrawal)] if v]
    if flags:
        lines.append(f"  Flags: {', '.join(flags)}")

    return "\n".join(lines)


# ── Reference validation (SI / RF models) ─────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def _mod97(number: str) -> int:
    """Compute number % 97 for an arbitrarily long decimal string."""
    rem = 0
    for ch in number:
        rem = (rem * 10 + (ord(ch) - 48)) % 97
    return rem


def _validate_rf_reference(ref: str) -> str:
    """Validate ISO 11649 creditor reference (RF..). Returns compact uppercase."""
    if not re.fullmatch(r"RF\d{2}[0-9A-Z]{1,21}", ref):
        raise UPNReferenceError(
            f"invalid RF reference format: {ref!r} (expected RFkk + 1-21 alnum)"
        )
    if len(ref) > 25:
        raise UPNReferenceError(
            f"RF reference too long: {len(ref)} > 25: {ref!r}"
        )

    # ISO 11649 / ISO 7064 MOD 97-10: move 'RFkk' to end, expand letters A=10..Z=35,
    # and check the remainder equals 1.
    rearranged = ref[4:] + ref[:4]
    digits = []
    for ch in rearranged:
        if "0" <= ch <= "9":
            digits.append(ch)
        else:
            digits.append(str(ord(ch) - 55))
    if _mod97("".join(digits)) != 1:
        raise UPNReferenceError(f"invalid RF reference checksum: {ref!r}")
    return ref


def _si_mod11_check_digit(number: str) -> int:
    """Return SI (Slovenian) MOD 11 check digit for the provided decimal string."""
    total = 0
    for i, ch in enumerate(reversed(number)):
        total += (2 + i) * (ord(ch) - 48)
    remainder = total % 11
    check = 11 - remainder
    if check in (10, 11):
        return 0
    return check


def _validate_si_reference(ref: str) -> str:
    """Validate SI reference 'SI' + model + reference. Returns compact uppercase."""
    if not re.fullmatch(r"SI\d{2}.+", ref):
        raise UPNReferenceError(
            f"invalid SI reference format: {ref!r} (expected SIxx...)"
        )

    model = ref[2:4]
    content = ref[4:]

    def validate_grouped_digits(*, min_groups: int = 1, max_groups: int = 3) -> None:
        # Enforce a sane "electronic" representation:
        # - digits with up to 2 hyphens (max 3 groups)
        # - each group 1-12 digits
        # - total digits <= 20, total length <= 22 (digits + hyphens)
        if len(content) > 22:
            raise UPNReferenceError(
                f"SI reference too long: {len(content)} > 22 (excluding model): {ref!r}"
            )
        if content.startswith("-") or content.endswith("-") or "--" in content:
            raise UPNReferenceError(f"invalid SI reference hyphen placement: {ref!r}")
        parts = content.split("-")
        if not (min_groups <= len(parts) <= max_groups):
            raise UPNReferenceError(
                f"invalid SI reference group count {len(parts)} (expected {min_groups}-{max_groups}): {ref!r}"
            )
        digit_count = 0
        for p in parts:
            if not p.isdigit():
                raise UPNReferenceError(f"invalid SI reference (non-digits): {ref!r}")
            if not (1 <= len(p) <= 12):
                raise UPNReferenceError(
                    f"invalid SI reference group length {len(p)} (must be 1-12): {ref!r}"
                )
            digit_count += len(p)
        if digit_count > 20:
            raise UPNReferenceError(
                f"invalid SI reference (too many digits {digit_count} > 20): {ref!r}"
            )

    if model in ("00", "99"):
        validate_grouped_digits(min_groups=1, max_groups=3)
        return ref

    if model == "12":
        if not content.isdigit():
            raise UPNReferenceError(
                f"SI12 reference must be digits only (no hyphens): {ref!r}"
            )
        if not (2 <= len(content) <= 13):
            raise UPNReferenceError(
                f"SI12 reference length must be 2-13 digits, got {len(content)}: {ref!r}"
            )
        expected = _si_mod11_check_digit(content[:-1])
        actual = ord(content[-1]) - 48
        if actual != expected:
            raise UPNReferenceError(
                f"invalid SI12 reference checksum: {ref!r} (expected last digit {expected})"
            )
        return ref

    if model == "07":
        # Model 07: P1 - (P2)K - (P3) where the check digit is the last digit of P2.
        # 2 mandatory groups (P1, P2), optional third group (P3).
        validate_grouped_digits(min_groups=2, max_groups=3)
        p2 = content.split("-")[1]
        if len(p2) < 2:
            raise UPNReferenceError(
                f"SI07 P2 must include a check digit (min 2 digits): {ref!r}"
            )
        expected = _si_mod11_check_digit(p2[:-1])
        actual = ord(p2[-1]) - 48
        if actual != expected:
            raise UPNReferenceError(
                f"invalid SI07 reference checksum in P2: {ref!r} (expected last digit {expected})"
            )
        return ref

    raise UPNReferenceError(
        f"unsupported SI reference model SI{model} (supported: SI00, SI07, SI12, SI99): {ref!r}"
    )


def validate_upn_reference(reference: str) -> str:
    """Validate and normalise UPN reference field (SI.. or RF..).

    Returns a compact, uppercase reference with whitespace removed. Empty input
    returns ''.
    """
    ref = _WS_RE.sub("", reference.strip()).upper()
    if not ref:
        return ""
    if ref.startswith("RF"):
        return _validate_rf_reference(ref)
    if ref.startswith("SI"):
        return _validate_si_reference(ref)
    raise UPNReferenceError(
        f"reference must start with SI or RF (or be empty), got {reference!r}"
    )
