"""本番推論 API の起動・停止の制御(prod_ctl.py)のテスト。実プロセス・実 API・共有・psutil は使わない(依存を差し替える)。"""
import json
import os
import socket
import types
import unittest
from pathlib import Path

from prod_helpers import HOST, ProdBase  # noqa: F401
import prod_ctl
import prod_config

HERE = Path(prod_ctl.__file__).resolve().parent


class FakeProc:
    def __init__(self, ps, pid, cmd, create_time=1000.0):
        self.ps, self.pid, self.cmd, self.ct = ps, pid, cmd, create_time
        self.killed = False

    def create_time(self):
        return self.ct

    def cmdline(self):
        return self.cmd

    def children(self, recursive=False):
        return [self.ps.procs[c] for c in self.ps.kids.get(self.pid, []) if c in self.ps.procs]

    def kill(self):
        self.killed = True
        self.ps.kill_log.append(self.pid)
        self.ps.procs.pop(self.pid, None)


class FakePsutil:
    def __init__(self, host):
        self.procs, self.kids, self.kill_log, self.listeners, self.host = {}, {}, [], [], host

    def Process(self, pid):  # noqa: N802
        if pid not in self.procs:
            raise ProcessLookupError(pid)
        return self.procs[pid]

    def pid_exists(self, pid):
        return pid in self.procs

    def net_connections(self, kind="tcp"):
        return [types.SimpleNamespace(status="LISTEN", laddr=types.SimpleNamespace(ip=ip, port=port), pid=pid) for ip, port, pid in self.listeners]

    def net_if_addrs(self):
        return {"eth0": [types.SimpleNamespace(family=socket.AF_INET, address=self.host)]}

    def add(self, pid, cmd, create_time=1000.0):
        self.procs[pid] = FakeProc(self, pid, cmd, create_time)
        return self.procs[pid]


class CtlBase(ProdBase):
    def setUp(self):
        super().setUp()
        self.cfg_obj = self.cfg()
        self.cfg_path = self.root / "prod_server.local.yaml"
        import yaml
        self.cfg_path.write_text(yaml.safe_dump(self.data(), allow_unicode=True), encoding="utf-8")
        self.run_dir = self.root / "run"
        self.ps = FakePsutil(HOST)
        self.lines, self.spawned, self.breaks, self.now = [], [], [], [0.0]
        self.spawn_extra = []
        self.health_script = []              # 呼ばれるたびに先頭から返す(最後の値は繰り返す)
        self.proc_state = {"rc": None}

    def health_ok(self):
        m = self.cfg_obj.model
        return 200, {"status": "ok", "ready": True, "mode": "production",
                     "model": {"id": m.id, "weights_sha256_12": m.weights_sha256[:12], "yaml_sha256_12": m.yaml_sha256[:12], "weights_size": m.weights_size}}

    def make(self, **kw):
        def http_get(url, timeout=3.0):
            self.assertTrue(url.startswith(f"http://{HOST}:{self.port}/health"), url)
            item = self.health_script[0] if len(self.health_script) == 1 else self.health_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def spawn(cfg_path, extra=()):
            pid = 4000 + len(self.spawned)
            self.spawn_extra.append(list(extra))
            proc = self.ps.add(pid, ["python", str(HERE / "prod_server.py"), "serve", "--config", str(cfg_path), *extra], create_time=2000.0 + len(self.spawned))
            self.spawned.append(pid)
            self.ps.add(pid + 500, ["python", "real"], 2000.5)                       # venv ランチャーの子(実体のインタープリター)
            self.ps.kids[pid] = [pid + 500]
            self.ps.listeners.append((HOST, self.port, pid + 500))
            return types.SimpleNamespace(pid=pid, poll=lambda: self.proc_state["rc"])

        def send_break(pid):
            self.breaks.append(pid)
            if not kw.get("ignore_break"):
                for c in [pid] + self.ps.kids.get(pid, []):
                    self.ps.procs.pop(c, None)
                self.ps.listeners = [x for x in self.ps.listeners if x[2] not in [pid] + self.ps.kids.get(pid, [])]

        def sleep(sec):
            self.now[0] += sec
        c = prod_ctl.Controller(self.cfg_path, run_dir=self.run_dir, psutil_mod=self.ps, http_get=http_get, spawn=spawn, send_break=send_break,
                                sleep=sleep, clock=lambda: self.now[0], out=self.lines.append)
        return c

    def text(self):
        return "\n".join(self.lines)


class StartTest(CtlBase):
    def test_start_spawns_once_waits_for_health_and_verifies_listen_and_model(self):
        self.health_script = [(503, {"status": "loading"}), (503, {"status": "loading"}), self.health_ok()]
        c = self.make()
        self.assertEqual(c.start(), prod_ctl.EXIT_OK)
        self.assertEqual(len(self.spawned), 1)
        rec = json.loads((self.run_dir / "prod_api.pid").read_text(encoding="utf-8"))
        self.assertEqual(rec["pid"], self.spawned[0])
        self.assertIn(f"http://{HOST}:{self.port}", self.text())
        self.assertIn("設定と一致", self.text())
        self.assertFalse((self.run_dir / "prod_api.start.lock").exists())              # ロックは解放される
        self.assertTrue((self.run_dir / "last_start.txt").is_file())

    def test_publish_is_off_by_default_and_only_passed_when_explicit(self):
        self.spawn_extra = []
        self.health_script = [self.health_ok()]
        self.assertEqual(self.make().start(), prod_ctl.EXIT_OK)
        self.assertEqual(self.spawn_extra, [[]])                                        # 既定: 公開しない(スタートアップのバッチと同じ)
        self.assertNotIn("--publish-config", " ".join(self.ps.procs[self.spawned[0]].cmd))

    def test_explicit_publish_and_replace_stale_are_passed_to_serve(self):
        self.spawn_extra = []
        self.health_script = [self.health_ok()]
        self.assertEqual(self.make().start(publish_config=True, replace_stale=True), prod_ctl.EXIT_OK)
        self.assertEqual(self.spawn_extra, [["--publish-config", "--replace-stale"]])
        self.assertIn("接続設定の公開を明示して起動します", self.text())

    def test_publish_is_not_applied_to_an_already_running_api(self):
        self.spawn_extra = []
        self.health_script = [self.health_ok()]
        self.assertEqual(self.make().start(), prod_ctl.EXIT_OK)
        self.assertEqual(self.make().start(publish_config=True), prod_ctl.EXIT_OK)
        self.assertEqual(self.spawn_extra, [[]])                                        # 2 回目は何も起動しない
        self.assertIn("公開(--publish-config)は行いません", self.text())

    def test_command_line_rules_for_the_publish_options(self):
        for argv in (["start", "--replace-stale"], ["stop", "--publish-config"], ["status", "--replace-stale", "--publish-config"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as cm:
                    prod_ctl.main(argv)
                self.assertEqual(cm.exception.code, 2)

    def test_second_start_does_not_start_a_second_process(self):
        self.health_script = [self.health_ok()]
        c = self.make()
        self.assertEqual(c.start(), prod_ctl.EXIT_OK)
        before = len(self.spawned)
        self.assertEqual(self.make().start(), prod_ctl.EXIT_OK)                        # 2 回目(スタートアップ + 手動の重複など)
        self.assertEqual(len(self.spawned), before)
        self.assertIn("すでに起動しています", self.text())

    def test_a_concurrent_start_in_progress_is_refused(self):
        self.run_dir.mkdir()
        (self.run_dir / "prod_api.start.lock").write_text("777", encoding="utf-8")
        self.ps.add(777, ["python", "prod_ctl.py", "start"])                            # 別の起動処理が生きている
        self.health_script = [self.health_ok()]
        self.assertEqual(self.make().start(), prod_ctl.EXIT_BUSY_START)
        self.assertEqual(self.spawned, [])
        self.assertIn("二重起動はしません", self.text())

    def test_a_stale_lock_and_stale_pid_record_are_cleaned_up(self):
        self.run_dir.mkdir()
        (self.run_dir / "prod_api.start.lock").write_text("888", encoding="utf-8")      # 持ち主はもういない
        (self.run_dir / "prod_api.pid").write_text(json.dumps({"pid": 999, "create_time": 1.0}), encoding="utf-8")
        self.health_script = [self.health_ok()]
        self.assertEqual(self.make().start(), prod_ctl.EXIT_OK)
        self.assertEqual(len(self.spawned), 1)

    def test_a_recycled_pid_is_not_mistaken_for_our_process(self):
        self.run_dir.mkdir()
        (self.run_dir / "prod_api.pid").write_text(json.dumps({"pid": 321, "create_time": 1000.0}), encoding="utf-8")
        self.ps.add(321, ["notepad.exe"], 1000.0)                                       # 同じ PID の別のプロセス(コマンドラインが違う)
        self.health_script = [self.health_ok()]
        self.assertEqual(self.make().start(), prod_ctl.EXIT_OK)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.ps.kill_log, [])                                          # 別のプロセスには触れない

    def test_port_used_by_another_process_is_refused_without_touching_it(self):
        self.ps.add(555, ["other.exe"])
        self.ps.listeners.append(("0.0.0.0", self.port, 555))
        self.health_script = [self.health_ok()]
        self.assertEqual(self.make().start(), prod_ctl.EXIT_PORT)
        self.assertEqual((self.spawned, self.ps.kill_log, self.breaks), ([], [], []))
        self.assertIn("別のプロセスは停止していません", self.text())

    def test_address_not_assigned_to_this_pc_is_refused(self):
        self.ps.host = "10.255.255.1"
        self.assertEqual(self.make().start(), prod_ctl.EXIT_ADDRESS)
        self.assertEqual(self.spawned, [])

    def test_invalid_config_is_refused_with_a_fixed_message(self):
        self.cfg_path.write_text("mode: production\nbind:\n  host:\n  port:\n", encoding="utf-8")
        self.assertEqual(self.make().start(), prod_ctl.EXIT_CONFIG)
        self.assertEqual(self.spawned, [])
        self.assertIn("check", self.text())

    def test_process_exit_before_ready_reports_a_fixed_reason_and_cleans_up(self):
        self.health_script = [(None, {})]
        self.proc_state["rc"] = 4
        c = self.make()
        self.assertEqual(c.start(), prod_ctl.EXIT_EXITED)
        self.assertIn("モデルの検証に失敗", self.text())
        self.assertFalse((self.run_dir / "prod_api.pid").exists())

    def test_health_timeout_stops_the_started_process_only(self):
        self.health_script = [(503, {"status": "loading"})]
        c = self.make()
        self.assertEqual(c.start(), prod_ctl.EXIT_TIMEOUT)
        self.assertEqual(self.breaks, [self.spawned[0]])
        self.assertNotIn(self.spawned[0], self.ps.procs)

    def test_model_mismatch_in_health_stops_the_process(self):
        status, body = self.health_ok()
        body["model"]["weights_sha256_12"] = "0" * 12
        self.health_script = [(status, body)]
        self.assertEqual(self.make().start(), prod_ctl.EXIT_MISMATCH)
        self.assertEqual(self.breaks, [self.spawned[0]])
        self.assertIn("モデル識別", self.text())

    def test_listening_by_another_process_is_a_mismatch(self):
        self.health_script = [self.health_ok()]
        c = self.make()

        def spawn_other(cfg_path):
            pid = 4100
            self.ps.add(pid, ["python", str(HERE / "prod_server.py"), "serve"], 3000.0)
            self.ps.add(777, ["other"])
            self.ps.listeners.append((HOST, self.port, 777))                            # 待受は別のプロセス
            return types.SimpleNamespace(pid=pid, poll=lambda: None)
        c._spawn = spawn_other
        self.assertEqual(c.start(), prod_ctl.EXIT_MISMATCH)
        self.assertNotIn(777, self.ps.kill_log)

    def test_messages_do_not_leak_paths(self):
        self.health_script = [self.health_ok()]
        self.make().start()
        for secret in (str(self.root), str(self.out), str(self.model_dir)):
            self.assertNotIn(secret, self.text())


class StopTest(CtlBase):
    def started(self):
        self.health_script = [self.health_ok()]
        c = self.make()
        self.assertEqual(c.start(), prod_ctl.EXIT_OK)
        self.lines.clear()
        return c

    def test_stop_sends_ctrl_break_to_the_recorded_pid_and_waits(self):
        c = self.started()
        pid = self.spawned[0]
        self.assertEqual(c.stop(), prod_ctl.EXIT_OK)
        self.assertEqual(self.breaks, [pid])
        self.assertEqual(self.ps.kill_log, [])                                           # 正常停止で終われば、強制終了しない
        self.assertFalse((self.run_dir / "prod_api.pid").exists())
        self.assertIn("停止しました", self.text())
        self.assertIn("server.yaml は変更していません", self.text())

    def test_stop_force_kills_only_that_pid_tree_after_the_grace_period(self):
        c = self.make(ignore_break=True)
        self.health_script = [self.health_ok()]
        self.assertEqual(c.start(), prod_ctl.EXIT_OK)
        pid = self.spawned[0]
        self.ps.add(9999, ["bystander.exe"])
        before = self.now[0]
        self.assertEqual(c.stop(), prod_ctl.EXIT_OK)
        self.assertGreaterEqual(self.now[0] - before, self.cfg_obj.limits.shutdown_grace_sec)    # 猶予まで待ってから
        self.assertEqual(sorted(self.ps.kill_log), sorted([pid, pid + 500]))                      # その PID と子だけ
        self.assertIn(9999, self.ps.procs)                                                        # 無関係なプロセスは残る

    def test_stop_does_nothing_when_the_recorded_pid_is_another_process(self):
        self.run_dir.mkdir()
        (self.run_dir / "prod_api.pid").write_text(json.dumps({"pid": 321, "create_time": 1000.0}), encoding="utf-8")
        self.ps.add(321, ["python", "something_else.py"], 1000.0)
        self.assertEqual(self.make().stop(), prod_ctl.EXIT_OK)
        self.assertEqual((self.breaks, self.ps.kill_log), ([], []))
        self.assertIn("別のプロセスのため、何も停止しませんでした", self.text())
        self.assertIn(321, self.ps.procs)

    def test_stop_without_a_record_does_nothing(self):
        self.assertEqual(self.make().stop(), prod_ctl.EXIT_OK)
        self.assertEqual((self.breaks, self.ps.kill_log), ([], []))
        self.assertIn("停止対象はありません", self.text())

    def test_status(self):
        c = self.started()
        self.assertEqual(c.status(), prod_ctl.EXIT_OK)
        self.assertIn(f"http://{HOST}:{self.port}", self.text())
        self.assertIn("起動中", self.text())


class BatchFilesTest(unittest.TestCase):
    def read(self, name):
        return (HERE / name).read_text(encoding="utf-8")

    def test_batches_use_the_controller_and_never_kill_by_name(self):
        start, stop = self.read("start_prod_api.bat"), self.read("stop_prod_api.bat")
        self.assertIn("prod_ctl.py start", start)
        self.assertIn("prod_ctl.py stop", stop)
        for text in (start, stop):
            low = text.lower()
            for forbidden in ("taskkill", "/im ", "stop-process", "--publish-config", "sc create", "nssm", "schtasks"):
                self.assertNotIn(forbidden, low)
            self.assertIn(".venv\\scripts\\python.exe", low)
            self.assertIn("chcp 65001", low)
        for name in ("start_prod_api.bat", "stop_prod_api.bat"):
            raw = (HERE / name).read_bytes()
            self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"), name)                      # 改行は CRLF だけ(cmd.exe 用)
            self.assertTrue(all(b < 128 for b in raw), name)                                  # ASCII だけ(cmd.exe は OEM コードページで読むため、日本語を入れない)

    def test_the_controller_never_publishes_the_shared_server_yaml(self):
        src = (HERE / "prod_ctl.py").read_text(encoding="utf-8")
        self.assertIn("publish_config: bool = False", src)                                  # 公開は、明示した指定のときだけ(既定は False)
        self.assertNotIn("publish_config=True", src)
        self.assertNotIn("taskkill", src.lower())


if __name__ == "__main__":
    unittest.main()
