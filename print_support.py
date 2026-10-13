"""IJCAD 用の印刷スクリプト(WIRING.SCR)の作成。

社内共通パッケージ bs_pkg の design_mod.output_print_script と同じ -PLOT の手順を、このリポジトリ側で管理する版
(bs_pkg は Git 管理外のため、印刷設定の変更をここで追跡できるようにした)。

変更点は「印刷オフセット」だけ: 従来の固定値 `5,5`(用紙の左下から 5mm)だと、図面と用紙の縦横比の差が
すべて右(または上)の余白になって偏る。`C`(Center the plot=用紙の中央に配置)にすると、
印刷範囲(Limits)が用紙の中央に置かれ、左右・上下の余白が等しくなる。
尺度は従来どおり `F`(Fit)で、印刷範囲(Limits)・用紙(ISO A3)も変えていないので、PDF 上の図枠の大きさ(縮尺)は変わらない。
`close y` は維持する(`close n` や引数なしの `close` にしない)。
"""
from __future__ import annotations

import os

PLOT_OFFSET = "C"  # 用紙の中央に配置(旧: "5,5")


def build_script_lines(dwg_path: str, pdf_path: str, plot_offset: str = PLOT_OFFSET) -> list[str]:
    """1 図面分の IJCAD スクリプト行(-PLOT の対話入力順は従来と同じ)。"""
    return [
        f"open {dwg_path}",
        "layer F *HOJYO,*MPRINT,*CMNTH,REVISION*",
        "",
        "-PLOT",
        "Y",                      # 詳細設定
        "Model",                  # レイアウト
        "DWG To PDF",             # 出力デバイス
        "ISO A3 (420.00 x 297.00 MM)",  # 用紙
        "M",                      # 単位: mm
        "L",                      # 用紙の向き: 横
        "N",                      # 上下反転なし
        "L",                      # 印刷範囲: Limits
        "F",                      # 尺度: Fit
        plot_offset,              # 印刷オフセット: 中央(旧 5,5)
        "Y",                      # 印刷スタイルを使用
        "thickness.ctb",
        "Y",                      # 線の太さを使用
        "A",                      # 表示どおり
        pdf_path,                 # 出力ファイルパス
        "N",                      # ページ設定を保存しない
        "Y",                      # 印刷を実行
        "close y",                # 図面を保存せず閉じる(忘れないこと)
    ]


def output_print_script(page_list, dir_target: str, dir_product: str, plot_offset: str = PLOT_OFFSET) -> str:
    """WIRING.SCR(shift-jis)を dir_product に作成して、そのパスを返す。出力 PDF は `{ページ}.pdf`(従来どおり)。

    設計図面が読めない場合は FileNotFoundError(従来の sys.exit() ではなく、呼び出し側が製番の失敗として扱えるようにした)。
    """
    script = os.path.join(dir_product, "WIRING.SCR")
    lines: list[str] = []
    for page in page_list:
        name = str(page)
        dwg_path = os.path.join(dir_target, name + ".dwg")
        if not os.access(dwg_path, os.R_OK):
            raise FileNotFoundError(f"印刷図面が存在しません: {dwg_path}")
        lines += build_script_lines(dwg_path, os.path.join(dir_product, name + ".pdf"), plot_offset)
    lines.append("quit y")
    with open(script, "w", encoding="shift-jis") as f:
        f.write("\n".join(lines) + "\n")
    return script
