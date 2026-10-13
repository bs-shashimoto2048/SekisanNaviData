# `cad_data_process.py` 詳細分析

## このコードが行うこと
`cad_data_process.py` は、図面解析パイプラインの中核ユーティリティです。  
役割は大きく5系統あります。

1. `baninf` の読込・整形・CSV化
2. 図種別ごとの検出領域計算
3. 図枠サイズの抽出（画像側・DXF側）
4. ファイル操作（コピー、一覧化、DWG→DXF変換）
5. 複数CSV統合と図枠スケール反映

`app.py` / `app_20251204.py` から呼ばれることを前提に設計された、業務ロジック寄りの共通モジュールです。

## 全体構成（処理フェーズ）
### フェーズA. DataFrame基礎整形
- `to_numeric_safe`
- `clean_zumei`
- `safe_apply`
- `export_csv`
- `file_exists`

### フェーズB. baninf読み込みと図種別CSV出力
- `load_baninf`
- `assign_detect_area_standard`
- `assign_detect_area_foundation`
- `side_view_classifier`
- `filter_calc_export`
- `export_ban_data`
- `natural_key`

### フェーズC. 画像/図面サイズ計測
- `specified_frame_detection`（画像から図枠検出して切り抜き）
- `get_frame_size`（DXFから図枠寸法を推定）
- `update_frame_info`（計測値をDataFrameへ反映）

### フェーズD. ファイル・外部ツール操作
- `chunk_list`
- `countdown`
- `convert_dwg2dxf`
- `copy_files_from_list`
- `listing_files`
- `is_file_in_use`

### フェーズE. CSV統合
- `merge_and_sort_csvs`

## ステップバイステップ（主要処理）
## 1) `load_baninf(folder_path, file_ext=".baninf")`
### 目的
`.baninf` ファイル群を1つのDataFrameに統合する。

### 処理手順
1. 対象フォルダから `*{file_ext}` を列挙。
2. 先頭文字が `0` または `x` のファイルはスキップ。
3. 各行を `|` で分割し、さらに `key$value` を列へ展開。
4. 読込元ファイル名列（`読込ファイル名`）を付与。
5. 収集した全行からDataFrame生成。
6. `to_numeric_safe` で数値変換可能な列を変換。
7. `PAGE`,`ZUMEI` がある場合はその順でソート。
8. `clean_zumei` で `ZUMEI` の全角/半角スペースを除去。
9. DataFrameを返却。

## 2) `export_ban_data(df, output_dir, output_prefix="")`
### 目的
統合DataFrameから図面種別ごとのCSVを作る。

### 出力ファイル
- 全体DF: `{prefix}df.csv`
- 内部機器配置図系: `{prefix}df_cubicle.csv`
- 基礎図系: `{prefix}df_foundation.csv`
- 外形図系: `{prefix}df_outline.csv`

### 処理手順
1. 出力先ディレクトリを作成。
2. `読込ファイル名` のユニークリスト（拡張子除去）を自然順ソートで生成。
3. `OUTPUT_COLUMNS`（図種別ごとの標準カラム構成）を定義。
4. 内部機器配置図:
   - `filter_calc_export(..., assign_detect_area_standard, ...)` 実行
   - 欠損カラム補完後にCSV出力
5. 基礎図:
   - `filter_calc_export(..., assign_detect_area_foundation, ...)` 実行
   - `BAN_MENNO`,`BAN_NO` ソートして出力
6. 外形図:
   - `ZUMEI` に外形図相当語を含む行を抽出
   - `side_view_classifier` で側面図の左右分類と属性コピー
   - カラム整形とソート後に出力
7. 「ユニークファイル名一覧」と3CSVパスを返却。

## 3) `side_view_classifier(df)`
### 目的
外形図の側面図を左/右側面へ分類し、必要属性を正面図/背面図から補完する。

### 処理手順
1. `BAN_TYPE == 側面図` を抽出。
2. 同一 `KITEN_Y` グループ単位で処理。
3. 側面図グループ内の `KITEN_X` 最小/最大を求める。
4. 正面図グループがあれば:
   - 最小X側を左側面、最大X側を右側面に寄せて属性コピー。
5. 正面図が無く背面図がある場合:
   - 左右を反転ロジックで補完。
6. 側面図の `DETECT_AREA_X/Y` は `BAN_H1`,`BAN_D` から再設定。
7. 正面/背面行には `assign_detect_area_standard` を適用。

## 4) `specified_frame_detection(img_path)`
### 目的
画像から最大図枠を検出し、図枠内へ切り抜く。

### 処理手順
1. ファイル存在チェック、画像読み込みチェック。
2. グレースケール化→Canny→膨張でエッジ抽出。
3. `HoughLinesP` で線分抽出。
4. 水平候補と垂直候補を長さ条件付きで選別。
5. 2本の水平線×2本の垂直線の組み合わせから矩形面積最大を探索。
6. 見つかった矩形を線幅補正込みで内側へオフセット。
7. 画像を切り抜き、同じ `img_path` へ上書き保存。
8. `image_size`, `frame_size`, `margin` を辞書で返却。

## 5) `get_frame_size(dxf_file)`
### 目的
DXF側の図枠サイズ（幅・高さ）を取得する。

### 処理手順
1. `ezdxf.readfile` でモデル空間を読む。
2. 条件一致エンティティのみ対象:
   - レイヤ `AUX`
   - 色 `0` or `1`
   - 線種 `BYLAYER`
3. `LWPOLYLINE` と `LINE` の点座標を収集。
4. `max_x`, `max_y` を幅/高さとして整数化して返す。
5. 点が無い場合は `(0, 0)`。

## 6) `merge_and_sort_csvs(csv_paths, sort_columns)`
### 目的
複数CSVをカラム互換を保ったまま結合し、指定キーでソートする。

### 処理手順
1. `csv_paths` が空なら例外。
2. 先頭CSVのカラム順を基準列として確定。
3. 後続CSVに不足列を追加、新規列は末尾に拡張。
4. 全CSVを `concat` で結合。
5. `PAGE` が存在する場合は文字列化。
6. `sort_columns` で昇順ソートして返却。

## 7) `update_frame_info(...)`
### 目的
指定ページ行へ図枠原寸・画像寸法・縮尺を反映する。

### 処理手順
1. `product_df["PAGE"] == page` のマスクを作成。
2. `FRAME_ORG_X/Y`, `FRAME_MINI_X/Y`, `SCALE_X/Y` を一括更新。
3. 更新済みDataFrameを返却（in-place更新）。

## 補助関数の役割
- `assign_detect_area_standard`:
  - `BAN_TYPE` に応じて検出領域X/Yを切替（正面/側面/背面）
- `assign_detect_area_foundation`:
  - 基礎図向けに `BAN_W`,`BAN_D` を採用
- `filter_calc_export`:
  - `PAGE` が数字の行だけを対象化
  - `ZUMEI` キーワードで抽出
  - 検出領域計算→必要列抽出→CSV出力
- `copy_files_from_list`:
  - 指定拡張子でコピー
- `convert_dwg2dxf`:
  - ODA File Converter を `subprocess.run` で実行
- `is_file_in_use`:
  - `msvcrt.locking` によるWindowsロック状態判定

## 入出力と副作用の観点
- 強い副作用を持つ関数:
  - `export_csv`, `export_ban_data`, `copy_files_from_list`
  - `convert_dwg2dxf`, `specified_frame_detection`（元画像上書き）
- 外部依存が強い箇所:
  - CP932エンコーディング前提
  - ODA Converter実行環境
  - DXF構造（AUX/BYLAYER等のルール）への依存

## 実装上の注意点
- 文字コードは `cp932` 前提の読み書きが多い。
- `specified_frame_detection` は元画像を上書きするため再実行時に入力が変化する。
- `side_view_classifier` は `BAN_TYPE` や `KITEN_X/Y` の品質に結果が強く依存する。
- キーワード抽出は `ZUMEI` の表記揺れに弱い（完全一致ではないが語彙依存）。
