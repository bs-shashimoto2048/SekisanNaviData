# 運用設定・実行管理画面(ops/)のテスト。合成データとモックだけ。
# 実図面・社内共有・本番 DB・本番 API・IJCAD・ODA は使わない。実タスクスケジューラ(schtasks)・実プロセスの操作もしない
# (スケジューラ操作は runner のモック、subprocess.run は呼ばれたらテスト失敗にするガードを入れている)。
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import yaml  # noqa: E402

from ops import checks, history as history_mod, runner as runner_mod, scheduler as scheduler_mod, server as server_mod  # noqa: E402
from ops.history import COMPLETED, FAILED, History, sanitize_reason  # noqa: E402
from ops.runner import ALL_PRODUCTS, SKIPPED, Busy, RunLock, RunManager  # noqa: E402
from ops.settings_store import OpsSettings, SettingsError, SettingsStore  # noqa: E402
from test_main_integration import PRODUCT, MainIntegrationBase  # noqa: E402

HDR = {"Content-Type": "application/json", "X-Ops-Request": "1"}


class NoRealSubprocess(unittest.TestCase):
    """実タスクスケジューラ・実プロセスを触らない。subprocess.run が呼ばれたら失敗にする。"""

    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="ops_test_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name).resolve()
        self.state = self.tmp / "state"
        self.state.mkdir()
        guard = mock.patch.object(subprocess, "run", side_effect=AssertionError("実プロセス/schtasks を実行しようとした"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)


# ------------------------------------------------------------------ 設定の保存・再読込・入力検証
class SettingsTest(NoRealSubprocess):
    def test_defaults_when_nothing_saved(self):
        self.assertEqual(SettingsStore(self.state).load(), OpsSettings())

    def test_save_and_reload_round_trip_in_a_local_yaml_only(self):
        store = SettingsStore(self.state)
        root = str(self.tmp / "out")
        saved = store.save({"output_root": root, "schedule_enabled": True, "schedule_time": "08:30", "schedule_interval_days": 3})
        self.assertEqual(saved, OpsSettings(root, True, "08:30", 3))
        self.assertEqual(SettingsStore(self.state).load(), saved)                 # 別インスタンス(再起動相当)でも同じ
        self.assertEqual([p.name for p in self.state.iterdir()], ["ops.local.yaml"])   # 保存先はローカル設定だけ(一時ファイルも残さない)
        self.assertEqual(yaml.safe_load((self.state / "ops.local.yaml").read_text(encoding="utf-8")),
                         {"output_root": root, "schedule_enabled": True, "schedule_time": "08:30", "schedule_interval_days": 3})

    def test_tracked_app_yaml_is_never_touched(self):
        before = hashlib.sha256((ROOT / "app.yaml").read_bytes()).hexdigest()
        SettingsStore(self.state).save({"output_root": "", "schedule_enabled": False, "schedule_time": "09:00"})
        self.assertEqual(hashlib.sha256((ROOT / "app.yaml").read_bytes()).hexdigest(), before)

    def test_invalid_inputs_are_rejected_with_a_message_and_nothing_is_saved(self):
        store = SettingsStore(self.state)
        bad = [
            {"output_root": "relative/dir", "schedule_time": "08:00"},        # 相対パス
            {"output_root": "<SHARE_ROOT>", "schedule_time": "08:00"},        # サンプルのプレースホルダ
            {"output_root": str(self.tmp), "schedule_time": "25:00"},         # 時刻が範囲外
            {"output_root": str(self.tmp), "schedule_time": "8:00"},          # 形式が違う
            {"output_root": str(self.tmp), "schedule_time": "08:60"},
            {"output_root": str(self.tmp), "schedule_enabled": "yes", "schedule_time": "08:00"},   # 真偽値でない
            {"output_root": "\\x", "schedule_time": "08:00"},                    # ドライブ名なしの "\x"
            {"output_root": "C:x", "schedule_time": "08:00"},                    # ドライブ相対
            {"output_root": str(self.tmp) + "\\..\\other", "schedule_time": "08:00"},   # ".." を含む
            {"schedule_time": "08:00", "schedule_interval_days": 0},             # 間隔が範囲外
            {"schedule_time": "08:00", "schedule_interval_days": 31},
            {"schedule_time": "08:00", "schedule_interval_days": "2"},           # 整数でない
            {"schedule_time": "08:00", "schedule_interval_days": True},
        ]
        for data in bad:
            with self.assertRaises(SettingsError, msg=str(data)):
                store.save(data)
        self.assertFalse((self.state / "ops.local.yaml").exists())
        with self.assertRaises(SettingsError):
            store.save("not a dict")                                           # type: ignore[arg-type]

    def test_corrupt_saved_file_is_reported_not_crashed(self):
        (self.state / "ops.local.yaml").write_text("a: [unclosed", encoding="utf-8")
        with self.assertRaises(SettingsError):
            SettingsStore(self.state).load()

    def test_default_interval_is_every_other_day_and_unc_roots_are_accepted(self):
        self.assertEqual(OpsSettings().schedule_interval_days, 2)                     # 従来の「1 日置き」
        self.assertEqual(SettingsStore(self.state).save({"schedule_time": "08:00"}).schedule_interval_days, 2)
        self.assertEqual(SettingsStore(self.state).save({"output_root": "\\\\srv\\share\\out", "schedule_time": "08:00"}).output_root,
                         "\\\\srv\\share\\out")
        self.assertEqual(SettingsStore(self.state).save({"schedule_time": "08:00", "schedule_interval_days": 1}).schedule_interval_days, 1)

    def test_blank_output_root_means_use_the_app_setting(self):
        self.assertEqual(SettingsStore(self.state).save({"output_root": "  ", "schedule_time": "07:05"}).output_root, "")


# ------------------------------------------------------------------ 履歴
class HistoryTest(NoRealSubprocess):
    def test_sanitize_hides_paths_and_secrets_and_truncates(self):
        text = r"失敗 C:\Users\someone\secret dir\file.csv と \\fileserver\share\x と /var/data/a/b password=abc123 token: xyz"
        out = sanitize_reason(text)
        for leak in ("someone", "fileserver", "/var/data", "abc123", "xyz", "C:\\"):
            self.assertNotIn(leak, out)
        self.assertIn("<パス>", out)
        self.assertEqual(len(sanitize_reason("あ" * 1000)), history_mod.MAX_REASON)
        spaced = sanitize_reason(r"読めません: C:\Program Files\Some Company\data dir\file.db(CP932)")   # フォルダ名に空白があっても全体を伏せる
        for leak in ("Program", "Some", "Company", "data dir", "file.db"):
            self.assertNotIn(leak, spaced)
        self.assertIn("読めません", spaced)
        self.assertNotIn("\n", sanitize_reason("a\nb\r\nc"))

    def test_start_then_finish_is_one_row_with_the_final_status(self):
        h = History(self.state, now=lambda: "2026-10-14 09:00:00")
        rid = h.start("手動", "A1TEST0001")
        self.assertEqual(h.runs()[0]["status"], "実行中")
        h.finish(rid, FAILED, r"design.csv を作成できません: C:\x\y")
        runs = h.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["kind"], runs[0]["product"], runs[0]["status"]), ("手動", "A1TEST0001", "失敗"))
        self.assertNotIn("C:\\", runs[0]["reason"])
        self.assertEqual(runs[0]["started"], "2026-10-14 09:00:00")

    def test_newest_first_and_broken_lines_are_skipped(self):
        h = History(self.state)
        a = h.start("手動", "A1TEST0001"); h.finish(a, COMPLETED)
        b = h.start("定期", ALL_PRODUCTS); h.finish(b, COMPLETED)
        with open(h.path, "a", encoding="utf-8") as f:
            f.write("{broken json\n")
        self.assertEqual([r["id"] for r in h.runs()], [b, a])

    def test_orphan_running_records_become_failed(self):
        h = History(self.state)
        rid = h.start("手動", "A1TEST0001")
        self.assertEqual(h.mark_orphans_failed(), 1)
        run = h.runs()[0]
        self.assertEqual((run["id"], run["status"]), (rid, "失敗"))
        self.assertIn("途中で終了", run["reason"])

    def test_history_file_is_a_local_ignored_name(self):
        self.assertTrue(history_mod.HISTORY_FILE.endswith(".local.jsonl"))
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(history_mod.HISTORY_FILE, ignore)
        self.assertIn(runner_mod.LOCK_FILE, ignore)


# ------------------------------------------------------------------ 動作チェック
class ChecksTest(NoRealSubprocess):
    def config(self, **over):
        cfg = {"data": {"output": str(self.tmp / "out"), "storage": "a_product", "initials": "A1", "box_types": ["OS"]},
               "api": {"dir_venv": str(self.tmp / "api"), "yaml_file": "api.yaml"},
               "dwg_conversion": {"converter": str(self.tmp / "conv.exe"), "cad_version": "ACAD2010"}}
        cfg.update(over)
        return cfg

    def run_it(self, cfg, settings=OpsSettings(), **kw):
        base = dict(load_config=lambda p: cfg, design_check=lambda: (True, "参照先 2 件中 2 件に到達できました"),
                    host_check=lambda initials: (True, "読み取り照会に成功しました"), health_url=lambda c: "http://127.0.0.1:1/health",
                    get=lambda url: (200, "{}"))
        base.update(kw)
        return {r["name"]: r for r in checks.run_checks("dummy.yaml", settings, **base)}

    def test_all_ok_with_a_good_synthetic_setup(self):
        (self.tmp / "out").mkdir()
        (self.tmp / "conv.exe").write_bytes(b"x")
        res = self.run_it(self.config())
        self.assertEqual({n: r["status"] for n, r in res.items()},
                         {"設定の形式": "ok", "出力先": "ok", "変換ソフト": "ok", "設計サーバー参照先": "ok",
                          "ホスト DB(読み取り専用)": "ok", "推論 API(/health)": "ok"})

    def test_missing_and_sample_config_are_ng(self):
        cfg = self.config(dwg_conversion={"converter": "<CONVERTER_DIR>/x.exe"})
        res = self.run_it(cfg)
        self.assertEqual(res["設定の形式"]["status"], "ng")
        self.assertIn("dwg_conversion.cad_version", res["設定の形式"]["detail"])
        self.assertIn("dwg_conversion.converter", res["設定の形式"]["detail"])

    def test_unreadable_config_returns_one_ng_item(self):
        def boom(p):
            raise OSError("no such file")
        res = checks.run_checks("x", OpsSettings(), load_config=boom)
        self.assertEqual([r["status"] for r in res], ["ng"])

    def _host_check_with_mocks(self, query, init=None):
        events = []
        with mock.patch("ops.com.co_initialize", side_effect=init or (lambda: events.append("init"))), \
                mock.patch("ops.com.co_uninitialize", side_effect=lambda: events.append("uninit")), \
                mock.patch("bs_pkg.host_mod.create_n3_design_list", side_effect=lambda ini: events.append("query") or query(ini)):
            try:
                result = checks.default_host_check("A1")
            except Exception as e:  # noqa: BLE001
                result = e
        return events, result

    def test_default_host_check_initializes_com_then_queries_then_uninitializes(self):
        events, result = self._host_check_with_mocks(lambda ini: ["A1X", "A1Y"])     # 実 DB・実 COM には触れない
        self.assertEqual(events, ["init", "query", "uninit"])
        self.assertEqual(result[0], True)
        self.assertIn("2 件", result[1])

    def test_default_host_check_uninitializes_com_even_when_the_query_fails(self):
        def boom(ini):
            raise RuntimeError("照会失敗")
        events, result = self._host_check_with_mocks(boom)
        self.assertEqual(events, ["init", "query", "uninit"])
        self.assertIsInstance(result, RuntimeError)

    def test_default_host_check_does_not_query_or_uninitialize_when_com_init_fails(self):
        def bad():
            raise OSError("init 失敗")
        events, result = self._host_check_with_mocks(lambda ini: [], init=bad)
        self.assertEqual(events, [])                                                 # 照会せず、初期化していないので終了もしない
        self.assertIsInstance(result, OSError)

    def test_output_root_uses_the_ops_setting_over_the_app_setting_and_is_not_created(self):
        mine = self.tmp / "mine"
        res = self.run_it(self.config(), OpsSettings(output_root=str(mine)))
        self.assertEqual(res["出力先"]["status"], "ok")            # 未作成だが親があり書ける → 実行時に作成
        self.assertFalse(mine.exists())                             # 動作チェックは何も作らない
        res = self.run_it(self.config(), OpsSettings(output_root=str(self.tmp / "nope" / "deeper")))
        self.assertEqual(res["出力先"]["status"], "ng")

    def test_missing_converter_is_ng(self):
        self.assertEqual(self.run_it(self.config())["変換ソフト"]["status"], "ng")

    def test_one_failing_check_does_not_stop_the_others_and_has_no_secrets(self):
        def host_fail(initials):
            raise ConnectionError("host=secret-host password=pw123")
        res = self.run_it(self.config(), host_check=host_fail)
        self.assertEqual(res["ホスト DB(読み取り専用)"]["status"], "ng")
        self.assertNotIn("secret-host", json.dumps(res, ensure_ascii=False))
        self.assertNotIn("pw123", json.dumps(res, ensure_ascii=False))
        self.assertEqual(res["推論 API(/health)"]["status"], "ok")

    def test_inference_api_is_only_asked_for_health_never_predict(self):
        urls = []
        self.run_it(self.config(), health_url=lambda c: "http://127.0.0.1:9/health", get=lambda url: urls.append(url) or (200, ""))
        self.assertEqual(urls, ["http://127.0.0.1:9/health"])
        self.assertTrue(all(u.endswith("/health") for u in urls))
        src = (ROOT / "ops" / "checks.py").read_text(encoding="utf-8")
        self.assertNotIn("/predict\"", src.replace("/predict は呼ばない", ""))   # コードとして /predict を組み立てない

    def test_health_failure_or_non_200_is_ng_and_unconfigured_is_skipped(self):
        self.assertEqual(self.run_it(self.config(), get=lambda u: (503, ""))["推論 API(/health)"]["status"], "ng")

        def refuse(u):
            raise ConnectionRefusedError("x")
        self.assertEqual(self.run_it(self.config(), get=refuse)["推論 API(/health)"]["status"], "ng")
        self.assertEqual(self.run_it(self.config(), health_url=lambda c: None)["推論 API(/health)"]["status"], "skip")

    def test_health_connection_refused_is_guided_and_other_errors_are_not(self):
        hint = "API が未起動、または server.yaml の接続先が古い可能性"
        secret = "host=secret-host path=C:\\secret password=pw123"

        def refused_direct(u):
            raise ConnectionRefusedError(secret)

        def refused_urlerror(u):                        # urllib は ConnectionRefusedError を URLError.reason に包む
            raise urllib.error.URLError(ConnectionRefusedError(10061, secret))

        def refused_winerror(u):
            raise urllib.error.URLError(OSError(None, secret, None, 10061))

        def timed_out(u):
            raise TimeoutError(secret)

        def other_urlerror(u):
            raise urllib.error.URLError(OSError(secret))

        for fn in (refused_direct, refused_urlerror, refused_winerror):
            item = self.run_it(self.config(), get=fn)["推論 API(/health)"]
            self.assertEqual(item["status"], "ng")                                 # 判定は従来どおり NG
            self.assertIn(hint, item["detail"])
            self.assertNotIn("secret", item["detail"])                             # 接続先・メッセージは出さない
            self.assertNotIn("pw123", item["detail"])
        for fn in (timed_out, other_urlerror):
            item = self.run_it(self.config(), get=fn)["推論 API(/health)"]
            self.assertEqual(item["status"], "ng")
            self.assertNotIn(hint, item["detail"])                                 # 接続拒否以外には案内を付けない
            self.assertIn("状態確認に失敗しました", item["detail"])
            self.assertNotIn("secret", item["detail"])
        self.assertEqual(self.run_it(self.config(), get=lambda u: (503, ""))["推論 API(/health)"]["detail"], "状態確認の応答コード 503")

    def test_checks_never_start_a_product_run(self):
        main_func = mock.Mock(name="app.main")
        with mock.patch.object(runner_mod.RunManager, "start_manual", side_effect=AssertionError("製番処理は呼ばない")):
            self.run_it(self.config())
        main_func.assert_not_called()


# ------------------------------------------------------------------ タスクスケジューラ連携(モック)
class SchedulerTest(NoRealSubprocess):
    def make(self, returncode=0, stdout=b"", stderr=b""):
        calls = []

        def fake(args):
            calls.append(list(args))
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        return scheduler_mod.TaskScheduler("C:/py/python.exe", "C:/repo/ops_console.py", runner=fake), calls

    def test_register_creates_a_daily_task_that_runs_the_normal_process_then_sets_enabled(self):
        sch, calls = self.make()
        sch.register("08:30", 2, True)
        create, change = calls
        self.assertEqual(create[:2], ["schtasks", "/Create"])
        self.assertEqual(create[create.index("/SC") + 1], "DAILY")
        self.assertEqual(create[create.index("/MO") + 1], "2")                 # 1 日置き(従来の運用間隔)
        self.assertEqual(create[create.index("/ST") + 1], "08:30")
        self.assertIn("/F", create)
        tr = create[create.index("/TR") + 1]
        self.assertIn("ops_console.py", tr)
        self.assertTrue(tr.endswith("run-scheduled"))                    # 製番の指定なしの通常処理
        self.assertEqual(change[-1], "/ENABLE")
        self.assertEqual(create[create.index("/TN") + 1], scheduler_mod.DEFAULT_TASK_NAME)

    def test_register_disabled_and_update_use_the_same_create(self):
        sch, calls = self.make()
        sch.register("07:00", 1, False)
        self.assertEqual(calls[1][-1], "/DISABLE")
        self.assertEqual(calls[0][calls[0].index("/MO") + 1], "1")             # 毎日にも設定できる
        sch.register("09:15", 3, True)                                     # 更新 = /F で上書き
        self.assertEqual(calls[2][calls[2].index("/ST") + 1], "09:15")
        self.assertEqual(calls[2][calls[2].index("/MO") + 1], "3")

    def test_delete_and_failure_messages(self):
        sch, calls = self.make()
        sch.delete()
        self.assertEqual(calls[0][:2], ["schtasks", "/Delete"])
        bad, _ = self.make(returncode=1, stderr="アクセスが拒否されました。".encode("cp932"))
        with self.assertRaises(scheduler_mod.SchedulerError) as ctx:
            bad.register("08:00", 2, True)
        self.assertIn("登録・更新に失敗", str(ctx.exception))
        self.assertNotIn("C:/", str(ctx.exception))

    def test_status_parses_the_xml_and_is_read_only(self):
        xml = ('<?xml version="1.0" encoding="UTF-16"?><Task><Triggers><CalendarTrigger><StartBoundary>2026-10-14T08:30:00'
               '</StartBoundary><ScheduleByDay><DaysInterval>2</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>'
               '<Settings><Enabled>false</Enabled></Settings></Task>')
        sch, calls = self.make(stdout=xml.encode("utf-16"))
        self.assertEqual(sch.status(), {"registered": True, "enabled": False, "time": "08:30", "interval_days": 2})
        self.assertEqual([c[1] for c in calls], ["/Query"])
        missing, _ = self.make(returncode=1)
        self.assertEqual(missing.status(), {"registered": False, "enabled": None, "time": None, "interval_days": None})

    def test_too_long_action_is_refused_before_calling_schtasks(self):
        calls = []
        long_sch = scheduler_mod.TaskScheduler("C:/" + "d" * 300 + "/python.exe", "C:/repo/ops_console.py",
                                               runner=lambda a: calls.append(a) or SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
        with self.assertRaises(scheduler_mod.SchedulerError) as ctx:
            long_sch.register("08:00", 2, True)
        self.assertIn("長すぎ", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_task_action_quotes_paths_with_spaces_and_the_config_argument(self):
        sch = scheduler_mod.TaskScheduler(r"C:\Program Files\Py\python.exe", r"D:\my repo\ops_console.py", extra_args=("--config", '"D:\\my repo\\app.local.yaml"'))
        action = sch.action()
        self.assertTrue(action.startswith('"C:\\Program Files\\Py\\python.exe" "D:\\my repo\\ops_console.py" run-scheduled'))
        self.assertIn('--config "D:\\my repo\\app.local.yaml"', action)

    def test_default_runner_is_the_only_real_call_and_tests_never_use_it(self):
        sch = scheduler_mod.TaskScheduler("p", "s")
        self.assertIs(sch.runner, scheduler_mod.default_runner)
        self.guard.assert_not_called()


# ------------------------------------------------------------------ 実行管理(指定製番・二重起動・失敗記録)
class RunnerTest(NoRealSubprocess):
    def make(self, main_func, **com):
        store = SettingsStore(self.state)
        hist = History(self.state)
        return RunManager(self.state, store, hist, main_func, config_path="cfg.yaml", **com), hist

    def test_com_is_initialized_in_the_worker_thread_around_main_and_always_uninitialized(self):
        import threading
        events = []
        tid = lambda: threading.current_thread().ident  # noqa: E731

        def fake_main(**kw):
            events.append(("main", tid()))
            raise RuntimeError("途中で失敗")
        mgr, hist = self.make(fake_main, com_init=lambda: events.append(("init", tid())),
                              com_uninit=lambda: events.append(("uninit", tid())))
        mgr.start_manual("A1TEST0001")[1].join(5)
        self.assertEqual([e[0] for e in events], ["init", "main", "uninit"])    # main の前に初期化、失敗しても終了する
        self.assertEqual(len({e[1] for e in events}), 1)                       # 3 つとも同じ worker スレッド
        self.assertNotEqual(events[0][1], threading.current_thread().ident)
        self.assertEqual(hist.runs()[0]["status"], "失敗")

    def test_com_init_failure_does_not_run_main_and_is_recorded_as_failed(self):
        ran, un = [], []

        def bad_init():
            raise OSError(r"C:\secret\x")
        mgr, hist = self.make(lambda **kw: ran.append(1) or {}, com_init=bad_init, com_uninit=lambda: un.append(1))
        mgr.start_manual("A1TEST0001")[1].join(5)
        self.assertEqual(ran, [])
        self.assertEqual(un, [])                                               # 初期化していないので終了もしない
        run = hist.runs()[0]
        self.assertEqual(run["status"], "失敗")
        self.assertIn("COM を初期化できない", run["reason"])
        self.assertNotIn("secret", run["reason"])
        self.assertFalse(mgr.status()["running"])

    def test_manual_run_calls_main_with_that_one_product_and_records_completed(self):
        calls = []

        def fake_main(**kw):
            calls.append(kw)
            return {"completed": [kw["products"][0]], "failed": {}, "skipped": {}}
        mgr, hist = self.make(fake_main)
        root = str(self.tmp / "out")
        rid, t = mgr.start_manual("A1TEST0001", root)
        t.join(5)
        self.assertEqual(calls, [{"config_path": "cfg.yaml", "products": ["A1TEST0001"], "output_root": root}])
        run = hist.runs()[0]
        self.assertEqual((run["id"], run["kind"], run["product"], run["status"]), (rid, "手動", "A1TEST0001", "完了"))
        self.assertFalse(mgr.status()["running"])                          # 終わったらロックを解放

    def test_saved_output_root_is_used_when_none_given_and_omitted_when_both_empty(self):
        seen = []
        mgr, _ = self.make(lambda **kw: seen.append(kw) or {"completed": ["A1TEST0001"], "failed": {}, "skipped": {}})
        mgr.start_manual("A1TEST0001")[1].join(5)
        self.assertNotIn("output_root", seen[0])                           # 指定なし → app の設定どおり
        mgr.store.save({"output_root": str(self.tmp / "saved"), "schedule_time": "08:00"})
        mgr.start_manual("A1TEST0001")[1].join(5)
        self.assertEqual(seen[1]["output_root"], str(self.tmp / "saved"))

    def test_failed_product_is_recorded_as_failed_with_reason_never_completed(self):
        mgr, hist = self.make(lambda **kw: {"completed": [], "failed": {"A1TEST0001": "RuntimeError: design.csv を作成できません"}, "skipped": {}})
        mgr.start_manual("A1TEST0001")[1].join(5)
        run = hist.runs()[0]
        self.assertEqual(run["status"], "失敗")
        self.assertIn("design.csv を作成できません", run["reason"])

    def test_exception_from_main_is_a_failure_and_releases_the_lock(self):
        def boom(**kw):
            raise RuntimeError(r"サンプル設定です C:\secret\app.local.yaml")
        mgr, hist = self.make(boom)
        mgr.start_manual("A1TEST0001")[1].join(5)
        run = hist.runs()[0]
        self.assertEqual(run["status"], "失敗")
        self.assertNotIn("secret", run["reason"])
        self.assertFalse(mgr.status()["running"])

    def test_system_exit_from_main_is_a_fixed_failure_never_left_running_or_completed(self):
        for code in (None, 1, r"C:\secret\app.local.yaml password=abc"):
            events = []
            def boom(_c=code, **kw):
                raise SystemExit(_c)
            mgr, hist = self.make(boom, com_init=lambda: events.append("init"), com_uninit=lambda: events.append("uninit"))
            mgr.start_manual("A1TEST0001")[1].join(5)
            run = hist.runs()[0]
            self.assertEqual(run["status"], "失敗", code)                        # 「実行中」のまま残さない・完了にしない
            self.assertTrue(run.get("finished"), code)
            self.assertEqual(run["reason"], "処理が途中で終了しました(SystemExit)", code)   # 終了コード・メッセージは出さない
            self.assertEqual(events, ["init", "uninit"], code)                  # COM の終了処理は従来どおり
            self.assertFalse(mgr.status()["running"], code)                     # ロックも解放
            self.assertIsNone(mgr.current)
            for f in self.state.iterdir():
                if f.suffix == ".jsonl":
                    f.unlink()

    def test_system_exit_in_scheduled_run_is_recorded_as_failed_and_returns_failed(self):
        def boom(**kw):
            raise SystemExit(r"\\server\share secret")
        mgr, hist = self.make(boom)
        self.assertEqual(mgr.run_scheduled(), FAILED)                          # run-scheduled の終了コード 1 になる
        run = hist.runs()[0]
        self.assertEqual((run["kind"], run["status"]), ("定期", "失敗"))
        self.assertTrue(run.get("finished"))
        self.assertNotIn("server", run["reason"])
        self.assertFalse(mgr.status()["running"])

    def test_skipped_product_is_not_reported_as_completed(self):
        mgr, hist = self.make(lambda **kw: {"completed": [], "failed": {}, "skipped": {"A1TEST0001": "処理済み"}})
        mgr.start_manual("A1TEST0001")[1].join(5)
        self.assertEqual(hist.runs()[0]["status"], SKIPPED)
        self.assertIn("処理済み", hist.runs()[0]["reason"])

    def test_invalid_product_numbers_are_rejected_before_anything_starts(self):
        mgr, hist = self.make(lambda **kw: {})
        for bad in ("", "  ", "a b", "A1/../x", "A1", "A1TEST;rm", None):
            with self.assertRaises(SettingsError, msg=str(bad)):
                mgr.start_manual(bad)
        self.assertEqual(hist.runs(), [])
        self.assertFalse(mgr.status()["running"])

    def test_relative_output_root_is_rejected(self):
        mgr, _ = self.make(lambda **kw: {})
        with self.assertRaises(SettingsError):
            mgr.start_manual("A1TEST0001", "relative")

    def test_double_start_is_refused_while_running(self):
        gate, started = threading.Event(), threading.Event()

        def slow(**kw):
            started.set()
            gate.wait(5)
            return {"completed": ["A1TEST0001"], "failed": {}, "skipped": {}}
        mgr, hist = self.make(slow)
        _, t = mgr.start_manual("A1TEST0001")
        self.assertTrue(started.wait(5))
        self.assertTrue(mgr.status()["running"])
        with self.assertRaises(Busy):
            mgr.start_manual("A1TEST0002")
        self.assertEqual(len(hist.runs()), 1)                              # 拒否した分は履歴を増やさない
        gate.set()
        t.join(5)
        mgr.start_manual("A1TEST0002")[1].join(5)                          # 終われば次を開始できる
        self.assertEqual(len(hist.runs()), 2)

    def test_lock_excludes_a_second_holder_and_is_released_on_release(self):
        mine, theirs = RunLock(self.state), RunLock(self.state)         # 別インスタンス(=別ハンドル)でも OS のロックで競合する
        mine.acquire("scheduled")
        self.assertTrue(theirs.is_held())
        with self.assertRaises(Busy):
            theirs.acquire("manual")
        with self.assertRaises(Busy):
            mine.acquire("again")                                          # 同じ持ち主の再取得も二重起動として拒否
        mine.release()
        self.assertFalse(theirs.is_held())
        theirs.acquire("manual")
        theirs.release()

    def test_lock_is_shared_with_a_real_other_process_and_freed_when_it_dies(self):
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]);\n"
                "from ops.runner import RunLock\n"
                "l = RunLock(sys.argv[2]); l.acquire('child'); print('locked', flush=True); time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(self.state)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            mine = RunLock(self.state)
            self.assertTrue(mine.is_held())
            with self.assertRaises(Busy):
                mine.acquire("scheduled")                                  # タスクスケジューラ側(別プロセス)が実行中なら画面からは開始できない
        finally:
            proc.kill()                                                    # 異常終了を模擬(ロックの解放処理は走らない)
            proc.wait(10)
        mine = RunLock(self.state)
        for _ in range(60):                                                # OS がロックを解放するまで、ごく短い遅れがありうる(最大 3 秒待つ)
            if not mine.is_held():
                break
            time.sleep(0.05)
        self.assertFalse(mine.is_held())                                   # 持ち主が死ねば、ロックファイルが残っていても解放される(古いロックにならない)
        mine.acquire("manual")
        mine.release()

    def test_leftover_lock_file_without_a_holder_does_not_block(self):
        (self.state / runner_mod.LOCK_FILE).write_text("999999 manual\n", encoding="utf-8")     # 前の版・異常終了の残りファイル
        lock = RunLock(self.state)
        self.assertFalse(lock.is_held())
        lock.acquire("manual")
        lock.release()

    def test_status_polling_does_not_make_a_real_start_fail(self):
        lock = RunLock(self.state)
        stop = threading.Event()
        poller = threading.Thread(target=lambda: [RunLock(self.state).is_held() for _ in iter(stop.is_set, True)], daemon=True)
        poller.start()
        try:
            for _ in range(20):
                lock.acquire("manual")                                     # 状態確認が一瞬ロックを取っても、再試行で取得できる
                lock.release()
        finally:
            stop.set()
            poller.join(5)

    def test_scheduled_run_calls_the_normal_process_without_products(self):
        calls = []
        mgr, hist = self.make(lambda **kw: calls.append(kw) or {"completed": ["A1X", "A1Y"], "failed": {}, "skipped": {"A1Z": "処理済み"}})
        self.assertEqual(mgr.run_scheduled(), COMPLETED)
        self.assertEqual(calls, [{"config_path": "cfg.yaml"}])             # 製番・出力先の指定なし = 既存の通常処理そのまま
        run = hist.runs()[0]
        self.assertEqual((run["kind"], run["product"], run["status"]), ("定期", ALL_PRODUCTS, "完了"))

    def test_scheduled_run_with_a_failed_product_is_failed(self):
        mgr, hist = self.make(lambda **kw: {"completed": ["A1X"], "failed": {"A1Y": "RuntimeError: material.csv を作成できません"}, "skipped": {}})
        self.assertEqual(mgr.run_scheduled(), FAILED)
        self.assertIn("失敗 1 件", hist.runs()[0]["reason"])

    def test_scheduled_run_initializes_com_around_main_only(self):
        events = []                                                          # app.main はモック。実タスク・DB・COM は使わない
        mgr, hist = self.make(lambda **kw: events.append("main") or {"completed": ["A1X"], "failed": {}, "skipped": {}},
                              com_init=lambda: events.append("init"), com_uninit=lambda: events.append("uninit"))
        self.assertEqual(mgr.run_scheduled(), COMPLETED)
        self.assertEqual(events, ["init", "main", "uninit"])
        self.assertFalse(mgr.status()["running"])

    def test_scheduled_run_with_com_init_failure_is_failed_and_releases_the_lock(self):
        ran = []

        def bad():
            raise OSError("init 失敗")
        mgr, hist = self.make(lambda **kw: ran.append(1) or {}, com_init=bad, com_uninit=lambda: ran.append("un"))
        self.assertEqual(mgr.run_scheduled(), FAILED)
        self.assertEqual(ran, [])
        self.assertIn("COM を初期化できない", hist.runs()[0]["reason"])
        self.assertFalse(mgr.status()["running"])

    def test_com_uninit_failure_does_not_change_the_recorded_result(self):
        def bad_un():
            raise OSError("uninit 失敗")
        mgr, hist = self.make(lambda **kw: {"completed": ["A1TEST0001"], "failed": {}, "skipped": {}},
                              com_init=lambda: None, com_uninit=bad_un)
        mgr.start_manual("A1TEST0001")[1].join(5)
        self.assertEqual(hist.runs()[0]["status"], "完了")                   # 終了処理の失敗で完了を失敗にしない
        self.assertFalse(mgr.status()["running"])
        mgr2, hist2 = self.make(lambda **kw: (_ for _ in ()).throw(RuntimeError("main 失敗")), com_init=lambda: None, com_uninit=bad_un)
        mgr2.start_manual("A1TEST0001")[1].join(5)
        self.assertEqual(hist2.runs()[0]["status"], "失敗")
        self.assertIn("main 失敗", hist2.runs()[0]["reason"])                # 元の失敗理由が終了処理の失敗で上書きされない

    def _dry_run_mgr(self, events, load_error=None, init=None, uninit=None):
        main = mock.Mock(name="app.main")                                    # 呼ばれたら失敗にする
        mgr, hist = self.make(main, com_init=init or (lambda: events.append("init")),
                              com_uninit=uninit or (lambda: events.append("uninit")))
        if load_error is not None:
            mgr.store.load = mock.Mock(side_effect=load_error)
        else:
            real = mgr.store.load
            mgr.store.load = mock.Mock(side_effect=lambda: events.append("load") or real())
        return mgr, hist, main

    def test_dry_run_checks_lock_com_settings_history_without_calling_main(self):
        events = []
        mgr, hist, main = self._dry_run_mgr(events)
        self.assertEqual(mgr.run_scheduled_dry_run(), COMPLETED)
        main.assert_not_called()                                              # app.main は呼ばない
        self.assertEqual(events, ["init", "load", "uninit"])                  # COM の初期化 → 設定の読込 → 終了
        self.assertFalse(mgr.status()["running"])                             # ロックを解放
        runs = hist.runs()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run["kind"], run["product"], run["status"]), ("定期(検証)", "(検証のみ)", "完了"))
        self.assertNotEqual(run["kind"], "定期")                                # 通常の定期処理と区別できる
        self.assertIn("製番処理は実行していません", run["reason"])

    def test_dry_run_records_no_paths_settings_or_products(self):
        events = []
        mgr, hist, _ = self._dry_run_mgr(events)
        mgr.store.save({"output_root": str(self.tmp / "secret-out"), "schedule_time": "08:00"})
        mgr.run_scheduled_dry_run()
        text = hist.path.read_text(encoding="utf-8")
        self.assertNotIn("secret-out", text)                                  # 出力先・設定値は記録しない
        self.assertNotIn("08:00", text)
        self.assertNotIn(str(self.tmp), text)

    def test_dry_run_while_busy_is_skipped_without_com_settings_or_main(self):
        events = []
        mgr, hist, main = self._dry_run_mgr(events)
        mgr.lock.acquire("manual")
        self.assertEqual(mgr.run_scheduled_dry_run(), SKIPPED)
        self.assertEqual(events, [])
        main.assert_not_called()
        run = hist.runs()[0]
        self.assertEqual((run["kind"], run["status"]), ("定期(検証)", SKIPPED))
        self.assertIn("二重起動", run["reason"])
        self.assertTrue(mgr.status()["running"])                              # 他の持ち主のロックは解放しない
        mgr.lock.release()

    def test_dry_run_com_init_failure_is_failed_and_releases_the_lock(self):
        events = []

        def bad():
            raise OSError(r"C:\secret\x")
        mgr, hist, main = self._dry_run_mgr(events, init=bad)
        self.assertEqual(mgr.run_scheduled_dry_run(), FAILED)
        self.assertEqual(events, [])                                          # 初期化していないので終了もしない
        main.assert_not_called()
        run = hist.runs()[0]
        self.assertIn("COM を初期化できません", run["reason"])
        self.assertNotIn("secret", run["reason"])
        self.assertFalse(mgr.status()["running"])

    def test_dry_run_settings_error_is_failed_but_still_uninitializes_com_and_releases_the_lock(self):
        events = []
        mgr, hist, main = self._dry_run_mgr(events, load_error=SettingsError(r"C:\secret\ops.local.yaml が壊れています"))
        self.assertEqual(mgr.run_scheduled_dry_run(), FAILED)
        self.assertEqual(events, ["init", "uninit"])
        main.assert_not_called()
        run = hist.runs()[0]
        self.assertIn("ローカル設定を読み込めませんでした(SettingsError)", run["reason"])
        self.assertNotIn("secret", run["reason"])                             # 例外メッセージ(パス)は出さない
        self.assertFalse(mgr.status()["running"])

    def test_dry_run_com_uninit_failure_does_not_change_the_result(self):
        def bad_un():
            raise OSError("uninit 失敗")
        mgr, hist, main = self._dry_run_mgr([], uninit=bad_un)
        self.assertEqual(mgr.run_scheduled_dry_run(), COMPLETED)
        main.assert_not_called()
        self.assertFalse(mgr.status()["running"])

    def test_normal_scheduled_run_is_unchanged_by_the_dry_run_option(self):
        calls = []
        mgr, hist = self.make(lambda **kw: calls.append(kw) or {"completed": ["A1X"], "failed": {}, "skipped": {}})
        self.assertEqual(mgr.run_scheduled(), COMPLETED)
        self.assertEqual(calls, [{"config_path": "cfg.yaml"}])
        self.assertEqual(hist.runs()[0]["kind"], "定期")                        # 通常は従来の種別のまま

    def test_scheduled_run_while_busy_does_not_start_and_is_recorded(self):
        calls = []
        mgr, hist = self.make(lambda **kw: calls.append(kw) or {})
        mgr.lock.acquire("manual")
        self.assertEqual(mgr.run_scheduled(), SKIPPED)
        self.assertEqual(calls, [])
        self.assertIn("二重起動", hist.runs()[0]["reason"])
        mgr.lock.release()


# ------------------------------------------------------------------ HTTP(127.0.0.1 のみ・ヘッダ検証・各操作)
class ServerTest(NoRealSubprocess):
    def setUp(self):
        super().setUp()
        self.sched_calls = []
        fake = lambda args: self.sched_calls.append(list(args)) or SimpleNamespace(returncode=0, stdout=b"", stderr=b"")  # noqa: E731
        self.scheduler = scheduler_mod.TaskScheduler("p", "s", runner=fake)
        self.main_calls = []
        self.gate = threading.Event()
        self.gate.set()

        def fake_main(**kw):
            self.main_calls.append(kw)
            self.gate.wait(5)
            return {"completed": list(kw.get("products") or []), "failed": {}, "skipped": {}}
        store, hist = SettingsStore(self.state), History(self.state)
        runs = RunManager(self.state, store, hist, fake_main, config_path="cfg.yaml")
        self.ctx = server_mod.AppContext(store=store, history=hist, runs=runs, scheduler=self.scheduler, config_path="cfg.yaml",
                                         run_checks=lambda cfg, s: [{"name": "設定の形式", "status": "ok", "detail": "ok"}])
        self.server = server_mod.make_server(self.ctx, port=0)
        self.port = self.server.server_address[1]
        th = threading.Thread(target=self.server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def call(self, method, path, body=None, headers=HDR, host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode("utf-8"), headers=dict(headers))
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def jcall(self, *a, **kw):
        code, raw = self.call(*a, **kw)
        return code, json.loads(raw.decode("utf-8"))

    def test_listens_on_loopback_only_and_refuses_other_bind_addresses(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            server_mod.make_server(self.ctx, port=0, host="0.0.0.0")

    def test_page_has_the_agreed_layout_and_large_type(self):
        code, raw = self.call("GET", "/", headers={})
        html = raw.decode("utf-8")
        self.assertEqual(code, 200)
        for title in ("1. 運用設定", "2. 動作チェック", "3. 定期実行", "4. 製番を指定して実行", "5. 実行履歴"):
            self.assertIn(title, html)
        self.assertLess(html.index("1. 運用設定"), html.index("5. 実行履歴"))
        self.assertIn("grid-template-columns: minmax(760px, 44%) 1fr", html)         # 左 1〜4 / 右 5
        self.assertIn("font-size: 22px", html)                                       # 以前の案(約 11px)の 2 倍
        self.assertIn("height: 48px", html)
        self.assertIn("ログオン中のみ動作します(ログオフ中は動きません)", html)           # 制約が画面で分かる
        self.assertIn('id="interval"', html)

    def test_mutating_requests_need_the_header_and_a_loopback_host(self):
        code, _ = self.jcall("PUT", "/api/settings", {"output_root": "", "schedule_time": "08:00"}, headers={"Content-Type": "application/json"})
        self.assertEqual(code, 403)
        code, _ = self.jcall("POST", "/api/run", {"product": "A1TEST0001"}, headers={"Content-Type": "application/json"})
        self.assertEqual(code, 403)
        code, _ = self.jcall("GET", "/api/state", host="evil.example.com", headers={})
        self.assertEqual(code, 403)
        self.assertEqual(self.main_calls, [])

    def test_settings_save_reload_and_validation_errors(self):
        root = str(self.tmp / "out")
        code, body = self.jcall("PUT", "/api/settings", {"output_root": root, "schedule_enabled": True, "schedule_time": "06:45",
                                                         "schedule_interval_days": 2})
        self.assertEqual((code, body["settings"]["schedule_time"], body["settings"]["schedule_interval_days"]), (200, "06:45", 2))
        code, body = self.jcall("POST", "/api/settings/reload", {})
        self.assertEqual((code, body["settings"]["output_root"]), (200, root))
        code, body = self.jcall("PUT", "/api/settings", {"output_root": "rel", "schedule_time": "06:45"})
        self.assertEqual(code, 400)
        self.assertIn("絶対パス", body["error"])
        self.assertEqual(self.ctx.store.load().output_root, root)                    # 不正な保存は既存の値を変えない

    def test_checks_endpoint_returns_results(self):
        code, body = self.jcall("POST", "/api/checks", {})
        self.assertEqual((code, body["results"][0]["name"]), (200, "設定の形式"))

    def test_run_starts_one_product_then_double_start_is_409_and_history_shows_it(self):
        self.gate.clear()
        code, body = self.jcall("POST", "/api/run", {"product": "A1TEST0001", "output_root": str(self.tmp / "out")})
        self.assertEqual(code, 200)
        for _ in range(100):
            if self.main_calls:
                break
            threading.Event().wait(0.02)
        code, body = self.jcall("POST", "/api/run", {"product": "A1TEST0002"})
        self.assertEqual(code, 409)
        self.assertIn("実行中", body["error"])
        code, state = self.jcall("GET", "/api/state", headers={})
        self.assertTrue(state["status"]["running"])
        code, hist = self.jcall("GET", "/api/history", headers={})
        self.assertEqual([r["status"] for r in hist["runs"]], ["実行中"])
        self.gate.set()
        for _ in range(100):
            if not self.ctx.runs.status()["running"]:
                break
            threading.Event().wait(0.02)
        code, hist = self.jcall("GET", "/api/history", headers={})
        self.assertEqual([(r["product"], r["status"]) for r in hist["runs"]], [("A1TEST0001", "完了")])
        self.assertEqual(self.main_calls[0]["products"], ["A1TEST0001"])

    def test_run_with_a_bad_product_is_400_and_starts_nothing(self):
        code, body = self.jcall("POST", "/api/run", {"product": "bad product"})
        self.assertEqual(code, 400)
        self.assertEqual(self.main_calls, [])

    def test_schedule_register_and_unregister_only_via_explicit_calls_with_the_mock(self):
        self.jcall("PUT", "/api/settings", {"output_root": "", "schedule_enabled": True, "schedule_time": "05:10", "schedule_interval_days": 2})
        self.jcall("GET", "/api/state?schedule=1", headers={})                     # 表示・照会では登録・解除しない
        self.assertEqual({c[1] for c in self.sched_calls}, {"/Query"})
        self.sched_calls.clear()
        code, _ = self.jcall("POST", "/api/schedule/register", {})
        self.assertEqual(code, 200)
        self.assertEqual([c[1] for c in self.sched_calls[:2]], ["/Create", "/Change"])
        self.assertEqual(self.sched_calls[0][self.sched_calls[0].index("/ST") + 1], "05:10")
        self.assertEqual(self.sched_calls[0][self.sched_calls[0].index("/MO") + 1], "2")
        self.sched_calls.clear()
        code, _ = self.jcall("DELETE", "/api/schedule")
        self.assertEqual(code, 200)
        self.assertEqual(self.sched_calls[0][1], "/Delete")

    def test_scheduler_failure_is_reported_as_an_error_message(self):
        self.scheduler.runner = lambda args: SimpleNamespace(returncode=5, stdout=b"", stderr=b"denied")
        code, body = self.jcall("POST", "/api/schedule/register", {})
        self.assertEqual(code, 502)
        self.assertIn("失敗しました", body["error"])


# ------------------------------------------------------------------ 起動入口
class EntryPointTest(NoRealSubprocess):
    def test_run_scheduled_runs_the_normal_process_once_via_the_mocks_and_returns_exit_code(self):
        import importlib
        oc = importlib.import_module("ops_console")
        ctx = oc.build_context(state_dir=self.state, config_path="cfg.yaml",
                               main_func=lambda **kw: {"completed": [], "failed": {"A1X": "RuntimeError: x"}, "skipped": {}},
                               scheduler=mock.Mock(name="scheduler"))
        self.assertEqual(ctx.runs.run_scheduled(), "失敗")                      # 失敗があれば失敗(終了コード 1 になる)
        self.assertEqual(ctx.history.runs()[0]["kind"], "定期")
        ctx.scheduler.assert_not_called()                                         # 起動入口の組み立てだけでタスク操作はしない

    def test_run_scheduled_dry_run_option_uses_dry_run_and_never_the_normal_process(self):
        import importlib
        oc = importlib.import_module("ops_console")
        captured = {}
        runs = SimpleNamespace(run_scheduled=mock.Mock(name="run_scheduled"), run_scheduled_dry_run=mock.Mock(return_value="完了"))

        def fake_build(**kw):
            captured.update(kw)
            return SimpleNamespace(runs=runs)
        import os
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        with mock.patch.object(oc, "build_context", side_effect=fake_build):
            self.assertEqual(oc.main(["run-scheduled", "--dry-run"]), 0)
            runs.run_scheduled_dry_run.return_value = "失敗"
            self.assertEqual(oc.main(["run-scheduled", "--dry-run"]), 1)        # 失敗は終了コード 1
        runs.run_scheduled.assert_not_called()
        with self.assertRaises(AssertionError):                                  # 渡された main_func は呼べない(万一でも app.main に届かない)
            captured["main_func"]()
        with mock.patch.object(oc, "build_context", side_effect=fake_build):
            runs.run_scheduled.return_value = "完了"
            self.assertEqual(oc.main(["run-scheduled"]), 0)                      # --dry-run なしは従来の通常処理
        runs.run_scheduled.assert_called_once()

    def test_scheduled_task_command_is_unchanged_and_has_no_dry_run(self):
        ts = scheduler_mod.TaskScheduler("py.exe", "ops_console.py")
        self.assertEqual(ts.action(), '"py.exe" "ops_console.py" run-scheduled')   # 既存タスクのコマンドは変えない
        self.assertNotIn("dry-run", " ".join(ts.create_args("08:00", 2)))

    def test_commands_pin_the_working_directory_to_the_repository(self):
        import importlib, os
        oc = importlib.import_module("ops_console")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        ctx = SimpleNamespace(runs=SimpleNamespace(run_scheduled=lambda: "完了"))
        os.chdir(self.tmp)
        with mock.patch.object(oc, "build_context", return_value=ctx):
            self.assertEqual(oc.cmd_run_scheduled(SimpleNamespace(config=None)), 0)    # タスクの既定の作業フォルダ(System32)でも、リポジトリ基準になる
        self.assertEqual(Path(os.getcwd()).resolve(), oc.BASE_DIR.resolve())

    def test_serve_does_not_fail_a_running_record_while_another_process_holds_the_lock(self):
        import importlib
        oc = importlib.import_module("ops_console")
        for held, expected in ((True, 0), (False, 1)):
            hist = History(self.state / f"h{held}") if (self.state / f"h{held}").mkdir() is None else None
            hist.start("定期", ALL_PRODUCTS)                                          # 「実行中」のまま残っている記録
            ctx = SimpleNamespace(runs=SimpleNamespace(lock=SimpleNamespace(is_held=lambda h=held: h)), history=hist)
            fake_server = SimpleNamespace(serve_forever=mock.Mock(side_effect=KeyboardInterrupt), server_close=mock.Mock())
            with mock.patch.object(oc, "build_context", return_value=ctx), \
                    mock.patch("ops.server.make_server", return_value=fake_server), mock.patch("builtins.print"):
                oc.cmd_serve(SimpleNamespace(config=None, port=0))
            self.assertEqual(sum(1 for r in hist.runs() if r["status"] == "失敗"), expected, f"held={held}")

    def test_serve_refuses_a_non_loopback_bind_and_cli_has_the_two_subcommands(self):
        import importlib
        oc = importlib.import_module("ops_console")
        for sub in ("serve", "run-scheduled"):
            with self.assertRaises(SystemExit) as ctx:
                oc.main([sub, "--help"])
            self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(server_mod.LOOPBACK, "127.0.0.1")


# ------------------------------------------------------------------ 既存処理(app.main)の再利用
class MainSelectionTest(MainIntegrationBase):
    """app.main(products=..., output_root=...): 指定した製番だけを、既存の製番処理(Step.1〜5)にそのまま通す。"""

    def test_default_call_is_unchanged_and_asks_the_host_for_the_list(self):
        self.run_main()
        self.assertEqual([c[0] for c in self.host_calls], ["create_n3_design_list", "get_property_data", "search_server"])
        self.assertEqual(self.results["completed"], [PRODUCT])

    def test_specified_product_skips_the_list_lookup_and_runs_the_same_steps(self):
        self.run_main(products=[PRODUCT])
        self.assertEqual([c[0] for c in self.host_calls], ["get_property_data", "search_server"])   # 一覧取得はしない
        self.assertEqual(self.results, {"completed": [PRODUCT], "failed": {}, "skipped": {}})
        self.assertTrue((self.product_out / "design.csv").exists())                                 # 出力契約はそのまま
        self.assertTrue((self.product_out / "material.csv").exists())

    def test_output_root_override_moves_the_output(self):
        other = self.tmp / "other_root"
        self.run_main(products=[PRODUCT], output_root=str(other))
        self.assertEqual(self.results["completed"], [PRODUCT])
        self.assertTrue((other / "a_product" / "output" / PRODUCT / "product_df.csv").exists())
        self.assertFalse(self.product_out.exists())

    def test_already_processed_product_is_skipped_not_completed(self):
        self.run_main(products=[PRODUCT])
        self.run_main(products=[PRODUCT])
        self.assertEqual(self.results["completed"], [])
        self.assertEqual(list(self.results["skipped"]), [PRODUCT])

    def test_failed_product_stays_failed_when_specified(self):
        self.run_main(products=[PRODUCT], convert_error=RuntimeError("DWG→DXF変換に失敗(テスト用)"))
        self.assertEqual(self.results["completed"], [])
        self.assertIn(PRODUCT, self.results["failed"])


if __name__ == "__main__":
    unittest.main()
