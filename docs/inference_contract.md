# 推論サーバー統合の契約(クライアント ⇔ サーバー ⇔ 後続処理)

S6(`docs/refactor_plan.md` §10)で、データ作成側・推論サーバー・後続処理の間の取り決めを 1 か所にまとめ、テストで固定した。
統合の構成・安全条件・実行方法は [inference_server.md](inference_server.md)、処理全体は [data_pipeline_overview.md](data_pipeline_overview.md)。

**この文書の「確定」は、このリポジトリの検証用サーバー(`inference_server/`)とデータ作成側について、テストで固定した範囲。本番サーバーが同じ契約かどうかは未確認(§7)。**

## 1. 登場者と依存点
| 登場者 | 実体 | 役割 |
|---|---|---|
| データ作成側の入口 | `pipeline/product_flow.py` の `step4_detect_objects` | 製番ごとに推論を依頼し、結果を後続処理へ渡す |
| **推論 API クライアント層** | **`pipeline/inference_client.py`** | 推論に関する `bs_pkg.image_mod` への依存を集約(下記) |
| クライアント実体(社内コード) | `bs_pkg.image_mod.slice_prediction_request` | ページごとに HTTP でリクエストを送る(リポジトリに含まれない) |
| 推論サーバー | `inference_server/server.py` | `/predict` などを提供し、検出結果のファイルを書く |
| 後続処理 | `estimate_code_processer.create_detected_csv` | `{page}_detected.txt` を読み、CAD 座標へ変換して `detected_df.csv` を作る |

`inference_client.py` が持つもの(推論に関する `image_mod` の機能は、ここ以外から呼ばない。テストで確認):
| 関数 | 内容 |
|---|---|
| `request_detections(dir_product, pages, api_config_path, *, use_proxy=True, timeout=…)` | `image_mod.slice_prediction_request` を呼ぶ。`timeout` は**指定したときだけ**渡す(省略=従来どおり待ち続ける。検証設定の推論サーバー統合では `request_timeout` を、未指定でも `None` として明示的に渡す) |
| `normalize_log_text(raw)` | 戻り値(通常は `str`)を `str` に正規化: `None` → 空文字+注意、`bytes` → UTF-8 で復号、その他 → `str()`+注意 |
| `format_log(text)` | 依頼ログの整形(空なら空文字) |
| `describe_failure(error)` | `InferenceRequestError` でログがあれば、整形した失敗内容。それ以外は `None` |

`product_flow` が `image_mod` を直接使うのは、推論に関係しない画像の作成(`pdf_to_images`・`process_images_from_jpeg`)だけ。

## 2. リクエスト契約(クライアント → サーバー)
- ページごとに 1 回、`POST <api の URL>/predict`(フォーム形式)。URL とプロキシは API 設定ファイル(`api.dir_venv` / `api.yaml_file`)から読む。`use_proxy: false`(検証設定)ではプロキシを使わない。
- フォーム項目は**この 3 つだけ**:
  | 項目 | 内容 |
  |---|---|
  | `work_dir` | 製番フォルダ(推論サーバーから見える同じパス) |
  | `page_nmb` | ページ番号(文字列) |
  | `extension` | 入力画像の拡張子(`png`) |
- サーバーは `work_dir/{page_nmb}.{extension}` を入力画像として読む。サーバーの受理条件(`inference_server/safety.validate_request`): `work_dir` は絶対パスで、実体解決後にサーバーの出力ルート配下(ルート自身・`..`・本番共有は拒否)、`page_nmb` は数字 1〜6 桁、`extension` は `png`/`jpeg`/`jpg`、画像が存在すること。
- **事前確認(本番モード)**: `app.main` は、推論サーバーを自分で起動しない設定(`inference_server` の節なし)のとき、重い処理(ホスト照会・設計データ読込・印刷・変換・画像作成)に入る前に、API 設定ファイルの接続先へ **`GET /health` を 1 度だけ**行う(`pipeline/api_preflight.py`)。プロキシは `/predict` と同じ決め方(`api.use_proxy`、`server.yaml` の `proxy.host` / `proxy.port`)。応答待ちは既定 5 秒(`api.health_timeout` で変更可。`/predict` のタイムアウトとは別)。**リダイレクトは追跡しない**(`allow_redirects=False`)。**最初の応答が HTTP 200 のときだけ**続行し、301/302/307 などのリダイレクトも失敗として扱う(転送先の 200 を「使える」と誤認しない)。
  - 続行しない場合(`server.yaml` が無い・読めない・項目不足、接続拒否、タイムアウト、HTTP 200 以外、その他の通信エラー)は、**製番処理を始めず失敗として記録して終了**する(終了コード 1・製番は完了扱いにしない・製番フォルダなどの部分出力を作らない)。通常処理は 1 度の確認で全製番を止め、`results["failed"]["(全体)"]` に理由を入れる。製番を指定した実行では、指定した製番ごとに同じ理由を失敗として記録する。
  - 理由は固定の文言で、**接続先(URL・ホスト名・ポート)・パス・例外メッセージは含めない**(例外型と HTTP 応答コードだけ)。履歴・ログにも同じ文言だけが残る。
  - 統合モード(`inference_server.enabled: true`)では呼ばない(検証用サーバーの起動時に `/health` の準備完了を確認する従来どおり)。
  - 本番の API が `GET /health` を提供するかは未確認(§7)。提供しない場合は 404 などになり、事前確認で止まる。
- **テスト**: `tests/test_inference_contract.py` が、クライアントが実際に送る内容(URL・項目名・値)と、それをサーバーの `validate_request` が受理すること、出力ルート外は拒否することを突き合わせている。

## 3. 応答契約(サーバー → クライアント)
| エンドポイント | ステータスと内容 |
|---|---|
| `POST /predict` | **200** `{"text_file_path": <検出テキストのパス>, "detections": <件数>}` / **400** 入力の拒否(`{"error": …}`) / **500** サーバーの準備失敗・推論失敗 / **503** 準備中 |
| `GET /health` | **200** 準備完了(`ready: true`、`instance_id`)/ **503** 読み込み中 / **500** 準備失敗 |
| `POST /verify_workdir` | **200** 目印ファイルが見える / **400** 入力の拒否 / **409** 見えない(別の場所を指している) |
| `POST /shutdown` | **200** / **403** `instance_id` 不一致 / **501** 停止手段なし |

- クライアントは、HTTP 200 でも (1) 応答が JSON でない、(2) `{page}_detected.txt` が製番フォルダに現れない(`verify_outputs=True` の既定。共有フォルダの反映遅延のため短時間再確認する)場合は失敗として扱う。
- テスト: クライアントの失敗系は `tests/test_slice_prediction_request.py`、サーバー単体は `inference_server/tests/test_api.py`、起動から停止までの通しは `tests/test_inference_e2e.py`。

## 4. ファイル契約(サーバーが書き、後続処理が読む)
製番フォルダに、ページごとに次を書く(再実行時は置き換え。失敗時は既存ファイルを変更しない)。
| ファイル | 内容 |
|---|---|
| `{page}_detected.png` | 検出結果の可視化画像(バックエンドが作れた場合のみ)。**先に**置く |
| `{page}_detected.txt` | 検出結果の本体。**後に**置く(後続処理が読むのはこれ。画像が揃ってから現れる) |

`{page}_detected.txt`:
- **UTF-8、改行 LF、1 行 1 検出**。検出 0 件のページは**空ファイル**(ファイル自体は作る)。
- 形式: `Index$<連番>|Device$<クラス名>|Score$<確信度>|Coordinates$<x,y:x,y:x,y:x,y>`
- `Coordinates` は画像上のピクセル座標で、4 点を **左上 → 右上 → 左下 → 右下** の順に `:` 区切りで並べる。
- クラス名に `|`・`$`・改行を含む場合は、形式が壊れないよう `_` に置換する。日本語のクラス名はそのまま(UTF-8)。

後続処理(`create_detected_csv`)の読み方と出力:
- `product_df.csv` の `SCALE_X`・`SCALE_Y`・`FRAME_ORG_Y` で画像座標を CAD 座標へ変換し(中心座標は画像上の矩形の中心 × 尺度。Y は図枠の高さから引く)、`detected_df.csv`(14 列、**CP932・BOM なし**)を書く。CP932 で表せないクラス名はエラーにする(`docs/data_pipeline_overview.md` §2)。
- 製番フォルダの `*_detected.txt` をすべて読む。`product_df.csv` に無いページは無視する。**4 項目に満たない行など形式の崩れた行は、黙ってスキップする**(エラーにならない)。
- テスト: `tests/test_inference_contract.py` が、サーバーの書式化関数の出力(日本語・区切り文字を含むクラス名、空ファイルを含む)を `create_detected_csv` に通して、クラス名・連番・確信度・中心座標を確認している。

## 5. 失敗の意味(成功扱いにしない)
- クライアントは、HTTP エラー・接続失敗・タイムアウト・応答不正・検出ファイル未生成・API 設定の読み込み失敗を `InferenceRequestError`(`failed_pages`・`log_text` を持つ)で送出する。
- `run_product` はこれを製番の失敗として扱う(`results["failed"]`、`推論APIの失敗内容` をログ、製番フォルダを後始末)。流れは `docs/refactor_plan.md` §6。成功したページだけを部分的に採用することはしない。
- 推論サーバーの準備(health/ready)に失敗したときは、製番の処理を始めない(`main()` が例外で終了、終了コード 1)。

## 6. 設定(キー名のみ。値はローカル設定)
- `api.dir_venv` / `api.yaml_file`: API 設定ファイルの場所。ファイル内に接続先(`host.pc_ip`・`host.port`)と、必要ならプロキシ(`proxy.host`・`proxy.port`)。
- `api.use_proxy`(省略時は `true`=従来どおりプロキシを使う)。検証設定(`app_test.yaml`)のみ `false`。
- `inference_server.*`(検証設定のみ。既定は無効): `enabled`・`port`・`backend`・`request_timeout` ほか(`docs/inference_server.md`)。

## 7. 確定していないこと(本番サーバー側。管理者への確認が必要)
下記はこのリポジトリのコード・テストでは確定できず、**契約の変更もしていない**。入力は `docs/acceptance_handoff.md` §5。
| 項目 | 現状 |
|---|---|
| 本番の `server.py` の版・モデルのハッシュ | 未確認(参照コピーとの一致可否が不明) |
| `torch` の版(2.5.1 か 2.6.0) | 未確認。2.6.0 ではモデル読み込みに失敗する可能性がある(`docs/inference_server.md` §9)。`requirements-real.txt` の既定は確認後に決める |
| 本番サーバーが `GET /health` を提供するか(事前確認が使う) | 未確認。提供しないと、本番モードの事前確認はすべての実行で失敗する |
| 本番サーバーが §2〜§4 と同じ契約か | 未確認。参照コピーは `/predict` の入力項目と応答のキーが同じだが、現行本番との一致は不明 |
| **本番サーバーが書く `_detected.txt` の文字コード** | 未確認。参照コピーは OS 既定の文字コードで書く(`docs/inference_server.md` §5)。後続処理は **UTF-8(`errors="ignore"`)** で読むため、UTF-8 以外で書かれると日本語のクラス名の文字が**エラーにならず欠落する**おそれがある |
| 実図面での検出結果 | 未検証(自動テストは `FakeBackend`、実モデルは合成画像のみ) |
