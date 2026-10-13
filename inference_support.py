"""データ作成側から推論サーバー(別プロセス・別venv)を起動・確認・停止するための支援モジュール。

Issue #52 の方針:
  - 起動入口(app.main)が推論サーバーを起動し、health/ready を確認してから積算処理を始める。
  - 終了時は「自分が起動した子プロセス」だけを停止する(名前や PID の一括検索で他プロセスを止めない)。
  - 検証モードでは loopback・検証専用ポート・検証用出力先を使い、プロキシは使わない。本番共有は拒否する。
  - 標準ライブラリのみ(推論用の重い依存関係をデータ作成側へ持ち込まない)。
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
INFERENCE_DIR = PROJECT_DIR / "inference_server"
HOST = "127.0.0.1"


class InferenceServerError(RuntimeError):
    """推論サーバーの起動・準備・共有確認の失敗。メッセージは原因が分かる内容にする(秘密情報は含めない)。"""


def _load_safety():
    spec = importlib.util.spec_from_file_location("inference_safety", INFERENCE_DIR / "safety.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("inference_safety", mod)
    spec.loader.exec_module(mod)
    return mod


safety = _load_safety()


@dataclass
class InferenceServerSettings:
    python: Path
    server_script: Path
    port: int
    backend: str = "sahi"
    model_dir: str | None = None
    model_yaml: str = "model.yaml"
    fake_script: str | None = None
    startup_timeout: float = 180.0   # モデル読み込みを含む準備完了までの待ち時間[秒]
    stop_timeout: float = 15.0
    poll_interval: float = 0.3
    request_timeout: float | None = None  # 推論リクエストのタイムアウト。None は従来どおり待ち続ける
    allow_unc: bool = False

    @classmethod
    def from_config(cls, cfg: dict, base_dir: Path = PROJECT_DIR) -> "InferenceServerSettings":
        def rel(p, default):
            p = Path(p if p else default)
            return p if p.is_absolute() else (Path(base_dir) / p)

        return cls(
            python=rel(cfg.get("python"), "inference_server/.venv/Scripts/python.exe"),
            server_script=rel(cfg.get("server_script"), "inference_server/server.py"),
            port=cfg.get("port"),
            backend=cfg.get("backend", "sahi"),
            model_dir=str(rel(cfg["model_dir"], "")) if cfg.get("model_dir") else None,
            model_yaml=cfg.get("model_yaml", "model.yaml"),
            fake_script=str(rel(cfg["fake_script"], "")) if cfg.get("fake_script") else None,
            startup_timeout=float(cfg.get("startup_timeout", 180)),
            stop_timeout=float(cfg.get("stop_timeout", 15)),
            request_timeout=cfg.get("request_timeout"),
            allow_unc=bool(cfg.get("allow_unc", False)),
        )


def _opener():
    # 環境変数のプロキシを使わない(loopback へ直接つなぐ)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(url: str, data: dict | None = None, timeout: float = 5.0):
    """(status, json or None)。接続できなければ例外(URLError 等)。"""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    try:
        with _opener().open(urllib.request.Request(url, data=body), timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "null")
        except ValueError:
            return e.code, None


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


class InferenceServerManager:
    """推論サーバーを子プロセスとして起動し、準備完了を確認し、終了時に自分の子だけを停止する。"""

    def __init__(self, settings: InferenceServerSettings, output_root: str, config_out: str, log_dir: str, logger=None):
        self.s = settings
        self.output_root = os.path.abspath(output_root)
        self.config_out = os.path.abspath(config_out)
        self.log_path = Path(os.path.abspath(log_dir)) / f"inference_server_{settings.port}.log"
        self.logger = logger
        self.instance_id = uuid.uuid4().hex
        self.proc: subprocess.Popen | None = None
        self.returncode: int | None = None
        self._log_fh = None
        self._atexit = None

    # ------------------------------------------------------------------ properties
    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.s.port}"

    def _log(self, level: str, msg: str):
        if self.logger is not None:
            getattr(self.logger, level)(msg)

    # ------------------------------------------------------------------ start / ready
    def validate(self):
        """起動前の検証(ここまでは何も書き込まない)。"""
        try:
            safety.validate_port(self.s.port)
            root = safety.validate_output_root(self.output_root, allow_unc=self.s.allow_unc)
            safety.assert_not_production(self.config_out, "設定出力先")
            safety.assert_under(self.config_out, str(root), "設定出力先")
            safety.assert_not_production(str(self.log_path), "ログ出力先")
        except safety.SafetyError as e:
            raise InferenceServerError(f"推論サーバーの検証設定が安全条件に反するため起動しません: {e}") from e
        if not Path(self.s.python).is_file():
            raise InferenceServerError(f"推論サーバー用のPython(別venv)が見つかりません: {self.s.python}")
        if not Path(self.s.server_script).is_file():
            raise InferenceServerError(f"推論サーバーのスクリプトが見つかりません: {self.s.server_script}")
        if self.s.backend == "sahi" and not (self.s.model_dir and Path(self.s.model_dir).is_dir()):
            raise InferenceServerError("backend=sahi には存在する model_dir が必要です。")
        if not _port_is_free(self.s.port):
            raise InferenceServerError(f"検証用ポート {self.s.port} は既に使用中です(他のサーバーへ接続しないよう起動を中止します)。")

    def start(self):
        if self.proc is not None:
            raise InferenceServerError("既に起動しています。")
        self.validate()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self.log_path, "ab")
        cmd = [str(self.s.python), str(self.s.server_script),
               "--output-root", self.output_root, "--port", str(self.s.port), "--host", HOST,
               "--config-out", self.config_out, "--instance-id", self.instance_id,
               "--backend", self.s.backend, "--parent-pid", str(os.getpid())]
        if self.s.backend == "sahi":
            cmd += ["--model-dir", self.s.model_dir, "--model-yaml", self.s.model_yaml]
        if self.s.fake_script:
            cmd += ["--fake-script", self.s.fake_script]
        if self.s.allow_unc:
            cmd += ["--allow-unc"]
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "NO_PROXY": "127.0.0.1,localhost"}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._log("info", f"推論サーバーを起動: port={self.s.port} backend={self.s.backend} root={self.output_root}")
        self.proc = subprocess.Popen(cmd, stdout=self._log_fh, stderr=subprocess.STDOUT, cwd=str(INFERENCE_DIR),
                                     env=env, creationflags=flags)
        self._atexit = atexit.register(self.stop)
        try:
            self.wait_ready()
        except BaseException:
            self.stop()
            raise
        self._log("info", f"推論サーバー準備完了: pid={self.proc.pid} url={self.base_url}")
        return self

    def _log_tail(self, n: int = 15) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
            return "\n".join(lines)
        except OSError:
            return "(ログを読めません)"

    def wait_ready(self):
        deadline = time.monotonic() + self.s.startup_timeout
        last = "接続待ち"
        while time.monotonic() < deadline:
            rc = self.proc.poll()
            if rc is not None:
                raise InferenceServerError(f"推論サーバーが準備完了前に終了しました(終了コード={rc})。ログ: {self.log_path}\n{self._log_tail()}")
            try:
                status, body = _http(f"{self.base_url}/health", timeout=2.0)
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = f"接続できません({type(e).__name__})"
            else:
                if isinstance(body, dict) and body.get("instance_id") not in (None, self.instance_id):
                    raise InferenceServerError("検証用ポートの応答が、起動した子プロセスのものではありません(instance_id 不一致)。")
                if status == 200 and isinstance(body, dict) and body.get("ready") is True:
                    return
                if isinstance(body, dict) and body.get("status") == "failed":
                    raise InferenceServerError(f"推論サーバーの準備に失敗しました: {body.get('error')}。ログ: {self.log_path}")
                last = f"準備中(status={status})"
            time.sleep(self.s.poll_interval)
        raise InferenceServerError(f"推論サーバーが {self.s.startup_timeout:g} 秒以内に準備完了しませんでした(最後の状態: {last})。ログ: {self.log_path}\n{self._log_tail()}")

    # ------------------------------------------------------------------ stop
    def stop(self):
        """自分が起動した子プロセスだけを停止する(正常停止 -> terminate -> kill の順)。"""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    _http(f"{self.base_url}/shutdown", data={"instance_id": self.instance_id}, timeout=3.0)
                except (urllib.error.URLError, OSError, ValueError):
                    pass
                try:
                    proc.wait(min(5.0, self.s.stop_timeout))
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(self.s.stop_timeout)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(5)
            self.returncode = proc.returncode
            self._log("info", f"推論サーバーを停止: 終了コード={self.returncode}")
        finally:
            self.proc = None
            if self._log_fh is not None:
                self._log_fh.close()
                self._log_fh = None
            if self._atexit is not None:
                atexit.unregister(self.stop)
                self._atexit = None
            self._remove_own_config()

    def _remove_own_config(self):
        # 強制終了で残った自分の server.yaml だけを消す(instance_id が一致する場合のみ)
        try:
            if f"instance_id: {self.instance_id}" in Path(self.config_out).read_text(encoding="utf-8"):
                os.remove(self.config_out)
        except OSError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def verify_shared_work_dir(base_url: str, work_dir: str, timeout: float = 10.0):
    """クライアントが work_dir に置いた目印ファイルがサーバーからも見えることを確認する(同じ場所を指す確認)。
    目印ファイルは自分が作ったものだけを、確認後に削除する。"""
    nonce = uuid.uuid4().hex
    probe = os.path.join(work_dir, f".probe_{nonce}")
    try:
        with open(probe, "w", encoding="ascii") as f:
            f.write("probe")
        try:
            status, body = _http(f"{base_url}/verify_workdir", data={"work_dir": os.path.abspath(work_dir), "nonce": nonce}, timeout=timeout)
        except (urllib.error.URLError, OSError) as e:
            raise InferenceServerError(f"work_dir の共有確認でサーバーに接続できません: {type(e).__name__}") from e
        if status != 200:
            msg = body.get("error") if isinstance(body, dict) else "(応答なし)"
            raise InferenceServerError(f"work_dir がサーバー側と同じ場所を指していません(HTTP {status}): {msg}")
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
