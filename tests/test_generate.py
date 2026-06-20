import unittest

from core.generate import DEFAULT_EPC_AMOUNT, upn_to_epc
from core.upn import UPN, UPNLegacyOCRError, format_legacy_upn_ocr


class TestUPNToEPC(unittest.TestCase):
    def _upn(
        self,
        *,
        recipient_reference: str,
        payment_purpose: str = "PURPOSE",
        recipient_name: str = "ACME",
        amount_cents: int = 123,
    ) -> UPN:
        return UPN(
            payer_iban="",
            deposit=False,
            withdrawal=False,
            payer_reference="",
            payer_name="",
            payer_street="",
            payer_city="",
            amount_cents=amount_cents,
            payment_date=None,
            urgent=False,
            purpose_code="",
            payment_purpose=payment_purpose,
            payment_deadline=None,
            recipient_iban="SI56192001234567892",
            recipient_reference=recipient_reference,
            recipient_name=recipient_name,
            recipient_street="",
            recipient_city="Ljubljana",
        )

    def test_invalid_rf_reference_kept_as_unstructured(self) -> None:
        epc = upn_to_epc(self._upn(recipient_reference="RF00INVALID"))
        self.assertEqual(epc.structured_ref, "")
        self.assertIn("RF00INVALID", epc.unstructured_ref)

    def test_valid_rf_reference_goes_to_structured_and_purpose_to_originator_info(self) -> None:
        epc = upn_to_epc(self._upn(recipient_reference="RF18539007547034", payment_purpose="hello"))
        self.assertEqual(epc.structured_ref, "RF18539007547034")
        self.assertEqual(epc.unstructured_ref, "")
        self.assertEqual(epc.originator_info, "hello")

    def test_missing_recipient_name_uses_default_for_epc(self) -> None:
        epc = upn_to_epc(
            self._upn(
                recipient_reference="SI001234",
                payment_purpose="",
                recipient_name="",
                amount_cents=0,
            )
        )
        self.assertEqual(epc.beneficiary_name, "PREJEMNIK")
        self.assertEqual(epc.amount, DEFAULT_EPC_AMOUNT)

    def test_missing_amount_defaults_to_minimum(self) -> None:
        epc = upn_to_epc(self._upn(recipient_reference="SI001234", amount_cents=0))
        self.assertEqual(epc.amount, DEFAULT_EPC_AMOUNT)

    def test_present_amount_is_preserved(self) -> None:
        epc = upn_to_epc(self._upn(recipient_reference="SI001234", amount_cents=5629))
        self.assertEqual(epc.amount, self._upn(recipient_reference="SI001234", amount_cents=5629).amount)

    def test_format_legacy_upn_ocr_uses_expected_segments(self) -> None:
        upn = self._upn(
            recipient_reference="SI126604876475931",
            amount_cents=5629,
        )
        self.assertEqual(
            format_legacy_upn_ocr(upn),
            "6604876475931 1234567892 000000005629 19200000 56",
        )

    def test_format_legacy_upn_ocr_requires_si12_reference(self) -> None:
        with self.assertRaises(UPNLegacyOCRError):
            format_legacy_upn_ocr(self._upn(recipient_reference="SI001234"))


if __name__ == "__main__":
    unittest.main()
