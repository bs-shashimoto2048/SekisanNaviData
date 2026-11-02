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
from ops.scheduled_plan import REASON_DONE, REASON_NOT_CONFIGURED, REASON_OVER_LIMIT, build_plan  # noqa: E402
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
        self.assertIn("今回の対象: " + ", ".join(LISTED[:2]), out)
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
