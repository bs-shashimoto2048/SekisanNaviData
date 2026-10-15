# tools/smoke_interim_api.py のテスト。通信はすべてモック(実 API・実ホスト・ネットワーク・実データ・共有・CAD には接続しない)。
# 一時フォルダの合成の設定・server.yaml だけを使い、実行時に requests / 推論クライアントが呼ばれたら失敗する。
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

import smoke_interim_api as smoke  # noqa: E402

SECRETS = ("10.9.8.7", "secret-host", "pw123", "proxy.secret")
GOOD_LINE = "Index$0|Device$トランス|Score$0.91|Coordinates$1.0,2.0:3.0,2.0:1.0,4.0:3.0,4.0"


def resp(status=200, body=None):
    body = {"instance_id": "i", "backend": "fake", "mode": "test", "status": "ok", "ready": True} if body is None else body
    return SimpleNamespace(status_code=status, json=lambda: body)


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="sekisan_smoke_")
        self.addCleanup(td.cleanup)
        self.repo = Path(td.name).resolve()
        self.api = self.repo / "output_interim" / "api"
        self.lines = []
        self.gets = []
        self.requests = []

    def write_config(self, name="app_interim.yaml", **over):
        cfg = {"data": {"output": "./output_interim", "storage": "a_product"},
               "api": {"dir_venv": "./output_interim/api", "yaml_file": "server.yaml", "use_proxy": False}}
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        (self.repo / name).write_text(yaml.safe_dump(cfg), encoding="utf-8")

    def write_server_yaml(self, host="127.0.0.1", port=23456):
        self.api.mkdir(parents=True, exist_ok=True)
        (self.api / "server.yaml").write_text(yaml.safe_dump({"host": {"pc_ip": host, "port": port}}), encoding="utf-8")

    def get(self, url, **kw):
        self.gets.append((url, kw))
        return resp()

    def request(self, folder, pages, cfg_path, timeout):
        self.requests.append((folder, pages, cfg_path, timeout))
        self.assertTrue(Path(folder, "1.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))     # 合成 PNG があること
        Path(folder, "1_detected.txt").write_text(GOOD_LINE + "\n", encoding="utf-8")

    def run_main(self, argv=(), get=None, request=None, net_drive=lambda p: False):
        return smoke.main(list(argv), get=get or self.get, request=request or self.request, net_drive=net_drive,
                          repo_root=self.repo, out=self.lines.append)

    @property
    def text(self):
        return "\n".join(self.lines)

    def assert_clean_output(self):
        for s in SECRETS + (str(self.repo), "http://", "Traceback"):
            self.assertNotIn(s, self.text)

    def smoke_dirs(self):
        d = self.repo / "output_interim" / "smoke"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def tree(self, base=None):
        """ファイル・フォルダの一覧と内容のハッシュ(書き込み・削除が無かったことの確認用)。リンクは辿らない。"""
        import hashlib
        import os
        out = {}
        for dp, dn, fn in os.walk(base or self.repo):
            for n in dn + fn:
                p = Path(dp, n)
                out[str(p.relative_to(base or self.repo))] = (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not smoke.is_link(p) else "dir/link")
        return out

    def good_env(self):
        self.write_config()
        self.write_server_yaml()


class HappyPathTest(Base):
    def test_all_steps_ok_with_synthetic_data_only_and_cleans_up(self):
        self.good_env()
        self.assertEqual(self.run_main(), 0)
        self.assertIn("RESULT: OK", self.lines)
        self.assertEqual(len(self.requests), 1)
        folder, pages, cfg_path, timeout = self.requests[0]
        self.assertEqual(pages, ["1"])
        self.assertEqual(Path(folder).parent, self.repo / "output_interim" / "smoke")          # 隔離出力先の smoke 配下
        self.assertEqual(Path(cfg_path), self.api / "server.yaml")
        self.assertEqual(timeout, smoke.DEFAULT_TIMEOUT)
        self.assertEqual(self.smoke_dirs(), [])                                                # 自分が作ったフォルダは削除
        self.assert_clean_output()

    def test_requests_are_loopback_without_proxy_or_redirects_and_only_health(self):
        self.good_env()
        self.run_main()
        self.assertEqual(len(self.gets), 2)                                                    # 事前確認 + 本文確認
        for url, kw in self.gets:
            self.assertEqual(url, "http://127.0.0.1:23456/health")
            self.assertEqual(kw["proxies"], {"http": None, "https": None})
            self.assertIs(kw["allow_redirects"], False)

    def test_keep_leaves_only_the_synthetic_folder(self):
        self.good_env()
        self.assertEqual(self.run_main(["--keep"]), 0)
        self.assertEqual(len(self.smoke_dirs()), 1)

    def test_zero_detections_empty_file_is_ok(self):
        self.good_env()

        def request(folder, pages, cfg, timeout):
            Path(folder, "1_detected.txt").write_text("", encoding="utf-8")
        self.assertEqual(self.run_main(request=request), 0)
        self.assertTrue(any("検出 0 件" in l for l in self.lines))

    def test_real_repo_sample_config_passes_the_isolation_check(self):
        cfg, out_root, api_dir = smoke.validate_config(ROOT / "app_interim.yaml", ROOT, net_drive=lambda p: False)
        self.assertEqual(out_root, (ROOT / "output_interim").resolve())
        self.assertEqual(api_dir, (ROOT / "output_interim" / "api").resolve())


class RefusalTest(Base):
    """安全条件に反する入力は、通信も書き込みもせずに終了コード 2。"""
    def assert_refused(self, code=2, **run):
        def no_get(*a, **k):
            raise AssertionError("通信してはいけない")

        def no_request(*a, **k):
            raise AssertionError("依頼してはいけない")
        before = self.tree()
        self.assertEqual(self.run_main(get=no_get, request=no_request, **run), code)
        self.assertTrue(self.text.startswith("REFUSED:"), self.text)
        self.assertEqual(self.tree(), before)                                           # 書き込み・削除・作成なし
        self.assertFalse((self.repo / "output_interim" / "smoke").exists())
        self.assert_clean_output()

    def test_non_interim_config_names_and_outside_repo_are_refused(self):
        self.write_server_yaml()
        for name in ("app.yaml", "app.local.yaml", "app_test.yaml"):
            self.write_config(name)
            self.lines.clear()
            self.assert_refused(argv=["--config", name])
        outside = Path(tempfile.mkdtemp(prefix="sekisan_out_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (outside / "app_interim.yaml").write_text("{}", encoding="utf-8")
        self.lines.clear()
        self.assert_refused(argv=["--config", str(outside / "app_interim.yaml")])

    def test_integrated_mode_and_proxy_usage_are_refused(self):
        self.write_server_yaml()
        self.write_config(inference_server={"enabled": True})
        self.assert_refused()
        for api in ({"use_proxy": True}, {"use_proxy": None}):
            self.lines.clear()
            self.write_config(api=api)
            self.assert_refused()

    def test_paths_outside_the_isolated_output_are_refused(self):
        self.write_server_yaml()
        for bad in ("./output_test", "./output_interim/../other", str(self.repo), "output_interim_x", "C:/Windows", "//host/share",
                    r"\\host\share\x", "../outside"):
            for key in ("data", "api"):
                self.lines.clear()
                over = {"data": {"output": bad}} if key == "data" else {"api": {"dir_venv": bad}}
                self.write_config(**over)
                self.assert_refused()

    def test_network_drive_is_refused(self):
        self.good_env()
        self.assert_refused(net_drive=lambda p: True)

    def test_yaml_file_with_a_path_is_refused(self):
        self.write_server_yaml()
        self.write_config(api={"yaml_file": "../server.yaml"})
        self.assert_refused()

    def test_non_loopback_server_yaml_is_refused_before_any_connection(self):
        self.write_config()
        for host in ("10.9.8.7", "secret-host", "192.168.0.1", "example.com", "0.0.0.0"):
            self.lines.clear()
            self.write_server_yaml(host=host)
            self.assert_refused()

    def test_bad_timeout_is_refused(self):
        self.good_env()
        for t in ("0", "-1", "99999"):
            self.lines.clear()
            self.assertEqual(self.run_main(["--timeout", t]), 2)
            self.assertEqual(self.gets, [])

    def test_loopback_names_are_accepted(self):
        self.write_config()
        for host in ("127.0.0.1", "localhost"):
            self.write_server_yaml(host=host)
            self.assertEqual(smoke.read_loopback_target(self.api / "server.yaml"), (host, 23456))
        for host in ("::1", "127.0.0.2", "0.0.0.0", "LOCALHOST.evil.example"):          # IPv6 や他のアドレスは対象外(接続前に拒否)
            self.write_server_yaml(host=host)
            with self.assertRaises(smoke.SmokeRefused, msg=host):
                smoke.read_loopback_target(self.api / "server.yaml")


class FailureTest(Base):
    def test_missing_server_yaml_is_ng_without_connecting(self):
        self.write_config()
        self.assertEqual(self.run_main(), 1)
        self.assertIn("server.yaml が見つかりません", self.text)
        self.assertEqual((self.gets, self.requests), ([], []))

    def test_broken_server_yaml_is_ng_with_type_only(self):
        self.write_config()
        self.api.mkdir(parents=True)
        (self.api / "server.yaml").write_text("host: {pc_ip: secret-host}", encoding="utf-8")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual((self.gets, self.requests), ([], []))
        self.assert_clean_output()

    def test_preflight_failures_stop_before_requesting(self):
        self.good_env()
        import requests

        def refused(url, **kw):
            raise requests.exceptions.ConnectionError(ConnectionRefusedError(10061, "host=secret-host password=pw123"))
        for get in (lambda u, **k: resp(503), lambda u, **k: resp(500), lambda u, **k: resp(302), refused):
            self.lines.clear()
            self.assertEqual(self.run_main(get=get), 1)
            self.assertIn("RESULT: NG", self.lines)
            self.assertEqual(self.requests, [])
            self.assert_clean_output()

    def test_health_body_with_internal_information_is_ng(self):
        self.good_env()
        for leak in ({"output_root": "x"}, {"pid": 1}):
            self.lines.clear()
            body = {"instance_id": "i", "backend": "fake", "mode": "test", "status": "ok", "ready": True, **leak}
            self.assertEqual(self.run_main(get=lambda u, _b=body, **k: resp(200, _b)), 1)
            self.assertIn("内部情報", self.text)
            self.assertEqual(self.requests, [])

    def test_health_not_ready_or_not_json_is_ng(self):
        self.good_env()
        calls = []

        def get(url, **kw):
            calls.append(1)
            if len(calls) == 1:
                return resp(200)                       # 事前確認(ステータスだけ)は通す
            return resp(200, {"ready": False, "status": "loading"})
        self.assertEqual(self.run_main(get=get), 1)
        self.assertIn("ready", self.text)

    def test_request_failure_is_ng_with_type_and_count_only_and_cleans_up(self):
        self.good_env()

        class InferenceRequestError(Exception):
            failed_pages = ["1"]

        def request(folder, pages, cfg, timeout):
            raise InferenceRequestError(f"API URL http://10.9.8.7:8123/predict {self.repo} password=pw123")
        self.assertEqual(self.run_main(request=request), 1)
        self.assertIn("InferenceRequestError", self.text)
        self.assertIn("失敗ページ 1 件", self.text)
        self.assert_clean_output()
        self.assertEqual(self.smoke_dirs(), [])

    def test_detected_file_problems_are_ng(self):
        self.good_env()
        variants = {
            "missing": lambda f: None,
            "not_utf8": lambda f: Path(f, "1_detected.txt").write_bytes("トランス".encode("cp932")),
            "bad_line": lambda f: Path(f, "1_detected.txt").write_text("garbage line\n", encoding="utf-8"),
            "three_fields": lambda f: Path(f, "1_detected.txt").write_text("Index$0|Device$a|Score$0.5\n", encoding="utf-8"),
        }
        for name, write in variants.items():
            self.lines.clear()
            self.assertEqual(self.run_main(request=lambda f, p, c, t, _w=write: _w(f)), 1, name)
            self.assertIn("RESULT: NG", self.lines, name)
            self.assertEqual(self.smoke_dirs(), [], name)


@unittest.skipUnless(sys.platform == "win32", "ジャンクション(_winapi.CreateJunction)は Windows のみ")
class LinkBoundaryTest(Base):
    """リンク・ジャンクション等を経由して、隔離出力先の外へ出ない(書き込み・削除をしない)。"""

    def setUp(self):
        super().setUp()
        td = tempfile.TemporaryDirectory(prefix="sekisan_outside_")
        self.addCleanup(td.cleanup)
        self.outside = Path(td.name).resolve()
        (self.outside / "canary.txt").write_text("canary", encoding="utf-8")

    def junction(self, link: Path, target: Path):
        import _winapi
        link.parent.mkdir(parents=True, exist_ok=True)
        _winapi.CreateJunction(str(target), str(link))
        self.addCleanup(lambda: link.exists() and __import__("os").rmdir(str(link)))      # ジャンクション自身だけを外す(中身は消さない)

    def assert_refused_and_untouched(self):
        before_outside = self.tree(self.outside)
        def no_get(*a, **k):
            raise AssertionError("通信禁止")

        def no_request(*a, **k):
            raise AssertionError("依頼禁止")
        self.assertEqual(self.run_main(get=no_get, request=no_request), 2)
        self.assertTrue(self.text.startswith("REFUSED:"), self.text)
        self.assertEqual(self.tree(self.outside), before_outside)                       # 外側に何も作られていない・消えていない
        self.assertEqual((self.outside / "canary.txt").read_text(encoding="utf-8"), "canary")
        self.assert_clean_output()

    def test_output_interim_being_a_junction_is_refused(self):
        self.junction(self.repo / "output_interim", self.outside)                        # output_interim → 外
        self.write_config()
        self.write_server_yaml()                                                         # 外側(junction 先)に置かれる
        self.assertTrue(smoke.has_link_in_path(self.repo / "output_interim", self.repo))
        self.assert_refused_and_untouched()
        self.assertFalse((self.outside / "smoke").exists())

    def test_smoke_directory_being_a_junction_is_refused(self):
        self.good_env()
        self.junction(self.repo / "output_interim" / "smoke", self.outside)              # smoke → 外
        self.assert_refused_and_untouched()

    def test_api_directory_being_a_junction_is_refused(self):
        self.write_config()
        self.junction(self.api, self.outside)                                            # output_interim\api → 外
        (self.outside / "server.yaml").write_text(yaml.safe_dump({"host": {"pc_ip": "127.0.0.1", "port": 23456}}), encoding="utf-8")
        self.assert_refused_and_untouched()

    def test_intermediate_directory_junction_in_a_configured_path_is_refused(self):
        self.junction(self.repo / "output_interim" / "sub", self.outside)
        self.write_config(data={"output": "./output_interim/sub"})
        self.write_server_yaml()
        self.assert_refused_and_untouched()

    def test_server_yaml_being_a_link_is_refused(self):
        import os
        self.write_config()
        self.api.mkdir(parents=True)
        real = self.outside / "elsewhere.yaml"
        real.write_text(yaml.safe_dump({"host": {"pc_ip": "127.0.0.1", "port": 23456}}), encoding="utf-8")
        try:
            os.symlink(real, self.api / "server.yaml")
        except OSError:
            self.skipTest("シンボリックリンクを作成する権限がありません")
        self.assert_refused_and_untouched()

    def test_junction_planted_inside_the_synthetic_folder_is_never_followed_on_cleanup(self):
        self.good_env()

        def request(folder, pages, cfg, timeout):
            self.junction(Path(folder) / "evil", self.outside)                           # 実行中に、内側へリンクが現れた想定
            Path(folder, "1_detected.txt").write_text("", encoding="utf-8")
        self.assertEqual(self.run_main(request=request), 0)
        self.assertEqual((self.outside / "canary.txt").read_text(encoding="utf-8"), "canary")   # リンク先は削除されない
        self.assertTrue(any(line.startswith("NOTE:") for line in self.lines))            # 削除を見送ったことを知らせる
        self.assertEqual(len(self.smoke_dirs()), 1)

    def test_link_helpers(self):
        self.assertFalse(smoke.is_link(self.outside))
        self.assertFalse(smoke.is_link(self.outside / "missing"))
        self.junction(self.repo / "lnk", self.outside)
        self.assertTrue(smoke.is_link(self.repo / "lnk"))
        self.assertTrue(smoke.has_link_in_path(self.repo / "lnk" / "deeper" / "x", self.repo))
        self.assertFalse(smoke.has_link_in_path(self.repo / "plain" / "x", self.repo))


class PartsTest(Base):
    def test_default_request_uses_the_production_client_without_proxy_and_swallows_stdout(self):
        printed = []
        with mock.patch("pipeline.inference_client.request_detections",
                        side_effect=lambda *a, **k: print("API-URL:http://10.9.8.7:8123/predict secret-host") or printed.append((a, k))), \
                mock.patch("sys.stdout") as stdout:
            smoke.default_request("D", ["1"], "S", 12.0)
        self.assertEqual(printed, [(("D", ["1"], "S"), {"use_proxy": False, "timeout": 12.0})])
        self.assertEqual(stdout.write.call_count, 0)                              # 経過の標準出力(接続先・パス)は捨てる

    def test_owned_folder_cleanup_is_limited_to_what_this_run_created(self):
        smoke_root = self.repo / "output_interim" / "smoke"
        other_dir = smoke_root / "someone_else"
        other_dir.mkdir(parents=True)                                            # 既存(他者のもの)
        (other_dir / "x.txt").write_text("keep", encoding="utf-8")
        (smoke_root / "y.txt").write_text("keep", encoding="utf-8")
        own = smoke.OwnedFolder(smoke_root, self.repo)
        folder = own.create()
        (folder / "1.png").write_bytes(b"x")
        self.assertEqual(own.created_parents, [])                                # smoke は既存なので、自分は作っていない
        self.assertTrue(own.cleanup())
        self.assertFalse(folder.exists())
        self.assertEqual((other_dir / "x.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual((smoke_root / "y.txt").read_text(encoding="utf-8"), "keep")

    def test_owned_folder_removes_only_parents_it_created_and_only_when_empty(self):
        smoke_root = self.repo / "output_interim" / "smoke"
        own = smoke.OwnedFolder(smoke_root, self.repo)
        own.create()
        self.assertEqual(own.created_parents, [self.repo / "output_interim", smoke_root])
        (self.repo / "output_interim" / "later.txt").write_text("x", encoding="utf-8")   # 後から他者が置いたもの
        self.assertTrue(own.cleanup())
        self.assertFalse(smoke_root.exists())
        self.assertTrue((self.repo / "output_interim" / "later.txt").is_file())          # 空でない親は残す

    def test_owned_folder_refuses_to_delete_when_the_marker_is_not_ours(self):
        smoke_root = self.repo / "output_interim" / "smoke"
        own = smoke.OwnedFolder(smoke_root, self.repo)
        folder = own.create()
        (folder / smoke.OWNER_MARKER).write_text("someone-else", encoding="ascii")        # 印が違う = 自分が作ったと確認できない
        self.assertFalse(own.cleanup())
        self.assertTrue(folder.is_dir())

    def test_owned_folder_never_adopts_an_existing_folder(self):
        smoke_root = self.repo / "output_interim" / "smoke"
        own = smoke.OwnedFolder(smoke_root, self.repo)
        own.folder.mkdir(parents=True)                                                   # 同名が既にある(他者のもの)
        (own.folder / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            own.create()
        self.assertFalse(own.created)
        self.assertTrue(own.cleanup())                                                   # 作っていないので何も消さない
        self.assertTrue((own.folder / "keep.txt").is_file())

    def test_cleanup_without_create_deletes_nothing(self):
        smoke_root = self.repo / "output_interim" / "smoke"
        folder = smoke_root / "smoke_x"
        folder.mkdir(parents=True)
        own = smoke.OwnedFolder(smoke_root, self.repo)
        own.folder = folder
        self.assertTrue(own.cleanup())
        self.assertTrue(folder.is_dir())

    @unittest.skipUnless(sys.platform == "win32", "Windows のドライブ種別の判定")
    def test_local_repo_drive_is_not_a_network_drive(self):
        self.assertFalse(smoke.is_network_drive(ROOT))                           # ローカルドライブの照会だけ(通信なし)

    def test_synthetic_png_is_a_valid_png(self):
        data = smoke.synthetic_png()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(data.endswith(b"IEND\xaeB`\x82"))
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            img.load()
            self.assertEqual(img.size, (64, 64))
        except ImportError:
            pass

    def test_script_has_no_forbidden_operations(self):
        src = (ROOT / "tools" / "smoke_interim_api.py").read_text(encoding="utf-8")
        for forbidden in ("schtasks", "subprocess", "host_mod", "design_mod", "app.main", "ops_history", "Register-", "app.local.yaml\")",
                          "requests.post", "socket"):
            self.assertNotIn(forbidden, src, forbidden)


if __name__ == "__main__":
    unittest.main()
