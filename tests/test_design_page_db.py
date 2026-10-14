# design.csv(設計製番フォルダのページ番号 .db から作る、製番単位の表)のテスト。値はすべて架空の合成データ(実データは使わない)。
import csv
import io
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import importlib  # noqa: E402
from test_main_integration import PRODUCT, MainIntegrationBase, page_db_lines, write_page_db  # noqa: E402

# cad_data 配下は窓口(cad_data_process)経由が原則のため、単体テストは importlib で直接読む(構造テストの直 import 禁止ルールに合わせる)
dp = importlib.import_module("cad_data.design_page_db")

EXPECTED_HEADER = ["blank", "整理番号", "製番", "ページ番号", "blank", "図名", "図面尺度１", "図面尺度２",
                   "承認設計", "年", "月", "日", "承認照査", "年", "月", "日",
                   "製作設計", "年", "月", "日", "製作照査", "年", "月", "日"]


def read_raw(path):
    return list(csv.reader(io.StringIO(Path(path).read_bytes().decode("cp932"), newline="")))


class DbBase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="design_csv_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name).resolve()
        self.design = self.tmp / "design"
        self.out = self.tmp / "out"
        self.design.mkdir()
        self.out.mkdir()

    def db(self, name, page=None, **override):
        """override は {"l6": "値"} のように 1 始まりの行番号で上書きする。"""
        lines = page_db_lines(page if page is not None else Path(name).stem)
        for key, val in override.items():
            lines[int(key.lstrip("l")) - 1] = val
        write_page_db(self.design, name, lines)


class FormatTest(DbBase):
    def test_header_is_the_agreed_24_columns_with_blank_and_duplicated_date_names(self):
        self.assertEqual(dp.DESIGN_COLUMNS, EXPECTED_HEADER)
        self.assertEqual(len(dp.DESIGN_COLUMNS), 24)
        self.assertEqual(dp.DESIGN_COLUMNS.count("年"), 4)
        self.assertEqual(dp.DESIGN_COLUMNS.count("blank"), 2)

    def test_each_db_becomes_one_row_in_line_order(self):
        self.db("1.db")
        path = dp.export_design_csv(self.design, self.out)
        rows = read_raw(path)
        self.assertEqual(Path(path), self.out / "design.csv")
        self.assertEqual(rows[0], EXPECTED_HEADER)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1], page_db_lines("1"))                  # 1〜24 行目がそのまま 24 列
        self.assertEqual(rows[1][1:4], ["S001", PRODUCT, "1"])
        self.assertTrue(all(len(r) == 24 for r in rows))

    def test_empty_values_and_blank_columns_are_kept(self):
        self.db("2.db", l1="", l5="", l8="")
        row = read_raw(dp.export_design_csv(self.design, self.out))[1]
        self.assertEqual((row[0], row[4], row[7]), ("", "", ""))
        self.assertEqual(len(row), 24)

    def test_values_are_not_altered_except_line_breaks(self):
        self.db("3.db", l6=' 図 名 ,"x" ', l7="１：２")               # 前後空白・カンマ・引用符・全角はそのまま
        row = read_raw(dp.export_design_csv(self.design, self.out))[1]
        self.assertEqual(row[5], ' 図 名 ,"x" ')
        self.assertEqual(row[6], "１：２")

    def test_line_breaks_cr_lf_crlf_are_removed_and_lines_beyond_24_are_ignored(self):
        lines = page_db_lines("4")
        lines[0], lines[4] = "p1", "p5"                                 # 空行を挟むと CR+LF が 1 つの改行になるため、空でない値にする
        seps = ["\r", "\n", "\r\n"]
        raw = "".join(v + seps[i % 3] for i, v in enumerate(lines)) + "extra25\r\nextra26"
        (self.design / "4.db").write_bytes(raw.encode("cp932"))
        row = read_raw(dp.export_design_csv(self.design, self.out))[1]
        self.assertEqual(row, lines)
        self.assertNotIn(b"extra25", (self.out / "design.csv").read_bytes())

    def test_cp932_without_bom_and_crlf_rows(self):
        self.db("5.db")
        raw = Path(dp.export_design_csv(self.design, self.out)).read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        raw.decode("cp932")
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")
        self.assertTrue(raw.endswith(b"\r\n"))
        self.assertEqual(raw.count(b"\r\n"), 2)


class OrderTest(DbBase):
    ORDER = ["0", "1", "2", "3", "01", "02", "03", "9", "10", "301", "10A", "9A"]

    def pages(self):
        return [r[3] for r in read_raw(dp.export_design_csv(self.design, self.out))[1:]]

    def test_priority_order_0_1_2_3_01_02_03_then_the_rest_deterministically(self):
        for name, page in [("103", "03"), ("301", "301"), ("102", "2"), ("110", "10"), ("101", "01"), ("100", "0"), ("113", "3"),
                           ("112", "02"), ("111", "1"), ("109", "9"), ("120", "9A"), ("121", "10A")]:
            self.db(f"{name}.db", page=page)                         # ファイル名は数字、順序の基準は 4 行目のページ番号
        self.assertEqual(self.pages(), self.ORDER)
        self.assertEqual(self.pages(), self.ORDER)                      # 何度でも同じ並び

    def test_order_is_based_on_the_page_value_in_the_db_not_the_file_name(self):
        self.db("11.db", page="3")      # ファイル名は 11 だが 4 行目のページ番号は 3
        self.db("12.db", page="1")
        self.db("13.db", page="2")
        self.assertEqual(self.pages(), ["1", "2", "3"])

    def test_same_page_value_is_ordered_by_file_name(self):
        self.db("22.db", page="5", l6="B")
        self.db("21.db", page="5", l6="A")
        rows = read_raw(dp.export_design_csv(self.design, self.out))[1:]
        self.assertEqual([r[5] for r in rows], ["A", "B"])              # ファイル名 21 → 22 の順

    def test_all_dbs_are_aggregated(self):
        for page in ["7", "8", "9", "10", "11"]:
            self.db(f"{page}.db")
        self.assertEqual(len(self.pages()), 5)

    def test_extension_case_is_ignored_and_non_page_dbs_are_not_read(self):
        self.db("1.DB")
        write_page_db(self.design, "Thumbs.db", ["not a page db"])      # ページ番号ではない .db は読まない(失敗にもしない)
        self.assertEqual(self.pages(), ["1"])


class FailureTest(DbBase):
    def assert_fails_without_output(self):
        with self.assertLogs("cad_data.design_page_db", level="WARNING"):
            with self.assertRaises(dp.DesignDataError) as ctx:
                dp.export_design_csv(self.design, self.out)
        self.assertEqual(list(self.out.iterdir()), [])                   # 部分的な design.csv も一時ファイルも残さない
        return str(ctx.exception)

    def test_no_db_fails(self):
        self.assertIn("ページ番号の .db がありません", self.assert_fails_without_output())

    def test_missing_folder_fails(self):
        with self.assertLogs("cad_data.design_page_db", level="WARNING"):
            with self.assertRaises(dp.DesignDataError):
                dp.export_design_csv(self.design / "missing", self.out)

    def test_short_db_fails_and_no_partial_output_even_if_other_dbs_are_good(self):
        self.db("1.db")
        write_page_db(self.design, "2.db", page_db_lines("2")[:23])      # 23 行(必要行が欠けている)
        reason = self.assert_fails_without_output()
        self.assertIn("2.db", reason)
        self.assertIn("行が足りません", reason)

    def test_non_cp932_db_fails(self):
        self.db("1.db")
        (self.design / "2.db").write_bytes(b"\x81" * 30)                 # 不正な CP932(2 バイト文字が完結しない)
        self.assertIn("2.db", self.assert_fails_without_output())

    def test_value_not_encodable_in_cp932_fails_without_partial_output(self):
        # CP932 に無い文字は書き出しで失敗する(.db は CP932 で読むので通常は起きない。書き出し側を直接確認)
        real = dp.build_design_rows
        dp.build_design_rows = lambda d, log=None: [["☃"] * 24]
        try:
            with self.assertLogs("cad_data.design_page_db", level="ERROR"):
                with self.assertRaises(dp.DesignDataError):
                    dp.export_design_csv(self.design, self.out)
        finally:
            dp.build_design_rows = real
        self.assertEqual(list(self.out.iterdir()), [])

    def test_write_failure_leaves_no_partial_file_or_temp(self):
        self.db("1.db")
        real = os.replace

        def boom(*a, **k):
            raise OSError("disk full (simulated)")

        os.replace = boom
        try:
            with self.assertLogs("cad_data.design_page_db", level="ERROR"):
                with self.assertRaises(dp.DesignDataError):
                    dp.export_design_csv(self.design, self.out)
        finally:
            os.replace = real
        self.assertEqual(list(self.out.iterdir()), [])

    def test_existing_design_csv_is_replaced_atomically_on_success(self):
        (self.out / "design.csv").write_bytes(b"old")
        self.db("1.db")
        dp.export_design_csv(self.design, self.out)
        self.assertEqual(len(read_raw(self.out / "design.csv")), 2)
        self.assertEqual([p.name for p in self.out.iterdir()], ["design.csv"])

    def test_original_dbs_are_untouched(self):
        self.db("1.db")
        self.db("2.db")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.design.iterdir()}
        dp.export_design_csv(self.design, self.out)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.design.iterdir()}, before)

    def test_uses_given_logger(self):
        stream = io.StringIO()
        lg = logging.getLogger("design_test_logger")
        lg.addHandler(logging.StreamHandler(stream))
        lg.setLevel(logging.INFO)
        with self.assertRaises(dp.DesignDataError):
            dp.export_design_csv(self.design, self.out, log=lg)
        self.assertIn("design.csv を作れません", stream.getvalue())


class PipelineWithDbTest(MainIntegrationBase):
    """main() の擬似環境で、design.csv が製番フォルダに出て、material.csv と共に後続の工程へ進むこと。"""

    def log_text(self):
        return next((self.product_out.parent.parent / "logs").glob("*.log")).read_text(encoding="utf-8")

    def test_design_csv_is_written_before_later_steps_and_run_completes(self):
        self.run_main()
        self.assertEqual(self.results["failed"], {})
        rows = read_raw(self.product_out / "design.csv")
        self.assertEqual(rows[0], EXPECTED_HEADER)
        self.assertEqual(len(rows) - 1, len(list(self.design_product.glob("*.db"))))
        self.assertTrue((self.product_out / "material.csv").exists())
        log = self.log_text()
        self.assertLess(log.index("CSV作成: 設計"), log.index("CSV作成: 材料"))
        self.assertLess(log.index("CSV作成: 材料"), log.index("[Step.2]"))     # 印刷・推論より前

    def test_original_dbs_are_untouched(self):
        before = {p: p.read_bytes() for p in self.design_product.glob("*.db")}
        self.run_main()
        self.assertEqual({p: p.read_bytes() for p in self.design_product.glob("*.db")}, before)


if __name__ == "__main__":
    unittest.main()
