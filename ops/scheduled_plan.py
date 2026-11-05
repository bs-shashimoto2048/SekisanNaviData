# 定期実行の「今回の対象」の確定(運用画面の定期実行・`run-scheduled`・`run-scheduled --dry-run` が同じ関数を使う)。
#
# 方針(2026-11 の実装。経緯: 出図リストは月ごとに入れ替わり、定期実行が意図しない製番へ広がる恐れがあった):
# - 対象は、設定(ops.local.yaml)に明示された製番リスト(`scheduled_products`)だけ。リストが未設定・空なら、対象 0 件で何もしない。
#   ホストの出図リストからは、自動で対象を追加しない(出図リストへフォールバックしない)。
# - リストの各製番について、処理済み(出力フォルダがあり、再処理マーカーが無い)は除く。型式・整理番号などの対象判定は、従来どおり
#   製番処理(product_flow.check_product)の中で行う(ここでは変えない)。
# - **失敗・スキップが続く製番は、後ろに回す**: 失敗した製番は後始末でフォルダが消え、対象外などでスキップされた製番はフォルダを作らないため、どちらも
#   次回も対象に残る。先頭に居続けて、毎回の件数上限(特に 1 件のとき)を占有しないよう、直近の定期実行で **2 回連続**して失敗・スキップした製番は、
#   未処理の製番(連続していないもの)より後ろに回す。**一覧から削除はしない**(後続を先に処理したあと、枠が余れば再試行される)。
#   直近の結果は履歴(定期実行の `results`)から数える。実行全体の中断・失敗(app.main が戻り値に付ける明示的な `aborted`〔推論 API の事前確認の失敗〕、実行全体の例外、COM 初期化失敗)は、
#   `scope: run` を付けて記録し、製番ごとの失敗として数えない(理由文の判定や「同じ理由で失敗」という推測はしない)。
#   原因を対処したら、`scheduled_retry_products` に入れると、通常の順序に戻る(docs/ops_console.md)。
# - 1 回あたりの件数上限(`scheduled_max_products`)を、並べ替えたあとの順に先頭から適用する。上限を超えた分は、次回以降に回す(今回は処理しない)。
# - 実行前に今回の対象を確定して履歴に記録し、dry-run も同じ関数で同じ一覧を返す(dry-run と実行前の一覧が一致する)。
# 時間上限(`scheduled_max_minutes`)は、実行側(app.main の製番ループ)が、新しい製番の処理を始める前に確認する。
import os
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ops.settings_store import OpsSettings

REASON_NOT_CONFIGURED = "対象製番リストが未設定(または空)のため、何も処理しません(出図リストからは対象を追加しません)"
REASON_DONE = "処理済み"
REASON_OVER_LIMIT = "1 回あたりの件数上限を超えるため、次回以降に回します"

DEMOTE_AFTER = 2                      # 直近の定期実行で、これ以上連続して失敗・スキップした製番を、後ろに回す

# 製番ごとの結果(履歴の results の status)
RESULT_DONE, RESULT_FAILED, RESULT_SKIPPED, RESULT_NOT_STARTED = "完了", "失敗", "スキップ", "未開始"
DEADLINE_MARK = "実行時間の上限"       # app.main が、時間上限で開始しなかった製番に付ける理由に含まれる語(app.DEADLINE_SKIP_REASON)
DONE_MARK = "処理済み"                 # check_product が、既に処理済みの製番に付ける理由
SCOPE_RUN = "run"                     # 製番固有ではなく、実行全体(環境・API 停止など)の失敗


def default_is_done(output_dir: str, product: str) -> bool:
    """製番処理(product_flow.check_product)と同じ「処理済み」の判定: 製番フォルダがあり、再処理マーカーが無い。"""
    if not os.path.isdir(os.path.join(output_dir, product)):
        return False
    from pipeline import product_flow          # 遅延 import(bs_pkg が必要。ops の他の機能の読込に影響させない)
    return not product_flow.has_incomplete_marker(output_dir, product)


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


def failure_streaks(runs: List[dict], products: Iterable[str]) -> Dict[str, int]:
    """履歴(新しい順。History.runs())の定期実行の results から、製番ごとの「直近の連続した失敗・スキップ」の回数を数える。
    **連続回数の定義**(docs/ops_console.md と tests/test_ops_scheduled_scope.py の `StreakDefinitionTest` で固定):
    - 数える(+1): その製番の「失敗」(製番固有。`scope: run` でないもの)と「スキップ」(処理済み・時間上限によるものを除く)。
    - リセット(0 に戻す): その製番の「完了」。
    - 数えず、リセットもしない(結果が無いものとして飛ばす): 「未開始」・`scope: run`(実行全体の中断・失敗)・その製番の結果が無い実行・処理済みや時間上限のスキップ。"""
    streaks: Dict[str, int] = {}
    scheduled = [r for r in runs if r.get("kind") == "定期" and isinstance(r.get("results"), dict)]
    for p in products:
        n = 0
        for run in scheduled:
            r = run["results"].get(p)
            if not r:
                continue
            status = r.get("status")
            if status == RESULT_DONE:
                break
            if r.get("scope") == SCOPE_RUN:
                continue
            if status == RESULT_FAILED:
                n += 1
            elif status == RESULT_SKIPPED and DONE_MARK not in str(r.get("reason", "")) and DEADLINE_MARK not in str(r.get("reason", "")):
                n += 1
        streaks[p] = n
    return streaks


@dataclass(frozen=True)
class ScheduledPlan:
    targets: Tuple[str, ...]                   # 今回処理を始める製番(この順。並べ替え後)
    excluded: Tuple[Tuple[str, str], ...]      # (製番, 除外の理由)。処理済み・件数上限
    listed: int                                # 設定に書かれた製番の件数
    max_products: int
    max_minutes: int
    deferred: Tuple[Tuple[str, int], ...] = () # (製番, 連続回数)。失敗・スキップが続き、未処理の製番より後ろに回した製番(対象に入るかは枠次第)

    def summary(self) -> str:
        """履歴の理由・dry-run の報告に載せる 1 行(製番そのものは targets / 履歴の targets に残す)。"""
        if self.listed == 0:
            return REASON_NOT_CONFIGURED
        done = sum(1 for _, r in self.excluded if r == REASON_DONE)
        over = sum(1 for _, r in self.excluded if r == REASON_OVER_LIMIT)
        later = f"、後ろに回した {len(self.deferred)} 件" if self.deferred else ""
        return (f"対象 {len(self.targets)} 件(リスト {self.listed} 件、処理済み {done} 件、上限超過 {over} 件{later}。"
                f"上限: {self.max_products} 件・{self.max_minutes} 分)")

    def deferred_lines(self) -> List[str]:
        """dry-run の表示: 後ろに回した製番と、その理由。"""
        return [f"{p}: 直近の定期実行で {n} 回連続して失敗・スキップしたため、未処理の製番より後ろに回しました"
                f"({'今回の対象に含まれます' if p in self.targets else '今回は枠が足りず対象外です'})" for p, n in self.deferred]


def build_plan(settings: OpsSettings, output_dir: str, is_done: Optional[Callable[[str, str], bool]] = None,
               streaks: Optional[Dict[str, int]] = None) -> ScheduledPlan:
    """設定と出力先(製番フォルダの親)から、今回の対象を確定する。副作用なし(出力先の一覧を読むだけ)。
    streaks は failure_streaks の結果(省略なら並べ替えなし)。`scheduled_retry_products` の製番は、連続回数にかかわらず通常の順序。"""
    is_done = is_done or default_is_done
    streaks = streaks or {}
    exempt = set(settings.scheduled_retry_products)
    listed = tuple(settings.scheduled_products)
    excluded, normal, later = [], [], []
    for product in listed:
        if is_done(output_dir, product):
            excluded.append((product, REASON_DONE))
        elif streaks.get(product, 0) >= DEMOTE_AFTER and product not in exempt:
            later.append(product)
        else:
            normal.append(product)
    ordered = normal + later                                            # 連続していない製番を先に。後ろに回した製番も、枠が余れば再試行される
    targets = ordered[:settings.scheduled_max_products]
    excluded += [(p, REASON_OVER_LIMIT) for p in ordered[settings.scheduled_max_products:]]
    return ScheduledPlan(tuple(targets), tuple(excluded), len(listed), settings.scheduled_max_products,
                         settings.scheduled_max_minutes, tuple((p, streaks[p]) for p in later))
