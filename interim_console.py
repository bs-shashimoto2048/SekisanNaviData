# 暫定 API 操作用 UI の起動入口(ローカル専用。運用画面 ops_console.py とは独立)。
#   .venv\Scripts\python.exe interim_console.py serve [--port N]     ブラウザで http://127.0.0.1:N/ を開く
#   .venv\Scripts\python.exe interim_console.py run-scheduled [--dry-run]   仮定期実行(専用タスクが呼ぶ)。全対象製番を通常処理で 1 回実行する
# 画面から、Sahi の暫定 API の起動・状態の更新・合成 PNG 1 枚のスモーク・停止、製番 1 件の指定実行、全製番の仮定期実行(タスクの登録・解除)ができる
# (設計: docs/inference_api_interim_operation.md §15・§16)。製番処理の出力先は隔離領域 output_interim だけ(app_interim.yaml)。
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


def cmd_run_scheduled(dry_run: bool) -> int:
    """専用タスク(SekisanNaviData_InterimOps)の起動入口。結果は履歴(種別「定期」/「定期(検証)」)に残る。終了コードは、失敗のときだけ 1。"""
    from interim.runs import InterimRuns, IsolationError
    try:
        runs = InterimRuns(BASE_DIR)
        status = runs.run_scheduled(dry_run=dry_run)
    except IsolationError:
        print("出力先が隔離領域であることを確認できないため、実行しませんでした。")
        return 1
    return 1 if status == "失敗" else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="暫定 API 操作用 UI(ローカル専用)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="画面を 127.0.0.1 で起動する")
    s.add_argument("--port", type=int, default=None, help="省略時は 8771〜8790 の空いている最初のポート")
    r = sub.add_parser("run-scheduled", help="仮定期実行(専用タスクが呼ぶ。全対象製番を通常処理で 1 回)")
    r.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="検証用: ロック・COM・設定の読込と履歴の記録だけ行い、製番処理(app.main)は実行しない")
    args = ap.parse_args(argv)

    import os
    os.chdir(BASE_DIR)                          # タスクスケジューラの既定の作業フォルダは System32 のため、リポジトリに固定する
    if args.cmd == "run-scheduled":
        return cmd_run_scheduled(args.dry_run)
    from interim.manager import ApiManager, port_is_free
    from interim.runs import InterimRuns
    from interim.server import make_server

    port = args.port if args.port is not None else pick_ui_port()
    if port is None or not (1024 <= port <= 65535) or not port_is_free(port):
        print("画面用の空いているポートを確保できませんでした(--port で別の値を指定してください)。")
        return 2
    manager = ApiManager(BASE_DIR)

    def api_ready() -> bool:
        a = manager.snapshot()["api"]
        return a["state"] in ("ready", "external") and a["http"] == 200

    runs = InterimRuns(BASE_DIR, api_ready=api_ready)
    runs.mark_orphans()
    server = make_server(manager, port, runs=runs)
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
