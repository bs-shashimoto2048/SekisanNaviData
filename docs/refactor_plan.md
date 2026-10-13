# 大規改修の設計と段階計画(ブランチ `refactor/data-pipeline`)

現状と出力契約は [data_pipeline_overview.md](data_pipeline_overview.md)。Issue #1(実機受入)は、この改修と並行する別の未完了項目で、改修の完了では閉じない。

## 1. 目的
- `app.py` の一枚岩の `main()` を、**単体でテストできる段階(ステップ)**に分け、失敗時の後始末・結果記録を一か所にまとめる。
- 外部処理(IJCAD・ODA・推論 API・ホスト・設計サーバー)と、純粋な処理(選別・縮尺・集計)を分離する。
- 積算ルール(`estimate_code_processer.py`)を、追加・変更しやすい形に整理する。

## 2. 不変条件(全段階で守る)
1. **出力仕様の維持**: CSV の列名・列順・文字コード(CP932・BOM なし)、ファイル名、PDF/PNG の出力条件を変えない。`tests/test_output_contract.py` が失敗する変更は、契約変更として別に記録・確認する(SekisanNavi 側の確認なしに行わない)。
2. 印刷の仕様(X/x 始まり除外、全ページ PDF、用紙中央、IJCAD の `close y`・`quit y`)を維持する(`docs/print_spec.md`)。
3. 本番共有・実 DWG・社内ホスト・本番 API に接続しない。テストはモックと一時ディレクトリ・loopback のみ。
4. `bs_pkg`(社内コード)はリポジトリに入れない。外部 API(`bs_pkg.*`)の呼び方を変えるときは `docs/setup.md` の「必要な版」を更新する。
5. 各段階は**挙動を変えない切り出し**から始め、全テスト(データ側・推論側・`pip check`)が通ってからコミットする。
6. 用途未確定ファイルは削除しない。`main` へはマージしない(`refactor/data-pipeline` へ push するだけ)。

## 3. 設計の方針
- 新しい部品は `pipeline/` パッケージに置く。外部モジュール(`cad_data_process`・`bs_pkg` の各 `*_mod`)は、**モジュールの属性経由**で呼ぶ(`cad_mod.xxx(...)`)。既存の統合テストが `mock.patch.object(app.cad_mod, ...)` で差し替えているため、`from x import f` の形にすると差し替えが効かなくなる。
- 段階間のデータは、ローカル変数ではなく明示的なデータクラス(例: `PagePlan`、後続の `ProductContext`)で受け渡す。
- 製番の結果(完了・失敗・スキップと理由)は 1 つの型で表し、`main()` はそれを集計して終了コードにする(挙動は現状と同じ)。

## 4. 段階計画
| 段階 | 内容 | 互換性への影響 | 状態 |
|---|---|---|---|
| S0 | 出力契約をテストで固定(`tests/test_output_contract.py`)、現状を文書化 | なし(テスト・文書のみ) | **完了** |
| S1 | ページ選別(Step.2 前半)を `pipeline/page_plan.py` の純関数へ切り出し、単体テスト | なし(同じ選び方・同じログ・同じ結果記録) | **完了** |
| S2 | `main()` の製番処理を `process_product(ctx)` と Step.1〜5 の関数に分割。`ProductContext`(パス・設定・logger・DF)と `ProductResult` を導入し、後始末(`_safe_rmtree`)を 1 か所へ | なし(統合テスト・契約テストが不変であること) | 未着手 |
| S3 | 設定の読込・検証を `Settings`(データクラス)に集約。サンプル設定の拒否・推論サーバー設定の検証を移す | なし(YAML のキーは変えない) | 未着手 |
| S4 | `estimate_code_processer.py`: 積算コードの集計ルールを関数単位に整理(重複定義 `estcode_stack_plate` の解消、未実装スタブの扱いの明記)。`create_estcode_df_and_csv` の行生成を純関数化 | **`estcode_df.csv` が不変**であることをテストで保証。未実装コードの集計追加は別件 | 未着手 |
| S5 | `cad_data_process.py`(約 780 行)を役割別(baninf 読込、図枠・縮尺、印刷・変換)に分割。`import cad_data_process as cad_mod` の公開名は維持 | なし(公開関数名を維持) | 未着手 |
| S6 | 推論サーバー統合の整理: リクエスト・応答の契約を文書とテストで固定し、`image_mod` への依存点を一か所に集約。本番サーバーの版・`torch` の確定(管理者情報)待ちの項目を分離 | なし。本番の版は入力待ち | 未着手 |
| S7 | 整理: 未使用 import・デバッグ `print`・誤ったログの修正、自動生成ドキュメント(`docs/app.md` 等)の再生成 | ログ文言のみ変わりうる(契約外)。変更点を記録 | 未着手 |

## 5. 変更記録
| 日付 | 段階 | 変更 | テスト | 互換性への影響 |
|---|---|---|---|---|
| 2026-10-13 | S0 | `tests/test_output_contract.py`(5 件)を追加。`docs/data_pipeline_overview.md` | 全 81 件 OK(データ側)・推論側 27 件 OK・`pip check` OK | なし |
| 2026-10-13 | S1 | `pipeline/page_plan.py`、`tests/test_page_plan.py`(7 件)を追加。`app.py` の Step.2 の選別を置き換え | 同上(既存の統合テスト・契約テストは変更なしで通過) | なし。`PagePlan.status` が従来の分岐(推論対象なし=スキップ、図面なし=失敗)と同じ結果を返す |

## 6. 未確認・入力待ち(改修では解消しない)
- 実図面での検出・積算結果、SekisanNavi 実画面での受入、実図面での印刷位置、本番推論サーバーの正本・版(`docs/acceptance_handoff.md`)。
- SekisanNavi の読み込みコード(参照対象外)。契約に触れる変更は、この確認なしに行わない。
