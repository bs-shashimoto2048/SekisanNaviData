# ##############################################################################
# Program: 積算工数自動取得(A製品)
# ##############################################################################
# File Name : app.py
# Developer : 2025-11-13, Satoshi Hashomoto
# Purpose   : 積算工数計算の自動化(A製品)
# Details   : 社内図面へAI物体検出を行い、工数算出用の情報の収集に務める
# Update    : Add:2025/11/18(Tue), ホスト情報取得処理
#           : Add:2025/11/19(Wed), 解析用DF及び図面画像の作成,前処理
#           : Add:2025/11/21(Fri), 解析API送信、解析情報作成
#           : Add:2025/11/26(Wed), DXF図面変換処理
#           : Add:2025/11/27(Thu), 座標計算用DF作成と一部モジュールの修正
#           : Add:2025/11/28(Fri), ログ情報書き出しの修正と一部モジュールの修正
#           : Add:2025/12/01(Mon), 積算座標情報変換DF作成

# ##############################################################################
# Library:
# ##############################################################################
# Common:
from datetime import datetime,timedelta
import os
from pathlib import Path
import re
import shutil    # ディレクトリごと削除するために使用
import sys
import time
import traceback # トレースバック出力に使用
import warnings
# Third Pparty:
import cv2          # opencv-python
import pandas as pd # pandas
# Betsukawa_Packages: 社内共通モジュール
from bs_pkg import config_loader, design_mod, host_mod, image_mod, util_mod
# Dedicated Module: 専用モジュール, スクリプト内の処理は全て外出しのモジュールで行う。
import cad_data_process as cad_mod        # CAD/設計データ・画像解析用
import estimate_code_processer as est_mod # 積算コード集計・算出用

# ##############################################################################
# Global Constant:
# ################################-##############################################
SCR_PATH = Path(__file__)       # Path:Self Script
MDL_DIR  = SCR_PATH.parent      # Path:Directory
SCR_DIR  = SCR_PATH.parent.name # Dir_Name:Self Script
SCR_NAME = SCR_PATH.stem        # File-Name:Self Script


def build_target_pages_by_missing_output(
    pages: list[str], output_dir: str, extension: str
) -> tuple[list[str], list[str]]:
    """
    対象ページのうち、出力ファイルが未作成のページのみを抽出する。
    """
    pending_pages = []
    completed_pages = []

    for page in pages:
        output_path = os.path.join(output_dir, f"{page}{extension}")
        if os.path.exists(output_path):
            completed_pages.append(page)
        else:
            pending_pages.append(page)

    return pending_pages, completed_pages




dir_design_product = os.environ.get("SEKISAN_DESIGN_PRODUCT_DIR", r"<DESIGN_PRODUCT_DIR>")  # 設計製番フォルダ(環境変数で指定。実パスはリポジトリに書かない)
baninf_pages = [
    "04","15"
    # "00","01","02","021","022",
    # "023","024","025","03","04",
    # "05","06","07","08","09","10",
    # "11","12","13","14","15",
    # "16","17",
    # "18",
    # "180","181",
    # "1810","1811","1812","1813","1814",
    # "1815","1816","1817","1818","1819",
    # "182","1820","1821","1822","1823",
    # "1824","1825","1826","1827","1828",
    # "1829","183","184","185","186",
    # "187","188","189","19"
    ]
dir_product = os.environ.get("SEKISAN_OUTPUT_DIR", r"<OUTPUT_DIR>")  # 出力フォルダ(環境変数で指定)

print(f" 図面印刷: DWG -> PDF(300dpi, 4961px X 3508px)")
print(f" ※ IJCADの負荷軽減のため5枚ずつ処理する")
print_pages, printed_pages = build_target_pages_by_missing_output(
    baninf_pages, dir_product, ".pdf"
)
print(f" 既印刷図面: {printed_pages}")
print(f" 印刷対象図面: {print_pages}")
if not print_pages:
    print(" 印刷対象なし(全ページ印刷済み)")
else:
    for chunk_5 in cad_mod.chunk_list(print_pages):
        product_script_win = design_mod.output_print_script(chunk_5, dir_design_product, dir_product)
        design_mod.print_dwg_for_pdf(product_script_win, len(chunk_5))
        print(f" └> Page: {chunk_5}")
        cad_mod.countdown(len(chunk_5)*4)
print(f"印刷完了!\n")

# PDF→JPEG→PNG変換
print(f"推論用画像作成: PDF -> JPEG(2481px X 1754px)\n")
jpeg_pages, existing_jpeg_pages = build_target_pages_by_missing_output(
    baninf_pages, dir_product, ".jpeg"
)
print(f" 既作成JPEG図面: {existing_jpeg_pages}")
print(f" JPEG作成対象図面: {jpeg_pages}")
if not jpeg_pages:
    print(" JPEG作成対象なし(全ページ作成済み)")
else:
    image_mod.pdf_to_images(dir_product, jpeg_pages)

print(f"推論用画像前処理: JPEG -> PNG(Gaus,Bedian,Sharp,Bilateral,Binary,Crop)\n")
png_pages, existing_png_pages = build_target_pages_by_missing_output(
    baninf_pages, dir_product, ".png"
)
print(f" 既作成PNG図面: {existing_png_pages}")
print(f" PNG作成対象図面: {png_pages}")
if not png_pages:
    print(" PNG作成対象なし(全ページ作成済み)")
else:
    image_mod.process_images_from_jpeg(dir_product, png_pages)

img_ext = ".png" # 拡張子: 対象図面
exit_outer = False

# 画像解析・座標算出用に図枠外余白をカット(CADのX,y座標へ画像サイズを合わせる)
for page in baninf_pages:
    print(f" └>対象図面: {page + img_ext}")

    # 画像の前処理: CAD図枠を検出し図枠外余白カットし画像を上書き
    img_path = os.path.join(dir_product, page + img_ext)
    frame_data = cad_mod.specified_frame_detection(img_path)

from PIL import Image

OUTPUT_DIR = os.path.join(dir_product,"binary")   # 出力フォルダ
WHITE_THRESHOLD = 250           # 白判定のしきい値（2値化済みなら255でOK）

os.makedirs(OUTPUT_DIR, exist_ok=True)

# 処理
for filename in os.listdir(dir_product):
    if not filename.lower().endswith(".png"):
        continue

    input_path = os.path.join(dir_product, filename)
    output_path = os.path.join(OUTPUT_DIR, filename)

    # 画像をRGBAで読み込み
    img = Image.open(input_path).convert("RGBA")
    pixels = img.getdata()

    new_pixels = []

    for r, g, b, a in pixels:
        # 白背景判定
        if r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD:
            # 白 → 完全透過
            new_pixels.append((255, 255, 255, 0))
        else:
            # 黒（図形） → 不透明
            new_pixels.append((r, g, b, 255))

    img.putdata(new_pixels)
    img.save(output_path, "PNG")

    print(f"変換完了: {filename}")



print("処理が完了しました。")
