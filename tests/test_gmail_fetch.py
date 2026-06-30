import unittest
from unittest import mock

from scripts.gmail_fetch import gmail_from_clause, record_failure


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


if __name__ == "__main__":
    unittest.main()
