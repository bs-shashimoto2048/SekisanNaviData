# estimate_code_processer(S4)のテスト: 積算コード集計用 DF の生成(純関数)、CSV 保存、未実装スタブの扱い、重複定義の解消。
# ファイルは一時ディレクトリだけ。外部接続なし。
import ast
import builtins
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import estimate_code_processer as est  # noqa: E402
from test_output_contract import EXPECTED_COLUMNS  # noqa: E402

SRC_COLUMNS = ["BAN_MENNO", "BAN_NO", "PAGE", "ZUMEI", "BAN_MEISYOU", "BAN_TYPE", "BAN_H1", "BAN_W", "BAN_D",
               "KITEN_X", "KITEN_Y", "DETECT_AREA_X", "DETECT_AREA_Y"]


def src(rows):
    return pd.DataFrame(rows, columns=SRC_COLUMNS)


def base(menno, x, no=None):
    return (menno, no or menno + 20, "6", "基礎図", f"盤{menno}", "基礎図", 2300, 800, 1500, x, 3055, 800, 1500)


def front(menno, page, x):
    return (menno, menno + 20, page, "内部機器配置図(1-1)", f"盤{menno}", "正面図", 2300, 800, 1500, x, 500, 800, 2300)


def side(menno, page, x):
    return (menno, menno + 20, page, "内部機器配置図(2-1)", f"盤{menno}", "側面図", 2300, 800, 1500, x, 500, 1500, 2300)


DET = pd.DataFrame({"PAGE": ["21", "21", "22", "22", "22"],
                    "DEVICE": ["panel", "panel", "transformer", "transformer", "transformer"],
                    "CENTER_X": [900, 3900, 2500, 3000, 3900], "CENTER_Y": [1500, 1500, 1200, 2000, 1200]})


class BuildEstcodeDfTest(unittest.TestCase):
    def test_columns_follow_the_output_contract(self):
        self.assertEqual(est.ESTCODE_COLUMNS, EXPECTED_COLUMNS["estcode_df.csv"])
        df = est.build_estcode_df("OS2K", src([base(1, 3000)]))
        self.assertEqual(list(df.columns), est.ESTCODE_COLUMNS)

    def test_does_not_touch_files_or_folders(self):
        df = src([base(1, 3000), front(1, "21", 500)])
        with mock.patch.object(builtins, "open", side_effect=AssertionError("開かないはず")), \
                mock.patch.object(os, "makedirs", side_effect=AssertionError("作らないはず")), \
                mock.patch.object(Path, "mkdir", side_effect=AssertionError("作らないはず")), \
                mock.patch.object(pd.DataFrame, "to_csv", side_effect=AssertionError("書かないはず")):
            out = est.build_estcode_df("OS2K", df)
        self.assertEqual(len(out), 1)

    def test_does_not_modify_the_input(self):
        df = src([base(1, 3000), base(2, 3900)])
        before = df.copy(deep=True)
        est.build_estcode_df("OS2K", df)
        pd.testing.assert_frame_equal(df, before)

    def test_single_base_is_alone(self):
        df = est.build_estcode_df("OS2K", src([base(1, 3000), front(1, "21", 500)]))
        self.assertEqual(list(df["BAN_CONNECT"]), ["箱･単独"])
        self.assertEqual(list(df["SORT_ORDER"]), [1])
        self.assertEqual(list(df["MODEL"]), ["OS2K"])

    def test_several_bases_are_ordered_by_kiten_x_with_left_center_right(self):
        df = est.build_estcode_df("OS2K", src([base(2, 3900), base(1, 3000), base(3, 4800)]))
        self.assertEqual(list(df["BAN_MENNO"].astype(int)), [1, 2, 3])          # KITEN_X 昇順 = SORT_ORDER 昇順
        self.assertEqual(list(df["BAN_CONNECT"]), ["箱･左右(L)", "箱･中央", "箱･左右(R)"])
        self.assertEqual(list(df["SORT_ORDER"]), [1, 2, 3])

    def test_without_base_diagram_rows_get_defaults(self):
        df = est.build_estcode_df("OS2K", src([front(1, "21", 500), side(2, "22", 2000)]))
        self.assertEqual(list(df["BAN_CONNECT"]), ["", ""])
        self.assertEqual(list(df["SORT_ORDER"]), [9999, 9999])

    def test_nan_kiten_x_sorts_last_and_duplicate_ban_gives_one_row(self):
        df = est.build_estcode_df("OS2K", src([base(1, np.nan), base(2, 3900), base(2, 3900)]))
        self.assertEqual(list(df["BAN_MENNO"].astype(int)), [2, 1])
        self.assertEqual(len(df), 2)

    def test_panel_height_width_depth_come_from_first_row_of_the_ban(self):
        df = est.build_estcode_df("OS2K", src([base(1, 3000)]))
        self.assertEqual((df.loc[0, "BAN_H"], df.loc[0, "BAN_W"], df.loc[0, "BAN_D"]), (2300, 800, 1500))

    def test_missing_required_columns_raise_keyerror_as_before(self):
        with self.assertRaises(KeyError):
            est.build_estcode_df("OS2K", src([base(1, 3000)]).drop(columns=["BAN_MENNO"]))
        with self.assertRaises(KeyError):
            est.build_estcode_df("OS2K", src([base(1, 3000)]).drop(columns=["ZUMEI"]))

    def test_base_diagram_maps_helper(self):
        order, connect = est._base_diagram_maps(src([base(2, 3900), base(1, 3000)]))
        self.assertEqual((order, connect), ({"1": 1, "2": 2}, {"1": "箱･左右(L)", "2": "箱･左右(R)"}))
        self.assertEqual(est._base_diagram_maps(src([front(1, "21", 500)])), ({}, {}))


class CreateEstcodeCsvTest(unittest.TestCase):
    def test_creates_folder_and_writes_cp932_without_bom(self):
        with tempfile.TemporaryDirectory(prefix="estcode_") as td:
            target = os.path.join(td, "new", "dir")
            df, path = est.create_estcode_df_and_csv("OS2K", src([base(1, 3000), base(2, 3900)]), target)
            self.assertEqual(path, os.path.join(target, "estcode_df.csv"))
            raw = Path(path).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(raw.decode("cp932"), df.to_csv(index=False))
            self.assertIn("箱･左右(L)".encode("cp932"), raw)
            with self.assertRaises(UnicodeDecodeError):
                raw.decode("utf-8")

    def test_dataframe_equals_the_pure_build(self):
        with tempfile.TemporaryDirectory(prefix="estcode_") as td:
            data = src([base(2, 3900), base(1, 3000)])
            df, _ = est.create_estcode_df_and_csv("OS2K", data, td)
            pd.testing.assert_frame_equal(df, est.build_estcode_df("OS2K", data))


class UnimplementedRulesTest(unittest.TestCase):
    IMPLEMENTED = {"PANEL", "TRANS"}
    IDENTITY = {"MODEL", "BAN_MENNO", "BAN_NO", "BAN_MEISYOU", "BAN_H", "BAN_W", "BAN_D", "BAN_CONNECT", "SORT_ORDER"}

    def test_every_column_is_exactly_one_of_identity_implemented_or_unimplemented(self):
        groups = [self.IDENTITY, self.IMPLEMENTED, set(est.UNIMPLEMENTED_ESTCODE_COLUMNS)]
        self.assertEqual(set().union(*groups), set(est.ESTCODE_COLUMNS))
        self.assertEqual(sum(len(g) for g in groups), len(est.ESTCODE_COLUMNS))   # 重複なし

    def test_unimplemented_columns_stay_at_initial_values_after_the_aggregation(self):
        data = src([base(1, 3000), base(2, 3900), front(1, "21", 500), side(2, "22", 2000)])
        df = est.build_estcode_df("OS2K", data)
        df = est.estcode_transformer(data, DET, est.estcode_panel(data, DET, df))
        for col in est.UNIMPLEMENTED_ESTCODE_COLUMNS:
            expected = 0.0 if col == "INPUT_CU_COEFF" else 0
            self.assertTrue((df[col] == expected).all(), col)
        self.assertEqual(int(df["PANEL"].astype(int).sum()), 1)      # 実装済みの 2 列は集計される
        self.assertEqual(int(df["TRANS"].astype(int).sum()), 2)

    def test_stub_functions_exist_are_marked_unimplemented_and_return_none(self):
        names = {v for v in est.UNIMPLEMENTED_ESTCODE_COLUMNS.values() if v} | {"estcode_box", "estcode_add_accs_price"}
        for name in names:
            fn = getattr(est, name)
            self.assertIn("【未実装】", fn.__doc__, name)
            self.assertIsNone(fn(), name)

    def test_passage_and_stack_plate_are_distinct_functions(self):
        # 以前は estcode_stack_plate が 2 回定義され、後の定義(通路)が前(段積みプレート)を上書きしていた
        self.assertIsNot(est.estcode_passage, est.estcode_stack_plate)
        self.assertIn("18321", est.estcode_passage.__doc__)
        self.assertIn("18308", est.estcode_stack_plate.__doc__)

    def test_no_function_is_defined_twice_at_module_level(self):
        tree = ast.parse((ROOT / "estimate_code_processer.py").read_text(encoding="utf-8"))
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        self.assertEqual([n for n in set(names) if names.count(n) > 1], [])


if __name__ == "__main__":
    unittest.main()
