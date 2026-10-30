"""モデル管理画面(prod_admin)の「性能(server 節)」「ログ(logging 節)」設定のテスト。
合成データ・一時フォルダ・loopback だけ。本番 API の serve・--publish-config・共有 server.yaml・製番処理・実ダイアログは使わない。"""
import contextlib
import io
import itertools
import json
import os
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

from prod_helpers import HOST, ProdBase  # noqa: F401
import prod_admin
import prod_config
import prod_server

NO_PROXY = {"http": None, "https": None}


def make_junction(link: Path, target: Path) -> bool:
    if os.name != "nt":
        return False
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    return r.returncode == 0 and link.exists()


class SettingsBase(ProdBase):
    def setUp(self):
        super().setUp()
        self.cfg_dir = self.root / "cfg"
        self.cfg_dir.mkdir()
        self.cfg_path = self.cfg_dir / "prod_server.local.yaml"
        self.write_config(self.data())
        self.admin = prod_admin.ModelAdmin(self.cfg_path)

    def write_config(self, data, newline="\n"):
        text = "# 設定(テスト)。このコメントは保存後も残ること\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False) + "\n# 末尾のコメント\n"
        self.cfg_path.write_bytes(text.replace("\n", newline).encode("utf-8"))

    def parsed(self):
        return yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))

    def only_sections(self, *drop):
        d = self.data()
        for k in drop:
            d.pop(k, None)
        return d

    def files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))

    def good_log(self, **kw):
        return {"file": str(self.cfg_dir / "logs" / "prod_api.log"), "max_bytes": 10 * 1024 * 1024, "backup_count": 5, **kw}


class DefaultsTest(SettingsBase):
    def test_defaults_are_shown_when_the_config_has_no_sections_and_nothing_is_written(self):
        self.write_config(self.only_sections("server", "logging"))
        before = self.cfg_path.read_bytes()
        s = self.admin.state()["settings"]
        self.assertFalse(s["server"]["configured"])
        self.assertEqual(s["server"]["values"], {"threads": 2, "max_pending_predict": 1, "queue_wait_sec": 30, "backlog": 4, "connection_limit": 8,
                                                 "channel_timeout_sec": 30, "shutdown_grace_sec": 60})
        self.assertEqual(s["server"]["values"], s["server"]["defaults"])
        lg = s["logging"]
        self.assertFalse(lg["configured"])
        self.assertEqual(os.path.normcase(lg["values"]["file"]), os.path.normcase(str(self.cfg_dir / "logs" / "prod_api.log")))   # 設定ファイルと同じフォルダ配下
        self.assertEqual((lg["values"]["max_bytes"], lg["values"]["backup_count"], lg["values"]["level"]), (10 * 1024 * 1024, 5, "INFO"))
        self.assertEqual(lg["max_total_mib"], 60.0)                                                                                # 最大保存量の目安(約 60 MiB)
        self.assertIn("世代数", lg["retention_note"])                                                                              # 保管期間の設定は無く、世代数で管理する
        self.assertEqual(self.cfg_path.read_bytes(), before)                                                                       # 表示だけでは書かない

    def test_null_valued_sections_also_show_defaults(self):
        d = self.data()
        d["server"] = {k: None for k in d["server"]}
        d["logging"] = {k: None for k in d["logging"]}
        self.write_config(d)
        s = self.admin.state()["settings"]
        self.assertFalse(s["server"]["configured"] or s["logging"]["configured"])
        self.assertEqual(s["server"]["values"]["threads"], 2)
        self.assertEqual(s["logging"]["values"]["backup_count"], 5)

    def test_configured_values_are_shown_when_present(self):
        s = self.admin.state()["settings"]
        self.assertTrue(s["server"]["configured"] and s["logging"]["configured"])
        self.assertEqual(s["server"]["values"]["threads"], 6)
        self.assertEqual(s["logging"]["values"]["max_bytes"], 65536)

    def test_every_field_has_a_label_a_unit_a_description_and_the_real_range(self):
        fields = self.admin.state()["settings"]["server"]["fields"]
        self.assertEqual([f["key"] for f in fields], list(prod_admin.PERF_DEFAULTS))
        for f in fields:
            with self.subTest(key=f["key"]):
                self.assertTrue(f["label"] and f["unit"] and len(f["description"]) > 10)
                self.assertEqual((f["min"], f["max"]), prod_config.LIMIT_RANGES[f["key"]])

    def test_defaults_are_valid_settings_for_the_real_validation(self):
        self.assertEqual(prod_admin.check_server_values(dict(prod_admin.PERF_DEFAULTS)), {})
        prod_config._limits(dict(prod_admin.PERF_DEFAULTS))
        self.assertEqual(self.admin.check_logging_values(self.good_log(), {}), {})
        self.assertEqual(prod_admin.PERF_DEFAULTS["max_pending_predict"], 1)                  # 推論の同時実行は 1 件

    def test_no_fake_settings_every_saved_key_is_used_by_the_server(self):
        self.assertEqual(set(prod_admin.PERF_DEFAULTS), set(prod_config.ServerLimits.__dataclass_fields__))
        self.assertTrue({"file", "max_bytes", "backup_count"} <= set(prod_config.LogConfig.__dataclass_fields__))
        self.assertNotIn("request_timeout", Path(prod_config.__file__).read_text(encoding="utf-8"))         # 本番 API の設定ではない


class ServerValidationTest(SettingsBase):
    def v(self, **over):
        return {**prod_admin.PERF_DEFAULTS, **over}

    def test_boundaries_follow_the_real_ranges(self):
        # 各項目の下限・上限は受け付け、その外側は、その項目のエラーにする(ほかの項目は、関係する規則を満たす値にしておく)
        base = {"threads": 4, "max_pending_predict": 2, "queue_wait_sec": 30, "backlog": 4, "connection_limit": 100, "channel_timeout_sec": 30, "shutdown_grace_sec": 60}
        context = {"threads": {"max_pending_predict": 1}, "max_pending_predict": {"threads": 64}, "connection_limit": {"threads": 2, "max_pending_predict": 1}}
        for key, (lo, hi) in prod_config.LIMIT_RANGES.items():
            for val, ok in ((lo, True), (hi, True), (lo - 1, False), (hi + 1, False)):
                with self.subTest(key=key, val=val):
                    values = {**base, **context.get(key, {}), key: val}
                    errs = prod_admin.check_server_values(values)
                    if ok:
                        self.assertEqual(errs, {}, errs)
                    else:
                        self.assertIn(key, errs)

    def test_cross_rules(self):
        e = prod_admin.check_server_values(self.v(threads=4, max_pending_predict=4))
        self.assertIn("max_pending_predict", e)
        self.assertIn("threads(4)", e["max_pending_predict"])
        self.assertEqual(prod_admin.check_server_values(self.v(threads=4, max_pending_predict=3, connection_limit=6)), {})
        e = prod_admin.check_server_values(self.v(threads=8, max_pending_predict=2, connection_limit=9))
        self.assertIn("connection_limit", e)
        self.assertEqual(prod_admin.check_server_values(self.v(threads=8, max_pending_predict=2, connection_limit=10)), {})

    def test_wrong_types_are_rejected(self):
        for bad in ("8", 8.0, True, None, [2]):
            with self.subTest(bad=bad):
                self.assertIn("threads", prod_admin.check_server_values(self.v(threads=bad)))
        self.assertIn("_", prod_admin.check_server_values("not a dict"))
        e = prod_admin.check_server_values({"threads": 2})
        self.assertEqual(set(e), set(prod_admin.PERF_DEFAULTS) - {"threads"})

    def test_screen_validation_agrees_with_the_server_validation_on_a_grid(self):
        grid = {"threads": (1, 2, 3, 64, 65), "max_pending_predict": (0, 1, 2, 3, 63, 64), "connection_limit": (3, 4, 5, 8, 66, 512, 513)}
        for t, p, c in itertools.product(grid["threads"], grid["max_pending_predict"], grid["connection_limit"]):
            values = self.v(threads=t, max_pending_predict=p, connection_limit=c)
            try:
                prod_config._limits(values)
                real = True
            except prod_config.ConfigError:
                real = False
            self.assertEqual(not prod_admin.check_server_values(values), real, (t, p, c))


class SaveServerTest(SettingsBase):
    NEW = {"threads": 2, "max_pending_predict": 1, "queue_wait_sec": 45, "backlog": 4, "connection_limit": 8, "channel_timeout_sec": 30, "shutdown_grace_sec": 90}

    def test_save_updates_only_the_server_section_and_keeps_everything_else(self):
        before_bytes, before = self.cfg_path.read_bytes(), self.parsed()
        res = self.admin.save_settings({"server": self.NEW})
        self.assertTrue(res["saved"], res)
        after = self.parsed()
        self.assertEqual(after["server"], self.NEW)
        for key in before:
            if key != "server":
                self.assertEqual(after[key], before[key], key)                                # model・output_root・logging・bind など、ほかの節は同じ
        text = self.cfg_path.read_text(encoding="utf-8")
        self.assertIn("このコメントは保存後も残ること", text)
        self.assertIn("末尾のコメント", text)
        backups = list(self.cfg_dir.glob("prod_server.bak-*.local.yaml"))
        self.assertEqual([b.name for b in backups], [res["backup_file"]])
        self.assertEqual(backups[0].read_bytes(), before_bytes)                               # バックアップ = 保存前の内容
        self.assertEqual([p.name for p in self.cfg_dir.iterdir() if p.name.endswith(".tmp")], [])
        prod_config.parse_config(after)                                                       # 設定として、そのまま使える
        self.assertEqual(self.admin.state()["settings"]["server"]["values"], self.NEW)       # 保存・再読込

    def test_text_outside_the_section_is_byte_identical(self):
        before = self.cfg_path.read_text(encoding="utf-8")
        self.admin.save_settings({"server": self.NEW})
        after = self.cfg_path.read_text(encoding="utf-8")

        def outside(t):
            lines = t.splitlines()
            i = next(k for k, ln in enumerate(lines) if ln.startswith("server:"))
            j = i + 1
            while j < len(lines) and (lines[j].strip() == "" or lines[j][0] in " \t"):
                j += 1
            return lines[:i] + lines[j:]
        self.assertEqual([ln for ln in outside(before) if ln.strip()], [ln for ln in outside(after) if ln.strip()])

    def test_crlf_is_kept(self):
        self.write_config(self.data(), newline="\r\n")
        self.admin.save_settings({"server": self.NEW})
        raw = self.cfg_path.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_sections_are_added_explicitly_when_missing_and_model_is_untouched(self):
        self.write_config(self.only_sections("server", "logging"))
        before = self.parsed()
        self.assertNotIn("server", before)
        res = self.admin.save_settings({"server": dict(prod_admin.PERF_DEFAULTS), "logging": self.good_log()})
        self.assertTrue(res["saved"])
        after = self.parsed()
        self.assertEqual(after["server"], dict(prod_admin.PERF_DEFAULTS))
        self.assertEqual(after["logging"]["backup_count"], 5)
        self.assertEqual(after["logging"]["level"], "INFO")
        for key in before:
            self.assertEqual(after[key], before[key], key)                                   # model を含む既存の節は同じ
        prod_config.parse_config(after)

    def test_invalid_values_are_refused_and_nothing_is_written(self):
        before, tree = self.cfg_path.read_bytes(), self.files()
        for bad in ({**self.NEW, "threads": 1}, {**self.NEW, "threads": 65}, {**self.NEW, "max_pending_predict": 2},
                    {**self.NEW, "connection_limit": 3}, {**self.NEW, "connection_limit": 513}, {**self.NEW, "queue_wait_sec": 0},
                    {**self.NEW, "backlog": 1025}, {**self.NEW, "channel_timeout_sec": 4}, {**self.NEW, "shutdown_grace_sec": 301},
                    {**self.NEW, "threads": "2"}, {"threads": 2}):
            with self.subTest(bad=bad):
                res = self.admin.save_settings({"server": bad})
                self.assertFalse(res["saved"])
                self.assertTrue(res["errors"]["server"])
        self.assertEqual(self.cfg_path.read_bytes(), before)
        self.assertEqual(self.files(), tree)                                                  # バックアップ・一時ファイルも作らない

    def test_save_is_atomic_when_the_replace_fails(self):
        before = self.cfg_path.read_bytes()
        with mock.patch("os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.admin.save_settings({"server": self.NEW})
        self.assertEqual(self.cfg_path.read_bytes(), before)
        self.assertEqual([p.name for p in self.cfg_dir.iterdir() if p.name.endswith(".tmp")], [])

    def test_nothing_to_save_and_wrong_target_files(self):
        with self.assertRaises(prod_admin.AdminError):
            self.admin.save_settings({})
        sample = self.cfg_dir / "prod_server.sample.yaml"
        sample.write_bytes(self.cfg_path.read_bytes())
        with self.assertRaises(prod_admin.AdminError):
            prod_admin.ModelAdmin(sample).save_settings({"server": self.NEW})
        with self.assertRaises(prod_admin.AdminError):
            prod_admin.ModelAdmin(self.cfg_dir / "missing.local.yaml").save_settings({"server": self.NEW})

    def test_a_unc_or_network_config_location_is_refused_before_anything_is_written(self):
        unc = prod_admin.ModelAdmin("\\\\server\\share\\prod_server.local.yaml")
        with self.assertRaises(prod_admin.AdminError) as cm:
            unc.save_settings({"server": self.NEW})
        self.assertEqual(str(cm.exception), prod_admin.STORAGE_MESSAGE)
        net = prod_admin.ModelAdmin(self.cfg_path, drive_type=lambda r: 4)
        before, tree = self.cfg_path.read_bytes(), self.files()
        with self.assertRaises(prod_admin.AdminError):
            net.save_settings({"server": self.NEW})
        self.assertEqual((self.cfg_path.read_bytes(), self.files()), (before, tree))


class LoggingValidationTest(SettingsBase):
    def err(self, **over):
        return self.admin.check_logging_values(self.good_log(**over), self.parsed())

    def test_a_normal_local_path_is_accepted(self):
        self.assertEqual(self.err(), {})
        self.assertEqual(self.err(file=str(self.cfg_dir / "logs" / "sub" / "x.log")), {})

    def test_non_local_and_unsafe_locations_are_refused(self):
        for bad in ("\\\\server\\share\\logs\\a.log", "//server/share/a.log", "relative\\a.log", "\\logs\\a.log", str(self.cfg_dir / ".." / "x.log"),
                    "", "   ", None):
            with self.subTest(bad=bad):
                self.assertIn("file", self.err(file=bad))
        e = self.err(file="\\\\server\\share\\logs\\a.log")
        self.assertNotIn("server", e["file"].replace("サーバー", ""))                           # パスをメッセージに含めない

    def test_network_and_removable_drives_are_refused_by_the_real_drive_check_logic(self):
        for kind in (0, 1, 2, 4, 5, 6):
            with self.subTest(kind=kind):
                admin = prod_admin.ModelAdmin(self.cfg_path, drive_type=lambda r, k=kind: k)
                self.assertIn("file", admin.check_logging_values(self.good_log(), self.parsed()))

    @unittest.skipUnless(os.name == "nt", "Windows のドライブ判定")
    def test_real_non_fixed_drives_are_refused_without_mocks(self):
        import ctypes
        types = {c: ctypes.windll.kernel32.GetDriveTypeW(c + ":\\") for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
        others = [c for c, t in types.items() if t in (2, 4)]
        if not others:
            self.skipTest("取り外し可能・ネットワークのドライブがない環境")
        for c in others:
            with self.subTest(drive=c):
                self.assertIn("file", self.err(file=f"{c}:\\prod_admin_log_test\\a.log"))

    @unittest.skipUnless(os.name == "nt", "Windows のジャンクション")
    def test_junction_and_symlink_locations_are_refused(self):
        target = self.root / "elsewhere"
        target.mkdir()
        link = self.root / "logs_link"
        if not make_junction(link, target):
            self.skipTest("ジャンクションを作れない環境")
        for p in (link / "a.log", link / "sub" / "a.log"):
            with self.subTest(p=str(p)):
                self.assertIn("file", self.err(file=str(p)))
        before, tree = self.cfg_path.read_bytes(), self.files()
        res = self.admin.save_settings({"logging": self.good_log(file=str(link / "a.log"))})
        self.assertFalse(res["saved"])
        self.assertEqual((self.cfg_path.read_bytes(), self.files(), list(target.iterdir())), (before, tree, []))
        real = target / "real.log"
        real.write_text("x", encoding="utf-8")
        slink = self.root / "log_symlink.log"
        try:
            os.symlink(str(real), str(slink))
        except (OSError, NotImplementedError):
            return
        self.assertIn("file", self.err(file=str(slink)))                                       # ログファイル自体がリンク

    def test_the_log_may_not_overlap_the_output_root_the_backup_dir_or_the_model_dir(self):
        for p in (self.out / "a.log", self.backup_dir / "a.log", self.model_dir / "a.log", self.server_yaml):
            with self.subTest(p=str(p)):
                self.assertIn("file", self.err(file=str(p)))

    def test_size_and_generation_ranges(self):
        lo, hi = prod_config.LOG_RANGES["max_bytes"]
        self.assertEqual((self.err(max_bytes=lo), self.err(max_bytes=hi)), ({}, {}))
        for bad in (lo - 1, hi + 1, "10", 1.5, True, None):
            self.assertIn("max_bytes", self.err(max_bytes=bad), bad)
        lo, hi = prod_config.LOG_RANGES["backup_count"]
        self.assertEqual((self.err(backup_count=lo), self.err(backup_count=hi)), ({}, {}))
        for bad in (lo - 1, hi + 1, "5", 2.5, False, None):
            self.assertIn("backup_count", self.err(backup_count=bad), bad)


class SaveLoggingTest(SettingsBase):
    def test_save_updates_only_the_logging_section_and_keeps_the_level(self):
        d = self.data()
        d["logging"]["level"] = "WARNING"
        self.write_config(d)
        before = self.parsed()
        new = self.good_log(max_bytes=20 * 1024 * 1024, backup_count=3)
        res = self.admin.save_settings({"logging": new})
        self.assertTrue(res["saved"], res)
        after = self.parsed()
        self.assertEqual(after["logging"], {"file": os.path.normpath(new["file"]), "max_bytes": 20 * 1024 * 1024, "backup_count": 3, "level": "WARNING"})
        for key in before:
            if key != "logging":
                self.assertEqual(after[key], before[key], key)
        s = self.admin.state()["settings"]["logging"]
        self.assertEqual((s["values"]["backup_count"], s["max_total_mib"]), (3, 80.0))             # (3 + 1) × 20 MiB
        self.assertEqual(len(list(self.cfg_dir.glob("prod_server.bak-*.local.yaml"))), 1)

    def test_invalid_log_settings_are_refused_and_nothing_is_written(self):
        before, tree = self.cfg_path.read_bytes(), self.files()
        for bad in (self.good_log(file="\\\\server\\share\\a.log"), self.good_log(max_bytes=1), self.good_log(backup_count=0),
                    self.good_log(file=str(self.out / "a.log"))):
            with self.subTest(bad=bad):
                res = self.admin.save_settings({"logging": bad})
                self.assertFalse(res["saved"])
                self.assertTrue(res["errors"]["logging"])
        self.assertEqual((self.cfg_path.read_bytes(), self.files()), (before, tree))

    def test_a_bad_log_location_in_the_config_is_reported_on_startup_and_in_the_state_without_the_path(self):
        d = self.data()
        d["logging"]["file"] = "\\\\server\\share\\secret_logs\\a.log"
        self.write_config(d)
        err = self.admin.log_storage_error()
        self.assertEqual(err, prod_admin.STORAGE_MESSAGE)
        s = self.admin.state()["settings"]["logging"]
        self.assertEqual(s["storage_error"], prod_admin.STORAGE_MESSAGE)
        stderr = io.StringIO()
        fake = mock.Mock()
        fake.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(prod_admin, "make_server", return_value=fake), contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = prod_admin.main(["--config", str(self.cfg_path), "--port", "0"])
        self.assertEqual(code, 0)                                                              # 画面は起動し(直せるように)、固定文の警告を出す
        self.assertIn(prod_admin.STORAGE_MESSAGE, stderr.getvalue())
        self.assertNotIn("secret_logs", stderr.getvalue())


class ApiSideStartupGuardTest(SettingsBase):
    @unittest.skipUnless(os.name == "nt", "Windows のジャンクション")
    def test_serve_and_check_refuse_a_log_location_that_is_not_proven_local(self):
        target = self.root / "elsewhere_logs"
        target.mkdir()
        link = self.root / "logs_via_junction"
        if not make_junction(link, target):
            self.skipTest("ジャンクションを作れない環境")
        cfg = self.cfg(logging={**self.data()["logging"], "file": str(link / "prod_api.log")})
        self.assertFalse(prod_server._log_location_is_local(cfg))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = prod_server.serve(cfg, backend_factory=mock.Mock(side_effect=AssertionError("起動しない")))
        self.assertEqual(code, prod_server.EXIT_CONFIG)
        self.assertIn("ローカルディスク内", err.getvalue())
        self.assertNotIn(str(self.root), err.getvalue())
        self.assertEqual(list(target.iterdir()), [])                                           # 解決先へ何も書かない
        res = prod_server.run_check(cfg)
        self.assertFalse(res["logging"]["location_is_local"])
        self.assertFalse(res["ok"])

    def test_a_normal_local_log_location_passes(self):
        cfg = self.cfg()
        self.assertTrue(prod_server._log_location_is_local(cfg))
        if prod_server._waitress_importable():
            self.assertTrue(prod_server.run_check(cfg)["logging"]["location_is_local"])


class HttpAndClientTimeoutTest(SettingsBase):
    def setUp(self):
        super().setUp()
        self.server = prod_admin.make_server(self.admin, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.hdr = {"X-Admin-Request": "1", "Content-Type": "application/json"}

    def post(self, path, body):
        return requests.post(self.base + path, data=json.dumps(body), headers=self.hdr, timeout=30, proxies=NO_PROXY)

    def test_state_validate_and_save_over_http(self):
        st = requests.get(self.base + "/api/state", timeout=5, proxies=NO_PROXY).json()
        self.assertIn("settings", st)
        page = requests.get(self.base + "/", timeout=5, proxies=NO_PROXY).text
        for text in ("性能を保存", "ログ設定を保存", "最大サイズ", "ローテーション後に残す世代数", "api.request_timeout", "perfTable"):
            self.assertIn(text, page)
        bad = self.post("/api/settings/validate", {"server": {**prod_admin.PERF_DEFAULTS, "threads": 1}}).json()
        self.assertFalse(bad["valid"])
        self.assertIn("threads", bad["errors"]["server"])
        ok = self.post("/api/settings/validate", {"logging": self.good_log()}).json()
        self.assertTrue(ok["valid"])
        self.assertEqual(ok["summary"]["max_total_mib"], 60.0)
        before = self.cfg_path.read_bytes()
        self.assertEqual(self.cfg_path.read_bytes(), before)                                  # 検証では書かない
        sv = self.post("/api/settings/save", {"server": dict(prod_admin.PERF_DEFAULTS)}).json()
        self.assertTrue(sv["saved"], sv)
        self.assertEqual(self.parsed()["server"], dict(prod_admin.PERF_DEFAULTS))
        refused = self.post("/api/settings/save", {"server": {**prod_admin.PERF_DEFAULTS, "threads": 99}}).json()
        self.assertFalse(refused["saved"])
        self.assertEqual(self.parsed()["server"], dict(prod_admin.PERF_DEFAULTS))             # 範囲外では保存されない
        self.assertEqual(self.post("/api/settings/save", {}).status_code, 400)
        no_header = requests.post(self.base + "/api/settings/save", data=json.dumps({"server": {}}), headers={"Content-Type": "application/json"},
                                  timeout=5, proxies=NO_PROXY)
        self.assertEqual(no_header.status_code, 403)                                           # 状態を変える操作にはカスタムヘッダが必要
        lan = requests.post(self.base + "/api/settings/save", data=json.dumps({"server": {}}), headers={**self.hdr, "Host": "192.0.2.5"},
                            timeout=5, proxies=NO_PROXY)
        self.assertEqual(lan.status_code, 403)

    def test_client_timeout_is_explained_but_never_written_to_the_api_config(self):
        ct = self.admin.state()["settings"]["client_timeout"]
        self.assertEqual((ct["setting"], ct["recommended_sec"]), ("api.request_timeout", 180))
        self.assertIn("app.local.yaml", ct["file"])
        for word in ("180 秒", "順番待ち", "停止待ち", "編集できません"):
            self.assertIn(word, ct["text"])
        self.admin.save_settings({"server": dict(prod_admin.PERF_DEFAULTS), "logging": self.good_log()})
        self.assertNotIn("request_timeout", self.cfg_path.read_text(encoding="utf-8"))        # 見かけだけの項目を作らない
        self.assertNotIn("request_timeout", Path(prod_admin.__file__).read_text(encoding="utf-8").replace(
            "api.request_timeout", "").replace("request_timeout で指定", ""))                   # 画面の説明以外に、保存処理で使わない

    def test_the_api_url_and_allowed_clients_are_shown_from_the_config(self):
        api = self.admin.state()["settings"]["api"]
        self.assertEqual(api["url"], f"http://{HOST}:{self.port}")
        self.assertEqual(api["allowed_clients"], ["127.0.0.1/32"])
        self.assertIn("HTTP", api["protocol"])
        page = requests.get(self.base + "/", timeout=5, proxies=NO_PROXY).text
        self.assertIn("apiInfo", page)
        d = self.data()
        d["bind"] = {"host": None, "port": None}
        d["allowed_clients"] = None
        self.write_config(d)
        api = self.admin.state()["settings"]["api"]
        self.assertEqual((api["url"], api["allowed_clients"]), (None, []))                    # 未設定なら、URL は出さない

    def test_responses_never_contain_unrelated_paths_or_hashes(self):
        res = self.admin.save_settings({"server": dict(prod_admin.PERF_DEFAULTS)})
        blob = json.dumps(res, ensure_ascii=False)
        for secret in (str(self.root), str(self.model_dir)):
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()
