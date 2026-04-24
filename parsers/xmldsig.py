"""Extract and verify XMLDSig signatures from eSLOG 2.0 invoices.

Extracts the X.509 signer certificate and verifies the RSA signature
over the canonicalized ds:SignedInfo, plus the digest references within
it. Uses the ``cryptography`` library for RSA verification and X.509
certificate parsing.

Inclusive C14N 1.0 (http://www.w3.org/TR/2001/REC-xml-c14n-20010315) is
used for subtree canonicalization. Python's stdlib ``ET.canonicalize()``
strips unused namespace declarations (exclusive behaviour), so we inject
ancestor namespace declarations manually and hash the raw result.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import defusedxml.ElementTree as ET

log = logging.getLogger(__name__)

NS_ESLOG = "urn:eslog:2.00"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
NS_XDS = "http://uri.etsi.org/01903/v1.3.2#"

_NS = {"ds": NS_DS, "xds": NS_XDS, "e": NS_ESLOG}

DIGEST_ALGORITHMS = {
    "http://www.w3.org/2000/09/xmldsig#sha1": "sha1",
    "http://www.w3.org/2001/04/xmlenc#sha256": "sha256",
    "http://www.w3.org/2001/04/xmldsig-more#sha256": "sha256",
}

SIGNATURE_ALGORITHMS: dict[str, tuple[str, str]] = {
    # URI -> (hash_name, padding_type)
    "http://www.w3.org/2000/09/xmldsig#rsa-sha1": ("sha1", "pkcs1v15"),
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256": ("sha256", "pkcs1v15"),
}


@dataclass
class SignerInfo:
    """X.509 signer certificate details."""

    subject: str
    issuer: str
    not_before: str
    not_after: str
    signing_time: Optional[str] = None


@dataclass
class SignatureResult:
    """Outcome of XMLDSig extraction and verification."""

    signed: bool
    valid: Optional[bool] = None  # None = could not verify
    signer: Optional[SignerInfo] = None
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


def _collect_root_ns_decls(xml_text: str) -> str:
    """Collect namespace declarations from the root element.

    Returns a string like 'xmlns="..." xmlns:ds="..."' suitable for
    injection into a subtree root element for inclusive C14N.
    Skips processing instructions (``<?...?>``) to find the actual root.
    """
    root_match = re.search(r"<[A-Za-z_][^>]*>", xml_text, re.DOTALL)
    if not root_match:
        return ""
    root_tag = root_match.group(0)
    default_ns: list[str] = []
    prefixed_ns: list[str] = []
    for m in re.finditer(r'xmlns(?::\w+)?="[^"]*"', root_tag):
        decl = m.group(0)
        if decl.startswith("xmlns="):
            default_ns.append(decl)
        else:
            prefixed_ns.append(decl)
    prefixed_ns.sort()
    return " ".join(default_ns + prefixed_ns)


def _extract_subtree(
    xml_text: str,
    open_tag: str,
    close_tag: str,
) -> Optional[str]:
    """Extract a subtree by tag name from raw XML text."""
    try:
        start = xml_text.index(f"<{open_tag}")
        end = xml_text.index(close_tag) + len(close_tag)
    except ValueError:
        return None
    return xml_text[start:end]


def _inject_ns(subtree: str, tag: str, ns_decls: str) -> str:
    """Inject namespace declarations right after the opening tag name."""
    pattern = f"<{re.escape(tag)}"
    match = re.search(pattern, subtree)
    if not match:
        return subtree
    insert_pos = match.end()
    return subtree[:insert_pos] + " " + ns_decls + subtree[insert_pos:]


def _c14n_subtree(
    xml_text: str,
    open_tag: str,
    close_tag: str,
    ns_decls: str,
) -> Optional[bytes]:
    """Canonicalize a subtree using inclusive C14N 1.0.

    Extracts the subtree from raw XML, injects ancestor namespace
    declarations, and returns the UTF-8 canonical bytes.
    """
    subtree = _extract_subtree(xml_text, open_tag, close_tag)
    if subtree is None:
        return None
    with_ns = _inject_ns(subtree, open_tag, ns_decls)
    return with_ns.encode("utf-8")


def _format_x509_name(name) -> str:
    """Format an x509.Name as a readable string."""
    from cryptography.x509.oid import NameOID

    oid_labels = {
        NameOID.COMMON_NAME: "CN",
        NameOID.ORGANIZATION_NAME: "O",
        NameOID.COUNTRY_NAME: "C",
        NameOID.STATE_OR_PROVINCE_NAME: "ST",
        NameOID.SERIAL_NUMBER: "serialNumber",
    }
    parts: list[str] = []
    for attr in name:
        label = oid_labels.get(attr.oid, attr.oid.dotted_string)
        parts.append(f"{label}={attr.value}")
    return ", ".join(parts)


def _parse_cert(cert_der: bytes) -> Optional[SignerInfo]:
    """Parse an X.509 certificate from DER bytes."""
    try:
        from cryptography import x509
    except ImportError:
        log.warning("cryptography not installed; cert parsing disabled")
        return None

    try:
        cert = x509.load_der_x509_certificate(cert_der)
    except Exception as exc:
        log.warning("failed to parse X.509 certificate: %s", exc)
        return None

    return SignerInfo(
        subject=_format_x509_name(cert.subject),
        issuer=_format_x509_name(cert.issuer),
        not_before=cert.not_valid_before_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        not_after=cert.not_valid_after_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


def _verify_rsa(
    cert_der: bytes,
    sig_bytes: bytes,
    data: bytes,
    hash_name: str,
) -> tuple[bool, str]:
    """Verify an RSA PKCS#1 v1.5 signature using ``cryptography``."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        return False, "cryptography library not installed"

    hash_map: dict[str, type[hashes.HashAlgorithm]] = {
        "sha1": hashes.SHA1,
        "sha256": hashes.SHA256,
    }
    hash_cls = hash_map.get(hash_name)
    if hash_cls is None:
        return False, f"unsupported hash: {hash_name}"

    try:
        cert = x509.load_der_x509_certificate(cert_der)
        pubkey = cert.public_key()
        if not isinstance(pubkey, rsa.RSAPublicKey):
            return False, f"unsupported key type: {type(pubkey).__name__}"
        pubkey.verify(sig_bytes, data, padding.PKCS1v15(), hash_cls())
        return True, "Verified OK"
    except Exception as exc:
        return False, str(exc)


def _verify_reference_digest(
    xml_text: str,
    uri: str,
    digest_alg_uri: str,
    expected_b64: str,
    ns_decls: str,
) -> tuple[bool, str]:
    """Verify one ds:Reference digest."""
    alg = DIGEST_ALGORITHMS.get(digest_alg_uri)
    if alg is None:
        return False, f"unsupported digest algorithm: {digest_alg_uri}"

    expected = base64.b64decode(expected_b64)

    if not uri.startswith("#"):
        return False, f"unsupported reference URI: {uri}"
    ref_id = uri[1:]

    # Find element with matching Id attribute.
    root = ET.fromstring(xml_text)
    target = None
    for elem in root.iter():
        if elem.get("Id") == ref_id:
            target = elem
            break

    if target is None:
        return False, f"referenced element Id={ref_id!r} not found"

    # Resolve the raw prefixed tag name from the Clark-notation tag.
    tag_local = target.tag
    for ns_uri, prefix in [
        (NS_ESLOG, ""),
        (NS_DS, "ds:"),
        (NS_XDS, "xds:"),
    ]:
        if tag_local.startswith(f"{{{ns_uri}}}"):
            local = tag_local.split("}")[-1]
            raw_tag = f"{prefix}{local}"
            break
    else:
        raw_tag = tag_local.split("}")[-1] if "}" in tag_local else tag_local

    close_tag = f"</{raw_tag}>"
    canonical = _c14n_subtree(xml_text, raw_tag, close_tag, ns_decls)
    if canonical is None:
        return False, f"could not extract element Id={ref_id!r}"

    h = hashlib.new(alg)
    h.update(canonical)
    actual = h.digest()

    if actual != expected:
        return False, (
            f"digest mismatch for Id={ref_id!r}: "
            f"expected {expected_b64}, "
            f"got {base64.b64encode(actual).decode()}"
        )

    return True, f"digest OK for Id={ref_id!r}"


def verify_eslog_signature(xml_bytes: bytes) -> SignatureResult:
    """Extract and verify the XMLDSig signature in an eSLOG document.

    Returns a ``SignatureResult`` with signer info and verification
    outcome.
    """
    try:
        xml_text = xml_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            xml_text = xml_bytes.decode("iso-8859-1")
        except UnicodeDecodeError:
            return SignatureResult(
                signed=False,
                error="could not decode XML as UTF-8 or ISO-8859-1",
            )

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return SignatureResult(signed=False, error=f"invalid XML: {exc}")

    sig_elem = root.find("ds:Signature", _NS)
    if sig_elem is None:
        return SignatureResult(signed=False)

    # Extract certificate.
    cert_elem = sig_elem.find(".//ds:X509Certificate", _NS)
    if cert_elem is None or not cert_elem.text:
        return SignatureResult(
            signed=True,
            valid=None,
            error="signature present but no X509Certificate found",
        )

    cert_b64 = cert_elem.text.strip()
    try:
        cert_der = base64.b64decode(cert_b64)
    except Exception as exc:
        return SignatureResult(
            signed=True,
            valid=None,
            error=f"invalid X509Certificate base64: {exc}",
        )

    signer = _parse_cert(cert_der)

    # Extract signing time from XAdES.
    signing_time_elem = sig_elem.find(
        ".//xds:SignedSignatureProperties/xds:SigningTime", _NS,
    )
    if signer and signing_time_elem is not None and signing_time_elem.text:
        signer.signing_time = signing_time_elem.text.strip()

    # Extract signature value.
    sig_val_elem = sig_elem.find("ds:SignatureValue", _NS)
    if sig_val_elem is None or not sig_val_elem.text:
        return SignatureResult(
            signed=True,
            valid=None,
            signer=signer,
            error="ds:SignatureValue missing or empty",
        )
    sig_bytes = base64.b64decode(sig_val_elem.text.strip())

    # Determine signature algorithm.
    signed_info = sig_elem.find("ds:SignedInfo", _NS)
    if signed_info is None:
        return SignatureResult(
            signed=True, valid=None, signer=signer,
            error="ds:SignedInfo missing",
        )

    sig_method = signed_info.find("ds:SignatureMethod", _NS)
    if sig_method is None:
        return SignatureResult(
            signed=True, valid=None, signer=signer,
            error="ds:SignatureMethod missing",
        )

    sig_alg_uri = sig_method.get("Algorithm", "")
    sig_alg = SIGNATURE_ALGORITHMS.get(sig_alg_uri)
    if sig_alg is None:
        return SignatureResult(
            signed=True, valid=None, signer=signer,
            error=f"unsupported signature algorithm: {sig_alg_uri}",
        )
    hash_name, _ = sig_alg

    # Collect ancestor namespace declarations for inclusive C14N.
    ns_decls = _collect_root_ns_decls(xml_text)

    # 1. Verify RSA signature over canonicalized SignedInfo.
    si_canonical = _c14n_subtree(
        xml_text, "ds:SignedInfo", "</ds:SignedInfo>", ns_decls,
    )
    if si_canonical is None:
        return SignatureResult(
            signed=True, valid=None, signer=signer,
            error="could not extract ds:SignedInfo from raw XML",
        )

    rsa_ok, rsa_msg = _verify_rsa(cert_der, sig_bytes, si_canonical, hash_name)
    if not rsa_ok:
        return SignatureResult(
            signed=True, valid=False, signer=signer,
            error=f"signature verification failed: {rsa_msg}",
        )

    # 2. Verify each ds:Reference digest.
    warnings: list[str] = []
    for ref in signed_info.findall("ds:Reference", _NS):
        uri = ref.get("URI", "")
        digest_method = ref.find("ds:DigestMethod", _NS)
        digest_value = ref.find("ds:DigestValue", _NS)
        if digest_method is None or digest_value is None or not digest_value.text:
            warnings.append(f"incomplete reference (URI={uri!r})")
            continue

        ok, msg = _verify_reference_digest(
            xml_text,
            uri,
            digest_method.get("Algorithm", ""),
            digest_value.text.strip(),
            ns_decls,
        )
        if not ok:
            return SignatureResult(
                signed=True, valid=False, signer=signer,
                error=msg,
            )
        log.debug("XMLDSig: %s", msg)

    return SignatureResult(
        signed=True, valid=True, signer=signer, warnings=warnings,
    )
