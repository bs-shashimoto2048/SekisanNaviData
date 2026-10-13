# pipeline.page_plan のテスト。DataFrame と一時フォルダの空ファイルだけを使い、CAD・外部処理には触れない。
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


class PagePlanTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="page_plan_")
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)

    def _touch(self, *names):
        for n in names:
            (self.dir / n).write_bytes(b"x")

    def plan(self, df):
        return page_plan.build_page_plan(df, str(self.dir), ".dwg")

    def test_normal_case_splits_inference_and_pdf_only(self):
        self._touch("21.dwg", "22.dwg", "24.DWG", "3A.dwg", "X1.dwg", "x2.dwg")
        p = self.plan(_df("21", "22", "22"))
        self.assertEqual(p.inference_pages, ["21", "22"])
        self.assertEqual(p.print_pages, ["21", "22", "24", "3A"])   # X/x 始まりを除く全ページ、文字列の昇順
        self.assertEqual(p.pdf_only_pages, ["24", "3A"])
        self.assertEqual(p.missing_pages, [])
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


if __name__ == "__main__":
    unittest.main()
