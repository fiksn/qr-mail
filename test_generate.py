import unittest

from generate import upn_to_epc
from upn import UPN


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
        self.assertIsNone(epc.amount)


if __name__ == "__main__":
    unittest.main()
