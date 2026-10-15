# 暫定 API 操作画面の「製番の指定実行」「全製番の仮定期実行」(interim/runs.py・interim/server.py の追加エンドポイント・interim_console.py run-scheduled)のテスト。
# すべてモック(実 app.main・ホスト DB・設計データ・CAD・API・schtasks は使わない)。一時フォルダの偽のリポジトリ配置だけを使う。
# HTTP のテストだけ、この PC の loopback の一時ポートを使う。subprocess.run が呼ばれたら失敗にする(実 schtasks を呼ばない)。
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import app  # noqa: E402,F401  (重い依存の import は、subprocess を禁止するパッチより前に済ませる)
import interim_console  # noqa: E402
from interim import manager as mg  # noqa: E402
from interim import runs as rn  # noqa: E402
from interim import server as srv  # noqa: E402
from ops.scheduler import SchedulerError  # noqa: E402

PRODUCT = "A1TEST0001"
SECRET_BITS = ("secret-host", "pw123", "10.9.8.7")


def write_config(repo: Path, **over):
    cfg = {"data": {"output": "./output_interim", "storage": "a_product", "initials": "A1", "box_types": ["OS"]},
           "api": {"dir_venv": "./output_interim/api", "yaml_file": "server.yaml", "use_proxy": False},
           "dwg_conversion": {"converter": "x", "cad_version": "ACAD2018"}}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    (repo / "app_interim.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


class Result:
    def __init__(self, completed=(), failed=None, skipped=None):
        self.d = {"completed": list(completed), "failed": dict(failed or {}), "skipped": dict(skipped or {})}


class FakeMain:
    """app.main の代わり。呼び出しを記録し、結果(results の辞書)を返す。COM の初期化は RunManager 側(同じスレッド)。"""

    def __init__(self, result=None, exc=None, gate=None):
        self.calls, self.result, self.exc, self.gate = [], result, exc, gate
        self.threads = []

    def __call__(self, **kw):
        self.calls.append(kw)
        self.threads.append(threading.current_thread().name)
        if self.gate is not None:
            self.gate.wait(10)
        if self.exc is not None:
            raise self.exc
        return (self.result or Result()).d


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="sekisan_runs_")
        self.addCleanup(td.cleanup)
        self.repo = Path(td.name).resolve()
        write_config(self.repo)
        self.main = FakeMain(Result(completed=[PRODUCT]))
        self.com = []
        self.schtasks = []
        patcher = mock.patch("subprocess.run", side_effect=AssertionError("実際の subprocess(schtasks 等)は呼ばない"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, **kw):
        base = dict(main_func=self.main, scheduler_runner=self.runner, python_exe=r"C:\py\python.exe", api_ready=lambda: True,
                    net_drive=lambda p: False, com_init=lambda: self.com.append("init"), com_uninit=lambda: self.com.append("uninit"))
        base.update(kw)
        return rn.InterimRuns(self.repo, **base)

    # --- schtasks の偽物(状態を持つ) ---
    task = None

    def runner(self, args):
        self.schtasks.append(list(args))
        verb = args[1]
        if verb == "/Query":
            if self.task is None:
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
            xml = (f"<Task><Triggers><CalendarTrigger><StartBoundary>2026-10-16T{self.task['time']}:00</StartBoundary>"
                   f"<ScheduleByDay><DaysInterval>{self.task['days']}</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>"
                   f"<Settings><Enabled>{'true' if self.task['enabled'] else 'false'}</Enabled></Settings>"
                   f"<Actions><Exec><Command>{self.task['cmd']}</Command></Exec></Actions></Task>")
            return SimpleNamespace(returncode=0, stdout=xml.encode("utf-16"), stderr=b"")
        if verb == "/Create":
            self.task = {"time": args[args.index("/ST") + 1], "days": args[args.index("/MO") + 1], "enabled": True, "cmd": args[args.index("/TR") + 1]}
        elif verb == "/Change":
            self.task["enabled"] = "/ENABLE" in args
        elif verb == "/Delete":
            self.task = None
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def wait_idle(self, runs, timeout=10):
        import time
        t0 = time.time()
        while runs.running() and time.time() - t0 < timeout:
            time.sleep(0.02)
        self.assertFalse(runs.running())


class TargetAndIsolationTest(Base):
    def test_target_shows_the_isolated_relative_output_dir_and_whether_it_exists(self):
        runs = self.make()
        t = runs.target_for(PRODUCT)
        self.assertEqual(t["output_dir"], f"output_interim/a_product/output/{PRODUCT}")
        self.assertEqual((t["exists"], t["isolated"]), (False, True))
        self.assertNotIn(str(self.repo), json.dumps(t, ensure_ascii=False))                 # 絶対パスは出さない
        (self.repo / "output_interim" / "a_product" / "output" / PRODUCT).mkdir(parents=True)
        self.assertTrue(runs.target_for(PRODUCT)["exists"])

    def test_invalid_product_numbers_are_refused(self):
        runs = self.make()
        for bad in ("", "  ", "a b", "A1/../x", "A1", "A1TEST;rm", None, "x" * 21, "A1\\x"):
            with self.assertRaises(rn.RunRefused, msg=str(bad)) as cm:
                runs.target_for(bad)
            self.assertEqual(cm.exception.code, "bad_product")
        self.assertEqual(self.main.calls, [])

    def test_unsafe_configs_are_refused_and_nothing_runs(self):
        cases = [dict(data={"output": "./output"}), dict(data={"output": "C:/Windows/Temp/out"}), dict(data={"output": r"\\host\share\x"}),
                 dict(data={"output": "./output_interim/../out"}), dict(data={"output": "."}), dict(data={"storage": "../x"}),
                 dict(api={"dir_venv": "./elsewhere"}), dict(api={"use_proxy": True}), dict(inference_server={"enabled": True})]
        for over in cases:
            write_config(self.repo, **over)
            runs = self.make()
            with self.assertRaises(rn.IsolationError, msg=str(over)):
                runs.target()
            with self.assertRaises(rn.IsolationError, msg=str(over)):
                runs.start(PRODUCT)
        self.assertEqual(self.main.calls, [])

    def test_network_drive_and_missing_or_wrong_named_config_are_refused(self):
        with self.assertRaises(rn.IsolationError):
            self.make(net_drive=lambda p: True).target()
        (self.repo / "app_interim.yaml").unlink()
        with self.assertRaises(rn.IsolationError):
            self.make().target()
        (self.repo / "app.yaml").write_text("data: {output: ./output_interim, storage: x}\n", encoding="utf-8")   # 本番設定は読まない
        with self.assertRaises(rn.IsolationError):
            rn.validate_run_config(self.repo / "app.yaml", self.repo)

    @unittest.skipUnless(sys.platform == "win32", "ジャンクションは Windows のみ")
    def test_junction_in_the_output_path_is_refused_at_run_time_even_if_it_appears_later(self):
        import _winapi
        runs = self.make()
        runs.target()                                                                            # 最初は OK
        outside = Path(tempfile.mkdtemp(prefix="sekisan_outside_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        link = self.repo / "output_interim" / "a_product"
        _winapi.CreateJunction(str(outside), str(link))                                          # 実行直前に、出力先が外へ出るリンクになった
        self.addCleanup(lambda: link.exists() and os.rmdir(str(link)))
        with self.assertRaises(rn.IsolationError):
            runs.target()
        run_id, _ = (lambda: (None, None))()
        with self.assertRaises(rn.IsolationError):
            runs.start(PRODUCT)
        self.assertEqual(self.main.calls, [])
        self.assertEqual(list(outside.iterdir()), [])                                            # 外側に何も作られていない

    @unittest.skipUnless(sys.platform == "win32", "ジャンクションは Windows のみ")
    def test_isolation_is_rechecked_inside_the_worker_and_recorded_as_failed(self):
        import _winapi
        runs = self.make()
        gate = threading.Event()
        real = runs._guarded_main
        outside = Path(tempfile.mkdtemp(prefix="sekisan_outside_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        link = self.repo / "output_interim" / "a_product"

        def swapped(**kw):                                                                       # start() の検証の後、worker が動く前にリンク化
            _winapi.CreateJunction(str(outside), str(link))
            return real(**kw)
        runs.runs.main_func = swapped
        runs.start(PRODUCT)
        self.wait_idle(runs)
        self.addCleanup(lambda: link.exists() and os.rmdir(str(link)))
        r = runs.history.runs()[0]
        self.assertEqual(r["status"], "失敗")
        self.assertEqual(self.main.calls, [])                                                    # app.main は呼ばれない
        self.assertEqual(list(outside.iterdir()), [])
        self.assertNotIn(str(self.repo), r["reason"])


class ManualRunTest(Base):
    def test_start_reuses_app_main_with_the_dedicated_config_and_no_output_override(self):
        runs = self.make()
        info = runs.start(PRODUCT)
        self.wait_idle(runs)
        (call,) = self.main.calls
        self.assertEqual(call["products"], [PRODUCT])                                            # 1 製番だけ。ホストの全件取得は app.main が省く
        self.assertEqual(Path(call["config_path"]), self.repo / "app_interim.yaml")             # app.yaml / app.local.yaml ではない
        self.assertNotIn("output_root", call)                                                    # 出力先は設定(隔離)どおり
        self.assertEqual(info["output_dir"], f"output_interim/a_product/output/{PRODUCT}")
        r = runs.history.runs()[0]
        self.assertEqual((r["kind"], r["product"], r["status"]), ("手動", PRODUCT, "完了"))

    def test_default_main_is_the_existing_app_main(self):
        self.assertIs(rn.InterimRuns(self.repo)._resolve_main(), app.main)                      # 処理ロジックを重複実装しない

    def test_statuses_completed_failed_skipped_are_recorded_without_exposing_paths(self):
        cases = [(Result(completed=[PRODUCT]), "完了"), (Result(failed={PRODUCT: "RuntimeError: design.csv を作成できません"}), "失敗"),
                 (Result(skipped={PRODUCT: "処理済み"}), "スキップ")]
        for result, expected in cases:
            self.main.result = result
            runs = self.make()
            runs.start(PRODUCT)
            self.wait_idle(runs)
            r = runs.history.runs()[0]
            self.assertEqual(r["status"], expected)
        self.assertEqual(self.main.calls.__len__(), 3)

    def test_exceptions_systemexit_and_com_failures_are_recorded_as_failed_with_safe_reasons(self):
        for exc in (RuntimeError(r"C:\secret\x password=pw123 host=secret-host"), SystemExit(r"C:\secret\x"), SystemExit(1)):
            self.main.exc = exc
            runs = self.make()
            runs.start(PRODUCT)
            self.wait_idle(runs)
            r = runs.history.runs()[0]
            self.assertEqual(r["status"], "失敗")
            for shown in (r["reason"], runs.state()["history"][0]["reason"], runs.history.path.read_text(encoding="utf-8")):    # 履歴ファイル・画面とも
                self.assertFalse(any(b in shown for b in ("secret", "pw123", "host=")), shown)
        self.assertEqual(runs.history.runs()[0]["reason"] in ("RuntimeError", "処理が途中で終了しました(SystemExit)"), True)
        self.assertEqual(self.com.count("init"), self.com.count("uninit"))                       # COM は必ず終了する
        bad = self.make(com_init=lambda: (_ for _ in ()).throw(OSError(r"C:\secret")))
        calls_before = len(self.main.calls)
        bad.start(PRODUCT)
        self.wait_idle(bad)
        self.assertEqual(bad.history.runs()[0]["status"], "失敗")
        self.assertEqual(len(self.main.calls), calls_before)                                      # COM を初期化できなければ app.main は呼ばない

    def test_com_is_initialized_in_the_worker_thread_that_runs_app_main(self):
        runs = self.make()
        runs.start(PRODUCT)
        self.wait_idle(runs)
        self.assertEqual(self.com, ["init", "uninit"])
        self.assertTrue(self.main.threads[0].startswith("ops-run"))

    def test_double_run_is_refused_while_running_and_released_afterwards(self):
        gate = threading.Event()
        self.main.gate = gate
        runs = self.make()
        runs.start(PRODUCT)
        self.assertTrue(runs.running())
        with self.assertRaises(rn.RunRefused) as cm:
            runs.start("A1TEST0002")
        self.assertEqual(cm.exception.code, "busy")
        other = self.make()                                                                        # 別インスタンス(別プロセスの定期実行を想定)も排他される
        self.assertEqual(other.run_scheduled(), "スキップ")
        gate.set()
        self.wait_idle(runs)
        self.assertEqual(len(self.main.calls), 1)
        runs.start("A1TEST0002")
        self.wait_idle(runs)
        self.assertEqual(len(self.main.calls), 2)

    def test_api_not_ready_refuses_before_any_processing(self):
        runs = self.make(api_ready=lambda: False)
        with self.assertRaises(rn.RunRefused) as cm:
            runs.start(PRODUCT)
        self.assertEqual(cm.exception.code, "api_not_ready")
        self.assertEqual((self.main.calls, runs.history.runs()), ([], []))

    def test_files_lists_names_and_sizes_only_and_never_follows_links(self):
        runs = self.make()
        d = self.repo / "output_interim" / "a_product" / "output" / PRODUCT
        (d / "sub").mkdir(parents=True)
        (d / "design.csv").write_bytes(b"abc")
        (d / "sub" / "p1.png").write_bytes(b"12345")
        files = runs.files(PRODUCT)
        self.assertEqual(sorted((f["name"], f["size"]) for f in files), [("design.csv", 3), ("sub/p1.png", 5)])
        self.assertNotIn(str(self.repo), json.dumps(files))
        self.assertEqual(runs.files("bad name"), [])
        if sys.platform == "win32":
            import _winapi
            outside = Path(tempfile.mkdtemp(prefix="sekisan_out_"))
            self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
            (outside / "secret.txt").write_text("x", encoding="utf-8")
            _winapi.CreateJunction(str(outside), str(d / "evil"))
            self.addCleanup(lambda: os.rmdir(str(d / "evil")) if (d / "evil").exists() else None)
            self.assertNotIn("evil/secret.txt", [f["name"] for f in runs.files(PRODUCT)])

    def test_files_are_capped(self):
        runs = self.make()
        d = self.repo / "output_interim" / "a_product" / "output" / PRODUCT
        d.mkdir(parents=True)
        for i in range(rn.MAX_LISTED_FILES + 20):
            (d / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
        self.assertEqual(len(runs.files(PRODUCT)), rn.MAX_LISTED_FILES)

    def test_state_and_history_are_kept_in_the_isolated_run_state_folder_only(self):
        runs = self.make()
        runs.start(PRODUCT)
        self.wait_idle(runs)
        state = runs.state(PRODUCT)
        self.assertEqual((state["running"], state["history"][0]["status"]), (False, "完了"))
        written = sorted(p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*") if p.is_file() and p.name != "app_interim.yaml")
        self.assertTrue(all(w.startswith("output_interim/run_state/") for w in written), written)    # 履歴・ロック・設定は隔離先の中だけ
        self.assertFalse((self.repo / "ops.local.yaml").exists() or (self.repo / "ops_history.local.jsonl").exists())

    def test_orphaned_running_history_is_marked_failed_on_startup_only_when_not_locked(self):
        runs = self.make()
        rid = runs.history.start("手動", PRODUCT)
        runs.mark_orphans()
        self.assertEqual(runs.history.runs()[0]["status"], "失敗")
        gate = threading.Event()
        self.main.gate = gate
        runs.start(PRODUCT)
        before = runs.history.runs()[0]["status"]
        runs.mark_orphans()                                                                       # 実行中(ロック保持)は触らない
        self.assertEqual(runs.history.runs()[0]["status"], before)
        gate.set()
        self.wait_idle(runs)


class SafeReasonTest(unittest.TestCase):
    def test_exception_messages_are_dropped_but_fixed_texts_and_counts_are_kept(self):
        cases = {
            r"RuntimeError: C:\secret\x host=secret-host": "RuntimeError",
            "完了 1 件・失敗 2 件・スキップ 0 件 — A1X00001: ValueError: 接続先 10.9.8.7 に接続できません / A1X00002: OSError: pw123": "完了 1 件・失敗 2 件・スキップ 0 件 — A1X00001: ValueError / A1X00002: OSError",
            "処理済み": "処理済み",
            "推論 API を利用できないため、製番処理を開始しませんでした(接続が拒否されました。API が未起動、または server.yaml の接続先が古い可能性があります)":
                "推論 API を利用できないため、製番処理を開始しませんでした(接続が拒否されました。API が未起動、または server.yaml の接続先が古い可能性があります)",
            "処理が途中で終了しました(SystemExit)": "処理が途中で終了しました(SystemExit)",
            "": "", None: "",
        }
        for raw, expected in cases.items():
            self.assertEqual(rn.safe_reason(raw), expected, raw)


class ScheduleTest(Base):
    def test_default_interval_is_two_days_and_logon_only_notice_is_always_present(self):
        s = self.make().schedule_state()
        self.assertEqual((s["registered"], s["default_interval_days"], s["interval_days"]), (False, 2, 2))
        self.assertIn("ログオンしている間だけ", s["logon_only"])
        self.assertEqual((s["task_name"], s["separate_from_production_task"], s["scope"]), ("SekisanNaviData_InterimOps", True, "全対象製番(通常処理)"))
        self.assertEqual(s["output_dir"], "output_interim/a_product/output")
        self.assertNotIn(str(self.repo), json.dumps(s, ensure_ascii=False))

    def test_register_builds_the_dedicated_task_with_the_default_two_day_interval(self):
        runs = self.make()
        s = runs.schedule_register("08:30", 2)
        create = next(a for a in self.schtasks if a[1] == "/Create")
        self.assertEqual(create[create.index("/TN") + 1], "SekisanNaviData_InterimOps")
        self.assertEqual((create[create.index("/SC") + 1], create[create.index("/MO") + 1], create[create.index("/ST") + 1]), ("DAILY", "2", "08:30"))
        tr = create[create.index("/TR") + 1]
        self.assertIn("interim_console.py", tr)
        self.assertTrue(tr.endswith("run-scheduled"))                                             # 製番も出力先も設定ファイルも指定しない
        for forbidden in ("ops_console.py", "--config", "--dry-run", "app.yaml"):
            self.assertNotIn(forbidden, tr)
        self.assertEqual([a[1] for a in self.schtasks if a[1] in ("/Create", "/Change")], ["/Create", "/Change"])    # 登録 → 有効化の順
        self.assertTrue(s["registered"] and s["enabled"])
        self.assertEqual((s["time"], s["interval_days"]), ("08:30", 2))
        saved = runs.store.load()
        self.assertEqual((saved.schedule_enabled, saved.schedule_time, saved.schedule_interval_days), (True, "08:30", 2))

    def test_only_the_dedicated_task_name_is_ever_used_and_the_production_name_is_rejected(self):
        runs = self.make()
        runs.schedule_register("08:00", 2)
        runs.schedule_state()
        runs.schedule_unregister()
        for args in self.schtasks:
            self.assertEqual(args[args.index("/TN") + 1], "SekisanNaviData_InterimOps", args)
        for bad in ("SekisanNaviData_Ops", "sekisandavigata_ops", "SekisanNaviData_Ops ", "Other", ""):
            with self.assertRaises(ValueError, msg=bad):
                rn.InterimScheduler("p", "s", task_name=bad)

    def test_unregister_removes_only_our_task_and_records_disabled(self):
        runs = self.make()
        runs.schedule_register("08:00", 2)
        s = runs.schedule_unregister()
        self.assertFalse(s["registered"])
        self.assertIsNone(self.task)
        self.assertFalse(runs.store.load().schedule_enabled)
        runs.schedule_unregister()                                                                # 未登録での解除は何もしない(エラーにしない)
        self.assertEqual(sum(1 for a in self.schtasks if a[1] == "/Delete"), 1)

    def test_a_same_named_task_that_is_not_ours_is_never_overwritten_or_deleted(self):
        self.task = {"time": "01:00", "days": "1", "enabled": True, "cmd": r'"C:\py\python.exe" "C:\other\something_else.py"'}
        runs = self.make()
        s = runs.schedule_state()
        self.assertTrue(s["foreign"])
        with self.assertRaises(rn.TaskForeignError):
            runs.schedule_register("08:00", 2)
        with self.assertRaises(rn.TaskForeignError):
            runs.schedule_unregister()
        self.assertEqual([a[1] for a in self.schtasks if a[1] in ("/Create", "/Change", "/Delete")], [])
        self.assertEqual(self.task["cmd"].count("something_else"), 1)

    def test_invalid_schedule_values_are_refused_without_calling_schtasks(self):
        runs = self.make()
        for t, n in (("25:00", 2), ("8:00", 2), ("08:00", 0), ("08:00", 31), ("08:00", True), ("08:00", "2"), (None, 2), ("08:00", None)):
            with self.assertRaises(rn.RunRefused, msg=str((t, n))) as cm:
                runs.schedule_register(t, n)
            self.assertEqual(cm.exception.code, "bad_schedule")
        self.assertEqual(self.schtasks, [])

    def test_register_is_refused_when_the_output_is_not_isolated(self):
        write_config(self.repo, data={"output": "./output"})
        runs = self.make()
        with self.assertRaises(rn.IsolationError):
            runs.schedule_register("08:00", 2)
        self.assertEqual(self.schtasks, [])

    def test_schtasks_failure_is_reported_without_details(self):
        def failing(args):
            return SimpleNamespace(returncode=1, stdout=b"", stderr=r"エラー: C:\secret\x password=pw123".encode("cp932"))
        runs = self.make(scheduler_runner=failing)
        with self.assertRaises(SchedulerError):
            runs.schedule_register("08:00", 2)

    def test_command_length_over_the_schtasks_limit_is_refused(self):
        runs = self.make(python_exe="C:\\" + "x" * 300 + "\\python.exe")
        with self.assertRaises(SchedulerError):
            runs.schedule_register("08:00", 2)
        self.assertEqual([a for a in self.schtasks if a[1] == "/Create"], [])


class ScheduledRunTest(Base):
    def test_run_scheduled_processes_all_targets_with_the_isolated_dedicated_config(self):
        self.main.result = Result(completed=["A1X00001", "A1X00002"], skipped={"A1X00003": "処理済み"})
        runs = self.make()
        self.assertEqual(runs.run_scheduled(), "完了")
        (call,) = self.main.calls
        self.assertNotIn("products", call)                                                       # 全対象製番(ホストの出図済み − 処理済み)
        self.assertNotIn("output_root", call)
        self.assertEqual(Path(call["config_path"]), self.repo / "app_interim.yaml")
        r = runs.history.runs()[0]
        self.assertEqual((r["kind"], r["status"]), ("定期", "完了"))
        self.assertIn("完了 2 件", r["reason"])

    def test_failures_and_isolation_errors_are_recorded_and_exit_code_is_one(self):
        self.main.result = Result(failed={"A1X00001": "RuntimeError: x"})
        self.assertEqual(self.make().run_scheduled(), "失敗")
        write_config(self.repo, data={"output": "./output"})
        runs = self.make()
        self.assertEqual(runs.run_scheduled(), "失敗")                                            # 隔離されていない設定では app.main を呼ばない
        self.assertEqual(len(self.main.calls), 1)

    def test_dry_run_never_calls_app_main(self):
        runs = self.make()
        self.assertEqual(runs.run_scheduled(dry_run=True), "完了")
        self.assertEqual(self.main.calls, [])
        r = runs.history.runs()[0]
        self.assertEqual((r["kind"], r["product"]), ("定期(検証)", "(検証のみ)"))
        self.assertEqual(self.com, ["init", "uninit"])                                            # COM の初期化と終了はタスクの入口でも行う

    def test_cli_entry_uses_the_dedicated_runs_and_maps_status_to_exit_codes(self):
        with mock.patch.object(rn, "InterimRuns") as cls:
            for status, code in (("完了", 0), ("スキップ", 0), ("失敗", 1)):
                cls.return_value.run_scheduled.return_value = status
                self.assertEqual(interim_console.cmd_run_scheduled(False), code)
            cls.return_value.run_scheduled.assert_called_with(dry_run=False)
            cls.side_effect = rn.IsolationError("x")
            self.assertEqual(interim_console.cmd_run_scheduled(True), 1)

    def test_run_scheduled_subcommand_changes_to_the_repo_folder_and_never_starts_the_server(self):
        with mock.patch.object(interim_console, "cmd_run_scheduled", return_value=0) as cmd, mock.patch("os.chdir") as chdir:
            self.assertEqual(interim_console.main(["run-scheduled", "--dry-run"]), 0)
        cmd.assert_called_once_with(True)
        chdir.assert_called_once_with(interim_console.BASE_DIR)                                   # タスクの既定の作業フォルダ(System32)に依存しない


class HttpTest(Base):
    def setUp(self):
        super().setUp()
        self.manager = mg.ApiManager(self.repo, settings_path=self.repo / "none.yaml", spawn=None)
        self.runs = self.make()
        self.server = srv.make_server(self.manager, 0, runs=self.runs)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def req(self, method, path, body=None, headers=None, host=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=h,
                                   data=json.dumps(body).encode("utf-8") if body is not None else None)
        if host:
            r.remove_header("Host")
            r.add_header("Host", host)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(r, timeout=15) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    POST = {"X-Interim-Request": "1"}

    def test_target_run_and_runs_endpoints(self):
        code, d = self.req("GET", f"/api/run/target?product={PRODUCT}")
        self.assertEqual((code, d["output_dir"], d["isolated"]), (200, f"output_interim/a_product/output/{PRODUCT}", True))
        self.assertEqual(self.req("GET", "/api/run/target?product=a%20b")[0], 400)
        self.assertEqual(self.req("GET", "/api/run/target")[0], 400)
        code, d = self.req("POST", "/api/run", {"product": PRODUCT}, self.POST)
        self.assertEqual((code, d["ok"]), (200, True))
        self.wait_idle(self.runs)
        code, d = self.req("GET", f"/api/runs?product={PRODUCT}")
        self.assertEqual((code, d["running"], d["history"][0]["status"]), (200, False, "完了"))
        text = json.dumps(d, ensure_ascii=False)
        self.assertNotIn(str(self.repo), text)

    def test_posts_require_the_custom_header_host_and_origin(self):
        for kw in (dict(), dict(headers={"X-Interim-Request": "1", "Origin": "http://evil.example"}), dict(headers=self.POST, host="evil.example")):
            self.assertEqual(self.req("POST", "/api/run", {"product": PRODUCT}, **kw)[0], 403)
        self.assertEqual(self.req("POST", "/api/schedule/register", {"time": "08:00", "interval_days": 2})[0], 403)
        self.assertEqual(self.main.calls, [])
        self.assertEqual(self.schtasks, [])

    def test_bad_input_and_busy_and_api_not_ready_have_fixed_messages(self):
        code, d = self.req("POST", "/api/run", {"product": "a b"}, self.POST)
        self.assertEqual((code, d["code"]), (400, "bad_product"))
        self.assertEqual(self.req("POST", "/api/run", None, self.POST)[0], 400)
        gate = threading.Event()
        self.main.gate = gate
        self.assertEqual(self.req("POST", "/api/run", {"product": PRODUCT}, self.POST)[0], 200)
        code, d = self.req("POST", "/api/run", {"product": "A1TEST0002"}, self.POST)
        self.assertEqual((code, d["code"], d["error"]), (409, "busy", rn.MESSAGES["busy"]))
        code, d = self.req("POST", "/api/stop", None, self.POST)                                  # 製番処理中は API を停止しない
        self.assertEqual((code, d["code"]), (409, "run_running"))
        gate.set()
        self.wait_idle(self.runs)
        self.runs._api_ready = lambda: False
        code, d = self.req("POST", "/api/run", {"product": PRODUCT}, self.POST)
        self.assertEqual((code, d["code"]), (409, "api_not_ready"))

    def test_schedule_endpoints_register_status_and_unregister(self):
        code, d = self.req("GET", "/api/schedule")
        self.assertEqual((code, d["registered"], d["interval_days"]), (200, False, 2))
        code, d = self.req("POST", "/api/schedule/register", {"time": "07:15", "interval_days": 2}, self.POST)
        self.assertEqual((code, d["registered"], d["time"], d["interval_days"], d["enabled"]), (200, True, "07:15", 2, True))
        code, d = self.req("POST", "/api/schedule/register", {"time": "99:99", "interval_days": 2}, self.POST)
        self.assertEqual((code, d["code"]), (400, "bad_schedule"))
        code, d = self.req("POST", "/api/schedule/unregister", {}, self.POST)
        self.assertEqual((code, d["registered"]), (200, False))

    def test_foreign_task_is_reported_as_409_with_a_fixed_message(self):
        self.task = {"time": "01:00", "days": "1", "enabled": True, "cmd": "something_else"}
        code, d = self.req("POST", "/api/schedule/register", {"time": "08:00", "interval_days": 2}, self.POST)
        self.assertEqual((code, d["code"], d["error"]), (409, "task_foreign", rn.MESSAGES["task_foreign"]))
        self.assertEqual(self.req("GET", "/api/schedule")[1]["foreign"], True)

    def test_unexpected_errors_return_fixed_500_without_details(self):
        self.runs.start = lambda p: (_ for _ in ()).throw(RuntimeError(r"C:\secret\x password=pw123 host=secret-host"))
        code, d = self.req("POST", "/api/run", {"product": PRODUCT}, self.POST)
        self.assertEqual(code, 500)
        self.assertFalse(any(b in json.dumps(d) for b in ("secret", "pw123")))

    def test_without_runs_the_new_routes_do_not_exist(self):
        plain = srv.make_server(self.manager, 0)
        port = plain.server_address[1]
        threading.Thread(target=plain.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (plain.shutdown(), plain.server_close()))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            opener.open(f"http://127.0.0.1:{port}/api/schedule", timeout=10)
        self.assertEqual(cm.exception.code, 404)


class StaticAndDocsTest(unittest.TestCase):
    def test_page_has_the_run_and_schedule_sections_with_the_required_notices(self):
        html = (ROOT / "interim" / "static" / "index.html").read_text(encoding="utf-8")
        for needle in ("製番を 1 件指定して実行", "出力先(隔離)", "全製番の仮定期実行", "ログオンしている間だけ", "既定は 2 日", 'value="2"',
                       "登録・更新", "解除", "実行履歴", "/api/run", "/api/schedule/register", "/api/schedule/unregister", "スキップ", "生成ファイル"):
            self.assertIn(needle, html)
        self.assertNotIn("http://", html)
        self.assertNotIn("eval(", html)
        for ln in html.split("<script>")[1].splitlines():
            if "innerHTML" in ln:                                                                 # 静的な文字列・空文字の代入だけ(データを連結しない)
                rhs = ln.split("innerHTML", 1)[1]
                self.assertNotIn("${", rhs, ln)
                self.assertEqual(rhs.replace("'<span class=\"' + cls + '\"", "").count("+"), 0, ln)       # 既存の 1 行(固定クラス名 ok/ng/na)だけ連結を許可

    def test_runtime_state_is_git_ignored(self):
        ignore = [ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        self.assertIn("output_interim/", ignore)                                                  # run_state(履歴・ロック・設定)もここに入る

    def test_sources_never_reference_the_production_task_config_or_shared_output(self):
        src = (ROOT / "interim" / "runs.py").read_text(encoding="utf-8")
        code = "\n".join(ln.split("  # ")[0] for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        for forbidden in ("app.local.yaml", "output_root=", "ops_console.py", "/Create", "subprocess", "shutil.rmtree", "os.remove", "unlink("):
            self.assertNotIn(forbidden, code, forbidden)                                          # 削除は既存の製番処理に任せる(ここでは削除しない)
        self.assertIn('PRODUCTION_TASK_NAME = "SekisanNaviData_Ops"', code)


if __name__ == "__main__":
    unittest.main()
