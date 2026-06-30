#!/usr/bin/env python3
"""Generate one QR image per transaction from an ISO 20022 pain.001 file.

A pain.001 (Customer Credit Transfer Initiation) batch holds many credit
transfers; this turns each into a scannable EPC SCT QR code (and optionally a
full UPN payment slip), writing one image per transaction to an output
directory.

Usage:
  python3 scripts/pain_to_qr.py batch.xml
  python3 scripts/pain_to_qr.py batch.xml --output-dir out --format both
  python3 scripts/pain_to_qr.py batch.xml --format slip --scale 8

Exit codes:
  0  at least one image written
  1  parse error or no images produced
  2  usage error
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.generate import upn_to_epc
from core.upn import UPN
from parsers.pain import PainParseError, parse_pain_credit_transfers
from scripts.generate_qr import generate_epc_qr_labeled, generate_upn_slip_png


def _summarize(index: int, upn: UPN) -> str:
    ref = upn.recipient_reference or "(no ref)"
    name = upn.recipient_name or "(no name)"
    return f"#{index}: {name} — {upn.recipient_iban} — EUR {upn.amount:.2f} — {ref}"


def _write_images(
    upn: UPN,
    *,
    stem: str,
    index: int,
    out_dir: Path,
    fmt: str,
    scale: int,
    slip_template: str | None,
) -> list[Path]:
    """Write the requested image(s) for one transaction; return written paths."""
    written: list[Path] = []
    base = out_dir / f"{stem}_{index:02d}"

    if fmt in ("epc", "both"):
        png = generate_epc_qr_labeled(upn_to_epc(upn), scale=scale)
        path = base.with_name(f"{base.name}_epc.png")
        path.write_bytes(png)
        written.append(path)

    if fmt in ("slip", "both"):
        png = generate_upn_slip_png(upn, template_path=slip_template)
        path = base.with_name(f"{base.name}_slip.png")
        path.write_bytes(png)
        written.append(path)

    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate one QR image per transaction from a pain.001 XML file.",
    )
    parser.add_argument("xml", help="Path to the pain.001 XML file")
    parser.add_argument(
        "--output-dir", "-o", default=".",
        help="Directory for the generated images (default: current directory).",
    )
    parser.add_argument(
        "--format", "-f", choices=("epc", "slip", "both"), default="epc",
        help="epc = EPC SCT QR (default); slip = full UPN slip; both.",
    )
    parser.add_argument(
        "--scale", type=int, default=10,
        help="QR pixel scale for the EPC image (default: 10).",
    )
    parser.add_argument(
        "--prefix", default="",
        help="Output filename prefix (default: the XML file's stem).",
    )
    parser.add_argument(
        "--city", default="Ljubljana",
        help="Recipient city to use when the XML omits one (default: Ljubljana).",
    )
    parser.add_argument(
        "--slip-template", default=None,
        help="UPN slip template image (default: bundled upn_base_empty.jpg).",
    )
    args = parser.parse_args()

    try:
        xml_bytes = Path(args.xml).read_bytes()
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        upns = parse_pain_credit_transfers(xml_bytes)
    except PainParseError as exc:
        print(f"ERROR: not a usable pain.001 file: {exc}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.prefix or Path(args.xml).stem

    total = 0
    for index, upn in enumerate(upns, start=1):
        if not upn.recipient_city:
            upn = replace(upn, recipient_city=args.city)
        print(_summarize(index, upn), file=sys.stderr)
        try:
            for path in _write_images(
                upn,
                stem=stem,
                index=index,
                out_dir=out_dir,
                fmt=args.format,
                scale=args.scale,
                slip_template=args.slip_template,
            ):
                print(path)
                total += 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: failed to render transaction #{index}: {exc}", file=sys.stderr)

    if total == 0:
        print("ERROR: no images produced", file=sys.stderr)
        sys.exit(1)
    print(f"Wrote {total} image(s) for {len(upns)} transaction(s) to {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
