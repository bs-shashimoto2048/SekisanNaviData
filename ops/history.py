# 実行履歴(運用画面の「5. 実行履歴」)。Git 管理外の `ops_history.local.jsonl` に 1 行 1 イベントで追記する。
# 記録するのは 日時・種別・製番・状態・理由 だけ。社内パス・秘密情報・材料値は記録しない(理由は整形して載せる)。
# 同じ id のイベントは後のものが前を上書きする(実行中 → 完了/失敗)。
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

HISTORY_FILE = "ops_history.local.jsonl"

RUNNING, COMPLETED, FAILED = "実行中", "完了", "失敗"
KIND_MANUAL, KIND_SCHEDULED = "手動", "定期"
MAX_REASON = 200

_WIN_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|,;]*")           # C:\… / \\server\share\…
_POSIX_PATH = re.compile(r"(?<![\w.])/(?:[\w.\-]+/)+[\w.\-]*")              # /a/b/c
_SECRET = re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*\S+")


def sanitize_reason(text, limit: int = MAX_REASON) -> str:
    """履歴・画面に出す理由。パスと秘密情報らしい値を伏せ、1 行にして切り詰める。"""
    s = "" if text is None else str(text)
    s = _SECRET.sub(lambda m: f"{m.group(1)}=<伏せ字>", s)
    s = _WIN_PATH.sub("<パス>", s)
    s = _POSIX_PATH.sub("<パス>", s)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


class History:
    def __init__(self, state_dir, now=None):
        self.path = Path(state_dir) / HISTORY_FILE
        self._lock = threading.Lock()
        self._now = now or (lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._seq = 0

    def _append(self, event: dict):
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8", newline="") as f:
                f.write(line + "\n")

    def start(self, kind: str, product: str) -> str:
        """実行の開始を記録して id を返す。"""
        with self._lock:
            self._seq += 1
            run_id = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{os.getpid()}-{self._seq}"
        self._append({"id": run_id, "started": self._now(), "kind": kind, "product": product, "status": RUNNING, "reason": ""})
        return run_id

    def finish(self, run_id: str, status: str, reason: str = ""):
        started = next((e["started"] for e in self.events() if e["id"] == run_id), self._now())
        self._append({"id": run_id, "started": started, "finished": self._now(), "status": status,
                      "reason": sanitize_reason(reason)})

    def events(self) -> List[dict]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue            # 壊れた行は読み飛ばす(履歴の破損で画面を止めない)
        return out

    def runs(self, limit: Optional[int] = 200) -> List[dict]:
        """実行ごとにまとめた履歴(新しい順)。同じ id は後のイベントで上書きする。"""
        merged = {}
        for e in self.events():
            cur = merged.setdefault(e["id"], {})
            cur.update(e)
        rows = sorted(merged.values(), key=lambda r: (r.get("started", ""), r["id"]), reverse=True)
        return rows if limit is None else rows[:limit]

    def mark_orphans_failed(self, active_id: Optional[str] = None) -> int:
        """起動時の後始末: 「実行中」のまま残った記録(プロセス終了などで終わりが書かれなかったもの)を失敗にする。"""
        n = 0
        for r in self.runs(limit=None):
            if r.get("status") == RUNNING and r["id"] != active_id:
                self.finish(r["id"], FAILED, "処理が途中で終了しました(プロセスの停止など)。完了とは扱いません。")
                n += 1
        return n
