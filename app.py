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
import os
from pathlib import Path
import shutil    # テストが app.shutil.rmtree を差し替える(削除の実体は pipeline.product_flow)
import sys
import traceback # トレースバック出力に使用
import warnings
# Betsukawa_Packages: 社内共通モジュール
# ※ config_loader・design_mod・host_mod・image_mod と下の cad_mod・print_support は、このファイル自身では使わないが、
#    テストが mock.patch.object(app.<名前>, ...) で差し替える接点(同じモジュールオブジェクトを指す)なので削除しない。
from bs_pkg import config_loader, design_mod, host_mod, image_mod, util_mod
# Dedicated Module: 専用モジュール, スクリプト内の処理は全て外出しのモジュールで行う。
import cad_data_process as cad_mod        # CAD/設計データ・画像解析用(テストの差し替え点)
from pipeline import product_flow     # 製番 1 件の処理(Step.1〜5)・結果記録・失敗時の後始末
from pipeline import settings as settings_mod # 設定の読込・検証(サンプル設定の拒否、出力先、推論サーバー設定の検証)
import print_support                      # 印刷スクリプト(WIRING.SCR)作成(テストの差し替え点)
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
    """設定ファイルの既定。ローカル設定 app.local.yaml(Git管理外)があればそれを、無ければ app.yaml(サンプル)。実体は pipeline.settings。"""
    return settings_mod.default_config_path(MDL_DIR, SCR_NAME)

# サンプル設定の拒否は pipeline.settings(旧名のまま使えるように別名を残す)
_assert_not_sample_config = settings_mod.assert_not_sample_config

# 出力ルート配下限定の削除(旧名のまま使えるように別名を残す。実体は pipeline.product_flow)
_is_under = product_flow.is_under
_safe_rmtree = product_flow.safe_rmtree

def main(config_path=None): # 社内図面解析処理
    # 設定初期化
    warnings.simplefilter("ignore", FutureWarning) # 警告出力なし

    # Initialize:Value
    # config_path 省略時は app.local.yaml(Git管理外のローカル設定)があればそれを、無ければ app.yaml(サンプル)を読む。
    # 指定時はそのYAML(検証用 app_test.yaml 等)を読む。読込・サンプル設定の拒否・出力先の決定は pipeline.settings
    cfg = settings_mod.load_settings(config_path, default_path=default_config_path)
    config_data = cfg.config_data
    # -> Set:作業ディレクトリ
    data_dir:str = cfg.data_dir
    product_dir = cfg.product_dir
    log_dir:str = cfg.log_dir
    output_dir:str = cfg.output_dir

    # 推論サーバー統合の設定を、出力フォルダ・ログの作成より前に検証する(本番共有・範囲外ならここで InferenceServerError。何も書かない)
    # 検証モードでは CAD 変換ソフトも本番共有上のものを使わない。無効(既定)のときは (None, None) で従来動作のまま。
    manager, settings = settings_mod.build_inference_manager(cfg)

    # -> Set:作業対象図面設定
    target_initial:str = cfg.target_initial
    box_types:list = cfg.box_types

    # -> Set:CAD変換設定
    converter_path = cfg.converter_path               # 変換ソフトパス(共有サーバーへ配置)
    converter_version = cfg.converter_version         # 出力CADバージョン
    converter_extension = cfg.converter_extension     # 変換前CAD拡張子
    converted_extension = cfg.converted_extension     # 変換後CAD拡張子

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
            # 製番 1 件の処理(Step 単位の関数)は pipeline.product_flow。結果の記録と失敗時の後始末もそちらで行う
            ctx = product_flow.ProductContext(
                product=product, output_dir=output_dir, dir_product=dir_product, logger=logger, config_data=config_data,
                target_initial=target_initial, box_types=box_types,
                converter_path=converter_path, converter_version=converter_version,
                converter_extension=converter_extension, converted_extension=converted_extension,
                manager=manager, request_timeout=settings.request_timeout if manager is not None else None)
            product_flow.run_product(ctx, results)
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
