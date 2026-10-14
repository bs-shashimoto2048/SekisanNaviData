# 運用画面のローカル HTTP サーバー(標準ライブラリのみ)。127.0.0.1 だけで待ち受ける。
# 画面(static/index.html)と JSON API。状態を変える操作(PUT/POST/DELETE)は、ブラウザの別サイトからの送信を防ぐため
# カスタムヘッダ X-Ops-Request を要求し、Host ヘッダも 127.0.0.1 / localhost だけを受け付ける。
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from ops import checks as checks_mod
from ops.history import History
from ops.runner import Busy, RunManager
from ops.scheduler import SchedulerError, TaskScheduler
from ops.settings_store import SettingsError, SettingsStore

LOOPBACK = "127.0.0.1"
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 64 * 1024


@dataclass
class AppContext:
    store: SettingsStore
    history: History
    runs: RunManager
    scheduler: TaskScheduler
    config_path: str
    run_checks: Callable = checks_mod.run_checks


def make_handler(ctx: AppContext):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SekisanOps/1"

        def log_message(self, fmt, *args):      # アクセスログは標準エラーに出さない(パス・製番を残さない)
            pass

        # ---- 共通 ----
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, code: int, message: str):
            self._json(code, {"ok": False, "error": message})

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise SettingsError("送信内容が大きすぎます。")
            if n == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise SettingsError("送信内容の形式が正しくありません。")
            if not isinstance(data, dict):
                raise SettingsError("送信内容の形式が正しくありません。")
            return data

        def _guard(self, mutating: bool) -> bool:
            if not self._host_ok():
                self._error(403, "このホスト名ではアクセスできません。")
                return False
            if mutating and self.headers.get("X-Ops-Request") != "1":
                self._error(403, "不正なリクエストです。")
                return False
            return True

        # ---- ルーティング ----
        def do_GET(self):
            if not self._guard(False):
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, self._state(with_schedule="schedule=1" in self.path))
            elif path == "/api/history":
                self._json(200, {"runs": ctx.history.runs()})
            else:
                self._error(404, "見つかりません。")

        def do_PUT(self):
            if not self._guard(True):
                return
            if self.path.split("?", 1)[0] != "/api/settings":
                return self._error(404, "見つかりません。")
            self._handle(lambda: {"ok": True, "settings": ctx.store.save(self._body()).as_dict()})

        def do_POST(self):
            if not self._guard(True):
                return
            path = self.path.split("?", 1)[0]
            routes = {
                "/api/settings/reload": lambda: {"ok": True, "settings": ctx.store.load().as_dict()},
                "/api/checks": self._do_checks,
                "/api/run": self._do_run,
                "/api/schedule/register": self._do_register,
            }
            if path not in routes:
                return self._error(404, "見つかりません。")
            self._handle(routes[path])

        def do_DELETE(self):
            if not self._guard(True):
                return
            if self.path.split("?", 1)[0] != "/api/schedule":
                return self._error(404, "見つかりません。")
            self._handle(self._do_unregister)

        def _handle(self, fn):
            try:
                self._json(200, fn())
            except SettingsError as e:
                self._error(400, str(e))
            except Busy as e:
                self._error(409, str(e))
            except SchedulerError as e:
                self._error(502, str(e))
            except Exception as e:  # noqa: BLE001 - 画面には型名だけ(内部の詳細・パスは出さない)
                self._error(500, f"処理に失敗しました({type(e).__name__})。")

        # ---- 各操作 ----
        def _state(self, with_schedule: bool = False) -> dict:
            try:
                settings = ctx.store.load().as_dict()
                err = None
            except SettingsError as e:
                settings, err = {}, str(e)
            out = {"settings": settings, "settings_error": err, "status": ctx.runs.status()}
            if with_schedule:          # タスクの登録状態の照会(schtasks /Query)は、画面の表示時と操作後だけ行う(定期更新では呼ばない)
                try:
                    out["schedule"] = ctx.scheduler.status()
                except Exception:  # noqa: BLE001 - 照会に失敗しても画面は出す
                    out["schedule"] = {"registered": None, "enabled": None, "time": None}
            return out

        def _do_checks(self):
            settings = ctx.store.load()
            return {"ok": True, "results": ctx.run_checks(ctx.config_path, settings)}

        def _do_run(self):
            body = self._body()
            run_id, _ = ctx.runs.start_manual(body.get("product"), body.get("output_root"))
            return {"ok": True, "id": run_id}

        def _do_register(self):
            s = ctx.store.load()
            ctx.scheduler.register(s.schedule_time, s.schedule_enabled)
            return {"ok": True, "schedule": ctx.scheduler.status()}

        def _do_unregister(self):
            ctx.scheduler.delete()
            return {"ok": True, "schedule": ctx.scheduler.status()}

    return Handler


def make_server(ctx: AppContext, port: int = 8765, host: str = LOOPBACK) -> ThreadingHTTPServer:
    if host != LOOPBACK:
        raise ValueError("運用画面は 127.0.0.1 だけで待ち受けます。")
    return ThreadingHTTPServer((host, port), make_handler(ctx))
