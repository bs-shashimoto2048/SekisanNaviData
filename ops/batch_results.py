# 定期実行・「今すぐ実行」の製番ごとの結果(履歴の results の形)を作る。両方が同じ関数を使う。
#
# 方針(2026-11 の改定。旧: 設定に明示した製番リスト〔PR #43〕→ 撤回):
# - 対象は、実行のたびにホスト DB(読み取り専用)から取得する(`ops/host_targets.py`)。固定の製番リスト・件数や時間の上限は持たない。
# - 取得した製番は、途中で打ち切らず、すべて処理する。製番ごとの通常例外・SystemExit はその製番の失敗として記録し、残りを続ける
#   (app.main → pipeline.product_flow.run_product)。全件を試した後、失敗があれば実行全体を失敗として記録する。
# - 実行全体の中断・失敗(推論 API の事前確認の失敗、実行全体の例外、COM 初期化失敗)は、`scope: run` を付けて記録する。
from typing import Dict, Iterable

# 製番ごとの結果(履歴の results の status)
RESULT_DONE, RESULT_FAILED, RESULT_SKIPPED, RESULT_NOT_STARTED = "完了", "失敗", "スキップ", "未開始"
DEADLINE_MARK = "実行時間の上限"       # app.main の時間上限(app.DEADLINE_SKIP_REASON。定期実行・今すぐ実行は上限を指定しないが、app.main の仕様として残る)
SCOPE_RUN = "run"                     # 製番固有ではなく、実行全体(環境・API 停止など)の失敗


def results_from(res: dict, targets: Iterable[str]) -> Dict[str, dict]:
    """app.main の戻り値から、製番ごとの結果(履歴に残す形)を作る。時間上限などで開始しなかった製番は「未開始」(実際に処理した製番と区別する)。
    app.main が **実行全体の中断**(`res["aborted"]`。推論 API の事前確認の失敗)を返したときは、対象が 1 件でも複数件でも、すべての製番を
    「未開始・`scope: run`」にする(製番固有の失敗として数えない)。理由文の文字列判定や「同じ理由で失敗」という推測は使わない。"""
    completed = set(res.get("completed") or [])
    failed = dict(res.get("failed") or {})
    skipped = dict(res.get("skipped") or {})
    targets = list(targets)
    if res.get("aborted"):                                  # 実行全体の中断: どの製番も処理を始めていない
        return run_level_results(targets, RESULT_NOT_STARTED, str(res["aborted"]))
    out: Dict[str, dict] = {}
    for p in targets:
        if p in completed:
            out[p] = {"status": RESULT_DONE}
        elif p in failed:
            out[p] = {"status": RESULT_FAILED, "reason": str(failed[p])}
        elif p in skipped:
            reason = str(skipped[p])
            out[p] = {"status": RESULT_NOT_STARTED if DEADLINE_MARK in reason else RESULT_SKIPPED, "reason": reason}
        else:
            out[p] = {"status": RESULT_NOT_STARTED, "reason": "結果が返りませんでした"}
    return out


def run_level_results(targets: Iterable[str], status: str, reason: str) -> Dict[str, dict]:
    """実行全体の失敗(例外・SystemExit・COM 初期化失敗)。どの製番が処理されたか分からないため、すべてに同じ結果を付け、製番固有の失敗には数えない。"""
    return {p: {"status": status, "reason": reason, "scope": SCOPE_RUN} for p in targets}
