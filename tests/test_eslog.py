from datetime import date

import pytest

from parsers.eslog import ESlogParseError, parse_eslog_invoice


def test_parse_eslog_invoice_with_payee_and_payment_fields() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:eslog:2.00">
  <M_INVOIC>
    <S_BGM>
      <C_C106>
        <D_1004>RAC-2026-001</D_1004>
      </C_C106>
    </S_BGM>
    <S_FTX>
      <D_4451>ALQ</D_4451>
      <C_C108>
        <D_4440>OTHR</D_4440>
      </C_C108>
    </S_FTX>
    <G_SG1>
      <S_RFF>
        <C_C506>
          <D_1153>PQ</D_1153>
          <D_1154>SI0012345-2026</D_1154>
        </C_C506>
      </S_RFF>
    </G_SG1>
    <G_SG2>
      <S_NAD>
        <D_3035>SE</D_3035>
        <C_C080>
          <D_3036>Prodajalec d.o.o.</D_3036>
        </C_C080>
        <C_C059>
          <D_3042>Glavna cesta 1</D_3042>
        </C_C059>
        <D_3164>Ljubljana</D_3164>
      </S_NAD>
      <S_FII>
        <D_3035>RB</D_3035>
        <C_C078>
          <D_3194>SI27020100012345678</D_3194>
          <D_3192>Prodajalec d.o.o.</D_3192>
        </C_C078>
      </S_FII>
    </G_SG2>
    <G_SG2>
      <S_NAD>
        <D_3035>PE</D_3035>
        <C_C080>
          <D_3036>Prejemnik placila d.o.o.</D_3036>
        </C_C080>
        <C_C059>
          <D_3042>Placilna ulica 2</D_3042>
        </C_C059>
        <D_3164>Maribor</D_3164>
      </S_NAD>
    </G_SG2>
    <G_SG8>
      <S_PAT>
        <D_4279>1</D_4279>
      </S_PAT>
      <S_DTM>
        <C_C507>
          <D_2005>13</D_2005>
          <D_2380>2026-05-15</D_2380>
        </C_C507>
      </S_DTM>
    </G_SG8>
    <G_SG50>
      <S_MOA>
        <C_C516>
          <D_5025>388</D_5025>
          <D_5004>123.45</D_5004>
        </C_C516>
      </S_MOA>
    </G_SG50>
  </M_INVOIC>
</Invoice>
"""

    upn = parse_eslog_invoice(xml)

    assert upn.recipient_iban == "SI27020100012345678"
    assert upn.recipient_reference == "SI0012345-2026"
    assert upn.recipient_name == "Prejemnik placila d.o.o."
    assert upn.recipient_street == "Placilna ulica 2"
    assert upn.recipient_city == "Maribor"
    assert upn.amount_cents == 12345
    assert upn.payment_deadline == date(2026, 5, 15)
    assert upn.purpose_code == "OTHR"
    assert upn.payment_purpose == "RAC-2026-001"


def test_parse_eslog_invoice_falls_back_to_seller_party() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:eslog:2.00">
  <M_INVOIC>
    <G_SG2>
      <S_NAD>
        <D_3035>SE</D_3035>
        <C_C080>
          <D_3036>Prodajalec d.o.o.</D_3036>
        </C_C080>
        <C_C059>
          <D_3042>Glavna cesta 1</D_3042>
        </C_C059>
        <D_3164>Celje</D_3164>
      </S_NAD>
      <S_FII>
        <D_3035>RB</D_3035>
        <C_C078>
          <D_3194>SI52031001000051063</D_3194>
        </C_C078>
      </S_FII>
    </G_SG2>
    <G_SG50>
      <S_MOA>
        <C_C516>
          <D_5025>388</D_5025>
          <D_5004>10.00</D_5004>
        </C_C516>
      </S_MOA>
    </G_SG50>
  </M_INVOIC>
</Invoice>
"""

    upn = parse_eslog_invoice(xml)

    assert upn.recipient_name == "Prodajalec d.o.o."
    assert upn.recipient_city == "Celje"
    assert upn.amount_cents == 1000
    assert upn.purpose_code == "OTHR"


def test_rejects_non_eslog_xml() -> None:
    with pytest.raises(ESlogParseError):
        parse_eslog_invoice("<root />")
