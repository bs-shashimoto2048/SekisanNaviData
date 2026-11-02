# 定期実行の対象範囲の限定と誤出力防止(ops/scheduled_plan.py・ops/runner.py の run_scheduled_targeted・ops_console.py)のテスト。
# 合成データとモックだけ。実図面・社内共有・本番 DB・本番 API・IJCAD・ODA・実タスクスケジューラ(schtasks)は使わない。
# 製番はすべて合成(A1TEST...)で、実際の対象リストは持たない。
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import yaml  # noqa: E402

from ops import runner as runner_mod, scheduler as scheduler_mod  # noqa: E402
from ops.history import COMPLETED, FAILED, History  # noqa: E402
from ops.runner import NO_TARGET_LABEL, SKIPPED, Busy, RunManager  # noqa: E402
from ops.scheduled_plan import (DEMOTE_AFTER, REASON_DONE, REASON_NOT_CONFIGURED, REASON_OVER_LIMIT, build_plan, failure_streaks,  # noqa: E402
                                results_from)
from ops.settings_store import (MAX_SCHEDULED_MINUTES_LIMIT, MAX_SCHEDULED_PRODUCTS_LIMIT, OpsSettings, SettingsError,  # noqa: E402
                                SettingsStore, validate)
from test_main_integration import PRODUCT, MainIntegrationBase  # noqa: E402

LISTED = [f"A1TEST{n:04d}" for n in range(1, 9)]         # 合成の製番(8 件)


def tree(path: Path):
    return sorted((p.relative_to(path).as_posix(), p.stat().st_size) for p in path.rglob("*") if p.is_file())


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="sched_scope_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name).resolve()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.out = self.tmp / "out" / "a_product" / "output"          # 実効の出力先(製番フォルダの親)
        self.out.mkdir(parents=True)
        self.store = SettingsStore(self.state)
        self.history = History(self.state)
        self.calls = []
        self.done = set()
        self.events = []
        guard = mock.patch.object(subprocess, "run", side_effect=AssertionError("実プロセス/schtasks を実行しようとした"))
        guard.start()
        self.addCleanup(guard.stop)

    def settings(self, **kw):
        data = {"scheduled_products": LISTED, "scheduled_max_products": 8, "scheduled_max_minutes": 120}
        data.update(kw)
        return self.store.save(data)

    def main(self, **kw):
        self.calls.append(kw)
        return {"completed": list(kw.get("products") or []), "failed": {}, "skipped": {}}

    def manager(self, main=None, output_guard=None, code_guard=None, resolver="default"):
        resolver = (lambda root: str(self.out)) if resolver == "default" else resolver
        return RunManager(self.state, self.store, self.history, main or self.main, config_path="cfg.yaml",
                          com_init=lambda: self.events.append("init"), com_uninit=lambda: self.events.append("uninit"),
                          output_guard=output_guard, code_guard=code_guard, output_dir_resolver=resolver,
                          is_done=lambda out, p: p in self.done)


# ------------------------------------------------------------------ 設定
class SettingsTest(Base):
    def test_defaults_are_safe_and_empty(self):
        s = OpsSettings()
        self.assertEqual((s.scheduled_products, s.scheduled_max_products, s.scheduled_max_minutes), ((), 5, 120))
        self.assertEqual(self.store.load(), OpsSettings())                      # ファイルが無くても同じ(既定値で無制限にはならない)

    def test_round_trip_keeps_order_and_removes_duplicates(self):
        saved = self.settings(scheduled_products=["A1TEST0003", "A1TEST0001", "A1TEST0003", " A1TEST0002 "], scheduled_max_products=2, scheduled_max_minutes=45)
        self.assertEqual(saved.scheduled_products, ("A1TEST0003", "A1TEST0001", "A1TEST0002"))
        self.assertEqual(SettingsStore(self.state).load(), saved)
        self.assertEqual(self.store.load().as_dict()["scheduled_products"], ["A1TEST0003", "A1TEST0001", "A1TEST0002"])

    def test_missing_or_null_values_use_the_defaults_never_unlimited(self):
        (self.state / "ops.local.yaml").write_text("output_root: ''\nscheduled_max_products: null\nscheduled_max_minutes: null\nscheduled_products: null\n", encoding="utf-8")
        s = self.store.load()
        self.assertEqual((s.scheduled_products, s.scheduled_max_products, s.scheduled_max_minutes), ((), 5, 120))

    def test_invalid_limits_are_rejected(self):
        for bad in (0, -1, MAX_SCHEDULED_PRODUCTS_LIMIT + 1, True, "5", 2.5, [], 10 ** 6):
            with self.assertRaises(SettingsError, msg=repr(bad)):
                validate({"scheduled_max_products": bad})
        for bad in (0, 9, -5, MAX_SCHEDULED_MINUTES_LIMIT + 1, False, "60", 60.0, 10 ** 6):
            with self.assertRaises(SettingsError, msg=repr(bad)):
                validate({"scheduled_max_minutes": bad})
        for ok in (1, MAX_SCHEDULED_PRODUCTS_LIMIT):
            validate({"scheduled_max_products": ok})
        for ok in (10, MAX_SCHEDULED_MINUTES_LIMIT):
            validate({"scheduled_max_minutes": ok})

    def test_a_stored_invalid_value_is_an_error_not_a_silent_fallback(self):
        (self.state / "ops.local.yaml").write_text("scheduled_max_products: 0\n", encoding="utf-8")
        with self.assertRaises(SettingsError):
            self.store.load()

    def test_one_invalid_product_rejects_the_whole_list(self):
        for bad in (["A1TEST0001", "bad product"], ["A1TEST0001", ""], ["A1TEST0001", 123], "A1TEST0001", {"a": 1}, ["A1/../x"], ["A1"]):
            with self.assertRaises(SettingsError, msg=repr(bad)):
                validate({"scheduled_products": bad})
        with self.assertRaises(SettingsError):
            validate({"scheduled_products": [f"A1TEST{n:05d}" for n in range(501)]})

    def test_saving_from_the_screen_without_the_scope_keys_keeps_the_stored_scope(self):
        self.settings(scheduled_products=LISTED[:3], scheduled_max_products=2, scheduled_max_minutes=30)
        self.store.save({"output_root": "", "schedule_enabled": False, "schedule_time": "07:00", "schedule_interval_days": 1})   # 画面の保存(対象範囲を含まない)
        kept = self.store.load()
        self.assertEqual((kept.scheduled_products, kept.scheduled_max_products, kept.scheduled_max_minutes), (tuple(LISTED[:3]), 2, 30))
        self.assertEqual(kept.schedule_time, "07:00")
        self.store.save({"scheduled_products": [], "scheduled_max_products": 3, "scheduled_max_minutes": 60})      # 明示すれば変えられる
        self.assertEqual(self.store.load().scheduled_products, ())

    def test_the_local_file_is_unchanged_when_the_scope_is_default(self):
        self.store.save({"output_root": "", "schedule_enabled": False, "schedule_time": "08:30", "schedule_interval_days": 3})
        self.assertEqual(set(yaml.safe_load((self.state / "ops.local.yaml").read_text(encoding="utf-8"))),
                         {"output_root", "schedule_enabled", "schedule_time", "schedule_interval_days"})


# ------------------------------------------------------------------ 対象の確定
class PlanTest(Base):
    def test_empty_or_unset_list_gives_zero_targets(self):
        for products in ((), None):
            plan = build_plan(OpsSettings(scheduled_products=tuple(products or ())), str(self.out), lambda o, p: False)
            self.assertEqual((plan.targets, plan.excluded, plan.listed), ((), (), 0))
            self.assertEqual(plan.summary(), REASON_NOT_CONFIGURED)

    def test_only_listed_products_in_order_done_ones_excluded_and_limit_applied_from_the_front(self):
        done = {LISTED[1], LISTED[4]}
        plan = build_plan(OpsSettings(scheduled_products=tuple(LISTED), scheduled_max_products=3, scheduled_max_minutes=60),
                          str(self.out), lambda o, p: p in done)
        self.assertEqual(plan.targets, (LISTED[0], LISTED[2], LISTED[3]))
        self.assertEqual(dict(plan.excluded)[LISTED[1]], REASON_DONE)
        self.assertEqual(dict(plan.excluded)[LISTED[4]], REASON_DONE)
        self.assertEqual([p for p, r in plan.excluded if r == REASON_OVER_LIMIT], LISTED[5:])
        self.assertTrue(set(plan.targets) <= set(LISTED))

    def test_default_done_rule_matches_the_product_flow_rule(self):
        from ops.scheduled_plan import default_is_done
        from pipeline import product_flow
        (self.out / "A1DONE0001").mkdir()
        (self.out / "A1MARK0001").mkdir()
        product_flow.mark_incomplete(str(self.out), "A1MARK0001")
        self.assertTrue(default_is_done(str(self.out), "A1DONE0001"))        # フォルダあり・マーカーなし = 処理済み
        self.assertFalse(default_is_done(str(self.out), "A1MARK0001"))       # マーカーあり = 再処理の対象
        self.assertFalse(default_is_done(str(self.out), "A1NONE0001"))       # フォルダなし


# ------------------------------------------------------------------ 定期実行(runner)
class ScheduledRunTest(Base):
    def test_empty_list_processes_nothing_and_does_not_fall_back_to_the_host_list(self):
        for products in ([], None):
            self.store.save({"scheduled_products": products} if products is not None else {})
            mgr = self.manager()
            self.assertEqual(mgr.run_scheduled_targeted(), SKIPPED)
            self.assertEqual(self.calls, [])                                   # app.main(出図リストを引く全件処理)を呼ばない
            self.assertEqual(self.events, [])                                  # COM も初期化しない
            self.assertFalse(mgr.status()["running"])
        run = self.history.runs()[0]
        self.assertEqual((run["kind"], run["product"], run["status"]), ("定期", NO_TARGET_LABEL, SKIPPED))
        self.assertEqual(run["targets"], [])
        self.assertIn("未設定", run["reason"])

    def test_only_the_listed_products_are_processed_and_the_plan_is_recorded_before_the_run(self):
        self.settings(scheduled_products=LISTED[:3], scheduled_max_products=8)
        seen = {}

        def main(**kw):
            running = self.history.runs()[0]                                   # 実行中の時点で、対象が履歴に記録済み
            seen.update(running)
            return self.main(**kw)
        mgr = self.manager(main)
        self.assertEqual(mgr.run_scheduled_targeted(), COMPLETED)
        self.assertEqual(self.calls[0]["products"], LISTED[:3])               # 許可リストの製番だけ(リスト外・出図リストは渡さない)
        self.assertEqual(self.calls[0]["config_path"], "cfg.yaml")
        self.assertEqual(seen["status"], "実行中")
        self.assertEqual(seen["targets"], LISTED[:3])
        self.assertIn("対象 3 件", seen["reason"])
        self.assertEqual(self.history.runs()[0]["targets"], LISTED[:3])
        self.assertEqual(self.events, ["init", "uninit"])

    def test_count_limit_done_products_and_order(self):
        self.done = {LISTED[0], LISTED[2]}
        self.settings(scheduled_products=LISTED, scheduled_max_products=3)
        self.manager().run_scheduled_targeted()
        self.assertEqual(self.calls[0]["products"], [LISTED[1], LISTED[3], LISTED[4]])    # 処理済みを除き、先頭から 3 件
        self.assertEqual(self.history.runs()[0]["targets"], [LISTED[1], LISTED[3], LISTED[4]])

    def test_everything_done_is_a_no_op(self):
        self.done = set(LISTED)
        self.settings()
        mgr = self.manager()
        self.assertEqual(mgr.run_scheduled_targeted(), SKIPPED)
        self.assertEqual(self.calls, [])

    def test_time_limit_is_passed_as_a_monotonic_deadline(self):
        self.settings(scheduled_products=LISTED[:2], scheduled_max_minutes=45)
        with mock.patch.object(runner_mod.time, "monotonic", return_value=1000.0):
            self.manager().run_scheduled_targeted()
        self.assertEqual(self.calls[0]["stop_after"], 1000.0 + 45 * 60)

    def test_products_skipped_by_the_time_limit_are_reported_but_the_run_is_not_a_failure(self):
        self.settings(scheduled_products=LISTED[:3])
        mgr = self.manager(lambda **kw: {"completed": [LISTED[0]], "failed": {}, "skipped": {LISTED[1]: "実行時間の上限に達したため開始しませんでした", LISTED[2]: "実行時間の上限に達したため開始しませんでした"}})
        self.assertEqual(mgr.run_scheduled_targeted(), COMPLETED)
        self.assertIn("2 件は時間上限のため開始せず", self.history.runs()[0]["reason"])

    def test_a_failed_product_is_a_failed_run(self):
        self.settings(scheduled_products=LISTED[:2])
        mgr = self.manager(lambda **kw: {"completed": [LISTED[0]], "failed": {LISTED[1]: "RuntimeError: x"}, "skipped": {}})
        self.assertEqual(mgr.run_scheduled_targeted(), FAILED)
        self.assertIn("失敗 1 件", self.history.runs()[0]["reason"])

    def test_busy_run_is_recorded_as_skipped_without_calling_main(self):
        self.settings(scheduled_products=LISTED[:2])
        mgr = self.manager()
        other = runner_mod.RunLock(self.state)
        other.acquire("manual")
        try:
            self.assertEqual(mgr.run_scheduled_targeted(), SKIPPED)
        finally:
            other.release()
        self.assertEqual(self.calls, [])
        self.assertIn("二重起動", self.history.runs()[0]["reason"])

    def test_the_legacy_all_products_run_is_unchanged_for_the_interim_operation(self):
        mgr = self.manager()
        self.assertEqual(mgr.run_scheduled(), COMPLETED)                       # 暫定運用(interim)が共有する従来の全件処理
        self.assertEqual(self.calls, [{"config_path": "cfg.yaml"}])            # 製番・時間上限の指定なし
        self.assertEqual(self.history.runs()[0]["product"], runner_mod.ALL_PRODUCTS)


# ------------------------------------------------------------------ 出力先・コード版の事前検査(副作用なし)
class GuardsTest(Base):
    def assert_refused_without_side_effects(self, mgr, before):
        self.assertEqual(mgr.run_scheduled_targeted() if not getattr(self, "dry", False) else mgr.run_scheduled_targeted_dry_run(), FAILED)
        self.assertTrue(mgr.refusal)
        self.assertEqual(tree(self.state), before)                             # 状態フォルダに何も作らない(履歴・ロックファイルも作らない)
        self.assertEqual(self.history.runs(), [])                              # 履歴を作らない
        self.assertEqual(self.calls, [])                                       # app.main(フォルダ作成・印刷・API 呼び出し)を呼ばない
        self.assertEqual(self.events, [])                                      # COM も初期化しない
        self.assertEqual(tree(self.tmp / "out"), [])                           # 出力先に何も作らない
        probe = runner_mod.RunLock(self.state)
        probe.acquire("probe")                                                 # ロックは取られていない(自由に取れる)
        probe.release()

    def snapshot(self):
        return tree(self.state)

    def test_output_root_mismatch_is_refused_before_anything(self):
        self.settings(output_root=str(self.tmp / "wrong"))
        before = self.snapshot()
        guard = mock.Mock(side_effect=SettingsError("運用設定の出力先が、設定 data.output と異なるため、実行しませんでした。"))
        mgr = self.manager(output_guard=guard)
        self.assert_refused_without_side_effects(mgr, before)
        guard.assert_called_once_with(str(self.tmp / "wrong"), "ops_setting")
        self.assertIn("出力先", mgr.refusal)

    def test_code_version_mismatch_is_refused_before_anything(self):
        self.settings()
        before = self.snapshot()
        code = mock.Mock(side_effect=SettingsError("コード(作業ツリーのコミット)が aaaaaaa から bbbbbbb へ更新されています。"))
        mgr = self.manager(output_guard=mock.Mock(), code_guard=code)
        self.assert_refused_without_side_effects(mgr, before)
        code.assert_called_once_with()

    def test_the_guards_run_before_the_plan_is_even_built(self):
        self.settings()
        resolver = mock.Mock(side_effect=AssertionError("検査より前に対象を確定しない"))
        mgr = self.manager(output_guard=mock.Mock(side_effect=SettingsError("拒否")), resolver=resolver)
        self.assertEqual(mgr.run_scheduled_targeted(), FAILED)
        resolver.assert_not_called()

    def test_missing_output_resolution_is_refused(self):
        self.settings()
        mgr = self.manager(resolver=None)
        self.assertEqual(mgr.run_scheduled_targeted(), FAILED)
        self.assertIn("出力先を確認できない", mgr.refusal)
        self.assertEqual((self.calls, self.history.runs()), ([], []))

    def test_invalid_stored_settings_refuse_and_never_run_unlimited(self):
        (self.state / "ops.local.yaml").write_text("scheduled_products: [A1TEST0001]\nscheduled_max_products: 0\n", encoding="utf-8")
        mgr = self.manager()
        self.assertEqual(mgr.run_scheduled_targeted(), FAILED)
        self.assertEqual((self.calls, self.history.runs()), ([], []))

    def test_unexpected_failure_while_resolving_the_plan_is_a_fixed_refusal_without_paths(self):
        self.settings()
        mgr = self.manager(resolver=mock.Mock(side_effect=OSError(r"\\server\share\secret")))
        self.assertEqual(mgr.run_scheduled_targeted(), FAILED)
        self.assertNotIn("server", mgr.refusal)
        self.assertEqual((self.calls, self.history.runs()), ([], []))

    def test_passing_guards_allow_the_run(self):
        self.settings(scheduled_products=LISTED[:2], output_root="")
        out_guard, code_guard = mock.Mock(), mock.Mock()
        self.assertEqual(self.manager(output_guard=out_guard, code_guard=code_guard).run_scheduled_targeted(), COMPLETED)
        out_guard.assert_called_once_with("", "ops_setting")
        code_guard.assert_called_once_with()
        self.assertEqual(len(self.calls), 1)


class DryRunTest(GuardsTest):
    dry = True

    def test_dry_run_refuses_with_the_same_checks_and_no_side_effects(self):
        self.settings(output_root=str(self.tmp / "wrong"))
        before = self.snapshot()
        mgr = self.manager(output_guard=mock.Mock(side_effect=SettingsError("運用設定の出力先が、設定 data.output と異なるため、実行しませんでした。")))
        self.assert_refused_without_side_effects(mgr, before)

    def test_dry_run_with_a_stale_code_version_refuses(self):
        self.settings()
        before = self.snapshot()
        mgr = self.manager(output_guard=mock.Mock(), code_guard=mock.Mock(side_effect=SettingsError("コードが更新されています。")))
        self.assert_refused_without_side_effects(mgr, before)

    def test_dry_run_target_list_equals_the_one_recorded_before_a_real_run(self):
        self.done = {LISTED[1]}
        self.settings(scheduled_products=LISTED, scheduled_max_products=4)
        dry_mgr = self.manager(main=mock.Mock(side_effect=AssertionError("dry-run では app.main を呼ばない")))
        self.assertEqual(dry_mgr.run_scheduled_targeted_dry_run(), COMPLETED)
        dry = self.history.runs()[0]
        self.assertEqual(dry["kind"], "定期(検証)")
        self.assertEqual(self.calls, [])                                       # 製番処理をしない
        real_mgr = self.manager()
        self.assertEqual(real_mgr.run_scheduled_targeted(), COMPLETED)
        real = [r for r in self.history.runs() if r["kind"] == "定期"][0]
        self.assertEqual(dry["targets"], real["targets"])                     # dry-run と実行前の対象が一致
        self.assertEqual(self.calls[0]["products"], dry["targets"])
        self.assertEqual(dry_mgr.last_plan.targets, real_mgr.last_plan.targets)
        self.assertIn("検証のみ", dry["reason"])

    def test_dry_run_with_an_empty_list_reports_zero_targets(self):
        mgr = self.manager()
        self.assertEqual(mgr.run_scheduled_targeted_dry_run(), COMPLETED)
        self.assertEqual(self.history.runs()[0]["targets"], [])
        self.assertIn("未設定", self.history.runs()[0]["reason"])


# ------------------------------------------------------------------ app.main の時間上限・入口・タスクの起動先照会
class AppDeadlineTest(MainIntegrationBase):
    def test_no_new_product_starts_after_the_deadline(self):
        self.run_main(products=[PRODUCT], stop_after=time.monotonic() - 1)
        self.assertEqual(self.print_script_calls, [])
        self.assertEqual(self.convert_calls, [])
        self.assertEqual(self.api_calls, [])
        self.assertFalse(self.product_out.exists())                           # 製番フォルダを作らない
        self.assertEqual(self.results["completed"], [])
        self.assertIn("実行時間の上限", self.results["skipped"][PRODUCT])
        self.assertEqual(self.results["failed"], {})

    def test_a_deadline_in_the_future_changes_nothing(self):
        self.run_main(products=[PRODUCT], stop_after=time.monotonic() + 3600)
        self.assertEqual(self.results["completed"], [PRODUCT])

    def test_the_skip_reason_contains_the_marker_the_runner_looks_for(self):
        import app
        self.assertIn(runner_mod.DEADLINE_MARK, app.DEADLINE_SKIP_REASON)


class EntryPointTest(Base):
    def run_cli(self, runs, dry=False, scheduler=None):
        import importlib
        oc = importlib.import_module("ops_console")
        import os
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        ctx = SimpleNamespace(runs=runs, scheduler=scheduler or mock.Mock(action_target_state=mock.Mock(return_value="mismatch")))
        out, err = [], []
        with mock.patch.object(oc, "build_context", return_value=ctx), mock.patch("builtins.print", side_effect=lambda *a, file=None, **k: (err if file is sys.stderr else out).append(" ".join(map(str, a)))):
            code = oc.cmd_run_scheduled(SimpleNamespace(config=None, dry_run=dry))
        return code, "\n".join(out), "\n".join(err)

    def test_a_refusal_is_printed_to_stderr_and_the_exit_code_is_one(self):
        runs = SimpleNamespace(run_scheduled_targeted=lambda: "失敗", refusal="運用設定の出力先が、設定 data.output と異なるため、実行しませんでした。", last_plan=None)
        code, out, err = self.run_cli(runs)
        self.assertEqual(code, 1)
        self.assertIn("定期実行を開始しませんでした", err)
        self.assertIn("出力先", err)

    def test_dry_run_prints_the_plan_and_the_task_target_state(self):
        plan = build_plan(OpsSettings(scheduled_products=tuple(LISTED[:2])), str(self.out), lambda o, p: False)
        runs = SimpleNamespace(run_scheduled_targeted_dry_run=lambda: "完了", refusal=None, last_plan=plan)
        code, out, err = self.run_cli(runs, dry=True)
        self.assertEqual(code, 0)
        self.assertIn("今回の対象(この順): " + ", ".join(LISTED[:2]), out)
        self.assertIn("定期タスクの実行先", out)
        self.assertIn("別の場所", out)                                         # mismatch の説明

    def test_build_context_wires_both_guards_and_the_output_resolver_into_the_scheduled_path(self):
        import importlib
        oc = importlib.import_module("ops_console")
        ctx = oc.build_context(state_dir=self.state, config_path="cfg.yaml", main_func=lambda **kw: {}, scheduler=mock.Mock())
        from ops.code_version import CodeVersionGuard
        from ops.output_guard import OutputGuard
        self.assertIsInstance(ctx.runs.output_guard, OutputGuard)
        self.assertIsInstance(ctx.runs.code_guard, CodeVersionGuard)
        self.assertTrue(callable(ctx.runs.output_dir_resolver))


class OrderingAndResultsTest(Base):
    """失敗・スキップが続く製番が、毎回の件数上限を占有しないこと(後ろに回す)と、製番ごとの結果の記録。"""

    def run_with(self, outcomes, cap=1, retry=(), listed=None, n=1):
        """定期実行を n 回行い、各回に処理した製番を返す。outcomes[p] = ok / fail / skip(未指定は ok)。ok は出力フォルダができる(処理済み)。"""
        self.settings(scheduled_products=listed or LISTED[:4], scheduled_max_products=cap, scheduled_retry_products=list(retry))
        seen = []

        def main(**kw):
            seen.append(list(kw["products"]))
            res = {"completed": [], "failed": {}, "skipped": {}}
            for p in kw["products"]:
                what = outcomes.get(p, "ok")
                if what == "ok":
                    self.done.add(p)
                    res["completed"].append(p)
                elif what == "fail":
                    res["failed"][p] = "RuntimeError: 製番固有の失敗"
                else:
                    res["skipped"][p] = "処理対象外"
            return res
        mgr = self.manager(main)
        for _ in range(n):
            mgr.run_scheduled_targeted()
        return seen, mgr

    def test_a_head_product_that_keeps_failing_does_not_starve_the_rest_with_a_cap_of_one(self):
        seen, _ = self.run_with({LISTED[0]: "fail"}, cap=1, n=7)
        # 連続 2 回に達するまでは先頭のまま(従来どおり)→ 達したら後ろに回る → 後続が順に処理され → 最後に再試行される
        self.assertEqual([x[0] for x in seen], [LISTED[0], LISTED[0], LISTED[1], LISTED[2], LISTED[3], LISTED[0], LISTED[0]])

    def test_a_head_product_that_keeps_being_skipped_is_handled_the_same_way(self):
        seen, _ = self.run_with({LISTED[0]: "skip"}, cap=1, n=5)
        self.assertEqual([x[0] for x in seen], [LISTED[0], LISTED[0], LISTED[1], LISTED[2], LISTED[3]])

    def test_the_order_and_existing_behaviour_are_kept_until_two_consecutive_failures(self):
        seen, mgr = self.run_with({LISTED[0]: "fail"}, cap=2, n=1)
        self.assertEqual(seen, [[LISTED[0], LISTED[1]]])                       # 1 回の失敗では並べ替えない
        self.assertEqual(mgr.last_plan.deferred, ())
        mgr.run_scheduled_targeted()                                            # 2 回目: 連続は 1 回なので、先頭のまま(処理済みの製番は除かれる)
        self.assertEqual(seen[1], [LISTED[0], LISTED[2]])

    def test_a_failure_followed_by_a_success_resets_the_streak(self):
        self.settings(scheduled_products=LISTED[:3], scheduled_max_products=1)
        mgr = self.manager(lambda **kw: {"completed": [], "failed": {kw["products"][0]: "x"}, "skipped": {}})
        mgr.run_scheduled_targeted()
        runs = self.history.runs(limit=None)
        self.assertEqual(failure_streaks(runs, [LISTED[0]])[LISTED[0]], 1)
        self.history.start("定期", LISTED[0], targets=[LISTED[0]])
        rid = self.history.runs(limit=None)[0]["id"]
        self.history.finish(rid, "完了", "", results={LISTED[0]: {"status": "完了"}})
        self.assertEqual(failure_streaks(self.history.runs(limit=None), [LISTED[0]])[LISTED[0]], 0)

    def test_completed_products_are_still_excluded(self):
        self.done = {LISTED[0]}
        seen, _ = self.run_with({}, cap=2, n=1)
        self.assertEqual(seen, [[LISTED[1], LISTED[2]]])

    def test_deferred_products_are_not_deleted_and_are_retried_when_slots_remain(self):
        seen, mgr = self.run_with({LISTED[0]: "fail"}, cap=8, listed=LISTED[:3], n=3)
        self.assertEqual(seen[2], [LISTED[0]])                                 # 後続は処理済み。残った枠で、後ろに回した製番が再試行される
        self.assertEqual(self.store.load().scheduled_products, tuple(LISTED[:3]))   # リストからは消さない

    API_DOWN = "推論 API を利用できないため、製番処理を開始しませんでした(接続が拒否されました)"

    def api_down_main(self, **kw):
        """app.main が、API の事前確認に失敗したときの戻り値(製番ごとの failed と、実行全体の中断を示す aborted)。"""
        self.calls.append(kw)
        return {"completed": [], "failed": {p: self.API_DOWN for p in kw["products"]}, "skipped": {}, "aborted": self.API_DOWN}

    def test_api_unavailable_with_a_single_product_is_not_counted_and_never_defers_it(self):
        self.settings(scheduled_products=LISTED[:1], scheduled_max_products=1)
        mgr = self.manager(self.api_down_main)
        for _ in range(5):                                                      # 何度続いても
            self.assertEqual(mgr.run_scheduled_targeted(), FAILED)
        self.assertEqual(failure_streaks(self.history.runs(limit=None), LISTED[:1]), {LISTED[0]: 0})
        self.assertEqual(mgr.last_plan.deferred, ())                              # 後順位に回らない
        self.assertEqual(mgr.last_plan.targets, (LISTED[0],))
        res = self.history.runs()[0]["results"][LISTED[0]]
        self.assertEqual((res["status"], res["scope"]), ("未開始", "run"))        # 処理を始めていない・実行全体の中断

    def test_api_unavailable_with_several_products_is_not_counted_either(self):
        self.settings(scheduled_products=LISTED[:3], scheduled_max_products=3)
        mgr = self.manager(self.api_down_main)
        for _ in range(4):
            mgr.run_scheduled_targeted()
        self.assertEqual(failure_streaks(self.history.runs(limit=None), LISTED[:3]), {p: 0 for p in LISTED[:3]})
        self.assertEqual(mgr.last_plan.deferred, ())
        self.assertTrue(all(r["scope"] == "run" and r["status"] == "未開始" for r in self.history.runs()[0]["results"].values()))

    def test_api_unavailable_does_not_reset_or_add_to_a_streak_of_a_product_specific_failure(self):
        self.settings(scheduled_products=LISTED[:2], scheduled_max_products=1)
        fail = lambda **kw: {"completed": [], "failed": {kw["products"][0]: "RuntimeError: 製番固有の失敗"}, "skipped": {}}
        self.manager(fail).run_scheduled_targeted()                               # 失敗 1 回目
        for _ in range(3):
            self.manager(self.api_down_main).run_scheduled_targeted()             # API の中断は、数えない・リセットもしない
        self.assertEqual(failure_streaks(self.history.runs(limit=None), LISTED[:2])[LISTED[0]], 1)
        self.manager(fail).run_scheduled_targeted()                               # 失敗 2 回目 → 2 回連続
        self.assertEqual(failure_streaks(self.history.runs(limit=None), LISTED[:2])[LISTED[0]], 2)

    def test_identical_product_specific_failures_are_still_counted(self):
        """「同じ理由で失敗した」という推測で、環境の失敗とはみなさない(製番固有の失敗は、複数件でも数える)。"""
        self.settings(scheduled_products=LISTED[:2], scheduled_max_products=2)
        same = lambda **kw: {"completed": [], "failed": {p: "[Step.2] 印刷(PDF): 1 件が作成されませんでした(未完了)" for p in kw["products"]}, "skipped": {}}
        for _ in range(2):
            self.manager(same).run_scheduled_targeted()
        self.assertEqual(failure_streaks(self.history.runs(limit=None), LISTED[:2]), {LISTED[0]: 2, LISTED[1]: 2})
        r = self.history.runs()[0]["results"]
        self.assertTrue(all("scope" not in v for v in r.values()))

    def test_whole_run_exceptions_and_time_limit_skips_do_not_count(self):
        self.settings(scheduled_products=LISTED[:2], scheduled_max_products=2)

        def boom(**kw):
            raise RuntimeError("全体の失敗")
        for _ in range(3):
            self.manager(boom).run_scheduled_targeted()
        self.assertEqual(failure_streaks(self.history.runs(limit=None), LISTED[:2]), {LISTED[0]: 0, LISTED[1]: 0})
        late = lambda **kw: {"completed": [], "failed": {}, "skipped": {p: "実行時間の上限に達したため開始しませんでした" for p in kw["products"]}}
        for _ in range(3):
            self.manager(late).run_scheduled_targeted()
        self.assertEqual(failure_streaks(self.history.runs(limit=None), LISTED[:2]), {LISTED[0]: 0, LISTED[1]: 0})

    def test_retry_setting_returns_a_deferred_product_to_the_normal_order(self):
        seen, mgr = self.run_with({LISTED[0]: "fail"}, cap=1, n=3)
        self.assertEqual(seen[2][0], LISTED[1])                                 # 後ろに回っている
        self.settings(scheduled_products=LISTED[:4], scheduled_max_products=1, scheduled_retry_products=[LISTED[0]])
        m = self.manager()
        m.run_scheduled_targeted_dry_run()
        self.assertEqual(m.last_plan.targets, (LISTED[0],))                    # 再試行の指定で通常の順序(先頭)に戻る
        self.assertEqual(m.last_plan.deferred, ())

    def test_results_are_recorded_per_product_and_distinguish_not_started(self):
        self.settings(scheduled_products=LISTED[:4], scheduled_max_products=4)
        mgr = self.manager(lambda **kw: {"completed": [LISTED[0]], "failed": {LISTED[1]: "RuntimeError: x"},
                                         "skipped": {LISTED[2]: "処理対象外", LISTED[3]: "実行時間の上限に達したため開始しませんでした"}})
        mgr.run_scheduled_targeted()
        r = self.history.runs()[0]["results"]
        self.assertEqual({p: v["status"] for p, v in r.items()}, {LISTED[0]: "完了", LISTED[1]: "失敗", LISTED[2]: "スキップ", LISTED[3]: "未開始"})
        self.assertNotIn("scope", r[LISTED[1]])                                # 製番固有の失敗
        self.assertEqual(self.history.runs()[0]["targets"], LISTED[:4])

    def test_a_history_with_the_old_finish_signature_still_works_for_manual_and_legacy_runs(self):
        """暫定運用(interim)は、finish(run_id, status, reason) だけを受け取る自前の履歴を渡す。従来の全件処理・手動実行は、その呼び出し形のまま。"""
        calls = []

        class OldHistory:
            def __init__(self, inner):
                self.inner = inner

            def start(self, kind, product):
                return self.inner.start(kind, product)

            def finish(self, run_id, status, reason=""):
                calls.append((status, reason))
                return self.inner.finish(run_id, status, reason)

            def events(self):
                return self.inner.events()
        mgr = RunManager(self.state, self.store, OldHistory(self.history), self.main, config_path="cfg.yaml",
                         com_init=lambda: None, com_uninit=lambda: None)
        self.assertEqual(mgr.run_scheduled(), COMPLETED)
        mgr.start_manual("A1TEST0001")[1].join(5)
        self.assertEqual([c[0] for c in calls], ["完了", "完了"])

    def test_results_are_not_recorded_for_manual_runs(self):
        mgr = self.manager()
        mgr.start_manual("A1TEST0001")[1].join(5)
        self.assertNotIn("results", self.history.runs()[0])

    def test_dry_run_shows_the_same_order_and_the_reason_as_the_real_run(self):
        for _ in range(2):                                                      # 先頭が 2 回連続で失敗した履歴を作る
            self.run_with({LISTED[0]: "fail"}, cap=1, n=1)
            self.done.clear()
        self.settings(scheduled_products=LISTED[:4], scheduled_max_products=3)
        dry = self.manager(main=mock.Mock(side_effect=AssertionError("dry-run では app.main を呼ばない")))
        self.assertEqual(dry.run_scheduled_targeted_dry_run(), COMPLETED)
        dry_event = [r for r in self.history.runs() if r["kind"] == "定期(検証)"][0]
        self.assertEqual(dry_event["targets"], [LISTED[1], LISTED[2], LISTED[3]])          # 並べ替え後(連続した製番は、枠があっても最後)
        self.assertEqual(dry_event["deferred"], [{"product": LISTED[0], "streak": 2}])
        self.assertTrue(any("2 回連続" in line and LISTED[0] in line for line in dry.last_plan.deferred_lines()))
        self.settings(scheduled_products=LISTED[:4], scheduled_max_products=3)
        real = self.manager()
        real.run_scheduled_targeted()
        self.assertEqual(self.calls[-1]["products"], dry_event["targets"])                # dry-run と実行時の対象が一致
        self.assertEqual(real.last_plan.deferred, dry.last_plan.deferred)

    def test_a_product_completed_by_a_concurrent_run_is_dropped_after_the_lock_and_does_not_waste_a_slot(self):
        self.settings(scheduled_products=LISTED[:4], scheduled_max_products=2)
        mgr = self.manager()
        real_acquire = mgr.lock.acquire

        def acquire_then_other_process_finishes(tag=""):
            real_acquire(tag)
            self.done.add(LISTED[0])                                             # ロック取得の直前〜直後に、並行処理が先頭の製番を完了した
        mgr.lock.acquire = acquire_then_other_process_finishes
        self.assertEqual(mgr.run_scheduled_targeted(), COMPLETED)
        self.assertEqual(self.calls[0]["products"], [LISTED[1], LISTED[2]])       # 枠(2 件)は、次の製番で埋まる
        self.assertEqual(self.history.runs()[0]["targets"], [LISTED[1], LISTED[2]])

    def test_if_everything_became_done_after_the_lock_nothing_runs(self):
        self.settings(scheduled_products=LISTED[:2], scheduled_max_products=2)
        mgr = self.manager()
        real_acquire = mgr.lock.acquire

        def acquire_then_all_done(tag=""):
            real_acquire(tag)
            self.done.update(LISTED[:2])
        mgr.lock.acquire = acquire_then_all_done
        self.assertEqual(mgr.run_scheduled_targeted(), SKIPPED)
        self.assertEqual(self.calls, [])
        self.assertFalse(mgr.status()["running"])

    def test_the_dry_run_also_replans_after_taking_its_lock(self):
        self.settings(scheduled_products=LISTED[:3], scheduled_max_products=2)
        mgr = self.manager()
        real_acquire = mgr.lock.acquire

        def acquire_then_done(tag=""):
            real_acquire(tag)
            self.done.add(LISTED[0])
        mgr.lock.acquire = acquire_then_done
        mgr.run_scheduled_targeted_dry_run()
        self.assertEqual(self.history.runs()[0]["targets"], [LISTED[1], LISTED[2]])


class StreakDefinitionTest(Base):
    """連続失敗回数の定義(docs/ops_console.md): 失敗・スキップは数える / 完了でリセット / 未開始・実行全体の中断・結果が無い実行は、数えずリセットもしない。"""

    P = "A1TEST0001"

    def history_of(self, sequence):
        """sequence は古い順の結果。(status, 追加項目) または None(その製番の結果が無い実行)。"""
        for item in sequence:
            rid = self.history.start("定期", self.P, targets=[self.P])
            self.history.finish(rid, "完了", "", results=({self.P: {"status": item[0], "reason": item[1].get("reason", ""), **{k: v for k, v in item[1].items() if k != "reason"}}} if item else {"A1OTHER0001": {"status": "完了"}}))
        return failure_streaks(self.history.runs(limit=None), [self.P])[self.P]

    FAIL = ("失敗", {})
    SKIP = ("スキップ", {"reason": "処理対象外"})
    DONE = ("完了", {})
    NOT_STARTED = ("未開始", {"reason": "実行時間の上限に達したため開始しませんでした"})
    RUN_ABORT = ("未開始", {"reason": "API", "scope": "run"})
    RUN_FAIL = ("失敗", {"reason": "実行全体の失敗", "scope": "run"})
    SKIP_DONE = ("スキップ", {"reason": "処理済み"})
    SKIP_TIME = ("スキップ", {"reason": "実行時間の上限に達したため開始しませんでした"})

    def test_failures_and_skips_are_counted(self):
        self.assertEqual(self.history_of([self.FAIL]), 1)
        self.assertEqual(self.history_of([self.SKIP, self.FAIL]), 3)              # 履歴は積み上がる(同じ Base の履歴に続けて書く)

    def test_the_definition_table(self):
        cases = [
            ([self.FAIL, self.FAIL], 2),
            ([self.SKIP, self.SKIP], 2),
            ([self.FAIL, self.SKIP], 2),
            ([self.FAIL, self.FAIL, self.DONE], 0),                                # 完了でリセット
            ([self.FAIL, self.DONE, self.FAIL], 1),                                # リセット後の数え直し
            ([self.FAIL, self.NOT_STARTED, self.FAIL], 2),                         # 未開始は数えず、リセットもしない
            ([self.FAIL, self.RUN_ABORT, self.FAIL], 2),                           # 実行全体の中断も同じ
            ([self.FAIL, self.RUN_FAIL, self.FAIL], 2),
            ([self.FAIL, None, self.FAIL], 2),                                     # その製番の結果が無い実行も同じ
            ([self.FAIL, self.SKIP_DONE, self.SKIP_TIME, self.FAIL], 2),           # 処理済み・時間上限のスキップは数えない
            ([self.NOT_STARTED, self.RUN_ABORT, self.RUN_FAIL, None], 0),          # 結果が無いものだけなら 0
            ([self.FAIL, self.FAIL, self.RUN_ABORT], 2),                           # 直近が中断でも、連続は維持される
        ]
        for sequence, expected in cases:
            with self.subTest(sequence=[c[0] if c else None for c in sequence]):
                for f in self.state.glob("ops_history.local.jsonl"):
                    f.unlink()                                                      # ケースごとに履歴を作り直す
                self.assertEqual(self.history_of(sequence), expected)

    def test_a_run_only_counts_scheduled_runs_with_results(self):
        rid = self.history.start("手動", self.P)
        self.history.finish(rid, "失敗", "x")                                       # 手動実行・結果(results)の無い実行は数えない
        rid = self.history.start("定期(検証)", "(検証のみ)", targets=[self.P])
        self.history.finish(rid, "完了", "")
        self.assertEqual(failure_streaks(self.history.runs(limit=None), [self.P])[self.P], 0)

    def test_two_consecutive_failures_defer_the_product_and_the_definition_keeps_it_there_across_aborts(self):
        self.settings(scheduled_products=[self.P, "A1TEST0002"], scheduled_max_products=1)
        fail = lambda **kw: {"completed": [], "failed": {kw["products"][0]: "RuntimeError: x"}, "skipped": {}}
        api = lambda **kw: {"completed": [], "failed": {p: "API" for p in kw["products"]}, "skipped": {}, "aborted": "API"}
        for main in (fail, api, fail):
            self.manager(main).run_scheduled_targeted()
        m = self.manager()
        m.run_scheduled_targeted_dry_run()
        self.assertEqual(m.last_plan.deferred, ((self.P, 2),))                       # 2 回連続(間の中断は飛ばす)→ 後順位
        self.assertEqual(m.last_plan.targets, ("A1TEST0002",))


class AppAbortedTest(MainIntegrationBase):
    """app.main が、API の事前確認の失敗を、対象が 1 件でも「実行全体の中断」として戻り値で明示する。"""
    REASON = "推論 API を利用できないため、製番処理を開始しませんでした(接続が拒否されました)"

    def test_the_abort_is_explicit_for_one_product_and_for_the_normal_run(self):
        for products in ([PRODUCT], [PRODUCT, "A1BL9999"], None):
            self.preflight_reason = self.REASON
            self.run_main(products=products)
            self.assertEqual(self.results["aborted"], self.REASON, products)
            self.assertTrue(all(v == self.REASON for v in self.results["failed"].values()), products)   # 既存の failed の形も従来どおり
            self.assertEqual(self.results["completed"], [], products)
            self.assertEqual(self.print_script_calls, [], products)

    def test_a_normal_run_has_no_aborted_key(self):
        self.preflight_reason = None
        self.run_main(products=[PRODUCT])
        self.assertNotIn("aborted", self.results)


class NormalizationTest(Base):
    def test_products_are_normalized_to_upper_case_and_deduplicated_after_normalization(self):
        saved = self.settings(scheduled_products=["a1test0002", "A1TEST0001", "A1test0002", " a1TEST0001 "], scheduled_retry_products=["a1test0003", "A1TEST0003"])
        self.assertEqual(saved.scheduled_products, ("A1TEST0002", "A1TEST0001"))      # 最初の出現の順
        self.assertEqual(saved.scheduled_retry_products, ("A1TEST0003",))
        self.assertEqual(self.store.load().scheduled_products, ("A1TEST0002", "A1TEST0001"))   # 保存・再読込でも大文字のまま

    def test_the_processed_targets_and_history_use_upper_case(self):
        self.settings(scheduled_products=["a1test0001", "a1test0002"], scheduled_max_products=2)
        self.manager().run_scheduled_targeted()
        self.assertEqual(self.calls[0]["products"], ["A1TEST0001", "A1TEST0002"])
        self.assertEqual(self.history.runs()[0]["targets"], ["A1TEST0001", "A1TEST0002"])

    def test_the_done_check_uses_the_upper_case_name(self):
        self.done = {"A1TEST0001"}
        self.settings(scheduled_products=["a1test0001", "a1test0002"], scheduled_max_products=2)
        self.manager().run_scheduled_targeted()
        self.assertEqual(self.calls[0]["products"], ["A1TEST0002"])


class RealWiringTest(Base):
    """build_context の実際の配線(実 OutputGuard・実 CodeVersionGuard・実の処理済み判定)で、定期実行が本番と同じ検査を通ることを確認する。"""

    def setUp(self):
        super().setUp()
        self.data_output = self.tmp / "share" / "estimatic"
        self.prod_dir = self.data_output / "a_product" / "output"
        self.prod_dir.mkdir(parents=True)
        self.wrong_root = self.data_output / "a_product" / "out"           # 運用設定が誤って指していた場所(本番の出力先とは別)
        self.wrong_root.mkdir(parents=True)
        self.cfg = self.tmp / "app.local.yaml"
        self.cfg.write_text(yaml.safe_dump({"data": {"output": str(self.data_output), "storage": "a_product"}}), encoding="utf-8")
        import importlib
        self.oc = importlib.import_module("ops_console")

    def ctx(self):
        return self.oc.build_context(state_dir=self.state, config_path=str(self.cfg), main_func=self.main, scheduler=mock.Mock())

    def test_the_known_misconfiguration_is_refused_before_any_side_effect(self):
        from ops import output_guard as og
        self.settings(output_root=str(self.wrong_root))                       # 実環境で見つかった誤り(…_product\out)と同じ形
        before = (tree(self.state), sorted(p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*")))
        ctx = self.ctx()
        self.assertEqual(ctx.runs.run_scheduled_targeted(), FAILED)
        self.assertEqual(ctx.runs.refusal, og.REASON_SETTING)
        self.assertEqual(self.calls, [])
        self.assertEqual(ctx.history.runs(), [])
        after = (tree(self.state), sorted(p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*")))
        self.assertEqual(after, before)                                       # 履歴・ロック・フォルダ(出力先・状態)を何も作らない

    def test_matching_or_blank_output_root_runs_and_uses_the_production_dir_for_the_done_check(self):
        (self.prod_dir / LISTED[0]).mkdir()                                   # 本番の出力先にある処理済み製番
        for root in ("", str(self.data_output)):
            self.calls.clear()
            self.settings(output_root=root, scheduled_products=LISTED[:3])
            ctx = self.ctx()
            self.assertEqual(ctx.runs.run_scheduled_targeted(), COMPLETED, root)
            self.assertEqual(self.calls[0]["products"], LISTED[1:3], root)    # 処理済みは、本番の出力先を見て除外される
            if root:
                self.assertEqual(self.calls[0]["output_root"], str(self.data_output))
            else:
                self.assertNotIn("output_root", self.calls[0])                # 空なら設定どおり(app が data.output を使う)

    def test_a_commit_change_after_startup_refuses_the_scheduled_run(self):
        self.settings(scheduled_products=LISTED[:1])
        ctx = self.ctx()
        ctx.runs.code_guard._current = lambda: "f" * 40                       # 起動後にコミットが変わった状態
        self.assertEqual(ctx.runs.run_scheduled_targeted(), FAILED)
        self.assertIn("更新", ctx.runs.refusal)
        self.assertEqual((self.calls, ctx.history.runs()), ([], []))


class TaskTargetStateTest(unittest.TestCase):
    XML = ('<Task><Actions><Exec><Command>{py}</Command><Arguments>"{script}" run-scheduled</Arguments></Exec></Actions></Task>')

    def sched(self, xml=None, rc=0):
        calls = []

        def runner(args):
            calls.append(args)
            return SimpleNamespace(returncode=rc, stdout=(xml or "").encode("utf-8"), stderr=b"")
        ts = scheduler_mod.TaskScheduler(r"C:\env\python.exe", r"C:\repo\new\ops_console.py", runner=runner)
        return ts, calls

    def test_states_are_read_only(self):
        ts, calls = self.sched(self.XML.format(py=r"C:\env\python.exe", script=r"C:\repo\new\ops_console.py"))
        self.assertEqual(ts.action_target_state(), "match")
        ts, _ = self.sched(self.XML.format(py=r"C:\env\python.exe", script=r"C:\repo\OLD\ops_console.py"))
        self.assertEqual(ts.action_target_state(), "mismatch")
        ts, _ = self.sched(self.XML.format(py=r"C:\other\python.exe", script=r"C:\repo\new\ops_console.py"))
        self.assertEqual(ts.action_target_state(), "mismatch")
        ts, _ = self.sched("", rc=1)
        self.assertEqual(ts.action_target_state(), "unregistered")
        ts, _ = self.sched("<Task></Task>")
        self.assertEqual(ts.action_target_state(), "unknown")

    def test_only_a_query_is_ever_issued(self):
        ts, calls = self.sched(self.XML.format(py=r"C:\env\python.exe", script=r"C:\repo\OLD\ops_console.py"))
        ts.action_target_state()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["schtasks", "/Query"])                 # 作成・変更・有効化・削除はしない


if __name__ == "__main__":
    unittest.main()
