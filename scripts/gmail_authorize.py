#!/usr/bin/env python3
"""One-time OAuth consent for single-user Gmail setup.

Runs the installed-app OAuth flow in a browser and writes a cached, refreshable
token. After this, the daemon (scripts/gmail_fetch.py) and backfill run headless
using the token alone — no further browser interaction.

Usage:
  GMAIL_IMPERSONATE_ADDRESS=me@example.com \\
  GMAIL_OAUTH_CLIENT_SECRET_FILE=client_secret.json \\
  GMAIL_OAUTH_TOKEN_FILE=token.json \\
  python3 scripts/gmail_authorize.py

This is only for the single-user OAuth mode. Service-account / domain-wide
delegation needs no consent flow.
"""
from __future__ import annotations

import os
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.gmail_client import GmailConfigError, get_credentials, load_gmail_config


def main() -> None:
    try:
        cfg = load_gmail_config()
    except GmailConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if cfg is None or cfg.auth_mode != "oauth":
        print(
            "ERROR: configure single-user OAuth first: set GMAIL_IMPERSONATE_ADDRESS, "
            "GMAIL_OAUTH_CLIENT_SECRET_FILE and GMAIL_OAUTH_TOKEN_FILE "
            "(do not set GMAIL_SERVICE_ACCOUNT_FILE).",
            file=sys.stderr,
        )
        sys.exit(1)

    if not cfg.token_file:
        print("ERROR: GMAIL_OAUTH_TOKEN_FILE is required to store the token", file=sys.stderr)
        sys.exit(1)

    # Triggers the browser consent flow and writes the token file.
    get_credentials(cfg)
    print(f"Authorized {cfg.user}; token written to {cfg.token_file}")


if __name__ == "__main__":
    main()
