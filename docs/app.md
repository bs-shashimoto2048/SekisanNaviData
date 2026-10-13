# `app.py` 詳細

## 概要
- 社内図面解析のメイン実行スクリプトです。
- YAML設定を読み込み、対象案件リストを生成します。
- .baninf解析、CSV生成・統合、画像前処理、積算出力までを一括実行します。
- 設定の読込・検証は `pipeline/settings.py`、製番 1 件の処理(Step.1〜5)・結果記録・失敗時の後始末は `pipeline/product_flow.py` にあります。`app.py` は、設定の読込・推論サーバーの起動と停止・製番ループ・集計の戻り値を担います(`docs/refactor_plan.md` §6・§7)。

## 基本情報
- ファイル: `app.py`
- 行数: `210`
- `__main__` ガード: `あり`

## import一覧
- `os`
- `from pathlib import Path`
- `shutil`
- `sys`
- `traceback`
- `warnings`
- `from bs_pkg import config_loader, design_mod, host_mod, image_mod, util_mod`
- `cad_data_process as cad_mod`
- `from pipeline import product_flow`
- `from pipeline import settings as settings_mod`
- `print_support`
- `inference_support`

## トップレベル変数
- `SCR_PATH`
- `MDL_DIR`
- `SCR_DIR`
- `SCR_NAME`

## 関数
### `default_config_path()`
- docstring: `あり`

### `main(config_path)`
- docstring: `なし`


## クラス
- なし
