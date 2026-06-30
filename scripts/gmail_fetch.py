#!/usr/bin/env python3
"""Gmail polling daemon: fetch new mail from allowed senders and insert QR replies.

Periodically searches the mailbox for unread messages from configured senders,
scans each for payment data, and — when any is found — inserts an artificial
reply carrying the EPC QR codes into the same conversation. Messages with no
payment data are marked processed but get no reply.

Works with either auth mode (auto-detected, see scripts/gmail_client.py):
  - single-user OAuth (GMAIL_OAUTH_TOKEN_FILE), or
  - service account + domain-wide delegation (GMAIL_SERVICE_ACCOUNT_FILE).

Configuration via environment variables:
  MY_ADDRESS                  From: address of the inserted replies (required)
  ALLOWED_SENDERS             colon-separated sender globs, e.g. "*@trusted.com"
  TRUSTED_SENDERS             additional sender globs (same effect here)
  GMAIL_IMPERSONATE_ADDRESS   mailbox to read and insert into (required)
  GMAIL_SERVICE_ACCOUNT_FILE / GMAIL_OAUTH_TOKEN_FILE / GMAIL_OAUTH_CLIENT_SECRET_FILE
  GMAIL_POLL_INTERVAL_S       seconds between polls (default: 60)
  GMAIL_PROCESSED_LABEL       label applied after handling (default: qr-mail-processed)
  GMAIL_FAILED_LABEL          label applied after repeated failures (default: qr-mail-failed)
  GMAIL_MAX_ATTEMPTS          retries before giving up on a message (default: 3)
  MAX_EMAIL_BYTES             split replies past this size (default: 20 MB)

The Gmail search is scoped to the configured senders so the daemon never touches
unrelated mail — important when running against a single user's own mailbox.
"""
from __future__ import annotations

import logging
import os
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.routing import parse_allowed_senders
from scripts.gmail_client import GmailClient, GmailConfigError, load_gmail_config
from scripts.gmail_reply import gmail_from_clause, process_message
from core.payments import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MAX_EMAIL_BYTES,
    DEFAULT_MAX_MESSAGE_RUNTIME_S,
)

DEFAULT_POLL_INTERVAL_S = 60
DEFAULT_PROCESSED_LABEL = "qr-mail-processed"
DEFAULT_FAILED_LABEL = "qr-mail-failed"
DEFAULT_MAX_ATTEMPTS = 3
MAX_PER_POLL = 50

log = logging.getLogger(__name__)


def record_failure(
    client: GmailClient,
    msg_id: str,
    attempts: dict[str, int],
    *,
    failed_label_id: str,
    failed_label: str,
    max_attempts: int,
    exc: Exception,
) -> None:
    """Count a per-message failure; label it failed once retries are exhausted."""
    attempts[msg_id] = attempts.get(msg_id, 0) + 1
    if attempts[msg_id] >= max_attempts:
        log.error(
            "message %s failed %d time(s); labelling %r and giving up: %s",
            msg_id, attempts[msg_id], failed_label, exc,
        )
        try:
            client.add_label(msg_id, failed_label_id)
        except Exception as label_exc:  # noqa: BLE001
            log.error("could not label %s failed: %s", msg_id, label_exc)
        attempts.pop(msg_id, None)
    else:
        log.warning(
            "message %s failed (attempt %d/%d); will retry: %s",
            msg_id, attempts[msg_id], max_attempts, exc,
        )


def _load_senders() -> list[str]:
    allowed = [p for p in os.environ.get("ALLOWED_SENDERS", "").split(":") if p]
    trusted = [p for p in os.environ.get("TRUSTED_SENDERS", "").split(":") if p]
    return parse_allowed_senders(allowed + trusted)


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="qr-mail-gmail: %(levelname)s %(message)s",
    )

    try:
        cfg = load_gmail_config()
    except GmailConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    if cfg is None:
        print(
            "ERROR: no Gmail credentials configured "
            "(set GMAIL_SERVICE_ACCOUNT_FILE or GMAIL_OAUTH_TOKEN_FILE)",
            file=sys.stderr,
        )
        sys.exit(1)

    my_address = os.environ.get("MY_ADDRESS", "").strip()
    if not my_address:
        print("ERROR: MY_ADDRESS is required", file=sys.stderr)
        sys.exit(1)

    senders = _load_senders()
    if not senders:
        print("ERROR: no senders configured (ALLOWED_SENDERS / TRUSTED_SENDERS)", file=sys.stderr)
        sys.exit(1)

    poll_interval = int(os.environ.get("GMAIL_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S))
    processed_label = os.environ.get("GMAIL_PROCESSED_LABEL", DEFAULT_PROCESSED_LABEL)
    failed_label = os.environ.get("GMAIL_FAILED_LABEL", DEFAULT_FAILED_LABEL)
    max_attempts = int(os.environ.get("GMAIL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
    max_email_bytes = int(os.environ.get("MAX_EMAIL_BYTES", DEFAULT_MAX_EMAIL_BYTES))
    max_attachment_bytes = int(os.environ.get("MAX_ATTACHMENT_BYTES", DEFAULT_MAX_ATTACHMENT_BYTES))
    max_runtime_s = int(os.environ.get("MAX_MESSAGE_RUNTIME_S", DEFAULT_MAX_MESSAGE_RUNTIME_S))

    log.info(
        "Starting (mode=%s, mailbox=%s, poll=%ds, label=%r)",
        cfg.auth_mode, cfg.user, poll_interval, processed_label,
    )

    client = GmailClient(cfg)
    label_id = client.get_or_create_label(processed_label)
    failed_label_id = client.get_or_create_label(failed_label)

    from_clause = gmail_from_clause(senders)
    if not from_clause:
        log.warning(
            "Sender patterns are not expressible as a Gmail query; "
            "all unread mail will be examined (verification still applies)"
        )
    # Exclude both processed and permanently-failed messages from future polls.
    query = f"is:unread -label:{processed_label} -label:{failed_label} {from_clause}".strip()
    log.info("Gmail query: %s", query)

    # Per-message failure counts (in-memory). A message that keeps throwing is
    # labelled failed after max_attempts so it stops being re-fetched forever.
    attempts: dict[str, int] = {}

    while True:
        try:
            batch = client.search(query, max_results=MAX_PER_POLL)
            # Drop stale failure counters for messages no longer in the working
            # set (e.g. read or deleted elsewhere) so the dict can't grow without
            # bound over long uptimes.
            batch_ids = {msg_id for msg_id, _ in batch}
            attempts = {k: v for k, v in attempts.items() if k in batch_ids}
            for msg_id, thread_id in batch:
                try:
                    raw = client.get_raw_message(msg_id)
                    status = process_message(
                        client,
                        raw,
                        my_address=my_address,
                        allowed_patterns=senders,
                        thread_id=thread_id,
                        strict_sender=True,
                        max_email_bytes=max_email_bytes,
                        max_attachment_bytes=max_attachment_bytes,
                        max_runtime_s=max_runtime_s,
                    )
                    if status == "skipped-sender":
                        # Could not verify the sender — flag handled but leave unread.
                        client.add_label(msg_id, label_id)
                    else:
                        client.mark_processed(msg_id, label_id)
                    attempts.pop(msg_id, None)
                    log.info("message %s: %s", msg_id, status)
                except Exception as exc:  # noqa: BLE001
                    record_failure(
                        client, msg_id, attempts,
                        failed_label_id=failed_label_id,
                        failed_label=failed_label,
                        max_attempts=max_attempts,
                        exc=exc,
                    )
        except Exception as exc:  # noqa: BLE001
            log.error("Error during poll: %s", exc)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
