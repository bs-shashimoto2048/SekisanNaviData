# main(config_path) の設定読込経路テスト(外部接続・実処理なし)
# - config_loader.load_yaml を差し替え、最初の読込パスを記録した時点で例外で打ち切る。
#   そのため main() はホスト接続・API・CAD起動・rmtree などの実処理に到達しない。
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["SEKISAN_PRODUCTION_HOSTS"] = "prod-host-example"  # 本番ホスト名はリポジトリに書かない。テスト用の架空ホストを本番扱いにする
import app  # noqa: E402


class _Stop(Exception):
    pass


def _first_loaded_path(**kwargs):
    seen = []

    def fake_load_yaml(path):
        seen.append(str(path))
        raise _Stop

    with mock.patch.object(app.config_loader, "load_yaml", side_effect=fake_load_yaml):
        try:
            app.main(**kwargs)
        except _Stop:
            pass
        else:
            raise AssertionError("main() は最初の設定読込で停止するはず")
    return seen


class MainConfigPathTest(unittest.TestCase):
    def test_signature_default_is_none(self):
        p = inspect.signature(app.main).parameters["config_path"]
        self.assertIsNone(p.default)

    def test_default_reads_app_yaml_when_no_local_config(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(app, "MDL_DIR", Path(td)):
            seen = _first_loaded_path()
            self.assertEqual(seen, [f"{td}/app.yaml"])

    def test_default_prefers_git_ignored_local_config(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(app, "MDL_DIR", Path(td)):
            (Path(td) / "app.local.yaml").write_text("data: {}" + chr(10), encoding="utf-8")
            seen = _first_loaded_path()
            self.assertEqual(seen, [str(Path(td) / "app.local.yaml")])

    def test_sample_config_placeholders_are_refused(self):
        sample = app.config_loader.load_yaml(str(ROOT / "app.yaml"))
        with self.assertRaises(RuntimeError) as cm:
            app._assert_not_sample_config(sample, "app.yaml")
        self.assertIn("app.local.yaml", str(cm.exception))
        real = {"data": {"output": "D:/work"}, "api": {"dir_venv": "D:/work/api"}, "dwg_conversion": {"converter": "C:/oda/ODAFileConverter.exe"}}
        app._assert_not_sample_config(real, "app.local.yaml")  # 例外なし

    def test_tracked_configs_have_no_shared_paths(self):
        # 追跡対象の設定(サンプル・検証用)に、UNC/共有パスの実値が無いこと
        for name in ("app.yaml", "app_20251204.yaml", "app_test.yaml"):
            cfg = app.config_loader.load_yaml(str(ROOT / name))
            for value in (cfg["data"]["output"], cfg["api"]["dir_venv"], cfg["dwg_conversion"]["converter"]):
                self.assertFalse(str(value).replace("\\", "/").startswith("//"), f"{name}: {value}")

    def test_explicit_path_is_used(self):
        target = str(ROOT / "app_test.yaml")
        seen = _first_loaded_path(config_path=target)
        self.assertEqual(seen, [target])

    def test_path_object_is_accepted(self):
        target = ROOT / "app_test.yaml"
        seen = _first_loaded_path(config_path=target)
        self.assertEqual(seen, [str(target)])

    def test_app_test_yaml_is_isolated_from_shared_folder(self):
        cfg = app.config_loader.load_yaml(str(ROOT / "app_test.yaml"))
        for value in (cfg["data"]["output"], cfg["api"]["dir_venv"], cfg["dwg_conversion"]["converter"]):
            self.assertFalse(str(value).replace("\\", "/").startswith("//"), value)   # 共有(UNC)を指さない


if __name__ == "__main__":
    unittest.main()
