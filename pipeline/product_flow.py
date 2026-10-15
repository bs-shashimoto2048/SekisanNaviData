# 製番 1 件分の処理(app.main() から切り出した Step 単位の関数)。S2 の成果。
#
# 方針: 処理順・ログ・出力ファイル・失敗/スキップの扱いは、切り出し前の app.main() と同じ。
# - 各 Step は ProductContext を読み書きし、「この製番はここで終わり」なら ProductResult を返し、続行なら None を返す。
# - 失敗時の製番フォルダ削除と、結果(完了・失敗・スキップ)の記録は run_product() の 1 か所だけで行う。
#   後始末の種類は 2 つ: ProductResult.cleanup=True(ignore_errors=True の静かな削除)と、例外時の削除(エラー内容をログに出す)。
# - 外部モジュール(cad_data_process・bs_pkg の各 *_mod・print_support など)は、テストが mock.patch.object で差し替える
#   モジュール属性を経由して呼ぶ。`from x import f` の形にしない。
import os
import shutil
import traceback
from dataclasses import dataclass, field
from typing import Any, List, Optional

import pandas as pd
from bs_pkg import config_loader, design_mod, host_mod, image_mod

import cad_data_process as cad_mod        # CAD/設計データ・画像解析用
import estimate_code_processer as est_mod # 積算コード集計・算出用
import inference_support                  # 推論サーバー(別プロセス)との作業フォルダ共有確認
import print_support                      # 印刷スクリプト(WIRING.SCR)作成: 印刷位置を用紙中央に配置
from pipeline import inference_client       # 推論 API クライアント層(推論に関する image_mod への依存はここだけ)
from pipeline import page_plan as page_plan_mod

COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class ProductResult:
    """製番 1 件の結果。cleanup=True は、製番フォルダを(出力ルート配下に限り ignore_errors=True で)削除することを求める。"""
    status: str
    reason: str = ""
    cleanup: bool = False


@dataclass
class ProductContext:
    """製番 1 件の処理に必要な入力(固定)と、Step の間で受け渡す状態(None のものは Step が埋める)。"""
    product: str
    output_dir: str
    dir_product: str
    logger: Any
    config_data: dict
    target_initial: str
    box_types: tuple
    converter_path: str
    converter_version: str
    converter_extension: str
    converted_extension: str
    manager: Any = None                  # 推論サーバーの管理(検証設定のみ。None なら従来動作)
    request_timeout: Optional[float] = None
    # --- Step が決める状態 ---
    serial: Optional[str] = None
    box_type: Optional[str] = None
    dwg_copy_dir: Optional[str] = None
    dir_design_serial: Optional[str] = None
    dir_design_product: Optional[str] = None
    product_df: Optional[pd.DataFrame] = None
    product_df_path: Optional[str] = None
    baninf_pages: List[str] = field(default_factory=list)
    print_pages: List[str] = field(default_factory=list)
    detected_df: Optional[pd.DataFrame] = None


# ##############################################################################
# 後始末
# ##############################################################################
def is_under(path: str, root: str) -> bool:
    """path が root の配下(root 自身は不可)か。実体解決後に比較する。"""
    p = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    r = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    try:
        return os.path.commonpath([p, r]) == r and p != r
    except ValueError:  # 別ドライブ等
        return False


def safe_rmtree(target: str, allowed_root: str, **kwargs):
    """削除対象を、設定された出力ルート(output_dir)配下に限定して shutil.rmtree する(Issue #52 安全条件)。"""
    if not is_under(target, allowed_root):
        raise RuntimeError(f"削除対象が出力ルート配下ではないため削除しません: {target}")
    shutil.rmtree(target, **kwargs)


SYSTEM_EXIT_REASON = "処理が途中で終了しました(SystemExit)"


def record_result(results: dict, product: str, result: ProductResult):
    """製番の結果を、戻り値(results)へ記録する。"""
    if result.status == COMPLETED:
        results["completed"].append(product)
    else:
        results[result.status][product] = result.reason


def cleanup_after_exception(ctx: ProductContext):
    """例外で終わった製番のフォルダを、出力ルート配下に限って削除する(失敗してもログに出して続行する)。"""
    if os.path.exists(ctx.dir_product):
        try:
            safe_rmtree(ctx.dir_product, ctx.output_dir)
            ctx.logger.info(f"Directory removed due to product error: {ctx.dir_product}")
        except Exception as del_error:
            ctx.logger.error(f"Directory cleanup error: {del_error}")


def run_product(ctx: ProductContext, results: dict):
    """製番 1 件を処理し、結果の記録と後始末をここで行う。例外は製番の失敗として扱い、次の製番へ進めるようにする。"""
    logger = ctx.logger
    try:
        result = process_product(ctx)
        if result.cleanup:
            safe_rmtree(ctx.dir_product, ctx.output_dir, ignore_errors=True)
        record_result(results, ctx.product, result)
    except Exception as e:
        logger.error(f"Process Failed for product {ctx.product}: {e}\n")
        logger.error(traceback.format_exc())
        results["failed"][ctx.product] = f"{type(e).__name__}: {str(e)[:200]}"
        failure_log = inference_client.describe_failure(e)
        if failure_log is not None:
            logger.error(f"推論APIの失敗内容(製番 {ctx.product} は未完了):\n{failure_log}")
        cleanup_after_exception(ctx)
    except SystemExit:
        # 下位モジュール(design_mod の印刷など)の sys.exit()。Exception ではないため上では捕まらず、製番フォルダ(部分出力)が残ったまま
        # 次回「処理済み」と見なされてしまう。他の失敗と同じ後始末(出力ルート配下に限る)をしてから、従来どおり終了させる(再送出)。
        # 終了コード・メッセージは記録しない(固定の理由)。
        logger.error(f"Process Aborted (SystemExit) for product {ctx.product}\n")
        results["failed"][ctx.product] = SYSTEM_EXIT_REASON
        cleanup_after_exception(ctx)
        raise


# ##############################################################################
# 製番 1 件の処理
# ##############################################################################
def process_product(ctx: ProductContext) -> ProductResult:
    """判定 → Step.1〜5 を順に実行する。途中で終わる場合はその結果を、最後まで進めば完了を返す。"""
    logger = ctx.logger
    logger.info(f"{'★'*60}")
    logger.info(f"製番処理開始: {ctx.product}")
    logger.info(f"{'★'*60}\n")

    for step in (check_product, step1_design_data, step2_inference_images, step3_frame_scale,
                 step4_detect_objects, step5_aggregate):
        result = step(ctx)
        if result is not None:
            return result
    return ProductResult(COMPLETED)


def check_product(ctx: ProductContext) -> Optional[ProductResult]:
    """物件情報の取得と、データ作成の対象かどうかの判定。製番フォルダを作る前なので、スキップしても後始末は要らない。"""
    logger = ctx.logger
    product = ctx.product
    logger.info("物件情報取得:")
    ctx.serial, ctx.box_type = host_mod.get_property_data(product)
    serial, box_type = ctx.serial, ctx.box_type
    logger.info(f" └> 整理番号: {serial = }, 箱体情報: {box_type = }\n")

    logger.info("***Check*** ホスト情報のブランク:")
    if not (serial and product and box_type):
        logger.warning(f"製番情報が取得できませんでした: {product}\n")
        return ProductResult(SKIPPED, "ホスト情報を取得できません")
    logger.info(f" └>OK!\n")

    logger.info("***Check*** データ作成条件: 1.整理番号ブランクでない, 2.製番A1で始まる, 3.箱体が処理対象型式である")
    logger.info(f" └> 1:{serial = }, 2:{product = }, 3:{box_type = }")
    if not (serial != "" and product.startswith(ctx.target_initial) and any(box_type.startswith(bt) for bt in ctx.box_types)):
        # 処理対象でないときはディレクトリ作成されないのでスルー
        logger.info(f"処理対象製番ではありません -> {product}\n")
        return ProductResult(SKIPPED, "処理対象外")

    logger.info(f" └> OK!\n")
    logger.info(f"***Check*** 作業済み")
    if os.path.exists(ctx.dir_product):
        logger.info(f" └> NG: 処理済みのためスキップ\n")
        return ProductResult(SKIPPED, "処理済み")  # 既存の製番フォルダは削除しない(cleanup なし)

    logger.info(f" └> OK!: 処理開始\n")
    return None


def step1_design_data(ctx: ProductContext) -> Optional[ProductResult]:
    """[Step.1] 設計データから盤情報 DataFrame と CSV を作り、共通製作仕様書があれば PDF にする。"""
    logger = ctx.logger
    dir_product = ctx.dir_product
    logger.info(f"{'-'*60}")
    logger.info(f"[Step.1] 図面画像及び盤情報DataFrame作成:")
    logger.info(f"{'-'*60}\n")

    logger.info(f"ディレクトリ作成: 設計DWG図面コピー用")
    os.makedirs(dir_product)  # Create Directory:Output(Srial Number)
    ctx.dwg_copy_dir = os.path.join(dir_product, "dwg")
    os.makedirs(ctx.dwg_copy_dir)  # Create Directory:COPY(DWG)
    logger.info(f" └> {config_loader.convert_win_path(dir_product) = }\n")

    # 整理番号をキーに設計サーバーパスを探索
    logger.info(f"設計図面パス探索:")
    dir_design_serial = ctx.dir_design_serial = design_mod.search_server(ctx.serial)
    logger.info(f" └> {config_loader.convert_win_path(dir_design_serial) = }\n")

    # 設計製番パスを作成
    logger.info(f"設計製番ディレクトリパス作成:")
    dir_design_product = ctx.dir_design_product = os.path.join(dir_design_serial, ctx.product)
    logger.info(f" └> {config_loader.convert_win_path(dir_design_product) = }\n")

    # 設計データより解析用DF作成
    logger.info(f"図面解析用DF作成:")
    df = cad_mod.load_baninf(ctx.dir_design_product)
    # -> 解析用DF出力、その読込図面ファイル名リストを取得
    import_dwg_names, csv_c, csv_f, csv_o = cad_mod.export_ban_data(df, dir_product, f"{ctx.product}")
    logger.info(f" └> 対象ページ Total: {len(import_dwg_names)}, {import_dwg_names = }")
    logger.info(f" └> CSV作成: 内器図 -> {config_loader.convert_win_path(csv_c)}")
    logger.info(f" └> CSV作成: 基礎図 -> {config_loader.convert_win_path(csv_f)}")
    logger.info(f" └> CSV作成: 外形図 -> {config_loader.convert_win_path(csv_o)}")

    # -> 座標計算用DF作成
    csv_list = [csv_c, csv_f, csv_o]
    sort_by = ["PAGE", "BAN_MENNO", "BAN_NO", "DETECT_AREA_X"]
    ctx.product_df = cad_mod.merge_and_sort_csvs(csv_list, sort_by)
    ctx.product_df_path = os.path.join(dir_product, "product_df.csv")
    cad_mod.export_csv(ctx.product_df, ctx.product_df_path)
    logger.info(f" └> CSV作成: 座標計算用 -> {config_loader.convert_win_path(ctx.product_df_path)}")
    logger.info(f" └> DataFrame作成(解析座標復元用): product_df \n")

    # design.csv(ページ番号 .db)と material.csv(*.boxspec)。どちらかが作れなければ製番を失敗として記録する
    # (部分的な CSV は作らない。参照元を整えて再実行する。後続の印刷・推論には進まない)
    logger.info("設計情報(ページ番号 .db -> design.csv):")
    try:
        design_path = cad_mod.export_design_csv(ctx.dir_design_product, dir_product, log=logger)
    except cad_mod.DesignDataError as e:
        logger.error(f"design.csv を作成できないため、製番 {ctx.product} を失敗とします: {e}\n")
        return ProductResult(FAILED, f"design.csv を作成できません: {e}", cleanup=True)
    logger.info(f" └> CSV作成: 設計 -> {config_loader.convert_win_path(design_path)}\n")

    logger.info("材料情報(boxspec -> material.csv):")
    try:
        material_path = cad_mod.export_material_csv(ctx.dir_design_product, dir_product, log=logger)
    except cad_mod.MaterialDataError as e:
        logger.error(f"material.csv を作成できないため、製番 {ctx.product} を失敗とします: {e}\n")
        return ProductResult(FAILED, f"material.csv を作成できません: {e}", cleanup=True)
    logger.info(f" └> CSV作成: 材料 -> {config_loader.convert_win_path(material_path)}\n")

    # 共通製作仕様書印刷 -> あれば焼く_2025/11/28
    logger.info(f"共通製作仕様書 印刷:")
    spec_sheets = ["1"]  # 共通製作仕様書(一般的な場)
    spec_sheets_name = spec_sheets[0] + ctx.converter_extension
    serial_Spec_dir = os.path.join(ctx.dir_design_serial, f"{ctx.serial}S0")
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
    return None


def step2_inference_images(ctx: ProductContext) -> Optional[ProductResult]:
    """[Step.2] 対象ページの選別 → 印刷(DWG→PDF) → 推論対象の DWG コピーと DXF 変換 → PDF→JPEG→PNG。"""
    logger = ctx.logger
    logger.info(f"{'-'*60}")
    logger.info(f"[Step.2] 推論用図面画像作成処理:")
    logger.info(f"{'-'*60}\n")

    result = _select_pages(ctx)
    if result is not None:
        return result
    result = _print_drawings(ctx)
    if result is not None:
        return result
    _make_inference_images(ctx)
    return None


def _select_pages(ctx: ProductContext) -> Optional[ProductResult]:
    """印刷対象と推論対象を決める(選別そのものは pipeline.page_plan)。推論できない製番はここで終わる。"""
    logger = ctx.logger
    logger.info(f"対象ページ取得:")
    # X/x 始まりの図面は、印刷・画像化・推論・データ作成のいずれにも入れない。選別は pipeline.page_plan
    plan = page_plan_mod.build_page_plan(ctx.product_df, ctx.dir_design_product, ctx.converter_extension)
    ctx.product_df = plan.product_df
    ctx.baninf_pages = plan.inference_pages
    ctx.print_pages = plan.print_pages
    logger.info(f" └> 推論対象ページ(従来の選び方、X始まりを除く): baninf_pages = {ctx.baninf_pages}")

    # 印刷対象: 設計製番フォルダの図面のうちX/x始まりを除く全ページ。推論対象以外はPDF印刷だけ行う
    logger.info(f" └> 印刷対象ページ({len(plan.print_pages)}件): print_pages = {plan.print_pages}")
    logger.info(f" └> PDF印刷のみのページ({len(plan.pdf_only_pages)}件): pdf_only_pages = {plan.pdf_only_pages}\n")
    if plan.status == page_plan_mod.OK:
        return None
    if plan.status == page_plan_mod.MISSING_DRAWINGS:
        logger.warning(f"推論対象ページの図面が設計フォルダにありません: {plan.missing_pages}")
        return ProductResult(FAILED, f"推論対象の図面がありません: {plan.missing_pages}(未完了)", cleanup=True)
    logger.warning(f"推論対象ページがありません(印刷対象 {len(plan.print_pages)} 件): {ctx.product}")
    return ProductResult(SKIPPED, "推論対象ページなし", cleanup=True)


def _print_drawings(ctx: ProductContext) -> Optional[ProductResult]:
    """印刷対象の全ページを PDF にする(IJCAD の負荷軽減のため 5 枚ずつ)。図面が他で使用中なら製番をスキップする。"""
    logger = ctx.logger
    logger.info(f" 図面印刷: DWG -> PDF(300dpi, 4961px X 3508px、印刷位置は用紙中央)")
    logger.info(f" ※ IJCADの負荷軽減のため5枚ずつ処理する")
    exit_outer = False
    for chunk_5 in cad_mod.chunk_list(ctx.print_pages):
        # チェック:図面使用
        for torken in chunk_5:
            page_path = os.path.join(
                ctx.dir_design_product,
                f"{torken}{ctx.converter_extension}"
                )
            if cad_mod.is_file_in_use(page_path):
                logger.warning(f"図面使用中のため製番をスキップ: {page_path}")
                exit_outer = True
                break  # chunk_5 を抜ける
        if exit_outer:
            break  # chunk_list を抜ける

        product_script_win = print_support.output_print_script(chunk_5, ctx.dir_design_product, ctx.dir_product)
        design_mod.print_dwg_for_pdf(product_script_win, len(chunk_5))
        logger.info(f" └> Page: {chunk_5}")
        cad_mod.countdown(len(chunk_5)*4)
    if exit_outer:
        logger.warning(f"図面使用中のため製番をスキップします: {ctx.product}")
        return ProductResult(SKIPPED, "図面使用中(未完了)", cleanup=True)
    logger.info(f"印刷完了!\n")
    return None


def _make_inference_images(ctx: ProductContext):
    """推論対象ページだけ、DWG のコピー → DXF 変換(図枠寸法用) → 推論用の画像(JPEG → 前処理 PNG)を作る。"""
    logger = ctx.logger
    logger.info(f"CAD図面(DWG)コピー:")
    logger.info(f" ※ ODAFileConverter 26.10.0 はディレクトリ単位でしか処理できない")
    cad_mod.copy_files_from_list(ctx.dir_design_product, ctx.dwg_copy_dir, ctx.baninf_pages, ctx.converter_extension)
    logger.info(f" └> コピー元(設製番計フォルダ): {ctx.dir_design_product}")
    logger.info(f" └> コピー先(プログラム製番フォルダ): {ctx.dwg_copy_dir}")
    logger.info(f" └> 対象ページ: {ctx.baninf_pages}\n")

    # 図面変換処理
    logger.info(f"図枠寸法取得用図面データ変換: DWG -> DXF")
    cad_mod.convert_dwg2dxf(ctx.dwg_copy_dir, ctx.dir_product, ctx.converter_path, ctx.converter_version)
    logger.info(f" └> 変換元: {ctx.dwg_copy_dir}")
    logger.info(f" └> 変換先: {ctx.dir_product}")
    logger.info(f" └> 対象ページ: {ctx.baninf_pages}\n")

    # PDF→JPEG→PNG変換
    logger.info(f"推論用画像作成: PDF -> JPEG(2481px X 1754px)\n")
    image_mod.pdf_to_images(ctx.dir_product, ctx.baninf_pages)

    logger.info(f"推論用画像前処理: JPEG -> PNG(Gaus,Bedian,Sharp,Bilateral,Binary,Crop)\n")
    image_mod.process_images_from_jpeg(ctx.dir_product, ctx.baninf_pages)


def step3_frame_scale(ctx: ProductContext) -> Optional[ProductResult]:
    """[Step.3] 図枠の寸法(DXF)と画像上の図枠から縮尺を求め、product_df に書き戻す。図枠が取れないページがあれば製番は失敗。"""
    logger = ctx.logger
    logger.info(f"{'-'*60}")
    logger.info(f"[Step.3] 解析用図面座標及び縮尺算出:")
    logger.info(f"{'-'*60}\n")

    img_ext = ctx.config_data["data_extension"]["original_draw"]  # 拡張子: 対象図面

    # 画像解析・座標算出用に図枠外余白をカット(CADのX,y座標へ画像サイズを合わせる)
    exit_outer = False
    for page in ctx.baninf_pages:
        logger.info(f" └>対象図面: {page + img_ext}")

        logger.info(f"\t\t├元図枠情報:")
        dxf_path = os.path.join(ctx.dir_product, f"{page}{ctx.converted_extension}")
        origin_frame_width, origin_frame_height = cad_mod.get_frame_size(dxf_path)
        if origin_frame_width == 0 or origin_frame_height == 0:
            logger.warning(f"\t └> 図枠サイズが取得できませんでした: {page}\n")
            exit_outer = True
            continue  # 次のページへ(全ページを見たあとで製番を失敗にする)

        logger.info(f"\t\t│	└> 図枠サイズ(w,h): {origin_frame_width},{origin_frame_height}")

        logger.info(f"\t\t├解析用画像情報:")
        # 画像の前処理: CAD図枠を検出し図枠外余白カットし画像を上書き
        img_path = os.path.join(ctx.dir_product, page + img_ext)
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
        ctx.product_df = cad_mod.update_frame_info(
            ctx.product_df,      # DataDFrame:盤情報
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
        logger.warning(f"作業対象外のためディレクトリを削除します: {ctx.dir_product}\n^")
        return ProductResult(FAILED, "図枠サイズを取得できません(未完了)", cleanup=True)

    # Update:盤情報CSV
    cad_mod.export_csv(ctx.product_df, ctx.product_df_path)
    logger.info(f"DataFrame上書き: product_df\n")
    return None


def step4_detect_objects(ctx: ProductContext) -> Optional[ProductResult]:
    """[Step.4] 推論 API へ画像を送って検出させ、結果を CAD 座標へ変換して detected_df.csv にする。API の失敗は例外で製番の失敗になる。"""
    logger = ctx.logger
    dir_product = ctx.dir_product
    logger.info(f"{'-'*60}")
    logger.info(f"[Step.4] 積算対象オブジェクト検出:")
    logger.info(f"{'-'*60}\n")

    # API解析リクエスト
    logger.info(f"APIリクエスト:")
    api = ctx.config_data["api"]
    api_cnf_path = os.path.join(api["dir_venv"], api["yaml_file"])
    logger.info(f" └> API問合せ設定: {api_cnf_path = }")
    logger.info(f" └> 対象フォルダ: {dir_product = }")
    logger.info(f" └> 解析対象ページ: baninf_pages = {ctx.baninf_pages}\n")

    # APIサーバーへ解析画像リクエスト送信
    logger.info(f"APIサーバーへ解析画像リクエスト送信:")
    # api.use_proxy は検証設定(app_test.yaml)のみ false を想定。省略時は従来どおりプロキシ使用
    use_proxy = api.get("use_proxy", True)
    request_kwargs = {}
    if ctx.manager is not None:
        # 推論サーバーと work_dir が同じ場所を指すこと(目印ファイルが双方から見えること)を確認してから依頼する
        inference_support.verify_shared_work_dir(ctx.manager.base_url, dir_product)
        logger.info(f" └> work_dir をサーバーと共有できていることを確認: {dir_product = }")
        request_kwargs["timeout"] = ctx.request_timeout
    elif ctx.request_timeout is not None:
        # 本番モードで api.request_timeout が指定されたときだけ渡す(省略時は渡さず、従来どおり待ち続ける)。
        # 超過は requests のタイムアウトとして image_mod が InferenceRequestError にし、製番の失敗・後始末の既存の経路で扱う(再試行なし)
        request_kwargs["timeout"] = ctx.request_timeout
    # 失敗(HTTPエラー・接続失敗・検出ファイル未生成)は InferenceRequestError で送出され、この製番は失敗として扱われる
    # (image_mod への依存は pipeline.inference_client に集約。契約は docs/inference_contract.md)
    response = inference_client.request_detections(
        dir_product, ctx.baninf_pages, api_cnf_path, use_proxy=use_proxy, **request_kwargs)
    log_text = response.log_text
    if response.warning:
        logger.warning(response.warning)
    formatted_log = inference_client.format_log(log_text)
    logger.info(f" └> リクエスト内容:\n{formatted_log}\n")

    # YOLO検出結果よりCAD座標へ変換したデータを作成
    # - 積算の判断で用いる座標はYOLOで得られた座標を元座標へ直して検出する
    # - 座標は各図面(ページ)毎に縮尺を算出し直す。
    logger.info(f"作成:検出結果をCAD座標へ変換したデータ")
    ctx.detected_df, detected_csv_path, discovery_pages = est_mod.create_detected_csv(dir_product)
    logger.info(f" └>出力ファイル :{detected_csv_path = }")
    logger.info(f" └>積算シンボル検出ページ :{discovery_pages = }\n")
    return None


def step5_aggregate(ctx: ProductContext) -> Optional[ProductResult]:
    """[Step.5] 積算コードの集計(estcode_df.csv)。最後まで来たら製番は完了。"""
    logger = ctx.logger
    logger.info(f"{'-'*60}")
    logger.info(f"[Step.5] 積算コード集計処理:")
    logger.info(f"{'-'*60}\n")

    # 作成:YOLO検出結果集計用DF
    logger.info(f"作成:YOLO検出結果集計用DF")
    estcode_df, estcode_df_path = est_mod.create_estcode_df_and_csv(ctx.box_type, ctx.product_df, ctx.dir_product)
    logger.info(f" └>出力ファイル :{estcode_df_path = }")

    # 検出オブジェクト毎に積算コードを集計
    logger.info(f"作成:積算コード集計用DataFrame")

    # 操作パネル
    estcode_df = est_mod.estcode_panel(ctx.product_df, ctx.detected_df, estcode_df)
    # 盤内トランス
    estcode_df = est_mod.estcode_transformer(ctx.product_df, ctx.detected_df, estcode_df)

    # Update:盤情報CSV
    cad_mod.export_csv(estcode_df, estcode_df_path)
    logger.info(f"DataFrame上書き: estcode_df\n")
    return None
