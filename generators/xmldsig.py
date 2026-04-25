"""Sign eSLOG 2.0 invoice XML with XMLDSig + XAdES.

Produces signatures compatible with the verifier in ``parsers.xmldsig``.
Uses inclusive C14N 1.0 with ancestor namespace injection (the same
text-based approach the verifier uses).

Signature algorithm: RSA-SHA256 with PKCS#1 v1.5 padding.

Configuration via environment variables:
    ESLOG_SIGNING_KEY_FILE   — path to PEM-encoded RSA private key
    ESLOG_SIGNING_CERT_FILE  — path to PEM-encoded X.509 certificate
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from datetime import UTC, datetime

NS_ESLOG = "urn:eslog:2.00"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
NS_XDS = "http://uri.etsi.org/01903/v1.3.2#"

DIGEST_ALG_URI = "http://www.w3.org/2001/04/xmlenc#sha256"
SIG_ALG_URI = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
C14N_ALG_URI = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"

# Namespace declarations that go on the root <Invoice> element.
# Sorted alphabetically by prefix (default first, then ds, xds).
ROOT_NS_DECLS = (
    f'xmlns="{NS_ESLOG}" '
    f'xmlns:ds="{NS_DS}" '
    f'xmlns:xds="{NS_XDS}"'
)


def _extract_subtree(xml_text: str, open_tag: str, close_tag: str) -> str:
    start = xml_text.index(f"<{open_tag}")
    end = xml_text.index(close_tag) + len(close_tag)
    return xml_text[start:end]


def _inject_ns(subtree: str, tag: str, ns_decls: str) -> str:
    pattern = f"<{re.escape(tag)}"
    match = re.search(pattern, subtree)
    if not match:
        return subtree
    insert_pos = match.end()
    return subtree[:insert_pos] + " " + ns_decls + subtree[insert_pos:]


def _digest_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def _canonicalize_subtree(
    xml_text: str, open_tag: str, close_tag: str,
) -> bytes:
    """Extract subtree and inject root namespace declarations.

    Mirrors ``parsers.xmldsig._c14n_subtree`` exactly.
    """
    subtree = _extract_subtree(xml_text, open_tag, close_tag)
    with_ns = _inject_ns(subtree, open_tag, ROOT_NS_DECLS)
    return with_ns.encode("utf-8")


def _format_x509_issuer(cert) -> str:
    """Format issuer name for ds:X509IssuerName.

    Uses the RFC 2253 / RFC 4514 Distinguished Name format that
    xmldsig verifiers expect.
    """
    from cryptography.x509.oid import NameOID

    oid_labels = {
        NameOID.COMMON_NAME: "CN",
        NameOID.ORGANIZATION_NAME: "O",
        NameOID.COUNTRY_NAME: "C",
        NameOID.ORGANIZATIONAL_UNIT_NAME: "OU",
        NameOID.STATE_OR_PROVINCE_NAME: "ST",
        NameOID.SERIAL_NUMBER: "serialNumber",
    }
    parts: list[str] = []
    for attr in cert.issuer:
        label = oid_labels.get(attr.oid, attr.oid.dotted_string)
        value = attr.value
        # Hex-encode non-standard OID values (like organizationIdentifier)
        if label == attr.oid.dotted_string:
            hex_val = value.encode("utf-8").hex()
            parts.append(f"{label}=#{hex_val}")
        else:
            parts.append(f"{label}={value}")
    return ",".join(parts)


def _build_signed_properties(
    cert, signing_time: datetime,
) -> str:
    """Build the xds:SignedProperties XML fragment."""
    cert_der = cert.public_bytes(
        encoding=__import__("cryptography.hazmat.primitives.serialization",
                            fromlist=["Encoding"]).Encoding.DER,
    )
    cert_digest = _digest_b64(cert_der)
    issuer_name = _format_x509_issuer(cert)
    serial = cert.serial_number
    ts = signing_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    return (
        f'<xds:SignedProperties Id="SignedPropertiesId">'
        f"<xds:SignedSignatureProperties>"
        f"<xds:SigningTime>{ts}</xds:SigningTime>"
        f"<xds:SigningCertificate>"
        f"<xds:Cert>"
        f"<xds:CertDigest>"
        f'<ds:DigestMethod Algorithm="{DIGEST_ALG_URI}"></ds:DigestMethod>'
        f"<ds:DigestValue>{cert_digest}</ds:DigestValue>"
        f"</xds:CertDigest>"
        f"<xds:IssuerSerial>"
        f"<ds:X509IssuerName>{issuer_name}</ds:X509IssuerName>"
        f"<ds:X509SerialNumber>{serial}</ds:X509SerialNumber>"
        f"</xds:IssuerSerial>"
        f"</xds:Cert>"
        f"</xds:SigningCertificate>"
        f"<xds:SignaturePolicyIdentifier>"
        f"<xds:SignaturePolicyImplied></xds:SignaturePolicyImplied>"
        f"</xds:SignaturePolicyIdentifier>"
        f"</xds:SignedSignatureProperties>"
        f"</xds:SignedProperties>"
    )


def _build_signed_info(
    data_digest: str, props_digest: str,
) -> str:
    return (
        f"<ds:SignedInfo>"
        f'<ds:CanonicalizationMethod Algorithm="{C14N_ALG_URI}">'
        f"</ds:CanonicalizationMethod>"
        f'<ds:SignatureMethod Algorithm="{SIG_ALG_URI}">'
        f"</ds:SignatureMethod>"
        f'<ds:Reference URI="#data">'
        f'<ds:DigestMethod Algorithm="{DIGEST_ALG_URI}"></ds:DigestMethod>'
        f"<ds:DigestValue>{data_digest}</ds:DigestValue>"
        f"</ds:Reference>"
        f'<ds:Reference Type="{NS_XDS}SignedProperties"'
        f' URI="#SignedPropertiesId">'
        f'<ds:DigestMethod Algorithm="{DIGEST_ALG_URI}"></ds:DigestMethod>'
        f"<ds:DigestValue>{props_digest}</ds:DigestValue>"
        f"</ds:Reference>"
        f"</ds:SignedInfo>"
    )


def _add_root_ns(invoice_xml: str) -> str:
    """Replace xmlns on root <Invoice> with full namespace declarations."""
    return re.sub(
        r"<Invoice\s+xmlns=\"[^\"]*\"",
        f"<Invoice {ROOT_NS_DECLS}",
        invoice_xml,
        count=1,
    )


def _load_key_and_cert(
    private_key_pem: bytes,
    certificate_pem: bytes,
    password: bytes | None,
) -> tuple:
    """Load and validate RSA private key and X.509 certificate."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = serialization.load_pem_private_key(
        private_key_pem, password=password,
    )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError(
            f"expected RSA private key, got {type(private_key).__name__}",
        )
    cert = x509.load_pem_x509_certificate(certificate_pem)
    return private_key, cert


def load_signing_credentials() -> tuple[bytes, bytes]:
    """Load signing key and certificate from environment variables.

    Reads ``ESLOG_SIGNING_KEY_FILE`` and ``ESLOG_SIGNING_CERT_FILE``.

    Returns:
        Tuple of (private_key_pem, certificate_pem) bytes.
    """
    key_path = os.environ.get("ESLOG_SIGNING_KEY_FILE", "").strip()
    cert_path = os.environ.get("ESLOG_SIGNING_CERT_FILE", "").strip()
    if not key_path:
        raise RuntimeError("ESLOG_SIGNING_KEY_FILE not set")
    if not cert_path:
        raise RuntimeError("ESLOG_SIGNING_CERT_FILE not set")

    with open(key_path, "rb") as f:
        key_pem = f.read()
    with open(cert_path, "rb") as f:
        cert_pem = f.read()
    return key_pem, cert_pem


def sign_eslog_invoice(
    invoice_xml: bytes,
    private_key_pem: bytes,
    certificate_pem: bytes,
    signing_time: datetime | None = None,
    password: bytes | None = None,
) -> bytes:
    """Sign an eSLOG 2.0 invoice XML with XMLDSig + XAdES.

    Args:
        invoice_xml: Unsigned XML from ``generate_eslog_invoice``.
        private_key_pem: PEM-encoded RSA private key.
        certificate_pem: PEM-encoded X.509 certificate.
        signing_time: Signing timestamp (defaults to now UTC).
        password: Private key password, or None if unencrypted.

    Returns:
        UTF-8 encoded signed XML bytes.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    if signing_time is None:
        signing_time = datetime.now(UTC)

    private_key, cert = _load_key_and_cert(
        private_key_pem, certificate_pem, password,
    )

    # Decode XML, strip XML declaration, add namespace prefixes to root.
    xml_text = invoice_xml.decode("utf-8")
    xml_decl = ""
    if xml_text.startswith("<?xml"):
        newline = xml_text.index("?>") + 2
        xml_decl = xml_text[:newline]
        xml_text = xml_text[newline:].lstrip("\n")

    xml_text = _add_root_ns(xml_text)

    # 1. Digest the M_INVOIC element (Id="data").
    data_canonical = _canonicalize_subtree(
        xml_text, "M_INVOIC", "</M_INVOIC>",
    )
    data_digest = _digest_b64(data_canonical)

    # 2. Build and digest the XAdES SignedProperties.
    signed_props = _build_signed_properties(cert, signing_time)
    props_canonical = _inject_ns(
        signed_props, "xds:SignedProperties", ROOT_NS_DECLS,
    ).encode("utf-8")
    props_digest = _digest_b64(props_canonical)

    # 3. Build and sign the SignedInfo.
    signed_info = _build_signed_info(data_digest, props_digest)
    si_canonical = _inject_ns(
        signed_info, "ds:SignedInfo", ROOT_NS_DECLS,
    ).encode("utf-8")

    sig_bytes = private_key.sign(si_canonical, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(sig_bytes).decode()

    # 4. Encode certificate.
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    cert_b64 = base64.b64encode(cert_der).decode()

    # 5. Assemble the ds:Signature block.
    signature_block = (
        f'<ds:Signature Id="SignatureId">'
        f"{signed_info}"
        f"<ds:SignatureValue>{sig_b64}</ds:SignatureValue>"
        f"<ds:KeyInfo>"
        f"<ds:X509Data>"
        f"<ds:X509Certificate>{cert_b64}</ds:X509Certificate>"
        f"</ds:X509Data>"
        f"</ds:KeyInfo>"
        f"<ds:Object>"
        f'<xds:QualifyingProperties Target="#SignatureId">'
        f"{signed_props}"
        f"</xds:QualifyingProperties>"
        f"</ds:Object>"
        f"</ds:Signature>"
    )

    # Insert signature before closing </Invoice>.
    signed_xml = xml_text.replace(
        "</Invoice>", f"{signature_block}</Invoice>",
    )

    if xml_decl:
        signed_xml = xml_decl + "\n" + signed_xml

    return signed_xml.encode("utf-8")
