# 推論 API の事前確認(本番モード)。重い製番処理(設計データ読込・印刷・変換・画像作成)に入る前に、
# 既存の API 設定ファイル(server.yaml)の接続先へ GET /health だけを行い、利用できなければ理由(固定の文言)を返す。
# - 接続先(URL・ホスト名・ポート)・パス・例外メッセージは、返す理由にも、ログにも含めない(例外型と HTTP 応答コードだけ)。
# - プロキシは、実際の /predict と同じ決め方(api.use_proxy、server.yaml の proxy.host / proxy.port)にそろえる。
# - 統合モード(検証用の推論サーバーを app が起動する設定)では呼ばない。そちらは起動時に /health の準備完了を確認する。
import os
from typing import Callable, Optional

import requests

from bs_pkg import config_loader

HEALTH_TIMEOUT = 5.0          # /health の応答待ち[秒]。軽い状態確認なので /predict(待ち続ける)とは別に短くする。api.health_timeout で変更できる
PREFIX = "推論 API を利用できないため、製番処理を開始しませんでした"


def _is_connection_refused(e: BaseException) -> bool:
    """接続拒否(待ち受けるプロセスがない)か。requests / urllib3 の例外は何重かに包むので、原因の連鎖をたどる。"""
    seen, stack = set(), [e]
    while stack and len(seen) < 16:
        x = stack.pop()
        if x is None or id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, ConnectionRefusedError) or getattr(x, "winerror", None) == 10061:
            return True
        stack.extend([getattr(x, "__cause__", None), getattr(x, "__context__", None), getattr(x, "reason", None)])
        stack.extend(a for a in getattr(x, "args", ()) if isinstance(a, BaseException))
    return False


def _reason(detail: str) -> str:
    return f"{PREFIX}({detail})"


def unavailable_reason(config_data: dict, *, get: Callable = requests.get) -> Optional[str]:
    """推論 API が使えるなら None、使えないなら利用者向けの理由(1 行・接続先なし)を返す。例外は出さない。"""
    api = config_data.get("api") or {}
    try:
        path = os.path.join(str(api["dir_venv"]), str(api["yaml_file"]))
        timeout = float(api.get("health_timeout", HEALTH_TIMEOUT))
    except Exception as e:  # noqa: BLE001
        return _reason(f"API の設定項目を読み込めません: {type(e).__name__}")
    if not os.path.isfile(path):
        return _reason("API 設定ファイル server.yaml が見つかりません")
    try:
        cfg = config_loader.load_yaml(path)
        url = f"http://{cfg['host']['pc_ip']}:{cfg['host']['port']}/health"
        if api.get("use_proxy", True):
            proxy_host, proxy_port = cfg["proxy"]["host"], cfg["proxy"]["port"]
            proxies = {"http": f"http://{proxy_host}:{proxy_port}", "https": f"https://{proxy_host}:{proxy_port}"}
        else:
            proxies = {"http": None, "https": None}
    except Exception as e:  # noqa: BLE001 - 読めない・項目が足りない(メッセージには値が入り得るので型名だけ)
        return _reason(f"server.yaml を読み込めません: {type(e).__name__}")
    try:
        res = get(url, proxies=proxies, timeout=timeout)
    except requests.exceptions.Timeout:
        return _reason(f"/health が {timeout:g} 秒以内に応答しませんでした(タイムアウト)")
    except Exception as e:  # noqa: BLE001 - 接続拒否・名前解決・その他の通信エラー
        if _is_connection_refused(e):
            return _reason("接続が拒否されました。API が未起動、または server.yaml の接続先が古い可能性があります")
        return _reason(f"/health に接続できません: {type(e).__name__}")
    if res.status_code != 200:
        return _reason(f"/health の応答が正常ではありません: HTTP {res.status_code}")
    return None
