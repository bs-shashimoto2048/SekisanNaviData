# セットアップ手順

このリポジトリには、**社内共通パッケージ `bs_pkg`(社内コード)は含まれていません**。クリーンな clone では、下記 3 を行って別途導入してください。

## 1. 取得と仮想環境

```
git clone https://github.com/bs-shashimoto2048/SekisanNaviData.git
cd SekisanNaviData
py -3.10 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. 推論サーバー用の別 venv(推論サーバー統合を使う場合のみ)

```
py -3.10 -m venv inference_server\.venv
inference_server\.venv\Scripts\python.exe -m pip install -r inference_server\requirements.txt
```
詳細は [`docs/inference_server.md`](inference_server.md)。

## 3. 社内共通パッケージ `bs_pkg` の入手と導入

`bs_pkg` は PyPI にも GitHub にもありません(社内コードのため公開しません)。**社内のパッケージ管理者から、配布物(wheel またはソース一式)を入手してください。**

```
.venv\Scripts\python.exe -m pip install --no-deps <入手した wheel のパス>
# ソース一式を手元に置いて開発する場合(Git 管理外に置くこと)
.venv\Scripts\python.exe -m pip install --no-deps -e <入手したソースのフォルダ>
```
- `--no-deps` を付けるのは、`bs_pkg` の宣言する依存関係の版固定が `requirements.txt` の版と食い違う場合があるためです。依存は `requirements.txt` 側で管理します。
- ソースを手元に置く場合は、**このリポジトリの `packages\bs_pkg\` に置いてください**(`.gitignore` 済みで、誤ってコミットされません)。

### 必要な版(機能)
古い配布版(0.1.0 の初期 wheel など)には、このプロジェクトが使う機能が**含まれていません**。次の確認で欠けがあれば、対応版を管理者に依頼してください。

```
.venv\Scripts\python.exe -c "from bs_pkg import host_mod, image_mod, design_mod, config_loader, util_mod; need={host_mod:['create_n3_design_list','get_missing_folders','get_property_data'], image_mod:['process_images_from_jpeg','log_formater','slice_prediction_request','InferenceRequestError'], design_mod:['search_server','output_print_script','print_dwg_for_pdf'], config_loader:['load_yaml','convert_win_path'], util_mod:['Logger']}; miss=[f'{m.__name__}.{n}' for m,ns in need.items() for n in ns if not hasattr(m,n)]; print('不足:', miss or 'なし')"
```
`image_mod.slice_prediction_request` は、引数 `use_proxy` / `timeout` / `verify_outputs` を持ち、失敗を `InferenceRequestError` で送出する版が必要です(テストが前提にしています)。

### 接続情報(ホスト照会)
ホスト照会の接続情報は、**Git 管理外のローカル設定ファイル(`*.local.yaml`)**に置きます。内容は社内の担当者から入手し、**コミット・共有しないでください**(`.gitignore` 済み)。

## 4. 動作確認

```
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m unittest discover -s tests
inference_server\.venv\Scripts\python.exe -m unittest discover -s inference_server\tests
```
`bs_pkg` が無い環境では、`bs_pkg` を import するテストが失敗します(`ModuleNotFoundError: bs_pkg`)。上記 3 を先に実施してください。
外部ホスト・設計サーバー・本番共有へは接続しません(テストはモックと loopback のみ)。

## 5. ローカル設定(共有パス・ホスト名など。Git 管理外)
このリポジトリには、社内のホスト名・共有パス・個人のローカルパスを書きません。次の**ローカル専用ファイル**に置きます(すべて `.gitignore` 済みで、コミットされません)。

| 用途 | ファイル | 作り方 |
|---|---|---|
| 本番のデータ作成設定(共有パス・変換ソフトのパス) | `app.local.yaml` | `app.yaml`(サンプル)を `app.local.yaml` にコピーし、`<SHARE_ROOT>` と `<CONVERTER_DIR>` を実際の値に置き換える。`app.py` は `app.local.yaml` があればそれを、無ければ `app.yaml` を読む。**サンプルのまま(`<...>` が残ったまま)では起動時にエラー**になる |
| 旧版(`app_20251204.py`)の設定 | `app_20251204.local.yaml` | 同上 |
| 推論サーバーの安全判定が「本番共有」とみなすホスト | `inference_server/production_hosts.local.txt`(1 行 1 ホスト、`#` 以降はコメント)または環境変数 `SEKISAN_PRODUCTION_HOSTS`(カンマ区切り) | 未設定のときは安全側に倒して、UNC パスはすべて本番扱いで拒否する(UNC の出力ルートは既定でも拒否) |
| 作業用スクリプト `0.py` / `1.py` / `create_template.py` の入出力先 | 環境変数 `SEKISAN_DESIGN_PRODUCT_DIR`(設計製番フォルダ)、`SEKISAN_OUTPUT_DIR`、`SEKISAN_TARGET_IMAGE`、`SEKISAN_TEMPLATE_DIR` | 未設定ならプレースホルダ(`<...>`)のまま。実際の値は各自の環境で設定する |

- 検証設定 `app_test.yaml` は、共有を指さないローカルのパスだけを持つ(そのままで使える)。
- 追跡対象のファイルに、社内ホスト名・共有パス・個人のローカルパス・案件(製番)・認証情報の実値を書かないこと。
