from datetime import date
import os

import pytest

from parsers.icl_envelope import ICLEnvelopeParseError, parse_icl_envelope


SAMPLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "ovojnica_57850109.xml",
)

needs_sample = pytest.mark.skipif(
    not os.path.exists(SAMPLE_PATH),
    reason=f"sample envelope not found: {SAMPLE_PATH}",
)


@needs_sample
def test_parse_icl_envelope_sample() -> None:
    with open(SAMPLE_PATH, "rb") as f:
        upn = parse_icl_envelope(f.read())

    assert upn.recipient_iban == "SI56011006030694121"
    assert upn.recipient_reference == "SI120055328027949"
    assert upn.recipient_name == "GIMNAZIJA VIČ, LJUBLJANA"
    assert upn.recipient_street == "TRŽAŠKA CESTA 72"
    assert upn.recipient_city == "1000 Ljubljana-dostava"
    assert upn.payer_iban == "SI56011111111111117"
    assert upn.payer_name == "Narobe Pogačnik Martina"
    assert upn.amount_cents == 14270
    assert upn.payment_deadline == date(2026, 5, 20)
    assert upn.purpose_code == "COST"
    assert upn.payment_purpose == "Račun 2794"


def test_rejects_non_envelope_xml() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:eslog:2.00"><M_INVOIC/></Invoice>"""

    with pytest.raises(ICLEnvelopeParseError, match="root element"):
        parse_icl_envelope(xml)
