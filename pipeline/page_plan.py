# ページ計画: 1 製番について「どのページを印刷し、どのページを推論に回すか」を決める(Step.2 の選別部分)。
# app.py の main() から切り出した純粋な処理。CAD・IJCAD・推論サーバー・ホストには接続せず、DataFrame と設計フォルダの
# ファイル一覧だけを使う。選び方は切り出し前と同じ(docs/print_spec.md)。
from dataclasses import dataclass
from typing import List

import pandas as pd

import cad_data_process as cad_mod

# 計画の判定結果
OK = "ok"                          # 推論に進む
MISSING_DRAWINGS = "missing"       # 推論対象ページの図面が設計フォルダに無い -> 製番は失敗(未完了)
NO_INFERENCE_PAGES = "no_inference"  # 推論対象ページが無い -> 製番はスキップ


@dataclass(frozen=True)
class PagePlan:
    inference_pages: List[str]   # 推論対象(.baninf のあるページ。X/x 始まりを除く。文字列の昇順)
    print_pages: List[str]       # 印刷対象(設計フォルダの図面のうち X/x 始まりを除く全ページ。文字列の昇順)
    pdf_only_pages: List[str]    # 印刷だけ行うページ(印刷対象のうち推論対象でないもの)
    missing_pages: List[str]     # 推論対象なのに設計フォルダに図面が無いページ
    product_df: pd.DataFrame     # X/x 始まりの行を除いた座標計算用 DF

    @property
    def status(self) -> str:
        if self.missing_pages:
            return MISSING_DRAWINGS
        if not self.inference_pages:
            return NO_INFERENCE_PAGES
        return OK


def build_page_plan(product_df: pd.DataFrame, dir_design_product: str, drawing_ext: str) -> PagePlan:
    """product_df(座標計算用 DF)と設計製番フォルダから、印刷・推論の対象ページを決める。"""
    pages = sorted(str(p) for p in product_df["PAGE"].dropna().unique().tolist())
    inference_pages = [p for p in pages if not cad_mod.is_excluded_page(p)]
    kept_df = product_df[~product_df["PAGE"].astype(str).map(cad_mod.is_excluded_page)]
    print_pages = cad_mod.list_print_pages(dir_design_product, drawing_ext)
    return PagePlan(
        inference_pages=inference_pages,
        print_pages=print_pages,
        pdf_only_pages=[p for p in print_pages if p not in inference_pages],
        missing_pages=[p for p in inference_pages if p not in print_pages],
        product_df=kept_df,
    )
