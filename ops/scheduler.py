# Windows タスクスケジューラ連携(運用画面の「3. 定期実行」)。schtasks.exe を呼ぶ。
# 登録・更新・解除は、画面での明示操作(TaskScheduler の register / delete)からだけ行う。status は読み取り(照会)だけ。
# テストでは runner(subprocess.run の代わり)を差し替える。実タスクの登録・変更・削除は行わない。
#
# 登録するタスクは「既存の通常処理を指定時刻に起動する」だけ:
#   <python> <ops_console.py> run-scheduled   -> app.main() をそのまま実行(製番検索・処理済み判定・実行間隔は変えない)
# 毎日、指定時刻に 1 回起動する。製番の指定はしない。
import re
import subprocess
from typing import Callable, Optional

DEFAULT_TASK_NAME = "SekisanNaviData_Ops"


class SchedulerError(RuntimeError):
    """schtasks の失敗。メッセージは画面に出す(パスは含めない)。"""


def _decode(raw) -> str:
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "utf-16", "cp932"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
    return ""


def default_runner(args):
    return subprocess.run(args, capture_output=True, check=False)   # noqa: S603 - 引数は配列で渡す(シェルを通さない)


class TaskScheduler:
    def __init__(self, python_exe: str, script_path: str, task_name: str = DEFAULT_TASK_NAME,
                 runner: Callable = default_runner, extra_args: tuple = ()):
        self.python_exe = python_exe
        self.script_path = script_path
        self.task_name = task_name
        self.runner = runner
        self.extra_args = tuple(extra_args)

    # ---- コマンドの組み立て(テストで内容を検証できるよう公開) ----
    def action(self) -> str:
        parts = [f'"{self.python_exe}"', f'"{self.script_path}"', "run-scheduled", *self.extra_args]
        return " ".join(parts)

    def create_args(self, hhmm: str) -> list:
        return ["schtasks", "/Create", "/TN", self.task_name, "/TR", self.action(), "/SC", "DAILY", "/ST", hhmm, "/F"]

    def change_args(self, enabled: bool) -> list:
        return ["schtasks", "/Change", "/TN", self.task_name, "/ENABLE" if enabled else "/DISABLE"]

    def delete_args(self) -> list:
        return ["schtasks", "/Delete", "/TN", self.task_name, "/F"]

    def query_args(self) -> list:
        return ["schtasks", "/Query", "/TN", self.task_name, "/XML"]

    # ---- 操作 ----
    def _run(self, args, what: str):
        res = self.runner(args)
        if res.returncode != 0:
            detail = " ".join(_decode(res.stderr or b"").split())[:120]
            raise SchedulerError(f"タスクの{what}に失敗しました(終了コード {res.returncode}){': ' + detail if detail else ''}")
        return res

    def register(self, hhmm: str, enabled: bool):
        """タスクを登録(既にあれば更新)し、有効/無効を反映する。"""
        self._run(self.create_args(hhmm), "登録・更新")
        self._run(self.change_args(enabled), "有効/無効の設定")

    def delete(self):
        self._run(self.delete_args(), "解除")

    def status(self) -> dict:
        """登録状態の照会(読み取りのみ)。{registered, enabled, time}。"""
        res = self.runner(self.query_args())
        if res.returncode != 0:
            return {"registered": False, "enabled": None, "time": None}
        xml = _decode(res.stdout)
        enabled = None
        m = re.search(r"<Enabled>\s*(true|false)\s*</Enabled>", xml, re.I)
        if m:
            enabled = m.group(1).lower() == "true"
        t: Optional[str] = None
        m = re.search(r"<StartBoundary>[^T<]*T(\d\d:\d\d)", xml)
        if m:
            t = m.group(1)
        return {"registered": True, "enabled": enabled, "time": t}
