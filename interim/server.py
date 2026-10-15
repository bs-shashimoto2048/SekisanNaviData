# 暫定 API 操作用 UI のローカル HTTP サーバー(標準ライブラリのみ)。127.0.0.1 だけで待ち受ける(他のアドレスは指定できない)。
# 画面(static/index.html)と JSON API。状態を変える操作(POST)は、別サイトからの送信を防ぐためカスタムヘッダ X-Interim-Request を要求し、
# Host ヘッダも 127.0.0.1 / localhost だけを受け付ける。応答は、状態・HTTP 状態・ポート・検出件数・固定文言だけ(パス・値・例外メッセージは出さない)。
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from interim.manager import MESSAGES, ApiManager, Busy, Refused
from interim.runs import MESSAGES as RUN_MESSAGES, InterimRuns, IsolationError, RunRefused, TaskForeignError
from ops.scheduler import SchedulerError

LOOPBACK = "127.0.0.1"
STATIC_DIR = Path(__file__).resolve().parent / "static"
HEADER = "X-Interim-Request"


def make_handler(manager: ApiManager, runs: "InterimRuns | None" = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "InterimConsole/1"

        def log_message(self, fmt, *args):          # アクセスログは出さない
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost")

        def _drain(self):
            """リクエスト本文を最初に読み切る(小さいものだけ。大きいものは読まずに、応答後に接続を閉じる)。"""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            self._raw = self.rfile.read(n) if 0 < n <= 4096 else b""
            if n > 4096:
                self.close_connection = True

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "Host が許可されていません。"})
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(200, {"ok": True, **manager.snapshot()})
            if runs is not None and path in ("/api/run/target", "/api/runs", "/api/schedule"):
                return self._runs_get(path)
            return self._json(404, {"ok": False, "error": "見つかりません。"})

        def _query(self) -> dict:
            from urllib.parse import parse_qs, urlsplit
            return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items() if v}

        def _fixed(self, code: int, key: str, extra=None):
            self._json(code, {"ok": False, "code": key, "error": RUN_MESSAGES.get(key, "受け付けられません。"), **(extra or {})})

        def _runs_get(self, path):
            try:
                if path == "/api/run/target":
                    return self._json(200, {"ok": True, **runs.target_for(self._query().get("product"))})
                if path == "/api/runs":
                    return self._json(200, {"ok": True, **runs.state(self._query().get("product"))})
                return self._json(200, {"ok": True, **runs.schedule_state()})
            except RunRefused as e:
                return self._fixed(400 if e.code in ("bad_product", "bad_schedule") else 409, e.code)
            except IsolationError:
                return self._fixed(409, "isolation")
            except Exception:  # noqa: BLE001 - 例外メッセージ(パス・接続先を含み得る)は返さない
                return self._json(500, {"ok": False, "error": "処理に失敗しました。"})

        def _origin_ok(self) -> bool:
            """Origin ヘッダーがあるときは、この画面自身(127.0.0.1 / localhost)のものだけ受け付ける(別サイトからの送信の多重防御)。"""
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            from urllib.parse import urlsplit
            try:
                return urlsplit(origin).hostname in ("127.0.0.1", "localhost")
            except ValueError:
                return False

        def do_POST(self):
            self._drain()
            if not self._host_ok() or not self._origin_ok():
                return self._json(403, {"ok": False, "error": "Host が許可されていません。"})
            if self.headers.get(HEADER) != "1":
                return self._json(403, {"ok": False, "error": "この操作は画面からのみ受け付けます。"})
            route = self.path.split("?", 1)[0]
            if runs is not None and route in ("/api/run", "/api/schedule/register", "/api/schedule/unregister"):
                return self._runs_post(route)
            if route == "/api/stop" and runs is not None and runs.running():
                return self._json(409, {"ok": False, "code": "run_running", "error": "製番処理の実行中は、API を停止できません。", **manager.snapshot()})
            action = {"/api/start": manager.start, "/api/stop": manager.stop, "/api/smoke": manager.smoke}.get(route)
            if action is None:
                return self._json(404, {"ok": False, "error": "見つかりません。"})
            try:
                snap = action()
            except Busy as e:
                return self._json(409, {"ok": False, "code": e.code, "error": MESSAGES.get(e.code, "処理中です。"), **manager.snapshot()})
            except Refused as e:
                return self._json(409, {"ok": False, "code": e.code, "error": MESSAGES.get(e.code, "受け付けられません。"), **manager.snapshot()})
            except Exception:  # noqa: BLE001 - 例外メッセージ(パス・接続先を含み得る)は返さない
                return self._json(500, {"ok": False, "error": "処理に失敗しました。", **manager.snapshot()})
            return self._json(200, {"ok": True, **snap})

        def _body(self) -> dict:
            raw = getattr(self, "_raw", b"")
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        def _runs_post(self, route):
            body = self._body()
            try:
                if route == "/api/run":
                    return self._json(200, {"ok": True, **runs.start(body.get("product"))})
                if route == "/api/schedule/register":
                    return self._json(200, {"ok": True, **runs.schedule_register(body.get("time"), body.get("interval_days"))})
                return self._json(200, {"ok": True, **runs.schedule_unregister()})
            except RunRefused as e:
                return self._fixed(400 if e.code in ("bad_product", "bad_schedule") else 409, e.code)
            except IsolationError:
                return self._fixed(409, "isolation")
            except TaskForeignError:
                return self._fixed(409, "task_foreign")
            except SchedulerError:                                              # schtasks の失敗(メッセージは出さない)
                return self._json(500, {"ok": False, "error": "タスクスケジューラの操作に失敗しました。"})
            except Exception:  # noqa: BLE001
                return self._json(500, {"ok": False, "error": "処理に失敗しました。"})

        def do_PUT(self):
            self._json(405, {"ok": False, "error": "許可されていません。"})

        do_DELETE = do_PUT

    return Handler


def make_server(manager: ApiManager, port: int, host: str = LOOPBACK, runs: "InterimRuns | None" = None) -> ThreadingHTTPServer:
    if host != LOOPBACK:
        raise ValueError("待ち受けは 127.0.0.1 だけです。")
    return ThreadingHTTPServer((host, port), make_handler(manager, runs))
