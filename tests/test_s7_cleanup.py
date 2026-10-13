# S7(整理)のテスト: デバッグ print が標準出力に出ないこと、重複・誤ったログの修正、app.py に残した「未使用」import が
# テストの差し替え点として実際に使われていること(誤って消されないための固定)。外部接続なし(test_main_integration と同じ擬似環境)。
import ast
import contextlib
import io
import sys
import unittest
from pathlib import Path

import ezdxf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import cad_data_process as cad  # noqa: E402
from test_main_integration import MainIntegrationBase  # noqa: E402

DEBUG_MARKERS = ("log_text = ", "max_x = ", "width = ", "DEBUG:")


class RunLogTest(MainIntegrationBase):
    def setUp(self):
        super().setUp()
        self.run_main()
        logs = sorted((self.out_root / "a_product" / "logs").glob("*.log"))
        self.log = logs[0].read_text(encoding="utf-8")

    def test_page_total_line_is_logged_once(self):
        self.assertEqual(self.log.count("対象ページ Total"), 1)

    def test_coordinate_df_csv_line_shows_the_product_df_path(self):
        line = next(ln for ln in self.log.splitlines() if "CSV作成: 座標計算用" in ln)
        self.assertTrue(line.rstrip().endswith("product_df.csv"), line)
        self.assertNotIn("_df_outline.csv", line)       # 以前は外形図の CSV のパスを出していた

    def test_other_csv_lines_are_unchanged(self):
        for label, name in (("内器図", "_df_cubicle.csv"), ("基礎図", "_df_foundation.csv"), ("外形図", "_df_outline.csv")):
            line = next(ln for ln in self.log.splitlines() if f"CSV作成: {label}" in ln)
            self.assertTrue(line.rstrip().endswith(name), line)

    def test_inference_request_is_still_logged_via_logger(self):
        # 標準出力のデバッグ表示(log_text = …)は廃止。同じ内容はロガーの「リクエスト内容」に出る
        self.assertIn("リクエスト内容:", self.log)

    def test_no_debug_print_reaches_stdout(self):
        out = self.stdout.getvalue()
        for marker in DEBUG_MARKERS:
            self.assertNotIn(marker, out, marker)


class NoDebugPrintTest(unittest.TestCase):
    def test_get_frame_size_prints_nothing_and_still_returns_the_size(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="s7_") as td:
            doc = ezdxf.new("R2010")
            doc.layers.add("AUX", color=1)
            doc.modelspace().add_lwpolyline([(0, 0), (4200, 0), (4200, 2970), (0, 2970)], close=True,
                                            dxfattribs={"layer": "AUX", "color": 1})
            path = Path(td) / "f.dxf"
            doc.saveas(path)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                size = cad.get_frame_size(str(path))
            self.assertEqual(tuple(size), (4200, 2970))
            self.assertEqual(buf.getvalue(), "")

    def test_get_frame_size_without_frame_objects_returns_zero_silently(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="s7_") as td:
            doc = ezdxf.new("R2010")
            doc.modelspace().add_circle((0, 0), 5)   # 図枠(AUX 層の赤い線)が無い
            path = Path(td) / "g.dxf"
            doc.saveas(path)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                size = cad.get_frame_size(str(path))
            self.assertEqual(tuple(size), (0, 0))
            self.assertEqual(buf.getvalue(), "")


class AppImportsTest(unittest.TestCase):
    # app.py 自身は使わないが、テストが mock.patch.object(app.<名前>, ...) で差し替えている接点
    SEAMS = {"config_loader", "design_mod", "host_mod", "image_mod", "cad_mod", "print_support", "shutil"}

    def unused_imports(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        imported = set()
        for n in tree.body:
            if isinstance(n, ast.Import):
                imported |= {(a.asname or a.name).split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                imported |= {a.asname or a.name for a in n.names}
        used = {x.id for x in ast.walk(tree) if isinstance(x, ast.Name)}
        return imported - used

    def test_only_documented_test_seams_remain_unused(self):
        self.assertEqual(self.unused_imports() - self.SEAMS, set())

    def test_every_seam_is_actually_patched_or_used_by_tests(self):
        text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "tests").glob("test_*.py") if p.name != Path(__file__).name)
        for name in self.SEAMS & self.unused_imports():
            self.assertIn(f"app.{name}", text, f"{name} を差し替えるテストが無い(不要なら削除してよい)")

    def test_removed_imports_are_not_needed_by_app(self):
        import app
        for name in ("cv2", "pd", "re", "time", "datetime", "timedelta", "est_mod"):
            self.assertFalse(hasattr(app, name), name)
        for name in self.SEAMS:
            self.assertTrue(hasattr(app, name), name)


if __name__ == "__main__":
    unittest.main()
