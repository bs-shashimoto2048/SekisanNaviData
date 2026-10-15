# 本番モードの api.request_timeout(/predict の応答待ち[秒]、任意)のテスト。モックだけ。
# 実 API・実ホスト DB・共有・設計データ・CAD には接続しない(app.main 側は MainIntegrationBase の外部呼び出しガードの中で動く)。
import io
import contextlib
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import requests  # noqa: E402
import yaml  # noqa: E402

import app  # noqa: E402
from bs_pkg import image_mod  # noqa: E402
from pipeline import settings as settings_mod  # noqa: E402
from pipeline.settings import ConfigValueError, parse_request_timeout  # noqa: E402
from test_main_integration import PRODUCT, MainIntegrationBase  # noqa: E402

REAL_SLICE_PREDICTION_REQUEST = image_mod.slice_prediction_request       # 差し替えられる前の実関数(requests.post だけモックして通す)


class ParseTest(unittest.TestCase):
    def test_unset_is_none_and_valid_numbers_are_floats(self):
        self.assertIsNone(parse_request_timeout(None))                   # 省略 = 従来どおり待ち続ける(既定値なし)
        for v, expected in ((300, 300.0), (0.5, 0.5), (1e6, 1e6), (1, 1.0)):
            self.assertEqual(parse_request_timeout(v), expected)
            self.assertIsInstance(parse_request_timeout(v), float)

    def test_invalid_values_are_rejected(self):
        for bad in (0, 0.0, -1, -0.5, math.nan, math.inf, -math.inf, True, False, "300", "", [], {}, [300], (1,)):
            with self.assertRaises(ConfigValueError, msg=repr(bad)):
                parse_request_timeout(bad)

    def test_huge_integers_beyond_float_range_are_rejected_not_overflow(self):
        for bad in (10 ** 400, -(10 ** 400), 10 ** 309, 2 ** 5000):             # float に変換できない(OverflowError になる)整数
            with self.assertRaises(ConfigValueError, msg=str(bad)[:20]):
                parse_request_timeout(bad)
        self.assertEqual(parse_request_timeout(10 ** 300), 1e300)               # float の範囲内の大きな整数は、有限の正の数として従来どおり通る
        self.assertEqual(parse_request_timeout(2 ** 1000), float(2 ** 1000))

    def test_error_message_has_no_value_and_says_what_to_do(self):
        try:
            parse_request_timeout("secret-value")
        except ConfigValueError as e:
            self.assertNotIn("secret-value", str(e))
            self.assertIn("正の有限の数", str(e))
            self.assertIn("省略すると従来どおり", str(e))


class LoadSettingsTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="sekisan_rt_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name).resolve()

    def load(self, api=None):
        cfg = {"data": {"output": str(self.tmp / "out"), "storage": "a_product"}}
        if api is not None:
            cfg["api"] = api
        path = self.tmp / "c.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return settings_mod.load_settings(str(path))

    def test_unset_keeps_none_and_value_is_carried(self):
        self.assertIsNone(self.load().api_request_timeout)                          # api 節なし
        self.assertIsNone(self.load({"dir_venv": "x"}).api_request_timeout)         # 項目なし
        self.assertIsNone(self.load({"request_timeout": None}).api_request_timeout)  # null
        s = self.load({"request_timeout": 300})
        self.assertEqual(s.api_request_timeout, 300.0)
        self.assertEqual(settings_mod.with_output_root(s, str(self.tmp / "o2")).api_request_timeout, 300.0)   # 出力先の差し替えでも保持

    def test_invalid_value_fails_at_load_before_anything_is_created(self):
        for bad in (0, -5, "300", True):
            with self.assertRaises(ConfigValueError):
                self.load({"request_timeout": bad})
        self.assertFalse((self.tmp / "out").exists())                                # 読込時の検出。出力先は作らない

    def test_huge_yaml_integer_is_rejected_at_load(self):
        cfg_text = f"data:\n  output: {self.tmp / 'out'}\n  storage: a_product\napi:\n  request_timeout: {10 ** 400}\n"   # YAML の非常に大きな整数
        path = self.tmp / "huge.yaml"
        path.write_text(cfg_text, encoding="utf-8")
        self.assertIsInstance(yaml.safe_load(cfg_text)["api"]["request_timeout"], int)         # YAML は整数として読む(オーバーフローしない)
        with self.assertRaises(ConfigValueError):                                              # OverflowError ではなく ConfigValueError
            settings_mod.load_settings(str(path))
        self.assertFalse((self.tmp / "out").exists())


class TimeoutInMainTest(MainIntegrationBase):
    """app.main(本番モード): api.request_timeout の伝播・未設定時の互換・タイムアウト時の失敗と後始末(requests.post はモック)"""

    def set_api(self, **api):
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        cfg["api"].update(api)
        self.config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    def capture_kwargs(self):
        """slice_prediction_request に渡された引数を記録するようにする(既存の模擬に委譲)"""
        self.seen_kwargs = []
        original = MainIntegrationBase._fake_slice_prediction_request

        def spy(this, dir_product, page_list, api_cnf_path, use_proxy=True, **kw):
            this.seen_kwargs.append(kw)
            return original(this, dir_product, page_list, api_cnf_path, use_proxy)
        self._spy = spy

    def test_unset_does_not_pass_timeout_at_all(self):
        self.capture_kwargs()
        with mock.patch.object(type(self), "_fake_slice_prediction_request", self._spy):
            self.run_main()
        self.assertEqual(self.seen_kwargs, [{}])                    # timeout を渡さない = 従来どおり(image_mod 既定の None = 待ち続ける)
        self.assertEqual(self.results["completed"], [PRODUCT])

    def test_value_reaches_the_predict_request_as_a_float(self):
        self.set_api(request_timeout=300)
        self.capture_kwargs()
        with mock.patch.object(type(self), "_fake_slice_prediction_request", self._spy):
            self.run_main()
        self.assertEqual(self.seen_kwargs, [{"timeout": 300.0}])
        self.assertEqual(self.results["completed"], [PRODUCT])      # 設定しても、結果の扱いは変わらない

    def test_invalid_value_stops_before_the_output_folder_logs_host_or_api_check(self):
        for bad in (0, -1, "300", True):
            self.set_api(request_timeout=bad)
            with self.assertRaises(ConfigValueError):
                self.run_main()
            self.assertFalse((self.out_root / "a_product").exists())    # 出力フォルダもログも作らない
            self.assertEqual(self.host_calls, [])
            self.assertEqual(self.preflight_calls, [])                  # API 事前確認にも進まない
            self.assert_no_external_calls()

    def test_cli_reports_an_invalid_value_with_exit_code_1_and_no_traceback(self):
        self.set_api(request_timeout=-1)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(app._cli(["--config", str(self.config_path)]), 1)
        self.assertIn("設定値が不正", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertFalse((self.out_root / "a_product").exists())

    def test_cli_rejects_a_huge_yaml_integer_with_exit_code_1_and_no_traceback(self):
        text = self.config_path.read_text(encoding="utf-8")
        cfg = yaml.safe_load(text)
        cfg["api"]["request_timeout"] = 10 ** 400                                     # safe_dump も大きな整数をそのまま書く
        self.config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(app._cli(["--config", str(self.config_path)]), 1)
        self.assertIn("設定値が不正", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("OverflowError", err.getvalue())
        self.assertFalse((self.out_root / "a_product").exists())                       # 出力フォルダもログも作らない
        self.assertEqual(self.host_calls if hasattr(self, "host_calls") else [], [])   # ホスト照会にも進まない

    def test_timeout_fails_the_product_through_the_existing_path_without_retry_or_partial_output(self):
        # 実の slice_prediction_request を通し、requests.post だけをモック(タイムアウトを起こす)
        (self.tmp / "api").mkdir(exist_ok=True)
        (self.tmp / "api" / "server.yaml").write_text(
            yaml.safe_dump({"host": {"pc_ip": "127.0.0.1", "port": 1}}), encoding="utf-8")     # 使われないダミー(接続しない)
        self.set_api(request_timeout=7.5, use_proxy=False)
        posts = []

        def post(url, **kw):
            posts.append(kw)
            raise requests.exceptions.ReadTimeout("read timed out")

        def real_with_mock_post(this, dir_product, page_list, api_cnf_path, use_proxy=True, **kw):
            with mock.patch.object(requests, "post", side_effect=post):
                return REAL_SLICE_PREDICTION_REQUEST(dir_product, page_list, api_cnf_path, use_proxy=use_proxy, **kw)

        with mock.patch.object(type(self), "_fake_slice_prediction_request", real_with_mock_post):
            self.run_main()
        self.assertEqual(len(posts), 1)                                              # 再試行なし。残りのページへも進まない
        self.assertEqual(posts[0]["timeout"], 7.5)                                   # 設定値が /predict の要求まで届く
        self.assertEqual(self.results["completed"], [])                              # 成功・完了扱いにしない
        self.assertEqual(set(self.results["failed"]), {PRODUCT})
        self.assertIn("InferenceRequestError", self.results["failed"][PRODUCT])
        self.assertIn("ReadTimeout", self.results["failed"][PRODUCT])
        self.assertEqual(self.results["skipped"], {})
        # 後始末: 既存の失敗の経路(cleanup_after_exception)が、製番フォルダを出力ルート配下に限って削除する
        removed = [Path(c.args[0]).resolve() for c in self.rmtree.call_args_list]
        self.assertIn(self.product_out.resolve(), removed)
        self.assertTrue(all(str(p).startswith(str(self.output_dir.resolve())) for p in removed))

    def test_unset_passes_timeout_none_to_requests_post_as_before(self):
        (self.tmp / "api").mkdir(exist_ok=True)
        (self.tmp / "api" / "server.yaml").write_text(
            yaml.safe_dump({"host": {"pc_ip": "127.0.0.1", "port": 1}}), encoding="utf-8")
        self.set_api(use_proxy=False)
        posts = []

        def real_with_mock_post(this, dir_product, page_list, api_cnf_path, use_proxy=True, **kw):
            def post(url, **k):
                posts.append(k)
                return SimpleNamespace(status_code=500, text="x", json=lambda: {})
            with mock.patch.object(requests, "post", side_effect=post):
                return REAL_SLICE_PREDICTION_REQUEST(dir_product, page_list, api_cnf_path, use_proxy=use_proxy, **kw)

        with mock.patch.object(type(self), "_fake_slice_prediction_request", real_with_mock_post):
            self.run_main()
        self.assertTrue(posts)
        self.assertTrue(all(k["timeout"] is None for k in posts))                    # 未設定: requests.post には timeout=None(従来どおり待ち続ける)


class ChecksTest(unittest.TestCase):
    def test_config_check_flags_an_invalid_value_and_accepts_unset_or_valid(self):
        from ops import checks
        base = {"data": {"output": "o", "storage": "s", "initials": "A1", "box_types": ["OS"]},
                "api": {"dir_venv": "d", "yaml_file": "server.yaml"},
                "dwg_conversion": {"converter": "c", "cad_version": "v"}}
        self.assertEqual(checks.check_config(base)["status"], "ok")                  # 省略
        ok = {**base, "api": {**base["api"], "request_timeout": 300}}
        self.assertEqual(checks.check_config(ok)["status"], "ok")
        for bad_value in (0, 10 ** 400):                                             # 非常に大きな整数も NG(例外にしない)
            bad = {**base, "api": {**base["api"], "request_timeout": bad_value}}
            res = checks.check_config(bad)
            self.assertEqual(res["status"], "ng")
            self.assertIn("api.request_timeout が不正", res["detail"])


if __name__ == "__main__":
    unittest.main()
