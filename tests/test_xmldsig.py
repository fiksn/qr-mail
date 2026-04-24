import os

import pytest

from parsers.xmldsig import verify_eslog_signature

SAMPLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "racun_26-390-0438150.xml",
)

needs_sample = pytest.mark.skipif(
    not os.path.exists(SAMPLE_PATH),
    reason=f"sample invoice not found: {SAMPLE_PATH}",
)


@pytest.fixture
def sample_xml() -> bytes:
    with open(SAMPLE_PATH, "rb") as f:
        return f.read()


@needs_sample
def test_verify_valid_signature(sample_xml: bytes) -> None:
    result = verify_eslog_signature(sample_xml)
    assert result.signed is True
    assert result.valid is True
    assert result.signer is not None
    assert "T-2" in result.signer.subject
    assert "SIGEN-CA" in result.signer.issuer
    assert result.signer.signing_time is not None
    assert result.error is None


def test_unsigned_document() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:eslog:2.00"><M_INVOIC/></Invoice>"""
    result = verify_eslog_signature(xml)
    assert result.signed is False
    assert result.valid is None


@needs_sample
def test_tampered_content(sample_xml: bytes) -> None:
    tampered = sample_xml.replace(b"58.27", b"99.99")
    result = verify_eslog_signature(tampered)
    assert result.signed is True
    assert result.valid is False
    assert result.error is not None


def test_non_xml() -> None:
    result = verify_eslog_signature(b"not xml at all")
    assert result.signed is False
