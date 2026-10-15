# tools/run_interim_api.ps1(Windows PowerShell 5.1)の動作テスト。実 API は起動しない。
# 一時フォルダに「リポジトリ相当のレイアウト」(スクリプトのコピー + 引数を記録するだけの偽の server.py)を作り、その中で実行する。
# ネットワーク要求・実データ・本番共有・実タスクには触れない(偽の server.py は待ち受けず、引数を記録して終了するだけ)。
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "run_interim_api.ps1"
POWERSHELL = shutil.which("powershell")

STUB_SERVER = r'''
import json, os, sys
a = sys.argv[1:]
opt = {a[i]: a[i + 1] for i in range(0, len(a) - 1, 2) if a[i].startswith("--")}
root = opt["--output-root"]
mode = os.environ.get("STUB_MODE", "ok")
with open(os.path.join(root, "argv.json"), "w", encoding="utf-8") as f:
    json.dump(a, f, ensure_ascii=False)
sys.stdout.write("起動しました(偽のサーバー) 日本語ログ\n")
sys.stdout.flush()
sys.stderr.write("標準エラーの1行 stderr-line\n")
sys.stderr.flush()
cfg = opt["--config-out"]
if mode in ("ok", "crash"):
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    with open(cfg, "w", encoding="utf-8") as f:
        f.write("host:\n  pc_ip: 127.0.0.1\n  port: %s\ninstance_id: %s\n" % (opt["--port"], opt["--instance-id"]))
if mode == "ok":
    os.remove(cfg)                       # 正常停止: 自分の server.yaml を消す
    sys.exit(0)
if mode == "crash":
    sys.exit(3)                          # 強制終了の想定: server.yaml が残る
sys.exit(0)                              # nowrite
'''


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "Windows PowerShell 5.1 が必要")
class LauncherBase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="sekisan_launch_")
        self.addCleanup(td.cleanup)
        self.repo = Path(td.name).resolve()
        (self.repo / "tools").mkdir()
        shutil.copy(SCRIPT, self.repo / "tools" / "run_interim_api.ps1")
        (self.repo / "inference_server").mkdir()
        (self.repo / "inference_server" / "server.py").write_text(STUB_SERVER, encoding="utf-8")
        (self.repo / "inference_server" / "fake_example.json").write_text("{}", encoding="utf-8")
        self.py = sys.executable                         # 偽の server.py を動かすだけ(PYTHONDONTWRITEBYTECODE は起動側が設定する)
        self.iso = self.repo / "output_interim"

    def run_ps(self, *args, mode="ok", timeout=90, backend="fake"):
        """backend=None のときは -Backend を付けない(既定の挙動の確認用)。"""
        env = dict(os.environ, STUB_MODE=mode)
        extra = [] if backend is None or "-Backend" in args else ["-Backend", backend]
        p = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                            str(self.repo / "tools" / "run_interim_api.ps1"), "-Port", "20000", "-PythonExe", self.py, *extra, *args],
                           capture_output=True, timeout=timeout, env=env, cwd=str(self.repo))
        return p

    def snapshot(self, base):
        out = {}
        for dp, dn, fn in os.walk(base):
            for n in dn + fn:
                q = Path(dp, n)
                out[str(q.relative_to(base))] = q.read_bytes() if q.is_file() else None
        return out

    def logs(self):
        d = self.iso / "logs"
        return sorted(d.glob("api_*.log")) if d.is_dir() else []


class FakeBackendSelectionTest(LauncherBase):
    """fake(偽の検出結果)は検証専用。明示したときだけ使え、既定・通常運用では選ばれない。"""

    def test_default_backend_is_the_real_model_and_does_not_start_without_a_model_dir(self):
        before = self.snapshot(self.repo)
        p = self.run_ps("-DryRun", backend=None)                                         # -Backend 省略 = 実モデル(sahi)= -ModelDir が必要
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.snapshot(self.repo), before)
        self.assertEqual(self.run_ps("-DryRun", backend=None, mode="ok").returncode, 2)  # 偽のサーバーでも起動しない
        self.assertFalse((self.iso / "argv.json").exists())

    def test_fake_runs_only_when_explicitly_requested_and_is_labeled(self):
        p = self.run_ps(backend="fake")
        self.assertEqual(p.returncode, 0, p.stdout[-300:])
        argv = json.loads((self.iso / "argv.json").read_text(encoding="utf-8"))
        self.assertEqual(argv[argv.index("--backend") + 1], "fake")
        self.assertIn("--fake-script", argv)
        # sahi は実モデルのフォルダを渡す(偽の検出結果の引数は渡さない)
        model = self.repo / "model"
        model.mkdir()
        shutil.rmtree(self.iso)
        p = self.run_ps("-Backend", "sahi", "-ModelDir", str(model))
        self.assertEqual(p.returncode, 0, p.stdout[-300:])
        argv = json.loads((self.iso / "argv.json").read_text(encoding="utf-8"))
        self.assertEqual(argv[argv.index("--backend") + 1], "sahi")
        self.assertNotIn("--fake-script", argv)

    def test_script_default_is_sahi_and_no_other_path_selects_fake(self):
        text = SCRIPT.read_bytes().decode("utf-8-sig")
        self.assertIn('$Backend = "sahi"', text)
        self.assertNotIn('$Backend = "fake"', text)
        self.assertNotIn("env:", text.replace("$env:PYTHON", "").replace("$prevPy", ""))   # 環境変数でバックエンドを選ばない

    def test_normal_operation_configs_never_select_fake(self):
        import yaml
        for name in ("app.yaml", "app_interim.yaml"):
            cfg = yaml.safe_load((ROOT / name).read_text(encoding="utf-8")) or {}
            self.assertNotIn("inference_server", cfg, name)                              # アプリが偽のサーバーを起動する設定ではない
            self.assertNotIn("fake", json.dumps(cfg).lower(), name)
        local = ROOT / "app.local.yaml"
        if local.is_file():                                                              # 値は読むが出力しない(有無の判定だけ)
            cfg = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
            self.assertNotIn("inference_server", cfg)
        src = (ROOT / "inference_server" / "server.py").read_text(encoding="utf-8")
        self.assertIn('default="sahi"', src)                                             # サーバー本体の既定も実モデル


class DryRunAndRefusalTest(LauncherBase):
    def assert_refused(self, *args, mode="ok"):
        before = self.snapshot(self.repo)
        p = self.run_ps(*args, mode=mode)
        self.assertEqual(p.returncode, 2, (args, p.stdout[-300:]))
        self.assertEqual(self.snapshot(self.repo), before, args)                         # 何も作らない・消さない・起動しない

    def test_dry_run_ok_creates_nothing_and_does_not_start_the_server(self):
        before = self.snapshot(self.repo)
        p = self.run_ps("-DryRun")
        self.assertEqual(p.returncode, 0, p.stdout[-300:])
        self.assertEqual(self.snapshot(self.repo), before)
        self.assertFalse(self.iso.exists())

    def test_paths_with_spaces_are_accepted_in_dry_run(self):
        p = self.run_ps("-DryRun", "-OutputRoot", r"output_interim\dir with space")
        self.assertEqual(p.returncode, 0, p.stdout[-300:])
        self.assertFalse((self.iso / "dir with space").exists())

    def test_ports_outside_the_allowed_range_are_refused(self):
        for bad in ("80", "1023", "65536", "0", "-1"):
            p = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                str(self.repo / "tools" / "run_interim_api.ps1"), "-Port", bad, "-PythonExe", self.py, "-DryRun"],
                               capture_output=True, timeout=60, cwd=str(self.repo))
            self.assertNotEqual(p.returncode, 0, bad)
        self.assertFalse(self.iso.exists())

    def test_output_roots_outside_the_isolated_folder_are_refused(self):
        for bad in (r"inference_server", r"tools", r".", r"..\outside", r"output_interim\..\tools", r"output_interim_x",
                    str(self.repo), r"C:\Windows", r"\\host\share\x", "//host/share/x"):
            self.assert_refused("-DryRun", "-OutputRoot", bad)

    def test_python_must_be_a_local_python_exe(self):
        evil = self.repo / "evil.exe"
        evil.write_text("x", encoding="utf-8")
        before = self.snapshot(self.repo)
        for bad in (str(evil), str(self.repo / "missing" / "python.exe"), r"\\host\share\python.exe"):
            p = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                str(self.repo / "tools" / "run_interim_api.ps1"), "-Port", "20000", "-PythonExe", bad, "-DryRun"],
                               capture_output=True, timeout=60, cwd=str(self.repo))
            self.assertEqual(p.returncode, 2, bad)
        self.assertEqual(self.snapshot(self.repo), before)

    def test_sahi_requires_a_local_model_dir_and_fake_script_must_exist_locally(self):
        self.assert_refused("-DryRun", "-Backend", "sahi")
        self.assert_refused("-DryRun", "-Backend", "sahi", "-ModelDir", r"\\host\share\model")
        self.assert_refused("-DryRun", "-FakeScript", r"\\host\share\x.json")
        self.assert_refused("-DryRun", "-FakeScript", "missing.json")

    def test_existing_server_yaml_is_not_overwritten_unless_replace_stale(self):
        (self.iso / "api").mkdir(parents=True)
        (self.iso / "api" / "server.yaml").write_text("instance_id: running\n", encoding="utf-8")
        self.assert_refused("-DryRun")
        p = self.run_ps("-DryRun", "-ReplaceStale")
        self.assertEqual(p.returncode, 0, p.stdout[-300:])

    def test_junction_in_the_output_path_is_refused(self):
        import _winapi
        outside = self.repo.parent / (self.repo.name + "_outside")
        outside.mkdir()
        (outside / "canary.txt").write_text("canary", encoding="utf-8")
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        _winapi.CreateJunction(str(outside), str(self.iso))                              # output_interim → 外
        self.addCleanup(lambda: os.rmdir(str(self.iso)) if self.iso.exists() else None)
        before_outside = self.snapshot(outside)
        for args in (("-DryRun",), ("-DryRun", "-OutputRoot", r"output_interim\sub")):
            p = self.run_ps(*args)
            self.assertEqual(p.returncode, 2, args)
        self.assertEqual(self.snapshot(outside), before_outside)
        os.rmdir(str(self.iso))
        self.iso.mkdir()
        _winapi.CreateJunction(str(outside), str(self.iso / "sub"))                       # output_interim\sub → 外
        self.addCleanup(lambda: os.rmdir(str(self.iso / "sub")) if (self.iso / "sub").exists() else None)
        p = self.run_ps("-DryRun", "-OutputRoot", r"output_interim\sub")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.snapshot(outside), before_outside)


class LaunchWithStubTest(LauncherBase):
    """偽の server.py で起動部分を検証する(引数・引用符・ログ・終了コード・後始末)。待ち受けも通信もしない。"""

    def test_normal_run_passes_arguments_intact_logs_utf8_and_propagates_exit_code(self):
        p = self.run_ps("-OutputRoot", r"output_interim\dir with space", mode="ok")
        self.assertEqual(p.returncode, 0, p.stdout[-300:] + p.stderr[-300:])
        root = self.iso / "dir with space"
        argv = json.loads((root / "argv.json").read_text(encoding="utf-8"))
        opt = {argv[i]: argv[i + 1] for i in range(0, len(argv) - 1, 2)}                  # 偽のサーバーは sys.argv[1:](オプションだけ)を記録する
        self.assertEqual(len(argv), 12)                                                    # 6 つのオプションと値だけ(余分な引数・分割なし)
        self.assertEqual(opt["--output-root"], str(root))                                 # 空白を含むパスが 1 つの引数のまま
        self.assertEqual(opt["--config-out"], str(root / "api" / "server.yaml"))
        self.assertEqual((opt["--port"], opt["--backend"]), ("20000", "fake"))
        self.assertRegex(opt["--instance-id"], r"^[0-9a-f]{32}$")
        for forbidden in ("--allow-unc", "--parent-pid"):
            self.assertNotIn(forbidden, argv)
        logs = sorted((root / "logs").glob("api_*.log"))
        self.assertEqual(len(logs), 1)
        text = logs[0].read_bytes().decode("utf-8")                                       # UTF-8 で読める(日本語・標準エラーも)
        self.assertIn("日本語ログ", text)
        self.assertIn("標準エラーの1行 stderr-line", text)                                 # 標準エラーで止まらず、ログに残る
        self.assertNotIn("NativeCommandError", text)
        self.assertFalse((root / "api" / "server.yaml").exists())

    def test_caller_supplied_instance_id_is_passed_through_and_used_for_cleanup(self):
        iid = "0123456789abcdef0123456789abcdef"
        p = self.run_ps("-InstanceId", iid, mode="crash")
        self.assertEqual(p.returncode, 3)
        argv = json.loads((self.iso / "argv.json").read_text(encoding="utf-8"))
        self.assertEqual(argv[argv.index("--instance-id") + 1], iid)                       # 呼び出し元(UI)が決めた値がそのまま渡る
        self.assertFalse((self.iso / "api" / "server.yaml").exists())                      # その id の server.yaml を後始末する

    def test_invalid_instance_ids_are_refused_before_anything_is_created(self):
        for bad in ("short", "G" * 32, "A" * 32, "0123456789abcdef0123456789abcdef0", "../x"):
            before = self.snapshot(self.repo)
            self.assertEqual(self.run_ps("-InstanceId", bad, "-DryRun").returncode, 2, bad)
            self.assertEqual(self.snapshot(self.repo), before)

    def test_crash_exit_code_is_propagated_and_only_our_stale_server_yaml_is_removed(self):
        p = self.run_ps(mode="crash")
        self.assertEqual(p.returncode, 3)                                                  # 異常終了の終了コードをそのまま返す
        self.assertFalse((self.iso / "api" / "server.yaml").exists())                      # 自分の(instance_id が一致する)残りは消す
        self.assertEqual(len(self.logs()), 1)                                              # ログは残す

    def test_a_foreign_server_yaml_is_never_removed(self):
        (self.iso / "api").mkdir(parents=True)
        foreign = self.iso / "api" / "server.yaml"
        foreign.write_text("host:\n  pc_ip: 127.0.0.1\n  port: 1\ninstance_id: someone-else\n", encoding="utf-8")
        p = self.run_ps("-ReplaceStale", mode="nowrite")
        self.assertEqual(p.returncode, 0, p.stdout[-300:])
        self.assertTrue(foreign.is_file())                                                 # instance_id が違うものは消さない
        self.assertIn("someone-else", foreign.read_text(encoding="utf-8"))

    def test_without_replace_stale_the_existing_yaml_blocks_the_start_before_anything_is_created(self):
        (self.iso / "api").mkdir(parents=True)
        (self.iso / "api" / "server.yaml").write_text("instance_id: running\n", encoding="utf-8")
        p = self.run_ps(mode="ok")
        self.assertEqual(p.returncode, 2)
        self.assertFalse((self.iso / "argv.json").exists())                                # サーバーは起動していない
        self.assertEqual(self.logs(), [])

    def test_missing_server_script_fails_before_anything_is_created(self):
        (self.repo / "inference_server" / "server.py").unlink()
        p = self.run_ps()
        self.assertEqual(p.returncode, 2)
        self.assertFalse(self.iso.exists())


if __name__ == "__main__":
    unittest.main()
