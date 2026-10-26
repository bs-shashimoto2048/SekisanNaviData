# 運用画面「1. 運用設定」の出力先の「参照…」: ローカルのフォルダ選択ダイアログ(このサーバーが動く PC の画面に表示)。
# ブラウザーはセキュリティ上、選んだフォルダの絶対パスを渡せないため、127.0.0.1 のサーバー側でダイアログを開く。
# 既存のフォルダを選ぶだけ(新しいフォルダの作成ボタンは出さない)。何も作成・変更しない。
# UNC・ネットワーク上の場所(ネットワークドライブを含む)は選択対象外。
# テストでは runner / drive_type を差し替える(実ダイアログ・実プロセスは起動しない)。
import ctypes
import os
import subprocess
import threading
from typing import Callable, Optional

from ops.settings_store import SettingsError, validate_output_root

DRIVE_REMOTE = 4
PICK_TIMEOUT_SECONDS = 300       # 操作されないまま放置されたときにサーバーの待ちを解く

# PowerShell(STA)の標準のフォルダ選択ダイアログ。新規フォルダ作成ボタンなし・「マイ コンピューター」起点(ネットワークの場所を出さない)。
# 選ばれたパスだけを標準出力(UTF-8)へ。キャンセルでは何も出さない。
_SCRIPT = (
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$o=New-Object System.Windows.Forms.Form;$o.TopMost=$true;$o.ShowInTaskbar=$false;"
    "$o.StartPosition='CenterScreen';$o.Size=New-Object System.Drawing.Size(0,0);"
    "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
    "$d.ShowNewFolderButton=$false;$d.RootFolder='MyComputer';"
    "$d.Description='出力先のフォルダを選んでください(既存のフォルダのみ)';"
    "if($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Out.Write($d.SelectedPath)}"
)


class PickerBusy(RuntimeError):
    """すでにフォルダ選択のダイアログが開いている。"""


def default_runner(args):
    return subprocess.run(args, capture_output=True, check=False, timeout=PICK_TIMEOUT_SECONDS)   # noqa: S603 - 引数は配列


def default_drive_type(root: str) -> int:
    return ctypes.windll.kernel32.GetDriveTypeW(root)


def _decode(raw) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return ""


def validate_picked(path: str, drive_type: Callable = default_drive_type) -> str:
    """選ばれたパスを、ローカルの既存フォルダの絶対パスとして検査して返す。不適格なら SettingsError(固定文言。パスは含めない)。"""
    text = (path or "").strip()
    if text.startswith("\\\\") or text.startswith("//"):
        raise SettingsError("ネットワーク上の場所は選べません。このPCのローカルのフォルダを選んでください。")
    text = validate_output_root(text)               # 絶対パス(ドライブ名つき)・「..」なし・使えない文字なし(既存の出力先検証)
    drive = os.path.splitdrive(text)[0]
    if drive_type(drive + "\\") == DRIVE_REMOTE:
        raise SettingsError("ネットワーク上の場所は選べません。このPCのローカルのフォルダを選んでください。")
    if not os.path.isdir(text):
        raise SettingsError("既存のフォルダを選んでください。")
    return os.path.normpath(text)


class FolderPicker:
    """フォルダ選択の実行(同時に 1 つだけ)。pick() は選ばれたパス、キャンセルなら None を返す。"""

    def __init__(self, runner: Callable = default_runner, drive_type: Callable = default_drive_type,
                 powershell: str = "powershell"):
        self._runner = runner
        self._drive_type = drive_type
        self._powershell = powershell
        self._lock = threading.Lock()

    def pick(self) -> Optional[str]:
        if not self._lock.acquire(blocking=False):
            raise PickerBusy("フォルダ選択の画面がすでに開いています。")
        try:
            try:
                res = self._runner([self._powershell, "-NoProfile", "-NonInteractive", "-STA", "-Command", _SCRIPT])
            except Exception as e:  # noqa: BLE001 - 理由は型名だけ(パス等を出さない)
                raise SettingsError(f"フォルダ選択の画面を開けませんでした({type(e).__name__})。") from e
            if getattr(res, "returncode", 1) != 0:
                raise SettingsError("フォルダ選択の画面を開けませんでした。")
            chosen = _decode(getattr(res, "stdout", b"")).strip()
            if not chosen:
                return None                          # キャンセル
            return validate_picked(chosen, self._drive_type)
        finally:
            self._lock.release()
