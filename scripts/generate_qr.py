#!/usr/bin/env python3
"""Interactive CLI to generate UPN and EPC SCT payment outputs.

Usage:
  python3 scripts/generate_qr.py
  python3 scripts/generate_qr.py --output invoice_123   # filename prefix for saved PNGs
"""
import argparse
import glob
import io
import os
import sys
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.epc import EPC
from core.generate import (
    epc_to_string,
    generate_epc_qr,
    generate_upn_qr,
    upn_to_epc,
    upn_to_string,
)
from core.upn import (
    UPN,
    UPNLegacyOCRError,
    UPNReferenceError,
    format_legacy_upn_ocr,
    validate_upn_reference,
)

DEFAULT_SLIP_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "upn_base_empty.jpg",
)
DEFAULT_LEGACY_SLIP_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "upn_base_legacy_ocr.jpg",
)
DEFAULT_OCRB_FONT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ocrb.ttf",
)
DEFAULT_PARTY_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "janez_novak.txt",
)
PAYER_FILE_ENV = "QR_MAIL_PAYER_FILE"
PAYEE_FILE_ENV = "QR_MAIL_PAYEE_FILE"


def _render_qr_ascii(
    payload: str,
    *,
    encoding: str,
    error: str = "m",
    version: Optional[int] = None,
    border: int = 1,
) -> str:
    import segno

    qr = segno.make_qr(payload, encoding=encoding, error=error, version=version)
    rows = [list(row) for row in qr.matrix]
    width = len(rows[0]) if rows else 0
    quiet_row = [False] * (width + 2 * border)
    lines: list[str] = []

    def pair_to_char(top: bool, bottom: bool) -> str:
        if top and bottom:
            return "█"
        if top:
            return "▀"
        if bottom:
            return "▄"
        return " "

    white_bg = "\x1b[47m"
    black_fg = "\x1b[30m"
    reset = "\x1b[0m"

    padded_rows = [quiet_row[:] for _ in range(border)]
    padded_rows.extend((([False] * border) + row + ([False] * border)) for row in rows)
    padded_rows.extend([quiet_row[:] for _ in range(border)])

    if len(padded_rows) % 2:
        padded_rows.append([False] * len(quiet_row))

    for i in range(0, len(padded_rows), 2):
        top = padded_rows[i]
        bottom = padded_rows[i + 1]
        line = "".join(pair_to_char(a, b) for a, b in zip(top, bottom))
        lines.append(f"{white_bg}{black_fg}{line}{reset}")
    return "\n".join(lines)


def render_upn_qr_ascii(upn: UPN) -> str:
    return _render_qr_ascii(upn_to_string(upn), encoding="iso-8859-2", error="m", version=15)


def render_epc_qr_ascii(upn: UPN) -> str:
    epc = upn_to_epc(upn)
    return _render_qr_ascii(epc_to_string(epc), encoding="utf-8", error="m")


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


def ask_optional_iban(prompt: str, *, default: str = "") -> str:
    while True:
        display = f"{prompt} (blank = none) [{default}]: " if default else f"{prompt} (blank = none): "
        raw = input(display).strip()
        if not raw:
            return default
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


def ask_iban(*, default: str = "") -> str:
    display = f"IBAN (required) [{default}]: " if default else "IBAN (required): "
    while True:
        raw = input(display).strip()
        if not raw:
            raw = default
        raw = raw.replace(" ", "").upper()
        if not raw:
            print("  (required)")
            continue
        if len(raw) < 15 or not raw[:2].isalpha() or not raw[2:].isdigit():
            print("  Doesn't look like a valid IBAN.")
            continue
        return raw


def load_party_template_from_file(path: str) -> tuple[str, str, str, str]:
    """Load IBAN/name/street/city from a file path.

    Expected file format is either:
      1. IBAN
      2. name
      3. street
      4. city

    or the legacy format:
      1. name
      2. street
      3. city

    An unreadable or invalid file returns ('', '', '', '').
    """
    if not path:
        return "", "", "", ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n").strip() for line in f.readlines()]
    except OSError:
        return "", "", "", ""

    if len(lines) >= 4:
        iban, name, street, city = lines[:4]
    elif len(lines) >= 3:
        iban = ""
        name, street, city = lines[:3]
    else:
        return "", "", "", ""

    if not name or not street or not city:
        return "", "", "", ""
    return iban[:34], name[:33], street[:33], city[:33]


def load_party_template_from_env(env_var: str) -> tuple[str, str, str, str]:
    """Load IBAN/name/street/city defaults from a file path in the environment."""
    path = os.environ.get(env_var, "").strip() or DEFAULT_PARTY_TEMPLATE
    return load_party_template_from_file(path)


def load_party_defaults_from_env(env_var: str) -> tuple[str, str, str]:
    """Load name/street/city defaults from a file path in the environment."""
    _, name, street, city = load_party_template_from_env(env_var)
    return name, street, city


def load_payer_defaults_from_env() -> tuple[str, str, str]:
    return load_party_defaults_from_env(PAYER_FILE_ENV)


def load_payee_defaults_from_env() -> tuple[str, str, str]:
    return load_party_defaults_from_env(PAYEE_FILE_ENV)


def load_payer_template_from_env() -> tuple[str, str, str, str]:
    return load_party_template_from_env(PAYER_FILE_ENV)


def load_payee_template_from_env() -> tuple[str, str, str, str]:
    return load_party_template_from_env(PAYEE_FILE_ENV)


def _load_font(size: int, *, bold: bool = False) -> Any:
    from PIL import ImageFont

    # Absolute path injected by the Nix wrapper. Pillow on NixOS cannot resolve
    # bare font names, so without this it would fall back to the bitmap default
    # which lacks Slovene diacritics (š/č/ž render as tofu).
    env_path = os.environ.get(
        "QR_MAIL_MONO_FONT_BOLD" if bold else "QR_MAIL_MONO_FONT_REGULAR", ""
    ).strip()
    if env_path:
        try:
            return ImageFont.truetype(env_path, size=size)
        except OSError:
            pass

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


def _epc_monogram(name: str) -> str:
    """Derive a 1–2 character monogram from a beneficiary name (e.g. 'SK')."""
    words = [w for w in name.strip().split() if w]
    if not words:
        return ""
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _fit_font(draw: Any, text: str, max_width: int, start_size: int, *, bold: bool) -> Any:
    """Return the largest mono font (down to size 9) whose text fits max_width."""
    size = start_size
    while size > 9:
        font = _load_font(size, bold=bold)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 1
    return _load_font(9, bold=bold)


def generate_epc_qr_labeled(epc: EPC, *, scale: int = 10) -> bytes:
    """Return PNG bytes of an EPC QR with a recipient/amount caption above it
    and a small monogram of the recipient's initials in the centre.

    The caption sits in a separate strip above the QR so the code's modules are
    never touched. The monogram covers only ~5% of the QR area, well within the
    error-correction budget (ECC M), so scannability is preserved.

    When the EPC carries no beneficiary name, the name line and the monogram are
    both omitted (no placeholder is invented).
    """
    from PIL import Image, ImageDraw

    base = Image.open(io.BytesIO(generate_epc_qr(epc, scale=scale))).convert("RGB")
    width, qr_height = base.size

    name = epc.beneficiary_name.strip()
    amount = f"EUR {epc.amount:.2f}" if epc.amount is not None else ""
    if not name and not amount:
        return generate_epc_qr(epc, scale=scale)

    pad = max(width // 40, 6)
    measure = ImageDraw.Draw(base)
    caption_lines: list[tuple[str, Any]] = []
    if name:
        name_font = _fit_font(measure, name, width - 2 * pad, max(width // 16, 16), bold=True)
        caption_lines.append((name, name_font))
        amount_size = max(int(name_font.size * 0.85), 12)
    else:
        amount_size = max(width // 16, 16)
    if amount:
        caption_lines.append((amount, _load_font(amount_size, bold=True)))

    line_h = max(f.size for _, f in caption_lines) + max(f.size for _, f in caption_lines) // 3
    caption_h = pad + line_h * len(caption_lines) + pad

    canvas = Image.new("RGB", (width, caption_h + qr_height), "white")
    canvas.paste(base, (0, caption_h))
    draw = ImageDraw.Draw(canvas)

    cx = width // 2
    for i, (text, font) in enumerate(caption_lines):
        draw.text((cx, pad + line_h * i + line_h // 2), text, font=font, fill="black", anchor="mm")

    monogram = _epc_monogram(name)
    if monogram:
        box = int(width * 0.22)
        bcx, bcy = width // 2, caption_h + qr_height // 2
        half = box // 2
        draw.rounded_rectangle(
            (bcx - half, bcy - half, bcx + half, bcy + half),
            radius=box // 6, fill="white", outline="#888888", width=max(scale // 5, 1),
        )
        mono_font = _fit_font(draw, monogram, int(box * 0.7), box, bold=True)
        draw.text((bcx, bcy), monogram, font=mono_font, fill="black", anchor="mm")

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def _load_ocr_font(size: int) -> Any:
    from PIL import ImageFont

    if os.path.exists(DEFAULT_OCRB_FONT):
        try:
            return ImageFont.truetype(DEFAULT_OCRB_FONT, size=size)
        except OSError:
            pass

    faces = [
        "OCRB Regular.ttf",
        "OCR B Std.otf",
        "OCR A Extended.ttf",
        "OCRAEXT.TTF",
    ]
    for face in faces:
        try:
            return ImageFont.truetype(face, size=size)
        except OSError:
            continue

    search_roots = [
        "/nix/store/*/share/fonts/truetype",
        "/nix/store/*/share/fonts/opentype",
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.expanduser("~/.fonts"),
    ]
    preferred_faces = [
        "DejaVuSansMono-Bold.ttf",
        "DejaVuSansMono.ttf",
        "LiberationMono-Bold.ttf",
        "LiberationMono-Regular.ttf",
        "DejaVuSans.ttf",
    ]
    for root in search_roots:
        for face in preferred_faces:
            for path in glob.glob(os.path.join(root, "**", face), recursive=True):
                try:
                    return ImageFont.truetype(path, size=size)
                except OSError:
                    continue
    return ImageFont.load_default()


def _format_eur_for_slip(amount_cents: int) -> str:
    if amount_cents < 0:
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
    line_gap: Optional[float] = None,
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
        if line_gap is not None:
            start_y = y1 + 4 + line_gap / 2
            for idx, line in enumerate(lines[:3]):
                if not line:
                    continue
                anchor_y = start_y + idx * line_gap
                if align == "right":
                    draw.text((x2 - 4, anchor_y), line, font=font, fill=fill, anchor="rm")
                else:
                    draw.text((x1 + 4, anchor_y), line, font=font, fill=fill, anchor="lm")
            return

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
    if amount_cents < 0:
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


def _create_legacy_slip_template() -> Any:
    from PIL import Image, ImageDraw

    width, height = 930, 480
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    orange = "#F28C28"
    pale_top = "#FCE6D7"
    pale_bottom = "#FFF2CC"
    line = "#303030"

    draw.rectangle((18, 18, 912, 452), outline=line, width=2)
    draw.line((280, 18, 280, 452), fill=line, width=2)
    draw.line((280, 168, 912, 168), fill=orange, width=2)
    draw.line((280, 374, 912, 374), fill=orange, width=2)
    draw.line((18, 390, 912, 390), fill=line, width=2)

    draw.rectangle((281, 19, 911, 167), fill=pale_top)
    draw.rectangle((281, 169, 911, 373), fill=pale_bottom)

    label = _load_font(13, bold=True)

    def box(coords: tuple[int, int, int, int], text: str = "") -> None:
        draw.rectangle(coords, outline=orange, width=2)
        if text:
            draw.text((coords[0] + 4, coords[1] - 16), text, font=label, fill=orange)

    draw.text((32, 26), "UPN", font=_load_font(26, bold=True), fill=line)
    box((28, 54, 264, 110), "Ime placnika")
    box((28, 132, 264, 198), "Namen / rok placila")
    box((106, 220, 264, 252), "Znesek")
    draw.text((58, 222), "EUR", font=_load_font(20, bold=True), fill=line)
    box((28, 274, 264, 346), "IBAN prejemnika")
    box((28, 366, 264, 396), "Referenca prejemnika")
    box((28, 414, 264, 448), "Ime prejemnika")

    box((325, 28, 654, 58), "IBAN")
    box((325, 76, 654, 106), "Referenca")
    box((325, 124, 735, 186), "Ime in naslov")
    box((764, 28, 812, 58), "Polog")
    box((836, 28, 884, 58), "Dvig")
    box((744, 124, 902, 186), "Podpis placnika")
    box((302, 202, 362, 232), "Koda namena")
    box((374, 202, 748, 232), "Namen / rok placila")
    box((770, 202, 892, 232), "Nujno")
    box((374, 246, 526, 278), "Znesek")
    box((540, 246, 690, 278), "Datum placila")
    box((710, 246, 892, 278), "BIC banke prejemnika")
    box((302, 296, 892, 328), "IBAN")
    box((302, 344, 714, 374), "Referenca")
    box((302, 396, 856, 440), "Ime in naslov")

    draw.text((333, 362), "UPN - legacy OCR", font=_load_font(16, bold=True), fill=line)
    draw.text((331, 404), "Prostor za vpise bank", font=_load_font(12, bold=False), fill=orange)
    draw.text((318, 410), "______________________________________________", font=_load_font(12, bold=False), fill=orange)
    draw.text((314, 408), "Prostor za vpise bank", font=_load_font(12, bold=False), fill=orange)
    draw.text((338, 414), "in opticni zapis podatkov (OCR)", font=_load_font(12, bold=False), fill=orange)
    return img


def _draw_mark(draw: Any, box: tuple[int, int, int, int], *, fill: str = "#1A1A1A") -> None:
    x1, y1, x2, y2 = box
    draw.line((x1 + 4, y1 + 4, x2 - 4, y2 - 4), fill=fill, width=3)
    draw.line((x2 - 4, y1 + 4, x1 + 4, y2 - 4), fill=fill, width=3)


def generate_legacy_upn_slip_png(
    upn: UPN,
    *,
    template_path: Optional[str] = None,
) -> bytes:
    """Render a legacy UPN slip with the OCR payload line at the bottom."""
    from PIL import Image, ImageDraw

    source = template_path or DEFAULT_LEGACY_SLIP_TEMPLATE
    if os.path.exists(source):
        img = Image.open(source).convert("RGB")
    else:
        img = _create_legacy_slip_template()
    draw = ImageDraw.Draw(img)

    font_small = _load_font(11, bold=True)
    font_main = _load_font(14, bold=True)
    font_boxed = _load_font(12, bold=True)
    font_amount = _load_font(16, bold=True)
    font_ocr = _load_ocr_font(23)

    amount_text = _format_eur_for_slip(upn.amount_cents)
    payment_date = upn.payment_date.strftime("%d.%m.%Y") if upn.payment_date else ""
    deadline = upn.payment_deadline.strftime("%d.%m.%Y") if upn.payment_deadline else ""
    payer_full = "\n".join(v for v in [upn.payer_name, upn.payer_street, upn.payer_city] if v)
    recipient_full = "\n".join(v for v in [upn.recipient_name, upn.recipient_street, upn.recipient_city] if v)
    purpose_full = "\n".join(v for v in [upn.payment_purpose, f"Rok placila {deadline}" if deadline else ""] if v)

    legacy_ocr_text = format_legacy_upn_ocr(upn)
    payer_iban_box = upn.payer_iban.replace(" ", "")
    payer_ref_box = _reference_for_boxes(upn.payer_reference)
    recipient_iban_box = upn.recipient_iban.replace(" ", "")
    recipient_ref_box = _reference_for_boxes(upn.recipient_reference)

    _draw_in_box(draw, box=(12, 30, 223, 95), text=payer_full, font=font_small, multiline=True, line_gap=12)
    _draw_in_box(draw, box=(12, 92, 223, 136), text=purpose_full, font=font_small, multiline=True, line_gap=11)
    _draw_in_box(draw, box=(122, 130, 223, 149), text=amount_text, font=font_main)
    _draw_in_box(draw, box=(12, 173, 223, 235), text=upn.recipient_iban, font=font_small, multiline=True)
    _draw_in_box(draw, box=(12, 237, 223, 257), text=upn.recipient_reference, font=font_small)
    _draw_in_box(draw, box=(12, 260, 223, 319), text=recipient_full, font=font_small, multiline=True, line_gap=12)

    _draw_boxed_chars(draw, box=(246, 4, 528, 26), text=payer_iban_box, cells=21, font=font_boxed, align="left")
    _draw_boxed_chars(draw, box=(246, 38, 632, 60), text=payer_ref_box, cells=26, font=font_boxed, align="left")
    _draw_in_box(draw, box=(246, 73, 640, 121), text=payer_full, font=font_small, multiline=True)
    _draw_boxed_chars(draw, box=(246, 129, 502, 151), text=upn.payment_purpose, cells=24, font=font_small, align="left", x_shift=1.0)
    _draw_amount_boxed(draw, box=(296, 160, 456, 183), amount_cents=upn.amount_cents, cells=10, font=font_amount)
    _draw_boxed_chars(draw, box=(470, 160, 590, 183), text=payment_date, cells=10, font=font_boxed, align="left")
    _draw_boxed_chars(draw, box=(246, 191, 753, 212), text=recipient_iban_box, cells=34, font=font_boxed, align="left")
    _draw_boxed_chars(draw, box=(246, 224, 767, 246), text=recipient_ref_box, cells=26, font=font_boxed, align="left")
    _draw_in_box(draw, box=(246, 258, 726, 320), text=recipient_full, font=font_small, multiline=True, line_gap=12)

    if upn.urgent:
        _draw_mark(draw, (546, 8, 563, 27))
    if upn.deposit:
        _draw_mark(draw, (577, 8, 594, 27))
    if upn.withdrawal:
        _draw_mark(draw, (608, 8, 626, 27))

    draw.text((246, 342), legacy_ocr_text, font=font_ocr, fill="#202020")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
        choices=[
            "upn",
            "epc",
            "both",
            "slip",
            "poloznica",
            "legacy_slip",
            "all",
            "upn_cli",
            "epc_cli",
            "legacy_ocr_cli",
        ],
        default="both",
        help="output mode: upn, epc, both, slip/poloznica, legacy_slip, all, upn_cli, epc_cli, legacy_ocr_cli (default: both)",
    )
    parser.add_argument(
        "--slip-template",
        default=DEFAULT_SLIP_TEMPLATE,
        help=f"path to empty UPN slip template image (default: {DEFAULT_SLIP_TEMPLATE})",
    )
    parser.add_argument(
        "--legacy-slip-template",
        default=DEFAULT_LEGACY_SLIP_TEMPLATE,
        help=f"path to empty legacy UPN OCR slip template image (default: {DEFAULT_LEGACY_SLIP_TEMPLATE})",
    )
    args = parser.parse_args()

    print("Generate payment outputs")
    print("─" * 40)

    print("Recipient (prejemnik)")
    payee_iban_default, payee_name_default, payee_street_default, payee_city_default = load_payee_template_from_env()
    iban = ask_iban(default=payee_iban_default)
    name = ask("Recipient name", default=payee_name_default, required=True)
    street = ask("Recipient street", default=payee_street_default)
    city = ask("Recipient city", default=payee_city_default, required=True)
    reference = ask_reference()
    amount_cents = ask_amount()
    payment_date = ask_date("Payment date", default_today=True)
    payment_deadline = ask_date("Payment deadline")
    purpose_code = ask("Purpose code", default="OTHR")
    payment_purpose = ask("Payment purpose / description")

    print()
    print("Payer (placnik) - optional")
    payer_iban_default, payer_name_default, payer_street_default, payer_city_default = load_payer_template_from_env()
    payer_iban = ask_optional_iban("Payer IBAN", default=payer_iban_default)
    payer_reference = ask_reference_optional("Payer reference")
    payer_name = ask("Payer name", default=payer_name_default)
    payer_street = ask("Payer street", default=payer_street_default)
    payer_city = ask("Payer city", default=payer_city_default)
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
    want_legacy_slip = args.format == "legacy_slip"
    want_upn_cli = args.format == "upn_cli"
    want_epc_cli = args.format == "epc_cli"
    want_legacy_ocr_cli = args.format == "legacy_ocr_cli"

    prefix = args.output

    if want_upn:
        upn_path = f"{prefix}_upn.png"
        with open(upn_path, "wb") as f:
            f.write(generate_upn_qr(upn))
        print(f"UPN QR saved: {upn_path}")

    if want_upn_cli:
        print("UPN QR (ASCII):")
        print(render_upn_qr_ascii(upn))

    if want_epc or want_epc_cli:
        try:
            epc = upn_to_epc(upn)
        except Exception as exc:
            print(f"EPC conversion failed: {exc}", file=sys.stderr)
            sys.exit(1)
        if want_epc:
            epc_path = f"{prefix}_epc.png"
            with open(epc_path, "wb") as f:
                f.write(generate_epc_qr(epc))
            print(f"EPC QR saved: {epc_path}")
        if want_epc_cli:
            print("EPC QR (ASCII):")
            print(_render_qr_ascii(epc_to_string(epc), encoding="utf-8", error="m"))

    if want_legacy_ocr_cli:
        try:
            print("Legacy UPN OCR payload:")
            print(format_legacy_upn_ocr(upn))
        except (UPNLegacyOCRError, UPNReferenceError) as exc:
            print(f"Legacy OCR conversion failed: {exc}", file=sys.stderr)
            sys.exit(1)

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

    if want_legacy_slip:
        try:
            legacy_slip_png = generate_legacy_upn_slip_png(
                upn,
                template_path=args.legacy_slip_template,
            )
        except ModuleNotFoundError as exc:
            if exc.name == "PIL":
                print("Legacy slip rendering requires Pillow: pip install pillow", file=sys.stderr)
                sys.exit(1)
            raise
        except (FileNotFoundError, UPNLegacyOCRError, UPNReferenceError) as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        legacy_slip_path = f"{prefix}_poloznica_ocr.png"
        with open(legacy_slip_path, "wb") as f:
            f.write(legacy_slip_png)
        print(f"Legacy UPN OCR slip saved: {legacy_slip_path}")


if __name__ == "__main__":
    main()
