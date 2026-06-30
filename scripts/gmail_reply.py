#!/usr/bin/env python3
"""Build and insert artificial QR replies into a Gmail mailbox.

This is the Google Workspace counterpart to the Postfix path in
``scripts/mail_processor.py``. Instead of forwarding mail, it scans a message
for payment data and, when any is found, inserts a synthetic reply (carrying the
EPC QR codes) into the same conversation via ``users.messages.insert``. Nothing
is sent over SMTP.

Both the periodic fetch daemon (``scripts/gmail_fetch.py``) and the historical
backfill (``scripts/gmail_backfill.py``) drive this module.

Key differences from the Postfix forward path:
  - Replies are threaded onto the original (``In-Reply-To`` / ``References`` /
    ``threadId``), not sent as a new ``Fwd:`` message.
  - Original attachments are NOT re-attached — the original already lives in the
    thread.
  - When no payment data is found, nothing is inserted (the Postfix path always
    forwards).
  - Large result sets are split into several replies at ``MAX_EMAIL_BYTES``.
"""
from __future__ import annotations

import email
import email.message
import html
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from typing import Optional

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.routing import matches
from scripts.gmail_client import GmailClient
from core.payments import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MAX_EMAIL_BYTES,
    DEFAULT_MAX_MESSAGE_RUNTIME_S,
    PaymentItem,
    _derive_verified_gmail_sender,
    attach_payment_artifacts,
    collect_payment_warnings,
    payments_section_html,
    payments_section_text,
    plan_payment_batches,
    scan_message_for_payments,
)

log = logging.getLogger(__name__)


def gmail_from_clause(senders: list[str]) -> str:
    """Build a Gmail ``(from:a OR from:b)`` clause from sender globs.

    Returns an empty string if any pattern cannot be expressed as a Gmail
    ``from:`` term (e.g. ``user@*``), signalling that the listing cannot be
    safely scoped and the caller must rely on per-message verification instead.
    """
    terms: list[str] = []
    for raw in senders:
        s = raw.strip().lower()
        if not s:
            continue
        if s.startswith("*@") and "*" not in s[2:]:
            terms.append(f"from:{s[2:]}")  # domain glob → match the domain
        elif "*" not in s:
            terms.append(f"from:{s}")
        else:
            return ""
    if not terms:
        return ""
    return "(" + " OR ".join(terms) + ")"


def _sanitize_header(value: str) -> str:
    """Strip CR/LF so attacker-controlled header values can't inject headers."""
    return value.replace("\r", " ").replace("\n", " ").strip()


def _reply_subject(original: email.message.Message, part_info: Optional[tuple[int, int]]) -> str:
    subject = _sanitize_header(original.get("Subject", "(no subject)"))
    if not subject.strip().lower().startswith("re:"):
        subject = "Re: " + subject
    if part_info is not None and part_info[1] > 1:
        subject = f"{subject} (part {part_info[0]}/{part_info[1]})"
    return subject


def build_reply(
    original: email.message.Message,
    my_address: str,
    mailbox: str,
    payments: list[PaymentItem],
    *,
    in_reply_to: str = "",
    references: str = "",
    extra_warnings: Optional[list[str]] = None,
    part_info: Optional[tuple[int, int]] = None,
) -> MIMEMultipart:
    """Build a synthetic reply carrying the payment QR codes.

    The reply is authored by ``my_address`` and addressed to ``mailbox`` (the
    inbox it will be inserted into), threaded onto the original message.
    """
    reply = MIMEMultipart("mixed")
    reply["From"] = my_address
    reply["To"] = mailbox
    reply["Subject"] = _reply_subject(original, part_info)
    in_reply_to = _sanitize_header(in_reply_to)
    references = _sanitize_header(references)
    if in_reply_to:
        reply["In-Reply-To"] = in_reply_to
        reply["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to

    related = MIMEMultipart("related")
    alternative = MIMEMultipart("alternative")
    related.attach(alternative)

    warnings = collect_payment_warnings(payments, extra_warnings)
    orig_from = original.get("From", "(unknown sender)")

    body_lines: list[str] = [
        "qr-mail extracted the following payment(s) and converted them to "
        "EPC SCT QR codes you can scan with any SEPA banking app.",
        "",
        f"In reply to: {orig_from} — {original.get('Subject', '')}",
        "",
    ]
    if warnings:
        body_lines += ["WARNINGS:", *[f"  - {w}" for w in warnings], ""]
    body_lines += payments_section_text(payments)
    alternative.attach(MIMEText("\n".join(body_lines), "plain", "utf-8"))

    html_parts: list[str] = [
        "<p>qr-mail extracted the following payment(s) and converted them to "
        "EPC SCT QR codes you can scan with any SEPA banking app.</p>",
    ]
    if warnings:
        html_parts.append("<h3>Warnings</h3><ul>")
        html_parts += [f"<li>{html.escape(w)}</li>" for w in warnings]
        html_parts.append("</ul>")
    html_parts += payments_section_html(payments)
    alternative.attach(
        MIMEText("<html><body>" + "\n".join(html_parts) + "</body></html>", "html", "utf-8")
    )

    reply.attach(related)
    attach_payment_artifacts(related, reply, payments)
    return reply


def deliver_replies(
    client: GmailClient,
    original: email.message.Message,
    payments: list[PaymentItem],
    *,
    my_address: str,
    thread_id: Optional[str] = None,
    extra_warnings: Optional[list[str]] = None,
    max_email_bytes: int = DEFAULT_MAX_EMAIL_BYTES,
) -> int:
    """Insert one or more threaded replies; return the number inserted."""
    if not payments:
        return 0

    in_reply_to = original.get("Message-ID", "").strip()
    references = original.get("References", "").strip()
    # Replies carry no original attachments, so reserve nothing for the first batch.
    batches = plan_payment_batches(payments, max_email_bytes, first_batch_reserved=0)
    total = len(batches)
    if total > 1:
        log.info("Splitting reply into %d messages (limit %d bytes)", total, max_email_bytes)

    for index, batch in enumerate(batches, start=1):
        reply = build_reply(
            original,
            my_address,
            client.user_id,
            batch,
            in_reply_to=in_reply_to,
            references=references,
            extra_warnings=extra_warnings if index == 1 else None,
            part_info=(index, total),
        )
        msg_id = client.insert_reply(reply.as_bytes(), thread_id=thread_id)
        log.debug("Inserted reply %d/%d as %s", index, total, msg_id)
    return total


def resolve_sender(msg: email.message.Message, *, strict: bool) -> str:
    """Return the permitted sender address for matching against the allowlist.

    With ``strict`` (the live daemon), the From address must be backed by an
    aligned SPF/DKIM/DMARC pass in the message's auth headers. Otherwise (e.g.
    admin-run backfill over an already-trusted mailbox) the From header is used.
    """
    verified = _derive_verified_gmail_sender(msg)
    if verified:
        return verified
    if strict:
        return ""
    _, addr = parseaddr(msg.get("From", ""))
    return addr.strip().lower()


def process_message(
    client: GmailClient,
    raw: bytes,
    *,
    my_address: str,
    allowed_patterns: list[str],
    thread_id: Optional[str] = None,
    strict_sender: bool = True,
    max_email_bytes: int = DEFAULT_MAX_EMAIL_BYTES,
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_runtime_s: int = DEFAULT_MAX_MESSAGE_RUNTIME_S,
) -> str:
    """Scan one raw message and, if warranted, insert reply(ies).

    Returns one of: ``"sent"``, ``"skipped-sender"``, ``"skipped-empty"``.
    """
    msg = email.message_from_bytes(raw)
    sender = resolve_sender(msg, strict=strict_sender)
    if not sender or not any(matches(sender, p) for p in allowed_patterns):
        log.info("skip: sender %r not permitted", sender)
        return "skipped-sender"

    payments, extra_warnings = scan_message_for_payments(
        msg, max_bytes=max_attachment_bytes, max_runtime_s=max_runtime_s,
    )
    if not payments:
        log.info("skip: no payment data (from %s)", sender)
        return "skipped-empty"

    deliver_replies(
        client,
        msg,
        payments,
        my_address=my_address,
        thread_id=thread_id,
        extra_warnings=extra_warnings,
        max_email_bytes=max_email_bytes,
    )
    log.info("replied: from %s payments=%d", sender, len(payments))
    return "sent"
