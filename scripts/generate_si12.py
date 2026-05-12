#!/usr/bin/env python3
"""Generate a UPN payment slip with payee from a file and an SI12 reference.

Behaves like scripts/generate_qr.py, with three differences:
- The recipient (payee) is loaded from a file given by --payee-file or the
  QR_MAIL_PAYEE_FILE environment variable; recipient prompts are skipped.
- The payer section is left empty (no payer prompts, no payer-on-slip rendering).
- The reference is entered as digits only; the SI12 model and the ZBS MOD11
  check digit are appended automatically.

All other fields (amount, dates, purpose code, payment purpose, deadline) are
prompted interactively.

Usage:
  python3 scripts/generate_si12.py --payee-file payee.txt
  QR_MAIL_PAYEE_FILE=payee.txt python3 scripts/generate_si12.py
"""
import argparse
import os
import sys
from datetime import datetime

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.upn import (
    UPNReferenceError,
    _si_mod11_check_digit,
    validate_upn_reference,
)
from scripts.generate_qr import (
    DEFAULT_SLIP_TEMPLATE,
    PAYEE_FILE_ENV,
    ask,
    ask_amount,
    ask_date,
    build_upn,
    generate_upn_slip_png,
    load_party_template_from_file,
)


def ask_si12_reference() -> str:
    """Prompt for the SI12 body digits and append the ZBS MOD11 check digit."""
    while True:
        raw = input("Reference digits (SI12 body, check digit will be added): ").strip()
        if not raw:
            print("  (required)")
            continue
        body = raw.upper().removeprefix("SI12")
        for sep in (" ", "-"):
            body = body.replace(sep, "")
        if not body.isdigit():
            print("  Reference must contain digits only.")
            continue
        if not (1 <= len(body) <= 12):
            print(f"  Reference body must be 1-12 digits, got {len(body)}.")
            continue
        check = _si_mod11_check_digit(body)
        ref = f"SI12{body}{check}"
        try:
            return validate_upn_reference(ref)
        except UPNReferenceError as exc:
            print(f"  Generated reference failed validation: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a UPN payment slip + QR with payee loaded from a file. "
            "The reference is entered as digits and the SI12 MOD11 check digit "
            "is appended automatically."
        ),
    )
    parser.add_argument(
        "--payee-file",
        default=os.environ.get(PAYEE_FILE_ENV, "").strip(),
        help=(
            f"path to the payee file (IBAN, name, street, city — one per line). "
            f"Falls back to ${PAYEE_FILE_ENV}."
        ),
    )
    parser.add_argument(
        "--output",
        "-o",
        metavar="FILE",
        default="payment_poloznica.png",
        help="output PNG path (default: payment_poloznica.png)",
    )
    parser.add_argument(
        "--deadline",
        metavar="DD.MM.YYYY",
        default="",
        help="payment deadline; skips the interactive prompt when set",
    )
    parser.add_argument(
        "--slip-template",
        default=DEFAULT_SLIP_TEMPLATE,
        help="path to empty UPN slip template image",
    )
    args = parser.parse_args()

    deadline_override = None
    if args.deadline:
        try:
            deadline_override = datetime.strptime(args.deadline, "%d.%m.%Y").date()
        except ValueError:
            print(
                f"--deadline must be DD.MM.YYYY, got {args.deadline!r}",
                file=sys.stderr,
            )
            sys.exit(1)

    if not args.payee_file:
        print(
            f"--payee-file or ${PAYEE_FILE_ENV} is required.",
            file=sys.stderr,
        )
        sys.exit(1)

    iban, name, street, city = load_party_template_from_file(args.payee_file)
    if not (iban and name and city):
        print(
            f"payee file {args.payee_file!r} is missing or invalid "
            f"(expected: IBAN, name, street, city on separate lines).",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Recipient (from file):")
    print(f"  IBAN:   {iban}")
    print(f"  Name:   {name}")
    print(f"  Street: {street}")
    print(f"  City:   {city}")
    print()

    reference = ask_si12_reference()
    amount_cents = ask_amount()
    payment_date = ask_date("Payment date", default_today=True)
    payment_deadline = deadline_override if deadline_override else ask_date("Payment deadline")
    purpose_code = ask("Purpose code", default="OTHR")
    payment_purpose = ask("Payment purpose / description")

    upn = build_upn(
        amount_cents, reference, purpose_code,
        payment_purpose, iban, name, street, city,
        payer_iban="", payer_reference="",
        payer_name="", payer_street="", payer_city="",
        deposit=False, withdrawal=False, urgent=False,
        payment_date=payment_date, payment_deadline=payment_deadline,
    )

    try:
        slip_png = generate_upn_slip_png(upn, template_path=args.slip_template)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    with open(args.output, "wb") as f:
        f.write(slip_png)
    print(f"UPN slip saved: {args.output} (reference: {reference})")


if __name__ == "__main__":
    main()
