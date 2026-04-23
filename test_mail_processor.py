import unittest

from mail_processor import PaymentItem, _merge_payments_with_precedence
from upn import UPN


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
