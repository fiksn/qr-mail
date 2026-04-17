import unittest

from upn import UPNReferenceError, validate_upn_reference


class TestUPNReferences(unittest.TestCase):
    def test_empty_ok(self) -> None:
        self.assertEqual(validate_upn_reference(""), "")
        self.assertEqual(validate_upn_reference("   "), "")

    def test_rf_valid_compacts_and_uppercases(self) -> None:
        # Common example used in documentation for ISO 11649.
        self.assertEqual(
            validate_upn_reference("rf18 5390 0754 7034"),
            "RF18539007547034",
        )

    def test_rf_invalid_checksum(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("RF17539007547034")  # changed check digits

    def test_si00_accepts_groups(self) -> None:
        self.assertEqual(validate_upn_reference("SI00 123-456-789"), "SI00123-456-789")

    def test_si99_accepts_groups(self) -> None:
        self.assertEqual(validate_upn_reference("SI99 1-2-3"), "SI991-2-3")

    def test_si_unknown_model_rejected(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI01 123-456")

    def test_si12_validates_mod11_check_digit(self) -> None:
        # Body 660487647593 has MOD11 check digit 1 with weights 2..13 (right to left).
        self.assertEqual(validate_upn_reference("SI12 6604876475931"), "SI126604876475931")

    def test_si12_invalid_check_digit(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI12 6604876475932")

    def test_si07_validates_mod11_check_digit_in_second_group(self) -> None:
        # SI07 model uses a MOD11 check digit as the last digit of the second group (P2).
        # Here P2 body=12345 -> check digit 5 -> P2=123455.
        self.assertEqual(validate_upn_reference("SI07 19-123455-84503"), "SI0719-123455-84503")

    def test_si07_invalid_check_digit(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI07 19-123454-84503")

    def test_rejects_unknown_prefix(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("AB12 123")


if __name__ == "__main__":
    unittest.main()
