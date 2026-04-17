import unittest

from epc import EPCParseError, parse_epc


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


if __name__ == "__main__":
    unittest.main()

