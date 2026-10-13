# 推論 API クライアント層(S6)。データ作成側が推論サーバーを使うときに依存する bs_pkg.image_mod の機能を、ここ 1 か所に集約した。
#   - 画像の送信と検出結果の取得: image_mod.slice_prediction_request
#   - 失敗の表現: image_mod.InferenceRequestError(failed_pages / log_text を持つ)
#   - ログの整形: image_mod.log_formater
# 画像の作成(pdf_to_images・process_images_from_jpeg)は推論 API には関係しないので、この層には含めない(product_flow が直接使う)。
#
# 契約(リクエスト・応答・ファイル形式・失敗の意味)は docs/inference_contract.md。
# 振る舞いは切り出し前の product_flow と同じ。image_mod は「モジュールの属性」経由で呼ぶ
# (テストが mock.patch.object(app.image_mod, "slice_prediction_request", ...) で差し替えるため)。
from dataclasses import dataclass
from typing import Optional

from bs_pkg import image_mod

_UNSET = object()


@dataclass(frozen=True)
class DetectionResponse:
    """推論 API への依頼の結果。log_text は依頼の経過ログ(str に正規化済み)。warning は、戻り値が想定外だったときの注意(無ければ None)。"""
    log_text: str
    warning: Optional[str] = None


def normalize_log_text(raw) -> DetectionResponse:
    """image_mod の戻り値(通常は str)を str に正規化する。None は空文字、bytes は UTF-8 で復号、それ以外は str() し、注意を付ける。"""
    if raw is None:
        return DetectionResponse("", "API request log is None.")
    if isinstance(raw, (bytes, bytearray)):
        return DetectionResponse(raw.decode("utf-8", errors="replace"))
    if not isinstance(raw, str):
        return DetectionResponse(str(raw), f"Unexpected API request log type: {type(raw)}")
    return DetectionResponse(raw)


def request_detections(dir_product: str, pages: list, api_config_path: str, *, use_proxy: bool = True,
                       timeout=_UNSET) -> DetectionResponse:
    """製番フォルダ内の各ページの画像を推論 API に送り、検出結果(`{page}_detected.txt` など)を製番フォルダに出力させる。

    失敗(HTTP エラー・接続失敗・応答不正・検出ファイル未生成・API 設定の読み込み失敗)は image_mod.InferenceRequestError で送出される
    (呼び出し側=run_product が製番の失敗として扱う。成功扱いにしない)。
    timeout を省略すると image_mod には渡さない(従来どおり待ち続ける)。検証設定(推論サーバー統合)のときだけ秒数を渡す。
    """
    kwargs = {} if timeout is _UNSET else {"timeout": timeout}
    raw = image_mod.slice_prediction_request(dir_product, pages, api_config_path, use_proxy=use_proxy, **kwargs)
    return normalize_log_text(raw)


def format_log(log_text: str) -> str:
    """依頼ログを読みやすく整形する(空なら空文字)。"""
    return image_mod.log_formater(log_text) if log_text else ""


def describe_failure(error: BaseException) -> Optional[str]:
    """推論 API の失敗(InferenceRequestError で、ログがあるもの)なら、整形した失敗内容を返す。それ以外は None。"""
    if isinstance(error, image_mod.InferenceRequestError) and error.log_text:
        return image_mod.log_formater(error.log_text)
    return None
