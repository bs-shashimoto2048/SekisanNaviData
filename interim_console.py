# 暫定 API 操作用 UI の起動入口(ローカル専用。運用画面 ops_console.py とは独立)。
#   .venv\Scripts\python.exe interim_console.py serve [--port N]     ブラウザで http://127.0.0.1:N/ を開く
# 画面から、Sahi の暫定 API の起動・状態の更新・合成 PNG 1 枚のスモーク・停止ができる(設計: docs/inference_api_interim_operation.md §15)。
# モデルフォルダと推論用 venv の python は interim_console.local.yaml(Git 管理外。キー model_dir / python)から読む。値は画面・ログに出さない。
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UI_PORTS = range(8771, 8791)         # 画面の既定ポート(空いている最初のもの。127.0.0.1 のみ)


def pick_ui_port(is_free=None):
    from interim.manager import port_is_free
    is_free = is_free or port_is_free
    return next((p for p in UI_PORTS if is_free(p)), None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="暫定 API 操作用 UI(ローカル専用)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="画面を 127.0.0.1 で起動する")
    s.add_argument("--port", type=int, default=None, help="省略時は 8771〜8790 の空いている最初のポート")
    args = ap.parse_args(argv)

    import os
    os.chdir(BASE_DIR)
    from interim.manager import ApiManager, port_is_free
    from interim.server import make_server

    port = args.port if args.port is not None else pick_ui_port()
    if port is None or not (1024 <= port <= 65535) or not port_is_free(port):
        print("画面用の空いているポートを確保できませんでした(--port で別の値を指定してください)。")
        return 2
    server = make_server(ApiManager(BASE_DIR), port)
    print(f"暫定 API 操作画面: http://127.0.0.1:{port}/  (Ctrl+C で終了。API は止まりません)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
