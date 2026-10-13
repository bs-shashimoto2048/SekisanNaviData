# material.csv(設計データの *.boxspec から作る材料一覧)のテスト。値はすべて架空の合成データ(実データ・実材料値は使わない)。
# 外部アクセスなし(一時ディレクトリのみ。統合テストは test_main_integration と同じ擬似環境)。
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
from test_main_integration import PRODUCT, MainIntegrationBase  # noqa: E402

# 実装モジュールの内部(解析・書き出し)を単体で試すためのテスト。呼び出し側のコードは窓口 cad_data_process 経由(test_cad_data_split の規則)
bm = importlib.import_module("cad_data.boxspec_material")

STEEL_ALL = ("シンボル名$BSP-STEELA-H|STEEL-BASE$BASE-X|STEEL-SIDE$SIDE-X|STEEL-ROOF$ROOF-X|STEEL-PANEL$PANEL-X|"
             "STEEL-DIVIDE$DIV-X|STEEL-FRAME$FRAME-X|STEEL-BOTTOM$BOTTOM-X|STEEL-INSIDEPANEL$INSIDE-X|STEEL-DOOR$DOOR-X|")


def write_boxspec(folder: Path, name: str, lines, encoding="cp932"):
    (folder / name).write_bytes(("\r\n".join(lines) + "\r\n").encode(encoding))


def read_rows(path):
    with open(path, encoding="cp932", newline="") as f:
        return list(csv.DictReader(f))


class BoxspecBase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="boxspec_")
        self.addCleanup(td.cleanup)
        self.design = Path(td.name) / "design"
        self.out = Path(td.name) / "out"
        self.design.mkdir()
        self.out.mkdir()


class ParseTest(BoxspecBase):
    def test_one_row_per_known_component_with_japanese_names(self):
        write_boxspec(self.design, "301.boxspec", ["シンボル名$BSP-CABTYPEA|CABTYPE$ZZ1|", STEEL_ALL])
        rows = bm.parse_boxspec(self.design / "301.boxspec")
        self.assertEqual([r["COMPONENT_CODE"] for r in rows], [c for c, _ in bm.COMPONENTS])
        self.assertEqual([r["COMPONENT_NAME"] for r in rows],
                         ["基礎ベース", "側板", "屋根・天井板", "パネル（背面板）", "仕切り板", "組枠", "底板", "補助・内部パネル", "扉板"])
        self.assertEqual({r["PAGE"] for r in rows}, {"301"})
        self.assertEqual({r["SOURCE_FILE"] for r in rows}, {"301.boxspec"})
        self.assertEqual({r["SYMBOL_NAME"] for r in rows}, {"BSP-STEELA-H"})
        self.assertEqual({r["CABTYPE"] for r in rows}, {"ZZ1"})
        self.assertEqual(rows[0]["MATERIAL_SPEC"], "BASE-X")     # 値は加工しない
        self.assertEqual(rows[8]["MATERIAL_SPEC"], "DOOR-X")

    def test_values_are_kept_as_is_including_symbols_and_dollar(self):
        write_boxspec(self.design, "1.boxspec", ["シンボル名$BSP-STEELA-H|STEEL-BASE$〓TEST-AA 10x20|STEEL-SIDE$t=9.9 $TESTMAT|"])
        rows = {r["COMPONENT_CODE"]: r["MATERIAL_SPEC"] for r in bm.parse_boxspec(self.design / "1.boxspec")}
        self.assertEqual(rows["STEEL-BASE"], "〓TEST-AA 10x20")
        self.assertEqual(rows["STEEL-SIDE"], "t=9.9 $TESTMAT")     # 最初の $ だけで分ける

    def test_nothing_is_kept_and_distinguished_from_not_described(self):
        write_boxspec(self.design, "2.boxspec", ["シンボル名$BSP-STEELA-H|STEEL-BASE$無し|STEEL-SIDE$|STEEL-ROOF$ROOF-X|"])
        spec = {r["COMPONENT_CODE"]: r["MATERIAL_SPEC"] for r in bm.parse_boxspec(self.design / "2.boxspec")}
        self.assertEqual(spec["STEEL-BASE"], "無し")             # 明示値
        self.assertEqual(spec["STEEL-SIDE"], "未記載")           # キーはあるが値が空
        self.assertEqual(spec["STEEL-PANEL"], "未記載")          # キー自体が無い
        self.assertEqual(spec["STEEL-ROOF"], "ROOF-X")
        self.assertNotEqual(bm.NOTHING, bm.NOT_DESCRIBED)

    def test_empty_value_and_missing_key_are_both_not_described_but_not_nothing(self):
        write_boxspec(self.design, "8.boxspec", ["シンボル名$BSP-STEELA-H|STEEL-BASE$|STEEL-SIDE$無し|"])
        spec = {r["COMPONENT_CODE"]: r["MATERIAL_SPEC"] for r in bm.parse_boxspec(self.design / "8.boxspec")}
        self.assertEqual(spec["STEEL-BASE"], "未記載")           # 値が空
        self.assertEqual(spec["STEEL-DOOR"], "未記載")           # キー無し
        self.assertEqual(spec["STEEL-SIDE"], "無し")             # 明示値は別
        self.assertEqual(sum(v == "無し" for v in spec.values()), 1)

    def test_unknown_steel_key_is_kept_and_logged(self):
        write_boxspec(self.design, "3.boxspec", ["シンボル名$BSP-STEELA-H|STEEL-BASE$B|STEEL-NEWPART$NEW-X|"])
        with self.assertLogs("cad_data.boxspec_material", level="WARNING") as cm:
            rows = bm.parse_boxspec(self.design / "3.boxspec")
        extra = [r for r in rows if r["COMPONENT_CODE"] == "STEEL-NEWPART"]
        self.assertEqual(len(extra), 1)
        self.assertEqual((extra[0]["COMPONENT_NAME"], extra[0]["MATERIAL_SPEC"]), ("", "NEW-X"))
        self.assertTrue(any("STEEL-NEWPART" in m for m in cm.output))
        self.assertEqual(len(rows), len(bm.COMPONENTS) + 1)

    def test_multiple_symbols_in_a_file_make_separate_rows(self):
        write_boxspec(self.design, "4.boxspec", ["シンボル名$BSP-STEELA-H|STEEL-BASE$A1|", "シンボル名$BSP-STEELA-L|STEEL-BASE$A2|"])
        rows = bm.parse_boxspec(self.design / "4.boxspec")
        self.assertEqual(len(rows), 18)
        self.assertEqual([r["SYMBOL_NAME"] for r in rows[:1] + rows[9:10]], ["BSP-STEELA-H", "BSP-STEELA-L"])

    def test_cabtype_absent_is_empty(self):
        write_boxspec(self.design, "5.boxspec", [STEEL_ALL])
        self.assertEqual({r["CABTYPE"] for r in bm.parse_boxspec(self.design / "5.boxspec")}, {""})

    def test_ambiguous_multiple_cabtypes_are_not_guessed(self):
        write_boxspec(self.design, "6.boxspec", ["シンボル名$BSP-CABTYPEA|CABTYPE$AA|", "シンボル名$BSP-CABTYPEA|CABTYPE$BB|", STEEL_ALL])
        with self.assertLogs("cad_data.boxspec_material", level="WARNING") as cm:
            rows = bm.parse_boxspec(self.design / "6.boxspec")
        self.assertEqual({r["CABTYPE"] for r in rows}, {""})
        self.assertTrue(any("CABTYPE" in m for m in cm.output))

    def test_same_cabtype_repeated_is_not_ambiguous(self):
        write_boxspec(self.design, "7.boxspec", ["シンボル名$BSP-CABTYPEA|CABTYPE$AA|", "シンボル名$BSP-CABTYPEA|CABTYPE$AA|", STEEL_ALL])
        self.assertEqual({r["CABTYPE"] for r in bm.parse_boxspec(self.design / "7.boxspec")}, {"AA"})

    def test_unrelated_symbols_and_blank_lines_are_ignored(self):
        write_boxspec(self.design, "8.boxspec", ["", "シンボル名$BSP-OTHER|KEY$V|", "", STEEL_ALL])
        self.assertEqual(len(bm.parse_boxspec(self.design / "8.boxspec")), 9)

    def test_malformed_element_raises(self):
        write_boxspec(self.design, "9.boxspec", ["シンボル名$BSP-STEELA-H|STEEL-BASE-without-dollar|"])
        with self.assertRaises(bm.BoxspecError):
            bm.parse_boxspec(self.design / "9.boxspec")

    def test_duplicate_component_key_raises(self):
        write_boxspec(self.design, "10.boxspec", ["シンボル名$BSP-STEELA-H|STEEL-BASE$A|STEEL-BASE$B|"])
        with self.assertRaises(bm.BoxspecError):
            bm.parse_boxspec(self.design / "10.boxspec")

    def test_not_cp932_raises(self):
        (self.design / "11.boxspec").write_bytes(b"\x81")      # 不完全な CP932 の並び
        with self.assertRaises(bm.BoxspecError):
            bm.parse_boxspec(self.design / "11.boxspec")

    def test_source_boxspec_is_read_only(self):
        write_boxspec(self.design, "12.boxspec", [STEEL_ALL])
        p = self.design / "12.boxspec"
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        bm.build_material_rows(self.design)
        self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)


class BuildTest(BoxspecBase):
    def test_multiple_pages_are_collected_in_page_order_with_source(self):
        for name in ("301.boxspec", "7.boxspec", "30.boxspec", "3A.boxspec"):
            write_boxspec(self.design, name, [STEEL_ALL])
        rows = bm.build_material_rows(self.design)
        order = list(dict.fromkeys(r["PAGE"] for r in rows))
        self.assertEqual(order, ["7", "30", "301", "3A"])           # 数字は数値順、それ以外は後ろ
        self.assertEqual({(r["PAGE"], r["SOURCE_FILE"]) for r in rows},
                         {("7", "7.boxspec"), ("30", "30.boxspec"), ("301", "301.boxspec"), ("3A", "3A.boxspec")})

    def test_uppercase_extension_is_found(self):
        write_boxspec(self.design, "5.BOXSPEC", [STEEL_ALL])
        self.assertEqual(len(bm.build_material_rows(self.design)), 9)

    def test_no_boxspec_is_empty_not_error(self):
        self.assertEqual(bm.build_material_rows(self.design), [])
        self.assertEqual(bm.build_material_rows(self.design / "missing"), [])

    def test_one_bad_file_makes_the_whole_list_unavailable(self):
        write_boxspec(self.design, "1.boxspec", [STEEL_ALL])
        write_boxspec(self.design, "2.boxspec", ["シンボル名$BSP-STEELA-H|oops|"])
        with self.assertLogs("cad_data.boxspec_material", level="WARNING") as cm:
            self.assertIsNone(bm.build_material_rows(self.design))
        self.assertEqual([r.levelname for r in cm.records], ["WARNING"])


class ExportTest(BoxspecBase):
    def test_writes_cp932_no_bom_with_fixed_columns(self):
        write_boxspec(self.design, "7.boxspec", ["シンボル名$BSP-CABTYPEA|CABTYPE$ZZ1|", STEEL_ALL])
        path = bm.export_material_csv(self.design, self.out)
        self.assertEqual(Path(path), self.out / "material.csv")
        raw = Path(path).read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        raw.decode("cp932")                                           # 読み戻せる
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")                                       # 日本語の列値があり UTF-8 ではない
        self.assertEqual(list(read_rows(path)[0].keys()), bm.MATERIAL_COLUMNS)
        self.assertEqual(len(read_rows(path)), 9)
        self.assertEqual(read_rows(path)[0]["COMPONENT_NAME"], "基礎ベース")

    def test_no_boxspec_creates_no_file(self):
        with self.assertLogs("cad_data.boxspec_material", level="INFO") as cm:
            self.assertIsNone(bm.export_material_csv(self.design, self.out))
        self.assertEqual([r.levelname for r in cm.records], ["INFO"])
        self.assertFalse((self.out / "material.csv").exists())

    def test_parse_failure_creates_no_file_and_does_not_raise(self):
        write_boxspec(self.design, "1.boxspec", ["シンボル名$BSP-STEELA-H|oops|"])
        with self.assertLogs("cad_data.boxspec_material", level="WARNING"):
            self.assertIsNone(bm.export_material_csv(self.design, self.out))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_write_failure_leaves_no_partial_file_or_temp(self):
        write_boxspec(self.design, "1.boxspec", [STEEL_ALL])
        real_replace = os.replace

        def boom(*a, **k):
            raise OSError("disk full (simulated)")

        os.replace = boom
        try:
            with self.assertLogs("cad_data.boxspec_material", level="ERROR"):
                self.assertIsNone(bm.export_material_csv(self.design, self.out))
        finally:
            os.replace = real_replace
        self.assertEqual(list(self.out.iterdir()), [])                # material.csv も一時ファイルも残さない

    def test_existing_material_csv_is_replaced_atomically_on_success(self):
        (self.out / "material.csv").write_bytes(b"old")
        write_boxspec(self.design, "1.boxspec", [STEEL_ALL])
        bm.export_material_csv(self.design, self.out)
        self.assertEqual(len(read_rows(self.out / "material.csv")), 9)
        self.assertEqual([p.name for p in self.out.iterdir()], ["material.csv"])

    def test_uses_given_logger(self):
        stream = io.StringIO()
        lg = logging.getLogger("boxspec_test_logger")
        lg.addHandler(logging.StreamHandler(stream))
        lg.setLevel(logging.INFO)
        bm.export_material_csv(self.design, self.out, log=lg)
        self.assertIn("material.csv は作成しません", stream.getvalue())


class PipelineIntegrationTest(MainIntegrationBase):
    """main() の擬似環境(外部呼び出しはモック・ガード)で、製番フォルダに material.csv が出ること/出なくても続行すること。"""
    boxspec_lines = None

    def _make_design_data(self):
        super()._make_design_data()
        if self.boxspec_lines is not None:
            write_boxspec(self.design_product, "301.boxspec", self.boxspec_lines)

    def log_text(self):
        return next((self.product_out.parent.parent / "logs").glob("*.log")).read_text(encoding="utf-8")


class WithBoxspecTest(PipelineIntegrationTest):
    boxspec_lines = ["シンボル名$BSP-CABTYPEA|CABTYPE$ZZ1|", STEEL_ALL]

    def test_material_csv_is_written_in_the_product_folder_and_run_completes(self):
        self.run_main()
        rows = read_rows(self.product_out / "material.csv")
        self.assertEqual(len(rows), 9)
        self.assertEqual({r["PAGE"] for r in rows}, {"301"})
        self.assertIn("CSV作成: 材料", self.log_text())
        self.assertTrue((self.product_out / "estcode_df.csv").exists())   # 既存の出力は従来どおり

    def test_original_boxspec_is_untouched(self):
        before = {p: p.read_bytes() for p in self.design_product.glob("*.boxspec")}
        self.run_main()
        self.assertEqual({p: p.read_bytes() for p in self.design_product.glob("*.boxspec")}, before)


class WithoutBoxspecTest(PipelineIntegrationTest):
    def test_run_continues_and_no_material_csv(self):
        self.run_main()
        self.assertFalse((self.product_out / "material.csv").exists())
        self.assertIn("material.csv は作成しませんでした", self.log_text())
        self.assertTrue((self.product_out / "estcode_df.csv").exists())


class BrokenBoxspecTest(PipelineIntegrationTest):
    boxspec_lines = ["シンボル名$BSP-STEELA-H|oops|"]

    def test_parse_error_does_not_stop_the_product(self):
        self.run_main()
        self.assertFalse((self.product_out / "material.csv").exists())
        self.assertIn("boxspec の解析に失敗", self.log_text())
        self.assertTrue((self.product_out / "estcode_df.csv").exists())


if __name__ == "__main__":
    unittest.main()
