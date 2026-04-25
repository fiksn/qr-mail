from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from generators.eslog import Invoice, LineItem, Party, generate_eslog_invoice
from generators.xmldsig import load_signing_credentials, sign_eslog_invoice
from parsers.eslog import parse_eslog_invoice
from parsers.xmldsig import verify_eslog_signature


def _generate_ca_and_leaf() -> tuple[bytes, bytes, bytes]:
    """Generate a CA root + leaf signing cert.

    Returns (leaf_key_pem, leaf_cert_pem, root_cert_pem).
    """
    now = datetime.now(UTC)

    # Root CA
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root CA")])
    root_cert = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=30))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=False,
                key_cert_sign=True, crl_sign=True,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(private_key=root_key, algorithm=hashes.SHA256())
    )

    # Leaf (signing) cert
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Signer")])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(root_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                key_cert_sign=False, crl_sign=False,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectAlternativeName([
                x509.RFC822Name("signer@example.com"),
            ]),
            critical=False,
        )
        .sign(private_key=root_key, algorithm=hashes.SHA256())
    )

    leaf_key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    leaf_cert_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
    root_cert_pem = root_cert.public_bytes(serialization.Encoding.PEM)
    return leaf_key_pem, leaf_cert_pem, root_cert_pem


def _simple_invoice() -> Invoice:
    return Invoice(
        invoice_number="INV-2026-001",
        issue_date=date(2026, 4, 1),
        seller=Party(
            name="Prodajalec d.o.o.",
            street="Glavna cesta 1",
            city="Ljubljana",
            postal_code="1000",
            tax_id="SI12345678",
            iban="SI56043020003519144",
        ),
        buyer=Party(
            name="Kupec d.o.o.",
            street="Stranska ulica 5",
            city="Maribor",
        ),
        items=[
            LineItem(
                description="Storitev",
                quantity=Decimal("1"),
                unit="C62",
                unit_price_net=Decimal("100.00"),
                vat_rate=Decimal("22"),
            ),
        ],
        payment_due_date=date(2026, 5, 1),
        payment_reference="SI001234567890123",
    )


def test_sign_and_verify_roundtrip(tmp_path, monkeypatch) -> None:
    leaf_key_pem, leaf_cert_pem, root_cert_pem = _generate_ca_and_leaf()
    invoice_xml = generate_eslog_invoice(_simple_invoice())

    signed_xml = sign_eslog_invoice(invoice_xml, leaf_key_pem, leaf_cert_pem)

    trust_file = tmp_path / "trust.pem"
    trust_file.write_bytes(root_cert_pem)

    from parsers.xmldsig import _load_trust_anchors
    _load_trust_anchors.cache_clear()
    monkeypatch.setenv("ESLOG_TRUSTED_CERTS_FILE", str(trust_file))
    monkeypatch.delenv("ESLOG_SKIP_CHAIN_VALIDATION", raising=False)

    try:
        result = verify_eslog_signature(signed_xml)
    finally:
        _load_trust_anchors.cache_clear()

    assert result.signed is True
    assert result.valid is True, f"verification failed: {result.error}"
    assert result.signer is not None
    assert "Test Signer" in result.signer.subject
    assert result.signer.signing_time is not None


def test_signed_xml_still_parseable() -> None:
    leaf_key_pem, leaf_cert_pem, _ = _generate_ca_and_leaf()
    invoice_xml = generate_eslog_invoice(_simple_invoice())
    signed_xml = sign_eslog_invoice(invoice_xml, leaf_key_pem, leaf_cert_pem)

    upn = parse_eslog_invoice(signed_xml)
    assert upn.recipient_iban == "SI56043020003519144"
    assert upn.amount_cents == 12200


def test_tampered_signed_xml_fails_verification(
    tmp_path, monkeypatch,
) -> None:
    leaf_key_pem, leaf_cert_pem, root_cert_pem = _generate_ca_and_leaf()
    invoice_xml = generate_eslog_invoice(_simple_invoice())
    signed_xml = sign_eslog_invoice(invoice_xml, leaf_key_pem, leaf_cert_pem)

    tampered = signed_xml.replace(b"100.00", b"999.99")

    trust_file = tmp_path / "trust.pem"
    trust_file.write_bytes(root_cert_pem)

    from parsers.xmldsig import _load_trust_anchors
    _load_trust_anchors.cache_clear()
    monkeypatch.setenv("ESLOG_TRUSTED_CERTS_FILE", str(trust_file))

    try:
        result = verify_eslog_signature(tampered)
    finally:
        _load_trust_anchors.cache_clear()

    assert result.signed is True
    assert result.valid is False


def test_sign_multiple_line_items() -> None:
    leaf_key_pem, leaf_cert_pem, _ = _generate_ca_and_leaf()
    inv = Invoice(
        invoice_number="MULTI-001",
        issue_date=date(2026, 4, 1),
        seller=Party(
            name="Seller", street="St 1", city="LJ",
            iban="SI56043020003519144",
        ),
        buyer=Party(name="Buyer", street="St 2", city="MB"),
        items=[
            LineItem(
                description="A", quantity=Decimal("2"), unit="C62",
                unit_price_net=Decimal("50"), vat_rate=Decimal("22"),
            ),
            LineItem(
                description="B", quantity=Decimal("1"), unit="C62",
                unit_price_net=Decimal("30"), vat_rate=Decimal("9.5"),
            ),
        ],
        payment_due_date=date(2026, 5, 1),
        payment_reference="SI0012345",
    )
    xml = generate_eslog_invoice(inv)
    signed = sign_eslog_invoice(xml, leaf_key_pem, leaf_cert_pem)

    assert b"ds:Signature" in signed
    assert b"ds:SignatureValue" in signed

    upn = parse_eslog_invoice(signed)
    assert upn.amount_cents == 15485


def test_sign_with_encrypted_key() -> None:
    leaf_key_pem, leaf_cert_pem, _ = _generate_ca_and_leaf()

    # Re-encrypt the key with a password.
    from cryptography.hazmat.primitives.serialization import (
        BestAvailableEncryption,
        load_pem_private_key,
    )
    key = load_pem_private_key(leaf_key_pem, password=None)
    encrypted_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        BestAvailableEncryption(b"test-password"),
    )

    invoice_xml = generate_eslog_invoice(_simple_invoice())

    # Without password -> fails.
    with pytest.raises(Exception):
        sign_eslog_invoice(invoice_xml, encrypted_pem, leaf_cert_pem)

    # With password -> works.
    signed = sign_eslog_invoice(
        invoice_xml, encrypted_pem, leaf_cert_pem,
        password=b"test-password",
    )
    assert b"ds:Signature" in signed
    upn = parse_eslog_invoice(signed)
    assert upn.amount_cents == 12200


def test_load_signing_credentials(tmp_path, monkeypatch) -> None:
    leaf_key_pem, leaf_cert_pem, _ = _generate_ca_and_leaf()

    key_file = tmp_path / "key.pem"
    cert_file = tmp_path / "cert.pem"
    key_file.write_bytes(leaf_key_pem)
    cert_file.write_bytes(leaf_cert_pem)

    monkeypatch.setenv("ESLOG_SIGNING_KEY_FILE", str(key_file))
    monkeypatch.setenv("ESLOG_SIGNING_CERT_FILE", str(cert_file))

    key_pem, cert_pem = load_signing_credentials()
    assert key_pem == leaf_key_pem
    assert cert_pem == leaf_cert_pem


def test_load_signing_credentials_missing_env(monkeypatch) -> None:
    monkeypatch.delenv("ESLOG_SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("ESLOG_SIGNING_CERT_FILE", raising=False)

    with pytest.raises(RuntimeError, match="ESLOG_SIGNING_KEY_FILE not set"):
        load_signing_credentials()
