# 運用画面(8765)の「起動後にコードが更新された」ことの検出。
# 経緯: 8765 は長く動かす常駐プロセスで、Python は起動時にモジュールを読み込む。作業ツリーが後から新しいコミットへ進んでも、動いているプロセスは起動時のコードのままになる。
# 2026-10-30 の本番 1 件試験(A1GP5344)は、10/26 起動の 8765 が、その後に統合された「表示用プレビュー(__preview.png)」「共通製作仕様書・設計作図完了シートの名前(0_S0 / 0_A1)」の
# 実装より古いコードで処理し、警告なしに、プレビューの無い出力を本番出力先へ作った。
# 対策: 起動時の作業ツリーのコミット(git HEAD)を記録し、手動実行の開始前(履歴・ロック・フォルダ作成より前)と動作チェックで、現在の HEAD と比べる。違えば固定の理由で拒否する/NG にする。
# - 比べるのはコミットだけ(未コミットの変更は見ない)。git のプロセスは起動せず .git のファイルを読む。読めない・リポジトリでない場合は「確認できない」とし、実行は止めない(従来どおり)。
# - 定期実行(run-scheduled)は毎回新しいプロセスで始まるため、対象外。暫定運用(interim)・開発用の RunManager には接続しない。
# - 理由は固定文(短縮した SHA のみ。パスは含めない)。
from pathlib import Path
from typing import Callable, Optional

from ops.settings_store import SettingsError

REASON_TEMPLATE = ("運用画面の起動後に、コード(作業ツリーのコミット)が {started} から {now} へ更新されています。"
                   "このプロセスは起動時のコード({started})のままで動いており、新しい機能(表示用プレビューなど)が反映されません。"
                   "製番フォルダの作成・印刷・API の呼び出しは行っていません。運用画面(ops_console.py serve)を再起動してから実行してください。")


def _is_sha(text: str) -> bool:
    return len(text) >= 7 and all(c in "0123456789abcdef" for c in text)


def current_commit(repo_dir) -> Optional[str]:
    """作業ツリーの現在のコミット(HEAD)。git のプロセスは起動せず、.git の中のファイルだけを読む(通常のリポジトリ・worktree・packed-refs に対応)。
    読めない・リポジトリでない場合は None。"""
    try:
        dotgit = Path(repo_dir) / ".git"
        if dotgit.is_file():                                              # worktree: `gitdir: <path>`
            line = dotgit.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir:"):
                return None
            gitdir = Path(line[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = (Path(repo_dir) / gitdir).resolve()
        elif dotgit.is_dir():
            gitdir = dotgit
        else:
            return None
        common = gitdir
        if (gitdir / "commondir").is_file():                              # worktree の共通の .git
            common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head if _is_sha(head) else None                        # detached HEAD
        ref = head[len("ref:"):].strip()
        for base in (gitdir, common):
            f = base / ref
            if f.is_file():
                sha = f.read_text(encoding="utf-8").strip()
                return sha if _is_sha(sha) else None
        packed = common / "packed-refs"
        if packed.is_file():
            for ln in packed.read_text(encoding="utf-8").splitlines():
                parts = ln.split()
                if len(parts) == 2 and parts[1] == ref and _is_sha(parts[0]):
                    return parts[0]
        return None
    except (OSError, ValueError):
        return None


class CodeVersionGuard:
    """起動時のコミットを記録し、現在のコミットと比べる。`guard()` は、更新されていれば SettingsError(固定理由)。`check_item()` は動作チェックの 1 項目。"""

    def __init__(self, repo_dir, current: Optional[Callable[[], Optional[str]]] = None):
        self.repo_dir = Path(repo_dir)
        self._current = current or (lambda: current_commit(self.repo_dir))
        self.started = self._current()

    def state(self):
        """(状態, 起動時, 現在)。状態は "same" / "stale" / "unknown"(どちらかが確認できない)。"""
        now = self._current()
        if self.started is None or now is None:
            return "unknown", self.started, now
        return ("same" if now == self.started else "stale"), self.started, now

    def __call__(self) -> None:
        status, started, now = self.state()
        if status == "stale":
            raise SettingsError(REASON_TEMPLATE.format(started=started[:7], now=now[:7]))

    def check_item(self) -> dict:
        status, started, now = self.state()
        if status == "stale":
            return {"name": "実行中のコードの版", "status": "ng",
                    "detail": f"起動後にコードが更新されています({started[:7]} → {now[:7]})。このプロセスは起動時のコードのままです。再起動してください。"}
        if status == "unknown":
            return {"name": "実行中のコードの版", "status": "skip", "detail": "コードの版(git)を確認できませんでした(実行は止めません)。"}
        return {"name": "実行中のコードの版", "status": "ok", "detail": f"起動時と同じコードです({started[:7]})。"}
