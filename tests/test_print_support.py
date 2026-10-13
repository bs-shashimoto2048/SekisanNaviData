# 印刷対象の選別(cad_data_process)と印刷スクリプト(print_support)の単体テスト。IJCAD は起動しない。
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cad_data_process as cad  # noqa: E402
import print_support  # noqa: E402


class ExcludedPageTest(unittest.TestCase):
    def test_x_and_lowercase_x_prefix_are_excluded(self):
        for name in ("X1", "x1", "XA", "xa", "X", "x", "X1.dwg", "x2.DWG", "Xtra"):
            self.assertTrue(cad.is_excluded_page(name), name)

    def test_others_are_not_excluded(self):
        for name in ("1", "21", "A1", "0", "AX", "a_x", "1X", "3A.dwg", "21.dwg"):
            self.assertFalse(cad.is_excluded_page(name), name)

    def test_only_the_leading_character_matters(self):
        self.assertFalse(cad.is_excluded_page("AX1"))
        self.assertFalse(cad.is_excluded_page("1x"))


class ListPrintPagesTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="print_pages_")
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)

    def touch(self, *names):
        for n in names:
            (self.dir / n).write_bytes(b"dummy")

    def test_all_pages_except_x_in_string_order(self):
        self.touch("3A.dwg", "21.dwg", "100.DWG", "22.dwg", "X1.dwg", "x2.dwg", "Xtra.DWG", "0.dwg")
        self.assertEqual(cad.list_print_pages(str(self.dir), ".dwg"), ["0", "100", "21", "22", "3A"])  # 文字列の昇順(従来のページ並び)

    def test_non_dwg_and_subdirectories_are_ignored(self):
        self.touch("21.dwg", "21.baninf", "22.bak", "readme.txt")
        (self.dir / "sub.dwg").mkdir()
        (self.dir / "dwg").mkdir()
        self.assertEqual(cad.list_print_pages(str(self.dir), ".dwg"), ["21"])

    def test_zero_targets(self):
        self.assertEqual(cad.list_print_pages(str(self.dir), ".dwg"), [])
        self.touch("X1.dwg", "x2.DWG")
        self.assertEqual(cad.list_print_pages(str(self.dir), ".dwg"), [])


class PrintScriptTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="print_script_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)
        self.design = self.tmp / "design"
        self.out = self.tmp / "out"
        self.design.mkdir()
        self.out.mkdir()
        for p in ("21", "22", "3A"):
            (self.design / f"{p}.dwg").write_bytes(b"dummy")

    def script(self, pages):
        path = print_support.output_print_script(pages, str(self.design), str(self.out))
        return Path(path), Path(path).read_bytes().decode("shift-jis").replace("\r\n", "\n").split("\n")

    def test_offset_is_centered_not_fixed(self):
        path, lines = self.script(["21"])
        self.assertEqual(path.name, "WIRING.SCR")
        self.assertIn("C", lines)
        self.assertNotIn("5,5", lines)
        i = lines.index("-PLOT")
        # 対話入力の順序(印刷範囲 L・尺度 F の次が印刷オフセット)は従来どおり
        self.assertEqual(lines[i + 1:i + 14], ["Y", "Model", "DWG To PDF", "ISO A3 (420.00 x 297.00 MM)", "M", "L", "N", "L", "F", "C",
                                               "Y", "thickness.ctb", "Y"])

    def test_close_y_is_kept_per_drawing_and_quit_y_at_the_end(self):
        _, lines = self.script(["21", "22", "3A"])
        self.assertEqual(lines.count("close y"), 3)
        self.assertEqual(lines.count("-PLOT"), 3)
        self.assertEqual([l for l in lines if re.fullmatch(r"close\b.*", l, re.I)], ["close y"] * 3)  # close n・引数なしの close は無い
        self.assertEqual(lines[-2], "quit y")

    def test_order_and_output_names_follow_page_list(self):
        _, lines = self.script(["3A", "21", "22"])
        opens = [l for l in lines if l.startswith("open ")]
        self.assertEqual([Path(o[5:]).name for o in opens], ["3A.dwg", "21.dwg", "22.dwg"])
        pdfs = [l for l in lines if l.lower().endswith(".pdf")]
        self.assertEqual([Path(p).name for p in pdfs], ["3A.pdf", "21.pdf", "22.pdf"])
        self.assertTrue(all(Path(p).parent == self.out for p in pdfs))

    def test_scale_and_area_are_unchanged_so_frame_size_in_pdf_does_not_change(self):
        _, lines = self.script(["21"])
        i = lines.index("-PLOT")
        self.assertEqual(lines[i + 4], "ISO A3 (420.00 x 297.00 MM)")  # 用紙
        self.assertEqual((lines[i + 8], lines[i + 9]), ("L", "F"))     # 印刷範囲 Limits / 尺度 Fit

    def test_missing_drawing_raises_instead_of_exiting(self):
        with self.assertRaises(FileNotFoundError):
            print_support.output_print_script(["21", "99"], str(self.design), str(self.out))

    def test_script_is_shift_jis_text(self):
        (self.design / "図1.dwg").write_bytes(b"dummy")
        path, lines = self.script(["図1"])
        self.assertTrue(any("図1.dwg" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
