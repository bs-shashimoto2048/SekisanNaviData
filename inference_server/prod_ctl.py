"""本番推論 API の起動・停止・状態確認(ログオン後の自動起動と手動操作のための補助)。**Windows サービスではない**(前景プロセスを、非表示のコンソールで起動する)。

    python prod_ctl.py start  [--config prod_server.local.yaml]   二重起動せずに起動し、待受アドレス・ポート・/health・モデル識別を確認する
    python prod_ctl.py stop   [--config ...]                      記録した PID(起動時刻まで一致するもの)だけを、正常停止(Ctrl+Break)→ 猶予後に強制終了する
    python prod_ctl.py status [--config ...]

- 二重起動の防止: 起動の排他ロック(`run/prod_api.start.lock`)・PID 記録(`run/prod_api.pid`: PID + プロセス作成時刻)・待受ポートの使用確認の 3 段。
- 停止は、記録した PID が「同じプロセス(作成時刻が一致し、コマンドラインに prod_server.py と serve が含まれる)」のときだけ。名前(python.exe)での一括終了はしない。
- 失敗の説明は固定文。パス・認証情報・例外メッセージは出さない(URL は、待受を確認する目的で表示する)。
- 起動しても、共有 server.yaml の公開(--publish-config)はしない。公開は別の明示操作。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import prod_config

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "prod_server.local.yaml"
RUN_DIR = HERE / "run"
PID_FILE = RUN_DIR / "prod_api.pid"
LOCK_FILE = RUN_DIR / "prod_api.start.lock"
LAST_FILE = RUN_DIR / "last_start.txt"
START_TIMEOUT_SEC = 300           # モデルの検証・読み込み(共有からのコピーを含む)を待つ上限
STOP_EXTRA_SEC = 20               # 停止の猶予(shutdown_grace_sec)に足す待ち時間。超えたら、その PID だけを強制終了する
IDENTITY_SLACK_SEC = 2.0          # 作成時刻の一致の許容

EXIT_OK, EXIT_FAIL, EXIT_CONFIG, EXIT_BUSY_START, EXIT_PORT, EXIT_ADDRESS, EXIT_EXITED, EXIT_TIMEOUT, EXIT_MISMATCH = 0, 1, 2, 3, 4, 5, 6, 7, 8
MESSAGES = {
    EXIT_CONFIG: "設定が不完全または不正なため起動しませんでした(`python prod_server.py check --config prod_server.local.yaml` で確認してください)。",
    EXIT_BUSY_START: "別の起動処理が進行中です。完了を待ってから、もう一度実行してください(二重起動はしません)。",
    EXIT_PORT: "設定のポートを、別のプロセスが使用しています。API は起動していません(別のプロセスは停止していません)。",
    EXIT_ADDRESS: "設定の待受アドレスが、この PC に割り当てられていません。API は起動していません。",
    EXIT_EXITED: "API のプロセスが、準備完了の前に終了しました。ログ(logging.file)とエラー出力を確認してください。",
    EXIT_TIMEOUT: "API が時間内に準備完了(/health が正常)になりませんでした。起動したプロセスは停止しました。",
    EXIT_MISMATCH: "API の応答(待受または モデル識別)が、設定と一致しません。起動したプロセスは停止しました。",
}
PROCESS_EXIT_REASONS = {2: "設定の不備", 3: "ポートが使用中", 4: "モデルの検証に失敗", 5: "接続設定の公開に失敗", 6: "モデルの読み込みに失敗", 7: "サーバーのループの失敗"}


class Controller:
    """依存(プロセス情報・HTTP・起動・停止信号・時計)を差し替えられる制御部(テスト用)。"""

    def __init__(self, config_path=DEFAULT_CONFIG, *, run_dir=RUN_DIR, psutil_mod=None, http_get=None, spawn=None, send_break=None,
                 sleep=time.sleep, clock=time.monotonic, out=print):
        self.config_path = Path(os.path.abspath(os.fspath(config_path)))
        self.run_dir = Path(run_dir)
        self.pid_file, self.lock_file, self.last_file = self.run_dir / "prod_api.pid", self.run_dir / "prod_api.start.lock", self.run_dir / "last_start.txt"
        if psutil_mod is None:
            import psutil as psutil_mod
        self.ps = psutil_mod
        self._http_get, self._spawn, self._send_break = http_get or self._default_get, spawn or self._default_spawn, send_break or self._default_break
        self.sleep, self.clock, self.out = sleep, clock, out

    # ---- 既定の実装(Windows)
    @staticmethod
    def _default_get(url, timeout=3.0):
        import requests
        r = requests.get(url, timeout=timeout, proxies={"http": None, "https": None}, allow_redirects=False)
        try:
            body = r.json()
        except ValueError:
            body = {}
        return r.status_code, body

    def _default_spawn(self, cfg_path, extra_args=()):
        flags = 0
        startup = None
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP    # 非表示の専用コンソール(停止時に Ctrl+Break を送るため)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0                                                       # SW_HIDE
        p = subprocess.Popen([sys.executable, "-X", "utf8", str(HERE / "prod_server.py"), "serve", "--config", str(cfg_path), *extra_args], cwd=str(HERE),
                             creationflags=flags, startupinfo=startup, stdin=subprocess.DEVNULL)
        return p

    @staticmethod
    def _default_break(pid) -> bool:
        """対象プロセスのコンソールへ Ctrl+Break を送る(このプロセスは、その間だけ対象のコンソールに接続する。無視設定にして巻き込まれない)。"""
        if os.name != "nt":
            os.kill(pid, signal.SIGTERM)
            return True
        import ctypes
        k = ctypes.windll.kernel32
        k.FreeConsole()
        try:
            if not k.AttachConsole(pid):
                return False
            k.SetConsoleCtrlHandler(None, True)
            ok = bool(k.GenerateConsoleCtrlEvent(1, pid))                                # CTRL_BREAK_EVENT、プロセスグループ = 対象 PID
            return ok
        finally:
            k.FreeConsole()

    # ---- 共通
    def say(self, msg):
        self.out(msg)

    def load(self):
        try:
            return prod_config.load_config(str(self.config_path))
        except prod_config.ConfigError as e:
            self.say(MESSAGES[EXIT_CONFIG])
            self.say(f"  ({e})")
            return None

    @staticmethod
    def url(cfg) -> str:
        return f"http://{cfg.host}:{cfg.port}"

    def health(self, cfg):
        try:
            return self._http_get(self.url(cfg) + "/health", 3.0)
        except Exception:  # noqa: BLE001 - 接続できない・タイムアウト = まだ応答しない
            return None, {}

    def _record(self):
        try:
            data = json.loads(self.pid_file.read_text(encoding="utf-8"))
            return int(data["pid"]), float(data["create_time"])
        except Exception:  # noqa: BLE001
            return None

    def is_our_process(self, pid, create_time) -> bool:
        """記録した PID が、起動したときと同じプロセス(作成時刻が一致し、コマンドラインに prod_server.py と serve を含む)か。"""
        try:
            p = self.ps.Process(pid)
            if abs(p.create_time() - create_time) > IDENTITY_SLACK_SEC:
                return False
            cmd = " ".join(p.cmdline())
            return "prod_server.py" in cmd and " serve" in cmd
        except Exception:  # noqa: BLE001 - 存在しない・権限が無い = 自分のプロセスと確認できない
            return False

    def _write_pid(self, pid):
        create_time = self.ps.Process(pid).create_time()
        self.pid_file.write_text(json.dumps({"pid": pid, "create_time": create_time, "config": self.config_path.name}), encoding="utf-8")
        return create_time

    def _listening_on(self, cfg):
        """設定のポートで待ち受けているプロセスの PID(無ければ None)。待受アドレスが設定のアドレスまたはワイルドカードのものだけ。"""
        try:
            conns = self.ps.net_connections(kind="tcp")
        except Exception:  # noqa: BLE001
            return None
        for c in conns:
            if c.status == "LISTEN" and c.laddr and c.laddr.port == cfg.port and c.laddr.ip in (cfg.host, "0.0.0.0", "::", ""):
                return c.pid if c.pid is not None else -1
        return None

    def _address_on_this_pc(self, cfg) -> bool:
        try:
            for addrs in self.ps.net_if_addrs().values():
                if any(a.family == socket.AF_INET and a.address == cfg.host for a in addrs):
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    # ---- 排他ロック(二重起動の防止)
    def _acquire_lock(self) -> bool:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    holder = int(self.lock_file.read_text(encoding="utf-8").strip() or "0")
                except (OSError, ValueError):
                    holder = 0
                if holder and holder != os.getpid() and self.ps.pid_exists(holder):
                    try:
                        if "prod_ctl.py" in " ".join(self.ps.Process(holder).cmdline()):
                            return False                                       # 生きている別の起動処理がある
                    except Exception:  # noqa: BLE001
                        return False
                try:
                    self.lock_file.unlink()                                   # 持ち主がいない古いロック
                except OSError:
                    return False
        return False

    def _release_lock(self):
        try:
            if self.lock_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.lock_file.unlink()
        except OSError:
            pass

    # ---- start
    def start(self, publish_config: bool = False, replace_stale: bool = False) -> int:
        """`publish_config` / `replace_stale` は、利用者が明示したときだけ(既定は公開しない。スタートアップのバッチは指定しない)。prod_server.py serve へそのまま渡す。"""
        cfg = self.load()
        if cfg is None:
            return EXIT_CONFIG
        if not self._acquire_lock():
            self.say(MESSAGES[EXIT_BUSY_START])
            return EXIT_BUSY_START
        try:
            return self._start_locked(cfg, publish_config, replace_stale)
        finally:
            self._release_lock()

    def _start_locked(self, cfg, publish_config: bool = False, replace_stale: bool = False) -> int:
        rec = self._record()
        if rec and self.is_our_process(*rec):                                    # すでに起動している(二重起動しない)
            if publish_config:
                self.say("API はすでに起動しているため、接続設定の公開(--publish-config)は行いません。いったん停止してから、もう一度実行してください。")
            status, body = self.health(cfg)
            if status == 200 and body.get("ready"):
                self.say(f"API はすでに起動しています(PID {rec[0]}): {self.url(cfg)}  health=ok  モデル={body.get('model', {}).get('weights_sha256_12', '?')}")
                return EXIT_OK
            self.say("API のプロセスは起動中です。準備完了を待ちます…")
            return self._wait_ready(cfg, proc=None, pid=rec[0], create_time=rec[1])
        if rec:                                                                  # 記録のプロセスはもう無い(古い記録)
            try:
                self.pid_file.unlink()
            except OSError:
                pass
        if not self._address_on_this_pc(cfg):
            self.say(MESSAGES[EXIT_ADDRESS])
            return EXIT_ADDRESS
        if self._listening_on(cfg) is not None:
            self.say(MESSAGES[EXIT_PORT])
            return EXIT_PORT
        extra = (["--publish-config"] if publish_config else []) + (["--replace-stale"] if replace_stale else [])
        if extra:
            self.say("接続設定の公開を明示して起動します: " + " ".join(extra) + "(準備完了のあとに、共有 server.yaml を、バックアップを作ってから更新します)")
        proc = self._spawn(self.config_path, extra) if extra else self._spawn(self.config_path)
        try:
            create_time = self._write_pid(proc.pid)
        except Exception:  # noqa: BLE001
            self._force(proc.pid)
            self.say(MESSAGES[EXIT_EXITED])
            return EXIT_EXITED
        self.say(f"API を起動しました(PID {proc.pid})。モデルの検証と読み込みを待ちます(数十秒〜数分)…")
        return self._wait_ready(cfg, proc=proc, pid=proc.pid, create_time=create_time)

    def _wait_ready(self, cfg, *, proc, pid, create_time) -> int:
        deadline = self.clock() + START_TIMEOUT_SEC
        while self.clock() < deadline:
            if proc is not None:
                rc = proc.poll()
                if rc is not None:
                    self._clear_pid()
                    self.say(MESSAGES[EXIT_EXITED] + f"(終了コード {rc}: {PROCESS_EXIT_REASONS.get(rc, '不明')})")
                    return EXIT_EXITED
            elif not self.is_our_process(pid, create_time):
                self._clear_pid()
                self.say(MESSAGES[EXIT_EXITED])
                return EXIT_EXITED
            status, body = self.health(cfg)
            if status == 200 and body.get("ready"):
                return self._verify(cfg, pid, create_time, body)
            if status == 500:                                                    # 読み込みの失敗(failed)。待たない
                self._stop_pid(pid, create_time, cfg)
                self.say(MESSAGES[EXIT_EXITED])
                return EXIT_EXITED
            self.sleep(1.0)
        self._stop_pid(pid, create_time, cfg)
        self.say(MESSAGES[EXIT_TIMEOUT])
        return EXIT_TIMEOUT

    def _verify(self, cfg, pid, create_time, body) -> int:
        model = body.get("model") or {}
        want = (cfg.model.id, cfg.model.weights_sha256[:12], cfg.model.yaml_sha256[:12], cfg.model.weights_size)
        got = (model.get("id"), model.get("weights_sha256_12"), model.get("yaml_sha256_12"), model.get("weights_size"))
        listen_pid = self._listening_on(cfg)
        listening_ok = listen_pid is not None and (listen_pid == -1 or listen_pid in self._tree(pid))        # 待受は、起動したプロセス(venv の起動ランチャーの子を含む)のもの
        if got != want or not listening_ok or body.get("status") != "ok" or body.get("mode") != "production":
            self._stop_pid(pid, create_time, cfg)
            self.say(MESSAGES[EXIT_MISMATCH])
            return EXIT_MISMATCH
        self.last_file.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} OK pid={pid} url={self.url(cfg)}\n", encoding="utf-8")
        self.say(f"API の起動を確認しました: {self.url(cfg)}")
        self.say(f"  PID={pid} / 待受=設定と一致 / health=ok / モデル={got[0]}(重み先頭12桁 {got[1]})= 設定と一致")
        return EXIT_OK

    # ---- stop
    def _clear_pid(self):
        try:
            self.pid_file.unlink()
        except OSError:
            pass

    def _tree(self, pid) -> list:
        """PID と、その子孫(Windows の venv の python.exe は、実体のインタープリターを子として起動する)。"""
        try:
            return [pid] + [c.pid for c in self.ps.Process(pid).children(recursive=True)]
        except Exception:  # noqa: BLE001
            return [pid]

    def _force(self, pid):
        """この PID と、その子孫だけを強制終了する(ほかのプロセスには触れない)。"""
        for target in reversed(self._tree(pid)):
            try:
                self.ps.Process(target).kill()
            except Exception:  # noqa: BLE001
                pass

    def _stop_pid(self, pid, create_time, cfg) -> bool:
        """検証済みの PID だけを、正常停止 → 猶予後に強制終了する。停止できたら True。"""
        if not self.is_our_process(pid, create_time):
            return False
        children = self._tree(pid)[1:]
        try:
            self._send_break(pid)
        except Exception:  # noqa: BLE001
            pass
        deadline = self.clock() + cfg.limits.shutdown_grace_sec + STOP_EXTRA_SEC
        while self.clock() < deadline:
            if not self.ps.pid_exists(pid) or not self.is_our_process(pid, create_time):
                self._clear_pid()
                return not any(self.ps.pid_exists(c) for c in children)                # 子(実体のインタープリター)も終了していること
            self.sleep(0.5)
        self._force(pid)                                                         # その PID と子孫だけ(他のプロセスには触れない)
        self.sleep(1.0)
        self._clear_pid()
        return not any(self.ps.pid_exists(x) for x in [pid] + children)

    def stop(self) -> int:
        cfg = self.load()
        if cfg is None:
            return EXIT_CONFIG
        rec = self._record()
        if not rec:
            self.say("停止対象はありません(起動の記録がありません)。")
            return EXIT_OK
        if not self.is_our_process(*rec):
            self._clear_pid()
            self.say("記録された PID は、すでに終了している、または別のプロセスのため、何も停止しませんでした(古い記録を消しました)。")
            return EXIT_OK
        pid = rec[0]
        self.say(f"API(PID {pid})を停止します。実行中の推論の完了を、最大 {cfg.limits.shutdown_grace_sec} 秒待ちます…")
        ok = self._stop_pid(pid, rec[1], cfg)
        self.say("停止しました。共有 server.yaml は変更していません。" if ok else "停止を確認できませんでした。")
        return EXIT_OK if ok else EXIT_FAIL

    # ---- status
    def status(self) -> int:
        cfg = self.load()
        if cfg is None:
            return EXIT_CONFIG
        rec = self._record()
        running = bool(rec and self.is_our_process(*rec))
        status, body = self.health(cfg) if running else (None, {})
        self.say(f"URL: {self.url(cfg)}")
        self.say(f"プロセス: {'起動中 PID ' + str(rec[0]) if running else '起動していません'}")
        if running:
            self.say(f"health: {status} {body.get('status', '?')} ready={body.get('ready')} モデル={body.get('model', {}).get('weights_sha256_12', '?')}")
        return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本番推論 API の起動・停止・状態確認")
    ap.add_argument("action", choices=("start", "stop", "status"))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--publish-config", action="store_true", help="start のとき、準備完了後に共有 server.yaml を更新する(明示したときだけ。既定は公開しない)")
    ap.add_argument("--replace-stale", action="store_true", help="--publish-config のとき、既存の接続先が接続を明示的に拒否する(古い設定)場合に置き換えを許可する")
    args = ap.parse_args(argv)
    if args.action != "start" and (args.publish_config or args.replace_stale):
        ap.error("--publish-config / --replace-stale は start のときだけ指定できます")
    if args.replace_stale and not args.publish_config:
        ap.error("--replace-stale は --publish-config と一緒に指定してください")
    c = Controller(args.config)
    if args.action == "start":
        return c.start(publish_config=args.publish_config, replace_stale=args.replace_stale)
    return {"stop": c.stop, "status": c.status}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
