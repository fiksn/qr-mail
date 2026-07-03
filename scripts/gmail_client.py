#!/usr/bin/env python3
"""Unified Gmail API access for qr-mail.

Supports two authentication modes, following the approach of
github.com/fiksn/thanks-for-all-the-phish:

  - ``oauth``: single-user OAuth with a cached, refreshable token. A normal
    Workspace (or consumer) user authorises their own mailbox once; no admin
    involvement and no domain-wide delegation are needed.
  - ``service_account``: domain-wide delegation. A service account impersonates
    any mailbox in the workspace.

A single least-privilege scope, ``gmail.modify``, covers reading, labelling and
inserting messages, so the same credentials drive the whole pipeline without the
permanent-delete capability that ``https://mail.google.com/`` would grant.

Unlike thanks-for-all-the-phish (which rewrites messages in place), qr-mail
delivers results by *inserting an artificial reply* into the mailbox
(``users.messages.insert``), threaded onto the original message. Nothing is
sent over SMTP.

The auth mode is auto-detected from configuration:

  GMAIL_IMPERSONATE_ADDRESS    mailbox to act on (required for any Gmail mode)
  GMAIL_SERVICE_ACCOUNT_FILE   service-account JSON → service_account mode
  GMAIL_OAUTH_CLIENT_SECRET_FILE
                               OAuth client-secret JSON (one-time consent flow)
  GMAIL_OAUTH_TOKEN_FILE       cached OAuth token JSON (runtime credential)

If neither a service-account file nor any OAuth file is configured, no Gmail
transport is active and the caller falls back to Postfix/SMTP.
"""
from __future__ import annotations

import base64
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Least privilege: gmail.modify covers read, label and insert — everything the
# pipeline needs — without granting permanent-delete (which mail.google.com would).
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
# Read-only Directory API scope, used only when expanding a DWD_USERS_GLOB into a
# concrete set of mailboxes. Must be authorised for the service account in the
# Workspace Admin console alongside SCOPES, and the impersonated subject must be
# a user with directory-read rights (an admin).
DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
_TOKEN_FILE_MODE = 0o600

log = logging.getLogger(__name__)


class GmailConfigError(ValueError):
    """Raised when Gmail configuration is present but inconsistent."""


@dataclass
class GmailConfig:
    """Resolved Gmail credentials configuration."""

    auth_mode: str  # "oauth" | "service_account"
    user: str       # mailbox address (OAuth bound user, or DWD subject)
    service_account_file: str = ""
    client_secret_file: str = ""
    token_file: str = ""


def load_gmail_config() -> Optional[GmailConfig]:
    """Build a GmailConfig from the environment, or None if Gmail is unconfigured.

    The auth mode is auto-detected: a configured service-account file selects
    domain-wide delegation; otherwise a configured OAuth client-secret or token
    file selects single-user OAuth.
    """
    user = os.environ.get("GMAIL_IMPERSONATE_ADDRESS", "").strip()
    service_account_file = os.environ.get("GMAIL_SERVICE_ACCOUNT_FILE", "").strip()
    client_secret_file = os.environ.get("GMAIL_OAUTH_CLIENT_SECRET_FILE", "").strip()
    token_file = os.environ.get("GMAIL_OAUTH_TOKEN_FILE", "").strip()

    if service_account_file:
        if not user:
            raise GmailConfigError(
                "GMAIL_SERVICE_ACCOUNT_FILE is set but GMAIL_IMPERSONATE_ADDRESS is empty"
            )
        return GmailConfig(
            auth_mode="service_account",
            user=user,
            service_account_file=service_account_file,
        )

    if client_secret_file or token_file:
        if not user:
            raise GmailConfigError(
                "OAuth is configured but GMAIL_IMPERSONATE_ADDRESS is empty"
            )
        if not token_file and not client_secret_file:
            raise GmailConfigError(
                "OAuth requires GMAIL_OAUTH_TOKEN_FILE (runtime) and/or "
                "GMAIL_OAUTH_CLIENT_SECRET_FILE (initial consent)"
            )
        return GmailConfig(
            auth_mode="oauth",
            user=user,
            client_secret_file=client_secret_file,
            token_file=token_file,
        )

    return None


def get_credentials(cfg: GmailConfig, subject: Optional[str] = None) -> Any:
    """Return Google credentials for the configured auth mode.

    In service_account mode, ``subject`` overrides which mailbox to impersonate.
    In oauth mode, ``subject`` must equal ``cfg.user`` (or be None): OAuth is
    bound to the account that granted consent.
    """
    if cfg.auth_mode == "service_account":
        return _service_account_credentials(cfg, subject or cfg.user)
    if subject is not None and subject != cfg.user:
        raise GmailConfigError(
            f"OAuth credentials are bound to {cfg.user!r}; cannot act as {subject!r}. "
            "Configure a service account for domain-wide delegation to impersonate "
            "other users."
        )
    return _oauth_credentials(cfg)


def _service_account_credentials(cfg: GmailConfig, subject: str) -> Any:
    from google.oauth2 import service_account

    if not cfg.service_account_file or not Path(cfg.service_account_file).exists():
        raise FileNotFoundError(
            f"service account key not found at {cfg.service_account_file!r}"
        )
    creds = service_account.Credentials.from_service_account_file(
        cfg.service_account_file, scopes=SCOPES
    )
    return creds.with_subject(subject)


def list_directory_users(cfg: GmailConfig, admin_subject: str) -> list[str]:
    """Return the primary email of every active Workspace user in the domain.

    Requires service_account (domain-wide delegation) mode. ``admin_subject`` is
    impersonated to query the Admin SDK Directory API, so it must be a user with
    directory-read rights and the ``DIRECTORY_SCOPE`` must be authorised for the
    service account. Suspended users are skipped.
    """
    if cfg.auth_mode != "service_account":
        raise GmailConfigError(
            "listing directory users requires service_account (domain-wide delegation) mode"
        )
    if not cfg.service_account_file or not Path(cfg.service_account_file).exists():
        raise FileNotFoundError(
            f"service account key not found at {cfg.service_account_file!r}"
        )

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        cfg.service_account_file, scopes=[DIRECTORY_SCOPE]
    ).with_subject(admin_subject)
    service = build("admin", "directory_v1", credentials=creds, cache_discovery=False)

    users: list[str] = []
    page_token = ""
    while True:
        kwargs: dict[str, Any] = {
            "customer": "my_customer",
            "maxResults": 500,
            "projection": "basic",
            "orderBy": "email",
        }
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().list(**kwargs).execute()
        for user in resp.get("users", []):
            email = user.get("primaryEmail")
            if email and not user.get("suspended", False):
                users.append(email)
        page_token = resp.get("nextPageToken", "")
        if not page_token:
            break
    return users


def _oauth_credentials(cfg: GmailConfig) -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = Path(cfg.token_file) if cfg.token_file else None

    creds: Optional[Credentials] = None
    if token_path and token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if token_path:
            _write_token_file(token_path, creds.to_json())
        return creds

    # No usable cached token — fall back to the interactive consent flow. This
    # only happens during one-time bootstrap (e.g. scripts/gmail_authorize.py);
    # a running daemon should always have a refreshable token.
    if not cfg.client_secret_file or not Path(cfg.client_secret_file).exists():
        raise FileNotFoundError(
            "no valid OAuth token and no client-secret file to run the consent "
            f"flow (GMAIL_OAUTH_CLIENT_SECRET_FILE={cfg.client_secret_file!r})"
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(cfg.client_secret_file, SCOPES)
    creds = flow.run_local_server(port=0, login_hint=cfg.user)
    if token_path:
        _write_token_file(token_path, creds.to_json())
    return creds


def _write_token_file(path: Path, content: str) -> None:
    """Write the OAuth token atomically with 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fchmod(tmp.fileno(), _TOKEN_FILE_MODE)
        os.replace(tmp_name, path)
        path.chmod(_TOKEN_FILE_MODE)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class GmailClient:
    """Thin Gmail API wrapper over a single mailbox."""

    def __init__(self, cfg: GmailConfig, subject: Optional[str] = None) -> None:
        from googleapiclient.discovery import build

        self._cfg = cfg
        self._user_id = subject or cfg.user
        creds = get_credentials(cfg, subject=self._user_id)
        # cache_discovery=False avoids writing to disk in restricted environments.
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    @classmethod
    def for_user(cls, cfg: GmailConfig, user: str) -> "GmailClient":
        """Create a client acting as ``user`` (service_account / DWD only)."""
        if cfg.auth_mode != "service_account":
            raise GmailConfigError(
                "for_user() requires the service_account auth mode (domain-wide delegation)"
            )
        return cls(cfg, subject=user)

    @property
    def user_id(self) -> str:
        return self._user_id

    def search(
        self, query: str, max_results: Optional[int] = None
    ) -> list[tuple[str, str]]:
        """Return (message_id, thread_id) pairs matching a Gmail search query.

        Scoping the listing with a query (e.g. ``from:`` clauses) is what keeps
        the daemon from touching unrelated mail in a single-user mailbox.
        With ``max_results`` set, listing stops once that many are collected;
        otherwise all pages are walked.
        """
        ids: list[tuple[str, str]] = []
        page_token = ""
        page_size = min(max_results, 500) if max_results else 500
        while True:
            kwargs: dict[str, Any] = {"userId": self._user_id, "q": query, "maxResults": page_size}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self._service.users().messages().list(**kwargs).execute()
            ids.extend((m["id"], m["threadId"]) for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken", "")
            if not page_token or (max_results and len(ids) >= max_results):
                break
        return ids[:max_results] if max_results else ids

    def get_raw_message(self, message_id: str) -> bytes:
        """Return the raw RFC 822 bytes of a message."""
        payload = (
            self._service.users()
            .messages()
            .get(userId=self._user_id, id=message_id, format="raw")
            .execute()
        )
        return base64.urlsafe_b64decode(payload["raw"] + "==")

    def insert_reply(
        self,
        raw_rfc822: bytes,
        *,
        thread_id: Optional[str] = None,
        unread: bool = True,
    ) -> str:
        """Insert a message into the mailbox, threaded onto ``thread_id``.

        The message lands directly in the inbox without being sent over SMTP.
        """
        label_ids = ["INBOX"]
        if unread:
            label_ids.append("UNREAD")
        body: dict[str, Any] = {
            "raw": base64.urlsafe_b64encode(raw_rfc822).decode("ascii"),
            "labelIds": label_ids,
        }
        if thread_id:
            body["threadId"] = thread_id
        resp = (
            self._service.users()
            .messages()
            .insert(userId=self._user_id, body=body, internalDateSource="dateHeader")
            .execute()
        )
        return resp["id"]

    def get_or_create_label(self, label_name: str) -> str:
        """Return the label ID, creating a hidden label if it does not exist."""
        result = self._service.users().labels().list(userId=self._user_id).execute()
        for label in result.get("labels", []):
            if label["name"] == label_name:
                return label["id"]
        created = (
            self._service.users()
            .labels()
            .create(
                userId=self._user_id,
                body={
                    "name": label_name,
                    "labelListVisibility": "labelHide",
                    "messageListVisibility": "hide",
                },
            )
            .execute()
        )
        log.info("Created Gmail label %r (id=%s)", label_name, created["id"])
        return created["id"]

    def add_label(self, message_id: str, label_id: str) -> None:
        """Add a label without changing read state.

        Used to mark a message handled (so it is not reprocessed) while leaving
        its UNREAD state intact — e.g. for senders that fail verification.
        """
        self._service.users().messages().modify(
            userId=self._user_id,
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    def mark_processed(self, message_id: str, processed_label_id: str) -> None:
        """Add the processed label and clear UNREAD on a message."""
        self._service.users().messages().modify(
            userId=self._user_id,
            id=message_id,
            body={"addLabelIds": [processed_label_id], "removeLabelIds": ["UNREAD"]},
        ).execute()
