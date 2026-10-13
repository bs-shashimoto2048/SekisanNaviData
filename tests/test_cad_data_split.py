# cad_data_process の役割別分割(S5)のテスト。
# - CadDataBehaviorTest: 移した関数の挙動(分割前のコードでも同じ結果で通る特性化テスト)。
# - CadDataStructureTest: 窓口(cad_data_process)の公開名・実装モジュールの役割分担・呼び出し側が窓口を経由していること。
# CAD(IJCAD・ODA)・外部サーバーには接続しない。DXF/CSV は一時ディレクトリに作る。
import ast
import importlib
import sys
import tempfile
import unittest
from pathlib import Path

import ezdxf
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cad_data_process as cad  # noqa: E402

# 分割前から公開されている名前(関数 24 + 定数 1)と、分割後の実装モジュール
PUBLIC_NAMES = {
    "baninf_tables": ["to_numeric_safe", "clean_zumei", "safe_apply", "export_csv", "assign_detect_area_standard",
                      "assign_detect_area_foundation", "load_baninf", "side_view_classifier", "filter_calc_export",
                      "natural_key", "export_ban_data", "merge_and_sort_csvs"],
    "frame_scale": ["specified_frame_detection", "get_frame_size", "update_frame_info"],
    "print_convert": ["file_exists", "chunk_list", "countdown", "convert_dwg2dxf", "copy_files_from_list",
                      "listing_files", "is_file_in_use", "EXCLUDED_PAGE_PREFIX", "is_excluded_page", "list_print_pages"],
}


class CadDataBehaviorTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="cad_split_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)

    def test_chunk_list_splits_into_fives_by_default(self):
        self.assertEqual(list(cad.chunk_list(list("abcdefg"))), [list("abcde"), list("fg")])
        self.assertEqual(list(cad.chunk_list([1, 2, 3], 2)), [[1, 2], [3]])
        self.assertEqual(list(cad.chunk_list([])), [])

    def test_natural_key_orders_numbers_inside_names(self):
        self.assertEqual(sorted(["10", "2", "1"], key=cad.natural_key), ["1", "2", "10"])
        self.assertEqual(sorted(["p10", "p9", "p1"], key=cad.natural_key), ["p1", "p9", "p10"])

    def test_update_frame_info_sets_columns_only_on_the_page_rows(self):
        df = pd.DataFrame({"PAGE": ["21", "21", "22"], "BAN_MENNO": [1, 2, 3]})
        out = cad.update_frame_info(df, "21", 4200, 2970, 2100, 1485, 2.0, 2.0)
        self.assertIs(out, df)   # in-place
        row = out[out["PAGE"] == "21"].iloc[0]
        self.assertEqual((row["FRAME_ORG_X"], row["FRAME_ORG_Y"], row["FRAME_MINI_X"], row["FRAME_MINI_Y"]), (4200, 2970, 2100, 1485))
        self.assertEqual((row["SCALE_X"], row["SCALE_Y"]), (2.0, 2.0))
        self.assertTrue(out[out["PAGE"] == "22"][["FRAME_ORG_X", "SCALE_X"]].isna().all().all())

    def test_merge_and_sort_csvs_fills_missing_columns_and_sorts(self):
        a, b = self.tmp / "a.csv", self.tmp / "b.csv"
        pd.DataFrame({"PAGE": [22, 21], "X": [1, 2]}).to_csv(a, index=False, encoding="cp932")
        pd.DataFrame({"PAGE": [20], "Y": ["あ"]}).to_csv(b, index=False, encoding="cp932")
        out = cad.merge_and_sort_csvs([str(a), str(b)], ["PAGE"])
        self.assertEqual(list(out.columns), ["PAGE", "X", "Y"])
        self.assertEqual(list(out["PAGE"]), ["20", "21", "22"])   # PAGE は文字列に統一してからソート
        with self.assertRaises(ValueError):
            cad.merge_and_sort_csvs([], ["PAGE"])
        with self.assertRaises(FileNotFoundError):
            cad.merge_and_sort_csvs([str(self.tmp / "none.csv")], ["PAGE"])

    def test_get_frame_size_reads_the_red_aux_polyline(self):
        doc = ezdxf.new("R2010")
        doc.layers.add("AUX", color=1)
        doc.modelspace().add_lwpolyline([(0, 0), (4200, 0), (4200, 2970), (0, 2970)], close=True,
                                        dxfattribs={"layer": "AUX", "color": 1})
        path = self.tmp / "f.dxf"
        doc.saveas(path)
        self.assertEqual(tuple(cad.get_frame_size(str(path))), (4200, 2970))

    def test_file_exists_and_listing_files(self):
        (self.tmp / "1.dwg").write_bytes(b"x")
        (self.tmp / "2.txt").write_bytes(b"x")
        self.assertTrue(cad.file_exists(str(self.tmp), "1.dwg"))
        self.assertFalse(cad.file_exists(str(self.tmp), "9.dwg"))
        self.assertEqual(sorted(cad.listing_files(str(self.tmp), "dwg")), ["1.dwg"])

    def test_print_page_selection_constants(self):
        self.assertEqual(cad.EXCLUDED_PAGE_PREFIX, "x")
        self.assertTrue(cad.is_excluded_page("X1") and cad.is_excluded_page("x2"))
        self.assertFalse(cad.is_excluded_page("AX1"))


class CadDataStructureTest(unittest.TestCase):
    def test_facade_exports_every_public_name_as_the_same_object_as_its_implementation(self):
        for mod, names in PUBLIC_NAMES.items():
            impl = importlib.import_module(f"cad_data.{mod}")
            for name in names:
                self.assertTrue(hasattr(cad, name), name)
                self.assertIs(getattr(cad, name), getattr(impl, name), name)
                if callable(getattr(impl, name)):
                    self.assertEqual(getattr(impl, name).__module__, f"cad_data.{mod}", name)

    def test_public_names_are_not_defined_twice(self):
        flat = [n for names in PUBLIC_NAMES.values() for n in names]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(len(flat), 25)

    def test_roles_are_separated_by_imports(self):
        def imports(mod):
            tree = ast.parse((ROOT / "cad_data" / f"{mod}.py").read_text(encoding="utf-8"))
            names = set()
            for n in tree.body:
                if isinstance(n, ast.Import):
                    names |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom):
                    names.add(n.module.split(".")[0])
            return names
        heavy = {"cv2", "ezdxf", "msvcrt", "subprocess", "numpy"}
        self.assertEqual(imports("baninf_tables") & heavy, set())            # 表データ処理は pandas とファイルのみ
        self.assertEqual(imports("frame_scale") & {"msvcrt", "subprocess", "shutil"}, set())   # 図枠・縮尺は変換・印刷に依存しない
        self.assertIn("cv2", imports("frame_scale"))
        self.assertIn("msvcrt", imports("print_convert"))
        for mod in PUBLIC_NAMES:   # 実装モジュール同士は import しない(循環・依存の逆転を防ぐ)
            self.assertFalse(any(i == "cad_data" or i == "cad_data_process" for i in imports(mod)), mod)

    def test_callers_go_through_the_facade_only(self):
        # テストは cad_mod(窓口)の属性を差し替えるため、cad_data 配下を直接 import する呼び出し側があってはならない
        offenders = []
        for p in ROOT.rglob("*.py"):
            rel = p.relative_to(ROOT)
            if rel.parts[0] in (".venv", "inference_server", "packages", "output", "output_test", "_backup") or rel.parts[0] == "cad_data":
                continue
            if rel.name in ("cad_data_process.py", "test_cad_data_split.py"):
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            if "from cad_data" in text or "import cad_data." in text:
                offenders.append(str(rel))
        self.assertEqual(offenders, [])

    def test_patching_the_facade_is_seen_by_callers(self):
        from unittest import mock
        from pipeline import product_flow
        with mock.patch.object(cad, "countdown", mock.Mock()) as m:
            product_flow.cad_mod.countdown(3)
        m.assert_called_once_with(3)


if __name__ == "__main__":
    unittest.main()
