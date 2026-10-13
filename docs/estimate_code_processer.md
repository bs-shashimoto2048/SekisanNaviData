# `estimate_code_processer.py` 詳細

## 概要
- 積算コード処理モジュールです。
- 集計用 DF の列定義 `ESTCODE_COLUMNS`(`estcode_df.csv` の列。順序も出力契約)と、集計されない列の一覧 `UNIMPLEMENTED_ESTCODE_COLUMNS`(列 → 対応する未実装関数。列名と各関数の対象品名からの推定。`None` は対応する関数なし)を持ちます。
- 集計されるのは `PANEL`(`estcode_panel`)と `TRANS`(`estcode_transformer`)のみで、`【未実装】` と書かれた `estcode_*` は本体がありません(S4)。
- `build_estcode_df` は、ファイル・フォルダに触れない純関数です(S4)。

## 基本情報
- ファイル: `estimate_code_processer.py`
- 行数: `803`
- `__main__` ガード: `なし`

## import一覧
- `glob`
- `io`
- `os`
- `from pathlib import Path`
- `re`
- `pandas as pd`

## トップレベル変数
- `ESTCODE_COLUMNS`
- `UNIMPLEMENTED_ESTCODE_COLUMNS`

## 関数
### `create_detected_csv(target_dir)`
- docstring: `あり`

### `build_estcode_df(box_type, target_df)`
- docstring: `あり`

### `create_estcode_df_and_csv(box_type, target_df, target_dir)`
- docstring: `あり`

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
