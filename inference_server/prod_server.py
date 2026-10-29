"""SekisanNaviData 本番推論 API。

旧サーバー(参照コピー server.py)・検証用 `server.py` とは別のエントリ。**旧サーバーの問題を作り直したもの**:
  - /health がある(準備完了 200 / 読み込み中 503 / 失敗 500。モデルの識別情報つき)。
  - 待受は設定で明示した IPv4 アドレス 1 つだけ(全インターフェース・ホスト名・IPv6 は拒否)。ポートは固定(自動採番しない)。
  - 接続元は設定の許可リスト(IP / CIDR)に含まれるものだけ(それ以外は 403)。任意で /predict に Bearer トークンを要求できる。
  - 起動時に共有 server.yaml を勝手に書かない。`--publish-config` を明示したときだけ、安全条件のもとで原子的に更新する(prod_publish.py)。
  - モデルは設定に明示した期待値(サイズ・SHA-256)と照合し、一致しなければ起動しない。**検証したバイト列そのもの**(ステージングしたコピー)を読み込む(prod_model.py)。
  - /predict は、設定した出力ルートの**直下の製番フォルダ**だけを受け付ける。結果は一時フォルダ経由で置き換える。
  - /shutdown・/verify_workdir は持たない(停止はシグナルだけ)。例外の詳細・パスは応答にも**ログにも**出さず、型名と呼び出し位置だけ残す。

使い方(詳細は docs/production_inference_api.md):
  python prod_server.py check --config prod_server.local.yaml              # 設定・モデル・公開計画の確認(待ち受けも書き込みもしない)
  python prod_server.py serve --config prod_server.local.yaml [--publish-config [--replace-stale]]
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import shutil
import signal
import socket
import sys
import threading
import traceback
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import prod_config  # noqa: E402
import prod_model  # noqa: E402
import prod_publish  # noqa: E402
from backends import VISUAL_NAME  # noqa: E402
from server import _format_detections  # noqa: E402  検出結果の書式は検証用サーバーと共通(UTF-8)

log = logging.getLogger("prod_inference_server")
TMP_PREFIX = ".infer_tmp_"
ALLOWED_EXTENSIONS = ("png", "jpeg", "jpg")
_PAGE_RE = re.compile(r"[0-9]{1,6}")
_PRODUCT_RE = re.compile(r"[A-Za-z0-9]{4,20}")              # 製番(ops の指定実行と同じ: 英数字 4〜20 文字)。fullmatch で使う(末尾の改行を許さない)
MAX_REQUEST_BYTES = 16 * 1024
MAX_CONCURRENT_PREDICT = 8                                   # 同時に受け付ける /predict の数(超過は 503)。推論自体は直列
MIN_TOKEN_LEN = 24

EXIT_CONFIG, EXIT_BIND, EXIT_MODEL, EXIT_PUBLISH, EXIT_LOAD = 2, 3, 4, 5, 6


class RequestRejected(ValueError):
    """/predict の入力が条件に反する。メッセージは固定文(値を含めない)。"""


def _same(a: str, b: str) -> bool:
    n = lambda p: os.path.normcase(os.path.normpath(p.replace("/", "\\"))) if os.name == "nt" else os.path.normpath(p)  # noqa: E731
    return n(a) == n(b)


def safe_trace(e: BaseException) -> str:
    """例外の型名と、呼び出し位置(ファイル名・行番号・関数名)だけ。例外メッセージ(パスを含み得る)は含めない。"""
    frames = traceback.extract_tb(e.__traceback__)[-4:]
    where = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(frames))
    return f"{type(e).__name__} @ {where}" if where else type(e).__name__


def validate_prod_request(work_dir, page_nmb, extension, output_root: str):
    """(work_dir 実体, ページ番号, 拡張子, 画像パス, クライアントが送った表記の work_dir)を返す。条件:
    page_nmb は数字 1〜6 桁 / extension は png・jpeg・jpg / work_dir は絶対パスで、**output_root の直下の製番フォルダ**
    (製番は英数字 4〜20 文字)/ リンク・ジャンクションで別の場所を指していない / 画像は通常ファイルでリンクでない。
    実際に使うパスは、受け取った文字列ではなく `realpath(output_root) + 検証済みの製番名` から組み立てる。"""
    if not isinstance(page_nmb, str) or not _PAGE_RE.fullmatch(page_nmb):
        raise RequestRejected("page_nmb は数字(1〜6桁)のみ指定できます。")
    ext = (extension or "").lower() if isinstance(extension, str) else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise RequestRejected(f"extension は {', '.join(ALLOWED_EXTENSIONS)} のみ指定できます。")
    if not isinstance(work_dir, str) or not work_dir.strip() or "\x00" in work_dir or not prod_config._is_abs(work_dir):
        raise RequestRejected("work_dir は絶対パスで指定してください。")
    if ".." in re.split(r"[\\/]+", work_dir):
        raise RequestRejected("work_dir に .. は使えません。")
    given = work_dir.replace("\\", "/").rstrip("/")
    parent, _, product = given.rpartition("/")
    if not _PRODUCT_RE.fullmatch(product):
        raise RequestRejected("work_dir の製番フォルダ名が不正です。")
    if not _same(parent, output_root):
        raise RequestRejected("work_dir は許可された出力ルートの直下の製番フォルダではありません。")
    expected_dir = os.path.join(os.path.realpath(output_root), product)
    real_dir = os.path.realpath(expected_dir)
    if not _same(real_dir, expected_dir) or not os.path.isdir(real_dir):
        raise RequestRejected("work_dir が存在しない、またはリンクです。")
    image = os.path.join(real_dir, f"{page_nmb}.{ext}")
    if os.path.islink(image) or not os.path.isfile(image) or not _same(os.path.realpath(image), image):
        raise RequestRejected("対象の画像ファイルが存在しない、またはリンクです。")
    return real_dir, page_nmb, ext, image, given


class ProdState:
    def __init__(self, cfg: prod_config.ProdConfig, instance_id: str, identity: prod_model.ModelIdentity, token: bytes | None = None):
        self.cfg = cfg
        self.instance_id = instance_id
        self.identity = identity
        self.token = token
        self.backend = None
        self.ready = False
        self.error = None
        self._predict_lock = threading.Lock()   # モデルの推論は直列化(GPU・モデルのスレッド安全性が不明なため。検証用サーバーと同じ)
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_PREDICT)
        self._target_locks = {}
        self._guard = threading.Lock()

    def target_lock(self, key):
        with self._guard:
            return self._target_locks.setdefault(key, threading.Lock())


def _remove_tmp(tmp_dir: str, work_dir: str):
    if os.path.basename(tmp_dir).startswith(TMP_PREFIX) and _same(os.path.dirname(tmp_dir), work_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)


def create_app(state: ProdState):
    from flask import Flask, jsonify, request

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    cfg = state.cfg

    def err(status, message):
        return jsonify({"error": message}), status

    @app.before_request
    def guard():
        # 接続元の許可リスト(プロキシ経由のとき remote_addr はプロキシの IP になる。X-Forwarded-For 等のヘッダーは信用しない)
        if not prod_config.client_allowed(cfg, request.remote_addr):
            log.warning("接続元が許可リストに無いため拒否: %s %s", request.method, request.path)
            return err(403, "許可されていない接続元です。")
        if state.token is not None and request.method == "POST" and request.path == "/predict":
            auth = request.headers.get("Authorization", "")
            given = auth[7:].strip().encode("utf-8") if auth.lower().startswith("bearer ") else b""
            if not hmac.compare_digest(given, state.token):
                return err(401, "認証に失敗しました。")
        return None

    @app.get("/health")
    def health():
        # 状態(200/503/500)と本文のキーが契約。パス・アドレス・プロセス ID・ハッシュ全文・例外メッセージは返さない(失敗時の error は型名だけ)
        body = {"instance_id": state.instance_id, "mode": "production", "model": state.identity.public()}
        if state.error:
            return jsonify({**body, "status": "failed", "ready": False, "error": str(state.error).split(":", 1)[0][:80]}), 500
        if not state.ready:
            return jsonify({**body, "status": "loading", "ready": False}), 503
        return jsonify({**body, "status": "ok", "ready": True}), 200

    @app.post("/predict")
    def predict():
        if state.error:
            return err(500, "サーバーの準備に失敗しています。")
        if not state.ready:
            return err(503, "サーバーは準備中です。")
        try:
            real_dir, page, ext, image, given = validate_prod_request(
                request.form.get("work_dir"), request.form.get("page_nmb"), request.form.get("extension"), cfg.output_root)
        except RequestRejected as e:
            log.warning("リクエスト拒否: %s", e)
            return err(400, str(e))
        if not state._slots.acquire(blocking=False):
            return err(503, "同時リクエスト数の上限です。しばらくしてから再試行してください。")
        tmp_dir = os.path.join(real_dir, f"{TMP_PREFIX}{uuid.uuid4().hex}")
        try:
            with state.target_lock((real_dir, page)):
                os.makedirs(tmp_dir)
                try:
                    with state._predict_lock:
                        dets = state.backend.predict(image, tmp_dir)
                    txt_tmp = os.path.join(tmp_dir, "detected.txt")
                    with open(txt_tmp, "w", encoding="utf-8", newline="\n") as f:
                        f.write(_format_detections(dets))
                    visual_tmp = os.path.join(tmp_dir, VISUAL_NAME)
                    txt_dst = os.path.join(real_dir, f"{page}_detected.txt")
                    png_dst = os.path.join(real_dir, f"{page}_detected.png")
                    # 書き込み直前に、製番フォルダが検証後にリンクへ差し替えられていないことを再確認する
                    if not _same(os.path.realpath(real_dir), real_dir):
                        raise RequestRejected("work_dir が検証後に変更されました。")
                    # 可視化画像 → 検出テキストの順で置き換える(txt は後続処理が読む本体。画像が揃ってから現れる)
                    if os.path.isfile(visual_tmp):
                        os.replace(visual_tmp, png_dst)
                    os.replace(txt_tmp, txt_dst)
                finally:
                    _remove_tmp(tmp_dir, real_dir)
            # 応答のパスは、クライアントが送った表記(検証済み)から作る(realpath による別表記の UNC を返さない)
            shown = os.path.join(given.replace("/", os.sep), f"{page}_detected.txt")
            return jsonify({"text_file_path": shown, "detections": len(dets)})
        except RequestRejected as e:
            return err(400, str(e))
        except Exception as e:  # noqa: BLE001  応答もログも、型名と呼び出し位置だけ(例外メッセージはパスを含み得る)
            log.error("推論に失敗 page=%s: %s", page, safe_trace(e))
            return err(500, f"推論に失敗しました: {type(e).__name__}")
        finally:
            state._slots.release()

    return app


# ----------------------------------------------------------------------------- check / serve
def _read_token(path: str) -> bytes:
    try:
        t = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        raise prod_config.ConfigError("トークンファイルを読み込めません: auth.token_file") from None
    if len(t) < MIN_TOKEN_LEN:
        raise prod_config.ConfigError(f"トークンが短すぎます({MIN_TOKEN_LEN} 文字以上): auth.token_file")
    return t.encode("utf-8")


def _check_model_in_place(cfg: prod_config.ProdConfig):
    """`check` 用: 元のフォルダを読み取って識別・照合する(ステージングしない)。(識別情報, 要約)。"""
    m = cfg.model
    ident = prod_model.identify(m.id, m.dir, m.weights, m.yaml_name)
    prod_model.verify(ident, m.weights_sha256, m.weights_size, m.yaml_sha256)
    return ident, prod_model.check_yaml_matches_weights(m.dir, m.yaml_name, m.weights)


def _port_busy(host: str, port: int) -> bool:
    """待ち受け先のポートが既に使われているか。指定アドレスと loopback へ接続を試す(Windows では、ワイルドカードで待ち受ける旧サーバーが
    あっても、特定アドレスの bind が成功することがあるため、bind の成否だけに頼らない)。接続して即閉じるだけ(データは送らない)。"""
    for h in dict.fromkeys((host, "127.0.0.1")):
        try:
            socket.create_connection((h, port), timeout=0.5).close()
            return True
        except OSError:
            continue
    return False


def run_check(cfg: prod_config.ProdConfig) -> dict:
    """設定・環境・モデル・公開計画の確認。待ち受けもファイルの書き込みもしない(出力ルート・既存 server.yaml・モデルの読み取りと、
    既存 server.yaml の接続先への /health 1 回だけ)。値(パス・アドレス・ハッシュ全文)は含めない。"""
    out: dict = {"config": "ok", "ok": True}
    try:
        local_ips = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)} | {"127.0.0.1"}
    except OSError:
        local_ips = {"127.0.0.1"}
    in_use = _port_busy(cfg.host, cfg.port)
    out["bind"] = {"scope": "loopback" if cfg.host.startswith("127.") else "single-lan-address",
                   "address_assigned_to_this_pc": cfg.host in local_ips, "port_in_use": in_use}
    out["allowed_clients"] = {"count": len(cfg.allowed_clients), "narrowest_prefix": max(n.prefixlen for n in cfg.allowed_clients),
                              "widest_prefix": min(n.prefixlen for n in cfg.allowed_clients)}
    out["output_root"] = {"exists": os.path.isdir(cfg.output_root)}
    out["auth"] = {"predict_token": "enabled" if cfg.token_file else "disabled"}
    ident = None
    try:
        ident, summary = _check_model_in_place(cfg)
        out["model"] = {"verified": True, **ident.public(), "device": summary["device"], "type": summary["model_type"]}
    except prod_model.ModelVerificationError as e:
        out["model"] = {"verified": False, "error": str(e)}
    if ident is not None:
        out["publish_plan"] = prod_publish.plan_publish(cfg, "check-only", ident.public(), replace_stale=False).public()
    else:
        out["publish_plan"] = None
    out["ok"] = bool(out["bind"]["address_assigned_to_this_pc"] and not in_use and out["output_root"]["exists"]
                     and out["model"]["verified"])
    return out


def serve(cfg: prod_config.ProdConfig, *, publish_config: bool = False, replace_stale: bool = False,
          backend_factory=None, on_started=None) -> int:
    """API を起動する。`backend_factory(cfg, model_dir)` は、**ステージングした(検証済みの)モデルフォルダ**から読み込むこと。"""
    instance_id = uuid.uuid4().hex
    try:
        token = _read_token(cfg.token_file) if cfg.token_file else None
    except prod_config.ConfigError as e:
        log.error("起動を拒否: %s", e)
        return EXIT_CONFIG
    m = cfg.model
    try:
        identity, staged = prod_model.stage_and_verify(m.id, m.dir, m.weights, m.yaml_name, m.weights_sha256, m.weights_size, m.yaml_sha256)
    except prod_model.ModelVerificationError as e:
        log.error("起動を拒否(モデルの検証に失敗): %s", e)
        return EXIT_MODEL
    try:
        prod_model.check_yaml_matches_weights(staged, m.yaml_name, m.weights)        # ステージングしたコピーから解釈する
    except prod_model.ModelVerificationError as e:
        prod_model.remove_staging(staged)
        log.error("起動を拒否(モデルの検証に失敗): %s", e)
        return EXIT_MODEL

    def cleanup_and_return(code):
        prod_model.remove_staging(staged)
        return code

    if publish_config:                                   # 公開できない状況なら、待ち受ける前に止める
        plan = prod_publish.plan_publish(cfg, instance_id, identity.public(), replace_stale=replace_stale)
        if plan.action == "refuse":
            log.error("起動を拒否(接続設定を公開できません): %s", plan.refuse_code)
            return cleanup_and_return(EXIT_PUBLISH)
    if _port_busy(cfg.host, cfg.port):
        log.error("ポートを待ち受けできません: 既に使用されています")
        return cleanup_and_return(EXIT_BIND)

    state = ProdState(cfg, instance_id, identity, token)
    app = create_app(state)
    from werkzeug.serving import make_server
    try:
        httpd = make_server(cfg.host, cfg.port, app, threaded=True)
    except (OSError, SystemExit) as e:   # werkzeug は使用中のポートで sys.exit(1) することがある(終了コードを統一するため捕捉する)
        log.error("ポートを待ち受けできません: %s", type(e).__name__)
        return cleanup_and_return(EXIT_BIND)
    log.info("起動: instance=%s model=%s(%s) allowed_clients=%d publish=%s", instance_id, identity.id,
             identity.weights_sha256[:prod_model.PREFIX_LEN], len(cfg.allowed_clients), publish_config)
    exit_code = {"v": 0}

    def stop(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    def load():
        try:
            try:
                if backend_factory is not None:
                    state.backend = backend_factory(cfg, staged)
                else:
                    from backends import SahiBackend
                    state.backend = SahiBackend(staged, m.yaml_name)
                if not prod_model.unchanged_since(identity, staged, m.weights, m.yaml_name, rehash=True):   # ステージングしたコピーが読み込み中に変わっていない
                    raise prod_model.ModelVerificationError("読み込み中にモデルのコピーが変更されました。")
            finally:
                prod_model.remove_staging(staged)         # 読み込みが終わったら、コピーは不要(失敗時も消す)
        except BaseException as e:  # noqa: BLE001
            state.ready = False
            state.error = f"{type(e).__name__}: load"
            log.error("バックエンドの準備に失敗して停止します: %s", safe_trace(e))
            exit_code["v"] = EXIT_LOAD
            stop()
            return
        if publish_config:       # 準備完了のうえで、接続設定の公開に成功してから ready にする(公開できなかったら API を提供しない)
            try:
                result = prod_publish.publish(cfg, instance_id, identity.public(), replace_stale=replace_stale)
                log.info("接続設定を公開しました: action=%s backup=%s", result["action"], bool(result["backup"]))
            except prod_publish.PublishRefused as e:
                state.error = f"PublishRefused: {e.code}"
                log.error("接続設定を公開できないため停止します: %s", e.code)
                exit_code["v"] = EXIT_PUBLISH
                stop()
                return
            except BaseException as e:  # noqa: BLE001
                state.error = f"{type(e).__name__}: publish"
                log.error("接続設定の公開中に失敗して停止します: %s", safe_trace(e))
                exit_code["v"] = EXIT_PUBLISH
                stop()
                return
        state.ready = True
        log.info("準備完了(ready)")

    threading.Thread(target=load, name="load-backend", daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, stop)
            except ValueError:   # テストなどメインスレッド以外
                pass
    if on_started is not None:
        on_started(httpd, state)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        prod_model.remove_staging(staged)
        log.info("停止(接続設定ファイルは削除しません。次回の起動時に --replace-stale で更新します)")
    return exit_code["v"]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="SekisanNaviData 本番推論 API")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="設定・環境・モデル・公開計画を確認する(待ち受けも書き込みもしない)")
    c.add_argument("--config", required=True)
    s = sub.add_parser("serve", help="API を起動する")
    s.add_argument("--config", required=True)
    s.add_argument("--publish-config", action="store_true", help="準備完了後に共有 server.yaml を更新する(既定は書かない)")
    s.add_argument("--replace-stale", action="store_true", help="既存の server.yaml の接続先が接続を拒否する(古い)場合に上書きを許可する")
    return p.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        cfg = prod_config.load_config(args.config)
    except prod_config.ConfigError as e:
        log.error("設定を拒否: %s", e)
        return EXIT_CONFIG
    if args.cmd == "check":
        result = run_check(cfg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    return serve(cfg, publish_config=args.publish_config, replace_stale=args.replace_stale)


if __name__ == "__main__":
    sys.exit(main())
