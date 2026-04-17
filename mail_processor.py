#!/usr/bin/env python3
"""
Postfix pipe handler: forwards mail from allowed senders to ADMIN_EMAIL.

Scans image and PDF attachments for QR codes. For each valid UPN payment order
found, it converts to an EPC SCT QR and includes both a readable summary and a
QR image. If an EPC SCT QR is found directly, it is also included (no conversion).
The original email body is quoted below and all original attachments are preserved.

Configuration via environment variables:
  ADMIN_EMAIL            recipient for forwarded mail
  MY_ADDRESS             the address that received the mail (used as From)
  ALLOWED_SENDERS        colon-separated glob patterns, e.g. "*@trusted.com:alice@*"
  ALLOWED_SENDER_ROUTES  optional routes: "<glob>=a@b.com,c@d.com:..."; admin is CCed
  TRUSTED_SENDERS        like ALLOWED_SENDERS, but reply to sender + CC admin
  MAX_ATTACHMENT_BYTES   size limit per attachment in bytes (default: 104857600 = 100 MB)
  EPC_TO_UPN_CITY        recipient city used when converting EPC → UPN (default: Ljubljana)
  MAX_PDF_PAGES          max PDF pages rendered per attachment (default: 10)
  PDF_RENDER_TIMEOUT_S   pdf2image/poppler render timeout seconds (default: 20)
  PDFINFO_TIMEOUT_S      pdfinfo timeout seconds when checking PDF encryption (default: 3)
  MAX_IMAGE_PIXELS       PIL image pixel limit / decompression bomb guard (default: 40000000)
  MAX_MESSAGE_RUNTIME_S  max total processing time per message (default: 60)

Standalone usage:
  ADMIN_EMAIL=admin@example.com \
  MY_ADDRESS=test@example.com \
  ALLOWED_SENDERS="*@trusted.com:alice@*" \
  python3 mail_processor.py < message.eml
"""
import email
import email.encoders
import html
import io
import hashlib
import logging
import os
import smtplib
import subprocess
import sys
import tempfile
import signal
from dataclasses import dataclass, field
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from typing import Optional

import pdf2image
from pdf2image.exceptions import PDFPageCountError
from PIL import Image
from pyzbar import pyzbar

from epc import EPC, EPCParseError, format_epc, parse_epc
from generate import epc_to_string, generate_epc_qr, upn_to_epc
from routing import (
    find_route,
    is_allowed_sender,
    matches,
    parse_allowed_sender_routes,
    parse_allowed_senders,
)
from upn import UPN, UPNParseError, UPNReferenceError, format_upn, parse_upn, validate_upn_reference

logging.basicConfig(
    stream=sys.stderr, level=logging.DEBUG,
    format="qr-mail: %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100 MB
DEFAULT_MAX_PDF_PAGES = 10
DEFAULT_PDF_RENDER_TIMEOUT_S = 20
# Keep conservative to avoid PIL DecompressionBomb warnings/errors on malicious inputs.
DEFAULT_MAX_IMAGE_PIXELS = 40_000_000
DEFAULT_MAX_MESSAGE_RUNTIME_S = 60
DEFAULT_PDFINFO_TIMEOUT_S = 3


class MessageProcessingTimeout(RuntimeError):
    pass


def _alarm_handler(signum, frame) -> None:  # noqa: ARG001
    raise MessageProcessingTimeout("message processing exceeded time limit")


@dataclass
class PaymentItem:
    sources: list[str]  # e.g. ["invoice.jpeg", "document.pdf#page=2"]
    kind: str    # "upn" or "epc"
    note: str    # short description, shown to user

    upn: Optional[UPN] = None
    epc: Optional[EPC] = None

    epc_qr_png: Optional[bytes] = None
    epc_payload: Optional[str] = None

    conversion_error: Optional[str] = None
    reference_errors: list[str] = field(default_factory=list)


def load_config() -> tuple[str, str, list[str], list[str], list[str], int]:
    missing = [v for v in ("ADMIN_EMAIL", "MY_ADDRESS") if not os.environ.get(v)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    allowed_raw = [p for p in os.environ.get("ALLOWED_SENDERS", "").split(":") if p]
    routes_raw = [p for p in os.environ.get("ALLOWED_SENDER_ROUTES", "").split(":") if p]
    trusted = [p for p in os.environ.get("TRUSTED_SENDERS", "").split(":") if p]
    if not allowed_raw and not routes_raw and not trusted:
        print(
            "ERROR: no senders configured (set ALLOWED_SENDERS, ALLOWED_SENDER_ROUTES and/or TRUSTED_SENDERS)",
            file=sys.stderr,
        )
        sys.exit(1)

    return (
        os.environ["ADMIN_EMAIL"],
        os.environ["MY_ADDRESS"],
        allowed_raw,
        routes_raw,
        trusted,
        int(os.environ.get("MAX_ATTACHMENT_BYTES", DEFAULT_MAX_ATTACHMENT_BYTES)),
    )


def _decode_qr_bytes(data: bytes) -> str:
    """Decode QR payload bytes. UPN spec mandates ISO 8859-2 (ECI 000004);
    fall back to UTF-8 for non-UPN codes."""
    try:
        return data.decode("iso-8859-2")
    except (UnicodeDecodeError, LookupError):
        return data.decode("utf-8", errors="replace")


def scan_image_for_qr(img: Image.Image) -> list[str]:
    try:
        results = pyzbar.decode(img, symbols=[pyzbar.ZBarSymbol.QRCODE])
        return [_decode_qr_bytes(r.data) for r in results]
    except Exception as exc:
        log.warning("pyzbar decode error: %s", exc)
        return []


def scan_pdf_for_qr(data: bytes) -> list[tuple[str, int]]:
    """Render each PDF page at 150 DPI and scan for QR codes."""
    codes: list[tuple[str, int]] = []
    try:
        max_pages = int(os.environ.get("MAX_PDF_PAGES", DEFAULT_MAX_PDF_PAGES))
        timeout_s = int(os.environ.get("PDF_RENDER_TIMEOUT_S", DEFAULT_PDF_RENDER_TIMEOUT_S))
        pages = pdf2image.convert_from_bytes(
            data,
            dpi=150,
            first_page=1,
            last_page=max_pages,
            timeout=timeout_s,
        )
    except PDFPageCountError as exc:
        log.warning("pdf2image failed to read page count: %s", exc)
        return codes
    except Exception as exc:
        log.warning("pdf2image conversion failed: %s", exc)
        return codes
    for page_num, page_img in enumerate(pages, start=1):
        found = scan_image_for_qr(page_img)
        log.debug("PDF page %d: %s", page_num,
                  f"found {len(found)} QR code(s)" if found else "no QR codes")
        for c in found:
            codes.append((c, page_num))
    return codes


def scan_image_bytes_for_qr(data: bytes) -> list[str]:
    try:
        Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", DEFAULT_MAX_IMAGE_PIXELS))
        img = Image.open(io.BytesIO(data))
        img.load()
    except Image.DecompressionBombError as exc:
        log.warning("image rejected (decompression bomb): %s", exc)
        return []
    except Exception as exc:
        log.warning("image load failed: %s", exc)
        return []
    return scan_image_for_qr(img)


def _is_pdf_encrypted(data: bytes) -> bool:
    """Best-effort encrypted-PDF detection via poppler's pdfinfo.

    If pdfinfo is missing or errors, return False (don't block scanning).
    """
    timeout_s = int(os.environ.get("PDFINFO_TIMEOUT_S", DEFAULT_PDFINFO_TIMEOUT_S))
    try:
        with tempfile.NamedTemporaryFile(prefix="qr-mail-", suffix=".pdf") as f:
            f.write(data)
            f.flush()
            proc = subprocess.run(
                ["pdfinfo", f.name],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("pdfinfo unavailable/timeout (%s); not checking encryption", exc)
        return False
    except Exception as exc:
        log.debug("pdfinfo failed (%s); not checking encryption", exc)
        return False

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for line in out.splitlines():
        if line.strip().lower().startswith("encrypted:"):
            return "yes" in line.lower()
    return False

def scan_attachments(
    msg: email.message.Message, max_bytes: int
) -> list[tuple[str, str]]:
    """Scan image and PDF attachments for QR codes.

    Returns a list of (qr_content, source_label) where source_label is the
    attachment filename, e.g. 'invoice.jpeg' or 'document.pdf'.
    """
    results: list[tuple[str, str]] = []

    for part in msg.walk():
        content_type = part.get_content_type()
        filename = part.get_filename() or f"<{content_type}>"

        is_image = content_type.startswith("image/")
        is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")

        if not (is_image or is_pdf):
            continue

        payload = part.get_payload(decode=True)
        if payload is None:
            log.debug("Skipping %r: empty payload", filename)
            continue

        size = len(payload)
        if size > max_bytes:
            log.warning("Skipping %r: %d bytes exceeds limit of %d", filename, size, max_bytes)
            continue

        log.debug("Scanning %r (%s, %d bytes)", filename, content_type, size)
        if is_pdf and _is_pdf_encrypted(payload):
            log.warning("Skipping %r: encrypted PDF", filename)
            codes_pdf: list[tuple[str, int]] = []
        else:
            codes_pdf = scan_pdf_for_qr(payload) if is_pdf else []
            codes_img = [] if is_pdf else scan_image_bytes_for_qr(payload)

        total_codes = len(codes_pdf) if is_pdf else len(codes_img)
        if total_codes:
            log.debug("%r: found %d QR code(s)", filename, total_codes)
        else:
            log.debug("%r: no QR codes found", filename)

        if is_pdf:
            for code, page_num in codes_pdf:
                results.append((code, f"{filename}#page={page_num}"))
        else:
            for code in codes_img:
                results.append((code, filename))

    return results


def dedupe_qr_results(qr_results: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    """Deduplicate QR payloads while preserving the first-seen order.

    Returns a list of (qr_text, sources[]).
    """
    seen: dict[str, tuple[str, list[str]]] = {}
    order: list[str] = []

    for qr_text, source in qr_results:
        # Normalize just for hashing (do not mutate the string that will be parsed).
        normalized = qr_text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        key = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()
        if key not in seen:
            seen[key] = (qr_text, [source])
            order.append(key)
        else:
            # Avoid noisy duplicates while preserving discovery order.
            srcs = seen[key][1]
            if source not in srcs:
                srcs.append(source)

    return [seen[k] for k in order]


def find_payments(qr_results: list[tuple[str, list[str]]]) -> list[PaymentItem]:
    """Parse QR codes and return a list of UPN (converted) and EPC (direct) items."""
    payments: list[PaymentItem] = []

    for qr_text, sources in qr_results:
        try:
            upn = parse_upn(qr_text)
            log.info("UPN found in %r:\n%s", sources[0] if sources else "<unknown>", format_upn(upn))
            ref_errors: list[str] = []
            if upn.payer_reference:
                try:
                    validate_upn_reference(upn.payer_reference)
                except UPNReferenceError as exc:
                    ref_errors.append(f"payer reference: {exc}")
            if upn.recipient_reference:
                try:
                    validate_upn_reference(upn.recipient_reference)
                except UPNReferenceError as exc:
                    ref_errors.append(f"recipient reference: {exc}")
            try:
                epc = upn_to_epc(upn)
                payload = epc_to_string(epc)
                png = generate_epc_qr(epc)
                payments.append(
                    PaymentItem(
                        sources=sources,
                        kind="upn",
                        note="UPN QR (converted to EPC SCT)",
                        upn=upn,
                        epc=epc,
                        epc_qr_png=png,
                        epc_payload=payload,
                        conversion_error=None,
                        reference_errors=ref_errors,
                    )
                )
            except Exception as exc:
                log.warning("UPN→EPC conversion failed for QR in %r: %s", sources[0] if sources else "<unknown>", exc)
                # Still include the parsed UPN in the forwarded mail, but without EPC QR.
                payments.append(
                    PaymentItem(
                        sources=sources,
                        kind="upn",
                        note="UPN QR (conversion to EPC SCT failed)",
                        upn=upn,
                        epc=None,
                        epc_qr_png=None,
                        epc_payload=None,
                        conversion_error=str(exc),
                        reference_errors=ref_errors,
                    )
                )
            continue
        except UPNParseError:
            pass

        try:
            epc = parse_epc(qr_text)
            log.info(
                "EPC SCT found in %r (no conversion):\n%s",
                sources[0] if sources else "<unknown>",
                format_epc(epc),
            )
            payload = qr_text.replace("\r\n", "\n").replace("\r", "\n")
            try:
                png = generate_epc_qr(epc)
            except Exception as exc:
                log.warning(
                    "EPC QR regeneration failed for QR in %r: %s",
                    sources[0] if sources else "<unknown>",
                    exc,
                )
                png = None
            payments.append(
                PaymentItem(
                    sources=sources,
                    kind="epc",
                    note="EPC SCT QR (no conversion)",
                    upn=None,
                    epc=epc,
                    epc_qr_png=png,
                    epc_payload=payload,
                )
            )
        except EPCParseError:
            log.debug("QR in %r is neither UPN nor EPC", source)

    return payments


def extract_text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                return payload.decode(charset, errors="replace") if payload else ""
        return "(no text body)"
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return "(no body)"


def _build_payment_text_block(payment: PaymentItem) -> list[str]:
    """Plain-text payment block used in forwarded mail."""
    lines: list[str] = []
    lines.append(f"  Type: {payment.note}")

    if payment.kind == "upn" and payment.upn is not None:
        recipient_ref_invalid = any(
            e.startswith("recipient reference:") for e in payment.reference_errors
        )
        in_recipient = False
        for line in format_upn(payment.upn).splitlines():
            if line.startswith("==="):
                continue
            if line.strip() == "Recipient:":
                in_recipient = True
            elif line.strip() == "Payer:":
                in_recipient = False
            if in_recipient and recipient_ref_invalid and line.strip().startswith("Reference:"):
                line = f"{line}  [INVALID]"
            lines.append(line)

    elif payment.kind == "epc" and payment.epc is not None:
        for line in format_epc(payment.epc).splitlines():
            if line.startswith("==="):
                continue
            lines.append(line)

    if payment.reference_errors:
        lines += [
            "",
            "  Reference validation:",
            *[f"    - {e}" for e in payment.reference_errors],
        ]

    if payment.epc_payload:
        lines += ["", "  EPC payload (copy/paste fallback):"]
        lines += [f"    {l}" for l in payment.epc_payload.splitlines()]

    if payment.conversion_error:
        lines += ["", f"  EPC QR: NOT GENERATED (reason: {payment.conversion_error})"]
    else:
        if payment.epc_qr_png is None:
            lines += ["", "  EPC QR: NOT GENERATED (reason: image regeneration failed)"]
        else:
            lines += ["", "  EPC QR: see inline preview (if supported) and attachment"]

    return lines


def _build_payment_html_block(payment: PaymentItem, *, cid: Optional[str]) -> str:
    """HTML payment block used in forwarded mail."""
    text_lines = _build_payment_text_block(payment)
    pre = "<pre>" + html.escape("\n".join(text_lines)) + "</pre>"
    img = ""
    if cid and payment.epc_qr_png is not None:
        img = (
            f'<div style="margin: 8px 0 12px 0;">'
            f'<img alt="EPC QR" src="cid:{html.escape(cid)}" '
            f'style="max-width: 320px; width: 100%; height: auto; border: 1px solid #ccc;" />'
            f"</div>"
        )
    return img + pre


def build_forward(
    original: email.message.Message,
    sender_addr: str,
    my_address: str,
    admin_email: str,
    *,
    reply_to_sender: bool,
    to_addrs: Optional[list[str]] = None,
    cc_admin: bool = False,
    extra_warnings: Optional[list[str]] = None,
    payments: list[PaymentItem],
) -> MIMEMultipart:
    fwd = MIMEMultipart("mixed")
    fwd["From"] = my_address
    if reply_to_sender:
        fwd["To"] = sender_addr
        fwd["Cc"] = admin_email
        fwd["Subject"] = "Fwd: " + original.get("Subject", "(no subject)")
    else:
        if to_addrs:
            fwd["To"] = ", ".join(to_addrs)
            if cc_admin:
                fwd["Cc"] = admin_email
        else:
            fwd["To"] = admin_email
        fwd["Subject"] = "Fwd: " + original.get("Subject", "(no subject)")

    # First part: multipart/related with text/plain + text/html and inline EPC QR images (CID).
    related = MIMEMultipart("related")
    alternative = MIMEMultipart("alternative")
    related.attach(alternative)

    warnings: list[str] = list(extra_warnings or [])
    for i, p in enumerate(payments, start=1):
        if p.reference_errors:
            warnings.append(f"Payment {i} ({', '.join(p.sources) if p.sources else '?'}): invalid reference(s)")
        if p.conversion_error:
            warnings.append(f"Payment {i} ({', '.join(p.sources) if p.sources else '?'}): EPC QR not generated")

    # ── Plain text body ─────────────────────────────────────────────────────
    body_lines: list[str] = []
    if warnings:
        body_lines += ["WARNINGS:", *[f"  - {w}" for w in warnings], ""]

    if payments:
        for i, payment in enumerate(payments, start=1):
            src = ", ".join(payment.sources) if payment.sources else "(unknown source)"
            body_lines += [f"Payment details {i} (found in: {src}):", ""]
            body_lines += _build_payment_text_block(payment)
            body_lines += [""]

    body_lines += [
        "-------- Forwarded message --------",
        f"From:    {original.get('From', sender_addr)}",
        f"Subject: {original.get('Subject', '')}",
        f"Date:    {original.get('Date', '')}",
        "",
        *[f"> {line}" for line in extract_text_body(original).splitlines()],
    ]

    alternative.attach(MIMEText("\n".join(body_lines), "plain", "utf-8"))

    # ── HTML body ───────────────────────────────────────────────────────────
    html_parts: list[str] = []
    if warnings:
        html_parts.append("<h3>Warnings</h3>")
        html_parts.append("<ul>")
        for w in warnings:
            html_parts.append(f"<li>{html.escape(w)}</li>")
        html_parts.append("</ul>")

    if payments:
        html_parts.append("<h3>Payments</h3>")
        for i, payment in enumerate(payments, start=1):
            cid = f"payment_{i}_epc_qr"
            src = ", ".join(payment.sources) if payment.sources else "(unknown source)"
            html_parts.append(f"<h4>Payment {i} (found in: {html.escape(src)})</h4>")
            html_parts.append(_build_payment_html_block(payment, cid=cid))

    html_parts.append("<hr />")
    html_parts.append("<pre>")
    html_parts.append(html.escape("-------- Forwarded message --------\n"))
    html_parts.append(html.escape(f"From:    {original.get('From', sender_addr)}\n"))
    html_parts.append(html.escape(f"Subject: {original.get('Subject', '')}\n"))
    html_parts.append(html.escape(f"Date:    {original.get('Date', '')}\n\n"))
    html_parts.append(html.escape(extract_text_body(original)))
    html_parts.append("</pre>")

    alternative.attach(
        MIMEText(
            "<html><body>" + "\n".join(html_parts) + "</body></html>",
            "html",
            "utf-8",
        )
    )

    # Inline images for HTML via CID.
    for i, payment in enumerate(payments, start=1):
        if payment.epc_qr_png is None:
            continue
        img_part = MIMEImage(payment.epc_qr_png, _subtype="png")
        img_part.add_header("Content-ID", f"<payment_{i}_epc_qr>")
        img_part.add_header("Content-Disposition", "inline", filename=f"payment_{i}_epc_qr.png")
        related.attach(img_part)

    fwd.attach(related)

    # ── EPC QR attachments ────────────────────────────────────────────────────
    for i, payment in enumerate(payments, start=1):
        if payment.epc_qr_png is None:
            continue
        img_part = MIMEImage(payment.epc_qr_png, _subtype="png")
        img_part.add_header(
            "Content-Disposition", "attachment",
            filename=f"payment_{i}_epc_qr.png",
        )
        fwd.attach(img_part)

    # ── Original attachments (preserved) ─────────────────────────────────────
    for part in original.walk():
        # Skip container parts and plain-text/html body parts
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() in ("text/plain", "text/html"):
            if part.get("Content-Disposition", "").strip().lower() != "attachment":
                continue

        payload = part.get_payload(decode=True)
        if payload is None:
            continue

        orig_part = MIMEBase(
            part.get_content_maintype(),
            part.get_content_subtype(),
        )
        orig_part.set_payload(payload)
        email.encoders.encode_base64(orig_part)

        filename = part.get_filename()
        if filename:
            orig_part.add_header("Content-Disposition", "attachment", filename=filename)
        else:
            orig_part.add_header("Content-Disposition", "attachment")

        fwd.attach(orig_part)

    return fwd


def main() -> None:
    admin_email, my_address, allowed_raw, routes_raw, trusted_senders, max_bytes = load_config()
    allowed_patterns = parse_allowed_senders(allowed_raw)
    try:
        routes = parse_allowed_sender_routes(routes_raw)
    except ValueError as exc:
        print(f"ERROR: invalid ALLOWED_SENDER_ROUTES: {exc}", file=sys.stderr)
        sys.exit(1)

    raw = sys.stdin.buffer.read()
    msg = email.message_from_bytes(raw)

    _, sender_addr = parseaddr(msg.get("From", ""))
    is_trusted = bool(sender_addr) and any(matches(sender_addr, p) for p in trusted_senders)
    route = find_route(sender_addr, routes) if sender_addr else None
    allowed = bool(sender_addr) and (is_allowed_sender(sender_addr, allowed_patterns) or route is not None)
    if not sender_addr or not (is_trusted or allowed):
        sys.exit(0)

    payments: list[PaymentItem] = []
    extra_warnings: list[str] = []
    runtime_s = int(os.environ.get("MAX_MESSAGE_RUNTIME_S", DEFAULT_MAX_MESSAGE_RUNTIME_S))
    if runtime_s > 0:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(runtime_s)
    try:
        qr_results = scan_attachments(msg, max_bytes)
        log.info("QR codes found (raw): %d", len(qr_results))
        qr_unique = dedupe_qr_results(qr_results)
        log.info("QR codes found (unique): %d", len(qr_unique))

        payments = find_payments(qr_unique)
        log.info("Payment QR items: %d", len(payments))
    except MessageProcessingTimeout as exc:
        log.warning("Message processing timed out: %s", exc)
        extra_warnings.append(f"Processing timed out after {runtime_s}s; results may be incomplete.")
        payments = []
    finally:
        if runtime_s > 0:
            signal.alarm(0)

    to_addrs: Optional[list[str]] = None
    cc_admin = False
    if not is_trusted and route and route.to_addrs:
        # Route to configured recipients and CC admin (admin should not be in To).
        routed = [a for a in route.to_addrs if a.lower() != admin_email.lower()]
        if routed:
            to_addrs = routed
            cc_admin = True

    fwd = build_forward(
        msg,
        sender_addr,
        my_address,
        admin_email,
        reply_to_sender=is_trusted,
        to_addrs=to_addrs,
        cc_admin=cc_admin,
        extra_warnings=extra_warnings,
        payments=payments,
    )

    # Inject via sendmail binary — queues directly into Postfix spool,
    # no live SMTP connection needed. On NixOS: /run/wrappers/bin/sendmail
    if is_trusted:
        recipients = [sender_addr, admin_email]
    else:
        recipients = [admin_email]
        if to_addrs:
            recipients = [*to_addrs, admin_email]
        else:
            recipients = [admin_email]

    recipients = list(dict.fromkeys(recipients))  # dedupe, preserve order
    subprocess.run(["sendmail", "-f", my_address, *recipients], input=fwd.as_bytes(), check=True)

    # Alternative: send via SMTP to localhost:25 (requires localhost in mynetworks)
    # with smtplib.SMTP("localhost") as smtp:
    #     smtp.sendmail(my_address, [admin_email], fwd.as_bytes())


if __name__ == "__main__":
    main()
