# `estimate_code_processer.py` 詳細

## 概要
- 積算コード処理モジュールです。

## 基本情報
- ファイル: `estimate_code_processer.py`
- 行数: `726`
- `__main__` ガード: `なし`

## import一覧
- `glob`
- `io`
- `os`
- `from pathlib import Path`
- `re`
- `pandas as pd`

## トップレベル変数
- なし

## 関数
### `create_detected_csv(target_dir)`
- docstring: `あり`

### `create_estcode_df_and_csv(box_type, target_df, target_dir)`
- docstring: `あり`

### `build_estcode_df(box_type, target_df)`
- 積算コード集計用 DF を作る純関数(ファイル・フォルダには触れない。S4 で `create_estcode_df_and_csv` から分離)

### `estcode_box()`
- docstring: `あり`

### `estcode_panel(source_df, detected_df, edit_df)`
- docstring: `あり`

### `estcode_transformer(source_df, detected_df, edit_df)`
- docstring: `あり`

### `estcode_door()`
- docstring: `あり`

### `estcode_Shield()`
- docstring: `あり`

### `estcode_add_accs_price()`
- docstring: `あり`

### `estcode_input_cu()`
- docstring: `あり`

### `estcode_stack_plate()`
- docstring: `あり`

### `estcode_drawer_dcevice()`
- docstring: `あり`

### `estcode_passage()`
- docstring: `あり`


## クラス
- なし

## 定数(S4)
- `ESTCODE_COLUMNS`: `estcode_df.csv` の列(順序も出力契約)
- `UNIMPLEMENTED_ESTCODE_COLUMNS`: 集計されない列と、対応する未実装関数(推定。`None` は対応する関数なし)。`estcode_*` の未実装関数は docstring に「【未実装】」と記載
