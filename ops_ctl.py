# 運用画面(ops_console.py serve, 既定ポート 8765)の起動・停止・状態確認。
#   python ops_ctl.py start  [--port 8765] [--config <app の設定ファイル>]
#   python ops_ctl.py stop   [--port 8765]
#   python ops_ctl.py status [--port 8765]
# 方針(API 側の prod_ctl.py と同じ考え方):
# - 起動した画面のプロセスは、PID とプロセス作成時刻を `ops_console.local.pid`(Git 管理外)に記録する。
# - 停止は、記録した PID が「同じプロセス(作成時刻が一致し、コマンドラインにこの場所の ops_console.py と serve と --port を含む)」で、
#   しかも待受ポートを持つのがそのプロセス(またはその子)のときだけ。名前(python.exe)での一括終了はしない。記録の無いプロセスは止めない。
# - 製番処理・定期実行・検証の実行中(実行ロックが取られている)は、停止しない。
# - 画面は 127.0.0.1 だけで待ち受ける(ops_console.py の仕様)。タスク・スタートアップ・サービス・設定・履歴は変更しない。
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = "ops_console.local.pid"
LOG_FILE = "ops_console.local.log"
DEFAULT_PORT = 8765
START_WAIT_SEC = 40
STOP_WAIT_SEC = 15
EXIT_OK, EXIT_FAIL = 0, 1


class SystemOps:
    """OS への問い合わせ・操作(テストでは差し替えず、隔離した偽の画面プロセスに対して実物を使う)。"""

    PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]

    def _ps_json(self, script: str):
        script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
        r = subprocess.run(self.PS + [script], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        text = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode != 0 or not text:
            return []
        data = json.loads(text)
        return data if isinstance(data, list) else [data]

    def processes(self) -> dict:
        rows = self._ps_json("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine,"
                             "@{n='Created';e={$_.CreationDate.ToString('o')}} | ConvertTo-Json -Compress")
        return {int(r["ProcessId"]): {"pid": int(r["ProcessId"]), "ppid": int(r["ParentProcessId"] or 0),
                                      "cmd": r.get("CommandLine") or "", "created": r.get("Created") or ""} for r in rows}

    def listeners(self, port: int) -> list:
        r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        pids = set()
        for line in r.stdout.decode("ascii", "replace").splitlines():
            cols = line.split()                              # Proto, Local, Foreign, State, PID
            if len(cols) >= 5 and cols[3].upper() == "LISTENING" and cols[1].rsplit(":", 1)[-1] == str(int(port)):
                pids.add(int(cols[4]))
        return sorted(pids)

    def kill(self, pid: int) -> None:
        os.kill(pid, signal.SIGTERM)          # Windows では TerminateProcess(その PID だけ)

    def spawn(self, argv, cwd, log_path) -> int:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        with open(log_path, "ab") as log:
            p = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags, close_fds=True)
        return p.pid


def default_busy_check(base: Path):
    sys.path.insert(0, str(base))
    from ops.runner import RunLock                 # 実行ロック(製番処理・定期実行・検証が持つ)。同じ仕組みで確認する
    return RunLock(base).is_held


def health_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


class OpsController:
    def __init__(self, base=BASE_DIR, port=DEFAULT_PORT, python=None, config=None, ops=None, busy_check=None, say=print):
        self.base = Path(base).resolve()
        self.port = int(port)
        self.python = python or sys.executable
        self.config = config
        self.ops = ops or SystemOps()
        self.busy_check = busy_check or default_busy_check(self.base)
        self.say = say
        self.pid_path = self.base / PID_FILE
        self.script = str(self.base / "ops_console.py")

    # ---- 記録・照合 ----
    def read_record(self):
        try:
            data = json.loads(self.pid_path.read_text(encoding="utf-8"))
            return {"pid": int(data["pid"]), "created": str(data["created"]), "port": int(data["port"]), "base": str(data["base"])}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_record(self, pid, created):
        self.pid_path.write_text(json.dumps({"pid": pid, "created": created, "port": self.port, "base": str(self.base)}), encoding="utf-8")

    def _clear_record(self):
        try:
            self.pid_path.unlink()
        except OSError:
            pass

    def is_ours(self, rec, procs) -> bool:
        p = procs.get(rec["pid"]) if rec else None
        if not p or p["created"] != rec["created"] or rec["port"] != self.port or Path(rec["base"]) != self.base:
            return False
        low = p["cmd"].lower()
        return self.script.lower() in low and " serve" in low and f"--port {self.port}" in low

    def _tree(self, pid, procs) -> list:
        """pid 自身と、その子孫のうち ops_console.py を実行しているものだけ(venv の起動ランチャー → 実体の python)。"""
        out, todo = [pid], [pid]
        while todo:
            cur = todo.pop()
            for q in procs.values():
                if q["ppid"] == cur and q["pid"] not in out and self.script.lower() in q["cmd"].lower():
                    out.append(q["pid"])
                    todo.append(q["pid"])
        return out

    # ---- 操作 ----
    def start(self) -> int:
        procs = self.ops.processes()
        rec = self.read_record()
        if rec and self.is_ours(rec, procs):
            self.say(f"運用画面はすでに起動しています(PID {rec['pid']}): http://127.0.0.1:{self.port}/")
            return EXIT_OK if health_ok(self.port) else EXIT_FAIL
        if rec:
            self._clear_record()                                  # 古い記録(別のプロセスになっている・終了済み)だけを消す
        holders = self.ops.listeners(self.port)
        if holders:
            h = procs.get(holders[0], {})
            self.say(f"起動しません: ポート {self.port} はすでに別のプロセス(PID {holders[0]})が使っています。{h.get('cmd', '')}")
            self.say("このスクリプトで起動したものではないため、停止もしません。画面の作業ツリーを確認してください。")
            return EXIT_FAIL
        argv = [self.python, "-X", "utf8", self.script, "serve", "--port", str(self.port)]
        if self.config:
            argv += ["--config", str(self.config)]
        pid = self.ops.spawn(argv, self.base, self.base / LOG_FILE)
        created = self._wait_created(pid)
        if not created:
            self.say("起動に失敗しました(プロセスを確認できません)。ログ: " + str(self.base / LOG_FILE))
            return EXIT_FAIL
        self._write_record(pid, created)
        deadline = time.monotonic() + START_WAIT_SEC
        while time.monotonic() < deadline:
            procs = self.ops.processes()
            if pid not in procs:
                break
            tree = self._tree(pid, procs)
            if health_ok(self.port) and any(h in tree for h in self.ops.listeners(self.port)):
                self.say(f"運用画面を起動しました(PID {pid}): http://127.0.0.1:{self.port}/  作業ツリー: {self.base}")
                return EXIT_OK
            time.sleep(0.5)
        self.say("起動を確認できませんでした。起動したプロセスだけを終了します。ログ: " + str(self.base / LOG_FILE))
        self._kill_tree(pid)
        self._clear_record()
        return EXIT_FAIL

    def _wait_created(self, pid):
        for _ in range(20):
            p = self.ops.processes().get(pid)
            if p:
                return p["created"]
            time.sleep(0.25)
        return None

    def _kill_tree(self, pid):
        procs = self.ops.processes()
        for q in reversed(self._tree(pid, procs)):                # 子 → 親の順
            try:
                self.ops.kill(q)
            except OSError:
                pass

    def stop(self) -> int:
        rec = self.read_record()
        procs = self.ops.processes()
        if not rec:
            self.say("停止しません: このスクリプトで起動した記録がありません。")
            holders = self.ops.listeners(self.port)
            if holders:
                self.say(f"(ポート {self.port} は PID {holders[0]} が使用中です。記録の無いプロセスは止めません。)")
            return EXIT_FAIL
        if rec["pid"] not in procs:
            self._clear_record()
            self.say("運用画面は、すでに停止しています(古い記録を消しました)。")
            return EXIT_OK
        if not self.is_ours(rec, procs):
            self.say(f"停止しません: 記録の PID {rec['pid']} は、起動したときと同じプロセスではありません(作成時刻・コマンドラインが不一致)。")
            return EXIT_FAIL
        tree = self._tree(rec["pid"], procs)
        holders = self.ops.listeners(self.port)
        if holders and not all(h in tree for h in holders):
            self.say(f"停止しません: ポート {self.port} を使っているのが、記録したプロセスではありません(PID {holders})。")
            return EXIT_FAIL
        try:
            busy = self.busy_check()
        except Exception as e:  # noqa: BLE001
            self.say(f"停止しません: 実行中かどうかを確認できませんでした({type(e).__name__})。")
            return EXIT_FAIL
        if busy:
            self.say("停止しません: 製番処理・定期実行・検証のいずれかが実行中です。完了してからもう一度実行してください。")
            return EXIT_FAIL
        self._kill_tree(rec["pid"])
        deadline = time.monotonic() + STOP_WAIT_SEC
        while time.monotonic() < deadline:
            if not any(p in self.ops.processes() for p in tree) and not self.ops.listeners(self.port):
                self._clear_record()
                self.say(f"運用画面を停止しました(PID {tree})。")
                return EXIT_OK
            time.sleep(0.3)
        self.say("停止を確認できませんでした。プロセスが残っている可能性があります(記録は残しています)。")
        return EXIT_FAIL

    def status(self) -> int:
        rec = self.read_record()
        procs = self.ops.processes()
        holders = self.ops.listeners(self.port)
        if rec and self.is_ours(rec, procs):
            self.say(f"運用画面: 起動中(PID {rec['pid']}) http://127.0.0.1:{self.port}/ health={'ok' if health_ok(self.port) else 'NG'} 作業ツリー: {self.base}")
            return EXIT_OK
        if holders:
            h = procs.get(holders[0], {})
            self.say(f"ポート {self.port} は、記録の無いプロセス(PID {holders[0]})が使用中です。{h.get('cmd', '')}")
            return EXIT_FAIL
        self.say("運用画面: 停止中")
        return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="運用画面(8765)の起動・停止")
    ap.add_argument("action", choices=["start", "stop", "status"])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--config", default=None, help="app の設定ファイル(省略時は ops_console.py の既定)")
    args = ap.parse_args(argv)
    ctl = OpsController(port=args.port, config=args.config)
    return getattr(ctl, args.action)()


if __name__ == "__main__":
    sys.exit(main())
