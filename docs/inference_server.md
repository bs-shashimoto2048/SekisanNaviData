# 推論サーバー統合(Issue #52)

SekisanNaviData に推論サーバーを組み込み、利用者から一つの起動手順で扱えるようにした。
**検証モード専用**であり、本番稼働中の推論サーバー・本番共有・既存の本番設定(`app.yaml`)には一切触れない。

## 1. 構成

```
app.py (データ作成, .venv)  --起動/health/停止-->  inference_server/server.py (別プロセス・別venv: inference_server/.venv)
        |                                                   ^
        +------ HTTP(127.0.0.1:検証専用ポート, プロキシなし) -+
        work_dir = 検証出力ルート配下の同一パス(同一PC。起動時に目印ファイルで双方から見えることを確認)
```

| 役割 | ファイル |
|---|---|
| 起動入口 | `app.py`(`python app.py --config app_test.yaml`。`inference_server.enabled: true` のときだけ統合起動) |
| 起動・health・共有確認・停止 | `inference_support.py`(`InferenceServerManager`、`verify_shared_work_dir`) |
| 推論サーバー | `inference_server/server.py`、`safety.py`(パス・本番共有の判定)、`backends.py`(`FakeBackend` / `SahiBackend`) |
| 依存関係 | `inference_server/requirements.txt`(軽量。検証 venv)、`requirements-real.txt`(実モデル用。**未導入・未検証**) |

設定例(`app_test.yaml` の `inference_server`。既定は `enabled: false`。`app.yaml` には存在せず、省略=無効=従来動作)。

## 2. 実行方法(検証)

```
# 推論サーバー用の別venv(初回のみ)
py -3.10 -m venv inference_server\.venv
inference_server\.venv\Scripts\python.exe -m pip install -r inference_server\requirements.txt

# データ作成 + 推論サーバー(app_test.yaml で inference_server.enabled を true にして)
.venv\Scripts\python.exe app.py --config app_test.yaml        # 失敗した製番/準備失敗があれば終了コード 1
```

テスト:
```
.venv\Scripts\python.exe -m unittest discover -s tests               # データ作成側(統合・E2E 含む)
inference_server\.venv\Scripts\python.exe -m unittest discover -s inference_server\tests   # サーバー単体
```

## 3. 安全条件と実装(Issue #52 の受入条件との対応)

| 条件 | 実装 |
|---|---|
| 検証起動で本番の `server.yaml`・共有出力を変更しない | サーバーは `--output-root` / `--config-out` を必須とし、設定は出力ルート配下にだけ書く。元コードの `shutil.copy`(本番共有へ `server.yaml` を複製)に当たる処理は無い。テストでソースに `shutil.copy` 等が無いことも確認 |
| 本番共有(またはその配下)は書き込み前に拒否 | `safety.assert_not_production`。`\\<本番ホスト>\…`・`//<本番ホスト>/…`・`\\?\UNC\…` 形を、**ファイルシステムに触れない字句判定を先に**行う(本番共有へ `realpath` 等のネットワーク照会を出さない)。UNC は既定で全て拒否 |
| loopback・検証専用ポート | bind は 127.0.0.1 のみ。ポートは 1024〜65535 の固定値(0/自動採番は不可)。使用中なら他のサーバーへ接続せず起動中止。`/health` の `instance_id` が自分の子プロセスと一致することも確認 |
| 準備できなければ処理を開始しない・原因がログで分かる | `manager.start()` が health/ready を待ち、失敗・終了・タイムアウトは `InferenceServerError`(原因とログ末尾付き)。`app.main` は処理開始前に例外を送出し、ログに「積算処理を開始しません」を残す |
| 終了時は自分の子だけ停止・子が残らない | 正常停止(`/shutdown` + instance_id)→ terminate → kill の順で、`Popen` したプロセスだけを対象にする。親が強制終了されても、サーバーが `--parent-pid` を監視して自分で終了(テストで確認) |
| work_dir がサーバーと同じ場所を指す | 検証モードは出力ルートを絶対パス化。依頼前に目印ファイル(`.probe_<nonce>`)をクライアントが置き、サーバーから見えることを `/verify_workdir` で確認(確認後に削除) |
| API が受け取る値の検証・出力ルート外の拒否 | `safety.validate_request`: work_dir は絶対パス・実体解決後に出力ルート配下(ルート自身・`..`・ジャンクション・本番共有は拒否)、page は数字1〜6桁、extension は png/jpeg/jpg |
| 再実行・途中失敗・同時実行 | 結果は一時ディレクトリへ書いてから `os.replace`(画像 → txt の順)。失敗時は既存ファイルを変更しない。同一(work_dir, page)は排他、モデル推論は直列化 |
| API 失敗を成功扱いにしない | `image_mod.slice_prediction_request` が失敗(HTTPエラー・接続失敗・応答不正・検出ファイル未生成)を `InferenceRequestError` で送出。`app.main` はその製番を `results["failed"]` に記録して出力を片付け、戻り値/終了コードで失敗を伝える。成功時の集計は従来どおり |
| `rmtree` を出力ルート配下に限定 | `app._safe_rmtree`(削除対象が `output_dir` 配下でなければ削除しない) |

## 4. 正本の調査結果(2026-10-09 時点)

**結論: 推論サーバーの正本・現行本番版は特定できていない。ローカルの `server.py` を正本と断定しない。**

確認した事実:
- 参照コピー: `<参照コピー>\server.py`(2025-12-01 更新、sha256 `a5f454db8050d1e3…`)。**git 管理外**(リモートなし)。
- 同じ `/predict` 契約を持つ別コピー: `<別系統のコピー>\app.py`(2025-11-20、出力先は別の共有)。コード系統が複数あり、どれが現行かの手掛かりは無い。
- 参照コピーの `server.log`(2026-02-09)に、本番共有の `server.yaml` を書いた起動の記録が残っている(**このコピーが本番に近い形で起動された形跡**だが、現行本番と同一の証拠ではない)。
- モデル: `model\best.pt`(52,047,755 バイト、2025-08-26、sha256 `6adf124ff1eeb658…`)。SekisanNavi の docs にある本番モデル名 `<本番モデル名>` と日付は符合するが、**同一ファイルかは未確認**(共有フォルダ上のモデルは読んでいない)。`model\version\` には 2024-08-27 の旧モデル(136,758,387 バイト)が2つ(同一内容)。
- 設定: `model.yaml` は yolov8 / `best.pt` / 閾値 0.81 / `cuda:0` / スライス 640・重なり 0.2。
- 依存関係(`requirements.txt`、UTF-16): Flask 3.0.3 / Werkzeug 3.0.2 / sahi 0.11.15 / ultralytics 8.2.2 / torch 2.6.0+cu124 / torchvision 0.21.0+cu124 / numpy 1.24.4 / opencv-python 4.7.0.72 / pillow 10.3.0。
- ハッシュ全文: server.py `a5f454db8050d1e3c667a32cf7470283b139adf4d2a97d6c8a8118a2a6935d1e` / model.yaml `ce8eb8ba0caf2afceb51e6e90490dc79ccf63802b72cab60863b14ab91bab007` / best.pt `6adf124ff1eeb658718ec3cf81d22508c7ead745b3a22f029c0459b49ffddb54` / requirements.txt `29a172567a1873d9cad6b7d8691cb708a493a080a8d30b7c3625c2056dd893b8`。

本統合の `inference_server/server.py` は、この参照コピーの `/predict` 契約(`work_dir`・`page_nmb`・`extension` → `{page}_detected.txt` / `{page}_detected.png`、応答 `{"text_file_path": …}`)を再現しつつ作り直したもの。**本番サーバーの置き換えではない。**

## 5. 元コードとの意図的な差(動作差の記録)
- 起動時の本番共有へのコピー・出力先固定を廃止(引数で明示)。
- `*_detected.txt` は **UTF-8** で書く(参照コピーは OS 既定の文字コード。データ作成側は UTF-8 で読むため。日本語クラス名はこちらの方が正しく渡る)。
- 検証モードはプロキシなし・loopback のみ・待ち受け前に設定ファイルを出力ルート配下へ書く。
- `prediction_visual.png` は固定名で衝突するため、リクエストごとの一時ディレクトリ経由にした。

## 6. 未確認・未実施(本番向け統合は完了していない)
- 実モデルでの**実図面**による検出結果の妥当性(§7 は合成画像での起動・推論・出力・停止まで。実図面は手元に無く未確認)。自動テストは `FakeBackend`。
- 本番サーバーの正本・版・モデルの対応、本番 `server.yaml` の内容(読んでいない)。
- 実機のホスト照会・設計サーバー・実 DWG の IJCAD 処理・Navi 実画面連携。
- 別PC構成(両者から見える UNC 共有)での `work_dir` 共有。現在は同一PC・ローカルパスのみ(UNC は既定で拒否)。
- 本番運用へ組み込む場合のサーバー管理者の承認、既存クライアントへの影響確認。

## 7. 実モデルでの検証(Issue #1、2026-10-09 実施)

手順(ローカル・loopback のみ。ホスト照会・設計サーバー・DWG・本番共有には接続しない):
```
.venv\Scripts\python.exe toolserify_real_model.py --python <推論用venv>\Scripts\python.exe --model-dir <model.yaml と best.pt のフォルダ> [--image 実画像.png]
```
`inference_server\.venv`(軽量)には sahi/ultralytics/torch が入っていないため、実モデルには別の推論用 venv の python を指定する(`app_test.yaml` の `inference_server.python`)。

結果: 参照コピーの venv(torch 2.5.1+cu124 / sahi 0.11.15 / ultralytics 8.2.2、CUDA 利用可)と参照モデル(`best.pt`、`model.yaml`)で、
SahiBackend が約 16 秒で ready → 推論 API で `{page}_detected.txt` / `.png` が出力 → `/shutdown` で終了コード 0 → 子プロセス・ポートとも残存なし(`RESULT: OK`)。
入力は合成画像(検出 0 件が正常)。同日に再実施しても同じ結果(約 10 秒で ready、出力 3 ファイル、終了コード 0、子プロセス・ポート残存なし)。検出ファイルに検出行がある場合の形式は FakeBackend のテストでのみ確認している。**実図面での検出結果・積算 CSV の値は未検証**。
注意: 参照 venv の torch は `requirements-real.txt` の固定版(2.6.0+cu124)と異なる(2.5.1+cu124)。固定版との差は未検証。

## 8. 検証設定の安全条件(追加)
- `app_test.yaml` の `dwg_conversion.converter` はローカルの ODA File Converter を指す。検証モード(`inference_server.enabled: true`)で本番共有上のパスを指定すると、起動・書き込み前に拒否する(`app.main`、テストあり)。IJCAD の `close y` は変更していない。

## 9. torch 版の差(2.5.1 と 2.6.0)の調査(2026-10-09)
- 参照コピーの requirements.txt(UTF-16)は `torch==2.6.0+cu124` / `torchvision==0.21.0+cu124`(`requirements-real.txt` はこれを写した)。
  一方、参照コピーの実 venv は `torch 2.5.1+cu124` / `torchvision 0.20.1+cu124`(その他の numpy 1.24.4 / opencv-python 4.7.0.72 / pillow 10.3.0 / sahi 0.11.15 / ultralytics 8.2.2 / Flask 3.0.3 / Werkzeug 3.0.2 は固定版と一致)。参照コピーの README は torch を固定せず cu124 の公式 index から入れる手順で、実 venv はその結果と見られる。
- 実機で確認できているのは **2.5.1 の組み合わせだけ**(§7)。2.6.0 での動作は未検証(導入していない)。
- 懸念(推定): torch 2.6 から `torch.load` の既定が `weights_only=True` になる。参照モデルを 2.5.1 上で `weights_only=True` で読むと `UnpicklingError` になるため、
  ultralytics 8.2.2(`weights_only` を指定せず読み込む)と 2.6.0 の組み合わせでは、モデルの読み込みに失敗する可能性がある。
- 対応案(依存版は変更していない): (A) 検証済みの 2.5.1 / 0.20.1 を `requirements-real.txt` の既定にする、(B) 2.6.0 を使うなら別の venv で実際に読み込めることを確認してから採用する(ultralytics の更新は挙動差が大きいので別途検証)。どちらにするかは本番サーバーの実環境の版を確認してから決める。

## 10. 正本の再調査(2026-10-09)
範囲: このPC内の `<参照コピー>`(server.py / model / requirements / README / server.log / venv の版)と本リポジトリのみ。共有フォルダ・認証情報・SekisanNavi・他リポジトリは読んでいない。
結果: §4 の結論は変わらない。参照コピーは「本番に近い形で起動された形跡(server.log)」まで確認できたが、現行本番との一致を示す資料はこのPC内に無い。特定には本番サーバーの管理者への確認(稼働中の server.py・モデルのハッシュ・pip freeze)が必要。

