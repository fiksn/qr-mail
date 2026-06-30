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

import email
import email.encoders
import html
import logging
import os
import smtplib
import ssl
import subprocess
import sys
from dataclasses import dataclass
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from typing import Optional

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.routing import (
    find_route,
    is_allowed_sender,
    matches,
    parse_allowed_sender_routes,
    parse_allowed_senders,
)
from core.payments import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MAX_EMAIL_BYTES,
    DEFAULT_MAX_MESSAGE_RUNTIME_S,
    PaymentItem,
    _derive_verified_gmail_sender,
    attach_payment_artifacts,
    collect_payment_warnings,
    extract_text_body,
    payments_section_html,
    payments_section_text,
    plan_payment_batches,
    scan_message_for_payments,
)

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


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    tls: str  # "starttls", "tls", or "none"


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
