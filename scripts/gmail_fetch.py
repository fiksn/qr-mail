#!/usr/bin/env python3
"""
Gmail polling daemon: fetches unread inbox messages and pipes each one to
the qr-mail processor, then marks it processed.

Uses a service account with domain-wide delegation (Google Workspace).

Configuration via environment variables:
  GMAIL_SERVICE_ACCOUNT_FILE   path to service_account.json (required)
  GMAIL_IMPERSONATE_ADDRESS    Gmail address to impersonate (required)
  GMAIL_POLL_INTERVAL_S        seconds between polls (default: 60)
  GMAIL_PROCESSED_LABEL        label applied after processing (default: qr-mail-processed)
  PROCESSOR_BIN                processor executable path (default: qr-mail-processor)

The processor binary is invoked as a subprocess and receives the raw RFC 2822
message on stdin — identical to what Postfix delivers via the pipe transport.
All ADMIN_EMAIL / MY_ADDRESS / ALLOWED_SENDERS etc. must be set in the
processor's own environment (the NixOS module handles this via its shell wrapper).
"""
import base64
import email
import logging
import os
import subprocess
import sys
import time
from email.utils import parseaddr

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

DEFAULT_POLL_INTERVAL_S = 60
DEFAULT_PROCESSED_LABEL = "qr-mail-processed"

log = logging.getLogger(__name__)


def _build_service(service_account_file: str, impersonate: str):
    creds = service_account.Credentials.from_service_account_file(
        service_account_file, scopes=SCOPES
    ).with_subject(impersonate)
    # cache_discovery=False avoids writing to /tmp in restricted systemd environments.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _get_or_create_label(service, label_name: str) -> str:
    """Return the label ID, creating the label if it does not exist."""
    result = service.users().labels().list(userId="me").execute()
    for label in result.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    created = service.users().labels().create(
        userId="me",
        body={
            "name": label_name,
            # Hidden from label list and message list to keep inbox tidy.
            "labelListVisibility": "labelHide",
            "messageListVisibility": "hide",
        },
    ).execute()
    log.info("Created Gmail label %r (id=%s)", label_name, created["id"])
    return created["id"]


def _list_unread_ids(service) -> list[str]:
    """Return IDs of unread INBOX messages (up to 50 per poll)."""
    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX", "UNREAD"],
        maxResults=50,
    ).execute()
    return [m["id"] for m in result.get("messages", [])]


def _fetch_raw(service, msg_id: str) -> bytes:
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="raw"
    ).execute()
    return base64.urlsafe_b64decode(msg["raw"] + "==")


def _mark_processed(service, msg_id: str, processed_label_id: str) -> None:
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={
            "addLabelIds": [processed_label_id],
            "removeLabelIds": ["UNREAD"],
        },
    ).execute()


def _extract_sender(raw: bytes) -> str:
    """Extract the From address from raw RFC 2822 bytes."""
    msg = email.message_from_bytes(raw)
    _, addr = parseaddr(msg.get("From", ""))
    return addr


def _run_processor(raw: bytes, processor_bin: str) -> bool:
    """Pipe raw RFC 2822 bytes to the processor. Returns True on success."""
    sender = _extract_sender(raw)
    result = subprocess.run(
        [processor_bin, sender], input=raw, capture_output=True,
    )
    if result.stderr:
        # Processor logs to stderr; relay at debug level to avoid double-logging.
        for line in result.stderr.decode(errors="replace").splitlines():
            log.debug("processor: %s", line)
    if result.returncode != 0:
        log.error("Processor exited %d", result.returncode)
        return False
    return True


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="qr-mail-gmail: %(levelname)s %(message)s",
    )

    sa_file = os.environ.get("GMAIL_SERVICE_ACCOUNT_FILE", "").strip()
    impersonate = os.environ.get("GMAIL_IMPERSONATE_ADDRESS", "").strip()
    if not sa_file or not impersonate:
        print(
            "ERROR: GMAIL_SERVICE_ACCOUNT_FILE and GMAIL_IMPERSONATE_ADDRESS are required",
            file=sys.stderr,
        )
        sys.exit(1)

    processor_bin = os.environ.get("PROCESSOR_BIN", "qr-mail-processor").strip()
    poll_interval = int(os.environ.get("GMAIL_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S))
    processed_label_name = os.environ.get("GMAIL_PROCESSED_LABEL", DEFAULT_PROCESSED_LABEL)

    log.info(
        "Starting (impersonating=%s, poll=%ds, label=%r)",
        impersonate, poll_interval, processed_label_name,
    )

    service = _build_service(sa_file, impersonate)
    processed_label_id = _get_or_create_label(service, processed_label_name)
    log.info("Processed label id=%s", processed_label_id)

    while True:
        try:
            msg_ids = _list_unread_ids(service)
            if msg_ids:
                log.info("Found %d unread message(s)", len(msg_ids))
            for msg_id in msg_ids:
                try:
                    raw = _fetch_raw(service, msg_id)
                    ok = _run_processor(raw, processor_bin)
                    if ok:
                        _mark_processed(service, msg_id, processed_label_id)
                        log.info("Processed message %s", msg_id)
                    else:
                        # Leave unread so it is retried on the next poll.
                        log.warning("Processing failed for %s; will retry", msg_id)
                except HttpError as exc:
                    log.error("Gmail API error on message %s: %s", msg_id, exc)
        except HttpError as exc:
            log.error("Gmail API error during poll: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.error("Unexpected error: %s", exc)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
