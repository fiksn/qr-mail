#!/usr/bin/env python3
"""
Debug CLI: process a PDF/image as if it arrived as an email attachment.

Usage:
  python3 scripts/debug_process.py [OPTIONS] FILE [FILE ...]

Options:
  --output FILE   write forwarded .eml to FILE (default: stdout)
  --from ADDR     fake sender address (default: sender@example.com)
  --subject TEXT  fake subject (default: Test invoice)
  --dump-qr       print raw QR payloads found before parsing

Environment (all optional, sensible defaults applied):
  ADMIN_EMAIL     (default: admin@example.com)
  MY_ADDRESS      (default: me@example.com)

Example:
  python3 scripts/debug_process.py racun_26-390-0438150.pdf --output out.eml
  python3 scripts/debug_process.py racun_26-390-0438150.pdf --dump-qr
"""
import argparse
import email
import email.encoders
import mimetypes
import os
import sys
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Provide defaults so the processor doesn't exit when env vars are absent.
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("MY_ADDRESS", "me@example.com")
os.environ.setdefault("ALLOWED_SENDERS", "*")

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.payments import dedupe_qr_results, find_payments, scan_attachments
from scripts.mail_processor import build_forward


def build_fake_email(files: list[str], sender: str, subject: str) -> email.message.Message:
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = os.environ["MY_ADDRESS"]
    msg["Subject"] = subject
    msg["Date"] = "Fri, 18 Apr 2025 12:00:00 +0000"
    msg.attach(MIMEText("Debug test message.", "plain", "utf-8"))

    for path in files:
        with open(path, "rb") as f:
            data = f.read()

        filename = os.path.basename(path)
        mime_type, _ = mimetypes.guess_type(path)
        if mime_type is None:
            mime_type = "application/octet-stream"

        maintype, subtype = mime_type.split("/", 1)
        part = MIMEBase(maintype, subtype)
        part.set_payload(data)
        email.encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    return msg


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process PDF/image files through qr-mail pipeline without sending email."
    )
    parser.add_argument("files", nargs="+", metavar="FILE")
    parser.add_argument("--output", metavar="FILE", help="write output .eml here (default: stdout)")
    parser.add_argument("--from", dest="sender", default="sender@example.com", metavar="ADDR")
    parser.add_argument("--subject", default="Test invoice")
    parser.add_argument("--dump-qr", metavar="FILE", nargs="?", const="epc_qr.png",
                        help="save EPC QR image(s) to FILE (default: epc_qr.png)")
    args = parser.parse_args()

    for f in args.files:
        if not os.path.exists(f):
            print(f"ERROR: file not found: {f}", file=sys.stderr)
            sys.exit(1)

    msg = build_fake_email(args.files, args.sender, args.subject)

    max_bytes = 100 * 1024 * 1024
    qr_results = scan_attachments(msg, max_bytes)
    print(f"QR codes found (raw): {len(qr_results)}", file=sys.stderr)


    qr_unique = dedupe_qr_results(qr_results)
    print(f"QR codes found (unique): {len(qr_unique)}", file=sys.stderr)

    payments = find_payments(qr_unique)
    print(f"Payment items: {len(payments)}", file=sys.stderr)
    for i, p in enumerate(payments, 1):
        src = ", ".join(p.sources)
        print(f"  [{i}] {p.note} — {src}", file=sys.stderr)
        if p.reference_errors:
            for e in p.reference_errors:
                print(f"       WARNING: {e}", file=sys.stderr)
        if p.conversion_error:
            print(f"       ERROR: {p.conversion_error}", file=sys.stderr)
        if p.epc_qr_png is not None and args.dump_qr:
            stem, ext = os.path.splitext(args.dump_qr)
            ext = ext or ".png"
            png_path = f"{stem}_{i}{ext}" if len(payments) > 1 else f"{stem}{ext}"
            with open(png_path, "wb") as f:
                f.write(p.epc_qr_png)
            print(f"  EPC QR saved: {png_path}", file=sys.stderr)

    fwd = build_forward(
        msg,
        args.sender,
        os.environ["MY_ADDRESS"],
        os.environ["ADMIN_EMAIL"],
        reply_to_sender=False,
        payments=payments,
    )

    output = fwd.as_bytes()
    if args.output:
        with open(args.output, "wb") as f:
            f.write(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
