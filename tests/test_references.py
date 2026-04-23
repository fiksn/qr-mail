import unittest

from core.upn import UPNReferenceError, _si_mod11_check_digit, validate_upn_reference


def _check(body: str) -> int:
    """Shorthand: MOD11 check digit for a given body string."""
    return _si_mod11_check_digit(body)


class TestUPNReferences(unittest.TestCase):
    def test_empty_ok(self) -> None:
        self.assertEqual(validate_upn_reference(""), "")
        self.assertEqual(validate_upn_reference("   "), "")

    # ── RF ────────────────────────────────────────────────────────────────────

    def test_rf_valid_compacts_and_uppercases(self) -> None:
        self.assertEqual(
            validate_upn_reference("rf18 5390 0754 7034"),
            "RF18539007547034",
        )

    def test_rf_invalid_checksum(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("RF17539007547034")

    # ── SI00 / SI99 ───────────────────────────────────────────────────────────

    def test_si00_accepts_groups(self) -> None:
        self.assertEqual(validate_upn_reference("SI00 123-456-789"), "SI00123-456-789")

    def test_si99_no_content(self) -> None:
        # SI99 has zero groups per spec
        self.assertEqual(validate_upn_reference("SI99"), "SI99")

    def test_si99_rejects_content(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI99 1-2-3")

    # ── SI12 ──────────────────────────────────────────────────────────────────

    def test_si12_validates_mod11_check_digit(self) -> None:
        # Body 660487647593 → check digit = _check("660487647593") = 1
        self.assertEqual(validate_upn_reference("SI12 6604876475931"), "SI126604876475931")

    def test_si12_invalid_check_digit(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI12 6604876475932")

    # ── SI05: (P1)K - P2 - P3 ────────────────────────────────────────────────

    def test_si05_validates_mod11_on_p1(self) -> None:
        # Spec example: SI05 19-1235-84503 → P1="19", body="1", check=_check("1")=9 ✓
        self.assertEqual(validate_upn_reference("SI05 19-1235-84503"), "SI0519-1235-84503")

    def test_si05_single_group(self) -> None:
        # With 1 group: just (P1)K
        body = "123"
        ref = f"SI05 {body}{_check(body)}"
        self.assertEqual(validate_upn_reference(ref), f"SI05{body}{_check(body)}")

    def test_si05_invalid_check_digit(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI05 18-1235-84503")  # P1 check wrong

    def test_si05_allows_hyphens(self) -> None:
        # SI05 uses grouped format (unlike SI12)
        body = "1"
        p1 = f"{body}{_check(body)}"  # "19"
        self.assertIsNotNone(validate_upn_reference(f"SI05 {p1}-999"))

    # ── SI07: P1 - (P2)K - P3 ────────────────────────────────────────────────

    def test_si07_validates_mod11_in_p2(self) -> None:
        self.assertEqual(validate_upn_reference("SI07 19-123455-84503"), "SI0719-123455-84503")

    def test_si07_invalid_check_digit(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI07 19-123454-84503")

    # ── SI19: (P1)K[8-digit] - (P2)K - P3 ───────────────────────────────────

    def test_si19_validates_tax_id_and_p2(self) -> None:
        # P1=69616469: body=6961646, check=_check("6961646")=9 ✓
        # P2=45004: body=4500, check=_check("4500")=?
        p2_body = "4500"
        p2 = f"{p2_body}{_check(p2_body)}"
        ref = f"SI19 69616469-{p2}"
        self.assertEqual(validate_upn_reference(ref), f"SI1969616469-{p2}")

    def test_si19_invalid_p1_checksum(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI19 69616468-45004")

    def test_si19_p1_wrong_length(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI19 1234567-45004")  # 7 digits, must be 8

    # ── SI01: (P1 - P2 - P3)K combined ───────────────────────────────────────

    def test_si01_single_group(self) -> None:
        body = "123"
        ref = f"SI01 {body}{_check(body)}"
        self.assertEqual(validate_upn_reference(ref), f"SI01{body}{_check(body)}")

    def test_si01_two_groups_combined_check(self) -> None:
        # Combined body = P1 + P2[:-1]; check digit = last digit of P2
        p1 = "123"
        p2_body = "456"
        combined_check = _check(p1 + p2_body)
        ref = f"SI01 {p1}-{p2_body}{combined_check}"
        self.assertEqual(validate_upn_reference(ref), f"SI01{p1}-{p2_body}{combined_check}")

    def test_si01_invalid_combined_check(self) -> None:
        # combined body "123456" → _check = 0; using 1 instead → invalid
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI01 123-4561")

    # ── SI02: P1 - (P2)K - (P3)K ────────────────────────────────────────────

    def test_si02_validates_p2_and_p3(self) -> None:
        p1 = "100"
        p2_body = "200"
        p3_body = "300"
        ref = f"SI02 {p1}-{p2_body}{_check(p2_body)}-{p3_body}{_check(p3_body)}"
        self.assertEqual(validate_upn_reference(ref), ref.replace(" ", "").upper())

    # ── SI11: (P1)K - (P2)K - P3 ────────────────────────────────────────────

    def test_si11_two_groups(self) -> None:
        p1_body = "100"
        p2_body = "200"
        ref = f"SI11 {p1_body}{_check(p1_body)}-{p2_body}{_check(p2_body)}"
        self.assertEqual(validate_upn_reference(ref), ref.replace(" ", "").upper())

    # ── SI09: (P1 - P2)K - P3 ────────────────────────────────────────────────

    def test_si09_two_groups_combined_check(self) -> None:
        p1 = "123"
        p2_body = "456"
        combined_check = _check(p1 + p2_body)
        ref = f"SI09 {p1}-{p2_body}{combined_check}"
        self.assertEqual(validate_upn_reference(ref), f"SI09{p1}-{p2_body}{combined_check}")

    def test_si09_three_groups_p3_free(self) -> None:
        p1 = "123"
        p2_body = "456"
        combined_check = _check(p1 + p2_body)
        ref = f"SI09 {p1}-{p2_body}{combined_check}-789"
        self.assertEqual(validate_upn_reference(ref), ref.replace(" ", "").upper())

    # ── Unknown model ─────────────────────────────────────────────────────────

    def test_si_unknown_model_rejected(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("SI13 123-456")

    def test_rejects_unknown_prefix(self) -> None:
        with self.assertRaises(UPNReferenceError):
            validate_upn_reference("AB12 123")


if __name__ == "__main__":
    unittest.main()
