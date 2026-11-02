"""表示用プレビューPNGの、内容範囲(図枠・印字)でのクロップ(上下左右で等しい余白)のテスト。合成PDFだけを使う。"""
import hashlib
import tempfile
import unittest
from pathlib import Path

import fitz
import numpy as np
from PIL import Image

import display_previews as dp
from display_previews import create_product_previews, preview_filename, render_pdf_preview

PT_PER_MM = 72 / 25.4
SIZES_MM = {            # (幅, 高さ)。横長・縦長・正方形に近いものを含める
    "A3_landscape": (420, 297),
    "A4_portrait": (210, 297),
    "A2_landscape": (594, 420),
    "A1_landscape": (841, 594),
    "square": (300, 300),
}


def ink_of(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("RGB")).min(axis=2) < dp.INK_THRESHOLD


def margins_of(image: Image.Image):
    ys, xs = np.nonzero(ink_of(image))
    h, w = ink_of(image).shape
    return int(xs.min()), int(ys.min()), int(w - 1 - xs.max()), int(h - 1 - ys.max())


class CropTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def pdf(self, name, size_mm=(420, 297), frame=(0.1, 0.1, 0.9, 0.9), *, draw=None):
        """用紙サイズ size_mm の PDF。frame(用紙に対する割合)に図枠を描き、枠の外側(左下)に用紙名の文字を置く。"""
        w, h = size_mm[0] * PT_PER_MM, size_mm[1] * PT_PER_MM
        path = self.root / name
        document = fitz.open()
        page = document.new_page(width=w, height=h)
        if frame is not None:
            rect = fitz.Rect(w * frame[0], h * frame[1], w * frame[2], h * frame[3])
            page.draw_rect(rect, color=(0, 0, 0), width=1.5)
            page.insert_text((rect.x0 + 20, rect.y1 - 20), "TITLE BLOCK 0123456789", fontsize=12)
            page.insert_text((max(2, rect.x0 - 28), rect.y1 + 12), "A3", fontsize=9)   # 枠の外側の印字
        if draw:
            draw(page, w, h)
        document.save(path)
        document.close()
        return path

    def render(self, source, log=None):
        target = self.root / preview_filename(source)
        if target.exists():
            target.unlink()
        render_pdf_preview(source, target, log=log)
        return target

    def full_page_ink_count(self, source):
        with fitz.open(source) as document:
            image = dp._render_page(document.load_page(0))
        return int(ink_of(image).sum())


class EqualMarginTest(CropTestBase):
    def test_margins_are_equal_on_all_sides_for_several_paper_sizes_and_aspect_ratios(self):
        for label, size in SIZES_MM.items():
            with self.subTest(size=label):
                # 枠を用紙の中で左右非対称に置く(元の余白が不均等なページ)
                source = self.pdf(f"{label}.pdf", size, frame=(0.04, 0.08, 0.93, 0.97))
                with Image.open(self.render(source)) as image:
                    left, top, right, bottom = margins_of(image)
                    self.assertLessEqual(max(left, top, right, bottom) - min(left, top, right, bottom), 2)
                    self.assertGreaterEqual(min(left, top, right, bottom), dp.MARGIN_MIN_PX - 2)
                    self.assertLess(image.width * image.height, (size[0] / 25.4 * 150) * (size[1] / 25.4 * 150))

    def test_no_drawn_content_is_cut_off(self):
        """クロップ後の描画画素数が、用紙全体を描画した場合と同じ(枠・タイトル欄・枠外の印字を1画素も落とさない)。"""
        for label, size in SIZES_MM.items():
            with self.subTest(size=label):
                source = self.pdf(f"{label}.pdf", size, frame=(0.05, 0.1, 0.95, 0.92))
                with Image.open(self.render(source)) as image:
                    self.assertEqual(int(ink_of(image).sum()), self.full_page_ink_count(source))

    def test_thin_pale_lines_are_kept(self):
        def draw(page, w, h):
            page.draw_line((w * 0.03, h * 0.5), (w * 0.97, h * 0.5), color=(0.8, 0.8, 0.8), width=0.25)  # 薄い横線が最も外側
        source = self.pdf("thin.pdf", frame=(0.2, 0.2, 0.8, 0.8), draw=draw)
        with Image.open(self.render(source)) as image:
            left, top, right, bottom = margins_of(image)
            self.assertLessEqual(max(left, top, right, bottom) - min(left, top, right, bottom), 2)
            self.assertEqual(int(ink_of(image).sum()), self.full_page_ink_count(source))
            self.assertGreater(image.width, 0.9 * 420 / 25.4 * 150)         # 薄い線の左右端まで含む

    def test_content_close_to_the_paper_edge_gets_white_padding_and_equal_margins(self):
        source = self.pdf("edge.pdf", frame=(0.0, 0.0, 1.0, 1.0))
        with Image.open(self.render(source)) as image:
            left, top, right, bottom = margins_of(image)
            self.assertLessEqual(max(left, top, right, bottom) - min(left, top, right, bottom), 2)
            self.assertGreaterEqual(image.width, 420 / 25.4 * 150)         # 用紙より小さくならない(切り落とさない)
            self.assertEqual(int(ink_of(image).sum()), self.full_page_ink_count(source))

    def test_each_page_uses_its_own_box(self):
        a = self.pdf("a.pdf", frame=(0.05, 0.05, 0.6, 0.6))
        b = self.pdf("b.pdf", frame=(0.3, 0.3, 0.95, 0.95))
        with Image.open(self.render(a)) as ia, Image.open(self.render(b)) as ib:
            self.assertNotEqual(ia.size, ib.size)      # 枠の大きさが違うので、別々の範囲になる
            for image in (ia, ib):
                left, top, right, bottom = margins_of(image)
                self.assertLessEqual(max(left, top, right, bottom) - min(left, top, right, bottom), 2)


class NoiseAndFallbackTest(CropTestBase):
    def test_isolated_noise_does_not_widen_the_crop(self):
        def noise(page, w, h):
            page.draw_circle((w * 0.01, h * 0.01), 0.4, color=(0, 0, 0), fill=(0, 0, 0))     # 用紙の隅に孤立した点
            page.draw_circle((w * 0.99, h * 0.985), 0.5, color=(0, 0, 0), fill=(0, 0, 0))
        clean = self.pdf("clean.pdf", frame=(0.15, 0.2, 0.85, 0.85))
        noisy = self.pdf("noisy.pdf", frame=(0.15, 0.2, 0.85, 0.85), draw=noise)
        with Image.open(self.render(clean)) as ic, Image.open(self.render(noisy)) as inoisy:
            self.assertLessEqual(abs(ic.width - inoisy.width), 2)
            self.assertLessEqual(abs(ic.height - inoisy.height), 2)
            left, top, right, bottom = margins_of(inoisy)
            self.assertLessEqual(max(left, top, right, bottom) - min(left, top, right, bottom), 2)

    def test_blank_page_falls_back_to_the_whole_page_and_logs_it(self):
        source = self.pdf("blank.pdf", frame=None)
        messages = []
        with Image.open(self.render(source, log=messages.append)) as image:
            with fitz.open(source) as document:
                expected = dp._render_page(document.load_page(0)).size
            self.assertEqual(image.size, expected)
        self.assertEqual(len(messages), 1)
        self.assertIn("白紙", messages[0])
        self.assertIn("ページ全体", messages[0])

    def test_page_without_a_detectable_frame_is_not_over_cropped(self):
        def stamp(page, w, h):
            page.insert_text((w * 0.45, h * 0.5), "SMALL STAMP", fontsize=14)                # 小さな印字だけ
        source = self.pdf("stamp.pdf", frame=None, draw=stamp)
        messages = []
        with Image.open(self.render(source, log=messages.append)) as image:
            with fitz.open(source) as document:
                expected = dp._render_page(document.load_page(0)).size
            self.assertEqual(image.size, expected)
        self.assertIn("検出できません", messages[0])

    def test_crop_is_logged_with_sizes(self):
        messages = []
        self.render(self.pdf("p.pdf"), log=messages.append)
        self.assertEqual(len(messages), 1)
        self.assertIn("クロップ", messages[0])
        self.assertIn("p.pdf", messages[0])
        self.assertNotIn(str(self.root), messages[0])                  # パスは出さない(ファイル名だけ)


class DocumentsAndSafetyTest(CropTestBase):
    def digest(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_spec_check_sheet_and_plain_pages_are_all_cropped_and_sources_inference_images_are_untouched(self):
        spec = self.pdf("0_S0.pdf", (594, 420), frame=(0.03, 0.06, 0.97, 0.95))     # A2 の仕様書
        check = self.pdf("0_A1.pdf", frame=(0.06, 0.1, 0.9, 0.93))
        page = self.pdf("01.pdf", frame=(0.1, 0.12, 0.88, 0.9))
        inference_png = self.root / "1.png"
        inference_png.write_bytes(b"inference-image")
        detected = self.root / "1_detected.png"
        detected.write_bytes(b"detected-image")
        csv_file = self.root / "estcode_df.csv"
        csv_file.write_bytes(b"a,b\n1,2\n")
        before = {p.name: self.digest(p) for p in (spec, check, page, inference_png, detected, csv_file)}
        messages = []

        created = create_product_previews(
            self.root, print_pages=["01", "1"], inference_pages=["1"],
            page_pdf_name=lambda p: f"{p}.pdf", spec_pdf_name="0_S0.pdf", check_sheet_pdf_name="0_A1.pdf",
            log=messages.append)

        self.assertEqual(created, ["01__preview.png", "0_S0__preview.png", "0_A1__preview.png"])
        self.assertEqual(len(messages), 3)
        self.assertTrue(all("クロップ" in m for m in messages))
        for name in created:
            with Image.open(self.root / name) as image:
                left, top, right, bottom = margins_of(image)
                self.assertLessEqual(max(left, top, right, bottom) - min(left, top, right, bottom), 2, name)
        after = {p.name: self.digest(p) for p in (spec, check, page, inference_png, detected, csv_file)}
        self.assertEqual(after, before)
        self.assertFalse((self.root / "1__preview.png").exists())      # 推論対象のページはプレビューを作らない

    def test_existing_preview_is_not_overwritten(self):
        source = self.pdf("x.pdf")
        target = self.root / "x__preview.png"
        target.write_bytes(b"keep")
        with self.assertRaises(dp.DisplayPreviewError):
            render_pdf_preview(source, target)
        self.assertEqual(target.read_bytes(), b"keep")

    def test_output_is_a_valid_png_in_rgb_and_keeps_colour(self):
        def red(page, w, h):
            page.draw_rect(fitz.Rect(w * 0.3, h * 0.3, w * 0.4, h * 0.4), color=(1, 0, 0), fill=(1, 0, 0))
        source = self.pdf("c.pdf", draw=red)
        with Image.open(self.render(source)) as image:
            image.verify()
        with Image.open(self.root / "c__preview.png") as image:
            self.assertEqual(image.format, "PNG")
            array = np.asarray(image.convert("RGB"))
            self.assertTrue(((array[:, :, 0] > 200) & (array[:, :, 1] < 60)).any())


if __name__ == "__main__":
    unittest.main()
