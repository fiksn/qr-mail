import os
from io import BytesIO
from pathlib import Path

from PIL import Image

from core.upn import UPN
from scripts.generate_qr import (
    PAYEE_FILE_ENV,
    PAYER_FILE_ENV,
    generate_legacy_upn_slip_png,
    generate_upn_slip_png,
    load_payee_defaults_from_env,
    load_payee_template_from_env,
    load_payer_defaults_from_env,
    load_party_defaults_from_env,
)


REPO_DIR = Path(__file__).parent.parent


def test_load_payer_defaults_from_env_ignores_bad_file(monkeypatch, tmp_path) -> None:
    bad_file = tmp_path / "bad_payer.txt"
    bad_file.write_text("Janez Novak\nCelovska 137\n", encoding="utf-8")
    monkeypatch.setenv(PAYER_FILE_ENV, os.fspath(bad_file))

    assert load_payer_defaults_from_env() == ("", "", "")


def test_load_payee_template_from_env_allows_blank_iban(monkeypatch, tmp_path) -> None:
    payee_file = tmp_path / "example_doo.txt"
    payee_file.write_text(
        "\n"
        "Example d.o.o.\n"
        "Dunajska cesta 10\n"
        "1000 Ljubljana\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(PAYEE_FILE_ENV, os.fspath(payee_file))

    assert load_payee_template_from_env() == (
        "",
        "Example d.o.o.",
        "Dunajska cesta 10",
        "1000 Ljubljana",
    )
    assert load_payee_defaults_from_env() == (
        "Example d.o.o.",
        "Dunajska cesta 10",
        "1000 Ljubljana",
    )


def test_load_party_defaults_from_env_truncates_fields(monkeypatch, tmp_path) -> None:
    long_file = tmp_path / "long_party.txt"
    long_file.write_text(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789\n"
        "Long Street Name 12345678901234567890\n"
        "Ljubljana-Siska-12345678901234567890\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(PAYEE_FILE_ENV, os.fspath(long_file))

    assert load_party_defaults_from_env(PAYEE_FILE_ENV) == (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567",
        "Long Street Name 1234567890123456",
        "Ljubljana-Siska-12345678901234567",
    )


def test_generate_upn_slip_png_marks_deposit_withdrawal_and_urgent(tmp_path) -> None:
    template = tmp_path / "template.jpg"
    Image.new("RGB", (900, 420), "white").save(template)

    upn = UPN(
        payer_iban="",
        deposit=True,
        withdrawal=True,
        payer_reference="",
        payer_name="Janez Novak",
        payer_street="Celovska 137",
        payer_city="1000 Ljubljana",
        amount_cents=12345,
        payment_date=None,
        urgent=True,
        purpose_code="OTHR",
        payment_purpose="Test payment",
        payment_deadline=None,
        recipient_iban="SI56192001234567892",
        recipient_reference="SI0012345",
        recipient_name="Example d.o.o.",
        recipient_street="Dunajska cesta 10",
        recipient_city="1000 Ljubljana",
    )

    png = generate_upn_slip_png(upn, template_path=os.fspath(template))
    img = Image.open(BytesIO(png)).convert("RGB")

    assert img.getpixel((822, 32)) != (255, 255, 255)
    assert img.getpixel((868, 32)) != (255, 255, 255)
    assert img.getpixel((852, 181)) != (255, 255, 255)


def test_generate_legacy_upn_slip_png_marks_flags_and_ocr_line() -> None:
    upn = UPN(
        payer_iban="",
        deposit=True,
        withdrawal=True,
        payer_reference="",
        payer_name="Janez Novak",
        payer_street="Celovska 137",
        payer_city="1000 Ljubljana",
        amount_cents=5629,
        payment_date=None,
        urgent=True,
        purpose_code="OTHR",
        payment_purpose="Test payment",
        payment_deadline=None,
        recipient_iban="SI56192001234567892",
        recipient_reference="SI126604876475931",
        recipient_name="Example d.o.o.",
        recipient_street="Dunajska cesta 10",
        recipient_city="1000 Ljubljana",
    )

    png = generate_legacy_upn_slip_png(upn)
    img = Image.open(BytesIO(png)).convert("RGB")

    assert img.getpixel((552, 14)) != (255, 255, 255)
    assert img.getpixel((585, 14)) != (255, 255, 255)
    assert img.getpixel((616, 14)) != (255, 255, 255)
    assert any(
        img.getpixel((x, y)) != (255, 255, 255)
        for y in range(352, 366)
        for x in range(246, 320)
    )
