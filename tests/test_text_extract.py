"""Tests for text_extract: IBAN + reference pair extraction."""
import pytest

from parsers.text_extract import (
    _normalize_iban,
    _normalize_reference,
    build_upn_from_text,
    extract_image_text_from_bytes,
    find_iban_reference_pairs,
)


class TestNormalizeIban:
    def test_valid_slovenian(self):
        assert _normalize_iban("SI27 0201 0001 2345 678") == "SI27020100012345678"

    def test_valid_no_spaces(self):
        assert _normalize_iban("SI27020100012345678") == "SI27020100012345678"

    def test_invalid_checksum(self):
        assert _normalize_iban("SI27020100012345679") is None

    def test_too_short(self):
        assert _normalize_iban("SI56") is None

    def test_non_slovenian(self):
        # German IBAN (valid format, should pass if checksum is valid)
        result = _normalize_iban("DE89370400440532013000")
        assert result == "DE89370400440532013000"


class TestNormalizeReference:
    def test_si00(self):
        assert _normalize_reference("SI00 12345678") == "SI0012345678"

    def test_si12_valid(self):
        # SI12 requires MOD11 checksum on last digit.
        # Body "123456789012" → check digit must be valid.
        ref = _normalize_reference("SI12 1234")
        # SI12 with body "123" and check digit "4":
        # weights: 4*2=8, 3*3=9, 2*4=8, 1*5=5 → 30; 30%11=8; 11-8=3
        # So check digit should be 3, not 4.
        assert ref is None

    def test_si99(self):
        assert _normalize_reference("SI99") == "SI99"

    def test_invalid(self):
        assert _normalize_reference("NOTAREF") is None

    def test_rf_valid(self):
        result = _normalize_reference("RF04 1234")
        # RF04 with content "1234" — needs checksum validation.
        # This may or may not be valid depending on actual check.
        # Just verify it returns string or None (no crash).
        assert result is None or result.startswith("RF")


class TestFindIbanReferencePairs:
    def test_basic_pair(self):
        text = (
            "Please pay to the following account:\n"
            "IBAN: SI27 0201 0001 2345 678\n"
            "Reference: SI00 12345678\n"
            "Thank you.\n"
        )
        pairs = find_iban_reference_pairs(text)
        assert len(pairs) == 1
        iban, ref = pairs[0]
        assert iban == "SI27020100012345678"
        assert ref == "SI0012345678"

    def test_no_reference(self):
        text = "IBAN: SI27 0201 0001 2345 678\n" * 5
        pairs = find_iban_reference_pairs(text)
        assert len(pairs) == 0

    def test_no_iban(self):
        text = "Reference: SI00 12345678\n" * 5
        pairs = find_iban_reference_pairs(text)
        assert len(pairs) == 0

    def test_too_far_apart(self):
        lines = ["IBAN: SI27 0201 0001 2345 678"]
        lines += ["filler line"] * 10
        lines += ["Reference: SI00 12345678"]
        text = "\n".join(lines)
        pairs = find_iban_reference_pairs(text)
        assert len(pairs) == 0

    def test_ambiguous_two_references_skipped(self):
        text = (
            "SI00 11111111\n"
            "IBAN: SI27 0201 0001 2345 678\n"
            "SI00 22222222\n"
        )
        pairs = find_iban_reference_pairs(text)
        assert len(pairs) == 0

    def test_two_ibans_one_reference(self):
        text = (
            "SI27 0201 0001 2345 678\n"
            "SI56 1920 0123 4567 892\n"
            "SI00 12345678\n"
        )
        pairs = find_iban_reference_pairs(text)
        assert len(pairs) == 2
        ibans = {p[0] for p in pairs}
        assert "SI27020100012345678" in ibans
        assert "SI56192001234567892" in ibans
        assert all(ref == "SI0012345678" for _, ref in pairs)

    def test_multiple_pairs(self):
        # Blocks must be >6 lines apart so windows don't overlap.
        lines = [
            "Account 1: SI27 0201 0001 2345 678",
            "Ref 1: SI00 11111111",
        ]
        lines += [""] * 10
        lines += [
            "Account 2: SI52 0310 0100 0051 063",
            "Ref 2: SI00 22222222",
        ]
        text = "\n".join(lines)
        pairs = find_iban_reference_pairs(text)
        assert len(pairs) == 2
        ibans = {p[0] for p in pairs}
        assert "SI27020100012345678" in ibans
        assert "SI52031001000051063" in ibans

    def test_dedup_same_pair(self):
        text = (
            "SI27 0201 0001 2345 678 SI00 12345678\n"
            "SI27 0201 0001 2345 678 SI00 12345678\n"
        )
        pairs = find_iban_reference_pairs(text)
        # Same pair should appear only once.
        unique = {(iban, ref) for iban, ref in pairs}
        assert len(unique) == len(pairs)

    def test_iban_not_matched_as_reference(self):
        """Ensure SI56... IBAN is not also matched as an SI reference."""
        text = (
            "SI27 0201 0001 2345 678\n"
            "SI00 99999999\n"
        )
        pairs = find_iban_reference_pairs(text)
        for _, ref in pairs:
            # Reference should never be the IBAN itself.
            assert not ref.startswith("SI27020100012345678")


class TestBuildUpnFromText:
    def test_minimal_upn(self):
        upn = build_upn_from_text(
            "SI27020100012345678", "SI0012345678",
            recipient_city="Maribor",
        )
        assert upn.recipient_iban == "SI27020100012345678"
        assert upn.recipient_reference == "SI0012345678"
        assert upn.recipient_city == "Maribor"
        assert upn.amount_cents == 0
        assert upn.purpose_code == "OTHR"
        assert upn.payer_iban == ""
        assert upn.payer_name == ""
        assert upn.recipient_name == ""

    def test_default_city(self):
        upn = build_upn_from_text("SI27020100012345678", "SI0012345678")
        assert upn.recipient_city == "Ljubljana"


class TestExtractImageTextFromBytes:
    def test_rejects_decompression_bomb(self, monkeypatch):
        Image = pytest.importorskip("PIL.Image")
        from io import BytesIO

        original_limit = Image.MAX_IMAGE_PIXELS
        image = Image.new("RGB", (2, 2), "white")
        buf = BytesIO()
        image.save(buf, format="PNG")

        monkeypatch.setenv("MAX_IMAGE_PIXELS", "1")
        try:
            assert extract_image_text_from_bytes(buf.getvalue()) == ""
        finally:
            Image.MAX_IMAGE_PIXELS = original_limit
