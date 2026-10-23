# ページ計画: 1 製番について「どのページを印刷し、どのページを推論に回すか」を決める(Step.2 の選別部分)。
# app.py の main() から切り出した純粋な処理。CAD・IJCAD・推論サーバー・ホストには接続せず、DataFrame と設計フォルダの
# ファイル一覧、design.csv の「ページ番号」の一覧だけを使う。選び方は docs/print_spec.md。
#
# 印刷対象の条件: design.csv の「ページ番号」が 6 文字以内で、先頭が X/x でない図面(DWG と照合は大文字小文字を区別せず、先頭ゼロは保持)。
# 条件外のページは、印刷しないだけでなく、印刷画像に依存する推論対象・座標計算用 DF(product_df)からも外す。
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List

import pandas as pd

import cad_data_process as cad_mod

# 計画の判定結果
OK = "ok"                          # 推論に進む
MISSING_DRAWINGS = "missing"       # 推論対象ページの図面が設計フォルダに無い -> 製番は失敗(未完了)
NO_INFERENCE_PAGES = "no_inference"  # 推論対象ページが無い -> 製番はスキップ


@dataclass(frozen=True)
class PagePlan:
    inference_pages: List[str]   # 推論対象(.baninf のあるページのうち、印刷対象の条件を満たすもの。文字列の昇順)
    print_pages: List[str]       # 印刷対象(設計フォルダの図面のうち、design.csv のページ番号が条件を満たすもの。文字列の昇順)
    pdf_only_pages: List[str]    # 印刷だけ行うページ(印刷対象のうち推論対象でないもの)
    missing_pages: List[str]     # 推論対象なのに設計フォルダに図面が無いページ
    product_df: pd.DataFrame     # 条件外のページの行を除いた座標計算用 DF
    excluded_pages: List[str] = field(default_factory=list)  # 条件外のため、印刷・推論のどちらにも入れなかったページ(図面名と .baninf のページ。記録用)

    @property
    def status(self) -> str:
        if self.missing_pages:
            return MISSING_DRAWINGS
        if not self.inference_pages:
            return NO_INFERENCE_PAGES
        return OK


def build_page_plan(product_df: pd.DataFrame, dir_design_product: str, drawing_ext: str, design_page_ids: Iterable[str]) -> PagePlan:
    """product_df(座標計算用 DF)、設計製番フォルダ、design.csv の「ページ番号」の一覧から、印刷・推論の対象ページを決める。
    `design_page_ids` は必須(design.csv を読めないときに、条件なしで全ページを対象にしないため)。"""
    design_page_ids = list(design_page_ids)
    allowed = cad_mod.printable_page_keys(design_page_ids)
    pages = sorted(str(p) for p in product_df["PAGE"].dropna().unique().tolist())
    inference_pages = [p for p in pages if cad_mod.page_key(p) in allowed]
    kept_df = product_df[product_df["PAGE"].astype(str).map(cad_mod.page_key).isin(allowed)]
    print_pages = cad_mod.list_print_pages(dir_design_product, drawing_ext, design_page_ids)
    ext = drawing_ext.lower()
    all_drawings = {p.stem for p in Path(dir_design_product).iterdir() if p.is_file() and p.suffix.lower() == ext}
    # 図面名と推論対象のページ名の照合は、design.csv との照合と同じく大文字小文字を区別しない(Windows のファイル名と同じ扱い)
    print_keys = {cad_mod.page_key(p) for p in print_pages}
    inference_keys = {cad_mod.page_key(p) for p in inference_pages}
    excluded = sorted(
        {p for p in all_drawings if cad_mod.page_key(p) not in allowed} | {p for p in pages if cad_mod.page_key(p) not in allowed}
    )
    return PagePlan(
        inference_pages=inference_pages,
        print_pages=print_pages,
        pdf_only_pages=[p for p in print_pages if cad_mod.page_key(p) not in inference_keys],
        missing_pages=[p for p in inference_pages if cad_mod.page_key(p) not in print_keys],
        product_df=kept_df,
        excluded_pages=excluded,
    )
