import unittest

from core.epc import EPC, EPCParseError, parse_epc
from core.generate import epc_to_string


class TestEPC(unittest.TestCase):
    def test_valid_iban_ok(self) -> None:
        payload = "\n".join(
            [
                "BCD",
                "002",
                "1",
                "SCT",
                "",
                "ACME",
                "SI56192001234567892",
                "EUR1.00",
                "",
                "",
                "hello",
                "",
            ]
        )
        epc = parse_epc(payload)
        self.assertEqual(epc.beneficiary_iban, "SI56192001234567892")

    def test_invalid_iban_rejected(self) -> None:
        payload = "\n".join(
            [
                "BCD",
                "002",
                "1",
                "SCT",
                "",
                "ACME",
                "SI56012345678901234",  # bad checksum
                "EUR1.00",
                "",
                "",
                "hello",
                "",
            ]
        )
        with self.assertRaises(EPCParseError):
            parse_epc(payload)

    def test_parse_epc_allows_trimmed_trailing_empty_fields(self) -> None:
        payload = "\n".join(
            [
                "BCD",
                "002",
                "1",
                "SCT",
                "",
                "PREJEMNIK",
                "SI56020110011572211",
                "",
                "OTHR",
                "",
                "SI004263",
            ]
        )
        epc = parse_epc(payload)
        self.assertEqual(epc.beneficiary_name, "PREJEMNIK")
        self.assertEqual(epc.unstructured_ref, "SI004263")

    def test_epc_to_string_trims_trailing_empty_fields(self) -> None:
        payload = epc_to_string(
            EPC(
                version="002",
                charset="utf-8",
                bic="",
                beneficiary_name="PREJEMNIK",
                beneficiary_iban="SI56020110011572211",
                amount=None,
                purpose_code="OTHR",
                structured_ref="",
                unstructured_ref="SI004263",
                originator_info="",
            )
        )
        self.assertFalse(payload.endswith("\n"))
        self.assertEqual(payload.split("\n")[-1], "SI004263")


if __name__ == "__main__":
    unittest.main()
