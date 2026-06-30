import email
import unittest
from email.mime.multipart import MIMEMultipart
from unittest import mock

from core.upn import UPN
from scripts.mail_processor import (
    PaymentItem,
    SmtpConfig,
    _build_payment_text_block,
    _derive_verified_gmail_sender,
    _merge_payments_with_precedence,
    _send_mail,
    plan_payment_batches,
)
from parsers.xmldsig import CertificateInfo, SignatureResult, SignerInfo


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
            "scripts.mail_processor.generate_epc_qr_labeled", return_value=b"epc"
        ):
            payments = _merge_payments_with_precedence(
                text_upns=[(_upn(iban=iban, reference=reference, name="Text"), "email-body")],
                eslog_upns=[(_upn(iban=iban, reference=reference, name="eSLOG"), "invoice.xml (eSLOG XML)", None)],
                qr_payments=[],
            )

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0].upn.recipient_name, "eSLOG")
        self.assertEqual(payments[0].note, "eSLOG XML (converted to EPC SCT)")

    def test_envelope_replaces_text_for_same_payment(self) -> None:
        iban = "SI56020100012345678"
        reference = "SI00123"

        with mock.patch(
            "scripts.mail_processor.generate_upn_slip_png",
            return_value=b"slip",
        ), mock.patch("scripts.mail_processor.generate_epc_qr_labeled", return_value=b"epc"):
            payments = _merge_payments_with_precedence(
                text_upns=[(_upn(iban=iban, reference=reference, name="Text"), "email-body")],
                eslog_upns=[],
                qr_payments=[],
                envelope_upns=[
                    (_upn(iban=iban, reference=reference, name="Envelope"), "ovojnica.xml")
                ],
            )

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0].upn.recipient_name, "Envelope")
        self.assertEqual(payments[0].note, "e-račun envelope (converted to EPC SCT)")

    def test_eslog_replaces_envelope_for_same_payment(self) -> None:
        iban = "SI56020100012345678"
        reference = "SI00123"

        with mock.patch(
            "scripts.mail_processor.generate_upn_slip_png",
            return_value=b"slip",
        ), mock.patch("scripts.mail_processor.generate_epc_qr_labeled", return_value=b"epc"):
            payments = _merge_payments_with_precedence(
                text_upns=[],
                eslog_upns=[
                    (_upn(iban=iban, reference=reference, name="eSLOG"), "invoice.xml", None)
                ],
                qr_payments=[],
                envelope_upns=[
                    (_upn(iban=iban, reference=reference, name="Envelope"), "ovojnica.xml")
                ],
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
            eslog_upns=[(_upn(iban=iban, reference=reference, name="eSLOG"), "invoice.xml (eSLOG XML)", None)],
            qr_payments=[qr_item],
        )

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0].upn.recipient_name, "QR")
        self.assertEqual(payments[0].note, "UPN QR (converted to EPC SCT)")


    def test_pain_replaces_eslog_but_qr_wins(self) -> None:
        iban = "SI56020100012345678"
        reference = "SI00123"
        qr_item = PaymentItem(
            sources=["invoice.pdf#page=1"],
            kind="upn",
            note="UPN QR (converted to EPC SCT)",
            upn=_upn(iban=iban, reference=reference, name="QR"),
        )

        with mock.patch("scripts.mail_processor.generate_upn_slip_png", return_value=b"slip"), mock.patch(
            "scripts.mail_processor.generate_epc_qr_labeled", return_value=b"epc"
        ):
            pain_only = _merge_payments_with_precedence(
                text_upns=[],
                eslog_upns=[(_upn(iban=iban, reference=reference, name="eSLOG"), "x.xml", None)],
                qr_payments=[],
                pain_upns=[(_upn(iban=iban, reference=reference, name="pain"), "batch.xml (pain.001 #1)")],
            )
            with_qr = _merge_payments_with_precedence(
                text_upns=[],
                eslog_upns=[],
                qr_payments=[qr_item],
                pain_upns=[(_upn(iban=iban, reference=reference, name="pain"), "batch.xml (pain.001 #1)")],
            )

        self.assertEqual(len(pain_only), 1)
        self.assertEqual(pain_only[0].upn.recipient_name, "pain")
        self.assertEqual(pain_only[0].note, "pain.001 (converted to EPC SCT)")
        self.assertEqual(len(with_qr), 1)
        self.assertEqual(with_qr[0].upn.recipient_name, "QR")

    def test_distinct_pain_transactions_are_all_kept(self) -> None:
        with mock.patch("scripts.mail_processor.generate_upn_slip_png", return_value=b"slip"), mock.patch(
            "scripts.mail_processor.generate_epc_qr_labeled", return_value=b"epc"
        ):
            payments = _merge_payments_with_precedence(
                text_upns=[],
                eslog_upns=[],
                qr_payments=[],
                pain_upns=[
                    (_upn(iban="SI56011006030694121", reference="SI00111", name="A"), "b.xml (pain.001 #1)"),
                    (_upn(iban="SI52031001000051063", reference="", name="B"), "b.xml (pain.001 #2)"),
                ],
            )

        self.assertEqual(len(payments), 2)


def _payment_with_png(size: int) -> PaymentItem:
    return PaymentItem(
        sources=["batch.xml"],
        kind="upn",
        note="pain.001",
        epc_qr_png=b"x" * size,
    )


class TestPaymentBatching(unittest.TestCase):
    def test_empty_payments_yield_one_empty_batch(self) -> None:
        self.assertEqual(plan_payment_batches([], 1000, 0), [[]])

    def test_payments_under_limit_stay_in_one_batch(self) -> None:
        payments = [_payment_with_png(100), _payment_with_png(100)]
        batches = plan_payment_batches(payments, 10_000_000, 0)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 2)

    def test_payments_split_when_over_limit(self) -> None:
        payments = [_payment_with_png(1000) for _ in range(4)]
        # Each payment ~= 1000*2*4//3 = 2666 bytes; limit fits two per email.
        batches = plan_payment_batches(payments, 6000, 0)
        self.assertEqual([len(b) for b in batches], [2, 2])

    def test_first_batch_budget_reduced_by_originals(self) -> None:
        payments = [_payment_with_png(1000) for _ in range(3)]
        batches = plan_payment_batches(payments, 6000, first_batch_reserved=5000)
        # First email reserves space for originals, so only one payment fits.
        self.assertEqual(len(batches[0]), 1)

    def test_oversized_payment_still_gets_its_own_batch(self) -> None:
        payments = [_payment_with_png(100_000)]
        batches = plan_payment_batches(payments, 10, 0)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 1)


class TestOutboundTransport(unittest.TestCase):
    def test_send_mail_starttls_passes_verified_context(self) -> None:
        fwd = MIMEMultipart("mixed")
        recipients = ["dest@example.com"]
        smtp_cfg = SmtpConfig(
            host="smtp.example.com",
            port=587,
            user="",
            password="",
            tls="starttls",
        )

        with mock.patch("scripts.mail_processor.ssl.create_default_context", return_value=object()) as create_ctx, \
             mock.patch("scripts.mail_processor.smtplib.SMTP") as smtp_cls:
            conn = smtp_cls.return_value
            _send_mail(fwd, "qr@example.com", recipients, smtp_cfg)

        create_ctx.assert_called_once_with()
        conn.starttls.assert_called_once()
        self.assertIs(conn.starttls.call_args.kwargs["context"], create_ctx.return_value)

    def test_send_mail_smtps_passes_verified_context(self) -> None:
        fwd = MIMEMultipart("mixed")
        recipients = ["dest@example.com"]
        smtp_cfg = SmtpConfig(
            host="smtp.example.com",
            port=465,
            user="",
            password="",
            tls="tls",
        )

        with mock.patch("scripts.mail_processor.ssl.create_default_context", return_value=object()) as create_ctx, \
             mock.patch("scripts.mail_processor.smtplib.SMTP_SSL") as smtp_ssl:
            conn = smtp_ssl.return_value
            _send_mail(fwd, "qr@example.com", recipients, smtp_cfg)

        create_ctx.assert_called_once_with()
        self.assertIs(smtp_ssl.call_args.kwargs["context"], create_ctx.return_value)
        conn.sendmail.assert_called_once()

    def test_send_mail_can_opt_out_of_tls_verification(self) -> None:
        fwd = MIMEMultipart("mixed")
        recipients = ["dest@example.com"]
        smtp_cfg = SmtpConfig(
            host="smtp.example.com",
            port=587,
            user="",
            password="",
            tls="starttls",
        )

        with mock.patch.dict("os.environ", {"SMTP_INSECURE_SKIP_VERIFY": "true"}, clear=True), \
             mock.patch("scripts.mail_processor.ssl._create_unverified_context", return_value=object()) as create_ctx, \
             mock.patch("scripts.mail_processor.smtplib.SMTP") as smtp_cls:
            conn = smtp_cls.return_value
            _send_mail(fwd, "qr@example.com", recipients, smtp_cfg)

        create_ctx.assert_called_once_with()
        self.assertIs(conn.starttls.call_args.kwargs["context"], create_ctx.return_value)


class TestMailFormatting(unittest.TestCase):
    def test_eslog_signature_chain_is_included_in_text_block(self) -> None:
        payment = PaymentItem(
            sources=["invoice.xml"],
            kind="upn",
            note="eSLOG XML (converted to EPC SCT)",
            upn=_upn(iban="SI56020100012345678", reference="SI00123", name="Signer"),
            signature=SignatureResult(
                signed=True,
                valid=True,
                signer=SignerInfo(
                    subject="CN=Signer",
                    issuer="CN=SIGEN-CA G2",
                    not_before="2023-01-01 00:00:00 UTC",
                    not_after="2028-01-01 00:00:00 UTC",
                    signing_time="2026-04-02T10:16:08.058Z",
                ),
                chain=[
                    CertificateInfo(
                        subject="CN=Signer",
                        issuer="CN=SIGEN-CA G2",
                        not_before="2023-01-01 00:00:00 UTC",
                        not_after="2028-01-01 00:00:00 UTC",
                    ),
                    CertificateInfo(
                        subject="CN=SIGEN-CA G2",
                        issuer="CN=SIGOV-CA",
                        not_before="2020-01-01 00:00:00 UTC",
                        not_after="2030-01-01 00:00:00 UTC",
                    ),
                ],
            ),
        )

        with mock.patch("scripts.mail_processor.generate_upn_slip_png", return_value=b"slip"), mock.patch(
            "scripts.mail_processor.generate_epc_qr_labeled", return_value=b"epc"
        ):
            lines = _build_payment_text_block(payment)

        block = "\n".join(lines)
        self.assertIn("eSLOG signature: VALID", block)
        self.assertIn("Chain:", block)
        self.assertIn("1. Subject: CN=Signer", block)
        self.assertIn("2. Subject: CN=SIGEN-CA G2", block)


class TestGmailSenderVerification(unittest.TestCase):
    def _message(self, auth_header: str) -> email.message.Message:
        raw = (
            "From: Alice <alice@example.com>\n"
            f"Authentication-Results: mx.google.com; {auth_header}\n"
            "Subject: Test\n"
            "\n"
            "body\n"
        ).encode()
        return email.message_from_bytes(raw)

    def test_accepts_aligned_dmarc_pass(self) -> None:
        msg = self._message("dmarc=pass header.from=example.com")
        self.assertEqual(_derive_verified_gmail_sender(msg), "alice@example.com")

    def test_accepts_exact_spf_match(self) -> None:
        msg = self._message("spf=pass smtp.mailfrom=alice@example.com")
        self.assertEqual(_derive_verified_gmail_sender(msg), "alice@example.com")

    def test_rejects_misaligned_authentication(self) -> None:
        msg = self._message("spf=pass smtp.mailfrom=attacker@evil.test")
        self.assertEqual(_derive_verified_gmail_sender(msg), "")

    def test_ignores_sender_forged_lower_auth_header(self) -> None:
        # Gmail stamps its verdict at the top; a forged Authentication-Results
        # line below it (here a fake dmarc=pass) must not be trusted.
        raw = (
            "From: Attacker <evil@spoofed.test>\n"
            "Authentication-Results: mx.google.com; dmarc=fail header.from=spoofed.test\n"
            "Authentication-Results: forged.invalid; dmarc=pass header.from=spoofed.test\n"
            "Subject: Test\n"
            "\n"
            "body\n"
        ).encode()
        msg = email.message_from_bytes(raw)
        self.assertEqual(_derive_verified_gmail_sender(msg), "")
