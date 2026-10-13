# 用途未確定ファイルの棚卸し(Issue #1、2026-10-13)

調査方法: 内容、import・設定読込の経路、docs からの参照、Git 履歴を確認した。Git 履歴はいずれも初回コミット(整理後 4b0f6f2)のみで、由来は履歴から分からない。
結論: **今回削除したファイルは無い**。用途を断定できない、または過去版・参考資料として価値があるため、すべて残す。

| ファイル | 用途 | 参照元 | Git 追跡 | 削除可否 |
|---|---|---|---|---|
| `0.py` | 簡易バッチ実行(DWG 印刷→PDF→画像化)。本番共有のパスを直書きした作業用スクリプト | `docs/0.md`、`docs/00_python_code_details_index.md`。`app.py` や tests からの import なし。`bs_pkg`・`cad_data_process`・`estimate_code_processer` を使う | 追跡 | **残す**。社内作業スクリプトで、同じ用途の他の入口が無い。判断は作成者 |
| `1.py` | テンプレートマッチングによる部品検出の検証(自動アノテーション補助)。`debug_images/`・`result.png` を生成 | `docs/1.md`、`detection_method_summary.md`、index | 追跡 | **残す**。検出手法の検証資料。本番共有のパスを直書き |
| `test.py` | `create_estcode_df_and_csv` の試験用スクリプト(`estimate_code_processer.py` に同名関数があり、内容は大きく異なる) | `docs/test.md`、index。tests は `tests/` 配下で別物 | 追跡 | **残す**。過去版の資料。名前が pytest/unittest の収集と紛らわしいため、将来は `docs` 側へ移す検討余地あり |
| `app_20251204.py` | `app.py` の 2025-12-04 時点の旧版。`app_20251204.yaml`(自分と同名の YAML)を読む | `docs/app_20251204.md`、index、`cad_data_process` の docs | 追跡 | **残す**。過去版として資料価値あり |
| `app_20251204.yaml` | 上記の設定(`app.yaml` と内容が異なる) | `app_20251204.py`(`SCR_NAME.yaml` として読込) | 追跡 | **残す**(上記とセット) |
| `create_template.py` | 「テンプレ元画像作成GUI」(tkinter)。`design_mod`・`image_mod`・`cad_data_process` を使い、`1.py` のテンプレート作成に対応するとみられる | コード・docs・tests からの参照なし | 追跡 | **残す**。参照が無いが、本プロジェクトの機能(`cad_data_process`)を使っており、無関係とは断定できない |
| `form_mst.yaml` | 図面の縮尺コード→分母の対応表(設計図面 `.db` の 8 行目のコード) | コード・docs・tests・`bs_pkg` からの参照なし | 追跡 | **残す**。マスタ資料。`scale_mst.yaml` と表の内容は同一 |
| `scale_mst.yaml` | 同上(コメントの行番号だけ 7 行目) | 同上 | 追跡 | **残す**。どちらが現行かは `.db` 仕様の確認が必要 |
| `install_requirements.py` | `pip install` のエラーを飛ばす補助(`requirements.txt` を読む) | `docs/install_requirements.md`、index | 追跡 | 残す |
| `detection_method_summary.md` | `1.py` の検出手法の要約 | `1.py` と対応 | 追跡 | 残す |
| 画像(`debug_images/*.png`、`result.png`) | `1.py` の生成物 | `1.py`、`docs/1.md`、`detection_method_summary.md` | **Git 管理外**(`.gitignore`) | 削除しない(ローカル生成物) |
| `docs/解析用カラム詳細.png` | `readme.md` の説明図だったもの(実図面由来) | なし(`readme.md` の参照は文章に置き換え済み) | **Git 管理外**(`docs/*.png`) | 削除しない。追跡・コミットしない |

追跡中の画像・バイナリは無い(確認: `git ls-files`)。`back.png` と `file_renamer_gui.*` は 2026-10-09 に削除済み(`cleanup_log_issue1.md`)。

## 追跡中ファイルの内部情報について
`0.py`・`1.py`・`cad_data_process.py`・`app.yaml` などに社内共有のホスト名・パスが書かれている。リポジトリは PRIVATE だが、再公開・共有する場合は先に取り除くこと。
