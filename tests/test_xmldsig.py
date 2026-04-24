from datetime import UTC, datetime, timedelta
import os
import tempfile

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from parsers.xmldsig import (
    _skip_chain_validation_enabled,
    _verify_certificate_trust,
    verify_eslog_signature,
)

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
    assert result.signer is not None
    assert "T-2" in result.signer.subject
    assert "SIGEN-CA" in result.signer.issuer
    assert result.signer.signing_time is not None
    if result.valid is True:
        assert result.error is None
    else:
        assert result.valid is False
        assert result.error is not None
        assert "certificate trust verification failed" in result.error


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


def _build_cert(subject_cn: str, issuer_name, issuer_key, subject_key, *, is_ca: bool, not_before: datetime, not_after: datetime) -> x509.Certificate:
    subject_public_key = subject_key.public_key()
    builder = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
        )
        .issuer_name(issuer_name)
        .public_key(subject_public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(subject_public_key),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    )
    if is_ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    else:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                key_cert_sign=False,
                crl_sign=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        ).add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        ).add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name("signer@example.com")]),
            critical=False,
        )
    return builder.sign(private_key=issuer_key, algorithm=hashes.SHA256())


def _generate_chain(*, expired_leaf: bool = False) -> tuple[x509.Certificate, x509.Certificate]:
    now = datetime.now(UTC)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root CA")])
    root_cert = _build_cert(
        "Test Root CA",
        root_name,
        root_key,
        root_key,
        is_ca=True,
        not_before=now - timedelta(days=30),
        not_after=now + timedelta(days=30),
    )
    leaf_cert = _build_cert(
        "Signer",
        root_cert.subject,
        root_key,
        leaf_key,
        is_ca=False,
        not_before=now - timedelta(days=2),
        not_after=now - timedelta(days=1) if expired_leaf else now + timedelta(days=2),
    )
    return root_cert, leaf_cert


def test_verify_certificate_trust_accepts_trusted_chain() -> None:
    root_cert, leaf_cert = _generate_chain()
    with tempfile.NamedTemporaryFile("wb", suffix=".pem") as trust_file:
        trust_file.write(root_cert.public_bytes(serialization.Encoding.PEM))
        trust_file.flush()
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ESLOG_TRUSTED_CERTS_FILE", trust_file.name)
            ok, msg, chain = _verify_certificate_trust(
                [leaf_cert.public_bytes(serialization.Encoding.DER)],
                signing_time=datetime.now(UTC),
            )
    assert ok is True
    assert msg == "trusted certificate chain"
    assert [cert.subject for cert in chain] == ["CN=Signer", "CN=Test Root CA"]


def test_verify_certificate_trust_rejects_expired_leaf() -> None:
    root_cert, leaf_cert = _generate_chain(expired_leaf=True)
    with tempfile.NamedTemporaryFile("wb", suffix=".pem") as trust_file:
        trust_file.write(root_cert.public_bytes(serialization.Encoding.PEM))
        trust_file.flush()
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ESLOG_TRUSTED_CERTS_FILE", trust_file.name)
            ok, msg, chain = _verify_certificate_trust(
                [leaf_cert.public_bytes(serialization.Encoding.DER)],
                signing_time=datetime.now(UTC),
            )
    assert ok is False
    assert "certificate trust verification failed" in msg
    assert [cert.subject for cert in chain] == ["CN=Signer", "CN=Test Root CA"]


def test_verify_certificate_trust_accepts_mixed_ca_bundle_as_intermediate_file() -> None:
    root_cert, leaf_cert = _generate_chain()
    with tempfile.NamedTemporaryFile("wb", suffix=".pem") as bundle_file:
        bundle_file.write(root_cert.public_bytes(serialization.Encoding.PEM))
        bundle_file.flush()
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("ESLOG_TRUSTED_CERTS_FILE", raising=False)
            mp.setenv("ESLOG_INTERMEDIATE_CERTS_FILE", bundle_file.name)
            ok, msg, chain = _verify_certificate_trust(
                [leaf_cert.public_bytes(serialization.Encoding.DER)],
                signing_time=datetime.now(UTC),
            )
    assert ok is True
    assert msg == "trusted certificate chain"
    assert [cert.subject for cert in chain] == ["CN=Signer", "CN=Test Root CA"]


def test_verify_certificate_trust_uses_default_intermediate_bundle() -> None:
    root_cert, leaf_cert = _generate_chain()
    with tempfile.NamedTemporaryFile("wb", suffix=".pem") as trust_file, tempfile.NamedTemporaryFile(
        "wb", suffix=".pem"
    ) as default_bundle_file:
        trust_file.write(root_cert.public_bytes(serialization.Encoding.PEM))
        trust_file.flush()
        default_bundle_file.write(root_cert.public_bytes(serialization.Encoding.PEM))
        default_bundle_file.flush()
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ESLOG_TRUSTED_CERTS_FILE", trust_file.name)
            mp.delenv("ESLOG_INTERMEDIATE_CERTS_FILE", raising=False)
            mp.setattr("parsers.xmldsig.DEFAULT_INTERMEDIATE_CERTS_FILE", default_bundle_file.name)
            ok, msg, chain = _verify_certificate_trust(
                [leaf_cert.public_bytes(serialization.Encoding.DER)],
                signing_time=datetime.now(UTC),
            )
    assert ok is True
    assert msg == "trusted certificate chain"
    assert [cert.subject for cert in chain] == ["CN=Signer", "CN=Test Root CA"]


def test_skip_chain_validation_accepts_slog_alias() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("ESLOG_SKIP_CHAIN_VALIDATION", raising=False)
        mp.setenv("SLOG_SKIP_CHAIN_VALIDATION", "1")
        assert _skip_chain_validation_enabled() is True
