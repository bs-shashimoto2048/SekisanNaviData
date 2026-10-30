# 製番 1 件分の処理(app.main() から切り出した Step 単位の関数)。S2 の成果。
#
# 方針: 処理順・ログ・出力ファイル・失敗/スキップの扱いは、切り出し前の app.main() と同じ。
# - 各 Step は ProductContext を読み書きし、「この製番はここで終わり」なら ProductResult を返し、続行なら None を返す。
# - 失敗時の製番フォルダ削除と、結果(完了・失敗・スキップ)の記録は run_product() の 1 か所だけで行う。
#   後始末の種類は 2 つ: ProductResult.cleanup=True(ignore_errors=True の静かな削除)と、例外時の削除(エラー内容をログに出す)。
# - 外部モジュール(cad_data_process・bs_pkg の各 *_mod・print_support など)は、テストが mock.patch.object で差し替える
#   モジュール属性を経由して呼ぶ。`from x import f` の形にしない。
import os
import hashlib
import shutil
import stat
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

import pandas as pd
from PIL import Image
from bs_pkg import config_loader, design_mod, host_mod, image_mod

import cad_data_process as cad_mod        # CAD/設計データ・画像解析用
import estimate_code_processer as est_mod # 積算コード集計・算出用
import inference_support                  # 推論サーバー(別プロセス)との作業フォルダ共有確認
import print_support                      # 印刷スクリプト(WIRING.SCR)作成: 印刷位置を用紙中央に配置
import display_previews                   # PDF-only図面・書類の表示用PNG(推論PNGとは別名)
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
    spec_work_pdf: Optional[str] = None  # 共通製作仕様書の PDF の作業フォルダでのパス(最終名へ移せていないとき。移せたら None)
    spec_settled: bool = False           # 仕様書の IJCAD が終了済み、または見守りが PDF の完成と安定を確認済み(追加の安定待ちが要らない)


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


_INCOMPLETE_MARKER_PREFIX = ".sekisan-incomplete-"
_INCOMPLETE_MARKER_SUFFIX = ".marker"


def incomplete_marker_path(output_dir: str, product: str) -> str:
    """製番名をファイル名へ露出させず、出力ルート直下の再処理マーカー位置を返す。"""
    if not isinstance(product, str):
        raise TypeError("製番マーカーの識別子が不正です")
    digest = hashlib.sha256(product.encode("utf-8")).hexdigest()
    return os.path.join(output_dir, f"{_INCOMPLETE_MARKER_PREFIX}{digest}{_INCOMPLETE_MARKER_SUFFIX}")


def has_incomplete_marker(output_dir: str, product: str) -> bool:
    """再処理マーカーがあるか。リンクは信頼せず失敗させる。"""
    marker = incomplete_marker_path(output_dir, product)
    if not os.path.lexists(marker):
        return False
    try:
        info = os.lstat(marker)
        if not stat.S_ISREG(info.st_mode) or os.path.islink(marker):
            raise RuntimeError("再処理マーカーが通常ファイルではありません")
        return True
    except OSError as exc:
        raise RuntimeError("再処理マーカーを確認できません") from exc


def mark_incomplete(output_dir: str, product: str):
    """処理開始を原子的に記録する。既存の有効マーカーは保持する。"""
    marker = incomplete_marker_path(output_dir, product)
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not has_incomplete_marker(output_dir, product):
            raise RuntimeError("再処理マーカーが不正です")
        return
    os.close(fd)  # 空ファイルの原子的作成をマーカーとして使う


def clear_incomplete_marker(output_dir: str, product: str):
    """通常ファイルであることを確認してから再処理マーカーを消す。"""
    marker = incomplete_marker_path(output_dir, product)
    if not os.path.lexists(marker):
        return
    if not has_incomplete_marker(output_dir, product):
        raise RuntimeError("再処理マーカーが不正です")
    os.unlink(marker)


def include_incomplete_products(output_dir: str, candidates: List[str], missing: List[str]) -> List[str]:
    """通常の未処理一覧に、出力フォルダが残った再処理対象だけを順序を保って加える。"""
    result = list(missing)
    included = set(result)
    for product in candidates:
        if product not in included and has_incomplete_marker(output_dir, product):
            result.append(product)
            included.add(product)
    return result


# ##############################################################################
# Step.2 の成果物の検査(各段階の直後)
# ##############################################################################
# 印刷(IJCAD)・DXF 変換(ODA)・PDF→JPEG→PNG(bs_pkg.image_mod)は、成果物ができなくても例外にならないことがある
# (image_mod は例外を握りつぶして次のページへ進む)。確認しないと、欠落は Step.3 で初めて FileNotFoundError になり、
# 全ページの印刷・変換を終えてから、段階も分からない形で失敗する。そこで、各段階の直後に「期待する成果物が、あるか・空でないか・読めるか」を検査する。
# 失敗理由は固定形式(段階名と件数だけ。製番・パス・例外メッセージ・設定値は含めない)。ページ名はログにだけ残す。
STAGE_PRINT, STAGE_DXF, STAGE_JPEG, STAGE_PNG = "印刷(PDF)", "DXF 変換", "JPEG 変換", "PNG 前処理"
STAGE_DISPLAY_PREVIEW = "表示用PNG"
STAGE_SPEC_PRINT = "共通製作仕様書の印刷"
ARTIFACT_GRACE_PRINT = 20      # 印刷は IJCAD が非同期に PDF を書き終えることがあるため、欠けているときだけ、最大この秒数(1 秒ごとに再確認)待つ
ARTIFACT_GRACE_DXF = 5         # 変換ソフトの終了直後の書き込み遅れ用(同上)。画像変換は同じプロセス内で完了するので待たない
_artifact_sleep = time.sleep   # テストが差し替える(実際には待たない)


def _is_nonempty_file(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _is_valid_pdf(path: str) -> bool:
    if not _is_nonempty_file(path):
        return False
    with open(path, "rb") as f:
        return f.read(5) == b"%PDF-"


def _is_valid_image(path: str) -> bool:
    if not _is_nonempty_file(path):
        return False
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:  # noqa: BLE001 - 読めない画像は不正として数える(理由に例外メッセージは出さない)
        return False


def _inspect_artifacts(dir_product: str, pages: List[str], name_of: Callable[[str], str], valid: Callable[[str], bool]) -> Tuple[List[str], List[str]]:
    """(欠落したページ, 存在するが空・不正なページ) を返す。"""
    missing, invalid = [], []
    for page in pages:
        path = os.path.join(dir_product, name_of(page))
        if not os.path.isfile(path):
            missing.append(page)
        elif not valid(path):
            invalid.append(page)
    return missing, invalid


def check_stage_artifacts(ctx: "ProductContext", stage: str, pages: List[str], name_of: Callable[[str], str], valid: Callable[[str], bool],
                          grace: int = 0) -> Optional["ProductResult"]:
    """段階の直後に、対象ページの成果物を検査する。すべて揃っていれば None(次の段階へ)。欠けていれば、その段階で製番を失敗にする
    (後続の重い処理へ進まない。製番フォルダは run_product が出力ルート配下に限って後始末する)。"""
    missing: List[str] = []
    invalid: List[str] = []
    for attempt in range(int(grace) + 1):
        missing, invalid = _inspect_artifacts(ctx.dir_product, pages, name_of, valid)
        if not missing and not invalid:
            return None
        if attempt < int(grace):
            _artifact_sleep(1.0)
    ctx.logger.warning(f" └> [{stage}] 成果物が不足しています(欠落: {missing} / 空または不正: {invalid})\n")
    bad = len(missing) + len(invalid)
    # 固定形式(「 / 」は使わない: 履歴は失敗した製番を「 / 」で連結するため、理由の中にあると画面の集計が分かれてしまう)
    return ProductResult(FAILED, f"[Step.2] {stage}の成果物が不足しています: 対象 {len(pages)} 件中 {bad} 件"
                                 f"(欠落 {len(missing)} 件・空または不正 {len(invalid)} 件)(未完了)", cleanup=True)


def print_timeout_result(stage: str, outcome: Any) -> Optional["ProductResult"]:
    """印刷(IJCAD)がハング(PDF が出ず応答もしない)と判定され、終了処理(print_support)まで済んだ場合に、製番を失敗にする結果を返す(そうでなければ None。
    プロセスが生きているだけ・PDF が不足しているだけでは、ここでは失敗にしない。PDF の不足は、続く既存の検査が「成果物が不足」で失敗にする)。
    理由は固定形式(段階名と終了の結果だけ。製番・ページ名・パス・例外メッセージ・標準出力/標準エラーは含めない)。製番フォルダは run_product が後始末する。"""
    if isinstance(outcome, print_support.PrintOutcome) and outcome.timed_out:
        return ProductResult(FAILED, print_support.timeout_reason(stage, outcome), cleanup=True)
    return None


def _jpeg_extension() -> str:
    """PDF→JPEG の出力拡張子(bs_pkg.image_mod の設定 detect_conversion.extention。読めなければ既定の jpeg)。
    image_mod は `{ページ}.{この値}` で書くので、同じ値を加工せずに使う(名前の食い違いで誤検出しない)。"""
    try:
        ext = (config_loader.load_yaml(image_mod.YAML_FILE_PATH).get("detect_conversion") or {}).get("extention") or "jpeg"
    except Exception:  # noqa: BLE001
        ext = "jpeg"
    return str(ext)


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
    if not os.path.exists(ctx.dir_product):
        try:
            clear_incomplete_marker(ctx.output_dir, ctx.product)
        except Exception as marker_error:
            ctx.logger.error(f"再処理マーカーを解除できませんでした: {type(marker_error).__name__}")


def _warn_if_folder_remains(ctx: ProductContext):
    """後始末(ignore_errors=True の静かな削除)のあとも製番フォルダが残っていれば警告する。マーカーにより次回は再処理対象になる。"""
    if os.path.exists(ctx.dir_product):
        ctx.logger.warning("後始末で製番フォルダを削除しきれませんでした(使用中のファイルが残っている可能性)。"
                           f"再処理マーカーを保持し、次回の全件実行で再試行します: {ctx.dir_product}\n")


def run_product(ctx: ProductContext, results: dict):
    """製番 1 件を処理し、結果の記録と後始末をここで行う。例外は製番の失敗として扱い、次の製番へ進めるようにする。"""
    logger = ctx.logger
    try:
        result = process_product(ctx)
        if result.status == COMPLETED:
            try:
                clear_incomplete_marker(ctx.output_dir, ctx.product)
            except Exception as marker_error:
                logger.error(f"再処理マーカーを解除できませんでした: {type(marker_error).__name__}")
                result = ProductResult(FAILED, "完了状態を記録できません(未完了)", cleanup=True)
        if result.cleanup:
            safe_rmtree(ctx.dir_product, ctx.output_dir, ignore_errors=True)
            _warn_if_folder_remains(ctx)
            if not os.path.exists(ctx.dir_product):
                try:
                    clear_incomplete_marker(ctx.output_dir, ctx.product)
                except Exception as marker_error:
                    logger.error(f"再処理マーカーを解除できませんでした: {type(marker_error).__name__}")
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
    incomplete = has_incomplete_marker(ctx.output_dir, product)
    if incomplete and os.path.exists(ctx.dir_product):
        try:
            safe_rmtree(ctx.dir_product, ctx.output_dir)
        except Exception:
            return ProductResult(FAILED, "前回の未完了フォルダを片付けられません(再処理待ち)")
        if os.path.exists(ctx.dir_product):
            return ProductResult(FAILED, "前回の未完了フォルダを片付けられません(再処理待ち)")
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

    # フォルダ生成前に記録し、作成直後の異常終了でも再処理対象から漏れないようにする。
    mark_incomplete(ctx.output_dir, ctx.product)
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
    result = _print_spec_sheet(ctx, dir_product)
    if result is not None:                           # 印刷の IJCAD がハングと判定された。後続へ進まない
        return result
    return None


def _print_spec_sheet(ctx: ProductContext, dir_product: str) -> Optional[ProductResult]:
    """共通製作仕様書(あれば)を印刷する。IJCAD がハング(PDF が出ず応答もしない)と判定されたら、製番を失敗にする。"""
    logger = ctx.logger
    logger.info(f"共通製作仕様書 印刷:")
    spec_sheets = ["1"]  # 共通製作仕様書(一般的な場)
    spec_sheets_name = spec_sheets[0] + ctx.converter_extension
    serial_Spec_dir = os.path.join(ctx.dir_design_serial, f"{ctx.serial}S0")
    if cad_mod.file_exists(serial_Spec_dir, spec_sheets_name):
        # 専用の作業フォルダ・専用のスクリプトで印刷する(図面ページの WIRING.SCR・`{ページ}.pdf`・製番フォルダ直下とは何も共有しない)。
        # 遅れて出力されても、図面ページの PDF を上書きできない。IJCAD に渡す PDF 名は ASCII の固定名(日本語名を渡さない)。
        # 最終名(0.pdf)へは、Step.2 の図面印刷の最後に、完成を安全に確認でき、かつ図面ページの PDF と衝突しないときだけ移す(_finalize_spec_sheet)。
        work_dir = os.path.join(dir_product, print_support.SPEC_WORK_DIR)
        work_pdf = os.path.join(work_dir, print_support.SPEC_WORK_PDF_NAME)
        os.makedirs(work_dir, exist_ok=True)
        try:
            if os.path.exists(work_pdf):             # 以前の実行の残り(完成済みに見えて、見守りが即座に終わるのを避ける)
                os.remove(work_pdf)
        except OSError:
            pass
        serial_script_win = print_support.output_print_script(spec_sheets, serial_Spec_dir, work_dir,
                                                              script_name=print_support.SPEC_SCRIPT_NAME,
                                                              pdf_name=lambda _page: print_support.SPEC_WORK_PDF_NAME)
        outcome = print_support.print_dwg_for_pdf(serial_script_win, len(spec_sheets), [work_pdf],
                                                   log=logger.warning, pdf_valid=print_support.pdf_is_complete)
        timed_out = print_timeout_result(STAGE_SPEC_PRINT, outcome)
        if timed_out is not None:                    # IJCAD がハング(PDF が出ず応答もしない)と判定され、終了処理まで済んだ。後続へ進まない
            return timed_out
        cad_mod.countdown(3)
        ctx.spec_work_pdf = work_pdf
        # プロセスが終了済み(COMPLETED)・見守りが完成と安定を確認済み(PDFS_READY)なら、移動のときに追加の安定待ちは要らない(待ち時間は変えない)。PENDING は書き込み中かもしれない
        ctx.spec_settled = outcome.status in (print_support.COMPLETED, print_support.PDFS_READY)
    else:
        logger.warning(f" └> 共通製作仕様書が見つかりませんでした: {serial_Spec_dir}\n")
    return None


def page_pdf_name(ctx: ProductContext, page) -> str:
    """図面ページの出力 PDF の名前。通常は `{ページ名}.pdf`(従来どおり)。設計作図完了シート(ページ名「0」)は、常に `0_A1.pdf`。
    ページ「0」が推論対象のときも同じ(推論用の `0.pdf` は、ここではなく `_ensure_inference_zero_pdf` が、完成した `0_A1.pdf` から別に用意する)。"""
    name = str(page)
    if name == print_support.CHECK_SHEET_PAGE:
        return print_support.CHECK_SHEET_PDF_NAME
    return f"{name}.pdf"


def _is_inference_zero(ctx: ProductContext) -> bool:
    """ページ「0」が推論対象か(画像変換 = bs_pkg が `0.pdf` を開く)。"""
    return print_support.CHECK_SHEET_PAGE in [str(p) for p in ctx.baninf_pages]


def _check_zero_outputs_absent(ctx: ProductContext, chunk: List[str]) -> Optional[ProductResult]:
    """ページ「0」を印刷する前に、その出力(`0_A1.pdf`。推論対象なら推論用の `0.pdf` も)が製番フォルダに無いことを確認する。
    製番フォルダは処理の開始時に新規に作られる(既にあれば「処理済み」でスキップ)ので、通常は無い。あれば異常(古い PDF の誤使用・上書きを避けるため、
    印刷せず、固定形式の理由で失敗にする。既存のファイルは変更しない。後始末は run_product が出力ルート配下の対象製番フォルダだけ)。"""
    if print_support.CHECK_SHEET_PAGE not in [str(p) for p in chunk]:
        return None
    names = [print_support.CHECK_SHEET_PDF_NAME] + (["0.pdf"] if _is_inference_zero(ctx) else [])
    if any(os.path.lexists(os.path.join(ctx.dir_product, n)) for n in names):
        ctx.logger.warning(" └> [印刷] 設計作図完了シートの出力先に、既存の PDF があります(古い PDF を使わないため、印刷しません)")
        return ProductResult(FAILED, "[Step.2] 設計作図完了シートの出力先に既存の PDF があるため、印刷しません(未完了)", cleanup=True)
    return None


def _ensure_inference_zero_pdf(ctx: ProductContext) -> Optional[ProductResult]:
    """ページ「0」が推論対象のとき、推論処理(画像変換)が開く `0.pdf` を、完成を確認した `0_A1.pdf` のコピーとして用意する
    (`0_A1.pdf` = 設計作図完了シートの成果物、`0.pdf` = 推論用の入力。役割は別で、内容は同じ図面)。
    一時ファイルにコピーしてから、既存を上書きしない `os.rename` で置く。既に `0.pdf` があれば上書きせず失敗にする。
    コピーが不完全なら残さない。用意できなければ、固定形式の理由で失敗にする(後続の変換・推論へ進まない)。"""
    if not _is_inference_zero(ctx):
        return None
    src = os.path.join(ctx.dir_product, print_support.CHECK_SHEET_PDF_NAME)
    dst = os.path.join(ctx.dir_product, "0.pdf")
    tmp = dst + ".part"
    made_tmp = False                                 # 自分が作った一時ファイルだけを、失敗時に消す(既存のファイルは消さない)
    try:
        if os.path.lexists(dst) or os.path.lexists(tmp):
            raise FileExistsError
        if not print_support.pdf_is_complete(src):
            raise ValueError
        made_tmp = True
        shutil.copyfile(src, tmp)
        if os.path.getsize(tmp) != os.path.getsize(src) or not print_support.pdf_is_complete(tmp):
            raise ValueError
        os.rename(tmp, dst)                          # 既存を上書きしない(Windows では既存の宛先があれば失敗する)
    except (OSError, ValueError) as e:
        try:
            if made_tmp and os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        ctx.logger.warning(f" └> [印刷] 推論用の PDF を用意できませんでした({type(e).__name__})")
        return ProductResult(FAILED, "[Step.2] 推論用の PDF を用意できません(未完了)", cleanup=True)
    ctx.logger.info(" └> 推論用の PDF を設計作図完了シートのコピーとして用意しました")
    return None


def _finalize_spec_sheet(ctx: ProductContext, dir_product: str) -> None:
    """作業フォルダの共通製作仕様書 PDF を、最終名(0_S0.pdf)へ移す。Step.2 の図面印刷の最後に 1 回だけ呼ぶ(図面ページの PDF が出揃ったあと)。
    次のどれかなら、移さない(作業フォルダに残し、固定形式の警告だけ。製番は失敗にしない・図面ページの PDF には触れない):
    ・製番フォルダ直下に同名のファイルが既にある(上書きしない) ・PDF の完成を安全に確認できない(PENDING のあとに遅れて出力される場合を含む)。"""
    work_pdf = ctx.spec_work_pdf
    if not work_pdf:
        return
    final_pdf = os.path.join(dir_product, print_support.SPEC_PDF_NAME)
    result = print_support.finalize_spec_pdf(work_pdf, final_pdf, sleep=_artifact_sleep,
                                             stable_seconds=0.0 if ctx.spec_settled else print_support.PDF_STABLE_SECONDS, log=ctx.logger.warning)
    if result == print_support.SPEC_MOVED:
        ctx.spec_work_pdf = None
        ctx.logger.info(f" └> {final_pdf = }\n")
    elif result == print_support.SPEC_PROTECTED:
        ctx.logger.warning(" └> [印刷] 共通製作仕様書の PDF と同名のファイルが既にあるため、最終名へ移していません(既存のファイルは上書きしていません。仕様書は作業フォルダに残しています)")
    else:
        ctx.logger.warning(" └> [印刷] 共通製作仕様書の PDF の完成を確認できなかったため、最終名へ移していません(図面ページの PDF には影響しません。手動で確認してください)")


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
    result = _make_display_previews(ctx)
    if result is not None:
        return result
    return _make_inference_images(ctx)         # 成果物が欠けていれば、その段階の失敗(ProductResult)。揃っていれば None(Step.3 へ)


def _select_pages(ctx: ProductContext) -> Optional[ProductResult]:
    """印刷対象と推論対象を決める(選別そのものは pipeline.page_plan)。推論できない製番はここで終わる。"""
    logger = ctx.logger
    logger.info(f"対象ページ取得:")
    # 印刷対象は design.csv の「ページ番号」が6文字以内で先頭がX/x でない図面だけ。条件外の図面は、印刷・画像化・推論・データ作成の
    # いずれにも入れない。design.csv を読み取れないときは、条件なしで全図面を印刷してしまわないよう、製番を失敗にする。選別は pipeline.page_plan
    design_csv = os.path.join(ctx.dir_product, cad_mod.DESIGN_CSV_NAME)
    try:
        design_page_ids = cad_mod.read_design_page_ids(design_csv)
    except cad_mod.DesignDataError as e:
        logger.error(f"design.csv を読み取れないため、印刷対象を決められません。製番 {ctx.product} を失敗とします: {e}\n")
        return ProductResult(FAILED, f"design.csv を読み取れません(印刷対象を決められません): {e}", cleanup=True)
    plan = page_plan_mod.build_page_plan(ctx.product_df, ctx.dir_design_product, ctx.converter_extension, design_page_ids)
    ctx.product_df = plan.product_df
    ctx.baninf_pages = plan.inference_pages
    ctx.print_pages = plan.print_pages
    logger.info(f" └> 推論対象ページ(印刷対象の条件を満たすもの): baninf_pages = {ctx.baninf_pages}")

    # 印刷対象: 設計製番フォルダの図面のうち、design.csv のページ番号が6文字以内で先頭がX/x でないもの。推論対象以外はPDF印刷だけ行う
    logger.info(f" └> 条件外のため印刷・推論に入れないページ({len(plan.excluded_pages)}件): excluded_pages = {plan.excluded_pages}")
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

        stale = _check_zero_outputs_absent(ctx, chunk_5)    # ページ「0」の出力先に既存の PDF があれば、印刷せず失敗(古い PDF の誤使用・上書きを避ける)
        if stale is not None:
            return stale
        # 設計作図完了シート(ページ「0」)を含む塊だけ、出力名を指定する(`0_A1.pdf`)。ほかの塊は、従来どおり指定なし(`{ページ}.pdf`)
        name_kw = {"pdf_name": lambda p: page_pdf_name(ctx, p)} if print_support.CHECK_SHEET_PAGE in [str(p) for p in chunk_5] else {}
        product_script_win = print_support.output_print_script(chunk_5, ctx.dir_design_product, ctx.dir_product, **name_kw)
        outcome = print_support.print_dwg_for_pdf(product_script_win, len(chunk_5), [os.path.join(ctx.dir_product, page_pdf_name(ctx, p)) for p in chunk_5],
                                                   log=logger.warning, pdf_valid=print_support.pdf_is_complete)
        logger.info(f" └> Page: {chunk_5}")
        timed_out = print_timeout_result(STAGE_PRINT, outcome)
        if timed_out is not None:                    # IJCAD がハングと判定され、終了処理まで済んだ。欠落の検査・次の塊・後続の変換や推論へ進まない
            return timed_out
        # 従来ここで待っていた「枚数 × 4 秒」(cad_mod.countdown)は、print_support.print_dwg_for_pdf の見守り(ペース)に含めた(二重に待たない)
        # 印刷の直後(この塊の分): 印刷対象ページの <ページ>.pdf(推論対象でない PDF だけのページも含む。設計作図完了シートは 0_A1.pdf)。欠けていれば次の塊・後続へ進まない
        result = check_stage_artifacts(ctx, STAGE_PRINT, chunk_5, lambda p: page_pdf_name(ctx, p), _is_valid_pdf, ARTIFACT_GRACE_PRINT)
        if result is not None:
            return result
        if print_support.CHECK_SHEET_PAGE in [str(p) for p in chunk_5]:     # ページ「0」が推論対象なら、推論用の 0.pdf を(完成を確認した 0_A1.pdf から)用意する
            result = _ensure_inference_zero_pdf(ctx)
            if result is not None:
                return result
    if exit_outer:
        logger.warning(f"図面使用中のため製番をスキップします: {ctx.product}")
        return ProductResult(SKIPPED, "図面使用中(未完了)", cleanup=True)
    _finalize_spec_sheet(ctx, ctx.dir_product)       # 共通製作仕様書の PDF を最終名へ(図面ページの PDF が出揃ったあと。衝突・未完成なら移さない)
    logger.info(f"印刷完了!\n")
    return None


def _make_inference_images(ctx: ProductContext) -> Optional[ProductResult]:
    """推論対象ページだけ、DWG のコピー → DXF 変換(図枠寸法用) → 推論用の画像(JPEG → 前処理 PNG)を作る。
    各段階の直後に、推論対象ページの成果物(DXF・JPEG・PNG)を検査し、欠けていればその段階で失敗にする。"""
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
    # DXF 変換の直後: 推論対象ページの <ページ><変換後拡張子>(Step.3 が図枠寸法を読む名前)。変換ソフトが終了コード 0 でも出力が無い場合を、ここで止める
    result = check_stage_artifacts(ctx, STAGE_DXF, ctx.baninf_pages, lambda p: f"{p}{ctx.converted_extension}", _is_nonempty_file, ARTIFACT_GRACE_DXF)
    if result is not None:
        return result

    # PDF→JPEG→PNG変換
    logger.info(f"推論用画像作成: PDF -> JPEG(2481px X 1754px)\n")
    image_mod.pdf_to_images(ctx.dir_product, ctx.baninf_pages)
    # JPEG 変換の直後: 推論対象ページの <ページ>.<image_mod の detect_conversion.extention>(image_mod は失敗を握りつぶすので、ここで確かめる)
    jpeg_ext = _jpeg_extension()
    result = check_stage_artifacts(ctx, STAGE_JPEG, ctx.baninf_pages, lambda p: f"{p}.{jpeg_ext}", _is_valid_image)
    if result is not None:
        return result

    logger.info(f"推論用画像前処理: JPEG -> PNG(Gaus,Bedian,Sharp,Bilateral,Binary,Crop)\n")
    image_mod.process_images_from_jpeg(ctx.dir_product, ctx.baninf_pages)
    # PNG 前処理の直後: 推論対象ページの <ページ><original_draw>(Step.3・推論が読む名前)
    png_ext = ctx.config_data["data_extension"]["original_draw"]
    result = check_stage_artifacts(ctx, STAGE_PNG, ctx.baninf_pages, lambda p: f"{p}{png_ext}", _is_valid_image)
    if result is not None:
        return result
    return None


def _make_display_previews(ctx: ProductContext) -> Optional[ProductResult]:
    """推論対象外の印刷図面と最上段の書類に、PDF由来の表示用PNGを作る。

    ファイル名は`{PDF名}__preview.png`。推論対象の`{ページ}.png`は触らず、
    書類PDF(0_S0/0_A1)はPDFのまま閲覧できるように保つ。失敗時は、画像なしの
    完了データを作らないよう製番を失敗にする。
    """
    try:
        expected = display_previews.create_product_previews(
            ctx.dir_product,
            ctx.print_pages,
            ctx.baninf_pages,
            lambda page: page_pdf_name(ctx, page),
            print_support.SPEC_PDF_NAME,
            print_support.CHECK_SHEET_PDF_NAME,
            log=ctx.logger.info,
            warn=ctx.logger.warning,
        )
    except display_previews.DisplayPreviewError as exc:
        ctx.logger.error(" └> [表示用PNG] PDFから表示用画像を作成できませんでした(%s)" % type(exc).__name__)
        return ProductResult(FAILED, "[Step.2] 表示用PNGを作成できません(未完了)", cleanup=True)

    result = check_stage_artifacts(ctx, STAGE_DISPLAY_PREVIEW, expected, lambda name: name, _is_valid_image)
    if result is not None:
        return result
    ctx.logger.info(f" └> PDF-only図面・書類の表示用PNGを作成しました({len(expected)}件)")
    return None


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
