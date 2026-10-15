# 推論 API の事前確認(pipeline.api_preflight と app.main への組み込み)のテスト。モックだけ。
# 実 API・実ホスト DB・共有・設計データ・CAD には接続しない(app.main 側のテストは MainIntegrationBase の外部呼び出しガードの中で動く)。
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
from pipeline import api_preflight  # noqa: E402
from test_main_integration import PRODUCT, MainIntegrationBase  # noqa: E402

SECRETS = ("10.9.8.7", "secret-host", "proxy.secret", "pw123", "8123")     # 接続先・プロキシ・値(理由やログに出てはいけない)


class PreflightUnitTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="sekisan_pf_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name).resolve()
        self.write_yaml({"host": {"pc_ip": "10.9.8.7", "port": 8123}, "proxy": {"host": "proxy.secret", "port": 3128}})

    def write_yaml(self, data):
        (self.tmp / "server.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def cfg(self, **api):
        return {"api": {"dir_venv": str(self.tmp), "yaml_file": "server.yaml", **api}}

    def check(self, get, **api):
        calls = []

        def _get(url, **kw):
            calls.append((url, kw))
            return get(url, **kw)
        return api_preflight.unavailable_reason(self.cfg(**api), get=_get), calls

    def assert_clean(self, reason):
        for s in SECRETS + (str(self.tmp), "http://", "server.yaml の接続先は"):
            if s == "server.yaml の接続先は":
                continue
            self.assertNotIn(s, reason)
        self.assertNotIn("\n", reason)

    # ---- 使える
    def test_ok_when_health_is_200_and_only_health_is_requested_once(self):
        reason, calls = self.check(lambda u, **kw: SimpleNamespace(status_code=200))
        self.assertIsNone(reason)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith("/health"))
        self.assertNotIn("predict", calls[0][0])                       # /predict は呼ばない

    # ---- 設定
    def test_missing_server_yaml_is_unavailable_and_does_not_connect(self):
        (self.tmp / "server.yaml").unlink()
        reason, calls = self.check(lambda u, **kw: self.fail("接続しない"))
        self.assertIn("server.yaml が見つかりません", reason)
        self.assertEqual(calls, [])
        self.assert_clean(reason)

    def test_unreadable_or_incomplete_server_yaml_is_unavailable_with_type_only(self):
        (self.tmp / "server.yaml").write_text("host: [unclosed\n  secret-host: pw123", encoding="utf-8")
        reason, calls = self.check(lambda u, **kw: self.fail("接続しない"))
        self.assertIn("server.yaml を読み込めません", reason)
        self.assertEqual(calls, [])
        self.assert_clean(reason)
        self.write_yaml({"host": {"pc_ip": "10.9.8.7"}})                 # port が無い
        reason, calls = self.check(lambda u, **kw: self.fail("接続しない"))
        self.assertIn("KeyError", reason)
        self.assert_clean(reason)

    def test_missing_api_settings_in_the_app_config_is_unavailable(self):
        reason = api_preflight.unavailable_reason({"api": {}}, get=lambda *a, **k: self.fail("接続しない"))
        self.assertIn("設定項目を読み込めません", reason)

    # ---- 接続の失敗
    def test_connection_refused_in_all_wrappings_is_guided(self):
        wrapped = requests.exceptions.ConnectionError(
            Exception("(Caused by NewConnectionError: secret-host 10.9.8.7)", ConnectionRefusedError(10061, "secret-host")))
        direct = ConnectionRefusedError("host=secret-host")
        chained = requests.exceptions.ConnectionError("failed secret-host")
        chained.__cause__ = ConnectionRefusedError(10061, "x")
        win = requests.exceptions.ConnectionError(OSError(None, "secret-host", None, 10061))
        for exc in (wrapped, direct, chained, win):
            reason, _ = self.check(lambda u, e=exc, **kw: (_ for _ in ()).throw(e))
            self.assertIn("API が未起動、または server.yaml の接続先が古い可能性", reason)
            self.assertIn("拒否", reason)
            self.assert_clean(reason)

    def test_timeouts_are_reported_as_timeouts(self):
        for exc in (requests.exceptions.ConnectTimeout("secret-host"), requests.exceptions.ReadTimeout("10.9.8.7")):
            reason, calls = self.check(lambda u, e=exc, **kw: (_ for _ in ()).throw(e))
            self.assertIn("タイムアウト", reason)
            self.assertIn("5 秒", reason)                                   # 既定 5 秒
            self.assertEqual(calls[0][1]["timeout"], 5.0)
            self.assert_clean(reason)

    def test_health_timeout_is_configurable(self):
        reason, calls = self.check(lambda u, **kw: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("x")), health_timeout=2)
        self.assertEqual(calls[0][1]["timeout"], 2.0)
        self.assertIn("2 秒", reason)

    def test_other_connection_errors_report_the_type_only(self):
        reason, _ = self.check(lambda u, **kw: (_ for _ in ()).throw(requests.exceptions.ConnectionError("Name or service not known: secret-host")))
        self.assertIn("ConnectionError", reason)
        self.assertNotIn("拒否", reason)                                     # 接続拒否の案内は付けない
        self.assert_clean(reason)
        reason, _ = self.check(lambda u, **kw: (_ for _ in ()).throw(RuntimeError("secret-host pw123")))
        self.assertIn("RuntimeError", reason)
        self.assert_clean(reason)

    def test_abnormal_http_responses_are_unavailable(self):
        for code in (503, 500, 404, 302, 204):
            reason, _ = self.check(lambda u, c=code, **kw: SimpleNamespace(status_code=c))
            self.assertIn(f"HTTP {code}", reason)
            self.assert_clean(reason)

    # ---- プロキシ(実際の /predict と同じ決め方)
    def test_proxy_follows_use_proxy_like_the_predict_request(self):
        _, calls = self.check(lambda u, **kw: SimpleNamespace(status_code=200), use_proxy=False)
        self.assertEqual(calls[0][1]["proxies"], {"http": None, "https": None})
        _, calls = self.check(lambda u, **kw: SimpleNamespace(status_code=200))          # 省略 = 使う(従来どおり)
        self.assertEqual(calls[0][1]["proxies"]["http"], "http://proxy.secret:3128")
        self.write_yaml({"host": {"pc_ip": "10.9.8.7", "port": 8123}})                    # use_proxy なのに proxy 項目が無い
        reason, calls = self.check(lambda u, **kw: self.fail("接続しない"))
        self.assertIn("KeyError", reason)
        self.assertEqual(calls, [])

    def test_reasons_fit_in_a_single_history_line(self):
        reason, _ = self.check(lambda u, **kw: (_ for _ in ()).throw(ConnectionRefusedError("x")))
        from ops.history import sanitize_reason
        self.assertEqual(sanitize_reason(reason), reason)                 # パス・秘密として伏せられない
        self.assertLess(len(reason), 100)


class PreflightInMainTest(MainIntegrationBase):
    """app.main: 推論 API が使えないとき、重い処理(ホスト照会・設計データ・印刷・変換・画像作成)に入らず、失敗として記録して終了する"""
    UNAVAILABLE = "推論 API を利用できないため、製番処理を開始しませんでした(接続が拒否されました)"

    def assert_nothing_heavy_started(self):
        self.assertEqual(self.host_calls, [])                      # ホスト照会(製番一覧・物件情報)もしない
        self.assertEqual(self.print_script_calls, [])
        self.assertEqual(self.convert_calls, [])
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.in_use_calls, [])
        self.assertFalse(self.product_out.exists())                # 製番フォルダ(部分出力)を作らない
        self.assertEqual(self.rmtree.call_count, 0)
        self.assertEqual(self.results["completed"], [])            # 完了扱いにしない
        self.assert_no_external_calls()
        self.assertEqual(len(self.preflight_calls), 1)             # 事前確認は一度だけ

    def test_normal_run_is_a_whole_failure_and_exit_code_is_failure(self):
        self.preflight_reason = self.UNAVAILABLE
        self.run_main()
        self.assert_nothing_heavy_started()
        self.assertEqual(self.results["failed"], {"(全体)": self.UNAVAILABLE})
        self.assertEqual(self.results["skipped"], {})
        with mock.patch.object(app, "main", return_value=self.results):     # 失敗があれば終了コード 1(既存の仕組み)
            self.assertEqual(app._cli(["--config", str(self.config_path)]), 1)

    def test_multiple_products_check_once_and_start_none(self):
        self.preflight_reason = self.UNAVAILABLE
        self.run_main(products=[PRODUCT, "A1BL9999", "A1BL9998"])
        self.assert_nothing_heavy_started()
        self.assertEqual(set(self.results["failed"]), {PRODUCT, "A1BL9999", "A1BL9998"})   # 全製番を失敗として記録
        self.assertTrue(all(v == self.UNAVAILABLE for v in self.results["failed"].values()))

    def test_manual_run_with_a_product_is_failed_not_completed_or_skipped(self):
        self.preflight_reason = self.UNAVAILABLE
        self.run_main(products=[PRODUCT])
        self.assert_nothing_heavy_started()
        self.assertEqual(self.results["failed"], {PRODUCT: self.UNAVAILABLE})
        self.assertEqual(self.results["skipped"], {})

    def test_failure_reason_reaches_the_ops_history_without_secrets(self):
        from ops.history import History
        from ops.runner import RunManager
        from ops.settings_store import SettingsStore
        self.preflight_reason = api_preflight._reason("接続が拒否されました。API が未起動、または server.yaml の接続先が古い可能性があります")
        state = self.tmp / "state"
        state.mkdir()
        hist = History(state)

        def main_func(config_path=None, products=None, output_root=None):    # run_main が config_path を渡すので、ここでは products / output_root だけ中継する
            self.run_main(products=products, output_root=output_root)
            return self.results
        mgr = RunManager(state, SettingsStore(state), hist, main_func, com_init=lambda: None, com_uninit=lambda: None)
        rid, t = mgr.start_manual(PRODUCT, str(self.out_root))
        t.join(60)
        run = hist.runs()[0]
        self.assertEqual(run["status"], "失敗")
        self.assertIn("推論 API を利用できないため", run["reason"])
        self.assertIn("接続が拒否されました", run["reason"])
        self.assertNotIn("http", run["reason"])

    def test_log_has_the_reason_without_url_or_host(self):
        self.preflight_reason = self.UNAVAILABLE
        self.run_main()
        blob = self.stdout.getvalue() + self.stderr.getvalue()
        logs = list((self.out_root / "a_product" / "logs").glob("*.log"))
        for p in logs:
            blob += p.read_text(encoding="utf-8", errors="ignore")
        self.assertIn(self.UNAVAILABLE, blob)
        self.assertNotIn("http://", blob.replace("http://mock", ""))

    def test_available_api_proceeds_to_the_normal_process(self):
        self.run_main()                                            # 既定: 使える
        self.assertEqual(len(self.preflight_calls), 1)
        self.assertEqual(self.results["completed"], [PRODUCT])
        self.assertEqual(self.results["failed"], {})
        self.assertTrue(self.product_out.is_dir())

    def test_integration_mode_does_not_run_the_preflight(self):
        manager = mock.Mock(name="manager")
        with mock.patch.object(app.settings_mod, "build_inference_manager",
                               return_value=(manager, SimpleNamespace(request_timeout=None))):
            try:
                self.run_main()
            except Exception:                                      # 後続(検証用サーバー前提の処理)はモックのため失敗してよい。確認したいのは呼び出しだけ
                pass
        self.assertEqual(self.preflight_calls, [])                 # 統合モードは起動時の /health 確認だけ(従来どおり)
        manager.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
