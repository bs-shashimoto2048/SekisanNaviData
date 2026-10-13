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
#           : Add:2025/12/24(Wed), 図面が他で使用中なら製番処理をスキップする

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
from pipeline import page_plan as page_plan_mod # 印刷・推論の対象ページ選別(純粋な処理)
import print_support                      # 印刷スクリプト(WIRING.SCR)作成: 印刷位置を用紙中央に配置
import inference_support                  # 推論サーバー(別プロセス・別venv)の起動・確認・停止(検証設定でのみ使用)

# ##############################################################################
# Global Constant:
# ################################-##############################################
SCR_PATH = Path(__file__)       # Path:Self Script
MDL_DIR  = SCR_PATH.parent      # Path:Directory
SCR_DIR  = SCR_PATH.parent.name # Dir_Name:Self Script
SCR_NAME = SCR_PATH.stem        # File-Name:Self Script

# ##############################################################################
# Maine Function:
# ##############################################################################
def default_config_path() -> str:
    """設定ファイルの既定。社内の共有パス等を書いたローカル設定 app.local.yaml(Git管理外)があればそれを、無ければ app.yaml(サンプル)。"""
    local = Path(MDL_DIR) / f"{SCR_NAME}.local.yaml"
    return str(local) if local.is_file() else f"{MDL_DIR}/{SCR_NAME}.yaml"

def _assert_not_sample_config(config_data:dict, path:str):
    """サンプルのまま(プレースホルダ '<...>' が残っている)の設定では実行しない。"""
    for section, key in (("data", "output"), ("api", "dir_venv"), ("dwg_conversion", "converter")):
        value = str((config_data.get(section) or {}).get(key, ""))
        if "<" in value:
            raise RuntimeError(
                f"{path} はサンプル設定です({section}.{key} が未設定)。app.local.yaml を作成して実際の値を設定してください"
                f"(手順は docs/setup.md の「ローカル設定」)。")

def _is_under(path:str, root:str) -> bool:
    """path が root の配下(root 自身は不可)か。実体解決後に比較する。"""
    p = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    r = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    try:
        return os.path.commonpath([p, r]) == r and p != r
    except ValueError: # 別ドライブ等
        return False

def _safe_rmtree(target:str, allowed_root:str, **kwargs):
    """削除対象を、設定された出力ルート(output_dir)配下に限定して shutil.rmtree する(Issue #52 安全条件)。"""
    if not _is_under(target, allowed_root):
        raise RuntimeError(f"削除対象が出力ルート配下ではないため削除しません: {target}")
    shutil.rmtree(target, **kwargs)

def main(config_path=None): # 社内図面解析処理
    # 設定初期化
    warnings.simplefilter("ignore", FutureWarning) # 警告出力なし

    # Initialize:Value
    # config_path 省略時は app.local.yaml(Git管理外のローカル設定)があればそれを、無ければ app.yaml(サンプル)を読む。
    # 指定時はそのYAML(検証用 app_test.yaml 等)を読む
    my_yaml_path:str = str(config_path) if config_path is not None else default_config_path()
    config_data = config_loader.load_yaml(my_yaml_path)
    if config_path is None:
        _assert_not_sample_config(config_data, my_yaml_path)
    # -> Set:作業ディレクトリ
    data_dir:str = config_data["data"]["output"]
    # 推論サーバー統合(検証用)。app.yaml には無く、省略時(無効)は従来動作のまま。
    infer_cfg:dict = config_data.get("inference_server") or {}
    infer_enabled:bool = bool(infer_cfg.get("enabled", False))
    if infer_enabled:
        data_dir = os.path.abspath(data_dir) # work_dir をサーバーと共有できる絶対パスにする
    storage:str = config_data["data"]["storage"]
    product_dir = os.path.join(data_dir,storage)
    log_dir:str = os.path.join(product_dir,"logs")
    output_dir:str = os.path.join(product_dir,"output")

    # 推論サーバー統合の設定を、出力フォルダ・ログの作成より前に検証する(本番共有・範囲外ならここで InferenceServerError。何も書かない)
    manager = None
    if infer_enabled:
        settings = inference_support.InferenceServerSettings.from_config(infer_cfg)
        api_cfg_path = os.path.abspath(os.path.join(config_data["api"]["dir_venv"], config_data["api"]["yaml_file"]))
        manager = inference_support.InferenceServerManager(
            settings, output_root=data_dir, config_out=api_cfg_path, log_dir=log_dir)
        manager.validate()
        # 検証モードでは CAD 変換ソフトも本番共有上のものを使わない(ローカルのパスを指定する)
        try:
            inference_support.safety.assert_not_production(
                str(config_data["dwg_conversion"]["converter"]), "dwg_conversion.converter")
        except inference_support.safety.SafetyError as e:
            raise inference_support.InferenceServerError(str(e)) from e

    # -> Set:作業対象図面設定
    target_initial:str = config_data["data"]["initials"]
    box_types:list = tuple(config_data["data"]["box_types"])

    # -> Set:CAD変換設定
    converter_path = config_data["dwg_conversion"]["converter"]        # 変換ソフトパス(共有サーバーへ配置)
    converter_version = config_data["dwg_conversion"]["cad_version"]   # 出力CADバージョン
    converter_extension = config_data["data_extension"]["convert_bfr"] # 変換前CAD拡張子
    converted_extension = config_data["data_extension"]["convert_aft"] # 変換後CAD拡張子

    if not os.path.exists(output_dir): # 出力フォルダが存在しない場合は作成
        os.makedirs(output_dir)

    # Setting:Log
    logger = util_mod.Logger(
        log_directory=log_dir, # ログデータ出力先
        log_level="DEBUG",     # 書出ログレベル:INFO -> WARNING -> ERROR -> DEBUG
        mode="overwrite"        # 書込モード:appendで追記, overwrite\で上書き
        )

    # Log-Title:
    logger.info(f"Get Ready!")
    logger.info(f"{'#'*80}")
    logger.info(f"PROGRAM: {SCR_PATH}\{SCR_NAME}.py")
    logger.info(f"{'#'*80}\n")

    # Log-Process:
    logger.info(f"{'-'*80}")
    logger.info(f"初期化設定:")
    logger.info(f"{'-'*80}\n")

    logger.info(f"グローバル変数:")
    logger.info(f" └> {SCR_PATH = }")
    logger.info(f" └> {MDL_DIR = }")
    logger.info(f" └> {SCR_DIR = }")
    logger.info(f" └> {SCR_NAME = }\n")

    logger.info(f"作業ディレクトリ:")
    logger.info(f" └> root: {data_dir = }")
    logger.info(f" └> データ格納先: {product_dir = }")
    logger.info(f" └> ログ格納先: {log_dir = }")
    logger.info(f" └> 出力先(製番ルート): {output_dir = }\n")

    logger.info(f"対象データ:")
    logger.info(f" └> 製番頭２文字: {target_initial = }")
    logger.info(f" └> 配電盤 箱体型式: {box_types = }\n")

    # 製番ごとの結果。失敗を成功扱いにしないよう、呼び出し元(起動入口)へ戻り値で伝える。
    results:dict = {"completed": [], "failed": {}, "skipped": {}}

    if manager is not None:
        try:
            manager.logger = logger
            logger.info("推論サーバーを起動し、準備完了(health/ready)を確認します。")
            manager.start()
        except Exception as e:
            # 準備できないときは積算処理を開始しない(ホスト照会・設計サーバーへのアクセスもしない)
            logger.error(f"推論サーバーの準備に失敗したため、積算処理を開始しません: {e}")
            logger.info("Process End!!\n")
            raise

    try:
        logger.info(f"{'-'*80}")
        logger.info(f"ホストより当月(～当日)出図完了製番(3番)リスト取得:")
        logger.info(f"{'-'*80}\n")

        # 出図済み製番リスト作成
        logger.info("3番出図完了製番リスト作成:")
        n3_list = host_mod.create_n3_design_list(target_initial)
        logger.info(f" └> Total: {len(n3_list)}, {n3_list = }\n")

        # 処理済み製番を除外
        logger.info("製番(3番出図済み)リスト作成:処理済み製番削除")
        product_list = host_mod.get_missing_folders(output_dir, n3_list)
        logger.info(f" └> Total: {len(product_list)}, {product_list = }\n")

        # >>>>>>>>>>>>>>>>>> DEBUG <<<<<<<<<<<<<<<<<<
        # >    テストは以下で対象製番を指定すること    <
        # >>>>>>>>>>>>>>>>>>>>> <<<<<<<<<<<<<<<<<<<<<
        # product_list = [

        #                 "",]
        # >>>>>>>>>>>>>>>>>>>>> <<<<<<<<<<<<<<<<<<<<<

        logger.info("処理開始\n")
        for product in product_list:
            dir_product = os.path.join(output_dir, product)
            try:
                exit_outer = False # ← 製番単位の中断フラグ
    
                logger.info(f"{'★'*60}")
                logger.info(f"製番処理開始: {product}")
                logger.info(f"{'★'*60}\n")
    
                logger.info("物件情報取得:")
                serial, box_type = host_mod.get_property_data(product)
                logger.info(f" └> 整理番号: {serial = }, 箱体情報: {box_type = }\n")
    
                logger.info("***Check*** ホスト情報のブランク:")
                if not (serial and product and box_type):
                    logger.warning(f"製番情報が取得できませんでした: {product}\n")
                    results["skipped"][product] = "ホスト情報を取得できません"
                    continue
                logger.info(f" └>OK!\n")
    
                logger.info("***Check*** データ作成条件: 1.整理番号ブランクでない, 2.製番A1で始まる, 3.箱体が処理対象型式である")
                logger.info(f" └> 1:{serial = }, 2:{product = }, 3:{box_type = }")
                if serial != "" and product.startswith(target_initial) and any(box_type.startswith(bt) for bt in box_types):
    
                    dir_product = os.path.join(output_dir, product)
                    logger.info(f" └> OK!\n")
    
                    logger.info(f"***Check*** 作業済み")
                    if os.path.exists(dir_product):
                        logger.info(f" └> NG: 処理済みのためスキップ\n")
                        results["skipped"][product] = "処理済み"
                        continue
    
                    logger.info(f" └> OK!: 処理開始\n")
    
                    # Log-Process:
                    logger.info(f"{'-'*60}")
                    logger.info(f"[Step.1] 図面画像及び盤情報DataFrame作成:")
                    logger.info(f"{'-'*60}\n")
    
                    logger.info(f"ディレクトリ作成: 設計DWG図面コピー用")
                    os.makedirs(dir_product) # Create Directory:Output(Srial Number)
                    dwg_copy_dir = os.path.join(dir_product,"dwg")
                    os.makedirs(dwg_copy_dir) # Create Directory:COPY(DWG)
                    logger.info(f" └> {config_loader.convert_win_path(dir_product) = }\n")
    
                    # 整理番号をキーに設計サーバーパスを探索
                    logger.info(f"設計図面パス探索:")
                    dir_design_serial = design_mod.search_server(serial)
                    logger.info(f" └> {config_loader.convert_win_path(dir_design_serial) = }\n")
    
                    # 設計製番パスを作成
                    logger.info(f"設計製番ディレクトリパス作成:")
                    dir_design_product = os.path.join(dir_design_serial, product)
                    logger.info(f" └> {config_loader.convert_win_path(dir_design_product) = }\n")
    
                    # 設計データより解析用DF作成
                    logger.info(f"図面解析用DF作成:")
                    df = cad_mod.load_baninf(dir_design_product)
                    # -> 解析用DF出力、その読込図面ファイル名リストを取得
                    import_dwg_names, csv_c, csv_f, csv_o = cad_mod.export_ban_data(df, dir_product, f"{product}")
                    logger.info(f" └> 対象ページ Total: {len(import_dwg_names)}, {import_dwg_names = }")
                    logger.info(f" └> 対象ページ Total: {len(import_dwg_names)}, {import_dwg_names = }")
                    logger.info(f" └> CSV作成: 内器図 -> {config_loader.convert_win_path(csv_c)}")
                    logger.info(f" └> CSV作成: 基礎図 -> {config_loader.convert_win_path(csv_f)}")
                    logger.info(f" └> CSV作成: 外形図 -> {config_loader.convert_win_path(csv_o)}")
    
                    # -> 座標計算用DF作成
                    csv_list = [csv_c, csv_f, csv_o]
                    sort_by = ["PAGE","BAN_MENNO","BAN_NO","DETECT_AREA_X"]
                    product_df = cad_mod.merge_and_sort_csvs(csv_list, sort_by)
                    product_df_path = os.path.join(dir_product,"product_df.csv")
                    cad_mod.export_csv(product_df, product_df_path)
                    logger.info(f" └> CSV作成: 座標計算用 -> {config_loader.convert_win_path(csv_o)}")
                    logger.info(f" └> DataFrame作成(解析座標復元用): product_df \n")
    
                    # 共通製作仕様書印刷 -> あれば焼く_2025/11/28
                    logger.info(f"共通製作仕様書 印刷:")
                    spec_sheets = ["1"]  # 共通製作仕様書(一般的な場)
                    spec_sheets_name = spec_sheets[0] + converter_extension
                    serial_Spec_dir = os.path.join(dir_design_serial, f"{serial}S0")
                    if cad_mod.file_exists(serial_Spec_dir, spec_sheets_name):
                        serial_script_win = print_support.output_print_script(spec_sheets, serial_Spec_dir, dir_product)
                        design_mod.print_dwg_for_pdf(serial_script_win, len(spec_sheets))
                        cad_mod.countdown(3)
                        pdf_name = os.path.join(dir_product, "1.pdf")
                        pdf_rename = os.path.join(dir_product, "共通製作仕様書.pdf")
                        if os.path.exists(pdf_name):
                            os.rename(pdf_name, pdf_rename)
                            logger.info(f" └> {pdf_rename = }\n")
                    else:
                        logger.warning(f" └> 共通製作仕様書が見つかりませんでした: {serial_Spec_dir}\n")
    
                    # Log-Process: 解析用画像作成
                    logger.info(f"{'-'*60}")
                    logger.info(f"[Step.2] 推論用図面画像作成処理:")
                    logger.info(f"{'-'*60}\n")
    
                    logger.info(f"対象ページ取得:")
                    # X/x 始まりの図面は、印刷・画像化・推論・データ作成のいずれにも入れない。選別は pipeline.page_plan
                    plan = page_plan_mod.build_page_plan(product_df, dir_design_product, converter_extension)
                    product_df = plan.product_df
                    baninf_pages = plan.inference_pages
                    logger.info(f" └> 推論対象ページ(従来の選び方、X始まりを除く): {baninf_pages = }")

                    # 印刷対象: 設計製番フォルダの図面のうちX/x始まりを除く全ページ。推論対象以外はPDF印刷だけ行う
                    print_pages = plan.print_pages
                    pdf_only_pages = plan.pdf_only_pages
                    logger.info(f" └> 印刷対象ページ({len(print_pages)}件): {print_pages = }")
                    logger.info(f" └> PDF印刷のみのページ({len(pdf_only_pages)}件): {pdf_only_pages = }\n")
                    if plan.status != page_plan_mod.OK:
                        if plan.status == page_plan_mod.MISSING_DRAWINGS:
                            logger.warning(f"推論対象ページの図面が設計フォルダにありません: {plan.missing_pages}")
                            results["failed"][product] = f"推論対象の図面がありません: {plan.missing_pages}(未完了)"
                        else:
                            logger.warning(f"推論対象ページがありません(印刷対象 {len(print_pages)} 件): {product}")
                            results["skipped"][product] = "推論対象ページなし"
                        _safe_rmtree(dir_product, output_dir, ignore_errors=True)
                        continue # 次の製番へ
    
                    logger.info(f" 図面印刷: DWG -> PDF(300dpi, 4961px X 3508px、印刷位置は用紙中央)")
                    logger.info(f" ※ IJCADの負荷軽減のため5枚ずつ処理する")
                    for chunk_5 in cad_mod.chunk_list(print_pages):
                        # チェック:図面使用
                        for torken in chunk_5:
                            page_path = os.path.join(
                                dir_design_product,
                                f"{torken}{converter_extension}"
                                )
                            if cad_mod.is_file_in_use(page_path):
                                logger.warning(f"図面使用中のため製番をスキップ: {page_path}")
                                exit_outer = True
                                break  # chunk_5 を抜ける
                        if exit_outer:
                            break  # chunk_list を抜ける
    
                        product_script_win = print_support.output_print_script(chunk_5, dir_design_product, dir_product)
                        design_mod.print_dwg_for_pdf(product_script_win, len(chunk_5))
                        logger.info(f" └> Page: {chunk_5}")
                        cad_mod.countdown(len(chunk_5)*4)
                    if exit_outer:
                        logger.warning(f"図面使用中のため製番をスキップします: {product}")
                        _safe_rmtree(dir_product, output_dir, ignore_errors=True)
                        results["skipped"][product] = "図面使用中(未完了)"
                        continue  # ← 次の製番へ
                    logger.info(f"印刷完了!\n")
    
                    logger.info(f"CAD図面(DWG)コピー:")
                    logger.info(f" ※ ODAFileConverter 26.10.0 はディレクトリ単位でしか処理できない")
                    cad_mod.copy_files_from_list(dir_design_product, dwg_copy_dir, baninf_pages, converter_extension)
                    logger.info(f" └> コピー元(設製番計フォルダ): {dir_design_product}")
                    logger.info(f" └> コピー先(プログラム製番フォルダ): {dwg_copy_dir}")
                    logger.info(f" └> 対象ページ: {baninf_pages}\n")
    
                    # 図面変換処理
                    logger.info(f"図枠寸法取得用図面データ変換: DWG -> DXF")
                    cad_mod.convert_dwg2dxf(dwg_copy_dir, dir_product, converter_path, converter_version)
                    logger.info(f" └> 変換元: {dwg_copy_dir}")
                    logger.info(f" └> 変換先: {dir_product}")
                    logger.info(f" └> 対象ページ: {baninf_pages}\n")
    
                    # PDF→JPEG→PNG変換
                    logger.info(f"推論用画像作成: PDF -> JPEG(2481px X 1754px)\n")
                    image_mod.pdf_to_images(dir_product, baninf_pages)
    
                    logger.info(f"推論用画像前処理: JPEG -> PNG(Gaus,Bedian,Sharp,Bilateral,Binary,Crop)\n")
                    image_mod.process_images_from_jpeg(dir_product, baninf_pages)
    
                    # Log-Process: 座標計算用図面&DF作成
                    logger.info(f"{'-'*60}")
                    logger.info(f"[Step.3] 解析用図面座標及び縮尺算出:")
                    logger.info(f"{'-'*60}\n")
    
                    img_ext = config_data["data_extension"]["original_draw"] # 拡張子: 対象図面
    
                    # 画像解析・座標算出用に図枠外余白をカット(CADのX,y座標へ画像サイズを合わせる)
                    for page in baninf_pages:
                        logger.info(f" └>対象図面: {page + img_ext}")
    
                        logger.info(f"\t\t├元図枠情報:")
                        dxf_path = os.path.join(dir_product, f"{page}{converted_extension}")
                        origin_frame_width, origin_frame_height = cad_mod.get_frame_size(dxf_path)
                        if origin_frame_width == 0 or origin_frame_height == 0:
                            logger.warning(f"\t └> 図枠サイズが取得できませんでした: {page}\n")
                            exit_outer = True
                            continue # 次の製番へ
    
                        logger.info(f"\t\t│	└> 図枠サイズ(w,h): {origin_frame_width},{origin_frame_height}")
    
                        logger.info(f"\t\t├解析用画像情報:")
                        # 画像の前処理: CAD図枠を検出し図枠外余白カットし画像を上書き
                        img_path = os.path.join(dir_product, page + img_ext)
                        frame_data = cad_mod.specified_frame_detection(img_path)
                        # 元図面図枠サイズ取得
                        image_width, image_height = map(int, frame_data["image_size"])
                        image_frame_width, image_frame_height = map(int, frame_data["frame_size"])
                        logger.info(f"\t\t│	└> 画像サイズ(w,h): {image_width},{image_height}")
                        logger.info(f"\t\t│	└> 図枠サイズ(w,h): {image_frame_width},{image_frame_height}")
    
                        # 縮尺計算
                        scale_x = origin_frame_width / image_frame_width
                        scale_y = origin_frame_height / image_frame_height
                        logger.info(f"\t\t└図面縮尺計算:")
                        logger.info(f"\t\t 	└> 縮尺情報(Scale_x,Scale_y): {scale_x} ,{scale_y}")
    
                        # 盤情報更新
                        product_df = cad_mod.update_frame_info(
                            product_df,          # DataDFrame:盤情報
                            page,                # ページ番号
                            origin_frame_width,  # オリジナル図面図枠サイズ:X
                            origin_frame_height, # オリジナル図面図枠サイズ:Y
                            image_frame_width,   # 解析用縮小図面図枠サイズ:X
                            image_frame_height,  # 解析用縮小図面図枠サイズ:Y
                            scale_x,             # 図枠縮尺:X
                            scale_y,             # 図枠縮尺:Y
                            )
                        logger.info(f" 更新: product_df\n")
    
                    if exit_outer:
                        logger.warning(f"作業対象外のためディレクトリを削除します: {dir_product}\n^")
                        _safe_rmtree(dir_product, output_dir, ignore_errors=True)
                        results["failed"][product] = "図枠サイズを取得できません(未完了)"
                        continue # 次の製番へ
    
                    # Update:盤情報CSV
                    cad_mod.export_csv(product_df, product_df_path)
                    logger.info(f"DataFrame上書き: product_df\n")
    
                    # 画像解析: YOLOv8(Ultralytics) + SAHI
                    logger.info(f"{'-'*60}")
                    logger.info(f"[Step.4] 積算対象オブジェクト検出:")
                    logger.info(f"{'-'*60}\n")
    
                    # API解析リクエスト
                    logger.info(f"APIリクエスト:")
                    api_cnf_path = os.path.join(config_data["api"]["dir_venv"], config_data["api"]["yaml_file"])
                    logger.info(f" └> API問合せ設定: {api_cnf_path = }")
                    logger.info(f" └> 対象フォルダ: {dir_product = }")
                    logger.info(f" └> 解析対象ページ: {baninf_pages = }\n")
    
                    # APIサーバーへ解析画像リクエスト送信
                    logger.info(f"APIサーバーへ解析画像リクエスト送信:")
                    # api.use_proxy は検証設定(app_test.yaml)のみ false を想定。省略時は従来どおりプロキシ使用
                    use_proxy = config_data["api"].get("use_proxy", True)
                    request_kwargs = {}
                    if manager is not None:
                        # 推論サーバーと work_dir が同じ場所を指すこと(目印ファイルが双方から見えること)を確認してから依頼する
                        inference_support.verify_shared_work_dir(manager.base_url, dir_product)
                        logger.info(f" └> work_dir をサーバーと共有できていることを確認: {dir_product = }")
                        request_kwargs["timeout"] = settings.request_timeout
                    # 失敗(HTTPエラー・接続失敗・検出ファイル未生成)は InferenceRequestError で送出され、この製番は失敗として扱われる
                    log_text = image_mod.slice_prediction_request(dir_product, baninf_pages, api_cnf_path, use_proxy=use_proxy, **request_kwargs)
                    if log_text is None:
                        logger.warning("API request log is None.")
                        log_text = ""
                    elif isinstance(log_text, (bytes, bytearray)):
                        log_text = log_text.decode("utf-8", errors="replace")
                    elif not isinstance(log_text, str):
                        logger.warning(f"Unexpected API request log type: {type(log_text)}")
                        log_text = str(log_text)
                    formatted_log = image_mod.log_formater(log_text) if log_text else ""
                    logger.info(f" └> リクエスト内容:\n{formatted_log}\n")
                    print(f"{log_text = }")
    
                    # YOLO検出結果よりCAD座標へ変換したデータを作成
                    # - 積算の判断で用いる座標はYOLOで得られた座標を元座標へ直して検出する
                    # - 座標は各図面(ページ)毎に縮尺を算出し直す。
                    logger.info(f"作成:検出結果をCAD座標へ変換したデータ")
                    # detected_csv_path = est_mod.create_detected_csv(dir_product)
                    detected_df, detected_csv_path, discovery_pages = est_mod.create_detected_csv(dir_product)
                    logger.info(f" └>出力ファイル :{detected_csv_path = }")
                    logger.info(f" └>積算シンボル検出ページ :{discovery_pages = }\n")
    
                    # :積算コード集計処理
                    logger.info(f"{'-'*60}")
                    logger.info(f"[Step.5] 積算コード集計処理:")
                    logger.info(f"{'-'*60}\n")
    
                    # 作成:YOLO検出結果集計用DF
                    logger.info(f"作成:YOLO検出結果集計用DF")
                    estcode_df, estcode_df_path = est_mod.create_estcode_df_and_csv(box_type,product_df,dir_product)
                    logger.info(f" └>出力ファイル :{estcode_df_path = }")
    
                    # 検出オブジェクト毎に積算コードを集計
                    logger.info(f"作成:積算コード集計用DataFrame")
    
                    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
                    # ↑まで編集済み 2025/12/05
                    # +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
    
                    # 操作パネル
                    estcode_df = est_mod.estcode_panel(product_df,detected_df,estcode_df)
                    # 盤内トランス
                    estcode_df = est_mod.estcode_transformer(product_df,detected_df,estcode_df)
    
                    # :
    
                    # Update:盤情報CSV
                    cad_mod.export_csv(estcode_df, estcode_df_path)
                    logger.info(f"DataFrame上書き: estcode_df\n")
                    results["completed"].append(product)
    
                else:
                    # 処理対象でないときはディレクトリ作成されないのでスルー
                    logger.info(f"処理対象製番ではありません -> {product}\n")
                    results["skipped"][product] = "処理対象外"
    
            except Exception as e:
                logger.error(f"Process Failed for product {product}: {e}\n")
                logger.error(traceback.format_exc())
                results["failed"][product] = f"{type(e).__name__}: {str(e)[:200]}"
                if isinstance(e, image_mod.InferenceRequestError) and e.log_text:
                    logger.error(f"推論APIの失敗内容(製番 {product} は未完了):\n{image_mod.log_formater(e.log_text)}")
                if os.path.exists(dir_product):
                    try:
                        _safe_rmtree(dir_product, output_dir)
                        logger.info(f"Directory removed due to product error: {dir_product}")
                    except Exception as del_error:
                        logger.error(f"Directory cleanup error: {del_error}")
                continue
    except Exception as e:
        logger.error(f"Process Failed: {e}\n")
        logger.error(traceback.format_exc())
        results["failed"]["(全体)"] = f"{type(e).__name__}: {str(e)[:200]}"
        if "dir_product" in locals() and dir_product and os.path.exists(dir_product):
            try:
                _safe_rmtree(dir_product, output_dir)
                logger.info(f"Directory removed due to product error: {dir_product}")
            except Exception as del_error:
                logger.error(f"Directory cleanup error: {del_error}")

    finally:
        if manager is not None:
            manager.stop() # 自分が起動した推論サーバー(子プロセス)だけを停止する
        logger.info(f"結果: 完了={len(results['completed'])}, 失敗={len(results['failed'])}, スキップ={len(results['skipped'])}")
        for prod, reason in results["failed"].items():
            logger.error(f"失敗した製番: {prod} -> {reason}")
        logger.info("Process End!!\n")

    return results

def _cli(argv=None) -> int:
    """起動入口。--config 省略時は従来どおり app.yaml。失敗した製番または準備失敗があれば終了コード 1。"""
    import argparse
    ap = argparse.ArgumentParser(description="積算用バックデータ作成(推論サーバー統合起動は設定ファイルの inference_server で指定)")
    ap.add_argument("--config", default=None, help="設定ファイル(省略時は app.yaml)")
    args = ap.parse_args(argv)
    try:
        res = main(config_path=args.config)
    except inference_support.InferenceServerError as e:
        print(f"推論サーバーの準備に失敗したため終了します: {e}", file=sys.stderr)
        return 1
    return 1 if res["failed"] else 0

if __name__ == "__main__":
    sys.exit(_cli())
