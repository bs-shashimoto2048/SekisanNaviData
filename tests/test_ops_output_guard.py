# 運用画面(8765)の手動実行の出力先の整合確認(ops/output_guard.py)のテスト。
# 一時フォルダ・合成データだけ。本番出力先・共有・DB・実 API・印刷・製番処理は使わない(app.main は差し替える)。
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "inference_server"))

from ops import output_guard as og  # noqa: E402
from ops.history import History  # noqa: E402
from ops.runner import RunManager  # noqa: E402
from ops.settings_store import SettingsError, SettingsStore  # noqa: E402

PRODUCT = "A1TEST0001"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_output = self.tmp / "share" / "estimatic"                   # 設定の data.output(共有の代わり)
        self.prod_dir = self.data_output / "a_product" / "output"             # 本番の出力先 = 製番フォルダの親 = API の output_root
        (self.prod_dir / PRODUCT).mkdir(parents=True)
        (self.data_output / "a_product" / "out").mkdir(parents=True)          # 運用設定側の別の場所(別の領域)
        self.other_root = self.data_output / "a_product" / "out"
        self.cfg = self.tmp / "app.local.yaml"
        self.cfg.write_text(yaml.safe_dump({"data": {"output": str(self.data_output), "storage": "a_product"}}), encoding="utf-8")
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.calls = []
        self.guard = og.OutputGuard(str(self.cfg))
        self.store = SettingsStore(self.state)
        self.history = History(self.state)

    def fake_main(self, **kw):
        self.calls.append(kw)
        return {"completed": list(kw.get("products") or []), "failed": {}, "skipped": {}}

    def manager(self, guard="default", main=None):
        guard = self.guard if guard == "default" else guard
        return RunManager(self.state, self.store, self.history, main or self.fake_main, config_path=str(self.cfg),
                          com_init=lambda: None, com_uninit=lambda: None, output_guard=guard)

    def tree(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if "state" not in p.parts)

    def save_ops_root(self, root):
        self.store.save({"output_root": str(root), "schedule_enabled": False, "schedule_time": "08:00", "schedule_interval_days": 2})


class EffectivePathTest(Base):
    def test_effective_output_dir_follows_the_apps_rule(self):
        self.assertEqual(self.guard.production_output_dir(), os.path.join(str(self.data_output), "a_product", "output"))
        self.assertEqual(self.guard.effective_output_dir(str(self.data_output)), self.guard.production_output_dir())
        # 別の出力ルートを渡すと、その下の storage/output になる(app.main(output_root=…) と同じ規則)
        self.assertEqual(self.guard.effective_output_dir(str(self.other_root)), os.path.join(str(self.other_root), "a_product", "output"))
        self.assertNotEqual(os.path.normcase(self.guard.effective_output_dir(str(self.other_root))), os.path.normcase(self.guard.production_output_dir()))

    def test_the_production_work_dir_is_accepted_by_the_api_and_the_other_one_is_not(self):
        import prod_config
        import prod_server
        sys.path.insert(0, str(ROOT / "inference_server" / "tests"))
        from prod_helpers import make_png
        make_png(self.prod_dir / PRODUCT / "21.png")
        api_root = prod_config.validate_output_root(self.guard.production_output_dir())
        work_dir = os.path.join(self.guard.effective_output_dir(str(self.data_output)), PRODUCT)       # 手動実行(出力先 = data.output)で app が作る work_dir
        real_dir, page, ext, image, given = prod_server.validate_prod_request(work_dir, "21", "png", api_root)
        self.assertEqual((page, ext), ("21", "png"))
        # 別の出力先(運用設定の `…/out`)から算出される work_dir は、API が受け付けない
        wrong = os.path.join(self.guard.effective_output_dir(str(self.other_root)), PRODUCT)
        os.makedirs(wrong, exist_ok=True)
        make_png(Path(wrong) / "21.png")
        with self.assertRaises(prod_server.RequestRejected):
            prod_server.validate_prod_request(wrong, "21", "png", api_root)

    def test_separator_case_and_trailing_separator_do_not_matter(self):
        for root in (str(self.data_output).replace("\\", "/"), str(self.data_output).upper() if os.name == "nt" else str(self.data_output),
                     str(self.data_output) + os.sep):
            with self.subTest(root=root):
                self.guard(root, og.SOURCE_FIELD)


class ManualRunGuardTest(Base):
    def test_matching_output_root_is_run_unchanged(self):
        rid, t = self.manager().start_manual(PRODUCT, str(self.data_output))
        t.join(10)
        self.assertEqual(self.calls, [{"config_path": str(self.cfg), "products": [PRODUCT], "output_root": str(self.data_output)}])
        self.assertEqual(self.history.runs()[0]["status"], "完了")

    def test_different_field_value_is_refused_before_any_side_effect(self):
        before = self.tree()
        mgr = self.manager(main=mock.Mock(side_effect=AssertionError("app.main は呼ばない")))
        with self.assertRaises(SettingsError) as cm:
            mgr.start_manual(PRODUCT, str(self.other_root))
        self.assertEqual(str(cm.exception), og.REASON_FIELD)
        self.assertEqual(self.history.runs(), [])                              # 履歴(実行中の記録)も作らない
        self.assertFalse(mgr.status()["running"])                              # ロックも取らない
        self.assertEqual(self.tree(), before)                                  # 製番フォルダ・出力フォルダを作らない
        mgr.lock.acquire("probe")                                              # ロックが残っていない(取得できる)
        mgr.lock.release()
        for leak in (str(self.tmp), "share", PRODUCT):
            self.assertNotIn(leak, str(cm.exception))                          # 理由は固定文(パスを含めない)

    def test_ops_setting_fallback_that_differs_is_refused_not_silently_used(self):
        self.save_ops_root(self.other_root)                                    # 画面の欄は空 → 運用設定の出力先が使われる経路
        mgr = self.manager(main=mock.Mock(side_effect=AssertionError("app.main は呼ばない")))
        before = self.tree()
        with self.assertRaises(SettingsError) as cm:
            mgr.start_manual(PRODUCT)
        self.assertEqual(str(cm.exception), og.REASON_SETTING)
        self.assertEqual(self.history.runs(), [])
        self.assertEqual(self.tree(), before)
        # 欄に正しい出力先を明示すれば、運用設定が違っていても、その指定どおり(本番の出力先)で実行できる
        _, t = self.manager().start_manual(PRODUCT, str(self.data_output))
        t.join(10)
        self.assertEqual(self.calls[-1]["output_root"], str(self.data_output))

    def test_ops_setting_equal_to_data_output_is_used_as_before(self):
        self.save_ops_root(self.data_output)
        _, t = self.manager().start_manual(PRODUCT)
        t.join(10)
        self.assertEqual(self.calls, [{"config_path": str(self.cfg), "products": [PRODUCT], "output_root": str(self.data_output)}])

    def test_no_output_root_anywhere_keeps_the_old_behaviour_and_does_not_read_the_config(self):
        loader = mock.Mock(side_effect=AssertionError("設定は読まない"))
        _, t = self.manager(guard=og.OutputGuard(str(self.cfg), load=loader)).start_manual(PRODUCT)
        t.join(10)
        self.assertEqual(self.calls, [{"config_path": str(self.cfg), "products": [PRODUCT]}])        # output_root を渡さない(従来どおり data.output)
        loader.assert_not_called()

    def test_unreadable_config_is_refused_with_a_fixed_reason(self):
        guard = og.OutputGuard(str(self.tmp / "missing.yaml"))
        with self.assertRaises(SettingsError) as cm:
            self.manager(guard=guard, main=mock.Mock(side_effect=AssertionError("呼ばない"))).start_manual(PRODUCT, str(self.data_output))
        self.assertEqual(str(cm.exception), og.REASON_UNREADABLE)
        self.assertNotIn("missing", str(cm.exception))
        self.assertEqual(self.history.runs(), [])

    def test_invalid_input_is_still_rejected_by_the_existing_validation_first(self):
        mgr = self.manager(guard=mock.Mock(side_effect=AssertionError("検証前に呼ばない")))
        with self.assertRaises(SettingsError):
            mgr.start_manual("bad product", str(self.data_output))
        with self.assertRaises(SettingsError):
            mgr.start_manual(PRODUCT, "relative")

    def test_without_a_guard_behaviour_is_exactly_as_before(self):                       # 暫定運用(interim)などは、ガードを付けない
        mgr = self.manager(guard=None)
        _, t = mgr.start_manual(PRODUCT, str(self.other_root))
        t.join(10)
        self.assertEqual(self.calls[-1]["output_root"], str(self.other_root))

    def test_scheduled_run_is_not_changed(self):
        self.save_ops_root(self.other_root)                                                  # 定期実行は、従来どおり運用設定の出力先を使う
        self.assertEqual(self.manager().run_scheduled(), "完了")
        self.assertEqual(self.calls, [{"config_path": str(self.cfg), "output_root": str(self.other_root)}])


class WiringTest(Base):
    def test_ops_console_wires_the_guard_for_manual_runs_only(self):
        import importlib
        oc = importlib.import_module("ops_console")
        ctx = oc.build_context(state_dir=self.state, config_path=str(self.cfg), main_func=self.fake_main, scheduler=mock.Mock(name="scheduler"))
        self.assertIsInstance(ctx.runs.output_guard, og.OutputGuard)
        with self.assertRaises(SettingsError):
            ctx.runs.start_manual(PRODUCT, str(self.other_root))
        self.assertEqual(self.calls, [])
        ctx.runs.start_manual(PRODUCT, str(self.data_output))[1].join(10)
        self.assertEqual(len(self.calls), 1)

    def test_api_run_endpoint_returns_the_fixed_reason_with_400_and_starts_nothing(self):
        import importlib
        from ops.server import make_server
        oc = importlib.import_module("ops_console")
        ctx = oc.build_context(state_dir=self.state, config_path=str(self.cfg), main_func=self.fake_main, scheduler=mock.Mock(name="scheduler"))
        server = make_server(ctx, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        url = f"http://127.0.0.1:{server.server_address[1]}/api/run"

        def post(body):
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "X-Ops-Request": "1"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(req, timeout=10) as r:
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode("utf-8"))
        status, body = post({"product": PRODUCT, "output_root": str(self.other_root)})
        self.assertEqual((status, body["ok"], body["error"]), (400, False, og.REASON_FIELD))
        self.assertEqual((self.calls, ctx.history.runs()), ([], []))
        status, body = post({"product": PRODUCT, "output_root": str(self.data_output)})            # 一致する指定は、従来どおり開始される
        self.assertEqual((status, body["ok"]), (200, True))


if __name__ == "__main__":
    unittest.main()
