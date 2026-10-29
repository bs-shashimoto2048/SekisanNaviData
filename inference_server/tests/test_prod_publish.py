"""本番推論 API: 共有 server.yaml の公開(既定で書かない・稼働中は拒否・古い設定は明示で退避して置換・原子的)。"""
import datetime
import os
import unittest
from unittest import mock

import requests
import yaml

from prod_helpers import HOST, ProdBase  # noqa: F401
import prod_publish

MODEL_PUB = {"id": "test_model_20260101", "weights_sha256_12": "a" * 12, "yaml_sha256_12": "b" * 12, "weights_size": 1}
INST = "c" * 32


def live_other(data, our_id, timeout):
    return "live-other"


def live_self(data, our_id, timeout):
    return "live-self"


def dead(data, our_id, timeout):
    return "dead"


class PublishTest(ProdBase):
    def existing_text(self):
        return yaml.safe_dump({
            "host": {"ip": "0.0.0.0", "port": 4321, "pc_ip": "192.0.2.77", "extra": "keep-me"},
            "proxy": {"host": "legacy-proxy.example", "port": 8080},
            "directory": {"output": "//legacy.example/share/a_product"},
            "owner_note": "unknown-key-keep-me"}, sort_keys=False)

    def test_absent_creates_new_file_with_only_the_expected_keys(self):
        cfg = self.cfg()
        plan = prod_publish.plan_publish(cfg, INST, MODEL_PUB)
        self.assertEqual((plan.existing, plan.action), ("absent", "create"))
        self.assertFalse(self.server_yaml.exists())                     # 計画だけでは書かない
        r = prod_publish.publish(cfg, INST, MODEL_PUB)
        self.assertEqual(r["action"], "create")
        d = yaml.safe_load(self.server_yaml.read_text(encoding="utf-8"))
        self.assertEqual(set(d), {"host", "proxy", "mode", "instance_id", "model"})
        self.assertEqual(d["host"], {"ip": HOST, "port": self.port, "pc_ip": HOST})
        self.assertEqual((d["mode"], d["instance_id"]), ("production", INST))
        self.assertEqual(d["proxy"], {"host": "proxy.example", "port": 80})   # 既存が無いので設定の proxy
        self.assertEqual(list(self.pub_dir.glob("*.tmp")), [])                # 一時ファイルを残さない

    def test_live_other_instance_is_always_refused_even_with_replace_stale(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        before = self.server_yaml.read_bytes()
        for flag in (False, True):
            with self.subTest(flag=flag):
                with self.assertRaises(prod_publish.PublishRefused) as cm:
                    prod_publish.publish(self.cfg(), INST, MODEL_PUB, replace_stale=flag, probe=live_other)
                self.assertEqual(cm.exception.code, "live_other_instance")
        self.assertEqual(self.server_yaml.read_bytes(), before)                # 変更なし
        self.assertFalse(self.backup_dir.exists())

    def test_stale_existing_requires_explicit_flag(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        before = self.server_yaml.read_bytes()
        with self.assertRaises(prod_publish.PublishRefused) as cm:
            prod_publish.publish(self.cfg(), INST, MODEL_PUB, replace_stale=False, probe=dead)
        self.assertEqual(cm.exception.code, "stale_needs_replace_flag")
        self.assertEqual(self.server_yaml.read_bytes(), before)

    def test_stale_replace_backs_up_first_preserves_proxy_and_directory(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        before = self.server_yaml.read_bytes()
        fixed = datetime.datetime(2026, 10, 30, 1, 2, 3)
        r = prod_publish.publish(self.cfg(), INST, MODEL_PUB, replace_stale=True, probe=dead, now=lambda: fixed)
        self.assertEqual(r["backup"], f"server.yaml.bak-20261030T010203Z-{INST[:8]}")
        self.assertEqual((self.backup_dir / r["backup"]).read_bytes(), before)  # 退避は元の内容と完全一致
        d = yaml.safe_load(self.server_yaml.read_text(encoding="utf-8"))
        self.assertEqual(d["proxy"], {"host": "legacy-proxy.example", "port": 8080})   # 既存の proxy を引き継ぐ(設定の proxy ではない)
        self.assertEqual(d["directory"], {"output": "//legacy.example/share/a_product"})
        self.assertEqual(d["host"]["port"], self.port)                                  # 変わるのは接続先と識別情報
        self.assertEqual(d["owner_note"], "unknown-key-keep-me")                        # 既存の未知キーは保持する(消さない)
        self.assertEqual(d["host"]["extra"], "keep-me")
        self.assertEqual(list(self.pub_dir.glob("*.tmp")), [])

    def test_backup_name_collision_is_refused_without_overwriting(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        fixed = datetime.datetime(2026, 10, 30, 1, 2, 3)
        self.backup_dir.mkdir()
        (self.backup_dir / f"server.yaml.bak-20261030T010203Z-{INST[:8]}").write_text("keep", encoding="utf-8")
        before = self.server_yaml.read_bytes()
        with self.assertRaises(prod_publish.PublishRefused) as cm:
            prod_publish.publish(self.cfg(), INST, MODEL_PUB, replace_stale=True, probe=dead, now=lambda: fixed)
        self.assertEqual(cm.exception.code, "backup_exists")
        self.assertEqual(self.server_yaml.read_bytes(), before)

    def test_unreadable_existing_is_refused(self):
        self.server_yaml.write_bytes(b"\xff\xfe\x00not yaml: [")
        with self.assertRaises(prod_publish.PublishRefused) as cm:
            prod_publish.publish(self.cfg(), INST, MODEL_PUB, replace_stale=True, probe=dead)
        self.assertEqual(cm.exception.code, "existing_unreadable")

    def test_live_self_is_replaced_in_place(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        plan = prod_publish.plan_publish(self.cfg(), INST, MODEL_PUB, probe=live_self)
        self.assertEqual(plan.action, "replace")

    def test_plan_shows_key_status_only_never_values(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        plan = prod_publish.plan_publish(self.cfg(), INST, MODEL_PUB, replace_stale=True, probe=dead).public()
        text = yaml.safe_dump(plan)
        for secret in ("192.0.2.77", "legacy-proxy.example", "legacy.example", str(self.port), HOST, "proxy.example"):
            self.assertNotIn(secret, text)
        status = {c["key"]: c["status"] for c in plan["changes"]}
        self.assertEqual(status["host.port"], "changed")
        self.assertEqual(status["proxy.host"], "unchanged")
        self.assertEqual(status["mode"], "added")

    def test_unknown_probe_result_is_refused_even_with_replace_stale(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        before = self.server_yaml.read_bytes()
        with self.assertRaises(prod_publish.PublishRefused) as cm:
            prod_publish.publish(self.cfg(), INST, MODEL_PUB, replace_stale=True, probe=lambda *a: "unknown")
        self.assertEqual(cm.exception.code, "status_unknown")
        self.assertEqual(self.server_yaml.read_bytes(), before)

    def test_read_timeout_is_not_treated_as_dead(self):
        data = {"host": {"pc_ip": "192.0.2.1", "port": 1}}
        for exc in (requests.exceptions.ReadTimeout("slow"), requests.exceptions.ConnectTimeout("drop"),
                    requests.exceptions.ConnectionError("unreachable")):
            with self.subTest(exc=type(exc).__name__):
                def boom(*a, _e=exc, **k):
                    raise _e
                self.assertEqual(prod_publish.probe_existing(data, INST, 1, get=boom), "unknown")

    def test_only_an_explicit_connection_refusal_is_dead(self):
        data = {"host": {"pc_ip": "192.0.2.1", "port": 1}}

        def refused(*a, **k):
            raise requests.exceptions.ConnectionError(ConnectionRefusedError("refused"))
        self.assertEqual(prod_publish.probe_existing(data, INST, 1, get=refused), "dead")

    def test_unparseable_or_non_ip_targets_are_unknown_and_never_requested(self):
        for data in ({}, {"host": {}}, {"host": {"pc_ip": "example.invalid", "port": 1}}, {"host": {"pc_ip": "192.0.2.1", "port": "80"}},
                     {"host": {"pc_ip": "192.0.2.1", "port": 0}}, {"host": {"pc_ip": "192.0.2.1@evil", "port": 80}}, {"host": {"pc_ip": "::1", "port": 80}}):
            with self.subTest(data=data):
                self.assertEqual(prod_publish.probe_existing(data, INST, 1, get=lambda *a, **k: self.fail("requested")), "unknown")

    def test_a_temporarily_unreadable_share_is_not_mistaken_for_absent(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        real_open = open

        def deny(path, *a, **k):
            if str(path) == str(self.server_yaml):
                raise PermissionError("share hiccup")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", deny):
            self.assertEqual(prod_publish.read_existing_bytes(str(self.server_yaml)), ("unreadable", None))
            plan = prod_publish.plan_publish(self.cfg(), INST, MODEL_PUB, replace_stale=True, probe=dead)
        self.assertEqual((plan.existing, plan.action, plan.refuse_code), ("unreadable", "refuse", "existing_unreadable"))
        self.assertEqual(prod_publish.read_existing_bytes(str(self.pub_dir / "nothing.yaml")), ("absent", None))

    def test_content_changed_between_plan_and_replace_is_refused(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        racing = {"n": 0}

        def probe_then_edit(data, our, timeout):
            racing["n"] += 1
            if racing["n"] == 1:                                  # 計画の後(バックアップより前)に、他者が書き換える
                self.server_yaml.write_text(self.existing_text() + "intruder: 1\n", encoding="utf-8")
            return "dead"
        with self.assertRaises(prod_publish.PublishRefused) as cm:
            prod_publish.publish(self.cfg(), INST, MODEL_PUB, replace_stale=True, probe=probe_then_edit)
        self.assertEqual(cm.exception.code, "changed_during_publish")
        self.assertIn("intruder", self.server_yaml.read_text(encoding="utf-8"))   # 他者の書き込みを上書きしていない
        self.assertFalse((self.pub_dir / "server.yaml.lock").exists())

    def test_target_becoming_live_just_before_replace_is_refused(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        answers = iter(["dead", "live-other"])
        before = self.server_yaml.read_bytes()
        with self.assertRaises(prod_publish.PublishRefused) as cm:
            prod_publish.publish(self.cfg(), INST, MODEL_PUB, replace_stale=True, probe=lambda *a: next(answers))
        self.assertEqual(cm.exception.code, "live_other_instance")
        self.assertEqual(self.server_yaml.read_bytes(), before)

    def test_concurrent_publish_is_refused_by_the_lock_and_lock_is_released(self):
        lock = self.pub_dir / "server.yaml.lock"
        lock.write_text("", encoding="utf-8")
        with self.assertRaises(prod_publish.PublishRefused) as cm:
            prod_publish.publish(self.cfg(), INST, MODEL_PUB)
        self.assertEqual(cm.exception.code, "publish_in_progress")
        self.assertTrue(lock.exists())                                              # 他者のロックは消さない
        lock.unlink()
        prod_publish.publish(self.cfg(), INST, MODEL_PUB)
        self.assertFalse(lock.exists())                                             # 成功後に自分のロックを解除する

    def test_replace_failure_leaves_no_temp_file_no_lock_and_maps_to_a_fixed_code(self):
        self.server_yaml.write_text(self.existing_text(), encoding="utf-8")
        before = self.server_yaml.read_bytes()
        with mock.patch.object(prod_publish.os, "replace", side_effect=PermissionError("locked by reader")), \
                mock.patch.object(prod_publish.time, "sleep"):
            with self.assertRaises(prod_publish.PublishRefused) as cm:
                prod_publish.publish(self.cfg(), INST, MODEL_PUB, replace_stale=True, probe=dead)
        self.assertEqual(cm.exception.code, "write_failed")
        self.assertEqual(self.server_yaml.read_bytes(), before)
        self.assertEqual(list(self.pub_dir.glob("*.tmp")), [])
        self.assertFalse((self.pub_dir / "server.yaml.lock").exists())

    def test_transient_permission_error_on_replace_is_retried(self):
        real = os.replace
        calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("reader has it open")
            return real(a, b)
        with mock.patch.object(prod_publish.os, "replace", flaky), mock.patch.object(prod_publish.time, "sleep"):
            prod_publish.publish(self.cfg(), INST, MODEL_PUB)
        self.assertEqual(calls["n"], 3)
        self.assertTrue(self.server_yaml.exists())

    def test_probe_existing_classifies_without_returning_values(self):
        class R:
            def __init__(self, body):
                self._b = body

            def json(self):
                if self._b is None:
                    raise ValueError("no json")
                return self._b
        data = {"host": {"pc_ip": "192.0.2.1", "port": 1}}
        self.assertEqual(prod_publish.probe_existing(data, INST, 1, get=lambda *a, **k: R({"instance_id": INST})), "live-self")
        self.assertEqual(prod_publish.probe_existing(data, INST, 1, get=lambda *a, **k: R({"instance_id": "x"})), "live-other")
        self.assertEqual(prod_publish.probe_existing(data, INST, 1, get=lambda *a, **k: R(None)), "live-other")   # 応答があれば稼働中

        def boom(*a, **k):
            raise requests.exceptions.ConnectionError(ConnectionRefusedError("refused"))
        self.assertEqual(prod_publish.probe_existing(data, INST, 1, get=boom), "dead")
        self.assertEqual(prod_publish.probe_existing({}, INST, 1, get=boom), "unknown")     # 接続先を読めない = 判定不能

    def test_unpublish_only_removes_our_own_file(self):
        prod_publish.publish(self.cfg(), INST, MODEL_PUB)
        self.assertFalse(prod_publish.unpublish_if_ours(self.cfg(), "d" * 32))
        self.assertTrue(self.server_yaml.exists())
        self.assertTrue(prod_publish.unpublish_if_ours(self.cfg(), INST))
        self.assertFalse(self.server_yaml.exists())


if __name__ == "__main__":
    unittest.main()
