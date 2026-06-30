import email
import unittest
from unittest import mock

from core.upn import UPN
from scripts.gmail_reply import (
    build_reply,
    deliver_replies,
    gmail_from_clause,
    process_message,
)
from core.payments import PaymentItem


def _payment(name: str, *, png: bytes = b"x" * 100) -> PaymentItem:
    return PaymentItem(
        sources=["batch.xml (pain.001 #1)"],
        kind="upn",
        note="pain.001 (converted to EPC SCT)",
        upn=UPN(
            payer_iban="", deposit=False, withdrawal=False, payer_reference="",
            payer_name="", payer_street="", payer_city="",
            amount_cents=100, payment_date=None, urgent=False,
            purpose_code="OTHR", payment_purpose="", payment_deadline=None,
            recipient_iban="SI56011006030694121", recipient_reference="",
            recipient_name=name, recipient_street="", recipient_city="Ljubljana",
        ),
        epc_qr_png=png,
    )


def _original() -> email.message.Message:
    raw = (
        "From: Vendor <billing@trusted.com>\n"
        "To: qr@example.com\n"
        "Subject: Invoice 7\n"
        "Message-ID: <orig-123@trusted.com>\n"
        "\n"
        "See attached.\n"
    ).encode()
    return email.message_from_bytes(raw)


class TestGmailFromClause(unittest.TestCase):
    def test_domain_glob_and_exact(self) -> None:
        clause = gmail_from_clause(["*@trusted.com", "noreply@google.com"])
        self.assertEqual(clause, "(from:trusted.com OR from:noreply@google.com)")

    def test_unexpressible_pattern_returns_empty(self) -> None:
        self.assertEqual(gmail_from_clause(["user@*"]), "")
        self.assertEqual(gmail_from_clause(["*"]), "")

    def test_empty(self) -> None:
        self.assertEqual(gmail_from_clause([]), "")


class TestBuildReply(unittest.TestCase):
    def test_reply_is_threaded_and_has_no_originals(self) -> None:
        reply = build_reply(
            _original(),
            "qr@example.com",
            "qr@example.com",
            [_payment("Acme")],
            in_reply_to="<orig-123@trusted.com>",
        )
        self.assertEqual(reply["From"], "qr@example.com")
        self.assertEqual(reply["Subject"], "Re: Invoice 7")
        self.assertEqual(reply["In-Reply-To"], "<orig-123@trusted.com>")
        self.assertIn("<orig-123@trusted.com>", reply["References"])
        # Only the generated EPC QR is attached — no original payload.
        filenames = [p.get_filename() for p in reply.walk() if p.get_filename()]
        self.assertIn("payment_1_epc_qr.png", filenames)

    def test_existing_re_prefix_not_doubled(self) -> None:
        orig = _original()
        orig.replace_header("Subject", "Re: Invoice 7")
        reply = build_reply(orig, "qr@example.com", "qr@example.com", [_payment("Acme")])
        self.assertEqual(reply["Subject"], "Re: Invoice 7")


class TestDeliverReplies(unittest.TestCase):
    def test_no_payments_inserts_nothing(self) -> None:
        client = mock.Mock()
        count = deliver_replies(client, _original(), [], my_address="qr@example.com")
        self.assertEqual(count, 0)
        client.insert_reply.assert_not_called()

    def test_splits_by_size_into_multiple_replies(self) -> None:
        client = mock.Mock()
        client.user_id = "qr@example.com"
        # Each payment ~= 100*2*4//3 bytes; a tiny limit forces one per reply.
        payments = [_payment("A"), _payment("B"), _payment("C")]
        count = deliver_replies(
            client, _original(), payments,
            my_address="qr@example.com", thread_id="T1", max_email_bytes=200,
        )
        self.assertEqual(count, 3)
        self.assertEqual(client.insert_reply.call_count, 3)
        for call in client.insert_reply.call_args_list:
            self.assertEqual(call.kwargs["thread_id"], "T1")


class TestProcessMessage(unittest.TestCase):
    def _raw_from(self, sender: str) -> bytes:
        return (
            f"From: {sender}\n"
            "To: qr@example.com\n"
            "Subject: Invoice\n"
            "Message-ID: <m@x>\n"
            "\n"
            "body\n"
        ).encode()

    def test_skips_disallowed_sender(self) -> None:
        client = mock.Mock()
        status = process_message(
            client,
            self._raw_from("evil@elsewhere.com"),
            my_address="qr@example.com",
            allowed_patterns=["*@trusted.com"],
            strict_sender=False,
        )
        self.assertEqual(status, "skipped-sender")
        client.insert_reply.assert_not_called()

    def test_allowed_sender_without_payments_is_skipped_empty(self) -> None:
        client = mock.Mock()
        with mock.patch(
            "scripts.gmail_reply.scan_message_for_payments", return_value=([], []),
        ):
            status = process_message(
                client,
                self._raw_from("billing@trusted.com"),
                my_address="qr@example.com",
                allowed_patterns=["*@trusted.com"],
                strict_sender=False,
            )
        self.assertEqual(status, "skipped-empty")
        client.insert_reply.assert_not_called()

    def test_allowed_sender_with_payments_inserts_reply(self) -> None:
        client = mock.Mock()
        client.user_id = "qr@example.com"
        with mock.patch(
            "scripts.gmail_reply.scan_message_for_payments",
            return_value=([_payment("Acme")], []),
        ):
            status = process_message(
                client,
                self._raw_from("billing@trusted.com"),
                my_address="qr@example.com",
                allowed_patterns=["*@trusted.com"],
                strict_sender=False,
            )
        self.assertEqual(status, "sent")
        client.insert_reply.assert_called_once()
