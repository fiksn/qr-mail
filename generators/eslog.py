"""Generate eSLOG 2.0 invoice XML.

Produces valid ``urn:eslog:2.00`` EDIFACT-mapped XML invoices that can
be parsed back by ``parsers.eslog`` and optionally signed with XMLDSig.

Reference: https://epos.si/assets/docs/e-SLOG-2.0-08-2020-EN.zip
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from xml.etree.ElementTree import Element, SubElement, tostring

from core.epc import _validate_iban

NS_URI = "urn:eslog:2.00"


@dataclass(frozen=True)
class Party:
    """Invoice party (seller, buyer, or payee)."""

    name: str
    street: str
    city: str
    postal_code: str = ""
    country_code: str = "SI"
    tax_id: str = ""
    registration_number: str = ""
    iban: str = ""
    bank_name: str = ""


@dataclass(frozen=True)
class LineItem:
    """Single invoice line item."""

    description: str
    quantity: Decimal
    unit: str  # C62 (piece), MON (month), HUR (hour), etc.
    unit_price_net: Decimal  # net price per unit (excl. VAT)
    vat_rate: Decimal  # e.g. Decimal("22") for 22%

    @property
    def net_amount(self) -> Decimal:
        return (self.quantity * self.unit_price_net).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )

    @property
    def vat_amount(self) -> Decimal:
        return (self.net_amount * self.vat_rate / 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )

    @property
    def gross_amount(self) -> Decimal:
        return self.net_amount + self.vat_amount


@dataclass(frozen=True)
class Invoice:
    """eSLOG 2.0 invoice data."""

    invoice_number: str
    issue_date: date
    seller: Party
    buyer: Party
    items: list[LineItem]
    payment_due_date: Optional[date] = None
    delivery_date: Optional[date] = None
    payment_reference: str = ""
    purpose_code: str = "OTHR"
    payment_purpose: str = ""
    currency: str = "EUR"
    document_type_code: str = "380"
    document_type_name: str = ""
    payee: Optional[Party] = None


def _el(parent: Element, tag: str, text: str = "") -> Element:
    child = SubElement(parent, tag)
    if text:
        child.text = text
    return child


def _ftx(parent: Element, qualifier: str, text: str) -> None:
    ftx = _el(parent, "S_FTX")
    _el(ftx, "D_4451", qualifier)
    c108 = _el(ftx, "C_C108")
    _el(c108, "D_4440", text)


def _moa(parent: Element, code: str, amount: Decimal) -> None:
    group = _el(parent, "G_SG50")
    moa = _el(group, "S_MOA")
    c516 = _el(moa, "C_C516")
    _el(c516, "D_5025", code)
    _el(c516, "D_5004", str(amount))


def _dtm(parent: Element, code: str, d: date) -> None:
    dtm = _el(parent, "S_DTM")
    c507 = _el(dtm, "C_C507")
    _el(c507, "D_2005", code)
    _el(c507, "D_2380", d.isoformat())


def _party_group(
    parent: Element, role: str, party: Party,
) -> Element:
    sg2 = _el(parent, "G_SG2")
    nad = _el(sg2, "S_NAD")
    _el(nad, "D_3035", role)
    c080 = _el(nad, "C_C080")
    _el(c080, "D_3036", party.name)
    if party.street:
        c059 = _el(nad, "C_C059")
        _el(c059, "D_3042", party.street)
    _el(nad, "D_3164", party.city)
    if party.postal_code:
        _el(nad, "D_3251", party.postal_code)
    if party.country_code:
        _el(nad, "D_3207", party.country_code)

    if party.iban:
        fii = _el(sg2, "S_FII")
        _el(fii, "D_3035", "RB")
        c078 = _el(fii, "C_C078")
        _el(c078, "D_3194", party.iban)
        if party.bank_name:
            c088 = _el(fii, "C_C088")
            _el(c088, "D_3432", party.bank_name)

    if party.registration_number:
        sg3 = _el(sg2, "G_SG3")
        rff = _el(sg3, "S_RFF")
        c506 = _el(rff, "C_C506")
        _el(c506, "D_1153", "0199")
        _el(c506, "D_1154", party.registration_number)

    if party.tax_id:
        sg3 = _el(sg2, "G_SG3")
        rff = _el(sg3, "S_RFF")
        c506 = _el(rff, "C_C506")
        _el(c506, "D_1153", "VA")
        _el(c506, "D_1154", party.tax_id)

    return sg2


def _line_item_group(
    parent: Element, line_number: int, item: LineItem,
) -> None:
    sg26 = _el(parent, "G_SG26")

    lin = _el(sg26, "S_LIN")
    _el(lin, "D_1082", str(line_number))

    imd = _el(sg26, "S_IMD")
    _el(imd, "D_7077", "F")
    c273 = _el(imd, "C_C273")
    _el(c273, "D_7008", item.description)

    qty = _el(sg26, "S_QTY")
    c186 = _el(qty, "C_C186")
    _el(c186, "D_6063", "47")
    _el(c186, "D_6060", str(item.quantity))
    _el(c186, "D_6411", item.unit)

    # Net line amount
    sg27 = _el(sg26, "G_SG27")
    moa = _el(sg27, "S_MOA")
    c516 = _el(moa, "C_C516")
    _el(c516, "D_5025", "203")
    _el(c516, "D_5004", str(item.net_amount))

    # Gross line amount
    sg27 = _el(sg26, "G_SG27")
    moa = _el(sg27, "S_MOA")
    c516 = _el(moa, "C_C516")
    _el(c516, "D_5025", "38")
    _el(c516, "D_5004", str(item.gross_amount))

    # Unit price
    sg29 = _el(sg26, "G_SG29")
    pri = _el(sg29, "S_PRI")
    c509 = _el(pri, "C_C509")
    _el(c509, "D_5125", "AAA")
    _el(c509, "D_5118", str(item.unit_price_net))

    # Tax detail
    sg34 = _el(sg26, "G_SG34")
    tax = _el(sg34, "S_TAX")
    _el(tax, "D_5283", "7")
    c241 = _el(tax, "C_C241")
    _el(c241, "D_5153", "VAT")
    c243 = _el(tax, "C_C243")
    _el(c243, "D_5278", f"{item.vat_rate:.4f}")
    _el(tax, "D_5305", "S")

    tax_base = _el(sg34, "S_MOA")
    c516 = _el(tax_base, "C_C516")
    _el(c516, "D_5025", "125")
    _el(c516, "D_5004", str(item.net_amount))

    tax_amt = _el(sg34, "S_MOA")
    c516 = _el(tax_amt, "C_C516")
    _el(c516, "D_5025", "124")
    _el(c516, "D_5004", str(item.vat_amount))


def _collect_vat_summary(
    items: list[LineItem],
) -> dict[Decimal, tuple[Decimal, Decimal]]:
    """Group items by VAT rate -> (total_net, total_vat)."""
    summary: dict[Decimal, tuple[Decimal, Decimal]] = {}
    for item in items:
        net, vat = summary.get(item.vat_rate, (Decimal(0), Decimal(0)))
        summary[item.vat_rate] = (net + item.net_amount, vat + item.vat_amount)
    return summary


def generate_eslog_invoice(invoice: Invoice) -> bytes:
    """Generate eSLOG 2.0 invoice XML from an Invoice dataclass.

    Returns UTF-8 encoded XML bytes.
    """
    _validate_iban(invoice.seller.iban.replace(" ", "").upper())

    root = Element("Invoice")
    root.set("xmlns", NS_URI)

    invoic = _el(root, "M_INVOIC")
    invoic.set("Id", "data")

    # S_UNH — message header
    unh = _el(invoic, "S_UNH")
    _el(unh, "D_0062", "1")
    s009 = _el(unh, "C_S009")
    _el(s009, "D_0065", "INVOIC")
    _el(s009, "D_0052", "D")
    _el(s009, "D_0054", "01B")
    _el(s009, "D_0051", "UN")

    # S_BGM — beginning of message
    bgm = _el(invoic, "S_BGM")
    c002 = _el(bgm, "C_C002")
    _el(c002, "D_1001", invoice.document_type_code)
    if invoice.document_type_name:
        _el(c002, "D_1000", invoice.document_type_name)
    c106 = _el(bgm, "C_C106")
    _el(c106, "D_1004", invoice.invoice_number)
    _el(bgm, "D_1225", "9")

    # Dates
    _dtm(invoic, "137", invoice.issue_date)
    if invoice.delivery_date:
        _dtm(invoic, "35", invoice.delivery_date)

    # Purpose code
    _ftx(invoic, "ALQ", invoice.purpose_code)

    # Payment purpose text (AAT = payment means text, used by parser)
    if invoice.payment_purpose:
        _ftx(invoic, "AAT", invoice.payment_purpose)

    # Payment reference
    if invoice.payment_reference:
        sg1 = _el(invoic, "G_SG1")
        rff = _el(sg1, "S_RFF")
        c506 = _el(rff, "C_C506")
        _el(c506, "D_1153", "PQ")
        _el(c506, "D_1154", invoice.payment_reference)

    # Parties
    _party_group(invoic, "SE", invoice.seller)
    _party_group(invoic, "BY", invoice.buyer)
    if invoice.payee:
        _party_group(invoic, "PE", invoice.payee)

    # Currency
    sg7 = _el(invoic, "G_SG7")
    cux = _el(sg7, "S_CUX")
    c504 = _el(cux, "C_C504")
    _el(c504, "D_6347", "2")
    _el(c504, "D_6345", invoice.currency)

    # Payment terms
    if invoice.payment_due_date:
        sg8 = _el(invoic, "G_SG8")
        pat = _el(sg8, "S_PAT")
        _el(pat, "D_4279", "1")
        _dtm(sg8, "13", invoice.payment_due_date)

    # Line items
    for i, item in enumerate(invoice.items, start=1):
        _line_item_group(invoic, i, item)

    # Summary amounts
    total_net = sum(
        (item.net_amount for item in invoice.items), Decimal(0),
    )
    total_vat = sum(
        (item.vat_amount for item in invoice.items), Decimal(0),
    )
    total_gross = total_net + total_vat

    _moa(invoic, "79", total_gross)   # total line items amount
    _moa(invoic, "389", total_net)    # total net
    _moa(invoic, "176", total_vat)    # total VAT
    _moa(invoic, "388", total_gross)  # amount due
    _moa(invoic, "9", total_gross)    # amount due for payment

    # Tax summary per VAT rate
    vat_summary = _collect_vat_summary(invoice.items)
    for rate, (net, vat) in sorted(vat_summary.items()):
        sg52 = _el(invoic, "G_SG52")
        tax = _el(sg52, "S_TAX")
        _el(tax, "D_5283", "7")
        c241 = _el(tax, "C_C241")
        _el(c241, "D_5153", "VAT")
        c243 = _el(tax, "C_C243")
        _el(c243, "D_5278", f"{rate:.4f}")
        _el(tax, "D_5305", "S")

        base_moa = _el(sg52, "S_MOA")
        c516 = _el(base_moa, "C_C516")
        _el(c516, "D_5025", "125")
        _el(c516, "D_5004", str(net))

        vat_moa = _el(sg52, "S_MOA")
        c516 = _el(vat_moa, "C_C516")
        _el(c516, "D_5025", "124")
        _el(c516, "D_5004", str(vat))

        gross_moa = _el(sg52, "S_MOA")
        c516 = _el(gross_moa, "C_C516")
        _el(c516, "D_5025", "168")
        _el(c516, "D_5004", str(net + vat))

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(
        root, encoding="unicode",
    ).encode("utf-8")
