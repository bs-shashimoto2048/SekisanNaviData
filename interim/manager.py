# 暫定 API 操作用 UI の管理部(標準ライブラリ + PyYAML)。ops(運用画面)とは独立。
# Sahi の暫定 API(inference_server/server.py。検証専用)を、この PC の loopback で、UI から起動・状態確認・スモーク・停止する。
#
# 安全条件:
# - 触れるのは、リポジトリ内の output_interim と tools/run_interim_api.ps1・tools/smoke_interim_api.py だけ。実図面・製番処理(app.main)・DB・共有・CAD・
#   外部ネットワーク・タスクスケジューラには触れない。通信先は 127.0.0.1(プロキシなし・リダイレクト追跡なし)だけ。
# - 起動は tools/run_interim_api.ps1(出力先の隔離・リンク/UNC の拒否・既存 server.yaml の保護はそちらで行う)。UI は instance_id(乱数 32 桁)を
#   自分で決めて渡し、(1) 起動した起動スクリプトのプロセス(PID + 作成時刻)、(2) server.yaml、(3) /health の instance_id の 3 つが一致するものだけを
#   「自分が管理する API」とする。一致しないものは「外部起動中」とし、停止も上書きもしない。
# - モデルフォルダと推論用 venv の python は、ローカル設定 interim_console.local.yaml(Git 管理外)から読む。値は画面・応答・ログに出さない。
#   起動スクリプトへ渡すコマンドライン(パス・引数)も、記録・表示しない。
# - 画面・応答に出すのは、状態・HTTP 状態・ポート・検出件数・固定文言だけ(パス・検出値・例外メッセージは出さない)。
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple

import yaml

REPO = Path(__file__).resolve().parent.parent

IDLE, STARTING, LOADING, READY, FAILED, EXTERNAL = "idle", "starting", "loading", "ready", "failed", "external"
LABELS = {IDLE: "待機中", STARTING: "起動中", LOADING: "準備中", READY: "準備完了", FAILED: "失敗", EXTERNAL: "外部起動中"}

PORT_RANGE = (40000, 49999)
STARTUP_TIMEOUT = 240.0                # 起動から準備完了までの上限[秒](超えたら失敗とし、自分の API を停止)
STOP_WAIT = 30.0
STILL_ACTIVE = 259

# 固定の文言(失敗の理由・拒否の理由。例外メッセージ・パスは入れない)
MESSAGES = {
    "settings": "ローカル設定(モデル・推論用 venv)が未設定、または使えません。",
    "busy": "起動・停止・スモークのいずれかが処理中です。",
    "already": "API は既に起動中です(二重起動はできません)。",
    "external": "外部から起動された API が動いています。この画面では起動も停止もしません。",
    "stale": "古い server.yaml が残っています(外部のもの)。稼働中でないと確認してから、手動で対処してください。",
    "not_managed": "この画面が起動した API ではないため、停止しません。",
    "owner_mismatch": "起動元の確認(PID・instance_id)が一致しないため、停止しません。",
    "not_ready": "API が準備完了ではないため、スモークは実行できません。",
    "smoke_running": "スモークを実行中です。",
    "launcher_refused": "起動スクリプトが安全条件により起動を拒否しました。",
    "launcher_exited": "起動スクリプトまたは API が、準備完了の前に終了しました。",
    "api_prepare_failed": "API の準備(モデルの読み込み)に失敗しました。",
    "startup_timeout": "準備完了まで待ちましたが、時間内に完了しませんでした。起動した API を停止しました。",
    "stop_failed": "API を停止できませんでした。",
    "spawn_failed": "起動スクリプトを開始できませんでした。",
    "no_port": "空いているポートを選べませんでした。",
}


class Refused(Exception):
    """操作を受け付けない(固定の理由コード)。"""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Busy(Refused):
    pass


# ---------------------------------------------------------------------------- OS・通信の部品(テストで差し替える)
def proc_start_time(pid: int) -> Optional[int]:
    """実行中のプロセスの作成時刻(FILETIME)。存在しない・終了済みなら None。PID の使い回しの判別に使う。"""
    if os.name != "nt" or not pid:
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, int(pid))              # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        k32.CloseHandle(h)


def kill_tree(pid: int) -> None:
    """起動スクリプトのプロセスと、その子(API 本体)を終了する。呼び出し側が、自分が起動したプロセスだと確認した後だけ呼ぶ。"""
    subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=30)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)      # プロキシなし・リダイレクト追跡なし


def _http(url: str, data: Optional[bytes] = None, timeout: float = 3.0) -> Tuple[Optional[int], Optional[dict]]:
    """loopback への 1 回の要求。(HTTP 状態, JSON 本文)。接続できなければ (None, None)。例外メッセージは返さない。"""
    try:
        with _OPENER.open(urllib.request.Request(url, data=data, method="POST" if data is not None else "GET"), timeout=timeout) as r:
            status, raw = r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        status, raw = e.code, (e.read(65536) if hasattr(e, "read") else b"")
    except (urllib.error.URLError, OSError, ValueError):
        return None, None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        body = None
    return status, body if isinstance(body, dict) else None


def http_health(port: int):
    return _http(f"http://127.0.0.1:{port}/health")


def http_shutdown(port: int, instance_id: str):
    return _http(f"http://127.0.0.1:{port}/shutdown", data=f"instance_id={instance_id}".encode("ascii"))


def port_is_free(port: int) -> bool:
    """loopback のポートが未使用か(bind できて、接続も受け付けられない)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_free_port(is_free: Callable[[int], bool] = port_is_free, rng=secrets.SystemRandom()) -> Optional[int]:
    for _ in range(60):
        p = rng.randint(*PORT_RANGE)
        if is_free(p):
            return p
    return None


def run_smoke_process(repo: Path) -> Tuple[int, str]:
    """tools/smoke_interim_api.py を 1 回実行する。(終了コード, 標準出力)。出力は呼び出し側が固定の形式だけを解析し、そのままは保存・表示しない。"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, str(repo / "tools" / "smoke_interim_api.py")], capture_output=True, timeout=400, cwd=str(repo), env=env)
    return p.returncode, p.stdout.decode("utf-8", "replace")


_STEP = re.compile(r"^\[(\d)/5\] .*: OK")
_DETECT = re.compile(r"検出 (\d+) 件")


def parse_smoke(code: int, out: str) -> dict:
    """スモークの出力から、手順ごとの OK と検出件数だけを取り出す(パス・値・メッセージは捨てる)。"""
    ok_steps = sorted({int(m.group(1)) for ln in out.splitlines() if (m := _STEP.match(ln))})
    detections = None
    for ln in out.splitlines():
        if ln.startswith("[5/5]") and (m := _DETECT.search(ln)):
            detections = int(m.group(1))
    success = code == 0 and ok_steps == [1, 2, 3, 4, 5]
    return {"state": "success" if success else "failed", "ok_steps": ok_steps, "detections": detections if success else None,
            "refused": code == 2, "exit": code}


# ---------------------------------------------------------------------------- ローカル設定
def load_settings(path: Path) -> Optional[dict]:
    """モデルフォルダと推論用 python。使えるものだけ返す(値は呼び出し側が表示しない)。"""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        model, py = str(data["model_dir"]), str(data["python"])
    except Exception:  # noqa: BLE001
        return None
    for v in (model, py):
        if v.startswith("\\\\") or v.startswith("//") or not os.path.isabs(v):
            return None
    if not (Path(model, "best.pt").is_file() and Path(model, "model.yaml").is_file()):
        return None
    if not (Path(py).is_file() and Path(py).name.lower().startswith("python") and Path(py).suffix.lower() == ".exe"):
        return None
    return {"model_dir": model, "python": py}


# ---------------------------------------------------------------------------- 管理
class ApiManager:
    def __init__(self, repo: Path = REPO, *, settings_path: Optional[Path] = None, spawn=subprocess.Popen, health=http_health,
                 shutdown=http_shutdown, start_time=proc_start_time, kill=kill_tree, free_port=pick_free_port,
                 smoke_runner=run_smoke_process, clock=time.monotonic, sleep=time.sleep, startup_timeout=STARTUP_TIMEOUT,
                 stop_wait=STOP_WAIT, background=True):
        self.repo = Path(repo)
        self.isolated = self.repo / "output_interim"
        self.yaml_path = self.isolated / "api" / "server.yaml"
        self.state_path = self.isolated / ".interim_console_state.json"
        self.settings_path = Path(settings_path) if settings_path else self.repo / "interim_console.local.yaml"
        self.launcher = self.repo / "tools" / "run_interim_api.ps1"
        self._spawn, self._health, self._shutdown, self._start_time, self._kill = spawn, health, shutdown, start_time, kill
        self._free_port, self._smoke_runner, self._clock, self._sleep = free_port, smoke_runner, clock, sleep
        self.startup_timeout, self.stop_wait, self._background = startup_timeout, stop_wait, background
        self.lock = threading.RLock()
        self.rec: Optional[dict] = None          # 自分が起動した API: {instance_id, pid, ctime, port, t0, proc, ever_ready}
        self.failure: Optional[str] = None       # 直近の失敗の理由コード
        self.op: Optional[str] = None            # 処理中の操作("start" / "stop")
        self.smoke_state: dict = {"state": "idle", "ok_steps": [], "detections": None, "refused": False, "exit": None}
        self._smoke_thread: Optional[threading.Thread] = None
        self._loaded = False

    # ---- 状態ファイル(UI を再起動しても、自分が起動した API を引き続き管理できるように) ----
    def _save_rec(self):
        r = self.rec
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({k: r[k] for k in ("instance_id", "pid", "ctime", "port")}), encoding="utf-8")

    def _clear_rec(self):
        self.rec = None
        try:
            self.state_path.unlink()
        except OSError:
            pass

    def _adopt_saved(self):
        """前回の UI が起動した API の記録を、PID・作成時刻・instance_id の形式を確認できたときだけ引き継ぐ。"""
        if self._loaded:
            return
        self._loaded = True
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            ok = (re.fullmatch(r"[0-9a-f]{32}", str(d["instance_id"])) and isinstance(d["pid"], int) and isinstance(d["ctime"], int)
                  and isinstance(d["port"], int) and PORT_RANGE[0] <= d["port"] <= PORT_RANGE[1])
        except Exception:  # noqa: BLE001
            return
        if ok and self._start_time(d["pid"]) == d["ctime"]:                 # PID の使い回し(別のプロセス)でないこと
            self.rec = dict(d, t0=self._clock(), proc=None, ever_ready=False)
        elif ok:
            self._remove_own_yaml(d["instance_id"])
            self._clear_rec()

    # ---- 補助 ----
    def _yaml_text(self) -> Optional[str]:
        try:
            return self.yaml_path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _remove_own_yaml(self, iid: str):
        t = self._yaml_text()
        if t is not None and re.search(rf"instance_id:\s*{iid}\b", t):
            try:
                self.yaml_path.unlink()
            except OSError:
                pass

    def _yaml_port(self) -> Optional[int]:
        t = self._yaml_text()
        m = re.search(r"^\s*port:\s*(\d+)", t or "", re.M)
        return int(m.group(1)) if m and 1024 <= int(m.group(1)) <= 65535 else None

    def _owner_alive(self) -> bool:
        r = self.rec
        return r["ctime"] is not None and self._start_time(r["pid"]) == r["ctime"]

    def _settings(self):
        return load_settings(self.settings_path)

    # ---- 状態 ----
    def _evaluate(self) -> Tuple[str, Optional[int], Optional[int]]:
        """(状態, ポート, HTTP 状態)。必要なら記録を更新する(外部のものは変更しない)。"""
        self._adopt_saved()
        r = self.rec
        if r:
            code, body = self._health(r["port"])
            mine = bool(body) and body.get("instance_id") == r["instance_id"]
            alive = self._owner_alive()
            if code is not None and not mine:                                # 応答はあるが instance_id が違う: 自分の API ではない
                self._clear_rec()
                return EXTERNAL, r["port"], code
            if not alive:
                reason = "launcher_exited"
                proc = r.get("proc")
                if proc is not None and proc.poll() == 2:
                    reason = "launcher_refused"
                self._remove_own_yaml(r["instance_id"])
                self._clear_rec()
                self.failure = reason
                return FAILED, None, None
            if code == 200 and mine:
                r["ever_ready"] = True
                r["last"] = READY
                return READY, r["port"], 200
            if code == 503 and mine:
                r["last"] = LOADING
                return LOADING, r["port"], 503
            if code == 500 and mine:
                self.failure = "api_prepare_failed"
                return FAILED, r["port"], 500
            if code is None and self._clock() - r["t0"] > self.startup_timeout and not r["ever_ready"]:
                self._terminate_own(r)
                self.failure = "startup_timeout"
                return FAILED, None, None
            if code is None and r.get("last"):                                   # 応答が一時的に遅れているだけ(読み込み中の負荷など): 直前の状態を保つ
                return r["last"], r["port"], None
            return STARTING, r["port"], None
        t = self._yaml_text()
        if t is not None:                                                    # 自分が起動していない server.yaml
            port = self._yaml_port()
            code, _ = self._health(port) if port else (None, None)
            if code is not None:
                return EXTERNAL, port, code
            return IDLE, None, None                                          # 古い server.yaml(外部)。起動は拒否する
        if self.failure:
            return FAILED, None, None
        return IDLE, None, None

    def _terminate_own(self, r: dict):
        """自分が起動したと確認できる(PID + 作成時刻)プロセスだけを、その子(API 本体)ごと終了する。"""
        if r["ctime"] is not None and self._start_time(r["pid"]) == r["ctime"]:
            self._kill(r["pid"])
        self._remove_own_yaml(r["instance_id"])
        self._clear_rec()

    def snapshot(self) -> dict:
        with self.lock:
            state, port, http = self._evaluate()
            stale = state == IDLE and self._yaml_text() is not None
            smoke_running = self.smoke_state["state"] == "running"
            busy = self.op is not None
            configured = self._settings() is not None
            return {
                "api": {"state": state, "label": LABELS[state], "port": port, "http": http, "managed": self.rec is not None,
                        "external": state == EXTERNAL or stale, "stale": stale,
                        "reason": MESSAGES.get(self.failure) if state == FAILED and self.failure else None,
                        "busy": busy, "settings_ok": configured},
                "can": {"start": (state in (IDLE, FAILED)) and self.rec is None and not stale and not busy and configured and not smoke_running,
                        "stop": self.rec is not None and not busy and not smoke_running,
                        "smoke": state in (READY, EXTERNAL) and http == 200 and not busy and not smoke_running},
                "smoke": dict(self.smoke_state),
            }

    # ---- 操作 ----
    def start(self) -> dict:
        with self.lock:
            if self.op:
                raise Busy("busy")
            state, _, _ = self._evaluate()
            if state in (STARTING, LOADING, READY) or self.rec is not None:
                raise Busy("already")
            if state == EXTERNAL:
                raise Refused("external")
            if self._yaml_text() is not None:
                raise Refused("stale")
            if self.smoke_state["state"] == "running":
                raise Busy("busy")
            st = self._settings()
            if not st:
                raise Refused("settings")
            port = self._free_port()
            if not port:
                raise Refused("no_port")
            iid = secrets.token_hex(16)
            args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.launcher),
                    "-Port", str(port), "-Backend", "sahi", "-ModelDir", st["model_dir"], "-PythonExe", st["python"], "-InstanceId", iid]
            self.op = "start"
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                proc = self._spawn(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   cwd=str(self.repo), creationflags=flags)
            except Exception:  # noqa: BLE001 - メッセージ(パスを含み得る)は残さない
                self.failure = "spawn_failed"
                raise Refused("spawn_failed") from None
            finally:
                self.op = None
            ctime = self._start_time(proc.pid)
            if ctime is None:
                # 起動直後にプロセスの作成時刻を確認できない(すでに終了した、または取得できない)。所有を確認できないものを管理外のまま残さないよう、
                # 自分が今起動したプロセス(Popen のハンドルを持つもの)だけを終了して失敗にする。
                try:
                    self._kill(proc.pid)
                except Exception:  # noqa: BLE001
                    pass
                self.failure = "spawn_failed"
                raise Refused("spawn_failed")
            self.failure = None
            self.rec = {"instance_id": iid, "pid": proc.pid, "ctime": ctime, "port": port, "t0": self._clock(), "proc": proc,
                        "ever_ready": False}
            self._save_rec()
            return self.snapshot()

    def stop(self) -> dict:
        with self.lock:
            if self.op:
                raise Busy("busy")
            if self.smoke_state["state"] == "running":
                raise Busy("smoke_running")                                  # スモーク中は停止しない(結果が不明になるため)
            self._evaluate()
            r = self.rec
            if r is None:
                raise Refused("external" if self._yaml_text() is not None else "not_managed")
            if not self._owner_alive():
                raise Refused("owner_mismatch")
            self.op = "stop"
            try:
                code, body = self._health(r["port"])
                if code is not None:
                    if not (body and body.get("instance_id") == r["instance_id"]):
                        raise Refused("owner_mismatch")                      # instance_id が違う: 止めない
                    self._shutdown(r["port"], r["instance_id"])                 # 正常停止(instance_id が一致する場合だけサーバーが受け付ける)
                    deadline = self._clock() + self.stop_wait
                    while self._owner_alive() and self._clock() < deadline:
                        self._sleep(0.5)
                if self._owner_alive():                                          # 応答がない(起動中)・正常停止に応じない: 自分のプロセスだけを終了
                    self._kill(r["pid"])
                    deadline = self._clock() + 10
                    while self._owner_alive() and self._clock() < deadline:
                        self._sleep(0.5)
                    if self._owner_alive():
                        self.failure = "stop_failed"
                        raise Refused("stop_failed")
                self._remove_own_yaml(r["instance_id"])
                self._clear_rec()
                self.failure = None
            finally:
                self.op = None
            return self.snapshot()

    def smoke(self) -> dict:
        with self.lock:
            if self.smoke_state["state"] == "running":
                raise Busy("smoke_running")
            if self.op:
                raise Busy("busy")
            state, _, http = self._evaluate()
            if not (state in (READY, EXTERNAL) and http == 200):
                raise Refused("not_ready")
            self.smoke_state = {"state": "running", "ok_steps": [], "detections": None, "refused": False, "exit": None}
            if self._background:
                t = threading.Thread(target=self._run_smoke, name="interim-smoke", daemon=True)
                self._smoke_thread = t
                t.start()
            else:
                self._run_smoke()
            return self.snapshot()

    def _run_smoke(self):
        try:
            code, out = self._smoke_runner(self.repo)
            result = parse_smoke(code, out)
        except Exception:  # noqa: BLE001 - 例外メッセージは残さない
            result = {"state": "failed", "ok_steps": [], "detections": None, "refused": False, "exit": None}
        with self.lock:
            self.smoke_state = result
