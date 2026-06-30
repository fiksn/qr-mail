#!/usr/bin/env python3
"""Backfill: scan a Google Workspace mailbox for past mail from configured
senders and insert QR replies into each matching conversation.

Same delivery model as the live daemon (scripts/gmail_fetch.py): for every
message whose From: matches the allowlist and that yields at least one payment,
an artificial reply carrying the EPC QR codes is inserted into the original
thread. Messages with no payment data are skipped silently.

Authenticates via either auth mode (auto-detected, see scripts/gmail_client.py).
A service account (domain-wide delegation) can impersonate any mailbox; OAuth is
limited to the consenting user's own mailbox.

Usage:
  GMAIL_SERVICE_ACCOUNT_FILE=key.json \\
  python3 scripts/gmail_backfill.py user@example.com 1M

  # Use a custom allowlist
  python3 scripts/gmail_backfill.py user@example.com 1M --config my_senders.txt

  # Preview matches; do not process or insert anything
  python3 scripts/gmail_backfill.py user@example.com 1M --dry-run

Duration syntax: N + d (days), w (weeks), m or M (months, ~30d), or y
(years, ~365d). Examples: 14d, 2w, 1M, 1y.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.routing import parse_allowed_senders
from scripts.gmail_client import (
    GmailClient,
    GmailConfig,
    GmailConfigError,
    load_gmail_config,
)
from scripts.gmail_reply import gmail_from_clause, process_message
from scripts.mail_processor import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MAX_EMAIL_BYTES,
    DEFAULT_MAX_MESSAGE_RUNTIME_S,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "allowed_senders.txt"

_DURATION_RE = re.compile(r"^(\d+)([dwmyDWMY])$")

log = logging.getLogger(__name__)


def parse_duration(spec: str) -> timedelta:
    """Parse '30d' / '2w' / '1M' / '1y' into a timedelta.

    Months and years are approximated (30d / 365d) because Gmail's `after:`
    operator wants a precise Unix timestamp.
    """
    m = _DURATION_RE.fullmatch(spec.strip())
    if not m:
        raise ValueError(
            f"invalid duration {spec!r}: expected N + d/w/m/y, e.g. 30d, 2w, 1M, 1y"
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    if unit == "m":
        return timedelta(days=n * 30)
    return timedelta(days=n * 365)


def load_allowed_senders(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read config {path!r}: {exc}")
    senders: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        senders.append(s)
    if not senders:
        raise SystemExit(f"config {path!r} has no senders")
    return senders


def build_client(mailbox: str, service_account_file: str) -> GmailClient:
    """Build a GmailClient for ``mailbox`` (DWD impersonation or OAuth)."""
    if service_account_file:
        cfg = GmailConfig(
            auth_mode="service_account",
            user=mailbox,
            service_account_file=service_account_file,
        )
        return GmailClient(cfg)

    cfg = load_gmail_config()
    if cfg is None:
        raise SystemExit(
            "no Gmail credentials configured (set GMAIL_SERVICE_ACCOUNT_FILE or "
            "GMAIL_OAUTH_TOKEN_FILE, or pass --service-account)"
        )
    if cfg.auth_mode == "service_account":
        return GmailClient.for_user(cfg, mailbox)
    if cfg.user != mailbox:
        raise SystemExit(
            f"OAuth credentials are bound to {cfg.user!r}; cannot backfill {mailbox!r}. "
            "Use a service account for domain-wide delegation."
        )
    return GmailClient(cfg)


def build_query(senders: list[str], since: datetime) -> str:
    from_clause = gmail_from_clause(senders) or "(" + " OR ".join(f"from:{s}" for s in senders) + ")"
    return f"{from_clause} after:{int(since.timestamp())}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill a Gmail mailbox and insert QR replies into threads.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("mailbox", help="Mailbox address to scan (DWD subject or OAuth user)")
    parser.add_argument("duration", help="Time window, e.g. 30d, 2w, 1M, 1y")
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help=f"Allowed-sender file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--service-account",
        default=os.environ.get("GMAIL_SERVICE_ACCOUNT_FILE", "").strip(),
        help="Service account JSON file (default: $GMAIL_SERVICE_ACCOUNT_FILE).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List matches; do not process or insert anything.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="qr-mail-backfill: %(levelname)s %(message)s",
    )

    senders = load_allowed_senders(Path(args.config))
    log.info("Loaded %d allowed sender(s) from %s", len(senders), args.config)
    allowed_patterns = parse_allowed_senders(senders)

    try:
        delta = parse_duration(args.duration)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    since = datetime.now(timezone.utc) - delta
    log.info("Scanning messages newer than %s (~%s ago)", since.isoformat(), delta)

    my_address = os.environ.get("MY_ADDRESS", "").strip() or args.mailbox
    max_email_bytes = int(os.environ.get("MAX_EMAIL_BYTES", DEFAULT_MAX_EMAIL_BYTES))
    max_attachment_bytes = int(os.environ.get("MAX_ATTACHMENT_BYTES", DEFAULT_MAX_ATTACHMENT_BYTES))
    max_runtime_s = int(os.environ.get("MAX_MESSAGE_RUNTIME_S", DEFAULT_MAX_MESSAGE_RUNTIME_S))

    try:
        client = build_client(args.mailbox, args.service_account)
    except GmailConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    query = build_query(senders, since)
    log.debug("Gmail query: %s", query)

    ids = client.search(query)
    log.info("Found %d matching message(s)", len(ids))

    if args.dry_run:
        for msg_id, thread_id in ids:
            print(f"{msg_id}  thread={thread_id}")
        return

    sent = skipped_sender = skipped_empty = failed = 0
    for msg_id, thread_id in ids:
        try:
            raw = client.get_raw_message(msg_id)
            status = process_message(
                client,
                raw,
                my_address=my_address,
                allowed_patterns=allowed_patterns,
                thread_id=thread_id,
                strict_sender=False,
                max_email_bytes=max_email_bytes,
                max_attachment_bytes=max_attachment_bytes,
                max_runtime_s=max_runtime_s,
            )
            if status == "sent":
                sent += 1
            elif status == "skipped-sender":
                skipped_sender += 1
            else:
                skipped_empty += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to process %s: %s", msg_id, exc)
            failed += 1

    log.info(
        "Done. replied=%d skipped_sender=%d skipped_empty=%d failed=%d total=%d",
        sent, skipped_sender, skipped_empty, failed, len(ids),
    )


if __name__ == "__main__":
    main()
