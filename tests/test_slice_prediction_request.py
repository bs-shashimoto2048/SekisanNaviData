# image_mod.slice_prediction_request / log_formater の単体テスト(モックのみ。HTTP通信は発生させない)
# 仕様(Issue #52): 成功時は従来どおりログ文字列を返す。失敗(HTTPエラー・接続失敗・応答不正・検出ファイル未生成・設定不備)は
# InferenceRequestError で上位へ伝える(失敗ページを無視して成功扱いにしない)。
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bs_pkg import image_mod  # noqa: E402
from bs_pkg.image_mod import InferenceRequestError  # noqa: E402

API_YAML = {"host": {"pc_ip": "192.0.2.10", "port": 5555}, "proxy": {"host": "proxy.invalid", "port": 8080}}
API_YAML_NO_PROXY = {"host": {"pc_ip": "127.0.0.1", "port": 5555}}  # proxy 項目なし
IMAGE_YAML = {"processor": {"extention": "png"}}
API_PATH = "dummy_api_server.yaml"


class _Resp:
    def __init__(self, status=200, json_body=None, json_error=False):
        self.status_code = status
        self._body, self._err = json_body, json_error

    def json(self):
        if self._err:
            raise ValueError("not json")
        return self._body


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="slice_req_")
        self.addCleanup(td.cleanup)
        self.dir = td.name
        for p in ("21", "22"):  # API が書いたはずの検出ファイル(既定では存在する)
            Path(self.dir, f"{p}_detected.txt").write_text("", encoding="utf-8")

    def call(self, api_yaml, responses, pages=(21, 22), **kw):
        """load_yaml と requests.post をモックして呼ぶ。実通信・実設定ファイル読込はなし。待機(sleep)も無効化する。"""
        def fake_load(path):
            return api_yaml if path == API_PATH else IMAGE_YAML

        post = mock.Mock(side_effect=responses)
        no_net = mock.Mock(side_effect=AssertionError("実ネットワーク接続"))
        with mock.patch.object(image_mod.config_loader, "load_yaml", side_effect=fake_load), \
                mock.patch.object(image_mod.requests, "post", post), \
                mock.patch.object(image_mod.time, "sleep"), \
                mock.patch.object(socket.socket, "connect", no_net):
            try:
                return image_mod.slice_prediction_request(self.dir, list(pages), API_PATH, **kw), post, None
            except InferenceRequestError as e:
                return None, post, e
            finally:
                assert no_net.call_count == 0


class ProxyTest(Base):
    def test_default_keeps_existing_proxy_behavior(self):
        log, post, err = self.call(API_YAML, [_Resp(200, {"text_file_path": "a"}), _Resp(200, {"text_file_path": "b"})])
        self.assertIsNone(err)
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["proxies"], {"http": "http://proxy.invalid:8080", "https": "https://proxy.invalid:8080"})
            self.assertEqual(call.args[0], "http://192.0.2.10:5555/predict")
            self.assertEqual(call.kwargs["data"]["extension"], "png")
            self.assertIsNone(call.kwargs["timeout"])   # 既定は従来どおり待ち続ける
        self.assertIn("推論完了 -> 'a'", log)

    def test_use_proxy_true_explicit_same_as_default(self):
        _, post, err = self.call(API_YAML, [_Resp(200, {}), _Resp(200, {})], use_proxy=True)
        self.assertIsNone(err)
        self.assertEqual(post.call_args.kwargs["proxies"]["http"], "http://proxy.invalid:8080")

    def test_proxy_disabled_sends_none_and_needs_no_proxy_section(self):
        log, post, err = self.call(API_YAML_NO_PROXY, [_Resp(200, {"text_file_path": "a"})] * 2, use_proxy=False)
        self.assertIsNone(err)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["proxies"], {"http": None, "https": None})

    def test_proxy_enabled_without_proxy_section_is_reported_as_failure(self):
        # 従来は None を返して成功扱いに進んでいた。設定不備は失敗として上位へ伝える。
        log, post, err = self.call(API_YAML_NO_PROXY, [])
        self.assertIsNotNone(err)
        self.assertEqual(err.failed_pages, ["21", "22"])
        self.assertIn("API設定の読み込みに失敗", str(err))
        self.assertEqual(post.call_count, 0)

    def test_none_proxy_overrides_environment_proxy_in_requests(self):
        # 実ライブラリの仕様確認(通信なし): proxies={"http": None} は環境変数のプロキシも無効化する
        env = {"HTTP_PROXY": "http://env-proxy.invalid:3128", "HTTPS_PROXY": "http://env-proxy.invalid:3128"}
        s = requests.Session()
        with mock.patch.dict("os.environ", env, clear=False):
            url = "http://127.0.0.1:5555/predict"
            disabled = s.merge_environment_settings(url, {"http": None, "https": None}, None, None, None)["proxies"]
            enabled = s.merge_environment_settings(url, {}, None, None, None)["proxies"]
        self.assertIsNone(disabled.get("http"))
        self.assertTrue(enabled.get("http"), "環境変数のプロキシが効く前提の確認")

    def test_timeout_is_passed_through_when_given(self):
        _, post, err = self.call(API_YAML_NO_PROXY, [_Resp(200, {})] * 2, use_proxy=False, timeout=12.5)
        self.assertIsNone(err)
        self.assertEqual(post.call_args.kwargs["timeout"], 12.5)


class HttpResultTest(Base):
    def test_success_logs_completion_and_returns_log(self):
        log, post, err = self.call(API_YAML_NO_PROXY, [_Resp(200, {"text_file_path": "D:/x/21_detected.txt"}),
                                                       _Resp(200, {"text_file_path": "D:/x/22_detected.txt"})], use_proxy=False)
        self.assertIsNone(err)
        self.assertIn("推論完了 -> 'D:/x/21_detected.txt'", log)
        self.assertIn("推論完了 -> 'D:/x/22_detected.txt'", log)
        self.assertNotIn("ERR", log)

    def test_http_error_raises_with_failed_pages_and_safe_log(self):
        e500 = {"error": "ERR: 処理失敗 -> boom\nline2 password=hunter2 token: abc123 http://user:pw@host/x"}
        log, post, err = self.call(API_YAML_NO_PROXY, [_Resp(500, e500), _Resp(200, {"text_file_path": "ok"})], use_proxy=False)
        self.assertIsNone(log)                      # 成功ログとして返さない
        self.assertIsNotNone(err)
        self.assertEqual(err.failed_pages, ["21"])  # 失敗ページを特定できる
        self.assertEqual(post.call_count, 2)        # 他ページの依頼は続ける
        text = err.log_text
        self.assertIn("ERR: 推論失敗 -> status=500", text)
        self.assertIn("21.png", text)
        self.assertIn("処理失敗 -> boom", text)
        for secret in ("hunter2", "abc123", "user:pw"):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, str(err))
        self.assertNotIn("\nline2", text)
        self.assertIn("推論完了 -> 'ok'", text)

    def test_all_pages_failing_lists_all(self):
        _, _, err = self.call(API_YAML_NO_PROXY, [_Resp(500, {"error": "x"}), _Resp(404, {})], use_proxy=False)
        self.assertEqual(err.failed_pages, ["21", "22"])

    def test_error_non_json_body_not_echoed(self):
        _, _, err = self.call(API_YAML_NO_PROXY, [_Resp(502, json_error=True), _Resp(404, {"detail": "secret=zzz"})], use_proxy=False)
        self.assertIn("status=502", err.log_text)
        self.assertIn("status=404", err.log_text)
        self.assertNotIn("zzz", err.log_text)

    def test_error_detail_is_truncated(self):
        _, _, err = self.call(API_YAML_NO_PROXY, [_Resp(500, {"error": "x" * 5000}), _Resp(500, {"error": "y"})], use_proxy=False)
        self.assertLess(len(err.log_text), 1500)
        self.assertIn("...(省略)", err.log_text)

    def test_connection_failure_fails_fast_without_leaking_exception_text(self):
        boom = requests.ConnectionError("HTTPConnectionPool(host='10.0.0.1', port=1): password=hunter2")
        _, post, err = self.call(API_YAML_NO_PROXY, [boom], use_proxy=False)
        self.assertEqual(post.call_count, 1)                        # サーバーが使えないので残りは依頼しない
        self.assertEqual(err.failed_pages, ["21", "22"])           # 依頼できなかった分も未完了として伝える
        self.assertIn("接続できません(ConnectionError)", str(err))
        self.assertNotIn("hunter2", err.log_text + str(err))

    def test_timeout_is_reported_as_failure(self):
        _, post, err = self.call(API_YAML_NO_PROXY, [requests.Timeout("slow")], use_proxy=False, timeout=1)
        self.assertIn("Timeout", str(err))
        self.assertEqual(err.failed_pages, ["21", "22"])

    def test_200_but_not_json_is_failure(self):
        _, _, err = self.call(API_YAML_NO_PROXY, [_Resp(200, json_error=True), _Resp(200, {"text_file_path": "ok"})], use_proxy=False)
        self.assertEqual(err.failed_pages, ["21"])
        self.assertIn("JSONではありません", err.log_text)

    def test_200_but_detected_file_missing_is_failure(self):
        Path(self.dir, "22_detected.txt").unlink()
        log, _, err = self.call(API_YAML_NO_PROXY, [_Resp(200, {"text_file_path": "a"})] * 2, use_proxy=False)
        self.assertEqual(err.failed_pages, ["22"])
        self.assertIn("22_detected.txt が見つかりません", err.log_text)

    def test_verify_outputs_can_be_disabled(self):
        Path(self.dir, "22_detected.txt").unlink()
        log, _, err = self.call(API_YAML_NO_PROXY, [_Resp(200, {"text_file_path": "a"})] * 2, use_proxy=False, verify_outputs=False)
        self.assertIsNone(err)

    def test_log_formater_keeps_error_lines(self):
        _, _, err = self.call(API_YAML_NO_PROXY, [_Resp(500, {"error": "boom"}), _Resp(200, {"text_file_path": "ok"})], use_proxy=False)
        formatted = image_mod.log_formater(err.log_text)
        self.assertIn("ERR: 推論失敗 -> status=500", formatted)
        self.assertIn("推論完了 -> 'ok'", formatted)

    def test_app_test_yaml_disables_proxy_but_app_yaml_does_not(self):
        t = yaml.safe_load((ROOT / "app_test.yaml").read_text(encoding="utf-8"))
        a = yaml.safe_load((ROOT / "app.yaml").read_text(encoding="utf-8"))
        self.assertIs(t["api"].get("use_proxy", True), False)
        self.assertIs(a["api"].get("use_proxy", True), True)   # 本番 app.yaml は変更せず、省略=従来どおり使用
        self.assertNotIn("inference_server", a)                # 本番 app.yaml に推論サーバー統合の設定はない(省略=無効)


if __name__ == "__main__":
    unittest.main()
