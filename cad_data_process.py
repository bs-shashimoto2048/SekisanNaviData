# *****************************************************************************
# Explanation:専用モジュール, 標準積算自動化プログラム
# *****************************************************************************
# File Name : cad_data_process.py # -> import cad_data_process as cad_mod
# Developer : Satoshi Hashomoto
# Date      : 2025/11/18(Tue)
# Details   : 図面処理用
# Update    : Rep:2025/11/19(Wed), 各関数のリファクタリング、ドッグストリングの追加・修正
#             Rep:2025/12/08(Fri), filter_calc_export のフィルタリング条件に数字の文字列型のPAGEのみを追加
#             Add:2025/12/24(Wed), 図面使用中を判定する関数を追加
#             Rep:2026/10/13, 役割別に分割(S5)。このファイルは窓口で、公開名(関数・定数)は分割前と同じ。実装は cad_data/ 配下。

# ##############################################################################
# 公開名の再エクスポート
# ##############################################################################
# 呼び出し側(app.py・pipeline/・tests・各スクリプト)は従来どおり `import cad_data_process as cad_mod` で `cad_mod.<関数名>` を使う。
# テストは `mock.patch.object(app.cad_mod, "countdown", ...)` のようにこのモジュールの属性を差し替えるため、呼び出し側は必ずこの窓口を経由すること
# (cad_data 配下のモジュールから直接 import しない)。
#   - cad_data.baninf_tables : 設計データ(.baninf)→ 盤情報の表(DataFrame)・CSV
#   - cad_data.frame_scale   : 図枠の検出・図枠寸法の取得・縮尺の書き戻し
#   - cad_data.print_convert : 印刷対象の選別、5 枚ずつの分割、待機、DWG→DXF 変換、DWG コピー、図面使用中の判定
from cad_data.baninf_tables import (  # noqa: F401
    assign_detect_area_foundation,
    assign_detect_area_standard,
    clean_zumei,
    export_ban_data,
    export_csv,
    filter_calc_export,
    load_baninf,
    merge_and_sort_csvs,
    natural_key,
    safe_apply,
    side_view_classifier,
    to_numeric_safe,
)
from cad_data.frame_scale import (  # noqa: F401
    get_frame_size,
    specified_frame_detection,
    update_frame_info,
)
from cad_data.print_convert import (  # noqa: F401
    EXCLUDED_PAGE_PREFIX,
    chunk_list,
    convert_dwg2dxf,
    copy_files_from_list,
    countdown,
    file_exists,
    is_excluded_page,
    is_file_in_use,
    list_print_pages,
    listing_files,
)
