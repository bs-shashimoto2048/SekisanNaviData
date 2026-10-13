# 設計データ(.baninf)から盤情報の表(DataFrame)を作り、CSV 出力・結合する処理。
# CAD・IJCAD・外部サーバーには触れない(pandas とファイルのみ)。
# 分割前の cad_data_process.py から関数本体を変更せずに移した(S5)。外部からは `import cad_data_process as cad_mod` で使う。
import re
from collections import OrderedDict
from pathlib import Path
from typing import List

import pandas as pd

# DFカラム数値変換
def to_numeric_safe(df: pd.DataFrame):
    """数値変換できる列のみ変換"""
    for col in df.columns:
        if col == "読込ファイル名":
            continue
        df[col] = pd.to_numeric(df[col], errors="ignore")
    return df

# 図名修正
def clean_zumei(df: pd.DataFrame):
    """ZUMEI 列の全角・半角スペースを削除"""
    if "ZUMEI" in df.columns:
        df["ZUMEI"] = df["ZUMEI"].astype(str).str.replace(r"[ 　]", "", regex=True)
    else:
        df["ZUMEI"] = ""
    return df

# DFブランク処理
def safe_apply(df: pd.DataFrame, func, cols):
    """空 DataFrame でも apply 代入できる"""
    if not df.empty:
        df[cols] = df.apply(func, axis=1)
    else:
        for col in cols:
            df[col] = pd.NA
    return df

# CSVラッパー
def export_csv(df: pd.DataFrame, path: str, sort_by=None):
    """CSV出力用ラッパー"""
    if sort_by:
        df = df.sort_values(by=sort_by)
    df.to_csv(path, index=False, encoding="cp932")

# 検出エリア算出
def assign_detect_area_standard(row):
    """
    指定された行データの BAN_TYPE に応じて、検出エリアの幅 (X) と高さ (Y) を決定する。

    Args:
        row (pd.Series): BAN_TYPE や BAN_W, BAN_H1, BAN_H2, BAN_D などの列を含む DataFrame の行

    Returns:
        pd.Series: 以下の2列を持つ Series
            - DETECT_AREA_X: 検出エリアの幅（X方向）
            - DETECT_AREA_Y: 検出エリアの高さ（Y方向）

    処理ルール:
        - 正面図: X = BAN_W, Y = BAN_H1
        - 側面図: X = BAN_D, Y = BAN_H1
        - 背面図: X = BAN_W, Y = BAN_H2
        - その他（例: 不確定な矢視図）: X = BAN_W, Y = BAN_H1
    """
    x = y = pd.NA
    ban_type = row.get("BAN_TYPE", "")

    if ban_type == "正面図":
        x, y = row.get("BAN_W", pd.NA), row.get("BAN_H1", pd.NA)
    elif ban_type == "側面図":
        x, y = row.get("BAN_D", pd.NA), row.get("BAN_H1", pd.NA)
    elif ban_type == "背面図": # 以下の条件より矢視図を削除_2025/11/19
    # elif ban_type == "背面図" or "矢視図" in str(ban_type):
        x, y = row.get("BAN_W", pd.NA), row.get("BAN_H2", pd.NA)
    else: # 背面図の隣に書かれる内部の矢視図は名称も不確定なので例外処理とする
        x, y = row.get("BAN_W", pd.NA), row.get("BAN_H1", pd.NA) # 外形は最大を採用

    return pd.Series({"DETECT_AREA_X": x, "DETECT_AREA_Y": y})

# 検出エリア割当
def assign_detect_area_foundation(row):
    return pd.Series({
        "DETECT_AREA_X": row.get("BAN_W", pd.NA),
        "DETECT_AREA_Y": row.get("BAN_D", pd.NA)
    })

# .baninf 読み込み
def load_baninf(folder_path: str, file_ext=".baninf") -> pd.DataFrame:
    """
    指定フォルダ内の .baninf ファイルを読み込み、DataFrame に変換する。

    Args:
        folder_path (str): .baninf ファイルを格納しているフォルダのパス
        file_ext (str, optional): 読み込むファイル拡張子（デフォルト ".baninf"）

    Returns:
        pd.DataFrame: 以下の処理が反映された DataFrame
            - 読込ファイル名列を先頭に追加
            - 行ごとに '|' 区切りの key$value を列に展開
            - 数値に変換可能な列は自動で変換
            - "ZUMEI" と "PAGE" 列が存在する場合、ZUMEI → PAGE の順でソート
            - clean_zumei 関数で ZUMEI 列を整形

    処理内容:
        1. フォルダ内の指定拡張子ファイルを順に取得
        2. ファイル名が "0" または "x" で始まる場合はスキップ
        3. 各行を '|' で分割し、key$value の形式で DataFrame 列を作成
        4. すべての行を集約して DataFrame を作成
        5. to_numeric_safe 関数で自動的に数値列に変換
        6. ZUMEI/PAGE によるソートと ZUMEI 列の整形を適用
    """
    folder = Path(folder_path)
    all_rows = []
    column_order = []

    for file_path in folder.glob(f"*{file_ext}"):
        if file_path.name[0].lower() in ["0", "x"]:
            continue

        with open(file_path, "r", encoding="cp932") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                row_dict = OrderedDict()
                row_dict["読込ファイル名"] = file_path.name

                for part in line.split("|"):
                    if "$" in part:
                        key, value = part.split("$", 1)
                        row_dict[key] = value
                        if key not in column_order:
                            column_order.append(key)

                all_rows.append(row_dict)

    columns = ["読込ファイル名"] + column_order
    df = pd.DataFrame(all_rows, columns=columns)
    df = to_numeric_safe(df)

    # # データ欠落対策(ソートで使用するカラムのみ確実に作成する)
    # REQUIRED_COLUMNS = ["PAGE", "ZUMEI",]
    # # -> チェックするカラムがなければブランクでデータ作成
    # for col in REQUIRED_COLUMNS:
    #     if col not in df.columns:
    #         df[col] = np.nan

    if "PAGE" in df.columns and "ZUMEI" in df.columns:
        df = df.sort_values(by=["ZUMEI", "PAGE"])

    df = clean_zumei(df)
    return df

# 側面図左右分類
def side_view_classifier(df: pd.DataFrame) -> pd.DataFrame:
    """
    側面図を左右に振り分け、必要なフィールドを正面/背面データからコピーする。
    - 側面図の KITEN_Y が正面図グループと一致する場合は正面図グループで判定（min->左、max->右）。
    - 背面図グループと一致する場合は背面図グループで判定（左右逆）。
    - 側面図行の DETECT_AREA_X/Y は常にその行の BAN_H1, BAN_D をコピーする。
    """
    df = df.copy()
    df_side = df[df.get("BAN_TYPE") == "側面図"]

    for idx, side_row in df_side.iterrows():
        ky = side_row.get("KITEN_Y")
        front_rows = df[(df.get("BAN_TYPE") == "正面図") & (df.get("KITEN_Y") == ky)]
        back_rows  = df[(df.get("BAN_TYPE") == "背面図") & (df.get("KITEN_Y") == ky)]
        side_group = df_side[df_side.get("KITEN_Y") == ky]

        if side_group.empty:
            df.at[idx, "DETECT_AREA_X"] = side_row.get("BAN_H1", pd.NA)
            df.at[idx, "DETECT_AREA_Y"] = side_row.get("BAN_D", pd.NA)
            continue

        min_x_idx = side_group["KITEN_X"].idxmin()
        max_x_idx = side_group["KITEN_X"].idxmax()
        copied = False

        if not front_rows.empty:
            if idx == min_x_idx:
                df.at[idx, "BAN_TYPE"] = "左側面図"
                src = front_rows.loc[front_rows["KITEN_X"].idxmin()]
                for col in ["BAN_MENNO", "BAN_NO", "PAGE", "ZUMEI", "BAN_MEISYOU"]:
                    if col in df.columns and col in src.index:
                        df.at[idx, col] = src.get(col, df.at[idx, col])
                copied = True
            elif idx == max_x_idx:
                df.at[idx, "BAN_TYPE"] = "右側面図"
                src = front_rows.loc[front_rows["KITEN_X"].idxmax()]
                for col in ["BAN_MENNO", "BAN_NO", "PAGE", "ZUMEI", "BAN_MEISYOU"]:
                    if col in df.columns and col in src.index:
                        df.at[idx, col] = src.get(col, df.at[idx, col])
                copied = True

        if (not copied) and (not back_rows.empty):
            if idx == min_x_idx:
                df.at[idx, "BAN_TYPE"] = "右側面図"
                src = back_rows.loc[back_rows["KITEN_X"].idxmin()]
                for col in ["BAN_MENNO", "BAN_NO", "PAGE", "ZUMEI", "BAN_MEISYOU"]:
                    if col in df.columns and col in src.index:
                        df.at[idx, col] = src.get(col, df.at[idx, col])
                copied = True
            elif idx == max_x_idx:
                df.at[idx, "BAN_TYPE"] = "左側面図"
                src = back_rows.loc[back_rows["KITEN_X"].idxmax()]
                for col in ["BAN_MENNO", "BAN_NO", "PAGE", "ZUMEI", "BAN_MEISYOU"]:
                    if col in df.columns and col in src.index:
                        df.at[idx, col] = src.get(col, df.at[idx, col])
                copied = True

        df.at[idx, "DETECT_AREA_X"] = side_row.get("BAN_H1", pd.NA)
        df.at[idx, "DETECT_AREA_Y"] = side_row.get("BAN_D", pd.NA)

    mask_other = df["BAN_TYPE"].isin(["正面図", "背面図"])
    if mask_other.any():
        df.loc[mask_other, ["DETECT_AREA_X", "DETECT_AREA_Y"]] = \
            df.loc[mask_other].apply(assign_detect_area_standard, axis=1)

    return df

# フィルタ&計算&出力
def filter_calc_export(df, keyword, assign_func, output_file, col_dict):
    """
    概要:
        PAGE が数字のみ文字列型の行のみをフィルタリングし、指定キーワードで
        DataFrame をフィルタリングし、検出エリア列を計算して CSV 出力する。

    Args:
        df (pd.DataFrame): 元のデータフレーム
        keyword (str): ZUMEI 列で検索するキーワード（例: "内部機器配置図"）
        assign_func (function): 各行に対して DETECT_AREA_X/Y を計算する関数
        output_file (str or Path): 出力する CSV ファイルパス
        col_dict (dict): キーワードごとの出力列を保持する辞書

    Returns:
        pd.DataFrame: フィルタリング＆加工後の DataFrame
    """
    # Add_2025/12/08:PAGE が数字のみの文字列型の行をピックアップ
    df_numeric_page = df[df["PAGE"].astype(str).str.isdigit()].copy()

    # キーワード一致
    df_filtered = df_numeric_page[
        df_numeric_page["ZUMEI"].astype(str).str.contains(keyword, na=False)
        ].copy()

    # 検出領域の計算
    df_filtered = safe_apply(df_filtered, assign_func, ["DETECT_AREA_X", "DETECT_AREA_Y"])

    desired_cols = [c for c in col_dict.get(keyword, []) if c in df_filtered.columns]

    # ソート
    sort_cols = ["BAN_MENNO"]
    if "BAN_TYPE" in df_filtered.columns:
        sort_cols.append("BAN_TYPE")

    export_csv(df_filtered[desired_cols].sort_values(by=sort_cols), output_file) # 出力
    return df_filtered

# ナチュラルソート
def natural_key(s):
    """文字列中の数字部分を数値として扱うナチュラルソート用キー"""
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split(r'(\d+)', s)]

# 盤情報DF作成&出力
def export_ban_data(
        df: pd.DataFrame,
        output_dir: str,
        output_prefix=""
        ) -> tuple[list[str], str, str, str]:
    """
    DataFrame 内の設備図情報を種類ごとにフィルタリング・加工して CSV 出力し、
    元 DataFrame の '読込ファイル名' のユニークリストと、
    出力した各 CSV ファイルのパスを返す。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ユニークリスト取得
    unique_files_no_ext = [Path(f).stem for f in df["読込ファイル名"].unique()]
    unique_files_no_ext.sort(key=natural_key)

    if output_prefix != "":
        output_prefix = f"{output_prefix}_"

    # デバッグ用: 元DF出力
    export_csv(df, output_dir / f"{output_prefix}df.csv")

    OUTPUT_COLUMNS = {
        "内部機器配置図": [
            "BAN_MENNO","BAN_NO","PAGE","ZUMEI","BAN_MEISYOU","BAN_TYPE",
            "BAN_H1","BAN_H2","BAN_W","BAN_D",
            "KITEN_X","KITEN_Y","DETECT_AREA_X","DETECT_AREA_Y",
            "FRAME_ORG_X","FRAME_ORG_Y","FRAME_MINI_X","FRAME_MINI_Y","SCALE_X","SCALE_Y",
        ],
        "基礎図": [
            "BAN_MENNO","BAN_NO","PAGE","ZUMEI","BAN_MEISYOU","BAN_TYPE",
            "KITEN_X","KITEN_Y","DETECT_AREA_X","DETECT_AREA_Y",
            "FRAME_ORG_X","FRAME_ORG_Y","FRAME_MINI_X","FRAME_MINI_Y","SCALE_X","SCALE_Y",
        ],
        "外形図": [
            "BAN_MENNO","BAN_NO","PAGE","ZUMEI","BAN_MEISYOU","BAN_TYPE",
            "BAN_H1","BAN_H2","BAN_W","BAN_D",
            "KITEN_X","KITEN_Y","DETECT_AREA_X","DETECT_AREA_Y",
            "FRAME_ORG_X","FRAME_ORG_Y","FRAME_MINI_X","FRAME_MINI_Y","SCALE_X","SCALE_Y",
        ],
    }

    def prepare_df_for_export(df_in, columns):
        """OUTPUT_COLUMNS に合わせて列を整列し、存在しない列は空で補う"""
        df_out = df_in.copy()
        for col in columns:
            if col not in df_out.columns:
                df_out[col] = None
        df_out = df_out[columns]
        return df_out

    # 内部機器配置図
    path_cubicle = output_dir / f"{output_prefix}df_cubicle.csv"
    df_cubicle = filter_calc_export(df, "内部機器配置図", assign_detect_area_standard, path_cubicle, OUTPUT_COLUMNS)
    df_cubicle = prepare_df_for_export(df_cubicle, OUTPUT_COLUMNS["内部機器配置図"])
    export_csv(df_cubicle, path_cubicle)

    # 基礎図
    path_foundation = output_dir / f"{output_prefix}df_foundation.csv"
    df_foundation = filter_calc_export(df, "基礎図", assign_detect_area_foundation, path_foundation, OUTPUT_COLUMNS)
    df_foundation = prepare_df_for_export(df_foundation, OUTPUT_COLUMNS["基礎図"])
    df_foundation = df_foundation.sort_values(by=["BAN_MENNO","BAN_NO"], ascending=True)
    export_csv(df_foundation, path_foundation)

    # 外形図
    df_gaikei = df[df["ZUMEI"].astype(str).str.contains("外形図", na=False)].copy()
    df_outline = side_view_classifier(df_gaikei)
    df_outline = prepare_df_for_export(df_outline, OUTPUT_COLUMNS["外形図"])
    df_outline = df_outline.sort_values(by=["BAN_MENNO","BAN_NO","PAGE","DETECT_AREA_X"], ascending=True)
    path_outline = output_dir / f"{output_prefix}df_outline.csv"
    export_csv(df_outline, path_outline)

    return unique_files_no_ext, str(path_cubicle), str(path_foundation), str(path_outline)

# 複数CSVから１つのDFを作成
def merge_and_sort_csvs(csv_paths: List[str],sort_columns: List[str]) -> pd.DataFrame:
    """
    複数のCSVを読み込み、最初のCSVのカラム順を基準に結合し、
    後続CSVに不足しているカラムは末尾に追加して補完する。
    最終的に指定カラムで昇順ソートした1つのDataFrameを作成する。

    CSVの結合時に「PAGE」列が存在する場合は文字列型に統一するため、
    文字列としてのページ番号でのマスクや比較が安全に行える。

    Args:
        csv_paths (List[str]): 読み込むCSVファイルのパスリスト
        sort_columns (List[str]): ソート対象のカラム名リスト（昇順ソート）

    Returns:
        pd.DataFrame: 結合・カラム補完・ソート済みのDataFrame
    """
    if not csv_paths:
        raise ValueError("csv_paths が空です。1つ以上のCSVファイルを指定してください。")

    merged_df = pd.DataFrame()
    final_columns = []

    for i, path in enumerate(csv_paths):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"CSVファイルが存在しません: {path}")
        df = pd.read_csv(path, encoding="cp932")

        if i == 0:
            # 最初のCSVのカラム順を基準に設定
            final_columns = df.columns.tolist()
        else:
            # 不足カラムを補完
            for col in final_columns:
                if col not in df.columns:
                    df[col] = None
            # 新しいCSVにしかないカラムは末尾に追加
            for col in df.columns:
                if col not in final_columns:
                    final_columns.append(col)
                    merged_df[col] = merged_df.get(col, None)

            # 最終的に列順を揃える
            df = df.reindex(columns=final_columns)

        merged_df = pd.concat([merged_df, df], ignore_index=True)

    # PAGE列を文字列に統一
    if "PAGE" in merged_df.columns:
        merged_df["PAGE"] = merged_df["PAGE"].astype(str)

    # ソート
    merged_df = merged_df.sort_values(by=sort_columns, ascending=True)

    return merged_df
