# 運用設定の保存・再読込(運用画面の「1. 運用設定」「3. 定期実行」)。
# 保存先は Git 管理外のローカル設定 `ops.local.yaml`(既存の `*.local.yaml` の方針)。追跡対象の app.yaml は読み書きしない。
# 保存するのは、出力先ルート・定期実行の有効/無効と開始時刻・間隔・対象製番リスト・1 回あたりの上限だけ。秘密情報は持たない。
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
    schedule_interval_days: int = 2   # 定期実行の間隔(日)。2 = 1 日置き(従来の運用)。1 = 毎日
    # --- 定期実行の対象範囲(明示した製番リストだけを処理する。出図リストからは自動で追加しない) ---
    scheduled_products: tuple = ()    # 定期実行の対象製番(許可リスト)。既定は空 = 何も処理しない(出図リストへフォールバックしない)
    scheduled_max_products: int = 5   # 1 回の定期実行で処理を開始する製番の上限(件)。未設定は既定値(無制限にはならない)
    scheduled_max_minutes: int = 120  # 1 回の定期実行で、新しい製番の処理を開始してよい時間の上限(分)。超えた後は新しい製番を始めない
    scheduled_retry_products: tuple = ()   # 「失敗・スキップが続いて後ろに回された製番」を、原因の対処後に、通常の順序へ戻す(再試行する)製番。既定は空

    def as_dict(self) -> dict:
        d = asdict(self)
        d["scheduled_products"] = list(self.scheduled_products)       # 画面・YAML では配列
        d["scheduled_retry_products"] = list(self.scheduled_retry_products)
        return d


def validate_output_root(value) -> str:
    text = "" if value is None else str(value).strip()
    if text == "":
        return ""
    if "<" in text or ">" in text:
        raise SettingsError("出力先にサンプルの記号(< >)が残っています。実際のフォルダを指定してください。")
    if "\n" in text or "\r" in text or "\x00" in text:
        raise SettingsError("出力先に使えない文字が含まれています。")
    drive, rest = os.path.splitdrive(text)
    if not drive or not rest.startswith(("\\", "/")):      # "C:\x" と "\\server\share\x" だけ。"x"・"\x"・"C:x" は相対/ドライブ省略なので不可
        raise SettingsError("出力先は絶対パス(ドライブ名つき、または \\\\サーバー\\共有名)で指定してください。")
    if ".." in re.split(r"[\\/]+", text):
        raise SettingsError("出力先に「..」を含めないでください。")
    return text


def validate_time(value) -> str:
    text = "" if value is None else str(value).strip()
    if not TIME_PATTERN.match(text):
        raise SettingsError("開始時刻は 24 時間表記の HH:MM(例: 08:30)で指定してください。")
    return text


MAX_INTERVAL_DAYS = 30


def validate_interval(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= MAX_INTERVAL_DAYS):
        raise SettingsError(f"実行間隔は 1〜{MAX_INTERVAL_DAYS} 日の整数で指定してください(1 = 毎日、2 = 1 日置き)。")
    return value


MAX_SCHEDULED_PRODUCTS_LIMIT = 20       # 1 回あたりの件数上限として設定できる最大値
MAX_SCHEDULED_MINUTES_LIMIT = 480        # 1 回あたりの時間上限として設定できる最大値(分)
MIN_SCHEDULED_MINUTES = 10
MAX_SCHEDULED_LIST = 500                 # 対象製番リストの最大長(誤って大量に書いた場合の安全弁)


def validate_scheduled_max_products(value) -> int:
    if value is None:                    # 未設定は既定値(無制限にはしない)
        return OpsSettings.scheduled_max_products
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= MAX_SCHEDULED_PRODUCTS_LIMIT):
        raise SettingsError(f"定期実行の 1 回あたりの件数上限は 1〜{MAX_SCHEDULED_PRODUCTS_LIMIT} 件の整数で指定してください。")
    return value


def validate_scheduled_max_minutes(value) -> int:
    if value is None:
        return OpsSettings.scheduled_max_minutes
    if isinstance(value, bool) or not isinstance(value, int) or not (MIN_SCHEDULED_MINUTES <= value <= MAX_SCHEDULED_MINUTES_LIMIT):
        raise SettingsError(f"定期実行の 1 回あたりの時間上限は {MIN_SCHEDULED_MINUTES}〜{MAX_SCHEDULED_MINUTES_LIMIT} 分の整数で指定してください。")
    return value


def validate_scheduled_products(value) -> tuple:
    """定期実行の対象製番リスト。未設定・空は空(何も処理しない)。**大文字に正規化**し(app の対象判定は大文字の `A1` 前方一致で、小文字だと毎回
    「処理対象外」になるため)、重複は大文字にそろえたあとで 1 つにする(順序は最初の出現のまま)。1 件でも不正な製番があれば全体を拒否する
    (一部だけ採用して、意図しない範囲で実行しないため)。"""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise SettingsError("定期実行の対象製番は、製番の配列で指定してください。")
    if len(value) > MAX_SCHEDULED_LIST:
        raise SettingsError(f"定期実行の対象製番が多すぎます(最大 {MAX_SCHEDULED_LIST} 件)。")
    out = []
    for item in value:
        if not isinstance(item, str):
            raise SettingsError("定期実行の対象製番は、文字列の製番で指定してください。")
        p = validate_product_no(item).upper()
        if p not in out:
            out.append(p)
    return tuple(out)


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
                       schedule_time=validate_time(data.get("schedule_time", "08:00")),
                       schedule_interval_days=validate_interval(data.get("schedule_interval_days", OpsSettings.schedule_interval_days)),
                       scheduled_products=validate_scheduled_products(data.get("scheduled_products")),
                       scheduled_max_products=validate_scheduled_max_products(data.get("scheduled_max_products")),
                       scheduled_max_minutes=validate_scheduled_max_minutes(data.get("scheduled_max_minutes")),
                       scheduled_retry_products=validate_scheduled_products(data.get("scheduled_retry_products")))


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

    SCHEDULED_KEYS = ("scheduled_products", "scheduled_max_products", "scheduled_max_minutes", "scheduled_retry_products")

    def save(self, data: dict) -> OpsSettings:
        # 画面からの保存(出力先・時刻・間隔)で、手で設定した定期実行の対象範囲(製番リスト・上限)を消さないよう、
        # 送られてこなかった対象範囲の項目は、保存済みの値を引き継ぐ。
        if isinstance(data, dict) and any(k not in data for k in self.SCHEDULED_KEYS):
            try:
                current = self.load().as_dict()
            except SettingsError:
                current = OpsSettings().as_dict()          # 壊れた設定は上書きで直せる(従来どおり)
            data = {**{k: current[k] for k in self.SCHEDULED_KEYS if k not in data}, **data}
        settings = validate(data)
        fd, tmp = tempfile.mkstemp(prefix=".ops_", suffix=".tmp", dir=str(self.path.parent))
        try:
            stored = settings.as_dict()
            defaults = OpsSettings().as_dict()
            for key in self.SCHEDULED_KEYS:                    # 既定値のままの定期実行の対象範囲は書き出さない(従来のファイルの形を変えない)
                if stored[key] == defaults[key]:
                    stored.pop(key)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                yaml.safe_dump(stored, f, allow_unicode=True, sort_keys=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return settings
