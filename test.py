# Common:
import io
import os
import glob
import re

# Third Pparty:
import pandas as pd


def create_estcode_df_and_csv(box_type: str, target_df: pd.DataFrame, target_dir: str):
    """
    概要:
        積算コード集計用 DF および CSV を作成する。

    Args:
        box_type (str): 配電盤 箱体型式
        target_df (pd.DataFrame): 座標計算用 DF（product_df）
        target_dir (str): 生成 CSV の保存先ディレクトリ

    Returns:
        pd.DataFrame: 作成した estcode_df
        str: 保存した CSV のパス
    """

    # =============================
    # 1. BAN_MENNO のユニーク取得
    # =============================
    uniq_ban = target_df["BAN_MENNO"].dropna().unique()

    # =============================
    # 2. 初期 DF 作成（カラム定義）
    # =============================
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

    # =============================
    # 3. BAN_MENNO ごとに行を作成
    # =============================
    for ban in uniq_ban:

        sub = target_df[target_df["BAN_MENNO"] == ban]

        # ---- BAN_CONNECT 判定 ----
        base_rows = sub[sub["ZUMEI"] == "基礎図"]

        if len(base_rows) == 1:
            ban_connect = "箱･単独"
            sort_map = {base_rows.index[0]: 1}
        else:
            # KITEN_X 昇順で並べる
            sorted_base = base_rows.sort_values("KITEN_X")

            # 最小が左、最大が右、それ以外が中
            min_idx = sorted_base.index[0]
            max_idx = sorted_base.index[-1]

            ban_connect_map = {}
            sort_map = {}

            for i, (idx, row) in enumerate(sorted_base.iterrows(), start=1):
                sort_map[idx] = i
                if idx == min_idx:
                    ban_connect_map[idx] = "箱･左右(L)"
                elif idx == max_idx:
                    ban_connect_map[idx] = "箱･左右(R)"
                else:
                    ban_connect_map[idx] = "箱･中"

            # BAN_CONNECT は「該当 BAN の中で基礎図の行が複数あればその判断」
            # BAN_MENNO は 1 行なので代表 1 行を採用
            ban_connect = ban_connect_map[min_idx]

        # =========================
        # 4. 行を構築
        # =========================

        row = {
            "MODEL": box_type,
            "BAN_MENNO": ban,
            "BAN_NO": sub["BAN_NO"].iloc[0] if "BAN_NO" in sub else "",
            "BAN_MEISYOU": sub["BAN_MEISYOU"].iloc[0] if "BAN_MEISYOU" in sub else "",
            "BAN_H": int(sub["BAN_H1"].iloc[0]) if "BAN_H1" in sub else 0,
            "BAN_W": int(sub["BAN_W"].iloc[0]) if "BAN_W" in sub else 0,
            "BAN_D": int(sub["BAN_D"].iloc[0]) if "BAN_D" in sub else 0,
            "BAN_CONNECT": ban_connect,

            # 以下は未指定 → データ型を壊さない初期値
            "PANEL": 0,
            "TRANS": 0,
            "IN_PANEL": 0,
            "SHIELD": 0,
            "DOOR_FRONT": 0,
            "DOOR_BACK": 0,
            "DOOR_STACK": 0,
            "DOOR_SIDE": 0,
            "DOOR_SMALL": 0,
            "FAN_ROOF": 0,
            "FAN_DOOR": 0,
            "MAIN_LINE": 0,
            "WIRE_MESH": 0,
            "STACK_PLATE": 0,
            "DRAWER_DEVICE": 0,
            "VCT_STAND": 0,
            "BUS_DUCT": 0,
            "PASSAGE": 0,
            "INPUT_CU_COEFF": 0.0,
            "SORT_ORDER": min(sort_map.values()) if len(sort_map) > 0 else 1
        }

        estcode_df.loc[len(estcode_df)] = row

    # =============================
    # 5. SORT_ORDER でソート
    # =============================
    estcode_df = estcode_df.sort_values("SORT_ORDER").reset_index(drop=True)

    # =============================
    # 6. CSV 保存
    # =============================
    estcode_df_path = os.path.join(target_dir, "estcode_df.csv")
    estcode_df.to_csv(estcode_df_path, index=False, encoding="utf-8")

    return estcode_df, estcode_df_path
