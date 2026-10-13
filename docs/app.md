# `app.py` 詳細

## 概要
- 社内図面解析のメイン実行スクリプトです。
- YAML設定を読み込み、対象案件リストを生成します。
- .baninf解析、CSV生成・統合、画像前処理、積算出力までを一括実行します。

## 基本情報
- ファイル: `app.py`
- 行数: `436`
- `__main__` ガード: `あり`

## import一覧
- `from datetime import datetime, timedelta`
- `os`
- `from pathlib import Path`
- `re`
- `shutil`
- `sys`
- `time`
- `traceback`
- `warnings`
- `cv2`
- `pandas as pd`
- `from bs_pkg import config_loader, design_mod, host_mod, image_mod, util_mod`
- `cad_data_process as cad_mod`
- `estimate_code_processer as est_mod`

## トップレベル変数
- `SCR_PATH`
- `MDL_DIR`
- `SCR_DIR`
- `SCR_NAME`

## 関数
### `main()`
- docstring: `なし`


## クラス
- なし
