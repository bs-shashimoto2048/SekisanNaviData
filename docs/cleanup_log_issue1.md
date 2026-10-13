# リポジトリ整理の作業記録(Issue #1、2026-10-09)

## 削除したファイル(無関係なコピー物)
| ファイル | 理由 |
|---|---|
| `file_renamer_gui.py` / `file_renamer_gui.spec`(Git 追跡から除外) | ファイル名変更の汎用 GUI。プログラム・設定・テスト・ドキュメントのいずれからも参照が無く、積算データ作成と無関係。履歴(初回コミット)から復元可能 |
| `back.png`(Git 管理外のローカル画像) | 上記 GUI の背景画像で、他から参照が無い |

## 保持したファイル(参照あり、または生成物・ローカル専用)
- 参照あり: `app.py` `app.yaml` `app_test.yaml` `cad_data_process.py` `estimate_code_processer.py` `inference_support.py` `inference_server/` `tests/` `docs/` `requirements.txt` `readme.md`
- ドキュメント(`docs/00_python_code_details_index.md`)が説明している: `0.py` `1.py` `test.py` `install_requirements.py` `app_20251204.py` `app_20251204.yaml` `detection_method_summary.md`
- 生成物・ローカル専用(削除しない): `.venv` `inference_server/.venv` `output/` `output_test/` `debug_images/` `result.png` `_backup/` `docs/解析用カラム詳細.png` `packages/bs_pkg/`(Git 管理外の社内パッケージ)

## 由来・用途を判断できないため残したもの(要確認)
- `0.py`(旧 `app.py` の複製と見られる)、`1.py`(テンプレートマッチ検証。本番共有パスを直書き)、`test.py`(`estimate_code_processer.py` の一部の複製と見られる)
- `app_20251204.py` / `app_20251204.yaml`(旧版)
- `create_template.py`(`bs_pkg` と `cad_data_process` を使う GUI。他から参照が無いが本プロジェクトの機能に近い)
- `form_mst.yaml` / `scale_mst.yaml`(縮尺・図面形式のマスタと見られる。コードからの参照が見当たらない)

## `.gitignore`
`*.local.yaml`(接続情報)、`file_renamer_gui.*`、`*.spec` を追加。`packages/bs_pkg/` と `docs/bs_pkg_version_diff.md` の除外は維持。

## 再確認(2026-10-09 追記)
次は参照関係と内容を再調査したが、いずれも docs(`docs/00_python_code_details_index.md` 等)が説明対象にしており、用途を断定できないため**削除せず残した**。
- `0.py`: 179行の独立スクリプト(`app.py` と内容は大きく異なる旧版)。docs/0.md が説明。
- `1.py`: テンプレートマッチ検証(docs/1.md、detection_method_summary.md が説明)。
- `test.py`: `create_estcode_df_and_csv` の試験用(docs/test.md が説明)。`estimate_code_processer.py` に同名関数がある。
- `app_20251204.py` / `.yaml`: docs/app_20251204.md が説明する旧版。
- `create_template.py`: docs からの参照なし。`cad_data_process` を使う。
- `form_mst.yaml` / `scale_mst.yaml`: コード・テストからの参照なし(設計図面 `.db` の行位置のメモ)。
削除してよいかは、これらを作成・利用した担当者の確認が必要。

