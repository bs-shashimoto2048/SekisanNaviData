# 定期実行と「今すぐ実行」の共通仕様(ops/host_targets.py・ops/runner.py の run_scheduled_host / start_run_now・ops/server.py の /api/run-now)のテスト。
# 合成データとモックだけ。実図面・社内共有・本番 DB(ホスト)・本番 API・IJCAD・ODA・実タスクスケジューラ(schtasks)は使わない。製番はすべて合成(A1TEST...)。
import json
import subprocess
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
sys.path.insert(0, str(ROOT / "tests"))

from ops import host_targets, runner as runner_mod, scheduler as scheduler_mod, server as server_mod  # noqa: E402
from ops.history import COMPLETED, FAILED, KIND_RUN_NOW, KIND_SCHEDULED, KIND_SCHEDULED_CHECK, History  # noqa: E402
from ops.host_targets import HOST_FAILED_REASON, OUTPUT_DIR_REASON, TargetError, TargetPlan, resolve_targets  # noqa: E402
from ops.runner import SKIPPED, Busy, RunManager  # noqa: E402
from ops.settings_store import OpsSettings, SettingsError, SettingsStore  # noqa: E402
from pipeline import product_flow  # noqa: E402

HDR = {"Content-Type": "application/json", "X-Ops-Request": "1"}


def rows(*products):
    return [{"SEIBAN": p} for p in products]


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="run_now_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name).resolve()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.store = SettingsStore(self.state)
        self.history = History(self.state)
        self.events, self.main_calls, self.fetch_calls = [], [], []
        self.host_rows = rows("A1TEST0001", "A1TEST0002", "A1TEST0003", "B2OTHER001")
        guard = mock.patch.object(subprocess, "run", side_effect=AssertionError("実プロセス/schtasks を実行しようとした"))
        guard.start()
        self.addCleanup(guard.stop)

    def fetch(self):
        self.fetch_calls.append(1)
        return list(self.host_rows)

    def resolver(self, root):
        return resolve_targets(str(self.out), "A1", fetch=self.fetch)

    def main(self, **kw):
        self.main_calls.append(kw)
        return {"completed": list(kw.get("products") or []), "failed": {}, "skipped": {}}

    def manager(self, main=None, resolver="default", output_guard=None, code_guard=None, com_init=None):
        return RunManager(self.state, self.store, self.history, main or self.main, config_path="cfg.yaml",
                          com_init=com_init or (lambda: self.events.append("init")), com_uninit=lambda: self.events.append("uninit"),
                          output_guard=output_guard, code_guard=code_guard,
                          target_resolver=self.resolver if resolver == "default" else resolver)

    def last_run(self):
        return self.history.runs(limit=1)[0]


# ------------------------------------------------------------------ 対象の確定(ホスト照会・出力済み除外・未完了の再処理)
class ResolveTargetsTest(Base):
    def test_uses_only_initial_a1_from_the_host_and_excludes_finished_folders(self):
        (self.out / "A1TEST0002").mkdir()                                       # 出力済み(マーカーなし)は除く
        plan = resolve_targets(str(self.out), "A1", fetch=self.fetch)
        self.assertEqual(plan.targets, ("A1TEST0001", "A1TEST0003"))            # 頭文字 B2 は対象外。固定リストは使わない
        self.assertEqual(plan.listed, 3)

    def test_includes_a_folder_with_an_incomplete_marker_for_reprocessing(self):
        (self.out / "A1TEST0002").mkdir()
        (self.out / "A1TEST0003").mkdir()
        product_flow.mark_incomplete(str(self.out), "A1TEST0002")               # 未完了マーカーあり → 再処理
        plan = resolve_targets(str(self.out), "A1", fetch=self.fetch)
        self.assertEqual(plan.targets, ("A1TEST0001", "A1TEST0002"))

    def test_empty_month_is_zero_targets_not_an_error(self):
        self.host_rows = []
        self.assertEqual(resolve_targets(str(self.out), "A1", fetch=self.fetch).targets, ())

    def test_host_failure_is_an_error_not_zero_targets(self):
        def boom():
            raise ConnectionError(r"C:\secret\host password=pw123")
        with self.assertRaises(TargetError) as cm:
            resolve_targets(str(self.out), "A1", fetch=boom)
        self.assertEqual(str(cm.exception), HOST_FAILED_REASON.format(kind="ConnectionError"))
        self.assertNotIn("secret", str(cm.exception))
        self.assertNotIn("pw123", str(cm.exception))

    def test_unreadable_output_dir_is_an_error_so_that_everything_is_not_reprocessed(self):
        with self.assertRaises(TargetError) as cm:
            resolve_targets(str(self.tmp / "missing"), "A1", fetch=self.fetch)
        self.assertEqual(str(cm.exception), OUTPUT_DIR_REASON)

    def test_reads_the_host_with_select_only(self):
        fake_win32 = mock.MagicMock()
        fake_conn, fake_rs = mock.MagicMock(), mock.MagicMock()
        fake_win32.client.Dispatch.side_effect = lambda name: fake_conn if name == "ADODB.Connection" else fake_rs
        fake_rs.EOF = True
        with mock.patch.dict(sys.modules, {"win32com": fake_win32, "win32com.client": fake_win32.client}), \
                mock.patch("bs_pkg.host_mod.get_tables", return_value=("TBL", ["F0", "F1", "F2", "F3", "F4", "F5", "F6", "DT"])), \
                mock.patch("bs_pkg.config_loader.load_yaml", return_value={"host_name": "h", "user_id": "u", "password": "p"}):
            self.assertEqual(host_targets.fetch_design3_rows(), [])
        sql = fake_rs.Open.call_args[0][0]
        self.assertTrue(sql.startswith("SELECT"))
        for word in ("UPDATE", "INSERT", "DELETE", "DROP"):
            self.assertNotIn(word, sql.upper())


# ------------------------------------------------------------------ 定期実行と今すぐ実行: 同じ処理
class SameProcessingTest(Base):
    def test_scheduled_and_run_now_call_the_same_main_once_with_the_same_targets_and_no_caps(self):
        self.host_rows = rows(*[f"A1TEST{n:04d}" for n in range(1, 31)])         # 30 件。件数・時間の上限で切らない
        m = self.manager()
        self.assertEqual(m.run_scheduled_host(), COMPLETED)
        _, t = m.start_run_now()
        t.join(5)
        self.assertEqual(len(self.main_calls), 2)                                # それぞれ 1 回だけ
        self.assertEqual(self.main_calls[0], self.main_calls[1])                 # 同じ引数(products・output_root・stop_after なし)
        self.assertNotIn("stop_after", self.main_calls[0])
        self.assertEqual(len(self.main_calls[0]["products"]), 30)
        runs = {r["kind"]: r for r in self.history.runs()}
        self.assertEqual(set(runs), {KIND_SCHEDULED, KIND_RUN_NOW})              # 履歴の種別だけが違う
        self.assertEqual(runs[KIND_SCHEDULED]["targets"], runs[KIND_RUN_NOW]["targets"])
        self.assertEqual(runs[KIND_RUN_NOW]["status"], COMPLETED)
        self.assertEqual({v["status"] for v in runs[KIND_RUN_NOW]["results"].values()}, {"完了"})

    def test_run_now_requeries_the_host_each_time_and_never_reuses_an_old_list(self):
        m = self.manager()
        _, t = m.start_run_now()
        t.join(5)
        self.host_rows = rows("A1TEST0009")                                      # ホストの内容が変わった
        _, t = m.start_run_now()
        t.join(5)
        self.assertEqual(len(self.fetch_calls), 2)
        self.assertEqual(self.main_calls[0]["products"], ["A1TEST0001", "A1TEST0002", "A1TEST0003"])
        self.assertEqual(self.main_calls[1]["products"], ["A1TEST0009"])

    def test_run_now_works_without_a_registered_or_enabled_task_and_never_touches_the_schedule(self):
        calls = []
        sched = scheduler_mod.TaskScheduler("p", "s", runner=lambda a: calls.append(a) or SimpleNamespace(returncode=1, stdout=b"", stderr=b""))
        before = self.store.save({"output_root": "", "schedule_enabled": False, "schedule_time": "05:00", "schedule_interval_days": 1})
        m = self.manager()
        _, t = m.start_run_now()                                                 # タスク未登録(照会は returncode=1)でも実行できる
        t.join(5)
        self.assertEqual(self.last_run()["status"], COMPLETED)
        self.assertEqual(calls, [])                                              # schtasks を一切呼ばない(登録・有効化・時刻変更なし)
        self.assertEqual(self.store.load(), before)                              # 設定(有効/無効・時刻・間隔)も変わらない
        self.assertEqual(sched.status()["registered"], False)

    def test_no_targets_is_skipped_not_a_zero_success(self):
        self.host_rows = rows()
        self.assertEqual(self.manager().run_scheduled_host(), SKIPPED)
        run = self.last_run()
        self.assertEqual((run["status"], run["product"]), (SKIPPED, runner_mod.NO_TARGET_LABEL))
        self.assertEqual(self.main_calls, [])


# ------------------------------------------------------------------ 二重起動・ロック
class LockTest(Base):
    def test_run_now_is_refused_while_another_run_holds_the_lock_and_the_lock_is_released_after(self):
        gate, started = threading.Event(), threading.Event()

        def slow(**kw):
            started.set()
            gate.wait(5)
            return {"completed": list(kw["products"]), "failed": {}, "skipped": {}}
        m = self.manager(main=slow)
        _, t = m.start_run_now()
        self.assertTrue(started.wait(5))
        other = RunManager(self.state, self.store, self.history, self.main, target_resolver=self.resolver,   # 別プロセス相当(同じ状態フォルダ・別の管理)
                           com_init=lambda: None, com_uninit=lambda: None)
        with self.assertRaises(Busy):
            other.start_run_now()
        self.assertEqual(other.run_scheduled_host(), SKIPPED)                    # 定期実行も二重起動しない
        with self.assertRaises(Busy):
            m.start_run_now()
        self.assertEqual(self.main_calls, [])                                    # 二重に処理しない
        self.assertEqual([r["kind"] for r in self.history.runs() if r["status"] == "実行中"], [KIND_RUN_NOW])
        gate.set()
        t.join(5)
        self.assertFalse(m.lock.is_held())                                       # 完了後は解放される
        _, t2 = m.start_run_now()
        t2.join(5)
        self.assertEqual(self.last_run()["status"], COMPLETED)


# ------------------------------------------------------------------ 製番ごとの失敗・全体の事前確認の失敗
class FailureTest(Base):
    def test_failed_products_do_not_stop_the_rest_and_the_whole_run_is_failed_afterwards(self):
        def partial(**kw):
            self.main_calls.append(kw)
            return {"completed": ["A1TEST0001", "A1TEST0003"], "failed": {"A1TEST0002": "処理が途中で終了しました(SystemExit)"}, "skipped": {}}
        m = self.manager(main=partial)
        self.assertEqual(m.run_scheduled_host(), FAILED)
        run = self.last_run()
        self.assertEqual({k: v["status"] for k, v in run["results"].items()}, {"A1TEST0001": "完了", "A1TEST0002": "失敗", "A1TEST0003": "完了"})
        self.assertFalse(m.lock.is_held())
        self.assertEqual(self.events, ["init", "uninit"])

    def test_system_exit_and_normal_exception_per_product_continue_through_the_real_product_loop(self):
        """app.main と同じ run_product で、1 件目が SystemExit・2 件目が通常例外でも 3 件目まで処理し、全体は失敗になる。"""
        seen = []

        def process(ctx):
            seen.append(ctx.product)
            if ctx.product == "A1TEST0001":
                raise SystemExit(3)
            if ctx.product == "A1TEST0002":
                raise RuntimeError("x")
            return product_flow.ProductResult(product_flow.COMPLETED, "")

        def main(**kw):
            results = {"completed": [], "failed": {}, "skipped": {}}
            for p in kw["products"]:
                ctx = SimpleNamespace(product=p, output_dir=str(self.out), dir_product=str(self.out / p),
                                      logger=mock.MagicMock(), manager=None)
                product_flow.run_product(ctx, results)
            return results
        with mock.patch.object(product_flow, "process_product", process), \
                mock.patch.object(product_flow, "cleanup_after_exception", lambda c: None), \
                mock.patch.object(product_flow, "clear_incomplete_marker", lambda *a: None):
            self.assertEqual(self.manager(main=main).run_scheduled_host(), FAILED)
        self.assertEqual(seen, ["A1TEST0001", "A1TEST0002", "A1TEST0003"])
        statuses = {k: v["status"] for k, v in self.last_run()["results"].items()}
        self.assertEqual(statuses, {"A1TEST0001": "失敗", "A1TEST0002": "失敗", "A1TEST0003": "完了"})

    def test_system_exit_outside_the_product_loop_is_a_failure_and_releases_everything(self):
        def exits(**kw):
            raise SystemExit(1)
        m = self.manager(main=exits)
        _, t = m.start_run_now()
        t.join(5)
        run = self.last_run()
        self.assertEqual(run["status"], FAILED)
        self.assertEqual(run["reason"], runner_mod.SYSTEM_EXIT_REASON)
        self.assertFalse(m.lock.is_held())
        self.assertEqual(self.events, ["init", "uninit"])

    def test_host_failure_starts_nothing_and_is_recorded_as_a_whole_failure(self):
        def host_down(root):
            raise TargetError(HOST_FAILED_REASON.format(kind="OSError"))
        for start in ("scheduled", "run_now"):
            m = self.manager(resolver=host_down)
            if start == "scheduled":
                self.assertEqual(m.run_scheduled_host(), FAILED)
            else:
                _, t = m.start_run_now()
                t.join(5)
            run = self.last_run()
            self.assertEqual(run["status"], FAILED)
            self.assertIn("ホストの製番一覧を取得できない", run["reason"])        # 理由が履歴に残る
            self.assertEqual(m.refusal, run["reason"])
            self.assertFalse(m.lock.is_held())
        self.assertEqual(self.main_calls, [])                                    # 製番処理を始めていない(0 件成功にもしない)

    def test_safety_check_failures_start_nothing_and_record_a_failure(self):
        def bad_output(root, source):
            raise SettingsError("出力先が本番と違うため実行しません")

        def stale_code():
            raise SettingsError("コード版が変わったため実行しません")
        cases = {"output": dict(output_guard=bad_output), "code": dict(code_guard=stale_code), "no-resolver": dict(resolver=None),
                 "com": dict(com_init=mock.Mock(side_effect=OSError("com")))}
        for name, kw in cases.items():
            self.events.clear()
            m = self.manager(**kw)
            self.assertEqual(m.run_scheduled_host(), FAILED, name)
            self.assertEqual(self.last_run()["status"], FAILED, name)
            self.assertTrue(self.last_run()["reason"], name)
            self.assertFalse(m.lock.is_held(), name)
        self.assertEqual(self.main_calls, [])
        self.assertEqual(self.fetch_calls, [])                                   # 全体の条件を満たさないときは、ホストの照会もしない

    def test_inference_api_abort_marks_every_target_not_started_and_fails_the_run(self):
        def aborted(**kw):
            return {"completed": [], "failed": {p: "推論 API を利用できません" for p in kw["products"]}, "skipped": {},
                    "aborted": "推論 API を利用できません"}
        self.assertEqual(self.manager(main=aborted).run_scheduled_host(), FAILED)
        results = self.last_run()["results"]
        self.assertEqual({v["status"] for v in results.values()}, {"未開始"})
        self.assertEqual({v.get("scope") for v in results.values()}, {"run"})


# ------------------------------------------------------------------ 検証用(dry-run)
class DryRunTest(Base):
    def test_dry_run_confirms_targets_without_processing(self):
        m = self.manager()
        self.assertEqual(m.run_scheduled_host_dry_run(), COMPLETED)
        run = self.last_run()
        self.assertEqual(run["kind"], KIND_SCHEDULED_CHECK)
        self.assertEqual(run["targets"], ["A1TEST0001", "A1TEST0002", "A1TEST0003"])
        self.assertEqual(self.main_calls, [])


# ------------------------------------------------------------------ 画面(HTTP)
class ServerTest(Base):
    def setUp(self):
        super().setUp()
        self.gate = threading.Event()
        self.gate.set()

        def main(**kw):
            self.main_calls.append(kw)
            self.gate.wait(5)
            return {"completed": list(kw["products"]), "failed": {}, "skipped": {}}
        self.sched_calls = []
        sched = scheduler_mod.TaskScheduler("p", "s", runner=lambda a: self.sched_calls.append(list(a)) or SimpleNamespace(returncode=1, stdout=b"", stderr=b""))
        runs = self.manager(main=main)
        self.ctx = server_mod.AppContext(store=self.store, history=self.history, runs=runs, scheduler=sched, config_path="cfg.yaml",
                                         run_checks=lambda c, s: [])
        self.server = server_mod.make_server(self.ctx, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def call(self, method, path, body=None, headers=HDR, host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode("utf-8"), headers=dict(headers))
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def wait_idle(self):
        for _ in range(100):
            if not self.ctx.runs.lock.is_held():
                return
            threading.Event().wait(0.05)
        self.fail("実行が終わりません")

    def test_run_now_needs_the_custom_header_a_loopback_host_and_explicit_confirmation(self):
        self.assertEqual(self.call("POST", "/api/run-now", {"confirmed": True}, headers={"Content-Type": "application/json"})[0], 403)   # 他サイトからの送信相当
        self.assertEqual(self.call("POST", "/api/run-now", {"confirmed": True}, host="evil.example")[0], 403)
        code, body = self.call("POST", "/api/run-now", {})                         # 確認ダイアログなし
        self.assertEqual(code, 400)
        self.assertIn("確認", body["error"])
        self.assertEqual(self.call("POST", "/api/run-now", {"confirmed": "yes"})[0], 400)
        self.assertEqual((self.main_calls, self.fetch_calls, self.history.runs()), ([], [], []))        # どれも何も始めない

    def test_run_now_is_accepted_runs_once_and_does_not_touch_the_schedule(self):
        code, body = self.call("POST", "/api/run-now", {"confirmed": True})
        self.assertEqual((code, body["ok"]), (200, True))
        self.wait_idle()
        self.assertEqual(len(self.main_calls), 1)
        self.assertEqual(self.sched_calls, [])                                    # タスクの登録・有効化・時刻変更なし
        run = self.history.runs()[0]
        self.assertEqual((run["id"], run["kind"], run["status"]), (body["id"], KIND_RUN_NOW, COMPLETED))

    def test_double_press_is_refused_with_already_running_and_state_is_restorable_from_history(self):
        self.gate.clear()
        code, _ = self.call("POST", "/api/run-now", {"confirmed": True})
        self.assertEqual(code, 200)
        for _ in range(100):
            if self.main_calls:
                break
            threading.Event().wait(0.05)
        code, body = self.call("POST", "/api/run-now", {"confirmed": True})
        self.assertEqual(code, 409)
        self.assertIn("すでに実行中", body["error"])
        # 画面を再読み込みしても、実行中であること・対象を、状態と履歴から復元できる
        self.assertTrue(self.call("GET", "/api/state")[1]["status"]["running"])
        run = self.call("GET", "/api/history")[1]["runs"][0]
        self.assertEqual((run["kind"], run["status"], len(run["targets"])), (KIND_RUN_NOW, "実行中", 3))
        self.gate.set()
        self.wait_idle()
        run = self.call("GET", "/api/history")[1]["runs"][0]
        self.assertEqual(run["status"], COMPLETED)
        self.assertEqual(sorted(run["results"]), ["A1TEST0001", "A1TEST0002", "A1TEST0003"])               # 完了後は製番ごとの結果が復元できる
        self.assertEqual(len(self.main_calls), 1)

    def test_a_failed_whole_run_is_visible_in_the_history_with_its_reason(self):
        self.ctx.runs.target_resolver = lambda root: (_ for _ in ()).throw(TargetError(HOST_FAILED_REASON.format(kind="OSError")))
        self.assertEqual(self.call("POST", "/api/run-now", {"confirmed": True})[0], 200)
        self.wait_idle()
        run = self.call("GET", "/api/history")[1]["runs"][0]
        self.assertEqual(run["status"], FAILED)
        self.assertIn("ホストの製番一覧を取得できない", run["reason"])
        self.assertEqual(self.main_calls, [])

    def test_the_page_has_the_run_now_button_with_a_confirmation_that_names_production_output(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        html = urllib.request.urlopen(req, timeout=5).read().decode("utf-8")
        for text in ('id="runnow"', "今すぐ実行", "当月の3番図面出図・initials=A1・未処理または未完了マーカーあり", "本番の出力", "window.confirm",
                     '"実行中"', "完了 ", "未開始"):
            self.assertIn(text, html, text)

    def test_settings_no_longer_have_a_fixed_product_list_or_caps(self):
        fields = set(OpsSettings().as_dict())
        self.assertEqual(fields, {"output_root", "schedule_enabled", "schedule_time", "schedule_interval_days"})
        (self.state / "ops.local.yaml").write_text("schedule_enabled: true\nschedule_time: '05:00'\nschedule_interval_days: 1\n"
                                                    "scheduled_products: [A1OLD0001]\nscheduled_max_products: 1\n", encoding="utf-8")
        loaded = self.store.load()                                                 # 古いキーは無視する(固定リストは使われない)
        self.assertEqual((loaded.schedule_time, loaded.schedule_enabled), ("05:00", True))
        self.store.save(loaded.as_dict())
        self.assertNotIn("scheduled_products", (self.state / "ops.local.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
