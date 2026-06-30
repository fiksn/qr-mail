import unittest
from unittest import mock

from scripts.gmail_client import GmailConfig, GmailConfigError, load_gmail_config


class TestLoadGmailConfig(unittest.TestCase):
    def test_no_config_returns_none(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(load_gmail_config())

    def test_service_account_mode(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "GMAIL_SERVICE_ACCOUNT_FILE": "/run/key.json",
                "GMAIL_IMPERSONATE_ADDRESS": "qr@example.com",
            },
            clear=True,
        ):
            cfg = load_gmail_config()
        self.assertEqual(
            cfg,
            GmailConfig(
                auth_mode="service_account",
                user="qr@example.com",
                service_account_file="/run/key.json",
            ),
        )

    def test_service_account_requires_mailbox(self) -> None:
        with mock.patch.dict(
            "os.environ", {"GMAIL_SERVICE_ACCOUNT_FILE": "/run/key.json"}, clear=True
        ):
            with self.assertRaises(GmailConfigError):
                load_gmail_config()

    def test_oauth_mode(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "GMAIL_IMPERSONATE_ADDRESS": "me@example.com",
                "GMAIL_OAUTH_TOKEN_FILE": "/home/me/token.json",
                "GMAIL_OAUTH_CLIENT_SECRET_FILE": "/home/me/client.json",
            },
            clear=True,
        ):
            cfg = load_gmail_config()
        self.assertEqual(cfg.auth_mode, "oauth")
        self.assertEqual(cfg.user, "me@example.com")
        self.assertEqual(cfg.token_file, "/home/me/token.json")

    def test_service_account_takes_precedence_over_oauth(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "GMAIL_IMPERSONATE_ADDRESS": "qr@example.com",
                "GMAIL_SERVICE_ACCOUNT_FILE": "/run/key.json",
                "GMAIL_OAUTH_TOKEN_FILE": "/home/me/token.json",
            },
            clear=True,
        ):
            cfg = load_gmail_config()
        self.assertEqual(cfg.auth_mode, "service_account")

    def test_oauth_requires_mailbox(self) -> None:
        with mock.patch.dict(
            "os.environ", {"GMAIL_OAUTH_TOKEN_FILE": "/home/me/token.json"}, clear=True
        ):
            with self.assertRaises(GmailConfigError):
                load_gmail_config()


if __name__ == "__main__":
    unittest.main()
