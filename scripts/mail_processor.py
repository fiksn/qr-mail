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
  MAX_EMAIL_BYTES        split forwarded mail into multiple messages when the
                         generated payment artifacts would exceed this size
                         (default: 20971520 = 20 MB)
  EPC_TO_UPN_CITY        recipient city used when converting EPC → UPN (default: Ljubljana)
  MAX_PDF_PAGES          max PDF pages rendered per attachment (default: 10)
  PDF_RENDER_DPI         DPI for PDF→image rendering (default: 200)
  PDF_RENDER_TIMEOUT_S   pdf2image/poppler render timeout seconds (default: 20)
  PDFINFO_TIMEOUT_S      pdfinfo timeout seconds when checking PDF encryption (default: 3)
  MAX_IMAGE_PIXELS       PIL image pixel limit / decompression bomb guard (default: 40000000)
  MAX_MESSAGE_RUNTIME_S  max total processing time per message (default: 60)
  SMTP_HOST              if set, send via SMTP instead of sendmail
  SMTP_PORT              SMTP port (default: 587)
  SMTP_USER              SMTP username for auth (optional)
  SMTP_PASSWORD          SMTP password for auth (optional)
  SMTP_TLS               TLS mode: starttls (default), tls (implicit/SMTPS), or none
  SMTP_INSECURE_SKIP_VERIFY
                         when "true", disable SMTP certificate and hostname
                         verification (discouraged; default: false)
  GMAIL_IMPERSONATE_ADDRESS
                         mailbox to act on for the Gmail transport. When any
                         Gmail credentials are configured, results are inserted
                         as artificial replies into this mailbox (threaded onto
                         the original) instead of being sent via sendmail/SMTP.
                         Replies are only inserted when payment data is found.
  GMAIL_SERVICE_ACCOUNT_FILE
                         service-account JSON key → domain-wide delegation mode
  GMAIL_OAUTH_CLIENT_SECRET_FILE
                         OAuth client-secret JSON → single-user OAuth mode
                         (used for the one-time consent flow)
  GMAIL_OAUTH_TOKEN_FILE
                         cached OAuth token JSON (runtime credential for OAuth mode)
  QRMAIL_GMAIL_THREAD_ID
                         Gmail thread ID of the message being processed, so the
                         inserted reply lands in the same conversation (set by
                         the Gmail fetch daemon)
  MAX_MESSAGE_BYTES      max raw message size to read from stdin (default: 157286400 = 150 MB)

Standalone usage:
  ADMIN_EMAIL=admin@example.com \
  MY_ADDRESS=test@example.com \
  ALLOWED_SENDERS="*@trusted.com:alice@*" \
  python3 scripts/mail_processor.py [envelope-sender] < message.eml

When called from Postfix pipe transport, pass the envelope sender as the
first argument (${sender} in master.cf).  If omitted, the RFC 2822 From
header is used (less secure — the From header is trivially spoofable).
"""
from __future__ import annotations

import contextlib
import email
import email.encoders
import html
import io
import hashlib
import logging
import os
import re
import signal
import smtplib
import ssl
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from typing import Any, Optional

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.epc import EPC, EPCParseError, format_epc, parse_epc
from core.generate import epc_to_string, upn_to_epc
from core.routing import (
    find_route,
    is_allowed_sender,
    matches,
    parse_allowed_sender_routes,
    parse_allowed_senders,
)
from core.upn import (
    UPN,
    UPNParseError,
    UPNReferenceError,
    format_upn,
    parse_upn,
    validate_upn_reference,
)
from parsers.eslog import ESlogParseError, parse_eslog_invoice
from parsers.icl_envelope import ICLEnvelopeParseError, parse_icl_envelope
from parsers.pain import PainParseError, parse_pain_credit_transfers
from parsers.xmldsig import SignatureResult, verify_eslog_signature
from parsers.text_extract import (
    build_upn_from_text,
    extract_image_text_from_bytes,
    extract_pdf_text,
    find_iban_reference_pairs,
)
from scripts.generate_qr import generate_epc_qr_labeled, generate_upn_slip_png

# Default to INFO: this runs as a Postfix pipe(8) command, whose stderr is
# captured into a bounded buffer for bounce diagnostics. DEBUG-level output
# (raw QR bytes, full payment dumps) overruns that buffer; Postfix then closes
# the pipe, and the interpreter's shutdown flush dies with BrokenPipeError —
# surfacing as "Command died with status 120" even though delivery succeeded.
# Override with QR_MAIL_LOG_LEVEL=DEBUG for local debugging.
_LOG_LEVEL = getattr(logging, os.environ.get("QR_MAIL_LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    stream=sys.stderr, level=_LOG_LEVEL,
    format="qr-mail: %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_MAX_MESSAGE_BYTES = 150 * 1024 * 1024  # 150 MB
DEFAULT_MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100 MB
DEFAULT_MAX_EMAIL_BYTES = 20 * 1024 * 1024  # 20 MB — split outbound mail past this
DEFAULT_MAX_PDF_PAGES = 10
DEFAULT_PDF_RENDER_DPI = 200
DEFAULT_PDF_RENDER_TIMEOUT_S = 20
# Keep conservative to avoid PIL DecompressionBomb warnings/errors on malicious inputs.
DEFAULT_MAX_IMAGE_PIXELS = 40_000_000
DEFAULT_MAX_MESSAGE_RUNTIME_S = 60
DEFAULT_PDFINFO_TIMEOUT_S = 3
_AUTH_PASS_RE = re.compile(r"(?<![\w-])([a-z]+)=pass(?:[\s;(]|$)", re.IGNORECASE)
_AUTH_PARAM_RE = re.compile(r"([A-Za-z0-9_.-]+)=([^;\s]+)")


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    tls: str  # "starttls", "tls", or "none"


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
    upn_slip_png: Optional[bytes] = None

    conversion_error: Optional[str] = None
    reference_errors: list[str] = field(default_factory=list)
    signature: Optional[SignatureResult] = None


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


def _email_domain(addr: str) -> str:
    if addr.count("@") != 1:
        return ""
    return addr.rsplit("@", 1)[1].lower()


def _auth_method_passed(header_value: str, method: str) -> bool:
    for matched_method in _AUTH_PASS_RE.findall(header_value):
        if matched_method.lower() == method.lower():
            return True
    return False


def _auth_param_values(header_value: str, key: str) -> list[str]:
    values: list[str] = []
    needle = key.lower()
    for param_key, param_value in _AUTH_PARAM_RE.findall(header_value):
        if param_key.lower() == needle:
            values.append(param_value.strip())
    return values


def _normalize_auth_identity(raw: str) -> str:
    value = raw.strip().strip("<>").strip().lower()
    if not value:
        return ""
    if value.startswith("@"):
        return value
    _, addr = parseaddr(value)
    return addr.lower() if addr else value


def _derive_verified_gmail_sender(msg: email.message.Message) -> str:
    """Return the RFC 2822 From address only if Gmail auth results align with it."""
    _, from_addr = parseaddr(msg.get("From", ""))
    from_addr = from_addr.strip().lower()
    from_domain = _email_domain(from_addr)
    if not from_addr or not from_domain:
        return ""

    # Only trust the topmost Authentication-Results header — the one stamped by
    # the receiving boundary (Gmail) at delivery. Headers appear newest-first, so
    # index 0 is Gmail's verdict; any Authentication-Results lines below it were
    # present in the message before it reached Gmail and are attacker-forgeable.
    # Trusting all of them would let a sender spoof "dmarc=pass" for any domain.
    auth_headers = (msg.get_all("Authentication-Results", []) or [])[:1] + (
        msg.get_all("ARC-Authentication-Results", []) or []
    )[:1]
    if not auth_headers:
        log.warning("Gmail message lacks Authentication-Results headers")
        return ""

    exact_match = False
    aligned_domain_pass = False
    for header_value in auth_headers:
        if _auth_method_passed(header_value, "dmarc"):
            for header_from in _auth_param_values(header_value, "header.from"):
                if _normalize_auth_identity(header_from).lstrip("@") == from_domain:
                    aligned_domain_pass = True

        if _auth_method_passed(header_value, "spf"):
            for mailfrom in _auth_param_values(header_value, "smtp.mailfrom"):
                identity = _normalize_auth_identity(mailfrom)
                if not identity:
                    continue
                if _email_domain(identity) == from_domain:
                    aligned_domain_pass = True
                if identity == from_addr:
                    exact_match = True

        if _auth_method_passed(header_value, "dkim"):
            for header_i in _auth_param_values(header_value, "header.i"):
                identity = _normalize_auth_identity(header_i)
                if not identity:
                    continue
                if identity.startswith("@"):
                    if identity[1:] == from_domain:
                        aligned_domain_pass = True
                elif _email_domain(identity) == from_domain:
                    aligned_domain_pass = True
                    if identity == from_addr:
                        exact_match = True
            for header_d in _auth_param_values(header_value, "header.d"):
                if _normalize_auth_identity(header_d).lstrip("@") == from_domain:
                    aligned_domain_pass = True

    if exact_match or aligned_domain_pass:
        return from_addr

    log.warning("Gmail From address %r is not backed by aligned SPF/DKIM/DMARC pass", from_addr)
    return ""


def _decode_qr_bytes(data: bytes) -> str:
    """Decode QR payload bytes. Try UTF-8 first (strict); fall back to ISO 8859-2.

    The UPN spec mandates ISO 8859-2 (ECI 000004), but many modern generators
    emit UTF-8 without an ECI marker. Since valid UTF-8 multi-byte sequences
    are not valid ISO 8859-2 text for the same characters, trying UTF-8 first
    avoids misinterpreting Slovenian/Croatian diacritics.
    """
    try:
        return data.decode("utf-8")
    except (UnicodeDecodeError, LookupError):
        return data.decode("iso-8859-2", errors="replace")


@contextlib.contextmanager
def _suppress_fd_stderr() -> Any:
    """Redirect OS-level fd 2 to /dev/null for the duration of the block.

    ZBar's C code writes diagnostics straight to fd 2, bypassing sys.stderr.
    That binary noise pollutes Postfix bounce diagnostics, so silence it unless
    DEBUG logging is requested.
    """
    if log.isEnabledFor(logging.DEBUG):
        yield
        return
    sys.stderr.flush()
    saved_fd = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(saved_fd, 2)
        os.close(devnull)
        os.close(saved_fd)


def scan_image_for_qr(img: Any) -> list[str]:
    try:
        from pyzbar import pyzbar

        with _suppress_fd_stderr():
            results = pyzbar.decode(img, symbols=[pyzbar.ZBarSymbol.QRCODE])
        return [_decode_qr_bytes(r.data) for r in results]
    except ImportError:
        log.info("pyzbar not installed; QR decoding disabled")
        return []
    except Exception as exc:
        log.warning("pyzbar decode error: %s", exc)
        return []


def scan_pdf_for_qr(data: bytes) -> list[tuple[str, int]]:
    """Render each PDF page and scan for QR codes."""
    codes: list[tuple[str, int]] = []
    try:
        import pdf2image
        from pdf2image.exceptions import PDFPageCountError

        max_pages = int(os.environ.get("MAX_PDF_PAGES", DEFAULT_MAX_PDF_PAGES))
        dpi = int(os.environ.get("PDF_RENDER_DPI", DEFAULT_PDF_RENDER_DPI))
        timeout_s = int(os.environ.get("PDF_RENDER_TIMEOUT_S", DEFAULT_PDF_RENDER_TIMEOUT_S))
        pages = pdf2image.convert_from_bytes(
            data,
            dpi=dpi,
            first_page=1,
            last_page=max_pages,
            timeout=timeout_s,
        )
    except ImportError:
        log.info("pdf2image not installed; PDF QR scanning disabled")
        return codes
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
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", DEFAULT_MAX_IMAGE_PIXELS))
        img = Image.open(io.BytesIO(data))
        img.load()
    except ImportError:
        log.info("Pillow not installed; image QR scanning disabled")
        return []
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


def _build_upn_payment(
    upn: UPN,
    *,
    sources: list[str],
    note_prefix: str = "UPN QR",
) -> PaymentItem:
    """Build a PaymentItem from a parsed UPN: validate refs, generate EPC QR + slip."""
    source_label = sources[0] if sources else "<unknown>"
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

    slip_png: Optional[bytes] = None
    try:
        slip_png = generate_upn_slip_png(upn)
    except Exception as exc:
        log.warning("UPN slip generation failed for %r: %s", source_label, exc)

    try:
        epc = upn_to_epc(upn)
        payload = epc_to_string(epc)
        png = generate_epc_qr_labeled(epc)
        return PaymentItem(
            sources=sources,
            kind="upn",
            note=f"{note_prefix} (converted to EPC SCT)",
            upn=upn,
            epc=epc,
            epc_qr_png=png,
            epc_payload=payload,
            upn_slip_png=slip_png,
            conversion_error=None,
            reference_errors=ref_errors,
        )
    except Exception as exc:
        log.warning("UPN→EPC conversion failed for %r: %s", source_label, exc)
        return PaymentItem(
            sources=sources,
            kind="upn",
            note=f"{note_prefix} (conversion to EPC SCT failed)",
            upn=upn,
            epc=None,
            epc_qr_png=None,
            epc_payload=None,
            upn_slip_png=slip_png,
            conversion_error=str(exc),
            reference_errors=ref_errors,
        )


def find_payments(qr_results: list[tuple[str, list[str]]]) -> list[PaymentItem]:
    """Parse QR codes and return a list of UPN (converted) and EPC (direct) items."""
    payments: list[PaymentItem] = []

    for qr_text, sources in qr_results:
        try:
            upn = parse_upn(qr_text)
            log.info("UPN found in %r:\n%s", sources[0] if sources else "<unknown>", format_upn(upn))
            payments.append(_build_upn_payment(upn, sources=sources))
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
                png = generate_epc_qr_labeled(epc)
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
            log.debug("QR in %r is neither UPN nor EPC", sources[0] if sources else "<unknown>")

    return payments


def _payment_dedup_key(payment: PaymentItem) -> Optional[tuple[str, str]]:
    """Return (iban, reference) key for deduplication, or None."""
    if payment.upn is not None:
        iban = payment.upn.recipient_iban.strip().upper()
        ref = payment.upn.recipient_reference.strip().upper().replace(" ", "")
        return (iban, ref)
    if payment.epc is not None:
        iban = payment.epc.beneficiary_iban.strip().upper()
        ref = (payment.epc.structured_ref or "").strip().upper().replace(" ", "")
        return (iban, ref)
    return None


def _merge_payments_with_precedence(
    text_upns: list[tuple[UPN, str]],
    eslog_upns: list[tuple[UPN, str, Optional[SignatureResult]]],
    qr_payments: list[PaymentItem],
    envelope_upns: Optional[list[tuple[UPN, str]]] = None,
    pain_upns: Optional[list[tuple[UPN, str]]] = None,
) -> list[PaymentItem]:
    """Merge payments by key with precedence.

    text < envelope < eSLOG XML < pain.001 < QR.
    """
    merged: dict[tuple[str, str], tuple[int, PaymentItem]] = {}
    extras: list[PaymentItem] = []

    def add(payment: PaymentItem, rank: int) -> None:
        key = _payment_dedup_key(payment)
        if key is None:
            extras.append(payment)
            return
        existing = merged.get(key)
        if existing is None or rank >= existing[0]:
            merged[key] = (rank, payment)

    for upn, source in text_upns:
        add(_build_upn_payment(upn, sources=[source], note_prefix="Text-extracted"), rank=1)

    for upn, source in envelope_upns or []:
        add(_build_upn_payment(upn, sources=[source], note_prefix="e-račun envelope"), rank=2)

    for upn, source, sig in eslog_upns:
        p = _build_upn_payment(upn, sources=[source], note_prefix="eSLOG XML")
        p.signature = sig
        add(p, rank=3)

    for upn, source in pain_upns or []:
        add(_build_upn_payment(upn, sources=[source], note_prefix="pain.001"), rank=4)

    for payment in qr_payments:
        add(payment, rank=5)

    return extras + [payment for _, payment in merged.values()]


def scan_text_for_payments(
    msg: email.message.Message,
    max_bytes: int,
) -> list[tuple[UPN, str]]:
    """Extract UPN candidates from body text, PDF text, and image OCR.

    Returns (upn, source_label) tuples, deduplicated by (iban, reference).
    """
    recipient_city = os.environ.get("EPC_TO_UPN_CITY", "Ljubljana")
    all_pairs: list[tuple[str, str, str]] = []  # (iban, ref, source)

    # 1. Email body text.
    body_text = extract_text_body(msg)
    for iban, ref in find_iban_reference_pairs(body_text):
        all_pairs.append((iban, ref, "email-body"))

    # 2. Attachment text (PDFs and images).
    for part in msg.walk():
        content_type = part.get_content_type()
        filename = part.get_filename() or f"<{content_type}>"

        is_image = content_type.startswith("image/")
        is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")
        if not (is_image or is_pdf):
            continue

        payload = part.get_payload(decode=True)
        if payload is None or len(payload) > max_bytes:
            continue

        if is_pdf:
            text = extract_pdf_text(payload)
            if text.strip():
                for iban, ref in find_iban_reference_pairs(text):
                    all_pairs.append((iban, ref, f"{filename} (text)"))

        if is_image:
            text = extract_image_text_from_bytes(payload)
            if text.strip():
                for iban, ref in find_iban_reference_pairs(text):
                    all_pairs.append((iban, ref, f"{filename} (OCR)"))

    # Deduplicate within text-extracted results.
    seen: set[tuple[str, str]] = set()
    results: list[tuple[UPN, str]] = []
    for iban, ref, source in all_pairs:
        key = (iban, ref)
        if key in seen:
            continue
        seen.add(key)
        upn = build_upn_from_text(iban, ref, recipient_city=recipient_city)
        results.append((upn, source))

    return results


def scan_eslog_xml_for_payments(
    msg: email.message.Message,
    max_bytes: int,
) -> list[tuple[UPN, str, Optional[SignatureResult]]]:
    """Extract UPN candidates from supported eSLOG XML attachments.

    Returns (upn, source_label, signature_result) tuples.
    Rejects (skips) XML attachments with invalid signatures.
    """
    results: list[tuple[UPN, str, Optional[SignatureResult]]] = []
    seen: set[tuple[str, str]] = set()

    for part in msg.walk():
        content_type = part.get_content_type()
        filename = part.get_filename() or f"<{content_type}>"
        is_xml = (
            content_type in ("application/xml", "text/xml")
            or filename.lower().endswith(".xml")
        )
        if not is_xml:
            continue

        payload = part.get_payload(decode=True)
        if payload is None or len(payload) > max_bytes:
            continue

        try:
            upn = parse_eslog_invoice(payload)
        except ESlogParseError:
            log.debug("XML attachment %r is not a supported eSLOG invoice", filename)
            continue
        except Exception as exc:
            log.warning("Failed to parse XML attachment %r: %s", filename, exc)
            continue

        sig = verify_eslog_signature(payload)
        if sig.valid is False:
            log.warning(
                "Rejecting eSLOG %r: invalid signature (%s)",
                filename, sig.error,
            )
            continue

        if not sig.signed:
            log.info("eSLOG %r: unsigned document", filename)
        elif sig.valid is True:
            signer_cn = sig.signer.subject if sig.signer else "?"
            log.info("eSLOG %r: valid signature (%s)", filename, signer_cn)

        key = (upn.recipient_iban, upn.recipient_reference)
        if key in seen:
            continue
        seen.add(key)
        results.append((upn, f"{filename} (eSLOG XML)", sig))

    return results


def scan_icl_envelopes_for_payments(
    msg: email.message.Message,
    max_bytes: int,
) -> list[tuple[UPN, str]]:
    """Extract fallback UPN candidates from ICL e-račun envelope XML attachments."""
    results: list[tuple[UPN, str]] = []
    seen: set[tuple[str, str]] = set()

    for part in msg.walk():
        content_type = part.get_content_type()
        filename = part.get_filename() or f"<{content_type}>"
        is_xml = (
            content_type in ("application/xml", "text/xml")
            or filename.lower().endswith(".xml")
        )
        if not is_xml:
            continue

        payload = part.get_payload(decode=True)
        if payload is None or len(payload) > max_bytes:
            continue

        try:
            upn = parse_icl_envelope(payload)
        except ICLEnvelopeParseError:
            log.debug("XML attachment %r is not a supported ICL envelope", filename)
            continue
        except Exception as exc:
            log.warning("Failed to parse ICL envelope %r: %s", filename, exc)
            continue

        key = (upn.recipient_iban, upn.recipient_reference)
        if key in seen:
            continue
        seen.add(key)
        results.append((upn, f"{filename} (e-račun envelope)"))

    return results


def scan_pain_xml_for_payments(
    msg: email.message.Message,
    max_bytes: int,
) -> list[tuple[UPN, str]]:
    """Extract UPN candidates from ISO 20022 pain.001 XML attachments.

    A single pain.001 batch can hold many credit transfers; each becomes its
    own UPN (and downstream its own invoice + QR code).
    """
    recipient_city = os.environ.get("EPC_TO_UPN_CITY", "Ljubljana")
    results: list[tuple[UPN, str]] = []
    seen: set[tuple[str, str]] = set()

    for part in msg.walk():
        content_type = part.get_content_type()
        filename = part.get_filename() or f"<{content_type}>"
        is_xml = (
            content_type in ("application/xml", "text/xml")
            or filename.lower().endswith(".xml")
        )
        if not is_xml:
            continue

        payload = part.get_payload(decode=True)
        if payload is None or len(payload) > max_bytes:
            continue

        try:
            upns = parse_pain_credit_transfers(payload)
        except PainParseError:
            log.debug("XML attachment %r is not a supported pain.001 message", filename)
            continue
        except Exception as exc:
            log.warning("Failed to parse pain.001 attachment %r: %s", filename, exc)
            continue

        for index, upn in enumerate(upns, start=1):
            if not upn.recipient_city:
                upn = replace(upn, recipient_city=recipient_city)
            key = (upn.recipient_iban, upn.recipient_reference)
            if key in seen:
                continue
            seen.add(key)
            results.append((upn, f"{filename} (pain.001 #{index})"))

    return results


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
        lines += [f"    {line}" for line in payment.epc_payload.splitlines()]

    if payment.conversion_error:
        lines += ["", f"  EPC QR: NOT GENERATED (reason: {payment.conversion_error})"]
    else:
        if payment.epc_qr_png is None:
            lines += ["", "  EPC QR: NOT GENERATED (reason: image regeneration failed)"]
        else:
            lines += ["", "  EPC QR: see inline preview (if supported) and attachment"]

    if payment.upn_slip_png is not None:
        lines += ["  UPN slip: see inline preview (if supported) and attachment"]
    elif payment.kind == "upn":
        lines += ["  UPN slip: NOT GENERATED"]

    # Signature info — only present for eSLOG XML sources.
    if payment.signature is not None:
        sig = payment.signature
        lines.append("")
        if not sig.signed:
            lines.append("  eSLOG signature: UNSIGNED")
        elif sig.valid is True and sig.signer:
            lines.append("  eSLOG signature: VALID")
            lines.append(f"    Signer:  {sig.signer.subject}")
            lines.append(f"    Issuer:  {sig.signer.issuer}")
            lines.append(f"    Valid:   {sig.signer.not_before} — {sig.signer.not_after}")
            if sig.signer.signing_time:
                lines.append(f"    Signed:  {sig.signer.signing_time}")
            if sig.chain:
                lines.append("    Chain:")
                for idx, cert in enumerate(sig.chain, start=1):
                    lines.append(f"      {idx}. Subject: {cert.subject}")
                    lines.append(f"         Issuer:  {cert.issuer}")
                    lines.append(f"         Valid:   {cert.not_before} — {cert.not_after}")
        elif sig.valid is None:
            lines.append(f"  eSLOG signature: COULD NOT VERIFY ({sig.error})")

    return lines


def _build_payment_html_block(
    payment: PaymentItem,
    *,
    cid: Optional[str],
    slip_cid: Optional[str] = None,
) -> str:
    """HTML payment block used in forwarded mail."""
    text_lines = _build_payment_text_block(payment)
    pre = "<pre>" + html.escape("\n".join(text_lines)) + "</pre>"
    epc_img = ""
    if cid and payment.epc_qr_png is not None:
        epc_img = (
            f'<div style="margin: 8px 0 12px 0;">'
            f'<img alt="EPC QR" src="cid:{html.escape(cid)}" '
            f'style="max-width: 320px; width: 100%; height: auto; border: 1px solid #ccc;" />'
            f"</div>"
        )
    slip_img = ""
    if slip_cid and payment.upn_slip_png is not None:
        slip_img = (
            f'<div style="margin: 8px 0 12px 0;">'
            f'<img alt="UPN payment slip" src="cid:{html.escape(slip_cid)}" '
            f'style="max-width: 900px; width: 100%; height: auto; border: 1px solid #ccc;" />'
            f"</div>"
        )
    return epc_img + slip_img + pre


def _payment_encoded_size(payment: PaymentItem) -> int:
    """Approximate the bytes a payment adds to an email.

    Each generated PNG is carried twice (inline CID preview + attachment) and
    base64 inflates binary payloads by roughly 4/3.
    """
    raw = 0
    if payment.epc_qr_png is not None:
        raw += len(payment.epc_qr_png) * 2
    if payment.upn_slip_png is not None:
        raw += len(payment.upn_slip_png) * 2
    return raw * 4 // 3


def _original_attachments_size(original: email.message.Message) -> int:
    """Approximate the base64-encoded size of preserved original attachments."""
    total = 0
    for part in original.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() in ("text/plain", "text/html"):
            if part.get("Content-Disposition", "").strip().lower() != "attachment":
                continue
        payload = part.get_payload(decode=True)
        if payload is not None:
            total += len(payload) * 4 // 3
    return total


def plan_payment_batches(
    payments: list[PaymentItem],
    max_email_bytes: int,
    first_batch_reserved: int,
) -> list[list[PaymentItem]]:
    """Split payments into batches that each fit within max_email_bytes.

    The first batch also carries the preserved original attachments, so its
    payment budget is reduced by first_batch_reserved. Each batch holds at least
    one payment even if that single payment exceeds the budget.
    """
    if not payments:
        return [[]]

    batches: list[list[PaymentItem]] = []
    current: list[PaymentItem] = []
    current_size = 0
    budget = max(0, max_email_bytes - first_batch_reserved)

    for payment in payments:
        size = _payment_encoded_size(payment)
        if current and current_size + size > budget:
            batches.append(current)
            current = []
            current_size = 0
            budget = max_email_bytes
        current.append(payment)
        current_size += size

    batches.append(current)
    return batches


def collect_payment_warnings(
    payments: list[PaymentItem], extra_warnings: Optional[list[str]] = None
) -> list[str]:
    """Build the warning list shown atop a forward/reply."""
    warnings: list[str] = list(extra_warnings or [])
    for i, p in enumerate(payments, start=1):
        src = ", ".join(p.sources) if p.sources else "?"
        if p.reference_errors:
            warnings.append(f"Payment {i} ({src}): invalid reference(s)")
        if p.conversion_error:
            warnings.append(f"Payment {i} ({src}): EPC QR not generated")
    return warnings


def payments_section_text(payments: list[PaymentItem]) -> list[str]:
    """Plain-text payment blocks shared by forwards and replies."""
    lines: list[str] = []
    for i, payment in enumerate(payments, start=1):
        src = ", ".join(payment.sources) if payment.sources else "(unknown source)"
        lines += [f"Payment details {i} (found in: {src}):", ""]
        lines += _build_payment_text_block(payment)
        lines += [""]
    return lines


def payments_section_html(payments: list[PaymentItem]) -> list[str]:
    """HTML payment blocks shared by forwards and replies."""
    parts: list[str] = ["<h3>Payments</h3>"]
    for i, payment in enumerate(payments, start=1):
        cid = f"payment_{i}_epc_qr"
        slip_cid = f"payment_{i}_upn_slip" if payment.upn_slip_png is not None else None
        src = ", ".join(payment.sources) if payment.sources else "(unknown source)"
        parts.append(f"<h4>Payment {i} (found in: {html.escape(src)})</h4>")
        parts.append(_build_payment_html_block(payment, cid=cid, slip_cid=slip_cid))
    return parts


def attach_payment_artifacts(
    related: MIMEMultipart,
    container: MIMEMultipart,
    payments: list[PaymentItem],
) -> None:
    """Attach inline CID previews to ``related`` and downloadable copies to ``container``."""
    for i, payment in enumerate(payments, start=1):
        if payment.epc_qr_png is not None:
            inline = MIMEImage(payment.epc_qr_png, _subtype="png")
            inline.add_header("Content-ID", f"<payment_{i}_epc_qr>")
            inline.add_header("Content-Disposition", "inline", filename=f"payment_{i}_epc_qr.png")
            related.attach(inline)
        if payment.upn_slip_png is not None:
            inline = MIMEImage(payment.upn_slip_png, _subtype="png")
            inline.add_header("Content-ID", f"<payment_{i}_upn_slip>")
            inline.add_header("Content-Disposition", "inline", filename=f"payment_{i}_upn_slip.png")
            related.attach(inline)

    for i, payment in enumerate(payments, start=1):
        if payment.epc_qr_png is not None:
            att = MIMEImage(payment.epc_qr_png, _subtype="png")
            att.add_header("Content-Disposition", "attachment", filename=f"payment_{i}_epc_qr.png")
            container.attach(att)
        if payment.upn_slip_png is not None:
            att = MIMEImage(payment.upn_slip_png, _subtype="png")
            att.add_header("Content-Disposition", "attachment", filename=f"payment_{i}_upn_slip.png")
            container.attach(att)


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
    include_originals: bool = True,
    part_info: Optional[tuple[int, int]] = None,
) -> MIMEMultipart:
    subject = "Fwd: " + original.get("Subject", "(no subject)")
    if part_info is not None and part_info[1] > 1:
        subject = f"{subject} (part {part_info[0]}/{part_info[1]})"

    fwd = MIMEMultipart("mixed")
    fwd["From"] = my_address
    if reply_to_sender:
        fwd["To"] = sender_addr
        fwd["Cc"] = admin_email
        fwd["Subject"] = subject
    else:
        if to_addrs:
            fwd["To"] = ", ".join(to_addrs)
            if cc_admin:
                fwd["Cc"] = admin_email
        else:
            fwd["To"] = admin_email
        fwd["Subject"] = subject

    # First part: multipart/related with text/plain + text/html and inline EPC QR images (CID).
    related = MIMEMultipart("related")
    alternative = MIMEMultipart("alternative")
    related.attach(alternative)

    warnings = collect_payment_warnings(payments, extra_warnings)

    # ── Plain text body ─────────────────────────────────────────────────────
    body_lines: list[str] = []
    if warnings:
        body_lines += ["WARNINGS:", *[f"  - {w}" for w in warnings], ""]

    if payments:
        body_lines += payments_section_text(payments)

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
        html_parts += payments_section_html(payments)

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

    fwd.attach(related)
    attach_payment_artifacts(related, fwd, payments)

    # ── Original attachments (preserved) ─────────────────────────────────────
    if not include_originals:
        return fwd
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


def _load_smtp_config() -> Optional[SmtpConfig]:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return None
    return SmtpConfig(
        host=host,
        port=int(os.environ.get("SMTP_PORT", "587")),
        user=os.environ.get("SMTP_USER", ""),
        password=os.environ.get("SMTP_PASSWORD", ""),
        tls=os.environ.get("SMTP_TLS", "starttls").lower(),
    )


def _smtp_skip_verify() -> bool:
    return os.environ.get("SMTP_INSECURE_SKIP_VERIFY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _build_smtp_tls_context() -> ssl.SSLContext:
    if _smtp_skip_verify():
        log.warning("SMTP TLS certificate verification is DISABLED")
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def scan_message_for_payments(
    msg: email.message.Message,
    *,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_runtime_s: int = DEFAULT_MAX_MESSAGE_RUNTIME_S,
) -> tuple[list[PaymentItem], list[str]]:
    """Scan a parsed RFC 2822 message for UPN/EPC payment data.

    Returns (payments, extra_warnings). extra_warnings carries a single
    notice when the scan hits the time limit.
    """
    payments: list[PaymentItem] = []
    extra_warnings: list[str] = []
    if max_runtime_s > 0:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(max_runtime_s)
    try:
        text_upns = scan_text_for_payments(msg, max_bytes)
        log.info("Text-extracted payment candidates: %d", len(text_upns))
        eslog_upns = scan_eslog_xml_for_payments(msg, max_bytes)
        log.info("eSLOG XML payment candidates: %d", len(eslog_upns))
        envelope_upns = scan_icl_envelopes_for_payments(msg, max_bytes)
        log.info("e-račun envelope payment candidates: %d", len(envelope_upns))
        pain_upns = scan_pain_xml_for_payments(msg, max_bytes)
        log.info("pain.001 payment candidates: %d", len(pain_upns))
        qr_results = scan_attachments(msg, max_bytes)
        log.info("QR codes found (raw): %d", len(qr_results))
        qr_unique = dedupe_qr_results(qr_results)
        log.info("QR codes found (unique): %d", len(qr_unique))
        qr_payments = find_payments(qr_unique)
        log.info("Payment QR items: %d", len(qr_payments))
        payments = _merge_payments_with_precedence(
            text_upns,
            eslog_upns,
            qr_payments,
            envelope_upns,
            pain_upns,
        )
        log.info("Merged payment items: %d", len(payments))
    except MessageProcessingTimeout as exc:
        log.warning("Message processing timed out: %s", exc)
        extra_warnings.append(
            f"Processing timed out after {max_runtime_s}s; results may be incomplete."
        )
        payments = []
    finally:
        if max_runtime_s > 0:
            signal.alarm(0)
    return payments, extra_warnings


def _send_mail(
    fwd: MIMEMultipart,
    my_address: str,
    recipients: list[str],
    smtp_cfg: Optional[SmtpConfig],
) -> None:
    """Send the forwarded message via sendmail or SMTP."""
    msg_bytes = fwd.as_bytes()
    if smtp_cfg is None:
        # Inject via sendmail binary — queues directly into Postfix spool,
        # no live SMTP connection needed. On NixOS: /run/wrappers/bin/sendmail
        subprocess.run(["sendmail", "-f", my_address, *recipients], input=msg_bytes, check=True)
        return

    log.debug("SMTP: connecting to %s:%d (tls=%s)", smtp_cfg.host, smtp_cfg.port, smtp_cfg.tls)
    tls_context = _build_smtp_tls_context() if smtp_cfg.tls != "none" else None
    if smtp_cfg.tls == "tls":
        conn: smtplib.SMTP = smtplib.SMTP_SSL(
            smtp_cfg.host, smtp_cfg.port, context=tls_context,
        )
    else:
        conn = smtplib.SMTP(smtp_cfg.host, smtp_cfg.port)

    with conn:
        if smtp_cfg.tls == "starttls":
            conn.starttls(context=tls_context)
        if smtp_cfg.user:
            conn.login(smtp_cfg.user, smtp_cfg.password)
        conn.sendmail(my_address, recipients, msg_bytes)
        log.debug("SMTP: sent to %s", recipients)


def main() -> None:
    admin_email, my_address, allowed_raw, routes_raw, trusted_senders, max_bytes = load_config()
    allowed_patterns = parse_allowed_senders(allowed_raw)
    try:
        routes = parse_allowed_sender_routes(routes_raw)
    except ValueError as exc:
        print(f"ERROR: invalid ALLOWED_SENDER_ROUTES: {exc}", file=sys.stderr)
        sys.exit(1)

    max_msg_bytes = int(os.environ.get("MAX_MESSAGE_BYTES", DEFAULT_MAX_MESSAGE_BYTES))
    raw = sys.stdin.buffer.read(max_msg_bytes + 1)
    if len(raw) > max_msg_bytes:
        log.error("Message exceeds %d byte limit; discarding", max_msg_bytes)
        sys.exit(1)
    msg = email.message_from_bytes(raw)

    # Prefer envelope sender (argv[1], set by Postfix ${sender}) over the
    # RFC 2822 From header, which is trivially spoofable.
    envelope_sender = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    sender_source = os.environ.get("QRMAIL_SENDER_SOURCE", "").strip().lower()
    if envelope_sender:
        sender_addr = envelope_sender
        log.debug("Using envelope sender: %s", sender_addr)
    elif sender_source == "gmail-headers":
        sender_addr = _derive_verified_gmail_sender(msg)
        if sender_addr:
            log.debug("Using Gmail-verified sender: %s", sender_addr)
        else:
            log.warning("No verified Gmail sender identity; discarding message")
    else:
        _, sender_addr = parseaddr(msg.get("From", ""))
        log.debug("No envelope sender; falling back to From header: %s", sender_addr)
    is_trusted = bool(sender_addr) and any(matches(sender_addr, p) for p in trusted_senders)
    route = find_route(sender_addr, routes) if sender_addr else None
    allowed = bool(sender_addr) and (is_allowed_sender(sender_addr, allowed_patterns) or route is not None)
    if not sender_addr or not (is_trusted or allowed):
        sys.exit(0)

    runtime_s = int(os.environ.get("MAX_MESSAGE_RUNTIME_S", DEFAULT_MAX_MESSAGE_RUNTIME_S))
    payments, extra_warnings = scan_message_for_payments(
        msg, max_bytes=max_bytes, max_runtime_s=runtime_s,
    )

    to_addrs: Optional[list[str]] = None
    cc_admin = False
    if not is_trusted and route and route.to_addrs:
        # Route to configured recipients and CC admin (admin should not be in To).
        routed = [a for a in route.to_addrs if a.lower() != admin_email.lower()]
        if routed:
            to_addrs = routed
            cc_admin = True

    if is_trusted:
        recipients = [sender_addr, admin_email]
    elif to_addrs:
        recipients = [*to_addrs, admin_email]
    else:
        recipients = [admin_email]

    recipients = list(dict.fromkeys(recipients))  # dedupe, preserve order

    max_email_bytes = int(os.environ.get("MAX_EMAIL_BYTES", DEFAULT_MAX_EMAIL_BYTES))
    batches = plan_payment_batches(
        payments, max_email_bytes, _original_attachments_size(msg)
    )
    total_parts = len(batches)
    if total_parts > 1:
        log.info("Splitting forward into %d emails (limit %d bytes)", total_parts, max_email_bytes)

    smtp_cfg = _load_smtp_config()
    for index, batch in enumerate(batches, start=1):
        fwd = build_forward(
            msg,
            sender_addr,
            my_address,
            admin_email,
            reply_to_sender=is_trusted,
            to_addrs=to_addrs,
            cc_admin=cc_admin,
            extra_warnings=extra_warnings if index == 1 else None,
            payments=batch,
            include_originals=(index == 1),
            part_info=(index, total_parts),
        )
        _send_mail(fwd, my_address, recipients, smtp_cfg)


def _silence_std_streams_on_exit() -> None:
    """Point stdout/stderr at /dev/null so the interpreter's shutdown flush
    can't raise BrokenPipeError when Postfix has already closed the pipe.

    Without this, a successful run that emitted more diagnostics than Postfix's
    pipe buffer holds exits with status 120, triggering a spurious bounce.
    """
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    try:
        sys.stderr.flush()
    except BrokenPipeError:
        pass
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())
    os.dup2(devnull, sys.stderr.fileno())
    os.close(devnull)


if __name__ == "__main__":
    try:
        main()
    finally:
        _silence_std_streams_on_exit()
