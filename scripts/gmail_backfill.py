#!/usr/bin/env python3
"""Backfill: scan a Google Workspace mailbox for past mail from configured
senders, parse payment data in-process, and forward results via the same
service account.

Authenticates via a service account with domain-wide delegation; the mailbox
is impersonated. Allowed senders are loaded from a config file (one address
per line, '#' for comments). For each message whose From: matches the
allowlist and whose contents yield at least one payment, a forwarded mail is
sent through the Gmail API (impersonating the mailbox) to the result address.
Messages with no payment data are skipped silently.

Usage:
  GMAIL_SERVICE_ACCOUNT_FILE=key.json \\
  python3 scripts/gmail_backfill.py user@example.com 1M

  # Forward results to a different mailbox
  python3 scripts/gmail_backfill.py user@example.com 1M \\
      --result-email admin@example.com

  # Use a custom allowlist
  python3 scripts/gmail_backfill.py user@example.com 1M --config my_senders.txt

  # Preview matches; do not process or send mail
  python3 scripts/gmail_backfill.py user@example.com 1M --dry-run

Duration syntax: N + d (days), w (weeks), m or M (months, ~30d), or y
(years, ~365d). Examples: 14d, 2w, 1M, 1y.
"""
import argparse
import base64
import email
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.routing import is_allowed_sender, parse_allowed_senders
from scripts.mail_processor import (
    GmailConfig,
    send_mail_via_gmail_api,
    build_forward,
    scan_message_for_payments,
)

# gmail.modify covers both read and send so the same credentials drive both
# listing/fetching and the outbound forward.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

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


def build_service(sa_file: str, impersonate: str) -> Any:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        sa_file, scopes=SCOPES,
    ).with_subject(impersonate)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_query(senders: list[str], since: datetime) -> str:
    from_clause = " OR ".join(f"from:{s}" for s in senders)
    return f"({from_clause}) after:{int(since.timestamp())}"


def list_matching_ids(service: Any, query: str) -> list[str]:
    ids: list[str] = []
    page_token = ""
    while True:
        kwargs: dict[str, Any] = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        for m in resp.get("messages", []):
            ids.append(m["id"])
        page_token = resp.get("nextPageToken", "")
        if not page_token:
            break
    return ids


def fetch_raw(service: Any, msg_id: str) -> bytes:
    resp = service.users().messages().get(
        userId="me", id=msg_id, format="raw",
    ).execute()
    return base64.urlsafe_b64decode(resp["raw"] + "==")


def fetch_summary(service: Any, msg_id: str) -> str:
    resp = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    headers = {h["name"]: h["value"] for h in resp.get("payload", {}).get("headers", [])}
    return (
        f"From={headers.get('From', '?')!r} "
        f"Subject={headers.get('Subject', '?')!r} "
        f"Date={headers.get('Date', '?')!r}"
    )


def process_message(
    raw: bytes,
    *,
    allowed_patterns: list[str],
    mailbox: str,
    result_email: str,
    gmail_cfg: GmailConfig,
) -> str:
    """Parse, scan, and conditionally forward one message.

    Returns one of: 'sent', 'skipped-sender', 'skipped-empty'.
    """
    msg = email.message_from_bytes(raw)
    _, sender_addr = parseaddr(msg.get("From", ""))
    sender_addr = sender_addr.strip().lower()
    if not sender_addr or not is_allowed_sender(sender_addr, allowed_patterns):
        log.info("skip: From %r not in allowlist", sender_addr)
        return "skipped-sender"

    payments, extra_warnings = scan_message_for_payments(msg)
    if not payments:
        log.info("skip: no payment data (From=%s)", sender_addr)
        return "skipped-empty"

    fwd = build_forward(
        msg,
        sender_addr,
        mailbox,
        result_email,
        reply_to_sender=False,
        extra_warnings=extra_warnings,
        payments=payments,
    )
    send_mail_via_gmail_api(fwd, [result_email], gmail_cfg)
    log.info("forwarded: From=%s payments=%d -> %s", sender_addr, len(payments), result_email)
    return "sent"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill a Gmail mailbox via DWD and forward parsed payment data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("mailbox", help="Mailbox address to impersonate via DWD")
    parser.add_argument("duration", help="Time window, e.g. 30d, 2w, 1M, 1y")
    parser.add_argument(
        "--result-email", default="",
        help="Recipient of forwarded results (default: mailbox).",
    )
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
        help="List matches; do not process or send mail.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="qr-mail-backfill: %(levelname)s %(message)s",
    )

    if not args.service_account:
        print(
            "ERROR: --service-account or $GMAIL_SERVICE_ACCOUNT_FILE is required",
            file=sys.stderr,
        )
        sys.exit(1)

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

    result_email = args.result_email.strip() or args.mailbox

    service = build_service(args.service_account, args.mailbox)
    query = build_query(senders, since)
    log.debug("Gmail query: %s", query)

    ids = list_matching_ids(service, query)
    log.info("Found %d matching message(s)", len(ids))

    if args.dry_run:
        for msg_id in ids:
            print(f"{msg_id}  {fetch_summary(service, msg_id)}")
        return
    if not ids:
        return

    gmail_cfg = GmailConfig(
        service_account_file=args.service_account,
        impersonate_address=args.mailbox,
    )

    sent = 0
    skipped_sender = 0
    skipped_empty = 0
    failed = 0
    for msg_id in ids:
        try:
            raw = fetch_raw(service, msg_id)
            result = process_message(
                raw,
                allowed_patterns=allowed_patterns,
                mailbox=args.mailbox,
                result_email=result_email,
                gmail_cfg=gmail_cfg,
            )
            if result == "sent":
                sent += 1
            elif result == "skipped-sender":
                skipped_sender += 1
            else:
                skipped_empty += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to process %s: %s", msg_id, exc)
            failed += 1

    log.info(
        "Done. sent=%d skipped_sender=%d skipped_empty=%d failed=%d total=%d",
        sent, skipped_sender, skipped_empty, failed, len(ids),
    )


if __name__ == "__main__":
    main()
