# 運用設定の保存・再読込(運用画面の「1. 運用設定」「3. 定期実行」)。
# 保存先は Git 管理外のローカル設定 `ops.local.yaml`(既存の `*.local.yaml` の方針)。追跡対象の app.yaml は読み書きしない。
# 保存するのは、出力先ルート・定期実行の有効/無効と開始時刻だけ。秘密情報は持たない。
import os
import re
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

import yaml

SETTINGS_FILE = "ops.local.yaml"
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsError(ValueError):
    """入力値が不正。メッセージは画面にそのまま表示する(日本語)。"""


@dataclass(frozen=True)
class OpsSettings:
    output_root: str = ""             # 出力先ルート(app の設定 data.output の代わりに使う。空なら app の設定どおり)
    schedule_enabled: bool = False    # 定期実行(タスクスケジューラ)の有効/無効
    schedule_time: str = "08:00"      # 定期実行の開始時刻(HH:MM、24 時間表記)

    def as_dict(self) -> dict:
        return asdict(self)


def validate_output_root(value) -> str:
    text = "" if value is None else str(value).strip()
    if text == "":
        return ""
    if "<" in text or ">" in text:
        raise SettingsError("出力先にサンプルの記号(< >)が残っています。実際のフォルダを指定してください。")
    if "\n" in text or "\r" in text or "\x00" in text:
        raise SettingsError("出力先に使えない文字が含まれています。")
    if not os.path.isabs(text):
        raise SettingsError("出力先は絶対パスで指定してください。")
    return text


def validate_time(value) -> str:
    text = "" if value is None else str(value).strip()
    if not TIME_PATTERN.match(text):
        raise SettingsError("開始時刻は 24 時間表記の HH:MM(例: 08:30)で指定してください。")
    return text


def validate_product_no(value) -> str:
    text = "" if value is None else str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9]{4,20}", text):
        raise SettingsError("製番は英数字 4〜20 文字で指定してください。")
    return text


def validate(data: dict) -> OpsSettings:
    """画面から受け取った辞書を検証して OpsSettings にする。不正なら SettingsError。"""
    if not isinstance(data, dict):
        raise SettingsError("設定の形式が正しくありません。")
    enabled = data.get("schedule_enabled", False)
    if not isinstance(enabled, bool):
        raise SettingsError("定期実行の有効/無効は真偽値で指定してください。")
    return OpsSettings(output_root=validate_output_root(data.get("output_root", "")),
                       schedule_enabled=enabled,
                       schedule_time=validate_time(data.get("schedule_time", "08:00")))


class SettingsStore:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / SETTINGS_FILE

    def load(self) -> OpsSettings:
        """保存済みの設定。ファイルが無ければ既定値。壊れていれば SettingsError(画面に理由を出す)。"""
        if not self.path.is_file():
            return OpsSettings()
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            raise SettingsError(f"保存済みの運用設定を読み込めません({type(e).__name__})。") from e
        return validate(raw)

    def save(self, data: dict) -> OpsSettings:
        settings = validate(data)
        fd, tmp = tempfile.mkstemp(prefix=".ops_", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                yaml.safe_dump(settings.as_dict(), f, allow_unicode=True, sort_keys=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return settings
