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
    """Return SI (Slovenian) MOD 11 check digit (ZBS standard, Appendix 3).

    Weights increase from 2 at the rightmost digit leftward.
    When the result is 10 or 11, the check digit is 0.
    """
    total = 0
    for i, ch in enumerate(reversed(number)):
        total += (2 + i) * (ord(ch) - 48)
    remainder = total % 11
    check = 11 - remainder
    if check in (10, 11):
        return 0
    return check


def _validate_si_grouped_digits(
    content: str, min_groups: int, max_groups: int, ref: str, model: str,
) -> None:
    """Validate grouped-digit structure: hyphens, group count, digit-only groups."""
    if len(content) > 22:
        raise UPNReferenceError(
            f"SI{model} content too long ({len(content)} > 22): {ref!r}"
        )
    if content.startswith("-") or content.endswith("-") or "--" in content:
        raise UPNReferenceError(f"invalid SI{model} hyphen placement: {ref!r}")
    parts = content.split("-")
    if not (min_groups <= len(parts) <= max_groups):
        raise UPNReferenceError(
            f"SI{model} requires {min_groups}-{max_groups} groups, "
            f"got {len(parts)}: {ref!r}"
        )
    digit_count = 0
    for p in parts:
        if not p.isdigit():
            raise UPNReferenceError(f"SI{model} group must be digits only: {ref!r}")
        if not (1 <= len(p) <= 12):
            raise UPNReferenceError(
                f"SI{model} group length must be 1-12 digits, got {len(p)}: {ref!r}"
            )
        digit_count += len(p)
    if digit_count > 20:
        raise UPNReferenceError(
            f"SI{model} has too many digits ({digit_count} > 20): {ref!r}"
        )


def _validate_checked_span(
    groups: list[str],
    span_start: int,
    span_end: int,
    model: str,
    ref: str,
    *,
    fixed_length: Optional[int] = None,
) -> None:
    """Validate a MOD11 check digit spanning groups[span_start..span_end] (inclusive).

    The check digit is the last digit of groups[span_end].
    Body = concat(groups[span_start..span_end-1]) + groups[span_end][:-1].
    fixed_length: if set, groups[span_end] must be exactly this many digits.
    """
    last_group = groups[span_end]
    if fixed_length is not None and len(last_group) != fixed_length:
        raise UPNReferenceError(
            f"SI{model} P{span_start + 1} must be exactly {fixed_length} digits, "
            f"got {len(last_group)}: {ref!r}"
        )
    if len(last_group) < 2:
        raise UPNReferenceError(
            f"SI{model} P{span_end + 1} must have at least 2 digits "
            f"(includes check digit): {ref!r}"
        )
    body = "".join(groups[span_start:span_end]) + last_group[:-1]
    expected = _si_mod11_check_digit(body)
    actual = ord(last_group[-1]) - 48
    if actual != expected:
        raise UPNReferenceError(
            f"invalid SI{model} reference checksum: {ref!r} "
            f"(expected last digit {expected})"
        )


# ── SI model table (ZBS, June 2011) ──────────────────────────────────────────
#
# Each entry: (min_groups, max_groups, fixed_spans)
# fixed_span: (span_start, span_end, fixed_length_or_None)
#   span_start..span_end are group indices (0-based) covered by one MOD11 check.
#   fixed_length: if set, the last group of the span must be exactly N digits.
#
# Models with a variable span end (01, 06, 09, 10) and special cases (12, 99)
# are handled explicitly in _validate_si_reference below.

_FixedSpan = tuple[int, int, Optional[int]]  # (start, end, fixed_length)
_ModelDef = tuple[int, int, list[_FixedSpan]]  # (min_groups, max_groups, spans)

_SI_MODEL_DEFS: dict[str, _ModelDef] = {
    # P1 free,  P2 check,  P3 check
    "02": (3, 3, [(1, 1, None), (2, 2, None)]),
    # P1 check, P2 check,  P3 check
    "03": (3, 3, [(0, 0, None), (1, 1, None), (2, 2, None)]),
    # P1 check, P2 free,   P3 check
    "04": (3, 3, [(0, 0, None), (2, 2, None)]),
    # P1 check, P2 free,   P3 free
    "05": (1, 3, [(0, 0, None)]),
    # P1 free,  P2 check,  P3 free
    "07": (2, 3, [(1, 1, None)]),
    # (P1-P2) combined check, P3 check
    "08": (3, 3, [(0, 1, None), (2, 2, None)]),
    # P1 check, P2 check,  P3 free
    "11": (2, 3, [(0, 0, None), (1, 1, None)]),
    "18": (2, 3, [(0, 0, None), (1, 1, None)]),
    # P1 check (8-digit tax ID), P2 check, P3 free
    "19": (2, 3, [(0, 0, 8),    (1, 1, None)]),
    # P1 check, P2 free
    "21": (2, 2, [(0, 0, None)]),
    # P1 check, P2 check,  P3 free
    "28": (2, 3, [(0, 0, None), (1, 1, None)]),
    # P1 check, P2 free
    "31": (2, 2, [(0, 0, None)]),
    # P1 check, P2 check,  P3 free
    "38": (2, 3, [(0, 0, None), (1, 1, None)]),
    "40": (2, 3, [(0, 0, None), (1, 1, None)]),
    "41": (2, 3, [(0, 0, None), (1, 1, None)]),
    "48": (2, 3, [(0, 0, None), (1, 1, None)]),
    "49": (2, 3, [(0, 0, None), (1, 1, None)]),
    "51": (2, 3, [(0, 0, None), (1, 1, None)]),
    # P1 check, P2 free,   P3 free
    "55": (1, 3, [(0, 0, None)]),
    # P1 check, P2 check,  P3 free
    "58": (2, 3, [(0, 0, None), (1, 1, None)]),
}

_ALL_SUPPORTED = sorted(
    {"00", "99", "12"} | set(_SI_MODEL_DEFS) | {"01", "06", "09", "10"}
)


def _validate_si_reference(ref: str) -> str:
    """Validate SI reference 'SI' + model + reference. Returns compact uppercase."""
    if not re.fullmatch(r"SI\d{2}.*", ref):
        raise UPNReferenceError(
            f"invalid SI reference format: {ref!r} (expected SIxx...)"
        )

    model = ref[2:4]
    content = ref[4:]

    # ── Model 99: no content (ZBS spec: 0 groups) ────────────────────────────
    if model == "99":
        if content:
            raise UPNReferenceError(
                f"SI99 must have no content after the model number, got {content!r}: {ref!r}"
            )
        return ref

    if not content:
        raise UPNReferenceError(
            f"SI{model} requires content after the model number: {ref!r}"
        )

    # ── Model 00: no check digits ─────────────────────────────────────────────
    if model == "00":
        _validate_si_grouped_digits(content, 1, 3, ref, model)
        return ref

    # ── Model 12: single digits-only group, up to 13 chars, MOD11 ────────────
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

    # ── Data-driven fixed-span models ─────────────────────────────────────────
    if model in _SI_MODEL_DEFS:
        min_g, max_g, spans = _SI_MODEL_DEFS[model]
        _validate_si_grouped_digits(content, min_g, max_g, ref, model)
        groups = content.split("-")
        for s_start, s_end, fixed_len in spans:
            _validate_checked_span(groups, s_start, s_end, model, ref, fixed_length=fixed_len)
        return ref

    # ── Variable-end-span models ──────────────────────────────────────────────

    if model == "01":
        # (P1 - ... - Pn)K: all groups share one combined check on last digit of last group
        _validate_si_grouped_digits(content, 1, 3, ref, model)
        groups = content.split("-")
        _validate_checked_span(groups, 0, len(groups) - 1, model, ref)
        return ref

    if model == "06":
        # P1 - (P2 - ... - Pn)K: P2..last share a combined check
        _validate_si_grouped_digits(content, 2, 3, ref, model)
        groups = content.split("-")
        _validate_checked_span(groups, 1, len(groups) - 1, model, ref)
        return ref

    if model == "09":
        # (P1 - P2)K - P3: P1+P2 share a combined check; P3 free
        # With 1 group: behaves as (P1)K
        _validate_si_grouped_digits(content, 1, 3, ref, model)
        groups = content.split("-")
        _validate_checked_span(groups, 0, min(1, len(groups) - 1), model, ref)
        return ref

    if model == "10":
        # (P1)K - (P2 - ... - Pn)K: P1 has own check; P2..last share a combined check
        _validate_si_grouped_digits(content, 2, 3, ref, model)
        groups = content.split("-")
        _validate_checked_span(groups, 0, 0, model, ref)
        _validate_checked_span(groups, 1, len(groups) - 1, model, ref)
        return ref

    raise UPNReferenceError(
        f"unsupported SI reference model SI{model} "
        f"(supported: {', '.join('SI' + m for m in _ALL_SUPPORTED)}): {ref!r}"
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
