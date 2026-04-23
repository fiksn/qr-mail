"""Extract IBAN + SI/RF reference pairs from plain text, PDFs, and images.

Scans text for Slovenian IBANs (validated via MOD97) and nearby SI/RF
references (validated via MOD11 / MOD97). Pairs found within a proximity
window are returned for payment processing.

Text sources:
  - Plain text (email body)
  - PDF embedded text (via poppler's pdftotext)
  - Image OCR (via pytesseract, optional)
"""
import io
import logging
import os
import re
import subprocess
import tempfile
from typing import Optional

from PIL import Image

from epc import EPCParseError, _validate_iban
from upn import UPN, UPNReferenceError, validate_upn_reference

log = logging.getLogger(__name__)

# Lines above/below an IBAN to search for a matching reference.
PROXIMITY_WINDOW = 6

# Regex for Slovenian IBANs: SI + 2 check digits + 15 digits, with
# optional spaces/separators between groups of 4.
_IBAN_RE = re.compile(
    r"\bSI\s?\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{3}\b"
)

# Regex for SI references: SI + 2-digit model + digit groups with
# optional hyphens/spaces. Loose match; validation filters false positives.
_SI_REF_RE = re.compile(
    r"\bSI\s?\d{2}[\d\- ]{1,25}\b"
)

# Regex for RF (ISO 11649) references.
_RF_REF_RE = re.compile(
    r"\bRF\s?\d{2}\s?[0-9A-Za-z\s]{1,21}\b"
)

_tesseract_warned = False


def _normalize_iban(raw: str) -> Optional[str]:
    """Normalize and validate an IBAN. Returns None if invalid."""
    candidate = raw.replace(" ", "").upper()
    try:
        return _validate_iban(candidate)
    except EPCParseError:
        return None


def _normalize_reference(raw: str) -> Optional[str]:
    """Normalize and validate an SI/RF reference. Returns None if invalid."""
    candidate = raw.replace(" ", "").upper()
    try:
        return validate_upn_reference(candidate)
    except UPNReferenceError:
        return None


def _find_refs_in_lines(lines: list[str]) -> list[str]:
    """Find all valid SI/RF references in a block of text lines."""
    text = "\n".join(lines)
    refs: list[str] = []
    for m in _SI_REF_RE.finditer(text):
        ref = _normalize_reference(m.group())
        if ref:
            refs.append(ref)
    for m in _RF_REF_RE.finditer(text):
        ref = _normalize_reference(m.group())
        if ref:
            refs.append(ref)
    return refs


def _is_iban_shaped(ref: str) -> bool:
    """True if a reference string looks like an IBAN rather than a payment ref."""
    stripped = ref.replace("-", "").replace(" ", "")
    return len(stripped) == 19 and stripped[:2] == "SI" and stripped[2:].isdigit()


def find_iban_reference_pairs(text: str) -> list[tuple[str, str]]:
    """Find (IBAN, reference) pairs where both are valid and nearby.

    Per IBAN, looks for references within a 5-line window. If exactly one
    valid reference is found, the pair is emitted. If zero or multiple
    references are found the IBAN is skipped (ambiguous). Multiple IBANs
    sharing the same single reference each produce a pair.

    Returns deduplicated list of (normalized_iban, normalized_reference).
    """
    lines = text.splitlines()
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for line_idx, line in enumerate(lines):
        for m in _IBAN_RE.finditer(line):
            iban = _normalize_iban(m.group())
            if not iban:
                continue

            window_start = max(0, line_idx - PROXIMITY_WINDOW)
            window_end = min(len(lines), line_idx + PROXIMITY_WINDOW + 1)
            window = lines[window_start:window_end]

            refs = [r for r in _find_refs_in_lines(window) if not _is_iban_shaped(r)]
            # Deduplicate refs within window (same ref matched twice).
            unique_refs = list(dict.fromkeys(refs))

            if len(unique_refs) != 1:
                continue

            key = (iban, unique_refs[0])
            if key not in seen:
                seen.add(key)
                pairs.append(key)

    return pairs


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract embedded text from a PDF using poppler's pdftotext."""
    timeout_s = int(os.environ.get("PDF_RENDER_TIMEOUT_S", "20"))
    try:
        with tempfile.NamedTemporaryFile(
            prefix="qr-mail-", suffix=".pdf"
        ) as f:
            f.write(pdf_bytes)
            f.flush()
            proc = subprocess.run(
                ["pdftotext", "-layout", f.name, "-"],
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        if proc.returncode != 0:
            log.warning(
                "pdftotext failed (exit %d): %s",
                proc.returncode,
                proc.stderr.decode(errors="replace")[:200],
            )
            return ""
        return proc.stdout.decode("utf-8", errors="replace")
    except FileNotFoundError:
        log.debug("pdftotext not available; skipping PDF text extraction")
        return ""
    except subprocess.TimeoutExpired:
        log.warning("pdftotext timed out after %ds", timeout_s)
        return ""


def extract_image_text(img: Image.Image) -> str:
    """Extract text from an image via OCR (pytesseract).

    Returns empty string if pytesseract or tesseract is not installed.
    """
    global _tesseract_warned  # noqa: PLW0603
    try:
        import pytesseract
    except ImportError:
        if not _tesseract_warned:
            log.info("pytesseract not installed; image OCR disabled")
            _tesseract_warned = True
        return ""

    try:
        return pytesseract.image_to_string(img, lang="slv+eng")
    except pytesseract.TesseractNotFoundError:
        if not _tesseract_warned:
            log.warning("tesseract binary not found; image OCR disabled")
            _tesseract_warned = True
        return ""
    except pytesseract.TesseractError:
        # Language data missing — retry with English only.
        try:
            return pytesseract.image_to_string(img, lang="eng")
        except Exception as exc:
            log.warning("pytesseract OCR failed: %s", exc)
            return ""


def extract_image_text_from_bytes(data: bytes) -> str:
    """Load image bytes and run OCR. Returns empty string on failure."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        log.warning("image load for OCR failed: %s", exc)
        return ""
    return extract_image_text(img)


def build_upn_from_text(
    iban: str,
    reference: str,
    *,
    recipient_city: str = "Ljubljana",
) -> UPN:
    """Build a minimal UPN from an extracted IBAN + reference pair."""
    return UPN(
        payer_iban="",
        deposit=False,
        withdrawal=False,
        payer_reference="",
        payer_name="",
        payer_street="",
        payer_city="",
        amount_cents=0,
        payment_date=None,
        urgent=False,
        purpose_code="OTHR",
        payment_purpose="",
        payment_deadline=None,
        recipient_iban=iban,
        recipient_reference=reference,
        recipient_name="",
        recipient_street="",
        recipient_city=recipient_city,
    )
