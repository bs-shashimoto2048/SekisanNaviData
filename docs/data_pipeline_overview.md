# データ作成パイプラインの現状(大規改修の出発点)

2026-10-13 時点の `main`(初回コミット b629173)を、コードとテストから読み取って整理したもの。改修中は、ここの「出力契約」を**根拠なく変えない**。
実図面・本番サーバー・SekisanNavi には接続せず、リポジトリ内のコード・テスト・ドキュメントだけで確認した。確認できていないことは「未確認」と書く。

## 1. 全体像
`app.py` の `main(config_path)` が、**製番(例: `A1xxxxxx`)ごと**に次を順に行う。製番 1 件の処理は `pipeline/product_flow.py`(S2 で分割。構造は `docs/refactor_plan.md` §6)。

| 段階 | 内容 | 主な呼び出し先 | 外部とのつながり |
|---|---|---|---|
| 準備 | 設定読込(`app.local.yaml` 優先)、推論サーバー設定の検証・起動、ログ初期化 | `bs_pkg.config_loader`、`inference_support` | 設定ファイル、(検証時)loopback の推論サーバー子プロセス |
| 対象製番 | 当月 3 番出図済み製番を取得し、処理済みを除外 | `bs_pkg.host_mod` | **ホスト(DB)** |
| 条件判定 | 整理番号あり・製番頭 2 文字・箱体型式・処理済みでないこと | `host_mod.get_property_data` | **ホスト** |
| Step.1 | 設計フォルダの `.baninf` から盤情報 DF を作り CSV 出力。共通製作仕様書(`{整理番号}S0\1.dwg`)があれば PDF 化 | `cad_data_process`、`design_mod`、`print_support` | **設計サーバー(共有)**、IJCAD |
| Step.2 | ページ選別(X/x 始まり除外)→ 全印刷対象を PDF 化 → 推論対象の DWG をコピー → DXF 変換 → PDF→JPEG→前処理 PNG | `cad_data_process`、`image_mod`、ODA File Converter、IJCAD | IJCAD、ODA、設計サーバー |
| Step.3 | DXF の図枠寸法と PNG の図枠寸法から縮尺を算出し、`product_df` に書き戻す | `cad_data_process`(`get_frame_size`、`specified_frame_detection`、`update_frame_info`) | なし |
| Step.4 | 推論 API へ PNG を送り `*_detected.txt` を得る → CAD 座標へ変換して `detected_df.csv` | `image_mod.slice_prediction_request`、`estimate_code_processer.create_detected_csv` | **推論 API(本番はリモート、検証は loopback)** |
| Step.5 | 積算コード集計: `estcode_df.csv` を作り、操作パネル・盤内トランスの数量を数える | `estimate_code_processer` | なし |

失敗・スキップは製番単位で `results = {"completed", "failed", "skipped"}` に記録され、失敗があれば終了コード 1(`_cli`)。失敗した製番のフォルダは、出力ルート配下に限って削除される(`_safe_rmtree`)。

### 設定
- 本番は `app.local.yaml`(Git 管理外)。`app.yaml` はサンプルで、`<...>` が残ったままだと起動時にエラー。
- 検証は `app_test.yaml`(ローカルの変換ソフト、`api.use_proxy: false`、`inference_server` セクション)。
- 推論サーバー統合は `inference_server.enabled: true` のときだけ有効(既定は従来動作)。

## 2. 出力契約(SekisanNavi 等が読むもの。`tests/test_output_contract.py` が固定)
出力先は `<data.output>/<data.storage>/output/<製番>/`。

| ファイル | 内容・文字コード |
|---|---|
| `{製番}_df.csv` | 全 `.baninf` の盤情報。13 列(先頭は `読込ファイル名`)。**CP932・BOM なし** |
| `{製番}_df_cubicle.csv` / `_foundation.csv` / `_outline.csv` | 内器図・基礎図・外形図の盤情報(CP932) |
| `product_df.csv` | 座標計算用 DF(上 3 つを結合・ソート)。20 列。Step.3 で `FRAME_*`・`SCALE_*` が確定。CP932 |
| `detected_df.csv` | 検出結果(CAD 座標)。14 列。**CP932・BOM なし**。CP932 で表せない文字を含むクラス名は、文字化けや UTF-8 切替をせずエラーにする |
| `estcode_df.csv` | 盤ごとの積算コード数量。28 列。CP932 |
| `material.csv` | 設計フォルダの `*.boxspec` から作る材料一覧(ページ × 材料シンボル × 構成部)。7 列 `PAGE, SOURCE_FILE, SYMBOL_NAME, CABTYPE, COMPONENT_CODE, COMPONENT_NAME, MATERIAL_SPEC`。**CP932・BOM なし**。`.boxspec` が無い製番、または解析に失敗した製番では**作られない**(処理は続行)。詳細は下の「material.csv」 |
| `{ページ}.pdf` | 印刷対象の全ページ(X/x 始まりを除く)。A3・用紙中央(`docs/print_spec.md`) |
| `{ページ}.jpeg` / `.png` / `.dxf` / `_detected.txt` | **推論対象ページだけ**。`_detected.txt` は UTF-8 で `Index$…|Device$…|Score$…|Coordinates$…` の行 |
| `dwg/{ページ}.dwg` | 推論対象ページの DWG コピー |
| `共通製作仕様書.pdf` | 共通製作仕様書があるときだけ |
| `../logs/*.log` | ログ(Git 管理外) |

列の一覧と順序は `tests/test_output_contract.py` を参照(順序も契約として固定している)。

## 3. SekisanNavi との互換性
- コード上で確認できている前提: `detected_df.csv` と `estcode_df.csv` を **CP932 で厳密に読む**(`estimate_code_processer.py` のコメント、`tests/test_detected_csv_encoding.py`)。
- **未確認**: SekisanNavi 側の読み込みコード・画面での表示。SekisanNavi は変更・参照対象外のため、実画面での受入は Issue #1 の未完了項目のまま。
- したがって、改修で CSV の列名・列順・文字コード、ファイル名、PDF/PNG の出力条件を変える場合は、**事前に契約変更として記録し、SekisanNavi 側の確認を経てから**行う。

## 4. 推論サーバー統合
- `inference_server/`(Flask、別 venv・別プロセス)を `inference_support.InferenceServerManager` が子プロセスとして起動・停止する(検証設定のみ)。loopback 限定、本番共有パスは拒否、親プロセス終了で自動終了。
- クライアントは `image_mod.slice_prediction_request`(`bs_pkg`、社内コード)。推論に関する `image_mod` への依存は `pipeline/inference_client.py` に集約(S6)。失敗は `InferenceRequestError` で製番の失敗として扱う。リクエスト・応答・ファイル形式・失敗の意味は [inference_contract.md](inference_contract.md)。
- 本番の推論サーバー(`server.py`・モデル・`torch` の版)の正本は未確認(`docs/acceptance_handoff.md` §5)。

## 5. 読み取れた構造上の課題(改修の動機。挙動は未変更)
1. ~~`main()` が設定・サーバー・ホスト照会・5 段階の処理・後始末・集計を 1 関数で持つ。~~ → **S2 で解消**: 製番 1 件の処理は `pipeline/product_flow.py` の Step 関数に分割済み(設定の読込・検証は S3 で `pipeline/settings.py` に集約。`main()` は推論サーバーの起動・停止、製番ループ、集計が残る)。
2. ~~ステップ間のデータがローカル変数で受け渡され、後始末が各所に散在~~ → **S2 で解消**: `ProductContext` で受け渡し、結果の記録と後始末は `run_product` の 1 か所。
3. ページ選別・印刷・縮尺算出などの純粋な処理が、外部処理(IJCAD・ODA・API)と同じ場所に書かれている。→ **S1 で選別を `pipeline/page_plan.py` に切り出した。**
4. `estimate_code_processer.py`: ~~`estcode_stack_plate` が 2 回定義~~ → **S4 で解消**(2 つ目を `estcode_passage` へ改名)。集計用 DF の生成は純関数 `build_estcode_df` に分離済み。積算コードのうち集計されるのは操作パネル(`PANEL`)と盤内トランス(`TRANS`)のみで、他の列は常に初期値(`UNIMPLEMENTED_ESTCODE_COLUMNS` に一覧)。未実装コードの集計追加は別件。
5. ~~`app.py` の未使用 import・デバッグ `print`・ログの誤り~~ → **S7 で整理**(テストの差し替え点として残した import を除く。`docs/refactor_plan.md` §11)。
6. ~~`docs/app.md` などの自動生成ドキュメントが古い~~ → **S7 で `docs/app.md`・`docs/estimate_code_processer.md` の機械的な節を実体に合わせた**。生成手順はリポジトリに無い(手作業)。
7. ~~`cad_data_process.py` が約 780 行で、表データ処理・図枠・印刷/変換が同居~~ → **S5 で解消**: `cad_data/` の 3 モジュール(`baninf_tables`・`frame_scale`・`print_convert`)に分割し、`cad_data_process.py` は公開名を再エクスポートする窓口。呼び出し側は従来どおり `cad_mod.<関数名>` を使う(`docs/refactor_plan.md` §9)。

## material.csv(材料一覧)

設計フォルダ(`.baninf` と同じ場所)直下の `*.boxspec`(CP932。拡張子の大文字小文字は問わない)を Step.1 で読み、製番フォルダ直下に `material.csv` を出す。元の `.boxspec` は読み取り専用で、変更・移動・削除しない。実装は `cad_data/boxspec_material.py`(窓口は `cad_data_process.export_material_csv`)。

- **ページ**: ファイル名(拡張子を除く)を `PAGE` とする(`301.boxspec` → `301`)。複数ファイルは製番単位の一覧にまとめ、各行に `PAGE` と `SOURCE_FILE` を残す。並びは数字のページは数値順、それ以外は後ろ。
- **読み方**: 1 行は `|` 区切りの要素で、各要素は最初の `$` でキーと値に分ける。先頭要素は `シンボル名$<名前>`。値の記号・寸法・材質表記は加工しない。
- **材料行**: `BSP-STEELA-*` のシンボルから 9 つの構成部(`STEEL-BASE` 基礎ベース / `STEEL-SIDE` 側板 / `STEEL-ROOF` 屋根・天井板 / `STEEL-PANEL` パネル(背面板) / `STEEL-DIVIDE` 仕切り板 / `STEEL-FRAME` 組枠 / `STEEL-BOTTOM` 底板 / `STEEL-INSIDEPANEL` 補助・内部パネル / `STEEL-DOOR` 扉板)を、1 シンボル × 9 行で出す。
- **`未記載` と `無し`**: キーが行に無い、またはキーはあるが値が空 → `未記載`。明示値 `無し` はそのまま保持し、`未記載` と区別する。(確定仕様。空欄・キー無しのケースはテストで固定)
- **未知の `STEEL-*` キー**: 捨てずに行として残し(`COMPONENT_NAME` は空)、ログに警告する。
- **CABTYPE**: `BSP-CABTYPEA` の `CABTYPE` が 1 種類だけならそのファイルの全材料行に付ける。無ければ空。**複数の異なる値があって対応が決められないときは推測せず空にし、警告する**(確定仕様)。
- **失敗時**: `.boxspec` が無いのは通常の欠損(情報ログ、`material.csv` なし)。1 ファイルでも読めない/解析できない(`$` の無い要素、同じキーの重複、CP932 でない等)ときは、**不完全な一覧を完全なものとして出さないため `material.csv` を作らず**、**警告ログ**を出して材料情報の作成だけを見送り、他の製番処理は続ける(部分的な `material.csv` は作らない)。
- **書き出し**: 一時ファイル経由(`os.replace`)で、途中失敗でも半端な `material.csv` を残さない。製番フォルダは毎回新規に作られる(既存フォルダはスキップ)ため、古い `material.csv` は残らない。
