#!/usr/bin/env python3
"""Verify an eSLOG 2.0 XML signature and extract payment data.

Signature verification details are printed to stderr.
On stdout, the extracted UPN fields are printed one per line in the
order expected by ``generate_qr.py``, so you can pipe them directly:

  python3 scripts/verify_and_extract_eslog.py invoice.xml \
    | python3 scripts/generate_qr.py --format all

Exit codes:
  0  success (valid signature or unsigned document)
  1  invalid signature or parse error
  2  usage error

By default, intermediate CA certificates are loaded from
``slo-intermediates.pem`` in the repository root. Set
``ESLOG_INTERMEDIATE_CERTS_FILE`` to override that with a different PEM
file. Mixed CA bundles are also accepted: self-signed roots from that
file are promoted to trust anchors automatically.

Set ``ESLOG_TRUSTED_CERTS_FILE`` to add PEM trust anchors explicitly.

Set ``SLOG_SKIP_CHAIN_VALIDATION=1`` or ``ESLOG_SKIP_CHAIN_VALIDATION=1``
to skip certificate trust-chain validation and verify only the XML
signature and digest references.
"""
from __future__ import annotations

import os
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parsers.eslog import ESlogParseError, parse_eslog_invoice
from parsers.xmldsig import verify_eslog_signature


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <eslog-invoice.xml>", file=sys.stderr)
        sys.exit(2)

    path = sys.argv[1]
    try:
        with open(path, "rb") as f:
            xml_bytes = f.read()
    except (OSError, IOError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    # ── Signature verification ──────────────────────────────────────────
    sig = verify_eslog_signature(xml_bytes)

    if not sig.signed:
        print("Signature: UNSIGNED", file=sys.stderr)
    elif sig.valid is True and sig.signer:
        print("Signature: VALID", file=sys.stderr)
        print(f"  Signer:  {sig.signer.subject}", file=sys.stderr)
        print(f"  Issuer:  {sig.signer.issuer}", file=sys.stderr)
        print(
            f"  Valid:   {sig.signer.not_before} — {sig.signer.not_after}",
            file=sys.stderr,
        )
        if sig.signer.signing_time:
            print(f"  Signed:  {sig.signer.signing_time}", file=sys.stderr)
        if sig.chain:
            print("  Chain:", file=sys.stderr)
            for idx, cert in enumerate(sig.chain, start=1):
                print(f"    {idx}. Subject: {cert.subject}", file=sys.stderr)
                print(f"       Issuer:  {cert.issuer}", file=sys.stderr)
                print(
                    f"       Valid:   {cert.not_before} — {cert.not_after}",
                    file=sys.stderr,
                )
        for warning in sig.warnings:
            print(f"  Warning: {warning}", file=sys.stderr)
    elif sig.valid is False:
        print(f"Signature: INVALID ({sig.error})", file=sys.stderr)
        sys.exit(1)
    elif sig.valid is None:
        print(f"Signature: COULD NOT VERIFY ({sig.error})", file=sys.stderr)
        for warning in sig.warnings:
            print(f"  Warning: {warning}", file=sys.stderr)

    # ── Parse eSLOG invoice ─────────────────────────────────────────────
    try:
        upn = parse_eslog_invoice(xml_bytes)
    except ESlogParseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    amount_eur = f"{upn.amount_cents / 100:.2f}" if upn.amount_cents else ""
    payment_date = upn.payment_date.strftime("%d.%m.%Y") if upn.payment_date else ""
    deadline = upn.payment_deadline.strftime("%d.%m.%Y") if upn.payment_deadline else ""

    # Output one field per line, matching generate_qr.py interactive
    # prompt order (recipient, then payer).
    lines = [
        upn.recipient_iban,           # IBAN
        upn.recipient_name,           # Recipient name
        upn.recipient_street,         # Recipient street
        upn.recipient_city,           # Recipient city
        upn.recipient_reference,      # Reference
        amount_eur,                   # Amount in EUR
        payment_date,                 # Payment date
        deadline,                     # Payment deadline
        upn.purpose_code or "OTHR",   # Purpose code
        upn.payment_purpose,          # Payment purpose
        "",                           # Payer IBAN
        "",                           # Payer reference
        "",                           # Payer name
        "",                           # Payer street
        "",                           # Payer city
        "n",                          # Polog (deposit)
        "n",                          # Dvig (withdrawal)
        "n",                          # Nujno (urgent)
    ]
    for line in lines:
        print(line)


if __name__ == "__main__":
    main()
