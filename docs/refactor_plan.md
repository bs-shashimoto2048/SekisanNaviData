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
| S2 | `main()` の製番処理を `process_product(ctx)` と Step.1〜5 の関数に分割。`ProductContext`(パス・設定・logger・DF)と `ProductResult` を導入し、後始末(`_safe_rmtree`)を 1 か所へ | なし(統合テスト・契約テストが不変であること) | **完了**(§7) |
| S3 | 設定の読込・検証を `Settings`(データクラス)に集約。サンプル設定の拒否・推論サーバー設定の検証を移す | なし(YAML のキーは変えない) | **完了**(§7) |
| S4 | `estimate_code_processer.py`: 積算コードの集計ルールを関数単位に整理(重複定義 `estcode_stack_plate` の解消、未実装スタブの扱いの明記)。`create_estcode_df_and_csv` の行生成を純関数化 | **`estcode_df.csv` が不変**であることをテストで保証。未実装コードの集計追加は別件 | **完了**(§8) |
| S5 | `cad_data_process.py`(約 780 行)を役割別(baninf 読込、図枠・縮尺、印刷・変換)に分割。`import cad_data_process as cad_mod` の公開名は維持 | なし(公開関数名を維持) | **完了**(§9) |
| S6 | 推論サーバー統合の整理: リクエスト・応答の契約を文書とテストで固定し、`image_mod` への依存点を一か所に集約。本番サーバーの版・`torch` の確定(管理者情報)待ちの項目を分離 | なし。本番の版は入力待ち | **完了**(§10) |
| S7 | 整理: 未使用 import・デバッグ `print`・誤ったログの修正、自動生成ドキュメント(`docs/app.md` 等)の再生成 | ログ文言のみ変わりうる(契約外)。変更点を記録 | **完了**(§11) |

## 5. 変更記録(S2 は §6、S3 は §7、S4 は §8、S5 は §9、S6 は §10、S7 は §11)
| 日付 | 段階 | 変更 | テスト | 互換性への影響 |
|---|---|---|---|---|
| 2026-10-13 | S0 | `tests/test_output_contract.py`(5 件)を追加。`docs/data_pipeline_overview.md` | 全 81 件 OK(データ側)・推論側 27 件 OK・`pip check` OK | なし |
| 2026-10-13 | S1 | `pipeline/page_plan.py`、`tests/test_page_plan.py`(7 件)を追加。`app.py` の Step.2 の選別を置き換え | 同上(既存の統合テスト・契約テストは変更なしで通過) | なし。`PagePlan.status` が従来の分岐(推論対象なし=スキップ、図面なし=失敗)と同じ結果を返す |
| 2026-10-13 | S2 | `pipeline/product_flow.py`、`tests/test_product_flow.py`(28 件)を追加。`app.main()` の製番ループを Step 関数へ分割、後始末・結果記録を `run_product` に集約 | データ側 109 件・推論側 27 件 OK・`pip check` OK。12 シナリオで分割前後のログ・削除要求・生成物が一致 | なし(§6) |
| 2026-10-13 | S3 | `pipeline/settings.py`(`Settings`・`load_settings`・`build_inference_manager`)、`tests/test_settings.py`(15 件)を追加。`app.main()` の設定読込・サンプル設定の拒否・推論サーバー設定の検証を移動 | データ側 124 件・推論側 27 件 OK・`pip check` OK。12 シナリオのログ・削除要求・生成物が S2 時点と一致。設定エラーの特性化テスト 4 件は S3 適用前のコードでも通過 | なし(§7) |
| 2026-10-13 | S4 | `estimate_code_processer.py`: `build_estcode_df`(純関数)・`_base_diagram_maps` を切り出し、`estcode_stack_plate` の重複定義を解消(2 つ目を `estcode_passage` へ改名)、未実装の列・関数を `UNIMPLEMENTED_ESTCODE_COLUMNS` と docstring で明記。`tests/test_estimate_code_rules.py`(17 件)を追加 | データ側 141 件・推論側 27 件 OK・`pip check` OK。旧モジュールとの差分比較(36 通り: DataFrame・dtype・CSV バイト列・後続集計・例外)が完全一致 | なし(§8) |
| 2026-10-13 | S5 | `cad_data_process.py`(778 行)を `cad_data/baninf_tables.py`・`frame_scale.py`・`print_convert.py` に分割し、窓口(52 行)から公開名を再エクスポート。`tests/test_cad_data_split.py`(12 件)を追加 | データ側 153 件・推論側 27 件 OK・`pip check` OK。関数本体が分割前と AST で同一(24 関数 + 定数 1)、12 シナリオのログ・削除要求・生成物が S4 時点と一致 | なし(§9) |
| 2026-10-13 | S6 | `pipeline/inference_client.py`(推論に関する `image_mod` への依存を集約)、`docs/inference_contract.md`(契約)、`tests/test_inference_contract.py`(11 件)を追加。`product_flow` の推論呼び出し・失敗ログを置き換え | データ側 164 件・推論側 27 件 OK・`pip check` OK。12 シナリオのログ・削除要求・生成物が S5 時点と一致 | なし(§10) |
| 2026-10-13 | S7 | デバッグ `print` 5 か所・`app.py` の未使用 import 6 つの削除、重複ログ 1 行・誤った出力先表示 1 行の修正、`docs/app.md`・`docs/estimate_code_processer.md` の機械的な節の更新。`tests/test_s7_cleanup.py`(10 件)を追加 | データ側 174 件・推論側 27 件 OK・`pip check` OK。12 シナリオの差分は意図した 3 種類のみ(ログ 1〜2 行、標準出力のデバッグ表示) | ログ文言と標準出力のみ(契約外。§11) |

## 6. S2 の実施結果(2026-10-13)
`app.main()` の製番ループ本体(約 360 行)を `pipeline/product_flow.py` の Step 関数へ分割した。`app.py` は 561 行 → 239 行。処理順・ログ・戻り値・出力ファイル・失敗/スキップの扱い・後始末の対象とタイミングは変えていない。

### 検証
- 分割前(`d35e784`)と分割後で、擬似環境の 12 シナリオ(成功 / DXF 変換失敗 / 図面使用中 / 推論対象の図面なし / 推論対象なし(X のみ)/ X 始まり `.baninf` / 処理済み / API が `InferenceRequestError` / API が想定外の例外 / 図枠が取れない / 対象外の箱体 / ホスト情報の空欄)について、**ログ全文(時刻・行番号・トレースバックのフレームを除く)、`shutil.rmtree` の呼び出し(対象と `ignore_errors`)、生成ファイル一覧、印刷・変換・API・DWG 使用確認の呼び出し、標準出力が完全に一致**することを確認した(手元の比較用スクリプトで実施。リポジトリには含めない)。
- テスト: データ側 109 件 OK(従来 81 件 + `tests/test_product_flow.py` 28 件)、推論側 27 件 OK、`pip check` OK。既存の統合テスト・契約テストは変更なしで通過。

### 構造
`run_product(ctx, results)` が 1 製番の入口で、結果の記録と後始末をここだけで行う。`process_product(ctx)` が Step を順に呼び、`ProductResult` を返した Step で終わる(`None` は続行)。

| 関数 | 責務 | 終わり方(`ProductResult`) |
|---|---|---|
| `check_product` | 物件情報の取得、ホスト情報の空欄・対象製番・箱体型式・処理済みの判定(製番フォルダを作る前) | スキップ: ホスト情報なし / 処理対象外 / 処理済み(**後始末なし**。既存フォルダは削除しない) |
| `step1_design_data` | 製番フォルダと `dwg/` の作成、設計サーバー探索、`.baninf` → 盤情報 CSV と `product_df`、共通製作仕様書の PDF | (例外のみ) |
| `step2_inference_images` | 次の 3 つを順に実行 | |
| ├ `_select_pages` | 印刷対象・推論対象の決定(`pipeline.page_plan`) | 失敗: 推論対象の図面なし / スキップ: 推論対象ページなし(いずれも**静かな削除**) |
| ├ `_print_drawings` | 印刷対象を 5 枚ずつ PDF 化(IJCAD)。図面が使用中なら中止 | スキップ: 図面使用中(**静かな削除**。それまでに印刷済みの PDF も製番フォルダごと削除) |
| └ `_make_inference_images` | 推論対象の DWG コピー → DXF 変換 → JPEG → 前処理 PNG | (例外のみ) |
| `step3_frame_scale` | 図枠寸法と縮尺を `product_df` へ。**全ページを見てから**判定 | 失敗: 図枠サイズを取得できません(**静かな削除**) |
| `step4_detect_objects` | 推論 API へ送信(検証設定では作業フォルダ共有を確認しタイムアウトを渡す)、`detected_df.csv` | (API 失敗などは例外) |
| `step5_aggregate` | `estcode_df.csv`(操作パネル・盤内トランス) | 最後まで来れば完了 |

### 失敗時の流れ(後始末は 2 種類・1 か所)
1. **Step が結果を返す場合**: `cleanup=True` なら、`run_product` が出力ルート配下に限って `shutil.rmtree(製番フォルダ, ignore_errors=True)`(静かな削除)→ 結果を記録 → 次の製番へ。
2. **例外が出た場合**(API 失敗・変換失敗・想定外のエラー・範囲外の削除要求を含む): `Process Failed for product …` とトレースバックをログ → `results["failed"][製番] = "<例外名>: <メッセージ先頭 200 文字>"` → `InferenceRequestError` なら推論 API の失敗内容をログ → 製番フォルダが存在すれば `safe_rmtree`(`ignore_errors` なし。失敗はログに出して続行)。
3. 製番ループの外(ホスト照会の失敗など)の例外は、従来どおり `main()` の外側の `except` が `results["failed"]["(全体)"]` として記録し、処理中の製番フォルダを削除する。
4. 削除は常に出力ルート配下の製番フォルダだけ(`safe_rmtree`)。推論サーバーの停止は `main()` の `finally` で、変更していない。

### 互換性への影響
- 出力ファイル・戻り値(`results`)・ログ・終了コード・SekisanNavi の読み込み契約: **変更なし**。
- `app._safe_rmtree` / `app._is_under` は別名として残した(実体は `pipeline.product_flow`)。テストの差し替え点(`app.host_mod` など `app.<モジュール>` の属性)はそのまま有効。

## 7. S3 の実施結果(2026-10-13)
`app.main()` 冒頭の「設定読込 → サンプル設定の拒否 → 出力先の決定 → 推論サーバー設定の検証」を `pipeline/settings.py` に集約した。`app.py` は 239 行 → 214 行。**YAML のキー、検証の順序、エラーの種類、出力ファイル・ログ・戻り値は変えていない。**

### 構成
| 名前 | 内容 |
|---|---|
| `Settings`(frozen データクラス) | 設定ファイルのパス、YAML そのまま(`config_data`)、`data_dir`・`storage`・`infer_cfg`・`infer_enabled`。`product_dir`・`log_dir`・`output_dir` は派生。`target_initial`・`box_types`・`converter_*`・`converted_extension` はプロパティ |
| `load_settings(config_path, default_path)` | YAML を読む。`config_path` 省略時は `default_path()`(`app.local.yaml` があればそれ、無ければ `app.yaml`)を読み、**サンプルのまま(`<...>` が残っている)なら `RuntimeError`**。推論サーバー統合が有効なら出力ルートを絶対パスにする |
| `build_inference_manager(cfg)` | 無効(既定)なら `(None, None)`。有効なら設定を検証して管理オブジェクトを作る(`validate()` → 本番共有上の CAD 変換ソフトの拒否。後者は `InferenceServerError` に包む)。**起動はしない**(起動・停止は従来どおり `main()`) |
| `default_config_path` / `assert_not_sample_config` | 旧 `app.py` の関数の実体。`app.default_config_path()` と `app._assert_not_sample_config` は別名として残した(既存テストの差し替え点を維持) |

### 順序を変えないための判断
- 対象製番・箱体型式・CAD 変換の設定(`target_initial` ほか)は、読込時に先読みせず**プロパティで使う時点に読む**。従来は推論サーバー設定の検証の後で読んでいたため、設定が不足していて推論サーバー設定も不正なときに先に出るエラー(`InferenceServerError`)を変えないため(`tests/test_settings.py` で固定)。
- 推論サーバー設定の検証は、従来どおり出力フォルダ・ログを作る前に呼ぶ(本番共有・範囲外なら何も書かずに失敗)。
- Step が参照する `api`・`data_extension` は、従来どおり `config_data` を使う時点で読む(`ProductContext.config_data`)。

### 検証
- 擬似環境の 12 シナリオ(S2 の §6 と同じ)で、S3 適用前後のログ全文・`rmtree` の呼び出し・生成物・外部呼び出し・標準出力が完全一致。
- `tests/test_settings.py` の `MainConfigBehaviorTest`(`app.main` 経由の 4 件: 対象製番の設定欠落 → `KeyError` で何も作らない / 出力先の欠落 / 推論サーバー設定の不正が設定欠落より先 / 本番共有上の変換ソフトの拒否)は、**S3 適用前のコードでも同じ結果で通る**ことを確認した。
- テスト: データ側 124 件 OK(従来 109 件 + 15 件)、推論側 27 件 OK、`pip check` OK。

### 互換性への影響
- YAML のキー、`app.yaml`(サンプル)・`app_test.yaml`・`app.local.yaml` の書式: **変更なし**。出力契約・SekisanNavi の読み込み契約・推論サーバー連携・IJCAD の `close y`・後始末: **変更なし**。
- 未対応(S3 の範囲外): `app_20251204.py`(旧版)の設定読込は対象外。設定値の型検証(例: `box_types` が空)の追加はしていない(挙動が変わるため)。

## 8. S4 の実施結果(2026-10-13)
`estimate_code_processer.py` の積算コード集計用 DF の生成を、入出力と分けた純関数にし、重複定義と未実装スタブを整理した。**`estcode_df.csv` の内容(値・列・列順・dtype・CP932・BOM なし)、`create_estcode_df_and_csv` / `estcode_panel` / `estcode_transformer` の呼び出し方と結果、ログ、処理順は変えていない。**

### 変更内容
| 項目 | 内容 |
|---|---|
| 行生成の純関数化 | `create_estcode_df_and_csv` を (1) `build_estcode_df(box_type, target_df)`(ファイル・フォルダに触れない純関数。盤ごとに 1 行)と (2) 保存だけの `create_estcode_df_and_csv`(フォルダ作成 → `build_estcode_df` → `estcode_df.csv` を CP932 で保存)に分けた。基礎図の位置から並び順と接続区分(箱･単独 / 左右(L)(R) / 中央)を決める部分は `_base_diagram_maps` に切り出した。DataFrame の作り方(空 DF + `.loc[len]=row`)は元のまま(dtype を変えないため) |
| 列定義 | `ESTCODE_COLUMNS`(28 列、順序も契約)をモジュール定数にした。`tests/test_output_contract.py` の期待値と同一であることをテストで固定 |
| 重複定義の解消 | `estcode_stack_plate` が 2 回定義(後の「基礎図/正面図,通路」が前の「段積みプレート」を上書き)。後の定義を `estcode_passage`(列 `PASSAGE` に対応)へ改名。どちらも本体が無く、どこからも呼ばれていないため、挙動は変わらない。モジュール内に関数の重複定義が無いことをテスト(AST)で固定 |
| 未実装の明記 | 集計されない列を `UNIMPLEMENTED_ESTCODE_COLUMNS`(列 → 対応する未実装関数。対応が無ければ `None`)に一覧化し、未実装の関数 8 つの docstring に「【未実装】」を付けた。実装済みは `PANEL`(`estcode_panel`)と `TRANS`(`estcode_transformer`)のみ。未実装列は常に初期値(0 / `INPUT_CU_COEFF` は 0.0)で出力される。**列と関数の対応は、列名と各関数の「対象品名」からの推定** |

### 検証
- 分割前の `estimate_code_processer.py`(`a2ed98d`)と新モジュールを、12 種類の入力 × 3 つの箱体型式(36 通り)で突き合わせ、**`estcode_df` の DataFrame(dtype を含む)、`estcode_df.csv` のバイト列、続く `estcode_panel` → `estcode_transformer` の結果、例外の種類とメッセージが完全一致**することを確認した(入力: 基礎図が 1 枚 / 3 枚 / 無し、`KITEN_X` が NaN、`BAN_MENNO` の重複・文字列・NaN・小数、空の DF、必須列の欠落など。手元の比較スクリプトで実施、リポジトリには含めない)。
- `tests/test_estimate_code_rules.py`(17 件): 列が出力契約と一致、`build_estcode_df` がファイル・フォルダ・`to_csv` に触れず入力を変更しない、基礎図の並び・接続区分・NaN・重複、必須列欠落で `KeyError`、保存(新規フォルダ作成・CP932・BOM なし)、純関数との一致、未実装列が集計後も初期値のまま、未実装関数の表示・`None` 戻り、重複定義なし。
- テスト: データ側 141 件 OK(従来 124 件 + 17 件)、推論側 27 件 OK、`pip check` OK。既存の統合テスト・契約テストは変更なしで通過。

### 互換性への影響・残課題
- 出力契約・SekisanNavi の読み込み契約・処理順・ログ・失敗/スキップ・後始末・推論サーバー連携・IJCAD の `close y`: **変更なし**。呼び出し元(`pipeline/product_flow.py`、`app_20251204.py`)は変更不要。
- 名前の変更は `estcode_stack_plate`(2 つ目)→ `estcode_passage` のみ。この名前を参照するコードは無い(`docs/estimate_code_processer.md` の関数一覧は更新済み)。
- 残課題(S4 の範囲外): 未実装の積算コードの集計追加(出力に数量が入るため SekisanNavi 側の確認が要る)。`estcode_panel` と `estcode_transformer` の共通処理(`safe_int` など)の共通化は、挙動を変えないことの検証が要るため見送った。

## 9. S5 の実施結果(2026-10-13)
`cad_data_process.py`(778 行)を、役割別の 3 モジュールに分割した。**関数本体は 1 文字も変えていない**(移動のみ)。公開名(関数 24 + 定数 1)・呼び出し方(`import cad_data_process as cad_mod` → `cad_mod.<名前>`)・挙動・ログ・出力は分割前と同じ。

### 構成
| ファイル | 役割 | 内容 |
|---|---|---|
| `cad_data/baninf_tables.py`(約 400 行) | 設計データ → 盤情報の表(pandas とファイルのみ。CAD・画像・サブプロセスに依存しない) | `to_numeric_safe`、`clean_zumei`、`safe_apply`、`export_csv`、`assign_detect_area_standard`、`assign_detect_area_foundation`、`load_baninf`、`side_view_classifier`、`filter_calc_export`、`natural_key`、`export_ban_data`、`merge_and_sort_csvs` |
| `cad_data/frame_scale.py`(約 180 行) | 図枠の検出(画像)・寸法の取得(DXF)・縮尺の書き戻し | `specified_frame_detection`(cv2)、`get_frame_size`(ezdxf)、`update_frame_info` |
| `cad_data/print_convert.py`(約 180 行) | 印刷・変換・ファイル補助 | `EXCLUDED_PAGE_PREFIX`、`is_excluded_page`、`list_print_pages`、`chunk_list`、`countdown`、`convert_dwg2dxf`、`copy_files_from_list`、`is_file_in_use`(msvcrt)、`file_exists`、`listing_files` |
| `cad_data_process.py`(52 行) | **窓口**。上の 25 の名前を再エクスポートするだけ(同一オブジェクト) | |

### 守った点
- **テストの差し替え点**: 既存テストは `mock.patch.object(app.cad_mod, "countdown" など, ...)` で窓口の属性を差し替える。呼び出し側(`app.py`・`pipeline/`・各スクリプト)は必ず窓口経由で呼ぶ(`cad_data/` を直接 import しない)ことをテストで固定した。実装モジュール内の関数同士の呼び出し(`list_print_pages` → `is_excluded_page`)は差し替えの対象外(従来も、テストが差し替えるのは `countdown`・`is_file_in_use`・`convert_dwg2dxf` など外部に触れる関数だけ)。
- **実装モジュール同士・窓口へは import しない**(循環・依存の逆転を防ぐ)。移動した 24 関数は、分割前から相互の呼び出しが分割後の同じモジュール内に収まる(依存は `baninf_tables` 内の関数同士と `print_convert` 内の 1 組のみ)。

### 検証
- 機械的な確認(手元のスクリプト): (1) 24 関数と定数 1 の **AST が分割前と完全一致**、(2) 各モジュールに**未定義名がない**(最初の分割で `Path`・`OrderedDict`・`List` の import 漏れを検出し、補った)、(3) 窓口が 25 の名前すべてを実装と同一のオブジェクトで公開。
- 擬似環境の 12 シナリオ(S2 の §6 と同じ。図枠検出・DXF 読込・縮尺計算は実際の関数を通る)で、S4 時点とログ全文・`rmtree` の呼び出し・生成物・外部呼び出し・標準出力が完全一致。
- `tests/test_cad_data_split.py`(12 件): 挙動 7 件(`chunk_list`・`natural_key`・`update_frame_info`・`merge_and_sort_csvs`・`get_frame_size`・`file_exists`/`listing_files`・除外ページ。**分割前のコードでも同じ結果で通る**ことを確認)、構造 5 件(窓口の公開名と実装の同一性、役割ごとの import の分離、呼び出し側が窓口経由であること、窓口の差し替えが呼び出し側に効くこと)。
- テスト: データ側 153 件 OK(従来 141 件 + 12 件)、推論側 27 件 OK、`pip check` OK。

### 互換性への影響・残課題
- 出力契約・SekisanNavi の読み込み契約・処理順・ログ・失敗/スキップ・後始末・推論サーバー連携・IJCAD の `close y`・積算ロジック(未実装列を含む): **変更なし**。呼び出し側(`app.py`・`pipeline/`・`0.py`・`create_template.py`・`app_20251204.py`・`tools/`・tests)の変更なし。
- 変わるのは、各関数の `__module__`(`cad_data.<モジュール名>`)と、例外のトレースバックに出るファイルパスだけ。
- 未実装の積算列の集計は、今回も追加していない(数量・集計ロジックの仕様確認が先。確認事項: 各列の積算コードの判定条件、検出クラス名との対応、`INPUT_CU_COEFF` の係数の算出方法。いずれも仕様が資料にないため推測しない)。
- 残課題(S5 の範囲外): 関数内のデバッグ `print`・未使用変数の整理(S7)。`docs/cad_data_process.md`(自動生成の解説)は構成の注記のみ追加し、全面的な再生成は S7。

## 10. S6 の実施結果(2026-10-13)
推論サーバー統合の「依存点の集約」と「契約の文書化・テスト化」を行った。本番サーバーの版・`torch` など管理者情報が要る項目は、契約から分離して「未確認」として記録した。**リクエスト・応答・ファイル形式・処理順・ログ・失敗/スキップ・後始末・推論サーバーの起動/停止は変えていない。**

### 変更内容
| 項目 | 内容 |
|---|---|
| 依存点の集約 | `pipeline/inference_client.py` を追加。推論に関する `bs_pkg.image_mod` の機能(`slice_prediction_request`・`log_formater`・`InferenceRequestError`)は、ここだけが呼ぶ。`step4_detect_objects` の依頼・戻り値の正規化(`None` / `bytes` / その他 → `str`、注意のログ)・ログ整形と、`run_product` の失敗内容のログを置き換えた。`timeout` は指定したときだけ渡す(従来どおり)。`image_mod` はモジュール属性経由で呼ぶため、テストの差し替え(`mock.patch.object(app.image_mod, "slice_prediction_request", …)`)はそのまま有効 |
| 契約の文書化 | `docs/inference_contract.md`: 登場者と依存点、リクエスト(フォーム 3 項目)、応答(エンドポイントごとのステータス)、ファイル(`{page}_detected.txt` の形式・書き込み順・空ファイル・区切り文字の置換)、失敗の意味、設定のキー、**確定していないこと(本番サーバー側)** |
| 契約テスト | `tests/test_inference_contract.py`(11 件)。クライアント層の単体(正規化・`timeout`・失敗の素通し・`product_flow` が推論に関する `image_mod` を直接使わないこと)と、クライアントが実際に送る内容 ⇔ サーバーの `validate_request`、サーバーの書式化関数の出力 ⇔ 後続の `create_detected_csv` の突き合わせ |

### 検証
- 擬似環境の 12 シナリオ(S2 の §6 と同じ。API が `InferenceRequestError` / 想定外の例外になる場合を含む)で、ログ全文・`rmtree` の呼び出し・生成物・外部呼び出し・標準出力が S5 時点と完全一致。
- テスト: データ側 164 件 OK(従来 153 件 + 11 件)、推論側 27 件 OK、`pip check` OK。既存の統合・E2E・契約テストは変更なしで通過(`test_slice_prediction_request`・`test_inference_e2e` を含む)。

### 契約から分離した「未確認」(コード変更なし。詳細は `docs/inference_contract.md` §7)
- 本番の `server.py` の版・モデルのハッシュ、`torch` の版(2.5.1 か 2.6.0)、本番サーバーが同じ契約か、実図面での検出結果。
- **本番サーバーが書く `_detected.txt` の文字コード**: 後続処理は UTF-8(`errors="ignore"`)で読むため、UTF-8 以外だと日本語のクラス名の文字がエラーにならず欠落するおそれがある(今回新たに明文化した確認事項。コードは変えていない)。
- 後続処理が形式の崩れた行を黙ってスキップする挙動は、現行どおり(契約に明記。変更すると出力に影響しうるため別件)。

### 互換性への影響・残課題
- 出力契約・SekisanNavi の読み込み契約・処理順・ログ・失敗/スキップ・後始末・IJCAD の `close y`・推論サーバー連携: **変更なし**。
- `product_flow` が `image_mod` を直接使うのは、推論に関係しない画像の作成(`pdf_to_images`・`process_images_from_jpeg`)だけ。
- 残課題(S6 の範囲外): 形式の崩れた行をスキップせず警告する案(出力への影響の検討が要る)、本番サーバーの管理者確認(`docs/acceptance_handoff.md` §5)、デバッグ `print`・未使用 import の整理(S7)。

## 11. S7 の実施結果(2026-10-13)
計画書の S7(未使用 import・デバッグ `print`・誤ったログの修正、自動生成ドキュメントの再生成)を、参照と副作用を確認したうえで実施した。**出力契約(CSV/PDF/PNG)・SekisanNavi の読み込み契約・処理順・失敗/スキップ・後始末・推論契約・推論サーバーの起動/停止・IJCAD の `close y`・積算ロジックは変えていない。変わるのは、ログ 1〜2 行と、標準出力のデバッグ表示だけ。**

### 削除・修正した項目と根拠
| 項目 | 変更 | 根拠 |
|---|---|---|
| 標準出力のデバッグ `print`(`pipeline/product_flow.py`) | `print(f"{log_text = }")` を削除 | 推論 API の依頼ログの標準出力への重複表示。同じ内容は直後にロガー(`リクエスト内容:`)へ出ている。`{log_text = }` 形式のデバッグ表示 |
| 標準出力のデバッグ `print`(`cad_data/frame_scale.py` の `get_frame_size`) | `DEBUG:` 付きの 2 行、`max_x = …`・`width = …` の 2 行、「対象オブジェクトが見つかりません」の 1 行を削除(戻り値は変更なし) | `DEBUG:` ラベル・`{x = }` 形式のデバッグ表示。戻り値・例外・ファイルへの副作用なし(テストで確認) |
| 重複ログ | `図面解析用DF作成` の `対象ページ Total` の行が 2 回出ていたのを 1 回に | 完全に同じ行の重複 |
| 誤った出力先表示 | `CSV作成: 座標計算用` の行が外形図 CSV のパスを出していたのを、実際の出力先 `product_df.csv` に | 同じ行が指す対象は `product_df.csv`(`cad_mod.export_csv(product_df, product_df_path)`)。外形図は直前の行で別に出している |
| 未使用 import(`app.py`) | `cv2`・`pandas`・`re`・`time`・`datetime`/`timedelta`・`est_mod` を削除 | `app.py` 内・他ファイル・テストのどこからも `app.<名前>` として参照されない。`cv2`・`pandas` は `cad_data`・`pipeline` が引き続き import するため、読み込みの副作用も変わらない |

### 残した「未使用」(誤判定して消さないための判断)
- **`app.py` の `config_loader`・`design_mod`・`host_mod`・`image_mod`・`cad_mod`・`print_support`・`shutil`**: `app.py` 自身は使わないが、テストが `mock.patch.object(app.<名前>, …)` で差し替える**接点**(同じモジュールオブジェクトを指す)。削除するとテストが壊れる。コメントを付け、`tests/test_s7_cleanup.py` で「未使用 import は、この接点だけであり、各接点を差し替えるテストが実在する」ことを固定した。
- **`cad_data_process.py` の再エクスポート**(S5): `# noqa: F401`。公開名そのもの。
- **`estimate_code_processer.py` の `estcode_transformer` 内の `required_edit`**: 代入されるが使われない。`edit_df` の必須列(`BAN_MENNO`・`TRANS`)の検証が書きかけの可能性があり、**削除も検証の追加もしない**(積算ロジックに触れるため。確認事項として記録)。
- **`0.py`・`1.py`・`test.py`・`app_20251204.py`・`tools/verify_real_model.py` の未使用 import / `print`**: 用途未確定ファイル・過去版・手動ツール(`docs/file_inventory.md`)。テストの対象外で、整理の効果より誤りのリスクが大きいため触れない。
- 標準出力の情報表示(`切り抜き画像を保存しました`・`detected.csv を作成しました`・`Copied: …`・DWG→DXF 変換ソフトの標準出力・待機中のカウントダウン)は、デバッグ表示ではなく運用上の経過表示なので残した。

### 自動生成ドキュメント
- **生成手順はリポジトリに定義されていない**(生成スクリプト・手順書なし。`git grep` で確認)。そのため「定められた手順での再生成」はできず、**新しい生成器を作ってコミットすることもしていない**。
- 既存の生成形式(基本情報・import 一覧・トップレベル変数・関数・クラス)を調べ、規則(非公開名は載せない、行数は `split("
")` の件数)を既存の記載から読み取って、**機械的に導ける節だけ**を AST から更新した: `docs/app.md`(行数 436 → 210、import・関数一覧)、`docs/estimate_code_processer.md`(行数、変数 `ESTCODE_COLUMNS` などと関数 `build_estcode_df` の追加。S4 で手書きした「定数」節は概要に移した)。**手書きの概要は保持**し、構成の注記を足した。
- 他の生成形式のドキュメント(`app_20251204.md`・`install_requirements.md`・`test.md`)は、行数・関数一覧が実体と一致していることを確認した(行数は `split("
")` の規則どおり)。手書きの解説(`0.md`・`1.md`・`cad_data_process.md`)は更新していない。
- リンク切れ 0 件。更新は 2 ファイルのみ。

### 検証
- 擬似環境の 12 シナリオを S6 時点と比較し、差分が**意図した 3 種類だけ**であることを機械的に確認した: (1) `対象ページ Total` の重複 1 行の減少、(2) `座標計算用` の行の出力先、(3) 標準出力のデバッグ表示の減少(`log_text = `・`max_x = `・`width = `・`DEBUG:`)。`rmtree` の呼び出し・生成物・印刷・変換・API・DWG 使用確認の呼び出しは**完全一致**。
- `tests/test_s7_cleanup.py`(10 件): 重複ログが 1 回、座標計算用の行が `product_df.csv`、他の CSV の行は不変、依頼内容が引き続きロガーに出る、標準出力にデバッグ表示が出ない、`get_frame_size` が無出力で同じ値を返す(図枠なしは `(0, 0)`)、`app.py` に残る未使用 import が接点だけ。
- テスト: データ側 174 件 OK(従来 164 件 + 10 件)、推論側 27 件 OK、`pip check` OK。既存の出力契約テスト・統合テスト・E2E は変更なしで通過。

### 互換性への影響・未解決事項
- ログのファイル(`logs/*.log`)の内容が変わるのは、重複 1 行の削除と `座標計算用` の行の出力先の 2 点。ログを機械処理している運用がある場合は、この 2 行に依存していないか確認してほしい(リポジトリ内には依存するコードなし)。標準出力は、デバッグ表示が減るだけ。
- 未解決: `required_edit` の扱い(積算ロジックの確認が要る)。生成ドキュメントの生成手順が未定義(今後も手作業になる。必要なら生成スクリプトの整備を別件で検討)。形式の崩れた行のスキップ(S6)。

## 12. 未確認・入力待ち(改修では解消しない)
- 実図面での検出・積算結果、SekisanNavi 実画面での受入、実図面での印刷位置、本番推論サーバーの正本・版(`docs/acceptance_handoff.md`)。
- SekisanNavi の読み込みコード(参照対象外)。契約に触れる変更は、この確認なしに行わない。
