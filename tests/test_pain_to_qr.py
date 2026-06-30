import sys
import unittest
from pathlib import Path
from unittest import mock

from scripts.pain_to_qr import main
from tests.test_pain import PAIN_TWO_TX


class TestPainToQr(unittest.TestCase):
    def _run(self, argv: list[str]) -> None:
        with mock.patch.object(sys, "argv", ["pain_to_qr.py", *argv]), mock.patch(
            "scripts.pain_to_qr.generate_epc_qr_labeled", return_value=b"\x89PNG-epc"
        ), mock.patch(
            "scripts.pain_to_qr.generate_upn_slip_png", return_value=b"\x89PNG-slip"
        ):
            main()

    def test_epc_format_writes_one_image_per_transaction(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "batch.xml"
            xml.write_text(PAIN_TWO_TX)
            out = Path(tmp) / "out"
            self._run([str(xml), "--output-dir", str(out), "--format", "epc"])

            images = sorted(p.name for p in out.glob("*.png"))
            self.assertEqual(images, ["batch_01_epc.png", "batch_02_epc.png"])
            self.assertEqual((out / "batch_01_epc.png").read_bytes(), b"\x89PNG-epc")

    def test_both_format_writes_epc_and_slip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "batch.xml"
            xml.write_text(PAIN_TWO_TX)
            out = Path(tmp) / "out"
            self._run([str(xml), "--output-dir", str(out), "--format", "both", "--prefix", "p"])

            images = sorted(p.name for p in out.glob("*.png"))
            self.assertEqual(
                images,
                ["p_01_epc.png", "p_01_slip.png", "p_02_epc.png", "p_02_slip.png"],
            )

    def test_non_pain_xml_exits_1(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "bad.xml"
            xml.write_text("<root/>")
            with self.assertRaises(SystemExit) as ctx:
                self._run([str(xml), "--output-dir", str(Path(tmp) / "out")])
            self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
