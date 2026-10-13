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
| S5 | `cad_data_process.py`(約 780 行)を役割別(baninf 読込、図枠・縮尺、印刷・変換)に分割。`import cad_data_process as cad_mod` の公開名は維持 | なし(公開関数名を維持) | 未着手 |
| S6 | 推論サーバー統合の整理: リクエスト・応答の契約を文書とテストで固定し、`image_mod` への依存点を一か所に集約。本番サーバーの版・`torch` の確定(管理者情報)待ちの項目を分離 | なし。本番の版は入力待ち | 未着手 |
| S7 | 整理: 未使用 import・デバッグ `print`・誤ったログの修正、自動生成ドキュメント(`docs/app.md` 等)の再生成 | ログ文言のみ変わりうる(契約外)。変更点を記録 | 未着手 |

## 5. 変更記録(S2 は §6、S3 は §7、S4 は §8)
| 日付 | 段階 | 変更 | テスト | 互換性への影響 |
|---|---|---|---|---|
| 2026-10-13 | S0 | `tests/test_output_contract.py`(5 件)を追加。`docs/data_pipeline_overview.md` | 全 81 件 OK(データ側)・推論側 27 件 OK・`pip check` OK | なし |
| 2026-10-13 | S1 | `pipeline/page_plan.py`、`tests/test_page_plan.py`(7 件)を追加。`app.py` の Step.2 の選別を置き換え | 同上(既存の統合テスト・契約テストは変更なしで通過) | なし。`PagePlan.status` が従来の分岐(推論対象なし=スキップ、図面なし=失敗)と同じ結果を返す |
| 2026-10-13 | S2 | `pipeline/product_flow.py`、`tests/test_product_flow.py`(28 件)を追加。`app.main()` の製番ループを Step 関数へ分割、後始末・結果記録を `run_product` に集約 | データ側 109 件・推論側 27 件 OK・`pip check` OK。12 シナリオで分割前後のログ・削除要求・生成物が一致 | なし(§6) |
| 2026-10-13 | S3 | `pipeline/settings.py`(`Settings`・`load_settings`・`build_inference_manager`)、`tests/test_settings.py`(15 件)を追加。`app.main()` の設定読込・サンプル設定の拒否・推論サーバー設定の検証を移動 | データ側 124 件・推論側 27 件 OK・`pip check` OK。12 シナリオのログ・削除要求・生成物が S2 時点と一致。設定エラーの特性化テスト 4 件は S3 適用前のコードでも通過 | なし(§7) |
| 2026-10-13 | S4 | `estimate_code_processer.py`: `build_estcode_df`(純関数)・`_base_diagram_maps` を切り出し、`estcode_stack_plate` の重複定義を解消(2 つ目を `estcode_passage` へ改名)、未実装の列・関数を `UNIMPLEMENTED_ESTCODE_COLUMNS` と docstring で明記。`tests/test_estimate_code_rules.py`(17 件)を追加 | データ側 141 件・推論側 27 件 OK・`pip check` OK。旧モジュールとの差分比較(36 通り: DataFrame・dtype・CSV バイト列・後続集計・例外)が完全一致 | なし(§8) |

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

## 9. 未確認・入力待ち(改修では解消しない)
- 実図面での検出・積算結果、SekisanNavi 実画面での受入、実図面での印刷位置、本番推論サーバーの正本・版(`docs/acceptance_handoff.md`)。
- SekisanNavi の読み込みコード(参照対象外)。契約に触れる変更は、この確認なしに行わない。
