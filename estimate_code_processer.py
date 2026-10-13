# *****************************************************************************
# Explanation:専用モジュール, 標準積算自動化プログラム
# *****************************************************************************
# File Name : estimate_code_process.py # -> import estimate_code_processer as est_mod
# Developer : Satoshi Hashomoto
# Date      : 2025/12/01(Mon)
# Details   : 座標変換用
# Update    : Rep:2025/12/04(Thu), create_detected_csv の検出センター座標をint型へ修正。
# Update    : Rep:2025/12/05(Fri), create_estcode_df_and_csv,

# ##############################################################################
# Library:
# ##############################################################################
# Common:
import glob
import io
import os
from pathlib import Path
import re

# Third Pparty:
import pandas as pd

# ******************************************************************************
# Functions:
# ******************************************************************************
def _is_cp932_encodable(text: str) -> bool:
    try:
        text.encode("cp932")
        return True
    except UnicodeEncodeError:
        return False

# ✔YOLO座標をCAD座標へ変換したCSVを作成
def create_detected_csv(target_dir: str):
    """
    指定フォルダ内の ***_detected.txt を読み取り detected.csv を作成する。\n
    各行は 'Index$0|Device$xxx|Score$0.xxx|Coordinates$...' の形式を想定。\n
    戻り値: (出力CSVパス, 検出結果があったページ番号リスト)
    """

    product_df_path = os.path.join(target_dir, "product_df.csv")
    if not os.path.exists(product_df_path):
        raise FileNotFoundError(f"product_df.csv が存在しません -> {product_df_path = }")

    try:
        product_df = pd.read_csv(product_df_path, encoding="utf-8")
    except UnicodeDecodeError:
        with open(product_df_path, encoding="cp932", errors="ignore") as f:
            content = f.read()
        product_df = pd.read_csv(io.StringIO(content))

    # ★ PAGE は文字列として扱う（重要）
    # カラム名の大文字小文字差を吸収して 'PAGE' を確実に持たせる
    col_map = {c.upper(): c for c in product_df.columns}
    if "PAGE" not in product_df.columns:
        if "PAGE" in col_map:
            product_df.rename(columns={col_map["PAGE"]: "PAGE"}, inplace=True)
        else:
            raise KeyError(f"product_df.csv に 'PAGE' カラムがありません。カラム一覧: {list(product_df.columns)}")
    product_df["PAGE"] = product_df["PAGE"].astype(str)

    rows = []
    detected_pages = []

    txt_files = glob.glob(os.path.join(target_dir, "*_detected.txt"))

    for txt_path in txt_files:
        filename = os.path.basename(txt_path)
        page = filename.replace("_detected.txt", "")

        # ★ page_num を文字列化して比較（product_df が str のため）
        page_key = str(page)

        # product_df の PAGE が str なのでここも str 比較
        df_row = product_df[product_df["PAGE"] == page_key]
        if df_row.empty:
            continue

        scale_x = float(df_row["SCALE_X"].values[0])
        scale_y = float(df_row["SCALE_Y"].values[0])
        frame_org_y = float(df_row["FRAME_ORG_Y"].values[0])

        try:
            with open(txt_path, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue

        page_has_detection = False

        for line in lines:
            line = line.strip()
            if not line:
                continue

            parts = line.split("|")
            if len(parts) < 4:
                continue

            index_match = re.match(r"Index\$(\d+)$", parts[0])
            if not index_match:
                continue
            yolo_index = int(index_match.group(1))

            device_match = re.match(r"Device\$(.+)$", parts[1])
            device = device_match.group(1) if device_match else ""

            score_match = re.match(r"Score\$([0-9.+-eE]+)$", parts[2])
            if not score_match:
                continue
            score = float(score_match.group(1))

            coord_match = re.match(r"Coordinates\$(.+)$", parts[3])
            if not coord_match:
                continue

            coord_parts = coord_match.group(1).split(":")
            if len(coord_parts) != 4:
                continue

            def parse_xy(text):
                x_str, y_str = text.split(",")
                return float(x_str), float(y_str)

            pts = [parse_xy(c) for c in coord_parts]

            def conv_x(x): return int(x * scale_x)
            def conv_y(y): return int(abs(y * scale_y - frame_org_y))

            LT_x, LT_y = conv_x(pts[0][0]), conv_y(pts[0][1])
            RT_x, RT_y = conv_x(pts[1][0]), conv_y(pts[1][1])
            LB_x, LB_y = conv_x(pts[2][0]), conv_y(pts[2][1])
            RB_x, RB_y = conv_x(pts[3][0]), conv_y(pts[3][1])

            center_x = int((LT_x + RT_x) / 2)
            center_y = int((LT_y + LB_y) / 2)

            rows.append({
                # ★ PAGE を文字列で入れる（これが必須）
                "PAGE": page_key,
                "YOLO_INDEX": yolo_index,
                "SCORE": score,
                "DEVICE": device,
                "LEFT_TOP_X": LT_x,
                "LEFT_TOP_Y": LT_y,
                "RIGHT_TOP_X": RT_x,
                "RIGHT_TOP_Y": RT_y,
                "LEFT_BOTTOM_X": LB_x,
                "LEFT_BOTTOM_Y": LB_y,
                "RIGHT_BOTTOM_X": RB_x,
                "RIGHT_BOTTOM_Y": RB_y,
                "CENTER_X": center_x,
                "CENTER_Y": center_y
            })

            page_has_detection = True

        if page_has_detection:
            detected_pages.append(page_key)

    df = pd.DataFrame(rows)

    # ★ 念のため df.PAGE を str に統一（存在しない場合は空文字列を設定）
    if "PAGE" in df.columns:
        df["PAGE"] = df["PAGE"].astype(str)
    else:
        df["PAGE"] = ""
    df = df.sort_values(by="PAGE")

    out_path = os.path.join(target_dir, "detected_df.csv")
    # SekisanNavi は detected_df.csv を CP932 で読むため、CP932(BOMなし)で書き出す。
    # CP932 で表現できない文字(DEVICE のクラス名など)は文字化けさせず、UTF-8 へも切り替えず、
    # 先にメモリ上でエンコードして失敗時はファイルを作らずエラーにする。
    try:
        csv_bytes = df.to_csv(index=False).encode("cp932")
    except UnicodeEncodeError as e:
        bad = sorted({str(v) for v in df["DEVICE"].dropna() if not _is_cp932_encodable(str(v))}) if "DEVICE" in df.columns else []
        raise ValueError(
            f"detected_df.csv を CP932 で書き出せません(SekisanNavi は CP932 で読み込みます)。"
            f"CP932 で表現できない文字を含む DEVICE: {bad}"
        ) from e
    with open(out_path, "wb") as f:
        f.write(csv_bytes)

    print(f"detected.csv を作成しました: {out_path}")
    return df, out_path, detected_pages

# ✔積算コード集計用DF及びCSVを作成
def create_estcode_df_and_csv(box_type: str, target_df: pd.DataFrame, target_dir: str):
    """
    概要:
        積算コード集計用DFおよびCSVを作成する。

    Args:
        box_type (str): 配電盤 箱体型式
        target_df (pd.DataFrame): 座標計算用DF (product_df)
        target_dir (str): 作成CSV保存先ディレクトリ

    Returns:
        (pd.DataFrame, str): (estcode_df, estcode_df_path)
    """
    # ensure target_dir exists
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    # カラム定義（仕様どおり、未指定項目は型を壊さない初期値にする）
    columns = [
        "MODEL", "BAN_MENNO", "BAN_NO", "BAN_MEISYOU",
        "BAN_H", "BAN_W", "BAN_D", "BAN_CONNECT",
        "PANEL", "TRANS", "IN_PANEL", "SHIELD",
        "DOOR_FRONT", "DOOR_BACK", "DOOR_STACK",
        "DOOR_SIDE", "DOOR_SMALL", "FAN_ROOF",
        "FAN_DOOR", "MAIN_LINE", "WIRE_MESH",
        "STACK_PLATE", "DRAWER_DEVICE", "VCT_STAND",
        "BUS_DUCT", "PASSAGE", "INPUT_CU_COEFF",
        "SORT_ORDER"
    ]

    estcode_df = pd.DataFrame(columns=columns)

    # BAN_MENNO のユニーク値（文字列扱いで）
    uniq_ban = target_df["BAN_MENNO"].dropna().unique().tolist()

    # 全体の「基礎図」行を取得（ZUMEI が '基礎図' を含む行）
    base_all = target_df[target_df["ZUMEI"].astype(str).str.contains("基礎図", na=False)].copy()

    # 基礎図が存在する場合、KITEN_Xに基づき順位・最小最大を決定する
    base_order_map = {}   # BAN_MENNO -> sort order (1..n)
    base_connect_map = {} # BAN_MENNO -> BAN_CONNECT string

    if not base_all.empty:
        # 各行に BAN_MENNO を確実に持たせる（string に変換しておく）
        base_all["BAN_MENNO"] = base_all["BAN_MENNO"].astype(str)

        # 並べ替え（KITEN_X 昇順）。NaN は一番後ろに。
        base_all_sorted = base_all.sort_values(by="KITEN_X", na_position="last").reset_index(drop=False)
        # base_all_sorted の index は元行 index を含む列 'index' になっているが重要なのは順序と BAN_MENNO

        # BAN_MENNO ごとの最初に出現した順序をベースに SORT_ORDER を付与（同じ BAN_MENNO が複数行あれば最初の出現順位）
        order = 1
        seen_ban = set()
        for _, row in base_all_sorted.iterrows():
            ban_m = str(row["BAN_MENNO"])
            if ban_m not in seen_ban:
                base_order_map[ban_m] = order
                seen_ban.add(ban_m)
                order += 1

        # 全体で基礎図が1行しかない場合は '箱･単独' を対象の BAN_MENNO に付与
        distinct_base_bans = sorted(list({str(v) for v in base_all_sorted["BAN_MENNO"].tolist()}))
        if len(distinct_base_bans) == 1:
            only_ban = distinct_base_bans[0]
            base_connect_map[only_ban] = "箱･単独"
        else:
            # 全体のKITEN_Xの最小・最大を求める（行単位で）
            # KITEN_XのBAN_MENNO(最小 -> 最大): L -> 中 -> R
            # 注意：同じKITEN_Xで重複する場合は最初に出た方を優先（business rule）
            overall_sorted = base_all.sort_values(by="KITEN_X", na_position="last")
            # get first and last BAN_MENNO among overall_sorted (string)
            first_ban = str(overall_sorted.iloc[0]["BAN_MENNO"])
            last_ban = str(overall_sorted.iloc[-1]["BAN_MENNO"])

            # map defaults to 中 for distinct ones
            for ban in distinct_base_bans:
                base_connect_map[ban] = "箱･中央"
            # override first/last
            base_connect_map[first_ban] = "箱･左右(L)"
            base_connect_map[last_ban] = "箱･左右(R)"

    # --- 各 BAN_MENNO に対して行を作成 ---
    for ban in uniq_ban:
        ban_s = str(ban)
        # target_df 部分抽出（同 BAN_MENNO の最初の行を代表として利用）
        sub = target_df[target_df["BAN_MENNO"].astype(str) == ban_s]
        if sub.empty:
            # もし空なら型を壊さないデフォルト行を入れる
            row = {
                "MODEL": box_type,
                "BAN_MENNO": ban_s,
                "BAN_NO": "",
                "BAN_MEISYOU": "",
                "BAN_H": 0,
                "BAN_W": 0,
                "BAN_D": 0,
                "BAN_CONNECT": "",
                "PANEL": 0, "TRANS": 0, "IN_PANEL": 0, "SHIELD": 0,
                "DOOR_FRONT": 0, "DOOR_BACK": 0, "DOOR_STACK": 0,
                "DOOR_SIDE": 0, "DOOR_SMALL": 0, "FAN_ROOF": 0,
                "FAN_DOOR": 0, "MAIN_LINE": 0, "WIRE_MESH": 0,
                "STACK_PLATE": 0, "DRAWER_DEVICE": 0, "VCT_STAND": 0,
                "BUS_DUCT": 0, "PASSAGE": 0, "INPUT_CU_COEFF": 0.0,
                "SORT_ORDER": 9999
            }
        else:
            # 代表行（先頭行）から値を取る
            rep = sub.iloc[0]
            ban_no = rep["BAN_NO"] if "BAN_NO" in rep else ""
            ban_meisyou = rep["BAN_MEISYOU"] if "BAN_MEISYOU" in rep else ""
            ban_h = int(rep["BAN_H1"]) if "BAN_H1" in rep and not pd.isna(rep["BAN_H1"]) else 0
            ban_w = int(rep["BAN_W"]) if "BAN_W" in rep and not pd.isna(rep["BAN_W"]) else 0
            ban_d = int(rep["BAN_D"]) if "BAN_D" in rep and not pd.isna(rep["BAN_D"]) else 0

            # BAN_CONNECT：base_connect_map に該当があれば使用。なければ空文字
            ban_connect = base_connect_map.get(ban_s, "")

            # SORT_ORDER：base_order_map に該当があれば使用、なければ大きな数値
            sort_order = base_order_map.get(ban_s, 9999)

            row = {
                "MODEL": box_type,
                "BAN_MENNO": ban_s,
                "BAN_NO": ban_no,
                "BAN_MEISYOU": ban_meisyou,
                "BAN_H": ban_h,
                "BAN_W": ban_w,
                "BAN_D": ban_d,
                "BAN_CONNECT": ban_connect,
                "PANEL": 0, "TRANS": 0, "IN_PANEL": 0, "SHIELD": 0,
                "DOOR_FRONT": 0, "DOOR_BACK": 0, "DOOR_STACK": 0,
                "DOOR_SIDE": 0, "DOOR_SMALL": 0, "FAN_ROOF": 0,
                "FAN_DOOR": 0, "MAIN_LINE": 0, "WIRE_MESH": 0,
                "STACK_PLATE": 0, "DRAWER_DEVICE": 0, "VCT_STAND": 0,
                "BUS_DUCT": 0, "PASSAGE": 0, "INPUT_CU_COEFF": 0.0,
                "SORT_ORDER": sort_order
            }

        estcode_df.loc[len(estcode_df)] = row

    # 最終ソート（SORT_ORDER 昇順）
    estcode_df = estcode_df.sort_values(by="SORT_ORDER", ascending=True).reset_index(drop=True)

    # CSV 保存（Windows で Excel 開くことを考え cp932 を採用）
    estcode_df_path = os.path.join(target_dir, "estcode_df.csv")
    estcode_df.to_csv(estcode_df_path, index=False, encoding="cp932")

    return estcode_df, estcode_df_path

# ******************************************************************************
# Functions:AI解析 積算コード estcode_***()
# ******************************************************************************
# 対象品名: 箱･単独 / 左右 / 中
def estcode_box():
    """
    積算コード: 箱体型式
    - 11001 ~ 11148 (OS)
    - 11501 ~ 11648 (IS)
    - 12001 ~ 1pppp044 (SS)
    - 11201 ~ 11442 (OA)
    - 11701 ~ 11942 (IA)

    積算品名:
    - 箱･単独 / 左右 / 中

    Args:
    - input_path (str): パス
    - output_path (str): パス

    Returns:
    - dict(dict) : 矩形情報
    - image_size(tuple) : (幅, 高さ)
    - frame_size(tuple) : (幅, 高さ)p

    Examples:
        >>> func(
            5, 6)
        11

    Note:
        注意事項や注釈など

    """

# ✔対象品名: パネル類(内部機器配置図)
def estcode_panel(source_df: pd.DataFrame,
                  detected_df: pd.DataFrame,
                  edit_df: pd.DataFrame) -> pd.DataFrame:
    """
    panel 検出を集計して edit_df の PANEL を増やす。

    仕様:
        - ZUMEI に "内部機器配置" を含み、
        - BAN_TYPE が "正面図" or "背面図"
        - 同一 PAGE 内で panel を検出したら、1Pageにつき1回のみカウント(重複対策)

    積算コード:
        - 18001 ~ 18016(操作パネル, 内部機器配置図/正面・側面図)
        - 18319 (回転パネル, 内部機器配置図/正面・背面図)

    積算品名:
        - 操作パネル
    """

    # 必要カラムチェック
    required_src = {"BAN_MENNO", "PAGE", "ZUMEI", "BAN_TYPE",
                    "KITEN_X", "KITEN_Y", "DETECT_AREA_X", "DETECT_AREA_Y"}
    required_det = {"PAGE", "DEVICE", "CENTER_X", "CENTER_Y"}

    if missing := (required_src - set(source_df.columns)):
        raise ValueError(f"source_df に必要なカラムがありません: {missing}")

    if missing := (required_det - set(detected_df.columns)):
        raise ValueError(f"detected_df に必要なカラムがありません: {missing}")

    if "BAN_MENNO" not in edit_df.columns:
        raise ValueError("edit_df に 'BAN_MENNO' がありません。")

    # PANEL カラムがなければ追加
    if "PANEL" not in edit_df.columns:
        edit_df["PANEL"] = 0

    # コピー
    src = source_df.copy()
    det = detected_df.copy()
    ed = edit_df.copy()

    # 数値化
    def safe_int(x):
        try:
            return int(x)
        except Exception:
            return None

    src["__PAGE_i"] = src["PAGE"].apply(safe_int)
    src["__BAN_MENNO_i"] = src["BAN_MENNO"].apply(safe_int)

    det["__PAGE_i"] = det["PAGE"].apply(safe_int)
    det["__CX_f"] = pd.to_numeric(det["CENTER_X"], errors="coerce")
    det["__CY_f"] = pd.to_numeric(det["CENTER_Y"], errors="coerce")

    ed["__BAN_MENNO_i"] = ed["BAN_MENNO"].apply(safe_int)

    # source 条件
    src_filt = src[
        src["ZUMEI"].astype(str).str.contains("内部機器配置", na=False)
        & src["BAN_TYPE"].isin(["正面図", "背面図"])
    ]

    # 🔥 追加：同じ PAGE は1回だけ処理
    processed_pages = set()

    # メイン処理
    for _, srow in src_filt.iterrows():

        page_i = srow["__PAGE_i"]
        ban_i = srow["__BAN_MENNO_i"]

        if page_i is None or ban_i is None:
            continue

        # 🔥すでに処理したページならスキップ
        if page_i in processed_pages:
            continue
        processed_pages.add(page_i)

        # 検出領域
        try:
            dx_min = float(srow["KITEN_X"])
            dx_max = dx_min + float(srow["DETECT_AREA_X"])
            dy_min = float(srow["KITEN_Y"])
            dy_max = dy_min + float(srow["DETECT_AREA_Y"])
        except Exception:
            continue

        # 該当する panel 検出
        det_candidates = det[
            (det["__PAGE_i"] == page_i)
            & (det["DEVICE"].astype(str) == "panel")
            & det["__CX_f"].notna()
            & det["__CY_f"].notna()
        ]
        if det_candidates.empty:
            continue

        # ★ページ内で一度でも領域に入れば 1 カウント
        hit = False
        for _, drow in det_candidates.iterrows():
            cx = float(drow["__CX_f"])
            cy = float(drow["__CY_f"])

            if dx_min <= cx <= dx_max and dy_min <= cy <= dy_max:
                hit = True
                break

        if hit:
            mask_edit = ed["__BAN_MENNO_i"] == ban_i
            if mask_edit.any():
                ed.loc[mask_edit, "PANEL"] = ed.loc[mask_edit, "PANEL"].astype(int) + 1

    # 後処理
    ed = ed.drop(columns=[c for c in ed.columns if c.startswith("__")], errors="ignore")
    return ed

# ✔対象品名: トランス(内部機器配置図)
def estcode_transformer(source_df: pd.DataFrame,
                        detected_df: pd.DataFrame,
                        edit_df: pd.DataFrame) -> pd.DataFrame:
    """
    トランス検出を集計して edit_df の TRANS を増やす。

    処理:
        - 1.source_df のうち ZUMEI に "内部機器配置" を含み、かつ BAN_TYPE が "側面図" の行を抽出。

        - 2.各行について検出領域を算出:
            detect_x: KITEN_X ～ KITEN_X + DETECT_AREA_X
            detect_y: KITEN_Y ～ KITEN_Y + DETECT_AREA_Y

        - 3.同一 PAGE かつ DEVICE == "transformer" の detected_df 行を取り出し、
            CENTER_X / CENTER_Y が上記領域内なら、その source 行の BAN_MENNO と同じ行の
            edit_df["TRANS"] を +1（検出毎に加算）する。

        - 4.更新した edit_df を返す。

    注意:
        - source_df, detected_df, edit_df に必要なカラムが存在しない場合は ValueError を送出します。
        - BAN_MENNO / PAGE は整数に変換して比較します（変換できない行はスキップ）。

    概要:
        以下のデータで説明します。各々のカラムは私用するもののみの抜粋です。

        - 1.source_df のZUMEIに"内部機器配置"を含み、かつ、BAN_TYPE が側面図の行を抜粋。

        - 2.各行ごとに物体の検出範囲としてデータ上で
            `detect_x`(source_df/KITEN_X  ~ source_df/KITEN_X+DETECT_AREA_X)と
            `detect_y`(source_df/KITEN_Y  ~ source_df/KITEN_Y+DETECT_AREA_Y)の範囲を持つ。

        - 3.source_dfのPAGEと同じdetected_dfのPAGE かつ DEVICEが "transformer" 行の
            CENTER_X, CENTER_Yが この時のsource_dfのdetect_xとdetect_yの範囲内か判定する。

        - 4.範囲内であれば、その時のsource_dfのBAN_MENNOと同じ値のedit_dfのBAN_MENNO行のカラムTRANSの値を+1する。

        - 5.処理後、edit_dfの内容を更新し、戻り値で返す。

        ---

        >>> source_df

        |BAN_MENNO|BAN_NO|PAGE|ZUMEI             |BAN_TYPE|KITEN_X|KITEN_Y|DETECT_AREA_X|DETECT_AREA_Y|detect_x |detect_y|
        |:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
        |1        |1     |24  |内部機器配置図(1-1)|側面図   |2560   |1000   |2200         |2100         |2560-4760|1000-3100|
        |2        |2     |26  |内部機器配置図(2-1)|側面図   |2560   |1000   |2200         |2100         |2560-4760|1000-3100|
        |3        |3     |28  |内部機器配置図(3-1)|側面図   |2560   |1000   |2200         |2100         |2560-4760|1000-3100|

        >>> detected_df

        |PAGE|DEVICE     |CENTER_X|CENTER_Y|
        |:-:|:-:|:-:|:-:|
        |28  |transformer|4237    |1401    |
        |31  |transformer|1292    |1359    |

        >>> edit_df

        MODEL|BAN_MENNO|BAN_NO|TRANS|
        |:-:|:-:|:-:|:-:|
        IS2K |1        |1     |0    |
        IS2K |2        |2     |0    |
        IS2K |3        |3     |0    |

        >>> edit_df(処理後)

        MODEL|BAN_MENNO|BAN_NO|TRANS
        |:-:|:-:|:-:|:-:|
        IS2K |1        |1     |0
        IS2K |2        |2     |0
        IS2K |3        |3     |`1`

    """

    # 必要カラムチェック
    required_src = {"BAN_MENNO", "PAGE", "ZUMEI", "BAN_TYPE", "KITEN_X", "KITEN_Y", "DETECT_AREA_X", "DETECT_AREA_Y"}
    required_det = {"PAGE", "DEVICE", "CENTER_X", "CENTER_Y"}
    required_edit = {"BAN_MENNO", "TRANS"}

    missing = required_src - set(source_df.columns)
    if missing:
        raise ValueError(f"source_df に必要なカラムがありません: {missing}")
    missing = required_det - set(detected_df.columns)
    if missing:
        raise ValueError(f"detected_df に必要なカラムがありません: {missing}")
    # edit_df の TRANS が無ければ作る（初期値0）。BAN_MENNO が無ければエラー。
    if "BAN_MENNO" not in edit_df.columns:
        raise ValueError("edit_df に 'BAN_MENNO' カラムがありません。")
    if "TRANS" not in edit_df.columns:
        edit_df["TRANS"] = 0

    # 型を合わせる（安全のためコピーして扱う）
    src = source_df.copy()
    det = detected_df.copy()
    ed  = edit_df.copy()

    # PAGE,BAN_MENNO を比較しやすいように整数化を試みる
    def safe_int(x):
        try:
            return int(x)
        except Exception:
            return None

    src["__PAGE_i"] = src["PAGE"].apply(safe_int)
    src["__BAN_MENNO_i"] = src["BAN_MENNO"].apply(safe_int)
    det["__PAGE_i"] = det["PAGE"].apply(safe_int)
    # CENTER_X/Y を float に（できない行は NaN -> 後でスキップ）
    det["__CX_f"] = pd.to_numeric(det["CENTER_X"], errors="coerce")
    det["__CY_f"] = pd.to_numeric(det["CENTER_Y"], errors="coerce")
    ed["__BAN_MENNO_i"] = ed["BAN_MENNO"].apply(safe_int)

    # フィルタ: source の条件
    mask_src = src["ZUMEI"].astype(str).str.contains("内部機器配置", na=False) & (src["BAN_TYPE"] == "側面図")
    src_filt = src[mask_src]

    # 1行ずつ処理
    for _, srow in src_filt.iterrows():
        page_i = srow["__PAGE_i"]
        ban_i  = srow["__BAN_MENNO_i"]

        # 整数変換失敗ならスキップ
        if page_i is None or ban_i is None:
            continue

        # 検出領域
        try:
            dx_min = float(srow["KITEN_X"])
            dx_max = float(srow["KITEN_X"]) + float(srow["DETECT_AREA_X"])
            dy_min = float(srow["KITEN_Y"])
            dy_max = float(srow["KITEN_Y"]) + float(srow["DETECT_AREA_Y"])
        except Exception:
            # 欠損や変換エラーがあればスキップ
            continue

        # 同一PAGEかつ DEVICE == "transformer" を抽出（かつ座標が数値）
        det_candidates = det[
            (det["__PAGE_i"] == page_i) &
            (det["DEVICE"].astype(str) == "transformer") &
            det["__CX_f"].notna() &
            det["__CY_f"].notna()
        ]

        if det_candidates.empty:
            continue

        # 各検出について範囲内か判定。ヒットするごとに edit_df の TRANS を +1
        for _, drow in det_candidates.iterrows():
            cx = float(drow["__CX_f"])
            cy = float(drow["__CY_f"])
            if dx_min <= cx <= dx_max and dy_min <= cy <= dy_max:
                # 対応する edit_df の行を特定（BAN_MENNO が一致する行すべてに加算）
                mask_edit = ed["__BAN_MENNO_i"] == ban_i
                if mask_edit.any():
                    ed.loc[mask_edit, "TRANS"] = ed.loc[mask_edit, "TRANS"].astype(int) + 1
                else:
                    # 一致行がない場合は無視（必要なら新規行作る案もあるが仕様では無視）
                    continue

    # 不要な補助列を除去して返す（元の edit_df と同じ列順に）
    ed = ed.drop(columns=[c for c in ed.columns if c.startswith("__")], errors="ignore")

    # 更新した DataFrame を返す
    return ed

# 対象品名: 扉
def estcode_door():
    """
    積算コード:
    - 18302 (側面扉:1枚追加, 単独左右の時は2でカウント)
    - 18330 (側面扉:なし)
    - 18303 (観音扉: W <= 1100)
    - 18305 (小扉,屋内操作用)
    - 18306 (小扉,屋外操作用)
    - 18307 (段積み扉,2段)
    - 18329 (段積み扉,3段)

    積算品名:
    - 内部パネル

    Note:
        Width = 1100 以下の観音扉
    """

# 対象品名: 盤間の仕切・遮蔽
def estcode_Shield():
    """
    積算コード:
        18201 ~ 18214 (盤間の仕切・遮蔽)
    積算品名:
        盤間仕切板
    Note:
        Hight + Width の値によってコードが変化。
    """

# 対象品名: 附属品加算価格
def estcode_add_accs_price():
    """
    積算コード:\n
         ~ 
    積算品名:\n
        内部パネル
    """

# 対象品名: A1入力主回路銅帯の内部機器の構成による入力係数
def estcode_input_cu():
    """
    積算コード:\n
         ~ 
    積算品名:\n
        内部パネル
    """

# 対象品名: 内部機器配置図/側面図,段積みプレート
def estcode_stack_plate():
    """
    積算コード:\n
        18308 (段積みプレート,2段)
        18309 (段積みプレート,3段)
    積算品名:\n
        内部パネル
    """

# 対象品名: 内部機器配置図/側面図,引出装置
def estcode_drawer_dcevice():
    """
    積算コード:\n
        18310 (引出装置)
    積算品名:\n
        附属品加算価格,引出装置(箱体型式 IA/OA の VCB/VT/LA/ZPC のみ)
    """

# 対象品名: 基礎図/正面図,通路
def estcode_stack_plate():
    """
    積算コード:\n
        18321 (盤内通路,IA/OA)
        18322 (盤内通路,IS/OS)
    積算品名:\n
        附属品加算価格,盤内通路
    """

# ******************************************************************************
# Functions:積算コード(プログラム集計): _2025/12/03
# ******************************************************************************
# 対象品名: 底板
# def   estcode_bottom_plate():

# 対象品名: 箱体価格倍率
# def estcode_add_box_price():

# 対象品名: パネル(A2)
# def estcode_panel_a2():

# 対象品名: OPA用アングル枠
# def estcode_opa_angle():

# 対象品名: 金網
# def estcode_wire_mesh():

# 対象品名: A1入力主回路銅帯
# def estcode_input_cu():
