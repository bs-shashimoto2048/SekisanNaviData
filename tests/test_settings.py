# pipeline.settings(S3)のテスト。外部接続・実処理なし(一時ディレクトリと mock のみ)。
# MainConfigBehaviorTest は app.main を通す特性化テスト(分割前の挙動と同じであること: 設定エラーの種類と、何も作らないこと)。
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["SEKISAN_PRODUCTION_HOSTS"] = "prod-host-example"  # 本番ホスト名はリポジトリに書かない。テスト用の架空ホストを本番扱いにする
import app  # noqa: E402
import inference_support  # noqa: E402
from pipeline import settings as settings_mod  # noqa: E402

PROD_UNC = r"\\prod-host-example\ShareData\exampleroot"


class ConfigFileBase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="settings_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name).resolve()
        self.out_root = self.tmp / "out"

    def write_config(self, mutate=None, name="cfg.yaml") -> Path:
        cfg = yaml.safe_load((ROOT / "app_test.yaml").read_text(encoding="utf-8"))
        cfg["data"]["output"] = str(self.out_root)
        cfg["api"]["dir_venv"] = str(self.tmp / "api")
        cfg["dwg_conversion"]["converter"] = str(self.tmp / "conv" / "ODAFileConverter.exe")
        if mutate:
            mutate(cfg)
        path = self.tmp / name
        path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        return path


class MainConfigBehaviorTest(ConfigFileBase):
    """設定が不完全・不正なとき、main() が(出力フォルダ・ログを作る前に)どのエラーで止まるか。"""

    def run_main(self, path):
        with mock.patch.object(app.host_mod, "create_n3_design_list", side_effect=AssertionError("開始しないはず")):
            return app.main(config_path=path)

    def test_missing_target_initials_raises_keyerror_and_creates_nothing(self):
        path = self.write_config(lambda c: c["data"].pop("initials"))
        with self.assertRaises(KeyError) as cm:
            self.run_main(path)
        self.assertEqual(cm.exception.args[0], "initials")
        self.assertFalse(self.out_root.exists())

    def test_missing_output_raises_keyerror(self):
        path = self.write_config(lambda c: c["data"].pop("output"))
        with self.assertRaises(KeyError) as cm:
            self.run_main(path)
        self.assertEqual(cm.exception.args[0], "output")

    def test_unsafe_inference_settings_are_reported_before_missing_keys(self):
        # 推論サーバー設定の検証は、対象製番・CAD変換の設定を読むより先(従来の順序)
        def mutate(c):
            c["data"]["output"] = PROD_UNC
            c["data"].pop("initials")
            c["inference_server"] = {"enabled": True, "port": 18765, "backend": "fake"}
        path = self.write_config(mutate)
        with self.assertRaises(inference_support.InferenceServerError):
            self.run_main(path)

    def test_production_converter_is_refused_as_inference_server_error(self):
        def mutate(c):
            c["dwg_conversion"]["converter"] = PROD_UNC + r"\ODAFileConverter.exe"
            c["inference_server"] = {"enabled": True, "port": 18765, "backend": "fake"}
        path = self.write_config(mutate)
        with mock.patch.object(inference_support.InferenceServerManager, "validate", lambda self_: None):
            with self.assertRaises(inference_support.InferenceServerError):
                self.run_main(path)
        self.assertFalse(self.out_root.exists())


class LoadSettingsTest(ConfigFileBase):
    def test_paths_follow_data_output_and_storage(self):
        cfg = settings_mod.load_settings(self.write_config())
        storage = cfg.storage
        self.assertEqual(cfg.data_dir, str(self.out_root))
        self.assertEqual(cfg.product_dir, os.path.join(str(self.out_root), storage))
        self.assertEqual(cfg.log_dir, os.path.join(cfg.product_dir, "logs"))
        self.assertEqual(cfg.output_dir, os.path.join(cfg.product_dir, "output"))
        self.assertFalse(cfg.infer_enabled)
        self.assertEqual(cfg.infer_cfg, cfg.config_data.get("inference_server") or {})   # app_test.yaml には無効(enabled: false)のセクションがある

    def test_data_dir_is_made_absolute_only_when_inference_server_is_enabled(self):
        def rel(c):
            c["data"]["output"] = "relative_out"
        self.assertEqual(settings_mod.load_settings(self.write_config(rel)).data_dir, "relative_out")

        def rel_on(c):
            rel(c)
            c["inference_server"] = {"enabled": True, "port": 18765}
        cfg = settings_mod.load_settings(self.write_config(rel_on, "on.yaml"))
        self.assertTrue(cfg.infer_enabled)
        self.assertEqual(cfg.data_dir, os.path.abspath("relative_out"))

    def test_enabled_flag_is_false_when_section_is_empty_or_disabled(self):
        for section in (None, {}, {"enabled": False, "port": 18765}):
            def mutate(c, s=section):
                c.pop("inference_server", None)
                if s is not None:
                    c["inference_server"] = s
            self.assertFalse(settings_mod.load_settings(self.write_config(mutate)).infer_enabled, section)

    def test_later_settings_are_read_when_used(self):
        cfg = settings_mod.load_settings(self.write_config(lambda c: c["data"].pop("initials")))  # 読込は成功する
        with self.assertRaises(KeyError):
            cfg.target_initial
        ok = settings_mod.load_settings(self.write_config(name="ok.yaml"))
        self.assertEqual(ok.box_types, tuple(ok.config_data["data"]["box_types"]))
        self.assertEqual(ok.converter_path, ok.config_data["dwg_conversion"]["converter"])
        self.assertEqual((ok.converter_extension, ok.converted_extension),
                         (ok.config_data["data_extension"]["convert_bfr"], ok.config_data["data_extension"]["convert_aft"]))

    def test_explicit_path_skips_sample_check_but_default_path_applies_it(self):
        sample = self.write_config(lambda c: c["data"].__setitem__("output", "<SHARE_ROOT>/x"), "sample.yaml")
        settings_mod.load_settings(sample)  # 明示指定(検証用 YAML など)は拒否しない(従来どおり)
        with self.assertRaises(RuntimeError) as cm:
            settings_mod.load_settings(None, default_path=lambda: str(sample))
        self.assertIn("app.local.yaml", str(cm.exception))

    def test_sample_check_covers_output_api_and_converter(self):
        for section, key in settings_mod.SAMPLE_CONFIG_KEYS:
            with self.assertRaises(RuntimeError, msg=(section, key)):
                settings_mod.assert_not_sample_config({section: {key: "<X>/y"}}, "app.yaml")
        settings_mod.assert_not_sample_config({"data": {"output": "D:/w"}}, "app.local.yaml")

    def test_default_config_path_prefers_local_file(self):
        self.assertEqual(settings_mod.default_config_path(self.tmp, "app"), f"{self.tmp}/app.yaml")
        (self.tmp / "app.local.yaml").write_text("data: {}", encoding="utf-8")
        self.assertEqual(settings_mod.default_config_path(self.tmp, "app"), str(self.tmp / "app.local.yaml"))


class BuildInferenceManagerTest(ConfigFileBase):
    def cfg(self, mutate=None):
        def m(c):
            c["inference_server"] = {"enabled": True, "port": 18765, "backend": "fake"}
            if mutate:
                mutate(c)
        return settings_mod.load_settings(self.write_config(m))

    def test_disabled_returns_none_and_touches_nothing(self):
        cfg = settings_mod.load_settings(self.write_config())
        with mock.patch.object(inference_support, "InferenceServerManager", side_effect=AssertionError("作らないはず")):
            self.assertEqual(settings_mod.build_inference_manager(cfg), (None, None))

    def test_enabled_validates_and_returns_manager_and_server_settings(self):
        cfg = self.cfg()
        with mock.patch.object(inference_support, "InferenceServerManager") as mgr_cls:
            manager, server_settings = settings_mod.build_inference_manager(cfg)
        mgr_cls.assert_called_once()
        self.assertEqual(mgr_cls.call_args.kwargs["output_root"], cfg.data_dir)
        self.assertEqual(mgr_cls.call_args.kwargs["log_dir"], cfg.log_dir)
        self.assertTrue(mgr_cls.call_args.kwargs["config_out"].endswith(cfg.config_data["api"]["yaml_file"]))
        manager.validate.assert_called_once_with()
        self.assertEqual(server_settings.port, 18765)

    def test_validation_error_stops_before_converter_check(self):
        cfg = self.cfg()
        with mock.patch.object(inference_support, "InferenceServerManager") as mgr_cls, \
                mock.patch.object(inference_support.safety, "assert_not_production", side_effect=AssertionError("呼ばれないはず")):
            mgr_cls.return_value.validate.side_effect = inference_support.InferenceServerError("範囲外")
            with self.assertRaises(inference_support.InferenceServerError):
                settings_mod.build_inference_manager(cfg)

    def test_production_converter_becomes_inference_server_error(self):
        cfg = self.cfg(lambda c: c["dwg_conversion"].__setitem__("converter", PROD_UNC + r"\ODAFileConverter.exe"))
        with mock.patch.object(inference_support, "InferenceServerManager"):
            with self.assertRaises(inference_support.InferenceServerError) as cm:
                settings_mod.build_inference_manager(cfg)
        self.assertIsInstance(cm.exception.__cause__, inference_support.safety.SafetyError)


if __name__ == "__main__":
    unittest.main()
