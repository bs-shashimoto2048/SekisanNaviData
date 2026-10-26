# 運用画面のローカル HTTP サーバー(標準ライブラリのみ)。127.0.0.1 だけで待ち受ける。
# 画面(static/index.html)と JSON API。状態を変える操作(PUT/POST/DELETE)は、ブラウザの別サイトからの送信を防ぐため
# カスタムヘッダ X-Ops-Request を要求し、Host ヘッダも 127.0.0.1 / localhost だけを受け付ける。
import json
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from ops import checks as checks_mod
from ops.folder_picker import FolderPicker, PickerBusy
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
    picker: FolderPicker = field(default_factory=FolderPicker)


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

        def _slurp(self):
            """リクエスト本文を最初に読み切る(拒否の応答を返す前に読まないと、送信側が接続の中断を受け取ることがある)。"""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            self._too_big = n > MAX_BODY
            self._raw = b"" if self._too_big or n <= 0 else self.rfile.read(n)
            if self._too_big:
                self.close_connection = True      # 大きすぎる本文は読まずに、応答後に接続を閉じる

        def _body(self) -> dict:
            if getattr(self, "_too_big", False):
                raise SettingsError("送信内容が大きすぎます。")
            raw = getattr(self, "_raw", b"")
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
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
            self._slurp()
            if not self._guard(True):
                return
            if self.path.split("?", 1)[0] != "/api/settings":
                return self._error(404, "見つかりません。")
            self._handle(lambda: {"ok": True, "settings": ctx.store.save(self._body()).as_dict()})

        def do_POST(self):
            self._slurp()
            if not self._guard(True):
                return
            path = self.path.split("?", 1)[0]
            routes = {
                "/api/settings/reload": lambda: {"ok": True, "settings": ctx.store.load().as_dict()},
                "/api/settings/browse-folder": self._do_browse,
                "/api/checks": self._do_checks,
                "/api/run": self._do_run,
                "/api/schedule/register": self._do_register,
            }
            if path not in routes:
                return self._error(404, "見つかりません。")
            self._handle(routes[path])

        def do_DELETE(self):
            self._slurp()
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
            except (Busy, PickerBusy) as e:
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
                    out["schedule"] = {"registered": None, "enabled": None, "time": None, "interval_days": None}
            return out

        def _do_browse(self):
            """出力先の「参照…」。この PC の画面にフォルダ選択を出し、選ばれたローカルの既存フォルダの絶対パスを返す
            (保存はしない。キャンセルは path=None)。ローカル画面からの要求(ループバックの Host とカスタムヘッダ)だけ受け付ける。"""
            path = ctx.picker.pick()
            return {"ok": True, "path": path, "cancelled": path is None}

        def _do_checks(self):
            settings = ctx.store.load()
            return {"ok": True, "results": ctx.run_checks(ctx.config_path, settings)}

        def _do_run(self):
            body = self._body()
            run_id, _ = ctx.runs.start_manual(body.get("product"), body.get("output_root"))
            return {"ok": True, "id": run_id}

        def _do_register(self):
            s = ctx.store.load()
            ctx.scheduler.register(s.schedule_time, s.schedule_interval_days, s.schedule_enabled)
            return {"ok": True, "schedule": ctx.scheduler.status()}

        def _do_unregister(self):
            ctx.scheduler.delete()
            return {"ok": True, "schedule": ctx.scheduler.status()}

    return Handler


def make_server(ctx: AppContext, port: int = 8765, host: str = LOOPBACK) -> ThreadingHTTPServer:
    if host != LOOPBACK:
        raise ValueError("運用画面は 127.0.0.1 だけで待ち受けます。")
    return ThreadingHTTPServer((host, port), make_handler(ctx))
