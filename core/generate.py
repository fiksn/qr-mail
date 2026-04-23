"""QR code generation and format conversion for UPN and EPC payment standards.

Requires: segno  (pip install segno)

Public API:
  upn_to_string(upn)                          → str   (QR payload, encode as ISO 8859-2)
  epc_to_string(epc)                          → str   (QR payload)
  generate_upn_qr(upn, *, scale)              → bytes (PNG)
  generate_epc_qr(epc, *, scale)              → bytes (PNG)
  upn_to_epc(upn)                             → EPC
  epc_to_upn(epc, *, recipient_city, ...)     → UPN
"""
import io

from core.epc import EPC, CHARSETS
from core.upn import UPN, UPNReferenceError, validate_upn_reference

DEFAULT_EPC_BENEFICIARY_NAME = "PREJEMNIK"


# ── Serialisers ────────────────────────────────────────────────────────────────

def upn_to_string(upn: UPN) -> str:
    """Serialise a UPN to its QR payload string.

    The returned string must be encoded as ISO 8859-2 before embedding in a QR
    code (spec §5.1: ECI 000004).  generate_upn_qr() handles this automatically.
    """
    fields = [
        "UPNQR",
        upn.payer_iban,
        "X" if upn.deposit else "",
        "X" if upn.withdrawal else "",
        upn.payer_reference,
        upn.payer_name,
        upn.payer_street,
        upn.payer_city,
        f"{upn.amount_cents:011d}",
        upn.payment_date.strftime("%d.%m.%Y") if upn.payment_date else "",
        "X" if upn.urgent else "",
        upn.purpose_code,
        upn.payment_purpose,
        upn.payment_deadline.strftime("%d.%m.%Y") if upn.payment_deadline else "",
        upn.recipient_iban,
        upn.recipient_reference,
        upn.recipient_name,
        upn.recipient_street,
        upn.recipient_city,
    ]
    checksum = sum(len(f) + 1 for f in fields)
    fields.append(f"{checksum:03d}")
    return "\n".join(fields)


def epc_to_string(epc: EPC) -> str:
    """Serialise an EPC payload to its QR string."""
    charset_key = next((k for k, v in CHARSETS.items() if v == epc.charset), "1")
    amount_str = f"EUR{epc.amount:.2f}" if epc.amount is not None else ""
    fields = [
        "BCD",
        epc.version,
        charset_key,
        "SCT",
        epc.bic,
        epc.beneficiary_name,
        epc.beneficiary_iban,
        amount_str,
        epc.purpose_code,
        epc.structured_ref,
        epc.unstructured_ref,
        epc.originator_info,
    ]
    while fields and fields[-1] == "":
        fields.pop()
    return "\n".join(fields)


# ── QR generators ──────────────────────────────────────────────────────────────

def generate_upn_qr(upn: UPN, *, scale: int = 10) -> bytes:
    """Return PNG bytes of a UPN QR code.

    Spec §5.1: Version 15 (77×77), ECC M, ISO 8859-2 / ECI 000004.
    segno sets the ECI designator automatically when encoding is specified.
    """
    import segno

    payload = upn_to_string(upn)
    qr = segno.make_qr(payload, encoding="iso-8859-2", error="m", version=15)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale)
    return buf.getvalue()


def generate_epc_qr(epc: EPC, *, scale: int = 10) -> bytes:
    """Return PNG bytes of an EPC QR code.

    ECC M; minimum QR version chosen automatically; ECI set per charset field.
    """
    import segno

    payload = epc_to_string(epc)
    qr = segno.make_qr(payload, encoding=epc.charset, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale)
    return buf.getvalue()


# ── Converters ─────────────────────────────────────────────────────────────────

def upn_to_epc(upn: UPN) -> EPC:
    """Convert a UPN payment order to an EPC SCT payload.

    Mapping:
    - Recipient IBAN / name / purpose code / amount map directly.
    - UPN reference starting with 'RF' → EPC structured_ref (ISO 11649).
    - Other UPN references and payment_purpose → EPC unstructured_ref,
      joined with ' / ' if both are present (truncated to 140 chars).
    - Payer details and UPN-specific flags are dropped (not in EPC SCT).
    - BIC is absent in UPN → version 002 (BIC optional) is used.
    """
    structured_ref = ""
    unstructured_parts: list[str] = []

    if upn.recipient_reference:
        # Be strict about the structured RF format (otherwise many scanners/banks reject the QR),
        # but stay lenient overall: if the reference is invalid/unsupported, keep it as free text.
        try:
            ref = validate_upn_reference(upn.recipient_reference)
            if ref.startswith("RF"):
                structured_ref = ref
            else:
                unstructured_parts.append(ref)
        except UPNReferenceError:
            unstructured_parts.append(upn.recipient_reference.strip())

    originator_info = ""
    if upn.payment_purpose:
        if structured_ref:
            # EPC structured and unstructured remittance fields are mutually exclusive.
            # Keep the free-text purpose as display-only info so the QR remains spec-compliant.
            originator_info = upn.payment_purpose[:70]
        else:
            unstructured_parts.append(upn.payment_purpose)

    unstructured_ref = " / ".join(unstructured_parts)[:140]

    beneficiary_name = (upn.recipient_name or "").strip() or DEFAULT_EPC_BENEFICIARY_NAME

    if len(beneficiary_name) > 70:
        raise ValueError(
            f"recipient name too long for EPC (max 70 chars): {beneficiary_name!r}"
        )

    return EPC(
        version="002",
        charset="utf-8",
        bic="",
        beneficiary_name=beneficiary_name,
        beneficiary_iban=upn.recipient_iban,
        amount=upn.amount if upn.amount_cents else None,
        purpose_code=upn.purpose_code,
        structured_ref=structured_ref,
        unstructured_ref=unstructured_ref,
        originator_info=originator_info,
    )


def epc_to_upn(
    epc: EPC,
    *,
    recipient_city: str,
    recipient_street: str = "",
) -> UPN:
    """Convert an EPC SCT payload to a UPN payment order.

    recipient_city is required because EPC does not carry it, but UPN mandates it.
    recipient_street is optional.

    Mapping:
    - Beneficiary IBAN / name / purpose code / amount map directly.
    - EPC structured_ref (RF…) → UPN recipient_reference.
    - EPC unstructured_ref → UPN payment_purpose (truncated to 42 chars).
    - Payer fields are empty; deposit / withdrawal / urgent are False.
    - originator_info is display-only in EPC and is dropped.
    """
    amount_cents = int(epc.amount * 100) if epc.amount is not None else 0

    recipient_reference = epc.structured_ref  # RF format or empty
    payment_purpose = epc.unstructured_ref[:42]

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
        purpose_code=epc.purpose_code,
        payment_purpose=payment_purpose,
        payment_deadline=None,
        recipient_iban=epc.beneficiary_iban,
        recipient_reference=recipient_reference,
        recipient_name=epc.beneficiary_name[:33],
        recipient_street=recipient_street[:33],
        recipient_city=recipient_city[:33],
    )
