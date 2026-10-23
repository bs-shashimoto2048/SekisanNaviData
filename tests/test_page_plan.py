# pipeline.page_plan のテスト。DataFrame と一時フォルダの空ファイルだけを使い、CAD・外部処理には触れない。
# 印刷対象の条件: design.csv の「ページ番号」が 6 文字以内で、先頭が X/x でない(DWG との照合は大文字小文字を区別せず、先頭ゼロは保持)。
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import page_plan  # noqa: E402


def _df(*pages):
    return pd.DataFrame({"PAGE": list(pages), "BAN_MENNO": range(1, len(pages) + 1)})


ALL_IDS = ["21", "22", "24", "3A", "X1", "x2", "X9", "x3", "6"]   # design.csv の「ページ番号」(既定)


class PagePlanTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="page_plan_")
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)

    def _touch(self, *names):
        for n in names:
            (self.dir / n).write_bytes(b"x")

    def plan(self, df, ids=None):
        return page_plan.build_page_plan(df, str(self.dir), ".dwg", ALL_IDS if ids is None else ids)

    def test_normal_case_splits_inference_and_pdf_only(self):
        self._touch("21.dwg", "22.dwg", "24.DWG", "3A.dwg", "X1.dwg", "x2.dwg")
        p = self.plan(_df("21", "22", "22"))
        self.assertEqual(p.inference_pages, ["21", "22"])
        self.assertEqual(p.print_pages, ["21", "22", "24", "3A"])   # 条件を満たす全ページ、文字列の昇順
        self.assertEqual(p.pdf_only_pages, ["24", "3A"])
        self.assertEqual(p.missing_pages, [])
        self.assertEqual(p.excluded_pages, ["X1", "x2"])
        self.assertEqual(p.status, page_plan.OK)

    def test_x_pages_are_dropped_from_inference_and_product_df(self):
        self._touch("21.dwg", "X9.dwg")
        p = self.plan(_df("21", "X9", "x3"))
        self.assertEqual(p.inference_pages, ["21"])
        self.assertEqual(list(p.product_df["PAGE"]), ["21"])
        self.assertEqual(p.print_pages, ["21"])

    def test_integer_pages_are_compared_as_strings(self):
        self._touch("6.dwg", "21.dwg")
        p = self.plan(pd.DataFrame({"PAGE": [21, 6, 6]}))   # 整数列でも文字列として扱い、文字列の昇順(従来と同じ)
        self.assertEqual(p.inference_pages, ["21", "6"])
        self.assertEqual(p.print_pages, ["21", "6"])
        self.assertEqual(p.status, page_plan.OK)

    def test_missing_drawing_is_failure(self):
        self._touch("21.dwg")
        p = self.plan(_df("21", "22"))
        self.assertEqual(p.missing_pages, ["22"])
        self.assertEqual(p.status, page_plan.MISSING_DRAWINGS)

    def test_no_inference_pages_is_skip(self):
        self._touch("24.dwg")
        p = self.plan(_df("X1"))
        self.assertEqual(p.inference_pages, [])
        self.assertEqual(p.status, page_plan.NO_INFERENCE_PAGES)
        p = self.plan(pd.DataFrame({"PAGE": [None]}))
        self.assertEqual(p.status, page_plan.NO_INFERENCE_PAGES)

    def test_zero_print_targets(self):
        p = self.plan(_df("X1"))
        self.assertEqual(p.print_pages, [])
        self.assertEqual(p.pdf_only_pages, [])

    def test_input_dataframe_is_not_modified(self):
        self._touch("21.dwg")
        df = _df("21", "X1")
        self.plan(df)
        self.assertEqual(list(df["PAGE"]), ["21", "X1"])

    # ---- 印刷対象の条件(design.csv の「ページ番号」) ----
    def test_page_ids_longer_than_six_characters_are_neither_printed_nor_inferred(self):
        self._touch("21.dwg", "ABCDEFG.dwg", "ABCDEF.dwg")
        ids = ["21", "ABCDEFG", "ABCDEF"]
        p = self.plan(_df("21", "ABCDEFG", "ABCDEF"), ids)
        self.assertEqual(p.print_pages, ["21", "ABCDEF"])           # ちょうど 6 文字は対象、7 文字は対象外
        self.assertEqual(p.inference_pages, ["21", "ABCDEF"])
        self.assertEqual(list(p.product_df["PAGE"]), ["21", "ABCDEF"])  # 座標計算にも残さない
        self.assertEqual(p.excluded_pages, ["ABCDEFG"])

    def test_drawing_not_listed_in_design_csv_is_not_printed_or_inferred(self):
        self._touch("21.dwg", "22.dwg")
        p = self.plan(_df("21", "22"), ["21"])
        self.assertEqual(p.print_pages, ["21"])
        self.assertEqual(p.inference_pages, ["21"])
        self.assertEqual(list(p.product_df["PAGE"]), ["21"])
        self.assertEqual(p.excluded_pages, ["22"])
        self.assertEqual(p.status, page_plan.OK)

    def test_matching_ignores_case_and_keeps_the_drawing_file_spelling(self):
        self._touch("3a.dwg", "Ab1.DWG")
        p = self.plan(_df("3A", "AB1"), ["3A", "aB1"])
        self.assertEqual(p.print_pages, ["3a", "Ab1"])             # 図面ファイルの綴りのまま(出力 PDF 名もこの綴り)
        self.assertEqual(p.inference_pages, ["3A", "AB1"])
        self.assertEqual(p.missing_pages, [])                       # 綴りの大文字小文字が違うだけなら「図面が無い」とは扱わない
        self.assertEqual(p.pdf_only_pages, [])                      # 推論対象でもある(PDFだけのページではない)

    def test_leading_zeros_are_preserved_not_normalized(self):
        self._touch("01.dwg", "1.dwg")
        p = self.plan(_df("01", "1"), ["01"])                       # design.csv には 01 だけ
        self.assertEqual(p.print_pages, ["01"])
        self.assertEqual(p.inference_pages, ["01"])
        self.assertEqual(p.excluded_pages, ["1"])

    def test_design_page_ids_are_required_so_an_unreadable_design_csv_cannot_mean_print_everything(self):
        self._touch("21.dwg")
        with self.assertRaises(TypeError):
            page_plan.build_page_plan(_df("21"), str(self.dir), ".dwg")      # type: ignore[call-arg]
        p = self.plan(_df("21"), [])                                       # 空の一覧 = 何も印刷しない(全部印刷にはならない)
        self.assertEqual((p.print_pages, p.inference_pages), ([], []))
        self.assertEqual(p.status, page_plan.NO_INFERENCE_PAGES)


if __name__ == "__main__":
    unittest.main()
