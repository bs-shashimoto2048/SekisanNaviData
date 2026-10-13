"""実モデル(SahiBackend)での推論サーバー検証スクリプト(検証専用・loopback のみ)。

起動 -> ready -> 推論API(slice_prediction_request)-> 検出ファイル出力 -> 停止 -> 子プロセス残存なし、を確認する。
ホスト照会・設計サーバー・DWG・本番共有には接続しない。入力画像は --image 未指定なら合成画像(検出 0 件が正常)。

  .venv\Scripts\python.exe tools\verify_real_model.py --python <推論用venvのpython> --model-dir <best.pt と model.yaml のフォルダ> [--image 実画像.png]
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import inference_support as isup  # noqa: E402
from bs_pkg import image_mod  # noqa: E402


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         encoding="cp932", errors="ignore").stdout
    return f'"{pid}"' in out


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", required=True, help="sahi/ultralytics/torch が入った推論用 venv の python.exe")
    ap.add_argument("--model-dir", required=True, help="model.yaml と重みファイルのあるフォルダ(読み取りのみ)")
    ap.add_argument("--image", help="推論に使う画像(PNG)。省略時は合成画像")
    ap.add_argument("--port", type=int, default=18766)
    ap.add_argument("--root", default=str(ROOT / "output_test" / "real_model_verify"), help="検証出力ルート(ローカル)")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    shutil.rmtree(root, ignore_errors=True)
    prod = root / "a_product" / "output" / "T-0001"
    prod.mkdir(parents=True)
    page = "21"
    if a.image:
        shutil.copyfile(a.image, prod / f"{page}.png")
    else:
        import cv2
        import numpy as np
        img = np.full((2970, 4200, 3), 255, np.uint8)
        cv2.rectangle(img, (500, 500), (1500, 1500), (0, 0, 0), 4)
        cv2.line(img, (0, 2000), (4200, 2000), (0, 0, 0), 3)
        cv2.imwrite(str(prod / f"{page}.png"), img)

    cfg = dict(enabled=True, port=a.port, backend="sahi", python=a.python, model_dir=a.model_dir, startup_timeout=300, stop_timeout=15)
    settings = isup.InferenceServerSettings.from_config(cfg)
    api_cfg = root / "api" / "server.yaml"
    mgr = isup.InferenceServerManager(settings, str(root), str(api_cfg), str(root / "logs"))
    ok = True
    t0 = time.monotonic()
    mgr.start()
    pid = mgr.proc.pid
    print(f"[ready] {time.monotonic() - t0:.1f}s pid={pid} backend=sahi")
    try:
        isup.verify_shared_work_dir(mgr.base_url, str(prod))
        log = image_mod.slice_prediction_request(str(prod), [page], str(api_cfg), use_proxy=False, timeout=600, verify_outputs=True)
        print("[predict] OK")
        txt = prod / f"{page}_detected.txt"
        png = prod / f"{page}_detected.png"
        print(f"[outputs] txt={txt.is_file()} png={png.is_file()} detections={len(txt.read_text(encoding='utf-8').splitlines()) if txt.is_file() else '-'}")
        ok &= txt.is_file() and png.is_file()
    finally:
        mgr.stop()
    gone = False
    for _ in range(50):
        if not pid_alive(pid) and not port_open(a.port):
            gone = True
            break
        time.sleep(0.2)
    print(f"[stop] returncode={mgr.returncode} child_gone={gone}")
    ok &= gone and mgr.returncode == 0
    print("RESULT:", "OK" if ok else "NG")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
