import unittest
from unittest import mock

from scripts.gmail_client import GmailConfig, GmailConfigError
from scripts.gmail_fetch import gmail_from_clause, record_failure, resolve_mailboxes


class TestRecordFailure(unittest.TestCase):
    def test_retries_then_gives_up_with_label(self) -> None:
        client = mock.Mock()
        attempts: dict[str, int] = {}
        exc = RuntimeError("boom")

        # First two failures: counted, no label, message stays for retry.
        record_failure(
            client, "m1", attempts,
            failed_label_id="L", failed_label="qr-mail-failed", max_attempts=3, exc=exc,
        )
        record_failure(
            client, "m1", attempts,
            failed_label_id="L", failed_label="qr-mail-failed", max_attempts=3, exc=exc,
        )
        self.assertEqual(attempts["m1"], 2)
        client.add_label.assert_not_called()

        # Third failure hits the limit: label applied, counter cleared.
        record_failure(
            client, "m1", attempts,
            failed_label_id="L", failed_label="qr-mail-failed", max_attempts=3, exc=exc,
        )
        client.add_label.assert_called_once_with("m1", "L")
        self.assertNotIn("m1", attempts)

    def test_max_attempts_one_labels_immediately(self) -> None:
        client = mock.Mock()
        attempts: dict[str, int] = {}
        record_failure(
            client, "m2", attempts,
            failed_label_id="L", failed_label="qr-mail-failed", max_attempts=1,
            exc=RuntimeError("x"),
        )
        client.add_label.assert_called_once_with("m2", "L")

    def test_label_api_error_is_swallowed(self) -> None:
        client = mock.Mock()
        client.add_label.side_effect = RuntimeError("api down")
        attempts: dict[str, int] = {}
        # Should not raise even if labelling fails.
        record_failure(
            client, "m3", attempts,
            failed_label_id="L", failed_label="qr-mail-failed", max_attempts=1,
            exc=RuntimeError("x"),
        )
        client.add_label.assert_called_once()


class TestFromClauseReexport(unittest.TestCase):
    def test_reexported_helper_works(self) -> None:
        self.assertEqual(gmail_from_clause(["*@trusted.com"]), "(from:trusted.com)")


class TestResolveMailboxes(unittest.TestCase):
    def _cfg(self, mode: str) -> GmailConfig:
        return GmailConfig(auth_mode=mode, user="admin@corp.com")

    def test_no_glob_returns_single_user(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                resolve_mailboxes(self._cfg("service_account")), ["admin@corp.com"]
            )

    def test_blank_glob_returns_single_user(self) -> None:
        with mock.patch.dict("os.environ", {"DWD_USERS_GLOB": " : "}, clear=True):
            self.assertEqual(
                resolve_mailboxes(self._cfg("oauth")), ["admin@corp.com"]
            )

    def test_glob_requires_service_account(self) -> None:
        with mock.patch.dict("os.environ", {"DWD_USERS_GLOB": "*@corp.com"}, clear=True):
            with self.assertRaises(GmailConfigError):
                resolve_mailboxes(self._cfg("oauth"))

    def test_glob_filters_directory_users(self) -> None:
        users = ["a@corp.com", "b@corp.com", "c@other.com"]
        env = {"DWD_USERS_GLOB": "*@corp.com"}
        with mock.patch.dict("os.environ", env, clear=True), mock.patch(
            "scripts.gmail_fetch.list_directory_users", return_value=users
        ):
            self.assertEqual(
                resolve_mailboxes(self._cfg("service_account")),
                ["a@corp.com", "b@corp.com"],
            )

    def test_glob_matching_none_returns_empty(self) -> None:
        with mock.patch.dict(
            "os.environ", {"DWD_USERS_GLOB": "*@nope.com"}, clear=True
        ), mock.patch(
            "scripts.gmail_fetch.list_directory_users", return_value=["a@corp.com"]
        ):
            self.assertEqual(resolve_mailboxes(self._cfg("service_account")), [])


if __name__ == "__main__":
    unittest.main()
