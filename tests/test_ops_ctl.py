# 運用画面(8765)の起動・停止(ops_ctl.py)の隔離テスト。
# 一時フォルダに偽の ops_console.py(127.0.0.1 の空きポートで /api/state に 200 を返すだけ)を置いて、それに対してだけ実物の制御を動かす。
# 実際の 8765 の画面・API(18080)・タスク・履歴・設定・共有ファイルには触れない。製番処理・/predict も行わない。
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ops_ctl  # noqa: E402
from ops.runner import RunLock  # noqa: E402

FAKE_CONSOLE = textwrap.dedent("""
    import argparse, http.server, sys
    ap = argparse.ArgumentParser(); ap.add_argument("cmd"); ap.add_argument("--port", type=int); ap.add_argument("--config")
    a = ap.parse_args()
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *x): pass
    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
""")
LISTENER = "import socket,sys,time;s=socket.socket();s.bind(('127.0.0.1',int(sys.argv[1])));s.listen(1);time.sleep(120)"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.name == "nt", "Windows 用(PowerShell・OS ロック)")
class OpsCtlTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        (self.base / "ops_console.py").write_text(FAKE_CONSOLE, encoding="utf-8")
        self.port = free_port()
        self.lines = []
        self.extra = []

    def tearDown(self):
        for p in self.extra:
            p.kill()
            p.wait()
        ctl = self.ctl()
        if ctl.read_record():
            ctl.busy_check = lambda: False
            ctl.stop()
        self._td.cleanup()

    def ctl(self, busy=None):
        return ops_ctl.OpsController(base=self.base, port=self.port, busy_check=busy or RunLock(self.base).is_held,
                                     say=self.lines.append)

    def text(self):
        return "\n".join(self.lines)

    def bystander(self):
        p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
        self.extra.append(p)
        return p

    def test_start_status_stop_touches_only_the_recorded_process(self):
        other = self.bystander()                                    # 同じ python.exe の無関係なプロセス(名前での一括終了なら巻き込まれる)
        c = self.ctl()
        self.assertEqual(c.start(), 0, self.text())
        rec = c.read_record()
        self.assertEqual(rec["port"], self.port)
        self.assertTrue(ops_ctl.health_ok(self.port))
        self.assertEqual(c.status(), 0)
        self.assertEqual(self.ctl().start(), 0)                     # 二重起動はしない(同じ PID のまま)
        self.assertEqual(c.read_record()["pid"], rec["pid"])
        self.assertEqual(c.stop(), 0, self.text())
        self.assertFalse(ops_ctl.health_ok(self.port))
        self.assertIsNone(c.read_record())
        self.assertIsNone(other.poll(), "無関係なプロセスを止めてはいけない")

    def test_start_refuses_when_the_port_is_used_by_another_process_and_leaves_it(self):
        holder = subprocess.Popen([sys.executable, "-c", LISTENER, str(self.port)])
        self.extra.append(holder)
        time.sleep(1.0)
        c = self.ctl()
        self.assertEqual(c.start(), 1)
        self.assertIn("別のプロセス", self.text())
        self.assertIsNone(holder.poll())
        self.assertIsNone(c.read_record())

    def test_stop_without_a_record_stops_nothing(self):
        holder = subprocess.Popen([sys.executable, "-c", LISTENER, str(self.port)])
        self.extra.append(holder)
        time.sleep(1.0)
        self.assertEqual(self.ctl().stop(), 1)
        self.assertIn("記録がありません", self.text())
        self.assertIsNone(holder.poll())

    def test_stop_refuses_when_the_record_is_not_the_same_process(self):
        c = self.ctl()
        self.assertEqual(c.start(), 0, self.text())
        rec = c.read_record()
        (self.base / ops_ctl.PID_FILE).write_text(json.dumps({**rec, "created": "1999-01-01T00:00:00.0000000+09:00"}), encoding="utf-8")
        self.assertEqual(c.stop(), 1)
        self.assertIn("同じプロセスではありません", self.text())
        self.assertTrue(ops_ctl.health_ok(self.port), "不一致のときは止めない")
        (self.base / ops_ctl.PID_FILE).write_text(json.dumps(rec), encoding="utf-8")   # 後片付け(tearDown が止める)

    def test_stop_refuses_while_a_run_holds_the_lock(self):
        c = self.ctl()
        self.assertEqual(c.start(), 0, self.text())
        lock = RunLock(self.base)
        lock.acquire("test")
        try:
            self.assertEqual(c.stop(), 1)
            self.assertIn("実行中", self.text())
            self.assertTrue(ops_ctl.health_ok(self.port))
        finally:
            lock.release()
        self.assertEqual(c.stop(), 0, self.text())                  # ロックが解放されたら停止できる

    def test_stop_refuses_when_the_busy_state_cannot_be_checked(self):
        c = self.ctl(busy=lambda: (_ for _ in ()).throw(OSError("x")))
        self.assertEqual(c.start(), 0, self.text())
        self.assertEqual(c.stop(), 1)
        self.assertIn("確認できませんでした", self.text())
        self.assertTrue(ops_ctl.health_ok(self.port))

    def test_stop_cleans_a_stale_record_without_touching_anything(self):
        (self.base / ops_ctl.PID_FILE).write_text(json.dumps({"pid": 4000000, "created": "x", "port": self.port, "base": str(self.base)}), encoding="utf-8")
        other = self.bystander()
        self.assertEqual(self.ctl().stop(), 0)
        self.assertIsNone(self.ctl().read_record())
        self.assertIsNone(other.poll())


class BatchFilesTest(unittest.TestCase):
    NAMES = ("start_ops_console.bat", "stop_ops_console.bat")

    def test_batches_use_the_controller_and_never_kill_by_name_or_register_anything(self):
        for name in self.NAMES:
            raw = (ROOT / name).read_bytes()
            low = raw.decode("ascii").lower()
            self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"), name)          # CRLF
            self.assertIn("ops_ctl.py " + ("start" if name.startswith("start") else "stop"), low)
            for forbidden in ("taskkill", "/im ", "stop-process", "sc create", "nssm", "schtasks", "startup", "reg add", "netsh", "publish"):
                if forbidden == "startup":
                    # コメント(rem)の「Startup folder に登録しない」という説明だけは許す
                    self.assertFalse([ln for ln in low.splitlines() if "startup" in ln and not ln.startswith("rem")], name)
                else:
                    self.assertNotIn(forbidden, low, name)
            self.assertNotRegex(low, r"(?m)^\s*timeout\b", name)                  # 標準入力が閉じていると失敗する(PR #44 と同じ理由)
            self.assertIn("chcp 65001", low)


if __name__ == "__main__":
    unittest.main()
