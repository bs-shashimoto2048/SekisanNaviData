# 暫定・隔離運用の専用設定(app_interim.yaml)と起動スクリプト(tools/run_interim_api.ps1)の静的テスト。
# ファイルを読むだけ。サーバーの起動・ネットワーク要求・実データ・本番共有には触れない。
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from ops import checks  # noqa: E402
from ops.settings_store import OpsSettings  # noqa: E402
from pipeline import api_preflight  # noqa: E402

CONFIG = ROOT / "app_interim.yaml"
SCRIPT = ROOT / "tools" / "run_interim_api.ps1"


def load():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


class InterimConfigTest(unittest.TestCase):
    def test_is_production_mode_with_isolated_relative_paths_and_no_proxy(self):
        cfg = load()
        self.assertNotIn("inference_server", cfg)                 # 統合モードにしない(本番モードの経路を確認する設定)
        self.assertEqual(cfg["data"]["output"], "./output_interim")
        self.assertEqual(cfg["api"]["dir_venv"], "./output_interim/api")
        self.assertIs(cfg["api"]["use_proxy"], False)
        self.assertNotIn("request_timeout", cfg["api"])           # GPU 環境で実測するまで未設定
        self.assertNotIn("proxy", cfg)

    def test_has_no_real_paths_hosts_ports_or_secrets(self):
        text = CONFIG.read_text(encoding="utf-8")
        values = [l.split("#")[0] for l in text.splitlines() if not l.lstrip().startswith("#")]
        body = "\n".join(values)
        self.assertNotRegex(body, r"\\\\|//[A-Za-z0-9]")           # UNC・共有
        self.assertNotRegex(body, r"\d+\.\d+\.\d+\.\d+")          # IP アドレス
        self.assertNotRegex(body, r"(?i)password|secret|token|host|port")
        self.assertNotRegex(body, r"(?i)\\Users\\|/Users/|share")
        for path in (cfg for cfg in (load()["data"]["output"], load()["api"]["dir_venv"])):
            self.assertTrue(path.startswith("./output_interim"))   # すべて隔離出力先の中

    def test_ops_config_check_accepts_it(self):
        cfg = load()
        res = {r["name"]: r for r in checks.run_checks(
            "dummy", OpsSettings(), load_config=lambda p: cfg, design_check=lambda: (True, "x"),
            host_check=lambda i: (True, "x"),
            health_check=lambda c: api_preflight.unavailable_reason(c, get=lambda u, **k: SimpleNamespace(status_code=200)))}
        self.assertEqual(res["設定の形式"]["status"], "ok")        # 必須項目がそろっている(サンプルの '<' なし)

    def test_server_yaml_location_matches_what_the_launcher_writes(self):
        cfg = load()
        self.assertEqual(cfg["api"]["yaml_file"], "server.yaml")
        script = SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn('Join-Path $root "api\\server.yaml"', script)   # --config-out = <隔離出力先>\api\server.yaml(設定の api.dir_venv と同じ場所)

    def test_gitignore_covers_the_isolated_output_and_local_config(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("output_interim/", [l.strip() for l in ignore])
        self.assertIn("*.local.yaml", [l.strip() for l in ignore])


class LauncherScriptTest(unittest.TestCase):
    def setUp(self):
        raw = SCRIPT.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))           # BOM 付き(PowerShell 5.1 が日本語を正しく読むため)
        self.text = raw.decode("utf-8-sig")

    def test_is_manual_only_and_does_not_touch_tasks_services_or_network(self):
        code = re.sub(r"<#.*?#>", "", self.text, flags=re.S)
        for forbidden in ("schtasks", "Register-ScheduledTask", "New-Service", "sc.exe", "Invoke-WebRequest", "Invoke-RestMethod",
                          "curl", "git ", "--allow-unc", "--parent-pid", "app.yaml", "app.local.yaml"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_validates_isolation_before_starting_and_supports_dry_run(self):
        t = self.text
        for check in ("Test-WithinIsolated $root $isolated", "Test-ReparseInPath $repo $root", "Assert-Local \"隔離出力先\""):
            self.assertLess(t.index(check), t.index("& $py @serverArgs"))                   # 隔離・リンク・ローカルの検証が先
        for needle in ("DriveType -eq [System.IO.DriveType]::Network", 'StartsWith("\\\\")', "[switch]$DryRun", "[switch]$ReplaceStale",
                       "if ($DryRun)", "$Port -lt 1024 -or $Port -gt 65535", "ReparsePoint"):
            self.assertIn(needle, t)
        self.assertLess(t.index("if ($DryRun)"), t.index("New-Item -ItemType Directory"))   # DryRun では何も作らない
        self.assertIn("[Parameter(Mandatory = $true)][int]$Port", t)                        # ポートは必須(値を固定しない)

    def test_contains_no_literal_ports_hosts_unc_or_user_paths(self):
        code = re.sub(r"<#.*?#>", "", self.text, flags=re.S)
        self.assertNotRegex(code, r"\d+\.\d+\.\d+\.\d+")
        self.assertNotRegex(code, r"(?i)C:\\Users|\\\\[A-Za-z]")
        numbers = set(re.findall(r"\b[1-9]\d{3,4}\b", code))
        self.assertEqual(numbers - {"1024", "65535"}, set())          # 具体的なポート番号を書かない(許可範囲の下限・上限だけ)


if __name__ == "__main__":
    unittest.main()
