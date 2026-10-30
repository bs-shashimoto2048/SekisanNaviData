# 運用画面(8765)の「起動後にコードが更新された」検出(ops/code_version.py)のテスト。
# 一時ディレクトリ・合成データ・一時の git リポジトリだけ。本番出力・共有・API・印刷・製番処理は使わない(app.main は差し替える)。
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops import code_version as cv  # noqa: E402
from ops.history import History  # noqa: E402
from ops.runner import RunManager  # noqa: E402
from ops.settings_store import SettingsError, SettingsStore  # noqa: E402

OLD, NEW = "4327297" + "a" * 33, "ecd484d" + "b" * 33
PRODUCT = "A1TEST0001"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.store, self.history = SettingsStore(self.state), History(self.state)
        self.calls = []
        self.head = [OLD]

    def main(self, **kw):
        self.calls.append(kw)
        return {"completed": list(kw.get("products") or []), "failed": {}, "skipped": {}}

    def guard(self):
        return cv.CodeVersionGuard(self.tmp, current=lambda: self.head[0])

    def manager(self, code_guard, main=None):
        return RunManager(self.state, self.store, self.history, main or self.main, config_path="cfg.yaml",
                          com_init=lambda: None, com_uninit=lambda: None, code_guard=code_guard)

    def tree(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if "state" not in p.parts)


class GuardTest(Base):
    def test_same_commit_passes_and_the_check_item_is_ok(self):
        g = self.guard()
        g()
        item = g.check_item()
        self.assertEqual((item["status"], item["name"]), ("ok", "実行中のコードの版"))
        self.assertIn(OLD[:7], item["detail"])

    def test_a_newer_commit_is_detected_with_a_fixed_reason_and_short_shas_only(self):
        g = self.guard()
        self.head[0] = NEW
        with self.assertRaises(SettingsError) as cm:
            g()
        msg = str(cm.exception)
        self.assertIn(OLD[:7], msg)
        self.assertIn(NEW[:7], msg)
        self.assertIn("再起動", msg)
        self.assertIn("製番フォルダの作成・印刷・API の呼び出しは行っていません", msg)
        for leak in (OLD, NEW, str(self.tmp)):
            self.assertNotIn(leak, msg)
        item = g.check_item()
        self.assertEqual(item["status"], "ng")
        self.assertIn("再起動", item["detail"])

    def test_unknown_commit_does_not_block_and_is_reported_as_skip(self):
        g = cv.CodeVersionGuard(self.tmp, current=lambda: None)                         # git が使えない・リポジトリでない
        g()
        self.assertEqual(g.check_item()["status"], "skip")
        g2 = self.guard()
        self.head[0] = None                                                             # 起動後に確認できなくなった
        g2()
        self.assertEqual(g2.state()[0], "unknown")

    def test_going_back_to_the_started_commit_clears_the_condition(self):
        g = self.guard()
        self.head[0] = NEW
        with self.assertRaises(SettingsError):
            g()
        self.head[0] = OLD
        g()


class ManualRunTest(Base):
    def test_unchanged_code_runs_as_before(self):
        _, t = self.manager(self.guard()).start_manual(PRODUCT)
        t.join(10)
        self.assertEqual(self.calls, [{"config_path": "cfg.yaml", "products": [PRODUCT]}])
        self.assertEqual(self.history.runs()[0]["status"], "完了")

    def test_updated_code_refuses_before_any_side_effect(self):
        g = self.guard()
        self.head[0] = NEW
        before = self.tree()
        mgr = self.manager(g, main=mock.Mock(side_effect=AssertionError("app.main は呼ばない")))
        with self.assertRaises(SettingsError):
            mgr.start_manual(PRODUCT, str(self.tmp / "out"))
        self.assertEqual(self.history.runs(), [])                                       # 履歴を作らない
        self.assertFalse(mgr.status()["running"])                                       # ロックを取らない
        self.assertEqual(self.tree(), before)                                           # フォルダ・ファイルを作らない
        mgr.lock.acquire("probe")
        mgr.lock.release()

    def test_invalid_input_is_still_rejected_first_by_the_existing_validation(self):
        guard = mock.Mock(side_effect=AssertionError("入力の検証より前に呼ばない"))
        with self.assertRaises(SettingsError):
            self.manager(guard).start_manual("bad product")

    def test_without_a_guard_the_behaviour_is_exactly_as_before(self):                 # 暫定運用(interim)などは、ガードを付けない
        _, t = self.manager(None).start_manual(PRODUCT)
        t.join(10)
        self.assertEqual(len(self.calls), 1)

    def test_scheduled_run_is_not_affected(self):
        g = self.guard()
        self.head[0] = NEW
        self.assertEqual(self.manager(g).run_scheduled(), "完了")                      # 定期実行は毎回新しいプロセスで始まる。ガードの対象外
        self.assertEqual(len(self.calls), 1)


class RealGitTest(unittest.TestCase):
    @unittest.skipIf(shutil.which("git") is None, "git が無い環境")
    def test_a_real_commit_after_startup_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            def git(*a):
                subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], check=True, capture_output=True)
            git("init", "-q")
            (Path(d) / "a.txt").write_text("1", encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "first")
            g = cv.CodeVersionGuard(d)
            self.assertIsNotNone(g.started)
            self.assertEqual(g.state()[0], "same")
            (Path(d) / "a.txt").write_text("2", encoding="utf-8")                       # 未コミットの変更は見ない
            self.assertEqual(g.state()[0], "same")
            git("commit", "-q", "-am", "second")
            self.assertEqual(g.state()[0], "stale")
            with self.assertRaises(SettingsError):
                g()

    def test_a_folder_that_is_not_a_repository_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(cv.current_commit(d))
            self.assertIsNone(cv.current_commit(Path(d) / "missing"))


class FileReadingTest(unittest.TestCase):
    """git のプロセスを起動せず、.git のファイルから HEAD を読む(通常・worktree・packed-refs・detached)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.sha = "1234567" + "c" * 33

    def test_no_git_process_is_started(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("git は起動しない")), mock.patch("subprocess.Popen", side_effect=AssertionError("git は起動しない")):
            self.assertIsNone(cv.current_commit(self.d))
            cv.CodeVersionGuard(self.d)

    def test_loose_ref_packed_ref_and_detached_head(self):
        git = self.d / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.assertIsNone(cv.current_commit(self.d))                                  # ref が無い
        (git / "refs" / "heads" / "main").write_text(self.sha + "\n", encoding="utf-8")
        self.assertEqual(cv.current_commit(self.d), self.sha)
        (git / "refs" / "heads" / "main").unlink()
        (git / "packed-refs").write_text("# pack-refs\n" + self.sha + " refs/heads/main\n", encoding="utf-8")
        self.assertEqual(cv.current_commit(self.d), self.sha)
        (git / "HEAD").write_text("abcdef0" + "d" * 33 + "\n", encoding="utf-8")
        self.assertEqual(cv.current_commit(self.d), "abcdef0" + "d" * 33)
        (git / "HEAD").write_text("garbage\n", encoding="utf-8")
        self.assertIsNone(cv.current_commit(self.d))

    def test_a_worktree_with_a_gitdir_file_and_commondir(self):
        main = self.d / "main" / ".git"
        wt = main / "worktrees" / "w1"
        (main / "refs" / "heads").mkdir(parents=True)
        wt.mkdir(parents=True)
        (main / "refs" / "heads" / "feat").write_text(self.sha + "\n", encoding="utf-8")
        (wt / "HEAD").write_text("ref: refs/heads/feat\n", encoding="utf-8")
        (wt / "commondir").write_text("../..\n", encoding="utf-8")
        work = self.d / "w1"
        work.mkdir()
        (work / ".git").write_text("gitdir: " + str(wt) + "\n", encoding="utf-8")
        self.assertEqual(cv.current_commit(work), self.sha)

    @unittest.skipIf(shutil.which("git") is None, "git が無い環境")
    def test_agrees_with_git_for_this_repository(self):
        r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("リポジトリではない")
        self.assertEqual(cv.current_commit(ROOT), r.stdout.strip())


class WiringTest(Base):
    def test_ops_console_wires_the_guard_and_the_checks_item_and_the_run_endpoint(self):
        import importlib
        from ops.server import make_server
        oc = importlib.import_module("ops_console")
        with mock.patch.object(cv, "current_commit", side_effect=lambda repo: self.head[0]):
            ctx = oc.build_context(state_dir=self.state, config_path=str(self.tmp / "app.yaml"), main_func=self.main, scheduler=mock.Mock(name="scheduler"))
        self.assertIsInstance(ctx.runs.code_guard, cv.CodeVersionGuard)
        self.assertEqual(ctx.runs.code_guard.started, OLD)
        ctx.runs.code_guard._current = lambda: self.head[0]                                   # 以降の「現在のコミット」を、テストが決める
        ctx.run_checks = lambda cfg, settings: [{"name": "x", "status": "ok", "detail": ""}]      # 設定の読み込みなどは使わない
        server = make_server(ctx, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        base = f"http://127.0.0.1:{server.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "X-Ops-Request": "1"})
            try:
                with opener.open(req, timeout=20) as r:
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode("utf-8"))
        # 起動時と同じコード: 動作チェックは OK、手動実行は開始できる
        st, res = post("/api/checks", {})
        self.assertEqual([i["status"] for i in res["results"]], ["ok", "ok"])
        self.assertEqual(res["results"][-1]["name"], "実行中のコードの版")
        # 起動後にコードが更新された: 動作チェックは NG、手動実行は 400 + 固定理由で、何も開始しない
        self.head[0] = NEW
        st, res = post("/api/checks", {})
        self.assertEqual(res["results"][-1]["status"], "ng")
        st, body = post("/api/run", {"product": PRODUCT, "output_root": str(self.tmp / "out")})
        self.assertEqual((st, body["ok"]), (400, False))
        self.assertIn("再起動", body["error"])
        self.assertEqual((self.calls, ctx.history.runs()), ([], []))


if __name__ == "__main__":
    unittest.main()
