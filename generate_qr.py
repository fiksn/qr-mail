#!/usr/bin/env python3
"""Interactive CLI to generate UPN and EPC SCT payment outputs.

Usage:
  python3 generate_qr.py
  python3 generate_qr.py --output invoice_123   # filename prefix for saved PNGs
"""
import argparse
import sys
import io
import os
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from generate import generate_epc_qr, generate_upn_qr, upn_to_epc
from upn import UPN, UPNReferenceError, validate_upn_reference

DEFAULT_SLIP_TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "upn_base_empty.jpg",
)


def ask(prompt: str, *, default: str = "", required: bool = False) -> str:
    display = f"{prompt} [{default}]: " if default else f"{prompt}: "
    while True:
        value = input(display).strip()
        if not value:
            value = default
        if required and not value:
            print("  (required)")
            continue
        return value


def ask_bool(prompt: str, *, default: bool = False) -> bool:
    default_txt = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{default_txt}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "1", "true"}:
            return True
        if raw in {"n", "no", "0", "false"}:
            return False
        print("  Enter y or n.")


def ask_amount() -> int:
    """Ask for EUR amount and return cents. Returns 0 if left blank."""
    while True:
        raw = input("Amount in EUR (blank = unspecified): ").strip()
        if not raw:
            return 0
        try:
            d = Decimal(raw.replace(",", "."))
            if d < 0:
                print("  Amount must be positive.")
                continue
            return int(d * 100)
        except InvalidOperation:
            print("  Enter a number, e.g. 58.27")


def ask_date(prompt: str, *, default_today: bool = False) -> Optional[date]:
    suffix = " (DD.MM.YYYY, blank=today)" if default_today else " (DD.MM.YYYY, blank=none)"
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw:
            return date.today() if default_today else None
        try:
            return datetime.strptime(raw, "%d.%m.%Y").date()
        except ValueError:
            print("  Enter date as DD.MM.YYYY")


def ask_reference() -> str:
    """Ask for a SI or RF reference, validate, and return compact form."""
    while True:
        raw = input("Reference (SI/RF, blank = none): ").strip()
        if not raw:
            return ""
        try:
            return validate_upn_reference(raw)
        except UPNReferenceError as exc:
            print(f"  Invalid reference: {exc}")


def ask_optional_iban(prompt: str) -> str:
    while True:
        raw = input(f"{prompt} (blank = none): ").strip()
        if not raw:
            return ""
        compact = raw.replace(" ", "").upper()
        if len(compact) < 15 or not compact[:2].isalpha() or not compact[2:].isdigit():
            print("  Doesn't look like a valid IBAN.")
            continue
        return compact


def ask_reference_optional(prompt: str) -> str:
    while True:
        raw = input(f"{prompt} (SI/RF, blank = none): ").strip()
        if not raw:
            return ""
        try:
            return validate_upn_reference(raw)
        except UPNReferenceError as exc:
            print(f"  Invalid reference: {exc}")


def ask_iban() -> str:
    while True:
        raw = input("IBAN (required): ").strip().replace(" ", "").upper()
        if not raw:
            print("  (required)")
            continue
        if len(raw) < 15 or not raw[:2].isalpha() or not raw[2:].isdigit():
            print("  Doesn't look like a valid IBAN.")
            continue
        return raw


def _load_font(size: int, *, bold: bool = False) -> Any:
    from PIL import ImageFont

    faces = (
        [
            "Courier New Bold.ttf",
            "Courier-Bold.ttf",
            "NimbusMonoPS-Bold.otf",
            "LiberationMono-Bold.ttf",
            "DejaVuSansMono-Bold.ttf",
        ]
        if bold
        else [
            "Courier New.ttf",
            "Courier.ttf",
            "NimbusMonoPS-Regular.otf",
            "LiberationMono-Regular.ttf",
            "DejaVuSansMono.ttf",
        ]
    )
    for face in faces:
        try:
            return ImageFont.truetype(face, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _format_eur_for_slip(amount_cents: int) -> str:
    if amount_cents <= 0:
        return "***"
    eur = f"{amount_cents / 100:.2f}".replace(".", ",")
    return f"***{eur}"


def _fit_text(draw: Any, text: str, font: Any, max_width: int) -> str:
    if not text:
        return ""
    value = text.strip()
    if draw.textlength(value, font=font) <= max_width:
        return value
    suffix = "..."
    while value and draw.textlength(value + suffix, font=font) > max_width:
        value = value[:-1]
    return (value + suffix) if value else ""


def _draw_in_box(
    draw: Any,
    *,
    box: tuple[int, int, int, int],
    text: str,
    font: Any,
    fill: str = "#202020",
    align: str = "left",
    multiline: bool = False,
) -> None:
    x1, y1, x2, y2 = box
    max_width = max(1, (x2 - x1) - 8)
    y = y1 + 4
    if multiline:
        lines: list[str] = []
        if "\n" in text:
            raw_lines = [ln.strip() for ln in text.splitlines()]
            lines = [_fit_text(draw, ln, font, max_width) for ln in raw_lines if ln][:3]
        else:
            remaining = text.strip()
            for _ in range(3):
                if not remaining:
                    break
                candidate = remaining
                while candidate and draw.textlength(candidate, font=font) > max_width:
                    cut = candidate.rfind(" ")
                    if cut <= 0:
                        candidate = _fit_text(draw, candidate, font, max_width)
                        break
                    candidate = candidate[:cut]
                lines.append(candidate)
                remaining = remaining[len(candidate):].strip()
        slot_h = max(1.0, (y2 - y1 - 8) / 3.0)
        for idx, line in enumerate(lines[:3]):
            if not line:
                continue
            if align == "right":
                draw.text((x2 - 4, y1 + 4 + idx * slot_h + slot_h / 2), line, font=font, fill=fill, anchor="rm")
            else:
                draw.text((x1 + 4, y1 + 4 + idx * slot_h + slot_h / 2), line, font=font, fill=fill, anchor="lm")
        return

    value = _fit_text(draw, text, font, max_width)
    if align == "right":
        tw = draw.textlength(value, font=font)
        draw.text((x2 - tw - 4, y), value, font=font, fill=fill)
    else:
        draw.text((x1 + 4, y), value, font=font, fill=fill)


def _draw_boxed_chars(
    draw: Any,
    *,
    box: tuple[int, int, int, int],
    text: str,
    cells: int,
    font: Any,
    fill: str = "#202020",
    align: str = "left",
    x_shift: float = 0.0,
    y_shift: float = 0.0,
    tracking: float = 0.0,
) -> None:
    if cells <= 0:
        return
    x1, y1, x2, y2 = box
    width = x2 - x1
    if width <= 0:
        return
    content = text or ""
    if len(content) > cells:
        content = content[-cells:] if align == "right" else content[:cells]
    if align == "right":
        content = content.rjust(cells)
    else:
        content = content.ljust(cells)

    cell_w = width / cells
    cy = (y1 + y2) // 2 + 1 + y_shift
    for i, ch in enumerate(content):
        if ch == " ":
            continue
        cx = int(x1 + (i + 0.5) * cell_w + x_shift + i * tracking)
        draw.text((cx, cy), ch, font=font, fill=fill, anchor="mm")


def _reference_for_boxes(ref: str) -> str:
    compact = (ref or "").replace(" ", "")
    # Keep a visible separator cell between SI model and reference body.
    if compact.startswith("SI") and len(compact) > 4:
        return compact[:4] + " " + compact[4:]
    return compact


def _draw_amount_boxed(
    draw: Any,
    *,
    box: tuple[int, int, int, int],
    amount_cents: int,
    cells: int,
    font: Any,
    fill: str = "#202020",
) -> None:
    if amount_cents <= 0:
        return
    digits = f"{amount_cents:d}"
    x1, y1, x2, y2 = box
    cell_w = (x2 - x1) / cells
    # Keep comma between the two cent digits (not occupying a box).
    cents = digits[-2:].rjust(2, "0")
    eur = digits[:-2] or "0"
    # Keep stars attached to amount (no gap between "***" and number),
    # then right-align the full group in the field.
    group = "***" + eur + cents
    content = list(group.rjust(cells))

    cy = (y1 + y2) // 2 + 1
    for i, ch in enumerate(content):
        if ch == " ":
            continue
        cx = int(x1 + (i + 0.5) * cell_w)
        draw.text((cx, cy), ch, font=font, fill=fill, anchor="mm")

    # Comma drawn between EUR and cents cells.
    comma_boundary = cells - 2
    comma_x = int(x1 + comma_boundary * cell_w)
    draw.text((comma_x, cy + 1), ",", font=font, fill=fill, anchor="mm")


def generate_upn_slip_png(
    upn: UPN,
    *,
    template_path: Optional[str] = None,
    render_payer_top_fields: bool = False,
) -> bytes:
    """Render a filled UPN payment slip using a scanned empty UPN form template."""
    from PIL import Image, ImageDraw

    source = template_path or DEFAULT_SLIP_TEMPLATE
    if not os.path.exists(source):
        raise FileNotFoundError(f"UPN slip template not found: {source}")

    img = Image.open(source).convert("RGB")
    draw = ImageDraw.Draw(img)

    font_small = _load_font(13, bold=True)
    font_main = _load_font(14, bold=True)
    font_main_bold = _load_font(14, bold=True)
    font_boxed = _load_font(13, bold=True)
    font_amount = _load_font(17, bold=True)

    amount_text = _format_eur_for_slip(upn.amount_cents)
    payment_date = upn.payment_date.strftime("%d.%m.%Y") if upn.payment_date else ""
    deadline = upn.payment_deadline.strftime("%d.%m.%Y") if upn.payment_deadline else ""
    payer_full = "\n".join(v for v in [upn.payer_name, upn.payer_street, upn.payer_city] if v)
    recipient_full = "\n".join(v for v in [upn.recipient_name, upn.recipient_street, upn.recipient_city] if v)
    purpose_full = " ".join(v for v in [upn.payment_purpose, deadline] if v)

    # Left detached confirmation part.
    _draw_in_box(draw, box=(8, 23, 239, 80), text=payer_full, font=font_small, multiline=True)
    _draw_in_box(draw, box=(8, 95, 239, 132), text=purpose_full, font=font_small, multiline=True)
    _draw_in_box(draw, box=(62, 146, 239, 165), text=amount_text, font=font_main)
    left_iban_ref = "\n".join(v for v in [upn.recipient_iban.strip(), upn.recipient_reference.strip()] if v)
    _draw_in_box(draw, box=(8, 176, 239, 235), text=left_iban_ref, font=font_small, multiline=True)
    _draw_in_box(draw, box=(8, 248, 239, 307), text=recipient_full, font=font_small, multiline=True)

    # Right main payment block.
    payer_iban_box = upn.payer_iban.replace(" ", "")
    payer_ref_box = _reference_for_boxes(upn.payer_reference)
    recipient_iban_box = upn.recipient_iban.replace(" ", "")
    recipient_ref_box = _reference_for_boxes(upn.recipient_reference)
    if render_payer_top_fields:
        _draw_boxed_chars(draw, box=(449, 24, 760, 42), text=payer_iban_box, cells=34, font=font_boxed, align="left", x_shift=4.0)
        _draw_boxed_chars(draw, box=(449, 54, 760, 77), text=payer_ref_box, cells=26, font=font_boxed, align="left", x_shift=4.5)
    _draw_in_box(draw, box=(449, 93, 760, 153), text=payer_full, font=font_small, multiline=True)

    # Amount boxes are filled right-aligned so the least significant digit lands in the rightmost box.
    _draw_amount_boxed(draw, box=(518, 167, 662, 189), amount_cents=upn.amount_cents, cells=10, font=font_amount)
    _draw_boxed_chars(draw, box=(681, 167, 818, 189), text=payment_date, cells=10, font=font_boxed, align="left")
    if upn.urgent:
        draw.line((838, 169, 866, 193), fill="#1A1A1A", width=2)
        draw.line((866, 169, 838, 193), fill="#1A1A1A", width=2)

    _draw_boxed_chars(draw, box=(266, 205, 334, 223), text=upn.purpose_code, cells=4, font=font_boxed, align="left")
    _draw_boxed_chars(
        draw,
        box=(336, 205, 741, 223),
        text=upn.payment_purpose,
        cells=42,
        font=font_small,
        align="left",
        x_shift=3.0,
        tracking=0.30,
    )
    _draw_boxed_chars(draw, box=(744, 205, 879, 223), text=deadline, cells=10, font=font_boxed, align="left")

    _draw_boxed_chars(draw, box=(266, 239, 818, 258), text=recipient_iban_box, cells=34, font=font_boxed, align="left", y_shift=2.0)
    _draw_boxed_chars(draw, box=(266, 274, 696, 296), text=recipient_ref_box, cells=26, font=font_boxed, align="left", x_shift=-4.0, y_shift=2.0)
    _draw_in_box(draw, box=(266, 309, 696, 378), text=recipient_full, font=font_small, multiline=True)

    if upn.deposit:
        draw.line((810, 23, 835, 42), fill="#1A1A1A", width=2)
        draw.line((835, 23, 810, 42), fill="#1A1A1A", width=2)
    if upn.withdrawal:
        draw.line((856, 23, 880, 42), fill="#1A1A1A", width=2)
        draw.line((880, 23, 856, 42), fill="#1A1A1A", width=2)

    qr_upn = upn
    if not render_payer_top_fields and upn.payer_iban:
        qr_upn = replace(upn, payer_iban="")
    qr_img = Image.open(io.BytesIO(generate_upn_qr(qr_upn, scale=8))).convert("RGB")
    qr_top = qr_img.resize((169, 169), Image.Resampling.NEAREST)
    img.paste(qr_top, (267, 24))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_upn(
    amount_cents: int,
    reference: str,
    purpose_code: str,
    payment_purpose: str,
    iban: str,
    name: str,
    street: str,
    city: str,
    *,
    payer_iban: str,
    payer_reference: str,
    payer_name: str,
    payer_street: str,
    payer_city: str,
    deposit: bool,
    withdrawal: bool,
    urgent: bool,
    payment_date: Optional[date],
    payment_deadline: Optional[date],
) -> UPN:
    return UPN(
        payer_iban=payer_iban[:34], deposit=deposit, withdrawal=withdrawal,
        payer_reference=payer_reference, payer_name=payer_name[:33], payer_street=payer_street[:33], payer_city=payer_city[:33],
        amount_cents=amount_cents,
        payment_date=payment_date, urgent=urgent,
        purpose_code=purpose_code,
        payment_purpose=payment_purpose,
        payment_deadline=payment_deadline,
        recipient_iban=iban,
        recipient_reference=reference,
        recipient_name=name[:33],
        recipient_street=street[:33],
        recipient_city=city[:33],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate UPN QR, EPC QR, and a full Slovenian UPN payment slip image."
    )
    parser.add_argument("--output", metavar="PREFIX", default="payment",
                        help="filename prefix for saved PNGs (default: payment)")
    parser.add_argument(
        "--format",
        choices=["upn", "epc", "both", "slip", "poloznica", "all"],
        default="both",
        help="output mode: upn, epc, both, slip/poloznica, all (default: both)",
    )
    parser.add_argument(
        "--slip-template",
        default=DEFAULT_SLIP_TEMPLATE,
        help=f"path to empty UPN slip template image (default: {DEFAULT_SLIP_TEMPLATE})",
    )
    args = parser.parse_args()

    print("Generate payment outputs")
    print("─" * 40)

    print("Recipient (prejemnik)")
    iban = ask_iban()
    name = ask("Recipient name", required=True)
    street = ask("Recipient street")
    city = ask("Recipient city", required=True)
    reference = ask_reference()
    amount_cents = ask_amount()
    payment_date = ask_date("Payment date", default_today=True)
    payment_deadline = ask_date("Payment deadline")
    purpose_code = ask("Purpose code", default="OTHR")
    payment_purpose = ask("Payment purpose / description")

    print()
    print("Payer (placnik) - optional")
    payer_iban = ask_optional_iban("Payer IBAN")
    payer_reference = ask_reference_optional("Payer reference")
    payer_name = ask("Payer name")
    payer_street = ask("Payer street")
    payer_city = ask("Payer city")
    deposit = ask_bool("Polog")
    withdrawal = ask_bool("Dvig")
    urgent = ask_bool("Nujno")

    print()

    upn = build_upn(amount_cents, reference, purpose_code,
                    payment_purpose, iban, name, street, city,
                    payer_iban=payer_iban, payer_reference=payer_reference,
                    payer_name=payer_name, payer_street=payer_street,
                    payer_city=payer_city, deposit=deposit,
                    withdrawal=withdrawal, urgent=urgent,
                    payment_date=payment_date, payment_deadline=payment_deadline)

    want_upn = args.format in ("upn", "both", "all")
    want_epc = args.format in ("epc", "both", "all")
    want_slip = args.format in ("slip", "poloznica", "all")

    prefix = args.output

    if want_upn:
        upn_path = f"{prefix}_upn.png"
        with open(upn_path, "wb") as f:
            f.write(generate_upn_qr(upn))
        print(f"UPN QR saved: {upn_path}")

    if want_epc:
        try:
            epc = upn_to_epc(upn)
        except Exception as exc:
            print(f"EPC conversion failed: {exc}", file=sys.stderr)
            sys.exit(1)
        epc_path = f"{prefix}_epc.png"
        with open(epc_path, "wb") as f:
            f.write(generate_epc_qr(epc))
        print(f"EPC QR saved: {epc_path}")

    if want_slip:
        try:
            slip_png = generate_upn_slip_png(upn, template_path=args.slip_template)
        except ModuleNotFoundError as exc:
            if exc.name == "PIL":
                print("Slip rendering requires Pillow: pip install pillow", file=sys.stderr)
                sys.exit(1)
            raise
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        slip_path = f"{prefix}_poloznica.png"
        with open(slip_path, "wb") as f:
            f.write(slip_png)
        print(f"UPN slip saved: {slip_path}")


if __name__ == "__main__":
    main()
