# 動作チェック(運用画面の「2. 動作チェック」)。設定形式・出力先・変換ソフト・設計サーバー参照先を確認する。
# 読み取りだけで、何も書かない・製番処理もしない。
#   - ホスト DB: 読み取り専用の照会(出図済み製番の一覧取得 = SELECT)だけ。
#   - 推論 API: /health の状態確認だけ。/predict は呼ばない。判定は製番処理の事前確認(pipeline.api_preflight)と同じ関数。
# 結果の文言にはパス・ホスト名・秘密情報を入れない(画面に表示するため)。
import os
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from ops.history import sanitize_reason
from ops.settings_store import OpsSettings

OK, NG, SKIP = "ok", "ng", "skip"

REQUIRED_KEYS = (("data", "output"), ("data", "storage"), ("data", "initials"), ("data", "box_types"),
                 ("api", "dir_venv"), ("api", "yaml_file"), ("dwg_conversion", "converter"), ("dwg_conversion", "cad_version"))


def _item(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": sanitize_reason(detail)}


def _load_config(config_path) -> dict:
    from bs_pkg import config_loader
    return config_loader.load_yaml(str(config_path))


def check_config(config: dict) -> dict:
    missing = []
    sample = []
    for section, key in REQUIRED_KEYS:
        value = (config.get(section) or {}).get(key)
        if value in (None, ""):
            missing.append(f"{section}.{key}")
        elif "<" in str(value):
            sample.append(f"{section}.{key}")
    problems = []
    try:                                                          # 任意項目 api.request_timeout の値(省略可。指定するなら正の有限数)
        from pipeline.settings import parse_request_timeout
        parse_request_timeout((config.get("api") or {}).get("request_timeout"))
    except ValueError:
        problems.append("api.request_timeout が不正です(正の有限の数[秒]で指定。省略すると従来どおり待ち続けます)")
    if missing:
        problems.append("未設定の項目があります: " + ", ".join(missing))
    if sample:
        problems.append("サンプルのままの項目があります: " + ", ".join(sample))
    if problems:
        return _item("設定の形式", NG, " / ".join(problems))
    return _item("設定の形式", OK, "必要な項目がそろっています")


def check_output_root(root: str) -> dict:
    if not root:
        return _item("出力先", NG, "出力先が決まっていません")
    p = Path(root)
    if p.is_dir():
        if os.access(str(p), os.W_OK):
            return _item("出力先", OK, "フォルダがあり、書き込み可能です")
        return _item("出力先", NG, "フォルダはありますが、書き込めません")
    parent = p.parent
    if parent.is_dir() and os.access(str(parent), os.W_OK):
        return _item("出力先", OK, "フォルダは未作成です(実行時に作成します)")
    return _item("出力先", NG, "フォルダも、作成先の親フォルダも見つかりません")


def check_converter(path: str) -> dict:
    if path and Path(path).is_file():
        return _item("変換ソフト", OK, "変換ソフトが見つかりました")
    return _item("変換ソフト", NG, "変換ソフトが見つかりません")


def default_design_check() -> Tuple[bool, str]:
    """設計サーバー参照先: bs_pkg の設計サーバー設定にあるフォルダが 1 つ以上見つかるか(存在確認のみ)。"""
    from bs_pkg import design_mod, config_loader
    cfg = config_loader.load_yaml(design_mod.YAML_FILE_PATH)
    base, servers = cfg["base_path"], cfg["server_list"]
    found = sum(1 for s in servers if os.path.exists(base + str(s) + "$"))
    return (found > 0, f"参照先 {len(servers)} 件中 {found} 件に到達できました")


def default_host_check(initials: str) -> Tuple[bool, str]:
    """ホスト DB: 読み取り専用の照会(出図済み製番の一覧取得)だけ。件数のみ返す。"""
    from bs_pkg import host_mod
    from ops import com as com_mod
    com_mod.co_initialize()          # HTTP の要求スレッドは COM 未初期化のため、同じスレッド内で前後する
    try:
        items = host_mod.create_n3_design_list(initials)
    finally:
        com_mod.co_uninitialize()
    return (True, f"読み取り照会に成功しました(出図済み製番 {len(items)} 件)")


def default_health_check(config: dict) -> Optional[str]:
    """推論 API の /health 確認。製番処理の事前確認(pipeline.api_preflight)と**同じ関数**を使う(接続先・プロキシ・リダイレクト・
    タイムアウトの条件が同一)。使えるなら None、使えないなら固定文言の理由(接続先・パス・例外メッセージなし)を返す。"""
    from pipeline import api_preflight
    return api_preflight.unavailable_reason(config)


def _integrated_mode(config: dict) -> bool:
    """推論サーバー統合(検証設定)か。app.main と同じ判定。統合モードでは事前確認をしない(起動時に app が health/ready を確認する)。"""
    return bool((config.get("inference_server") or {}).get("enabled", False))


def run_checks(config_path, settings: OpsSettings, *,
               load_config: Callable = _load_config,
               design_check: Callable = default_design_check,
               host_check: Callable = default_host_check,
               health_check: Callable = default_health_check) -> List[dict]:
    """全項目を順に確認して結果のリストを返す。1 項目の失敗で他の確認を止めない。"""
    results: List[dict] = []
    try:
        config = load_config(config_path)
    except Exception as e:  # noqa: BLE001 - 読めないこと自体を結果として返す
        return [_item("設定の形式", NG, f"設定ファイルを読み込めません({type(e).__name__})")]
    results.append(check_config(config))
    data = config.get("data") or {}
    root = settings.output_root or str(data.get("output", ""))
    results.append(check_output_root(root))
    results.append(check_converter(str((config.get("dwg_conversion") or {}).get("converter", ""))))
    for name, fn in (("設計サーバー参照先", design_check),
                     ("ホスト DB(読み取り専用)", lambda: host_check(str(data.get("initials", ""))))):
        try:
            ok, detail = fn()
            results.append(_item(name, OK if ok else NG, detail))
        except Exception as e:  # noqa: BLE001
            results.append(_item(name, NG, f"確認できませんでした({type(e).__name__})"))
    # 推論 API: 製番処理の事前確認と同じ判定。ここが OK なら、同じ設定で事前確認も通る(接続先・プロキシ・リダイレクト・タイムアウトが同一)。
    if _integrated_mode(config):
        results.append(_item("推論 API(/health)", SKIP, "推論サーバー統合の設定のため確認しません(起動時に準備完了を確認します)"))
    else:
        try:
            reason = health_check(config)
            if reason is None:
                results.append(_item("推論 API(/health)", OK, "製番処理の事前確認と同じ条件で、HTTP 200 を確認しました"))
            else:
                results.append(_item("推論 API(/health)", NG, reason))
        except Exception as e:  # noqa: BLE001 - 理由は固定で返る想定。万一の例外も、型名だけにする
            results.append(_item("推論 API(/health)", NG, f"確認できませんでした({type(e).__name__})"))
    return results
