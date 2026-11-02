# 定期実行の「今回の対象」の確定(運用画面の定期実行・`run-scheduled`・`run-scheduled --dry-run` が同じ関数を使う)。
#
# 方針(2026-11 の実装。経緯: 出図リストは月ごとに入れ替わり、定期実行が意図しない製番へ広がる恐れがあった):
# - 対象は、設定(ops.local.yaml)に明示された製番リスト(`scheduled_products`)だけ。リストが未設定・空なら、対象 0 件で何もしない。
#   ホストの出図リストからは、自動で対象を追加しない(出図リストへフォールバックしない)。
# - リストの各製番について、処理済み(出力フォルダがあり、再処理マーカーが無い)は除く。型式・整理番号などの対象判定は、従来どおり
#   製番処理(product_flow.check_product)の中で行う(ここでは変えない)。
# - 1 回あたりの件数上限(`scheduled_max_products`)を、リストの順に先頭から適用する。上限を超えた分は、次回以降に回す(今回は処理しない)。
# - 実行前に今回の対象を確定して履歴に記録し、dry-run も同じ関数で同じ一覧を返す(dry-run と実行前の一覧が一致する)。
# 時間上限(`scheduled_max_minutes`)は、実行側(app.main の製番ループ)が、新しい製番の処理を始める前に確認する。
import os
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from ops.settings_store import OpsSettings

REASON_NOT_CONFIGURED = "対象製番リストが未設定(または空)のため、何も処理しません(出図リストからは対象を追加しません)"
REASON_DONE = "処理済み"
REASON_OVER_LIMIT = "1 回あたりの件数上限を超えるため、次回以降に回します"


def default_is_done(output_dir: str, product: str) -> bool:
    """製番処理(product_flow.check_product)と同じ「処理済み」の判定: 製番フォルダがあり、再処理マーカーが無い。"""
    if not os.path.isdir(os.path.join(output_dir, product)):
        return False
    from pipeline import product_flow          # 遅延 import(bs_pkg が必要。ops の他の機能の読込に影響させない)
    return not product_flow.has_incomplete_marker(output_dir, product)


@dataclass(frozen=True)
class ScheduledPlan:
    targets: Tuple[str, ...]                   # 今回処理を始める製番(この順)
    excluded: Tuple[Tuple[str, str], ...]      # (製番, 除外の理由)。処理済み・件数上限
    listed: int                                # 設定に書かれた製番の件数
    max_products: int
    max_minutes: int

    def summary(self) -> str:
        """履歴の理由・dry-run の報告に載せる 1 行(製番そのものは targets / 履歴の targets に残す)。"""
        if self.listed == 0:
            return REASON_NOT_CONFIGURED
        done = sum(1 for _, r in self.excluded if r == REASON_DONE)
        over = sum(1 for _, r in self.excluded if r == REASON_OVER_LIMIT)
        return (f"対象 {len(self.targets)} 件(リスト {self.listed} 件、処理済み {done} 件、上限超過 {over} 件。"
                f"上限: {self.max_products} 件・{self.max_minutes} 分)")


def build_plan(settings: OpsSettings, output_dir: str, is_done: Optional[Callable[[str, str], bool]] = None) -> ScheduledPlan:
    """設定と出力先(製番フォルダの親)から、今回の対象を確定する。副作用なし(出力先の一覧を読むだけ)。"""
    is_done = is_done or default_is_done
    listed = tuple(settings.scheduled_products)
    targets, excluded = [], []
    for product in listed:
        if is_done(output_dir, product):
            excluded.append((product, REASON_DONE))
        elif len(targets) >= settings.scheduled_max_products:
            excluded.append((product, REASON_OVER_LIMIT))
        else:
            targets.append(product)
    return ScheduledPlan(tuple(targets), tuple(excluded), len(listed), settings.scheduled_max_products, settings.scheduled_max_minutes)
