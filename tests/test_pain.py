from datetime import date

import pytest

from parsers.pain import PainParseError, parse_pain_credit_transfers


PAIN_TWO_TX = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03">
  <CstmrCdtTrfInitn>
    <GrpHdr>
      <MsgId>BATCH-2026-001</MsgId>
      <CreDtTm>2026-05-01T09:00:00</CreDtTm>
      <NbOfTxs>2</NbOfTxs>
      <InitgPty><Nm>Initiator d.o.o.</Nm></InitgPty>
    </GrpHdr>
    <PmtInf>
      <PmtInfId>PMT-1</PmtInfId>
      <PmtMtd>TRF</PmtMtd>
      <ReqdExctnDt>2026-05-15</ReqdExctnDt>
      <Dbtr>
        <Nm>Placnik d.o.o.</Nm>
        <PstlAdr>
          <StrtNm>Dunajska cesta</StrtNm>
          <BldgNb>5</BldgNb>
          <PstCd>1000</PstCd>
          <TwnNm>Ljubljana</TwnNm>
        </PstlAdr>
      </Dbtr>
      <DbtrAcct><Id><IBAN>SI56011111111111117</IBAN></Id></DbtrAcct>
      <CdtTrfTxInf>
        <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>
        <Amt><InstdAmt Ccy="EUR">123.45</InstdAmt></Amt>
        <Cdtr>
          <Nm>Prejemnik Ena d.o.o.</Nm>
          <PstlAdr>
            <StrtNm>Glavna cesta</StrtNm>
            <BldgNb>1</BldgNb>
            <PstCd>2000</PstCd>
            <TwnNm>Maribor</TwnNm>
          </PstlAdr>
        </Cdtr>
        <CdtrAcct><Id><IBAN>SI56011006030694121</IBAN></Id></CdtrAcct>
        <Purp><Cd>GDDS</Cd></Purp>
        <RmtInf>
          <Strd><CdtrRefInf><Ref>SI00123456</Ref></CdtrRefInf></Strd>
        </RmtInf>
      </CdtTrfTxInf>
      <CdtTrfTxInf>
        <PmtId><EndToEndId>E2E-2</EndToEndId></PmtId>
        <Amt><InstdAmt Ccy="EUR">10.00</InstdAmt></Amt>
        <Cdtr><Nm>Prejemnik Dva d.o.o.</Nm></Cdtr>
        <CdtrAcct><Id><IBAN>SI52031001000051063</IBAN></Id></CdtrAcct>
        <RmtInf><Ustrd>Racun 2026-7</Ustrd></RmtInf>
      </CdtTrfTxInf>
    </PmtInf>
  </CstmrCdtTrfInitn>
</Document>
"""


def test_parse_pain_returns_one_upn_per_transaction() -> None:
    upns = parse_pain_credit_transfers(PAIN_TWO_TX)

    assert len(upns) == 2

    first, second = upns
    assert first.recipient_iban == "SI56011006030694121"
    assert first.recipient_reference == "SI00123456"
    assert first.recipient_name == "Prejemnik Ena d.o.o."
    assert first.recipient_street == "Glavna cesta 1"
    assert first.recipient_city == "2000 Maribor"
    assert first.amount_cents == 12345
    assert first.purpose_code == "GDDS"
    assert first.payment_deadline == date(2026, 5, 15)
    assert first.payer_iban == "SI56011111111111117"
    assert first.payer_name == "Placnik d.o.o."

    assert second.recipient_iban == "SI52031001000051063"
    assert second.recipient_reference == ""
    assert second.payment_purpose == "Racun 2026-7"
    assert second.purpose_code == "OTHR"
    assert second.amount_cents == 1000


def test_parse_pain_can_drop_payer() -> None:
    upns = parse_pain_credit_transfers(PAIN_TWO_TX, include_payer=False)

    assert all(u.payer_iban == "" and u.payer_name == "" for u in upns)


def test_parse_pain_nested_execution_date() -> None:
    xml = PAIN_TWO_TX.replace(
        "<ReqdExctnDt>2026-05-15</ReqdExctnDt>",
        "<ReqdExctnDt><Dt>2026-06-20</Dt></ReqdExctnDt>",
    )
    upns = parse_pain_credit_transfers(xml)
    assert upns[0].payment_deadline == date(2026, 6, 20)


def test_rejects_non_pain_xml() -> None:
    with pytest.raises(PainParseError, match="root element"):
        parse_pain_credit_transfers(
            b'<Invoice xmlns="urn:eslog:2.00"><M_INVOIC/></Invoice>'
        )


def test_rejects_pain_without_transactions() -> None:
    xml = """<?xml version="1.0"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03">
  <CstmrCdtTrfInitn>
    <GrpHdr><MsgId>X</MsgId></GrpHdr>
  </CstmrCdtTrfInitn>
</Document>"""
    with pytest.raises(PainParseError, match="no credit transfers"):
        parse_pain_credit_transfers(xml)


def test_rejects_transaction_without_creditor_iban() -> None:
    xml = """<?xml version="1.0"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03">
  <CstmrCdtTrfInitn>
    <GrpHdr><MsgId>X</MsgId></GrpHdr>
    <PmtInf>
      <CdtTrfTxInf>
        <Amt><InstdAmt Ccy="EUR">5.00</InstdAmt></Amt>
        <Cdtr><Nm>No Account</Nm></Cdtr>
      </CdtTrfTxInf>
    </PmtInf>
  </CstmrCdtTrfInitn>
</Document>"""
    with pytest.raises(PainParseError, match="creditor IBAN"):
        parse_pain_credit_transfers(xml)
