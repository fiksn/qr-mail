from datetime import date
from decimal import Decimal

import pytest

from generators.eslog import (
    Invoice,
    LineItem,
    Party,
    generate_eslog_invoice,
)
from parsers.eslog import parse_eslog_invoice


def _simple_invoice(**overrides) -> Invoice:
    defaults = dict(
        invoice_number="INV-2026-001",
        issue_date=date(2026, 4, 1),
        seller=Party(
            name="Prodajalec d.o.o.",
            street="Glavna cesta 1",
            city="Ljubljana",
            postal_code="1000",
            country_code="SI",
            tax_id="SI12345678",
            iban="SI56043020003519144",
        ),
        buyer=Party(
            name="Kupec d.o.o.",
            street="Stranska ulica 5",
            city="Maribor",
            postal_code="2000",
            country_code="SI",
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
        purpose_code="OTHR",
    )
    defaults.update(overrides)
    return Invoice(**defaults)


def test_generate_produces_valid_xml() -> None:
    xml = generate_eslog_invoice(_simple_invoice())
    assert xml.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    assert b"urn:eslog:2.00" in xml


def test_roundtrip_basic_fields() -> None:
    inv = _simple_invoice()
    xml = generate_eslog_invoice(inv)
    upn = parse_eslog_invoice(xml)

    assert upn.recipient_iban == "SI56043020003519144"
    assert upn.recipient_reference == "SI001234567890123"
    assert upn.recipient_name == "Prodajalec d.o.o."
    assert upn.recipient_city == "Ljubljana"
    assert upn.amount_cents == 12200  # 100 + 22% VAT = 122.00
    assert upn.payment_deadline == date(2026, 5, 1)
    assert upn.purpose_code == "OTHR"


def test_roundtrip_with_payee() -> None:
    payee = Party(
        name="Prejemnik placila d.o.o.",
        street="Placilna ulica 2",
        city="Celje",
    )
    inv = _simple_invoice(payee=payee)
    xml = generate_eslog_invoice(inv)
    upn = parse_eslog_invoice(xml)

    assert upn.recipient_name == "Prejemnik placila d.o.o."
    assert upn.recipient_city == "Celje"
    assert upn.recipient_iban == "SI56043020003519144"


def test_roundtrip_multiple_items() -> None:
    items = [
        LineItem(
            description="Storitev A",
            quantity=Decimal("2"),
            unit="C62",
            unit_price_net=Decimal("50.00"),
            vat_rate=Decimal("22"),
        ),
        LineItem(
            description="Storitev B",
            quantity=Decimal("1"),
            unit="C62",
            unit_price_net=Decimal("30.00"),
            vat_rate=Decimal("9.5"),
        ),
    ]
    inv = _simple_invoice(items=items)
    xml = generate_eslog_invoice(inv)
    upn = parse_eslog_invoice(xml)

    # 2*50 = 100 + 22% = 122.00
    # 1*30 = 30 + 9.5% = 32.85
    assert upn.amount_cents == 15485  # 154.85


def test_roundtrip_payment_purpose() -> None:
    inv = _simple_invoice(payment_purpose="Plačilo računa INV-2026-001")
    xml = generate_eslog_invoice(inv)
    upn = parse_eslog_invoice(xml)

    assert upn.payment_purpose == "Plačilo računa INV-2026-001"


def test_roundtrip_no_due_date() -> None:
    inv = _simple_invoice(payment_due_date=None)
    xml = generate_eslog_invoice(inv)
    upn = parse_eslog_invoice(xml)

    assert upn.payment_deadline is None


def test_rejects_invalid_seller_iban() -> None:
    with pytest.raises(Exception):
        generate_eslog_invoice(
            _simple_invoice(
                seller=Party(
                    name="Bad", street="", city="X",
                    iban="INVALID",
                ),
            ),
        )


def test_line_item_amounts() -> None:
    item = LineItem(
        description="Test",
        quantity=Decimal("3"),
        unit="C62",
        unit_price_net=Decimal("10.00"),
        vat_rate=Decimal("22"),
    )
    assert item.net_amount == Decimal("30.00")
    assert item.vat_amount == Decimal("6.60")
    assert item.gross_amount == Decimal("36.60")


def test_invoice_number_in_xml() -> None:
    xml = generate_eslog_invoice(_simple_invoice())
    assert b"INV-2026-001" in xml
