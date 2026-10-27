import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image

from display_previews import DisplayPreviewError, create_product_previews, preview_filename, render_pdf_preview


class DisplayPreviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_pdf(self, name="drawing.pdf", *, blank=False, multipage=False):
        path = self.root / name
        document = fitz.open()
        page = document.new_page(width=1191, height=842)
        if not blank:
            # 用紙端に近い枠と、枠外注記を置く。クロップ後も両方が残ることを確認する。
            page.draw_rect(fitz.Rect(110, 85, 1080, 760), color=(0, 0, 0), width=2)
            page.insert_text((60, 60), "CORNER NOTE", fontsize=10)
        if multipage:
            second = document.new_page(width=1191, height=842)
            second.draw_rect(fitz.Rect(400, 300, 800, 500), color=(1, 0, 0), width=10)
        document.save(path)
        document.close()
        return path

    def test_renders_cropped_png_with_margin_and_preserves_corner_content(self):
        source = self.make_pdf()
        target = self.root / preview_filename(source)

        render_pdf_preview(source, target)

        with Image.open(target) as image:
            self.assertEqual(image.format, "PNG")
            self.assertLess(image.width, 1191 * 150 / 72)
            self.assertLess(image.height, 842 * 150 / 72)
            # すべて白紙ではなく、枠・注記を含む画素がある。
            self.assertGreater(len(image.convert("RGB").getcolors(maxcolors=image.width * image.height) or []), 1)
        self.assertEqual(source.read_bytes()[:5], b"%PDF-")

    def test_blank_pdf_uses_full_page_instead_of_failing_or_cropping_arbitrarily(self):
        source = self.make_pdf("blank.pdf", blank=True)
        target = self.root / preview_filename(source)

        render_pdf_preview(source, target)

        with Image.open(target) as image:
            # PyMuPDFは小数ピクセルを切り上げるため、1191×842ptは2482×1755px。
            self.assertEqual(image.size, (2482, 1755))

    def test_multpage_pdf_preview_is_only_the_first_page(self):
        source = self.make_pdf("document.pdf", multipage=True)
        target = self.root / preview_filename(source)
        render_pdf_preview(source, target)
        with Image.open(target) as image:
            self.assertLess(image.width, 2481)

    def test_existing_target_is_never_overwritten(self):
        source = self.make_pdf()
        target = self.root / preview_filename(source)
        target.write_bytes(b"keep-existing")

        with self.assertRaises(DisplayPreviewError):
            render_pdf_preview(source, target)

        self.assertEqual(target.read_bytes(), b"keep-existing")

    def test_invalid_pdf_leaves_no_preview_and_does_not_expose_path(self):
        source = self.root / "private-name.pdf"
        target = self.root / "private-name__preview.png"
        source.write_bytes(b"not a PDF")

        with self.assertRaises(DisplayPreviewError) as caught:
            render_pdf_preview(source, target)

        self.assertFalse(target.exists())
        self.assertNotIn(str(source), str(caught.exception))
        self.assertNotIn(str(target), str(caught.exception))

    def test_product_previews_cover_pdf_only_pages_and_both_document_pdfs(self):
        page_pdf = self.make_pdf("01.pdf")
        spec_pdf = self.make_pdf("0_S0.pdf")
        check_pdf = self.make_pdf("0_A1.pdf")
        inference_png = self.root / "1.png"
        inference_png.write_bytes(b"untouched-inference-image")
        originals = {p.name: p.read_bytes() for p in (page_pdf, spec_pdf, check_pdf)}

        created = create_product_previews(
            self.root,
            print_pages=["01", "1"],
            inference_pages=["1"],
            page_pdf_name=lambda page: f"{page}.pdf",
            spec_pdf_name="0_S0.pdf",
            check_sheet_pdf_name="0_A1.pdf",
        )

        self.assertEqual(created, ["01__preview.png", "0_S0__preview.png", "0_A1__preview.png"])
        for name in created:
            with Image.open(self.root / name) as image:
                self.assertEqual(image.format, "PNG")
                image.verify()
        self.assertEqual({p.name: p.read_bytes() for p in (page_pdf, spec_pdf, check_pdf)}, originals)
        self.assertEqual(inference_png.read_bytes(), b"untouched-inference-image")
        self.assertFalse((self.root / "1__preview.png").exists())

    def test_check_sheet_is_previewed_even_when_page_zero_is_inference(self):
        self.make_pdf("0_A1.pdf")
        (self.root / "0.png").write_bytes(b"inference-zero")

        created = create_product_previews(
            self.root,
            print_pages=["0"],
            inference_pages=["0"],
            page_pdf_name=lambda page: f"{page}.pdf",
            spec_pdf_name="0_S0.pdf",
            check_sheet_pdf_name="0_A1.pdf",
        )

        self.assertEqual(created, ["0_A1__preview.png"])
        self.assertFalse((self.root / "0__preview.png").exists())
        self.assertEqual((self.root / "0.png").read_bytes(), b"inference-zero")

    def test_product_preview_rejects_source_path_traversal(self):
        outside = self.root.parent / "outside.pdf"
        outside.write_bytes(self.make_pdf().read_bytes())
        try:
            with self.assertRaises(DisplayPreviewError):
                create_product_previews(
                    self.root,
                    print_pages=["../outside"],
                    inference_pages=[],
                    page_pdf_name=lambda page: f"{page}.pdf",
                    spec_pdf_name="0_S0.pdf",
                    check_sheet_pdf_name="0_A1.pdf",
                )
        finally:
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
