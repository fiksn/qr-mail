#!/usr/bin/env python3
"""Interactive CLI to generate UPN and EPC SCT payment QR codes.

Usage:
  python3 generate_qr.py
  python3 generate_qr.py --output invoice_123   # filename prefix for saved PNGs
"""
import argparse
import os
import sys
from decimal import Decimal, InvalidOperation

from generate import generate_epc_qr, generate_upn_qr, upn_to_epc
from upn import UPN, UPNReferenceError, validate_upn_reference


def ask(prompt: str, *, default: str = "", required: bool = False) -> str:
    display = f"{prompt} [{default}]: " if default else f"{prompt}: "
    while True:
        value = input(display).strip()
        if not value:
            value = default
        if required and not value:
            print("  (required)")
            continue
        return value


def ask_amount() -> int:
    """Ask for EUR amount and return cents. Returns 0 if left blank."""
    while True:
        raw = input("Amount in EUR (blank = unspecified): ").strip()
        if not raw:
            return 0
        try:
            d = Decimal(raw.replace(",", "."))
            if d < 0:
                print("  Amount must be positive.")
                continue
            return int(d * 100)
        except InvalidOperation:
            print("  Enter a number, e.g. 58.27")


def ask_reference() -> str:
    """Ask for a SI or RF reference, validate, and return compact form."""
    while True:
        raw = input("Reference (SI/RF, blank = none): ").strip()
        if not raw:
            return ""
        try:
            return validate_upn_reference(raw)
        except UPNReferenceError as exc:
            print(f"  Invalid reference: {exc}")


def ask_iban() -> str:
    while True:
        raw = input("IBAN (required): ").strip().replace(" ", "").upper()
        if not raw:
            print("  (required)")
            continue
        if len(raw) < 15 or not raw[:2].isalpha() or not raw[2:].isdigit():
            print("  Doesn't look like a valid IBAN.")
            continue
        return raw


def build_upn(amount_cents: int, reference: str, purpose_code: str,
              payment_purpose: str, iban: str, name: str,
              street: str, city: str) -> UPN:
    return UPN(
        payer_iban="", deposit=False, withdrawal=False,
        payer_reference="", payer_name="", payer_street="", payer_city="",
        amount_cents=amount_cents,
        payment_date=None, urgent=False,
        purpose_code=purpose_code,
        payment_purpose=payment_purpose,
        payment_deadline=None,
        recipient_iban=iban,
        recipient_reference=reference,
        recipient_name=name[:33],
        recipient_street=street[:33],
        recipient_city=city[:33],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate UPN and EPC SCT payment QR code images."
    )
    parser.add_argument("--output", metavar="PREFIX", default="payment",
                        help="filename prefix for saved PNGs (default: payment)")
    parser.add_argument("--format", choices=["upn", "epc", "both"], default="both",
                        help="which QR format(s) to generate (default: both)")
    args = parser.parse_args()

    print("Generate payment QR codes")
    print("─" * 40)

    iban = ask_iban()
    name = ask("Recipient name", required=True)
    street = ask("Recipient street")
    city = ask("Recipient city", required=True)
    reference = ask_reference()
    amount_cents = ask_amount()
    purpose_code = ask("Purpose code", default="OTHR")
    payment_purpose = ask("Payment purpose / description")

    print()

    upn = build_upn(amount_cents, reference, purpose_code,
                    payment_purpose, iban, name, street, city)

    want_upn = args.format in ("upn", "both")
    want_epc = args.format in ("epc", "both")

    prefix = args.output

    if want_upn:
        upn_path = f"{prefix}_upn.png"
        with open(upn_path, "wb") as f:
            f.write(generate_upn_qr(upn))
        print(f"UPN QR saved: {upn_path}")

    if want_epc:
        try:
            epc = upn_to_epc(upn)
        except Exception as exc:
            print(f"EPC conversion failed: {exc}", file=sys.stderr)
            sys.exit(1)
        epc_path = f"{prefix}_epc.png"
        with open(epc_path, "wb") as f:
            f.write(generate_epc_qr(epc))
        print(f"EPC QR saved: {epc_path}")


if __name__ == "__main__":
    main()
