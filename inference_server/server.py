"""SekisanNaviData 推論サーバー(検証モード専用)。

由来: <参照コピー>\\server.py (2025-12-01, git管理外・正本未確認。docs/inference_server.md 参照)を元に、
Issue #52 の方針で安全面を作り直したもの。**本番稼働中のサーバーとは別物**であり、本番共有には一切書き込まない。

元コードとの主な違い:
  - 出力ルート/ポート/設定出力先を引数で明示する(必須)。bind は loopback のみ。本番共有(\\\\<本番ホスト>\\...)配下は拒否。
  - 起動時に server.yaml を本番共有へコピーしない。設定は --config-out(出力ルート配下)へだけ書く。
  - /predict は work_dir・ページ番号・拡張子を検証し、出力ルートの外の読み書きを拒否する。
  - 結果は一時ディレクトリへ書いてから置き換える(再実行・途中失敗でも既存ファイルを壊さない。同一対象は排他)。
  - /health は準備完了(ready)・読み込み中・失敗を区別して返す。親プロセスが消えたら自分で終了する(--parent-pid)。
  - *_detected.txt は UTF-8 で書く(データ作成側が UTF-8 で読む。元コードは OS 既定の文字コードだった)。
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import signal
import sys
import threading
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import safety  # noqa: E402
from backends import VISUAL_NAME  # noqa: E402

log = logging.getLogger("inference_server")
TMP_DIRNAME = ".infer_tmp"
_NONCE_RE = re.compile(r"^[0-9a-f]{8,64}$")


class ServerState:
    def __init__(self, output_root: str, instance_id: str, backend_name: str):
        self.output_root = os.path.realpath(output_root)
        self.instance_id = instance_id
        self.backend_name = backend_name
        self.backend = None
        self.ready = False
        self.error = None
        self.shutdown_callback = None            # main() が設定(/shutdown から呼ぶ)
        self._predict_lock = threading.Lock()   # モデルの推論は直列化(GPU/モデルのスレッド安全性が不明なため)
        self._target_locks = {}                 # (work_dir, page) ごとの排他(同一ページの同時書き込み防止)
        self._locks_guard = threading.Lock()

    def target_lock(self, key):
        with self._locks_guard:
            return self._target_locks.setdefault(key, threading.Lock())


def _clean_device(name: str) -> str:
    # 出力形式の区切り文字・改行を含むクラス名で形式が壊れないようにする
    return re.sub(r"[|$\r\n]+", "_", str(name))


def _format_detections(dets) -> str:
    lines = []
    for idx, d in enumerate(dets):
        x1, y1, x4, y4 = float(d.minx), float(d.miny), float(d.maxx), float(d.maxy)
        w, h = abs(x1 - x4), abs(y1 - y4)
        coords = f"{x1},{y1}:{x1 + w},{y1}:{x1},{y1 + h}:{x4},{y4}"
        lines.append(f"Index${idx}|Device${_clean_device(d.name)}|Score${d.score}|Coordinates${coords}")
    return "".join(line + "\n" for line in lines)


def create_app(state: ServerState):
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    def err(status, message):
        return jsonify({"error": message}), status

    @app.get("/health")
    def health():
        body = {"instance_id": state.instance_id, "backend": state.backend_name, "mode": "test",
                "output_root": state.output_root, "pid": os.getpid()}
        if state.error:
            return jsonify({**body, "status": "failed", "ready": False, "error": state.error}), 500
        if not state.ready:
            return jsonify({**body, "status": "loading", "ready": False}), 503
        return jsonify({**body, "status": "ok", "ready": True}), 200

    @app.post("/shutdown")
    def shutdown():
        """起動元(データ作成側)による正常停止。instance_id が一致する場合だけ受け付ける(loopback 限定のサーバー内でさらに照合)。"""
        if request.form.get("instance_id") != state.instance_id:
            return err(403, "instance_id が一致しません。")
        if state.shutdown_callback is None:
            return err(501, "停止手段が設定されていません。")
        threading.Thread(target=state.shutdown_callback, daemon=True).start()
        return jsonify({"ok": True})

    @app.post("/verify_workdir")
    def verify_workdir():
        """クライアントが work_dir に置いた目印ファイルを、サーバー側から見えるか確認する(同じ場所を指すことの確認)。"""
        try:
            work_dir = request.form.get("work_dir", "")
            nonce = request.form.get("nonce", "")
            real_dir = safety.validate_work_dir(work_dir, state.output_root)
            if not _NONCE_RE.match(nonce):
                raise safety.SafetyError("nonce が不正です。")
            if not os.path.isfile(os.path.join(real_dir, f".probe_{nonce}")):
                return err(409, "サーバーから work_dir 内の目印ファイルが見えません(クライアントとサーバーが別の場所を指している)。")
            return jsonify({"ok": True, "work_dir": real_dir})
        except safety.SafetyError as e:
            return err(400, str(e))

    @app.post("/predict")
    def predict():
        if state.error:
            return err(500, "サーバーの準備に失敗しています。")
        if not state.ready:
            return err(503, "サーバーは準備中です。")
        try:
            real_dir, page, ext, image = safety.validate_request(
                request.form.get("work_dir"), request.form.get("page_nmb"), request.form.get("extension"), state.output_root)
        except safety.SafetyError as e:
            log.warning("リクエスト拒否: %s", e)
            return err(400, str(e))

        tmp_dir = os.path.join(state.output_root, TMP_DIRNAME, uuid.uuid4().hex)
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
                    # 可視化画像 -> 検出テキストの順で置き換える(txt は後続処理が読む本体。画像が揃ってから現れる)
                    if os.path.isfile(visual_tmp):
                        os.replace(visual_tmp, png_dst)
                    os.replace(txt_tmp, txt_dst)
                finally:
                    _remove_tmp_dir(tmp_dir, state.output_root)
            return jsonify({"text_file_path": txt_dst, "detections": len(dets)})
        except Exception as e:  # noqa: BLE001  (内部情報は応答に出さず、型名と短いメッセージのみ)
            log.exception("推論に失敗 page=%s", page)
            return err(500, f"推論に失敗しました: {type(e).__name__}: {str(e)[:200]}")

    return app


def _remove_tmp_dir(tmp_dir: str, output_root: str):
    """自分が作った一時ディレクトリだけを、出力ルート配下であることを確認してから削除する。"""
    if os.path.basename(os.path.dirname(tmp_dir)) == TMP_DIRNAME and safety.is_under(tmp_dir, output_root):
        shutil.rmtree(tmp_dir, ignore_errors=True)


def parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def write_config_atomically(path: str, text: str):
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def build_backend(args):
    if args.backend == "fake":
        from backends import FakeBackend
        return FakeBackend(args.fake_script)
    from backends import SahiBackend
    if not args.model_dir:
        raise SystemExit("--backend sahi には --model-dir が必要です。")
    return SahiBackend(args.model_dir, args.model_yaml)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="SekisanNaviData 推論サーバー(検証モード専用)")
    p.add_argument("--output-root", required=True, help="検証用の出力ルート(本番共有配下は拒否)")
    p.add_argument("--port", type=int, required=True, help="検証専用の固定ポート(1024-65535)")
    p.add_argument("--host", default="127.0.0.1", help="loopback のみ指定可")
    p.add_argument("--config-out", required=True, help="検出用 server.yaml の書き出し先(出力ルート配下)")
    p.add_argument("--instance-id", default=None, help="起動元が /health で自分の子プロセスと確認するための識別子")
    p.add_argument("--backend", choices=("fake", "sahi"), default="sahi")
    p.add_argument("--model-dir", default=None)
    p.add_argument("--model-yaml", default="model.yaml")
    p.add_argument("--fake-script", default=None, help="--backend fake の検出結果(JSON)")
    p.add_argument("--parent-pid", type=int, default=0, help="このPIDが消えたら自分で終了する")
    p.add_argument("--allow-unc", action="store_true", help="UNCの出力ルートを許可(本番共有は常に拒否)")
    return p.parse_args(argv)


def prepare(args):
    """引数を検証し、(出力ルート, config_out, instance_id) を返す。違反は SafetyError。書き込みはまだ行わない。"""
    safety.validate_loopback(args.host)
    safety.validate_port(args.port)
    root = safety.validate_output_root(args.output_root, allow_unc=args.allow_unc)
    safety.assert_not_production(args.config_out, "設定出力先")
    safety.assert_under(args.config_out, str(root), "設定出力先")
    return root, os.path.abspath(args.config_out), args.instance_id or uuid.uuid4().hex


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        root, config_out, instance_id = prepare(args)
    except safety.SafetyError as e:
        log.error("起動を拒否: %s", e)
        return 2

    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.dirname(config_out), exist_ok=True)
    state = ServerState(str(root), instance_id, args.backend)
    app = create_app(state)

    from werkzeug.serving import make_server
    try:
        httpd = make_server(args.host, args.port, app, threaded=True)
    except OSError as e:
        log.error("ポート %s を bind できません: %s", args.port, e)
        return 3

    # server.yaml(クライアントが読む接続情報)。本番共有ではなく出力ルート配下にだけ書く。proxy 項目は書かない。
    write_config_atomically(config_out, (
        f"host:\n  ip: {args.host}\n  port: {args.port}\n  pc_ip: {args.host}\n"
        f"directory:\n  output: '{root}'\nmode: test\ninstance_id: {instance_id}\nbackend: {args.backend}\n"))
    log.info("起動: instance=%s backend=%s root=%s port=%s config=%s", instance_id, args.backend, root, args.port, config_out)

    def load():
        try:
            state.backend = build_backend(args)
            state.ready = True
            log.info("準備完了(ready)")
        except BaseException as e:  # noqa: BLE001
            state.error = f"{type(e).__name__}: {str(e)[:300]}"
            log.exception("バックエンドの準備に失敗")

    threading.Thread(target=load, name="load-backend", daemon=True).start()

    def stop(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    state.shutdown_callback = httpd.shutdown

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, stop)

    if args.parent_pid:
        def watch():
            while True:
                threading.Event().wait(1.0)
                if not parent_alive(args.parent_pid):
                    log.warning("親プロセス(%s)が終了したため停止します", args.parent_pid)
                    stop()
                    return
        threading.Thread(target=watch, name="parent-watch", daemon=True).start()

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        try:  # 自分が書いた設定だけを消す(別インスタンスのものは消さない)
            if f"instance_id: {instance_id}" in Path(config_out).read_text(encoding="utf-8"):
                os.remove(config_out)
        except OSError:
            pass
        log.info("停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
