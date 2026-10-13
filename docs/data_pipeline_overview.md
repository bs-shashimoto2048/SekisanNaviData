# データ作成パイプラインの現状(大規改修の出発点)

2026-10-13 時点の `main`(初回コミット b629173)を、コードとテストから読み取って整理したもの。改修中は、ここの「出力契約」を**根拠なく変えない**。
実図面・本番サーバー・SekisanNavi には接続せず、リポジトリ内のコード・テスト・ドキュメントだけで確認した。確認できていないことは「未確認」と書く。

## 1. 全体像
`app.py` の `main(config_path)` が、**製番(例: `A1xxxxxx`)ごと**に次を順に行う(約 470 行の 1 関数)。

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
- クライアントは `image_mod.slice_prediction_request`(`bs_pkg`、社内コード)。失敗は `InferenceRequestError` で製番の失敗として扱う。
- 本番の推論サーバー(`server.py`・モデル・`torch` の版)の正本は未確認(`docs/acceptance_handoff.md` §5)。

## 5. 読み取れた構造上の課題(改修の動機。挙動は未変更)
1. `main()` が設定・サーバー・ホスト照会・5 段階の処理・後始末・集計を 1 関数で持つ。ステップ単位のテストが書きにくく、統合テストは全モックの全体実行だけ。
2. ステップ間のデータ(`product_df`、`baninf_pages`、`dir_product`、`exit_outer` フラグ)が関数内のローカル変数で受け渡され、失敗時の後始末が各所に散在(`_safe_rmtree` が 5 か所)。
3. ページ選別・印刷・縮尺算出などの純粋な処理が、外部処理(IJCAD・ODA・API)と同じ場所に書かれている。→ **S1 で選別を `pipeline/page_plan.py` に切り出した。**
4. `estimate_code_processer.py`: `estcode_stack_plate` が 2 回定義(後の定義が前を上書き)、`estcode_box`・`estcode_door` などは本体が docstring のみ(未実装)で、積算コードは操作パネル・盤内トランス以外は集計されていない。積算ルールが関数内の分岐に埋まっている。
5. `app.py` に使われていない import(`cv2`、`re`、`time`、`datetime`/`timedelta`、`pd`)、デバッグ出力 `print(f"{log_text = }")`、ログの誤り(座標計算用 DF の CSV 名に外形図のパスを出している、対象ページの行が重複)がある。
6. `docs/app.md` などの自動生成ドキュメントが古い(行数・関数一覧が実体と一致しない)。
