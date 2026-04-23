import unittest
from email.mime.multipart import MIMEMultipart
from unittest import mock

from core.upn import UPN
from scripts.mail_processor import (
    GmailConfig,
    PaymentItem,
    SmtpConfig,
    _load_gmail_config,
    _merge_payments_with_precedence,
    _send_mail,
)


def _upn(*, iban: str, reference: str, name: str) -> UPN:
    return UPN(
        payer_iban="",
        deposit=False,
        withdrawal=False,
        payer_reference="",
        payer_name="",
        payer_street="",
        payer_city="",
        amount_cents=100,
        payment_date=None,
        urgent=False,
        purpose_code="OTHR",
        payment_purpose="",
        payment_deadline=None,
        recipient_iban=iban,
        recipient_reference=reference,
        recipient_name=name,
        recipient_street="",
        recipient_city="Ljubljana",
    )


class TestPaymentPrecedence(unittest.TestCase):
    def test_eslog_replaces_text_for_same_payment(self) -> None:
        iban = "SI56020100012345678"
        reference = "SI00123"

        with mock.patch("scripts.mail_processor.generate_upn_slip_png", return_value=b"slip"), mock.patch(
            "scripts.mail_processor.generate_epc_qr", return_value=b"epc"
        ):
            payments = _merge_payments_with_precedence(
                text_upns=[(_upn(iban=iban, reference=reference, name="Text"), "email-body")],
                eslog_upns=[(_upn(iban=iban, reference=reference, name="eSLOG"), "invoice.xml (eSLOG XML)")],
                qr_payments=[],
            )

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0].upn.recipient_name, "eSLOG")
        self.assertEqual(payments[0].note, "eSLOG XML (converted to EPC SCT)")

    def test_qr_replaces_text_for_same_payment(self) -> None:
        iban = "SI56020100012345678"
        reference = "SI00123"
        qr_item = PaymentItem(
            sources=["invoice.pdf#page=1"],
            kind="upn",
            note="UPN QR (converted to EPC SCT)",
            upn=_upn(iban=iban, reference=reference, name="QR"),
        )

        payments = _merge_payments_with_precedence(
            text_upns=[(_upn(iban=iban, reference=reference, name="Text"), "email-body")],
            eslog_upns=[],
            qr_payments=[qr_item],
        )

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0].upn.recipient_name, "QR")
        self.assertEqual(payments[0].note, "UPN QR (converted to EPC SCT)")

    def test_qr_replaces_eslog_for_same_payment(self) -> None:
        iban = "SI56020100012345678"
        reference = "SI00123"
        qr_item = PaymentItem(
            sources=["invoice.pdf#page=1"],
            kind="upn",
            note="UPN QR (converted to EPC SCT)",
            upn=_upn(iban=iban, reference=reference, name="QR"),
        )

        payments = _merge_payments_with_precedence(
            text_upns=[],
            eslog_upns=[(_upn(iban=iban, reference=reference, name="eSLOG"), "invoice.xml (eSLOG XML)")],
            qr_payments=[qr_item],
        )

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0].upn.recipient_name, "QR")
        self.assertEqual(payments[0].note, "UPN QR (converted to EPC SCT)")


class TestOutboundTransport(unittest.TestCase):
    def test_load_gmail_config_requires_both_values(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(_load_gmail_config())

        with mock.patch.dict("os.environ", {"GMAIL_SERVICE_ACCOUNT_FILE": "/tmp/key.json"}, clear=True):
            self.assertIsNone(_load_gmail_config())

        with mock.patch.dict(
            "os.environ",
            {
                "GMAIL_SERVICE_ACCOUNT_FILE": "/tmp/key.json",
                "GMAIL_IMPERSONATE_ADDRESS": "qr@example.com",
            },
            clear=True,
        ):
            cfg = _load_gmail_config()

        self.assertEqual(
            cfg,
            GmailConfig(
                service_account_file="/tmp/key.json",
                impersonate_address="qr@example.com",
            ),
        )

    def test_send_mail_prefers_gmail_api_over_smtp(self) -> None:
        fwd = MIMEMultipart("mixed")
        fwd["From"] = "qr@example.com"
        fwd["To"] = "dest@example.com"
        recipients = ["dest@example.com"]
        smtp_cfg = SmtpConfig(
            host="smtp.example.com",
            port=587,
            user="user",
            password="pass",
            tls="starttls",
        )
        gmail_cfg = GmailConfig(
            service_account_file="/tmp/key.json",
            impersonate_address="qr@example.com",
        )

        with mock.patch("scripts.mail_processor._send_mail_via_gmail_api") as gmail_send, \
             mock.patch("scripts.mail_processor.smtplib.SMTP") as smtp_send, \
             mock.patch("scripts.mail_processor.subprocess.run") as sendmail_run:
            _send_mail(fwd, "qr@example.com", recipients, smtp_cfg, gmail_cfg)

        gmail_send.assert_called_once_with(fwd, recipients, gmail_cfg)
        smtp_send.assert_not_called()
        sendmail_run.assert_not_called()
