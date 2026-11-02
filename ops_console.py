# 運用設定・実行管理画面の起動入口。
#   python ops_console.py serve            ローカル画面を 127.0.0.1 で起動する(既定ポート 8765)
#   python ops_console.py run-scheduled    定期実行(タスクスケジューラが呼ぶ)。通常処理 app.main() を 1 回実行して履歴に残す
#   python ops_console.py run-scheduled --dry-run   検証用。app.main は呼ばず、ロック・COM・設定の読込と履歴(種別「定期(検証)」)だけ
# 設定は ops.local.yaml、履歴は ops_history.local.jsonl(どちらも Git 管理外)。app.yaml は読み書きしない。
import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def build_context(state_dir=BASE_DIR, config_path=None, main_func=None, scheduler=None):
    import app
    from ops.history import History
    from ops.runner import RunManager
    from ops.scheduler import TaskScheduler
    from ops.server import AppContext
    from ops.settings_store import SettingsStore

    store = SettingsStore(state_dir)
    history = History(state_dir)
    config = config_path or app.default_config_path()
    from ops.output_guard import OutputGuard
    # 手動実行・定期実行の出力先を、本番の出力先(設定 data.output の下の storage/output = API の output_root)と照合する。暫定運用は変えない
    from ops.code_version import CodeVersionGuard
    code_guard = CodeVersionGuard(BASE_DIR)    # 起動時のコミットを記録する(常駐の 8765 が、起動後に更新されたコードを使っていないかを確認するため)
    output_guard = OutputGuard(config)
    # 定期実行も、手動実行と同じ出力先ガードとコード版の照合を、履歴・ロック・フォルダ作成より前に通す。処理済みの除外には、実効の出力先(製番フォルダの親)を使う
    def output_dir_of(root):
        return output_guard.effective_output_dir(root) if root else output_guard.production_output_dir()
    runs = RunManager(state_dir, store, history, main_func or app.main, config_path=config, output_guard=output_guard, code_guard=code_guard,
                      output_dir_resolver=output_dir_of)
    extra = ("--config", f'"{config_path}"') if config_path else ()
    sched = scheduler or TaskScheduler(sys.executable, str(Path(__file__).resolve()), extra_args=extra)
    return AppContext(store=store, history=history, runs=runs, scheduler=sched, config_path=config, code_check=code_guard.check_item)


TARGET_STATE_TEXT = {
    "match": "このコードの場所と一致しています",
    "mismatch": "別の場所(古い作業ツリー等)を指しています。有効化の前に、登録先を見直してください",
    "unregistered": "タスクは未登録です",
    "unknown": "照会できませんでした",
}


def cmd_serve(args) -> int:
    from ops.server import make_server
    os.chdir(BASE_DIR)                          # 相対パスの設定を、どこから起動しても同じ場所基準で読む
    ctx = build_context(config_path=args.config)
    if not ctx.runs.lock.is_held():            # 別プロセス(定期実行など)が実行中なら、その「実行中」の記録は失敗にしない
        ctx.history.mark_orphans_failed()      # 前回、終わりが書かれずに止まった「実行中」を失敗にする
    server = make_server(ctx, port=args.port)
    print(f"運用画面: http://127.0.0.1:{args.port}/  (Ctrl+C で終了)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_run_scheduled(args) -> int:
    os.chdir(BASE_DIR)                          # タスクスケジューラの既定の作業フォルダは System32 のため、リポジトリに固定する
    if getattr(args, "dry_run", False):         # 検証用: app.main は呼ばない(万一呼ばれても失敗するよう、呼べない関数を渡す)
        def _never(**_kw):
            raise AssertionError("--dry-run では app.main を呼びません")
        ctx = build_context(config_path=args.config, main_func=_never)
        status = ctx.runs.run_scheduled_targeted_dry_run()
    else:
        ctx = build_context(config_path=args.config)
        status = ctx.runs.run_scheduled_targeted()
    if ctx.runs.refusal:                       # 検査に通らず、副作用なしで拒否した(履歴・ロック・フォルダ作成・印刷・API 呼び出しの前)
        print(f"定期実行を開始しませんでした: {ctx.runs.refusal}", file=sys.stderr)
    elif getattr(args, "dry_run", False):
        plan = ctx.runs.last_plan
        if plan is not None:
            print(f"検査に合格しました(設定・出力先・コード版)。{plan.summary()}")
            print("今回の対象: " + (", ".join(plan.targets) if plan.targets else "(なし)"))
        try:
            print("定期タスクの実行先: " + TARGET_STATE_TEXT[ctx.scheduler.action_target_state()])
        except Exception:  # noqa: BLE001 - 参考情報。照会の失敗で検証結果を変えない
            print("定期タスクの実行先: " + TARGET_STATE_TEXT["unknown"])
    return 1 if status == "失敗" else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="運用設定・実行管理画面")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="ローカル画面を起動")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--config", default=None, help="app の設定ファイル(省略時は app.local.yaml、無ければ app.yaml)")
    s.set_defaults(fn=cmd_serve)
    r = sub.add_parser("run-scheduled", help="定期実行(通常処理を 1 回)")
    r.add_argument("--config", default=None)
    r.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="検証用: ロック・COM・ローカル設定の読込と履歴の記録だけ行い、製番処理(app.main)は実行しない")
    r.set_defaults(fn=cmd_run_scheduled)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
