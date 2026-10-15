# 暫定 API 操作用 UI(interim/・interim_console.py)のテスト。すべてモック(実 API・実プロセス・モデル・ネットワークは使わない)。
# 一時フォルダの偽のレイアウト・偽のプロセス・偽の /health だけを使う。HTTP サーバーのテストだけ、この PC の loopback の一時ポートを使う。
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import interim_console  # noqa: E402
from interim import manager as mg  # noqa: E402
from interim import server as srv  # noqa: E402

IID_RE = r"^[0-9a-f]{32}$"
SMOKE_OK = "\n".join(["[1/5] 安全条件: OK(隔離出力先・リンクなし・loopback・プロキシなし)", "[2/5] 事前確認(/health): OK(製番処理の事前確認と同じ判定)",
                      "[3/5] /health の本文: OK(ready・内部情報なし)", "[4/5] 推論の依頼(合成画像 1 枚): OK",
                      "[5/5] 検出結果ファイル: OK(検出 0 件。0 件は正常)", "RESULT: OK"])


class FakeProc:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None

    def poll(self):
        return self.returncode


class Env:
    """偽の OS・通信。API の状態(health)・プロセスの生死・呼び出しの記録を、テストが自由に操作する。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.model = tmp / "model"
        self.model.mkdir()
        (self.model / "best.pt").write_bytes(b"x")
        (self.model / "model.yaml").write_text("x", encoding="utf-8")
        self.py = tmp / "venv" / "python.exe"
        self.py.parent.mkdir()
        self.py.write_bytes(b"x")
        self.settings = tmp / "interim_console.local.yaml"
        self.settings.write_text(f"model_dir: '{self.model}'\npython: '{self.py}'\n", encoding="utf-8")
        self.alive = {}                       # pid -> 作成時刻
        self.spawned, self.shutdowns, self.kills = [], [], []
        self.health_fn = lambda port: (None, None)
        self.shutdown_stops_process = True
        self.now = 0.0
        self.next_pid = 4000
        self.port = 45555
        self.smoke_calls = 0

    # --- 注入する部品 ---
    def spawn(self, args, **kw):
        self.next_pid += 1
        self.spawned.append((list(args), kw))
        self.alive[self.next_pid] = 1000 + self.next_pid
        return FakeProc(self.next_pid)

    def start_time(self, pid):
        return self.alive.get(pid)

    def kill(self, pid):
        self.kills.append(pid)
        self.alive.pop(pid, None)

    def health(self, port):
        return self.health_fn(port)

    def shutdown(self, port, iid):
        self.shutdowns.append((port, iid))
        if self.shutdown_stops_process:
            for pid in list(self.alive):
                self.alive.pop(pid)
        return 200, {"ok": True}

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def manager(self, **kw):
        base = dict(settings_path=self.settings, spawn=self.spawn, health=self.health, shutdown=self.shutdown,
                    start_time=self.start_time, kill=self.kill, free_port=lambda: self.port, clock=self.clock, sleep=self.sleep,
                    smoke_runner=lambda repo: (0, SMOKE_OK), startup_timeout=240.0, stop_wait=5.0, background=False)
        base.update(kw)
        return mg.ApiManager(self.tmp, **base)

    # --- 状態の操作 ---
    def iid(self, m):
        return m.rec["instance_id"]

    def serve_as(self, m, code, mine=True):
        iid = self.iid(m) if mine else "f" * 32
        self.health_fn = lambda port: (code, {"instance_id": iid, "status": "x", "ready": code == 200})

    def write_yaml(self, iid, port=None):
        d = self.tmp / "output_interim" / "api"
        d.mkdir(parents=True, exist_ok=True)
        (d / "server.yaml").write_text(f"host:\n  pc_ip: 127.0.0.1\n  port: {port or self.port}\ninstance_id: {iid}\nbackend: sahi\n", encoding="utf-8")

    def yaml_exists(self):
        return (self.tmp / "output_interim" / "api" / "server.yaml").exists()


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="sekisan_interim_")
        self.addCleanup(td.cleanup)
        self.env = Env(Path(td.name).resolve())
        self.m = self.env.manager()

    def state(self):
        return self.m.snapshot()["api"]["state"]


class StartTest(Base):
    def test_start_launches_the_launcher_with_sahi_and_our_own_instance_id(self):
        snap = self.m.start()
        self.assertEqual(snap["api"]["state"], "starting")
        (args, kw), = self.env.spawned
        self.assertEqual(Path(args[args.index("-File") + 1]), self.env.tmp / "tools" / "run_interim_api.ps1")
        self.assertEqual(args[args.index("-Backend") + 1], "sahi")                           # 既定どおり実モデル(fake は選ばない)
        self.assertNotIn("fake", " ".join(args).lower().replace("fake_", "").replace("-fakescript", ""))
        self.assertRegex(args[args.index("-InstanceId") + 1], IID_RE)
        self.assertEqual(args[args.index("-Port") + 1], str(self.env.port))
        self.assertTrue(mg.PORT_RANGE[0] <= self.env.port <= mg.PORT_RANGE[1])
        self.assertEqual(kw["cwd"], str(self.env.tmp))
        for forbidden in ("-ReplaceStale", "-DryRun"):
            self.assertNotIn(forbidden, args)

    def test_response_never_contains_paths_args_or_ids(self):
        snap = self.m.start()
        text = json.dumps(snap, ensure_ascii=False)
        for leaked in (str(self.env.model), str(self.env.py), str(self.env.tmp), self.env.iid(self.m), "-ModelDir", "-PythonExe", "-InstanceId",
                       "best.pt", "powershell"):
            self.assertNotIn(leaked, text)
        self.assertEqual(set(snap["api"]) - {"state", "label", "port", "http", "managed", "external", "stale", "reason", "busy", "settings_ok"}, set())

    def test_state_transitions_starting_loading_ready(self):
        self.m.start()
        self.assertEqual(self.state(), "starting")                                           # まだ待ち受けていない(接続待ち)
        self.env.serve_as(self.m, 503)
        self.assertEqual(self.state(), "loading")
        self.env.serve_as(self.m, 200)
        snap = self.m.snapshot()
        self.assertEqual((snap["api"]["state"], snap["api"]["label"], snap["api"]["http"]), ("ready", "準備完了", 200))
        self.assertTrue(snap["can"]["smoke"] and snap["can"]["stop"] and not snap["can"]["start"])

    def test_a_slow_health_response_does_not_flip_the_state_back_to_starting(self):
        self.m.start()
        self.env.serve_as(self.m, 503)
        self.assertEqual(self.state(), "loading")
        self.env.health_fn = lambda port: (None, None)                                         # 読み込みの負荷で、一時的に応答がない
        snap = self.m.snapshot()
        self.assertEqual(snap["api"]["state"], "loading")                                      # 「起動中」へ戻さない
        self.assertFalse(snap["can"]["smoke"])                                                 # 応答が確認できない間は、スモークを許可しない
        self.env.serve_as(self.m, 200)
        self.assertEqual(self.state(), "ready")
        self.env.health_fn = lambda port: (None, None)
        self.assertEqual(self.state(), "ready")

    def test_double_start_is_refused_in_every_active_state(self):
        self.m.start()
        for code in (None, 503, 200):
            if code:
                self.env.serve_as(self.m, code)
            with self.assertRaises(mg.Busy) as cm:
                self.m.start()
            self.assertEqual(cm.exception.code, "already")
        self.assertEqual(len(self.env.spawned), 1)

    def test_concurrent_starts_launch_exactly_once(self):
        results = []
        barrier = threading.Barrier(6)

        def go():
            barrier.wait()
            try:
                self.m.start()
                results.append("ok")
            except mg.Refused as e:
                results.append(e.code)
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(len(self.env.spawned), 1)

    def test_start_is_refused_without_usable_local_settings(self):
        for content in ("", "model_dir: relative\npython: x.exe\n", f"model_dir: '{self.env.model}'\npython: '{self.env.tmp / 'missing.exe'}'\n",
                        "model_dir: '\\\\\\\\host\\\\share'\npython: 'C:\\\\python.exe'\n"):
            self.env.settings.write_text(content, encoding="utf-8")
            with self.assertRaises(mg.Refused) as cm:
                self.m.start()
            self.assertEqual(cm.exception.code, "settings")
        self.assertEqual(self.env.spawned, [])
        self.assertFalse(self.m.snapshot()["api"]["settings_ok"])

    def test_no_free_port_refuses_before_spawning(self):
        m = self.env.manager(free_port=lambda: None)
        with self.assertRaises(mg.Refused) as cm:
            m.start()
        self.assertEqual(cm.exception.code, "no_port")
        self.assertEqual(self.env.spawned, [])

    def test_spawn_failure_is_a_fixed_failure_and_creates_nothing(self):
        def boom(*a, **k):
            raise OSError(r"C:\secret\path password=pw")
        m = self.env.manager(spawn=boom)
        with self.assertRaises(mg.Refused) as cm:
            m.start()
        self.assertEqual(cm.exception.code, "spawn_failed")
        snap = m.snapshot()
        self.assertEqual(snap["api"]["state"], "failed")
        self.assertNotIn("secret", json.dumps(snap, ensure_ascii=False))
        self.assertTrue(snap["can"]["start"])                                                 # 失敗後は、もう一度起動できる
        self.assertFalse((self.env.tmp / "output_interim" / ".interim_console_state.json").exists())

    def test_unverifiable_launcher_process_is_terminated_instead_of_left_unmanaged(self):
        self.env.start_time = lambda pid: None                                                 # 起動直後に作成時刻を確認できない
        m = self.env.manager(start_time=lambda pid: None)
        with self.assertRaises(mg.Refused) as cm:
            m.start()
        self.assertEqual(cm.exception.code, "spawn_failed")
        pid = self.env.next_pid
        self.assertEqual(self.env.kills, [pid])                                                # 自分が今起動したプロセスだけを終了
        self.assertIsNone(m.rec)
        self.assertFalse((self.env.tmp / "output_interim" / ".interim_console_state.json").exists())
        self.assertEqual(self.env.shutdowns, [])
        self.assertTrue(m.snapshot()["can"]["start"])

    def test_free_port_helpers(self):
        self.assertEqual(mg.pick_free_port(lambda p: True) in range(*[mg.PORT_RANGE[0], mg.PORT_RANGE[1] + 1]), True)
        self.assertIsNone(mg.pick_free_port(lambda p: False))
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            busy = s.getsockname()[1]
            s.listen(1)
            self.assertFalse(mg.port_is_free(busy))
        self.assertTrue(mg.port_is_free(busy))


class ExternalAndOwnerTest(Base):
    def test_external_api_is_shown_and_never_started_over_or_stopped(self):
        self.env.write_yaml("e" * 32)
        self.env.health_fn = lambda port: (200, {"instance_id": "e" * 32, "ready": True})
        snap = self.m.snapshot()
        self.assertEqual((snap["api"]["state"], snap["api"]["label"], snap["api"]["external"]), ("external", "外部起動中", True))
        self.assertFalse(snap["can"]["start"] or snap["can"]["stop"])
        self.assertTrue(snap["can"]["smoke"])                                                  # 準備完了なら、スモークだけは実行できる
        with self.assertRaises(mg.Refused) as cm:
            self.m.start()
        self.assertEqual(cm.exception.code, "external")
        with self.assertRaises(mg.Refused) as cm:
            self.m.stop()
        self.assertEqual(cm.exception.code, "external")
        self.assertEqual((self.env.spawned, self.env.shutdowns, self.env.kills), ([], [], []))
        self.assertTrue(self.env.yaml_exists())                                                # 既存の server.yaml は変更しない

    def test_stale_foreign_server_yaml_blocks_start_and_is_left_alone(self):
        self.env.write_yaml("d" * 32)                                                          # 応答なし = 古い残り(外部のもの)
        snap = self.m.snapshot()
        self.assertEqual(snap["api"]["state"], "idle")
        self.assertTrue(snap["api"]["stale"] and not snap["can"]["start"])
        with self.assertRaises(mg.Refused) as cm:
            self.m.start()
        self.assertEqual(cm.exception.code, "stale")
        self.assertTrue(self.env.yaml_exists())
        self.assertEqual((self.env.spawned, self.env.kills), ([], []))

    def test_instance_id_mismatch_means_not_ours_and_is_never_stopped(self):
        self.m.start()
        self.env.serve_as(self.m, 200, mine=False)                                             # 応答はあるが instance_id が違う
        self.assertEqual(self.state(), "external")
        self.assertIsNone(self.m.rec)                                                          # 自分の記録は破棄する(止める対象にしない)
        with self.assertRaises(mg.Refused):
            self.m.stop()
        self.assertEqual((self.env.shutdowns, self.env.kills), ([], []))

    def test_pid_reuse_is_detected_by_creation_time_and_nothing_is_killed(self):
        self.m.start()
        pid = self.m.rec["pid"]
        self.env.alive[pid] = 999999                                                           # 同じ PID で、作成時刻が違う別のプロセス
        self.assertEqual(self.state(), "failed")
        self.assertEqual((self.env.shutdowns, self.env.kills), ([], []))
        with self.assertRaises(mg.Refused):
            self.m.stop()
        self.assertEqual((self.env.shutdowns, self.env.kills), ([], []))

    def test_owner_mismatch_on_stop_refuses_without_signalling(self):
        self.m.start()
        self.env.serve_as(self.m, 200)
        calls = {"n": 0}
        mine = self.env.health_fn

        def flip(port):                                                                         # _evaluate では自分のもの、stop の確認では別の instance_id
            calls["n"] += 1
            return mine(port) if calls["n"] == 1 else (200, {"instance_id": "c" * 32, "ready": True})
        self.env.health_fn = flip
        with self.assertRaises(mg.Refused) as cm:
            self.m.stop()
        self.assertEqual(cm.exception.code, "owner_mismatch")
        self.assertEqual((self.env.shutdowns, self.env.kills), ([], []))
        self.assertIsNone(self.m.op)


class StopTest(Base):
    def test_stop_uses_the_instance_id_shuts_down_gracefully_and_cleans_only_our_files(self):
        self.m.start()
        iid = self.env.iid(self.m)
        self.env.write_yaml(iid)
        self.env.serve_as(self.m, 200)
        snap = self.m.stop()
        self.assertEqual(self.env.shutdowns, [(self.env.port, iid)])
        self.assertEqual(self.env.kills, [])                                                    # 正常停止できたので強制終了しない
        self.assertEqual(snap["api"]["state"], "idle")
        self.assertFalse(self.env.yaml_exists())
        self.assertFalse((self.env.tmp / "output_interim" / ".interim_console_state.json").exists())
        self.assertTrue(snap["can"]["start"])                                                   # 停止後は、もう一度起動できる

    def test_graceful_shutdown_ignored_falls_back_to_killing_only_our_verified_launcher(self):
        self.m.start()
        pid = self.m.rec["pid"]
        self.env.serve_as(self.m, 200)
        self.env.shutdown_stops_process = False
        snap = self.m.stop()
        self.assertEqual(self.env.kills, [pid])
        self.assertEqual(snap["api"]["state"], "idle")

    def test_stop_while_starting_terminates_only_our_launcher(self):
        self.m.start()
        pid = self.m.rec["pid"]
        self.m.stop()
        self.assertEqual((self.env.shutdowns, self.env.kills), ([], [pid]))
        self.assertEqual(self.state(), "idle")

    def test_stop_that_cannot_stop_reports_failure_without_losing_track(self):
        self.m.start()

        def stubborn(pid):
            self.env.kills.append(pid)                                                           # 終了しない
        m = self.m
        m._kill = stubborn
        with self.assertRaises(mg.Refused) as cm:
            m.stop()
        self.assertEqual(cm.exception.code, "stop_failed")
        self.assertIsNotNone(m.rec)
        self.assertIsNone(m.op)

    def test_stop_without_managed_api_is_refused(self):
        with self.assertRaises(mg.Refused) as cm:
            self.m.stop()
        self.assertEqual(cm.exception.code, "not_managed")
        self.assertEqual((self.env.shutdowns, self.env.kills), ([], []))


class FailureTest(Base):
    def test_launcher_refusal_is_a_failure_and_cleans_only_our_yaml(self):
        self.m.start()
        pid = self.m.rec["pid"]
        proc = self.m.rec["proc"]
        proc.returncode = 2
        self.env.alive.pop(pid)
        self.env.write_yaml(self.env.iid(self.m))                                              # 自分の(instance_id が一致する)残り
        snap = self.m.snapshot()
        self.assertEqual((snap["api"]["state"], snap["api"]["label"]), ("failed", "失敗"))
        self.assertEqual(snap["api"]["reason"], mg.MESSAGES["launcher_refused"])
        self.assertFalse(self.env.yaml_exists())
        self.assertTrue(snap["can"]["start"] and not snap["can"]["stop"])
        self.assertEqual(self.env.kills, [])

    def test_failure_never_removes_a_foreign_server_yaml(self):
        self.m.start()
        self.env.alive.pop(self.m.rec["pid"])
        self.env.write_yaml("b" * 32)                                                          # 他者のもの
        self.m.snapshot()
        self.assertTrue(self.env.yaml_exists())

    def test_startup_timeout_stops_only_our_process(self):
        self.m.start()
        pid = self.m.rec["pid"]
        self.env.write_yaml(self.env.iid(self.m))
        self.env.now += 241
        snap = self.m.snapshot()
        self.assertEqual(snap["api"]["state"], "failed")
        self.assertEqual(snap["api"]["reason"], mg.MESSAGES["startup_timeout"])
        self.assertEqual(self.env.kills, [pid])
        self.assertFalse(self.env.yaml_exists())

    def test_api_prepare_failure_keeps_it_stoppable_but_not_restartable(self):
        self.m.start()
        self.env.serve_as(self.m, 500)
        snap = self.m.snapshot()
        self.assertEqual(snap["api"]["state"], "failed")
        self.assertEqual(snap["api"]["reason"], mg.MESSAGES["api_prepare_failed"])
        self.assertTrue(snap["can"]["stop"] and not snap["can"]["start"])
        with self.assertRaises(mg.Busy):
            self.m.start()

    def test_restart_after_failure_clears_the_failure(self):
        self.m.start()
        self.env.alive.pop(self.m.rec["pid"])
        self.assertEqual(self.state(), "failed")
        self.m.start()
        self.assertIsNone(self.m.failure)
        self.assertEqual(len(self.env.spawned), 2)


class AdoptionTest(Base):
    def test_ui_restart_keeps_managing_the_api_it_started(self):
        self.m.start()
        iid = self.env.iid(self.m)
        m2 = self.env.manager()                                                                  # UI の再起動(同じ状態ファイル)
        self.env.health_fn = lambda port: (200, {"instance_id": iid, "ready": True})
        snap = m2.snapshot()
        self.assertEqual((snap["api"]["state"], snap["api"]["managed"]), ("ready", True))
        m2.stop()
        self.assertEqual(self.env.shutdowns, [(self.env.port, iid)])

    def test_saved_record_with_a_different_creation_time_is_discarded_without_touching_anything(self):
        self.m.start()
        self.env.alive[self.m.rec["pid"]] = 1                                                    # PID の使い回し
        m2 = self.env.manager()
        self.assertEqual(m2.snapshot()["api"]["state"], "idle")
        self.assertFalse((self.env.tmp / "output_interim" / ".interim_console_state.json").exists())
        self.assertEqual((self.env.shutdowns, self.env.kills), ([], []))

    def test_broken_state_file_is_ignored(self):
        p = self.env.tmp / "output_interim" / ".interim_console_state.json"
        p.parent.mkdir(parents=True)
        for content in ("{", json.dumps({"instance_id": "x", "pid": 1, "ctime": 1, "port": 1}), json.dumps({"instance_id": "a" * 32, "pid": "1", "ctime": 1, "port": 45000})):
            p.write_text(content, encoding="utf-8")
            m = self.env.manager()
            self.assertEqual(m.snapshot()["api"]["state"], "idle")
            self.assertIsNone(m.rec)


class SmokeTest(Base):
    def ready(self):
        self.m.start()
        self.env.serve_as(self.m, 200)

    def test_smoke_runs_only_when_ready_and_reports_steps_and_count_only(self):
        with self.assertRaises(mg.Refused) as cm:
            self.m.smoke()
        self.assertEqual(cm.exception.code, "not_ready")
        self.ready()
        snap = self.m.smoke()
        self.assertEqual(snap["smoke"], {"state": "success", "ok_steps": [1, 2, 3, 4, 5], "detections": 0, "refused": False, "exit": 0})

    def test_smoke_output_is_never_retained_or_shown(self):
        leaky = SMOKE_OK + "\nTraceback C:\\secret\\x password=pw 10.9.8.7 Index$0|Device$secret|Score$0.9|Coordinates$1,2"
        m = self.env.manager(smoke_runner=lambda repo: (0, leaky))
        m.start()
        self.env.serve_as(m, 200)
        text = json.dumps(m.smoke(), ensure_ascii=False)
        for leaked in ("secret", "pw", "10.9.8.7", "Device", "Coordinates", "Traceback"):
            self.assertNotIn(leaked, text)

    def test_smoke_failure_variants(self):
        cases = {"ng": (1, SMOKE_OK.split("\n")[0] + "\nNG: x\nRESULT: NG"), "refused": (2, "REFUSED: x"), "partial_ok_but_exit1": (1, SMOKE_OK)}
        for name, (code, out) in cases.items():
            m = self.env.manager(smoke_runner=lambda repo, c=code, o=out: (c, o))
            self.env.alive.clear()
            self.env.health_fn = lambda port: (None, None)
            m.start()
            self.env.serve_as(m, 200)
            s = m.smoke()["smoke"]
            self.assertEqual(s["state"], "failed", name)
            self.assertIsNone(s["detections"], name)
            self.assertEqual(s["refused"], code == 2, name)
            m.stop()

    def test_smoke_runner_exception_is_a_failure_without_message(self):
        def boom(repo):
            raise RuntimeError(r"C:\secret password=pw")
        m = self.env.manager(smoke_runner=boom)
        m.start()
        self.env.serve_as(m, 200)
        snap = m.smoke()
        self.assertEqual(snap["smoke"]["state"], "failed")
        self.assertNotIn("secret", json.dumps(snap))

    def test_double_smoke_is_refused_while_running_and_buttons_are_disabled(self):
        gate, started = threading.Event(), threading.Event()

        def slow(repo):
            started.set()
            gate.wait(10)
            return 0, SMOKE_OK
        m = self.env.manager(smoke_runner=slow, background=True)
        m.start()
        self.env.serve_as(m, 200)
        snap = m.smoke()
        self.assertTrue(started.wait(5))
        self.assertEqual(snap["smoke"]["state"], "running")
        with self.assertRaises(mg.Busy) as cm:
            m.smoke()
        self.assertEqual(cm.exception.code, "smoke_running")
        s = m.snapshot()
        self.assertEqual((s["can"]["smoke"], s["can"]["start"], s["can"]["stop"]), (False, False, False))     # 実行中は該当ボタンを無効化
        with self.assertRaises(mg.Refused):
            m.stop()                                                                                            # スモーク中は停止もしない(状態が不明になるため)
        gate.set()
        m._smoke_thread.join(10)
        self.assertEqual(m.snapshot()["smoke"]["state"], "success")
        self.assertEqual(self.env.shutdowns, [])

    def test_parse_smoke_handles_only_the_fixed_format(self):
        self.assertEqual(mg.parse_smoke(0, SMOKE_OK)["detections"], 0)
        self.assertEqual(mg.parse_smoke(0, SMOKE_OK.replace("検出 0 件", "検出 7 件"))["detections"], 7)
        self.assertEqual(mg.parse_smoke(0, "")["state"], "failed")
        self.assertEqual(mg.parse_smoke(1, SMOKE_OK)["state"], "failed")


class LocalSettingsAndStaticTest(unittest.TestCase):
    def test_local_settings_and_runtime_files_are_git_ignored(self):
        ignore = [ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        self.assertIn("*.local.yaml", ignore)                       # interim_console.local.yaml(モデル・venv の場所)
        self.assertIn("output_interim/", ignore)                    # 状態ファイル・ログ・server.yaml
        self.assertEqual(mg.ApiManager(ROOT).settings_path.name, "interim_console.local.yaml")

    def test_sources_do_not_touch_forbidden_targets_or_non_loopback(self):
        for name in ("manager.py", "server.py"):
            lines = (ROOT / "interim" / name).read_text(encoding="utf-8").splitlines()
            src = chr(10).join(ln.split("  # ")[0] for ln in lines if not ln.lstrip().startswith("#"))      # コメントは除く(禁止事項の説明が入るため)
            forbidden_all = ("schtasks", "host_mod", "design_mod", "app.main", "ops_history", "requests.", "pipeline.")
            for forbidden in forbidden_all + (("ops.",) if name == "manager.py" else ()):         # API 管理部は ops を使わない(server.py は例外型だけ)
                self.assertNotIn(forbidden, src, (name, forbidden))
            for ln in src.splitlines():
                if "http://" in ln:
                    self.assertIn("127.0.0.1", ln, (name, ln))
        self.assertIn("ProxyHandler({})", (ROOT / "interim" / "manager.py").read_text(encoding="utf-8"))   # プロキシなし

    def test_static_page_is_self_contained_and_shows_url_and_steps(self):
        html = (ROOT / "interim" / "static" / "index.html").read_text(encoding="utf-8")
        for needle in ("location.origin", "起動", "更新", "スモーク実行", "停止", "待機中" if False else "確認中"):
            self.assertIn(needle, html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("eval(", html)
        self.assertIn("X-Interim-Request", html)
        for label in ("待機中", "起動中", "準備中", "準備完了", "失敗", "外部起動中"):
            self.assertIn(label, (ROOT / "interim" / "manager.py").read_text(encoding="utf-8"))          # 状態の表示名

    def test_ui_port_selection(self):
        self.assertEqual(interim_console.pick_ui_port(lambda p: p == 8775), 8775)
        self.assertEqual(interim_console.pick_ui_port(lambda p: True), 8771)
        self.assertIsNone(interim_console.pick_ui_port(lambda p: False))
        self.assertTrue(all(8771 <= p <= 8790 for p in interim_console.UI_PORTS))

    def test_proc_start_time_for_current_and_missing_process(self):
        import os
        if os.name == "nt":
            self.assertIsInstance(mg.proc_start_time(os.getpid()), int)
        self.assertIsNone(mg.proc_start_time(0))
        self.assertIsNone(mg.proc_start_time(2 ** 22 - 4))


class HttpServerTest(Base):
    def setUp(self):
        super().setUp()
        self.server = srv.make_server(self.m, 0)
        self.port = self.server.server_address[1]
        self.t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.t.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def req(self, method, path, headers=None, host=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=dict(headers or {}))
        if host:
            r.remove_header("Host")
            r.add_header("Host", host)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(r, timeout=10) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_binds_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for host in ("0.0.0.0", "", "192.168.0.1", "::"):
            with self.assertRaises(ValueError):
                srv.make_server(self.m, 0, host=host)

    def test_page_and_status(self):
        code, body = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("暫定 API 操作".encode("utf-8"), body)
        code, body = self.req("GET", "/api/status")
        data = json.loads(body)
        self.assertEqual((code, data["ok"], data["api"]["state"]), (200, True, "idle"))
        self.assertEqual(self.req("GET", "/nope")[0], 404)

    def test_host_header_and_custom_header_are_enforced(self):
        self.assertEqual(self.req("GET", "/api/status", host="evil.example")[0], 403)
        self.assertEqual(self.req("POST", "/api/start")[0], 403)                                  # カスタムヘッダなし(別サイトからの送信を想定)
        self.assertEqual(self.req("POST", "/api/start", {"X-Interim-Request": "1"}, host="evil.example")[0], 403)
        self.assertEqual(self.env.spawned, [])
        self.assertEqual(self.req("PUT", "/api/start", {"X-Interim-Request": "1"})[0], 405)
        self.assertEqual(self.req("DELETE", "/api/stop", {"X-Interim-Request": "1"})[0], 405)

    def test_cross_origin_posts_are_refused_even_with_the_custom_header(self):
        for origin in ("http://evil.example", "https://127.0.0.1.evil.example", "null", "http://[::1"):
            code, _ = self.req("POST", "/api/start", {"X-Interim-Request": "1", "Origin": origin})
            self.assertEqual(code, 403, origin)
        self.assertEqual(self.env.spawned, [])
        code, _ = self.req("POST", "/api/start", {"X-Interim-Request": "1", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(code, 200)                                                                # この画面自身(loopback)からは受け付ける
        self.assertEqual(len(self.env.spawned), 1)

    def test_start_stop_smoke_over_http_with_fixed_messages(self):
        h = {"X-Interim-Request": "1"}
        code, body = self.req("POST", "/api/start", h)
        self.assertEqual((code, json.loads(body)["api"]["state"]), (200, "starting"))
        code, body = self.req("POST", "/api/start", h)
        d = json.loads(body)
        self.assertEqual((code, d["code"], d["error"]), (409, "already", mg.MESSAGES["already"]))     # 二重起動は 409
        self.assertEqual(self.req("POST", "/api/smoke", h)[0], 409)                                    # 準備完了前のスモークは拒否
        self.env.serve_as(self.m, 200)
        code, body = self.req("POST", "/api/smoke", h)
        self.assertEqual((code, json.loads(body)["smoke"]["state"]), (200, "success"))
        code, body = self.req("POST", "/api/stop", h)
        self.assertEqual((code, json.loads(body)["api"]["state"]), (200, "idle"))
        text = body.decode("utf-8")
        for leaked in (str(self.env.tmp), str(self.env.model), "powershell", "-InstanceId"):
            self.assertNotIn(leaked, text)

    def test_unexpected_exception_is_a_fixed_500_without_details(self):
        self.m.start = lambda: (_ for _ in ()).throw(RuntimeError(r"C:\secret\x password=pw 10.9.8.7"))
        code, body = self.req("POST", "/api/start", {"X-Interim-Request": "1"})
        self.assertEqual(code, 500)
        for leaked in ("secret", "pw", "10.9.8.7"):
            self.assertNotIn(leaked, body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
