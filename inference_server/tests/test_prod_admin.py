"""本番推論 API のモデル管理画面(prod_admin)のテスト。合成データ・一時フォルダ・loopback だけ。
実モデル・共有・本番 server.yaml・製番フォルダ・実ダイアログは使わない(ダイアログは runner を差し替える)。"""
import contextlib
import hashlib
import io
import json
import os
import socket
import threading
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

from prod_helpers import HOST, ProdBase, free_port, sha  # noqa: F401
import prod_admin
import prod_config
import prod_model
import prod_server

NO_PROXY = {"http": None, "https": None}
CKPT = b"PK\x03\x04" + b"synthetic-checkpoint-bytes" * 200          # PyTorch の zip 形式の先頭だけを模した合成データ(中身は無意味)
NAME = "estimatic_test_20260101.pt"


def yaml_text(my_model=NAME):
    return ("model:\n  use_model_type: yolov8\n  my_model: " + my_model + "\n  threshold: 0.81\n  use_device: cuda:0\n"
            "sahi:\n  slice_higth: 640\n  slice_width: 640\n  over_lap_higth: 0.2\n  over_lap_width: 0.2\n")


class AdminBase(ProdBase):
    def setUp(self):
        super().setUp()
        self.mdir = self.root / "newmodel"
        self.mdir.mkdir()
        self.w = self.mdir / NAME
        self.w.write_bytes(CKPT)
        self.y = self.mdir / "model.yaml"
        self.y.write_bytes(yaml_text().encode("utf-8"))
        self.cfg_path = self.root / "prod_server.local.yaml"
        self.write_config()
        self.admin = prod_admin.ModelAdmin(self.cfg_path, picker=prod_admin.FilePicker(runner=self.fake_runner))
        self.picked = b""

    def fake_runner(self, args, env):
        self.last_env = env
        return mock.Mock(returncode=0, stdout=self.picked)

    def write_config(self, newline="\n", extra_comment=True):
        data = self.data()
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        if extra_comment:
            text = "# 設定案(テスト)。この行のコメントは保存後も残ること\n" + text.replace("model:\n", "# モデルの節\nmodel:\n", 1) + "\n# 末尾のコメント\n"
        self.cfg_path.write_bytes(text.replace("\n", newline).encode("utf-8"))

    def body(self, **kw):
        return {"weights_path": str(self.w), "yaml_path": "", **kw}


class PathValidationTest(unittest.TestCase):
    def test_unc_and_drive_paths_are_accepted(self):
        for p in (prod_admin.DEFAULT_WEIGHTS_PATH, r"D:\models\a.pt", "C:/models/a.pt", r"\\host\share\dir\sub\x_1.pt"):
            with self.subTest(p=p):
                self.assertTrue(prod_admin.validate_model_path(p, prod_admin.WEIGHTS_EXTS, "x").endswith(".pt"))

    def test_default_path_is_the_designated_production_model(self):
        self.assertEqual(prod_admin.DEFAULT_WEIGHTS_PATH, r"\\beans-f1\ShareData\estimatic\a_product\model\estimatic_a_20250826.pt")
        self.assertEqual(os.path.basename(prod_admin.DEFAULT_WEIGHTS_PATH), "estimatic_a_20250826.pt")
        self.assertTrue(prod_admin._filename_ok("estimatic_a_20250826.pt"))

    def test_bad_paths_are_rejected_with_fixed_messages(self):
        for p in ("", "   ", "relative\\a.pt", r"\\host\share", r"\\host\share\a.txt", r"C:\a\..\b.pt", "C:\\a.pt\nx.pt", r"C:\a\b.pt|x",
                  r"C:a.pt", r"\a\b.pt", None):
            with self.subTest(p=p):
                with self.assertRaises(prod_admin.AdminError) as cm:
                    prod_admin.validate_model_path(p, prod_admin.WEIGHTS_EXTS, "重みファイル")
                self.assertNotIn("host", str(cm.exception).lower().replace("ホスト", ""))        # 入力値を、メッセージに含めない


class InspectTest(AdminBase):
    def test_size_and_sha256_come_from_the_real_file(self):
        res, internal = prod_admin.inspect_model(str(self.w))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["weights"], {"name": NAME, "size": len(CKPT), "sha256_12": hashlib.sha256(CKPT).hexdigest()[:12]})
        self.assertEqual(internal["weights_sha256"], hashlib.sha256(CKPT).hexdigest())
        self.assertEqual(internal["weights_size"], len(CKPT))
        self.assertTrue(res["pair"]["ok"])
        self.assertEqual(res["yaml_source"], "重みと同じフォルダの model.yaml")
        blob = json.dumps(res, ensure_ascii=False)
        for secret in (hashlib.sha256(CKPT).hexdigest(), str(self.root), str(self.mdir)):                 # 画面用の結果に、全文ハッシュ・パスを含めない
            self.assertNotIn(secret, blob)

    def test_missing_file(self):
        res, internal = prod_admin.inspect_model(str(self.mdir / "nothing.pt"))
        self.assertFalse(res["ok"])
        self.assertIsNone(internal)
        self.assertTrue(any("見つかりません" in p for p in res["problems"]))

    def test_invalid_format_is_rejected_without_loading_it(self):
        bad = self.mdir / "bad.pt"
        bad.write_bytes(b"this is plain text, not a checkpoint")
        res, internal = prod_admin.inspect_model(str(bad), str(self.y))
        self.assertFalse(res["ok"])
        self.assertIsNone(internal)
        self.assertTrue(any("形式ではありません" in p for p in res["problems"]))
        empty = self.mdir / "empty.pt"
        empty.write_bytes(b"")
        self.assertFalse(prod_admin.inspect_model(str(empty), str(self.y))[0]["ok"])
        (self.mdir / "weird name.pt").write_bytes(CKPT)                                                  # 設定が受け付けないファイル名
        self.assertFalse(prod_admin.inspect_model(str(self.mdir / "weird name.pt"), str(self.y))[0]["ok"])
        (self.mdir / "folder.pt").mkdir()
        self.assertFalse(prod_admin.inspect_model(str(self.mdir / "folder.pt"), str(self.y))[0]["ok"])

    def test_weights_alone_never_pass(self):
        self.y.unlink()
        res, internal = prod_admin.inspect_model(str(self.w))
        self.assertFalse(res["ok"])
        self.assertIsNone(internal)
        self.assertIsNone(res["yaml"])
        self.assertTrue(any("重みだけでは保存できません" in p for p in res["problems"]))

    def test_model_yaml_that_does_not_match_the_weights_is_refused(self):
        self.y.write_bytes(yaml_text("best.pt").encode("utf-8"))                       # 本番の状況と同じ: 別の名前を指す model.yaml
        res, internal = prod_admin.inspect_model(str(self.w))
        self.assertFalse(res["ok"])
        self.assertIsNone(internal)
        self.assertFalse(res["pair"]["ok"])
        self.assertIn("best.pt", res["pair"]["message"])
        self.assertIn(NAME, res["pair"]["message"])
        self.assertEqual(self.y.read_bytes(), yaml_text("best.pt").encode("utf-8"))    # model.yaml は編集しない

    def test_broken_or_incomplete_model_yaml(self):
        for text in ("::: not yaml", "model:\n  my_model: x.pt\n", "just a string", "[1, 2]"):
            with self.subTest(text=text):
                self.y.write_bytes(text.encode("utf-8"))
                res, internal = prod_admin.inspect_model(str(self.w))
                self.assertFalse(res["ok"])
                self.assertIsNone(internal)

    def test_model_yaml_in_another_folder_can_be_selected(self):
        other = self.root / "yamls"
        other.mkdir()
        (other / "pair.yaml").write_bytes(yaml_text().encode("utf-8"))
        self.y.unlink()
        res, internal = prod_admin.inspect_model(str(self.w), str(other / "pair.yaml"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["yaml"]["name"], "pair.yaml")
        self.assertEqual(os.path.normcase(internal["yaml_dir"]), os.path.normcase(str(other)))


@unittest.skipUnless(os.name == "nt", "Windows の管理共有(UNC)")
class UncReadTest(AdminBase):
    def test_a_real_unc_path_is_read(self):
        drive, rest = os.path.splitdrive(str(self.w))
        unc = r"\\127.0.0.1" + "\\" + drive[0] + "$" + rest
        if not os.path.isfile(unc):
            self.skipTest("管理共有(C$)にアクセスできない環境")
        res, internal = prod_admin.inspect_model(unc)
        self.assertTrue(res["ok"], res)
        self.assertEqual(internal["weights_sha256"], hashlib.sha256(CKPT).hexdigest())
        self.assertTrue(internal["weights_dir"].startswith("\\\\"))


class SaveTest(AdminBase):
    def test_save_writes_model_section_from_the_real_files_and_keeps_the_rest(self):
        before = self.cfg_path.read_bytes()
        res = self.admin.save(self.body())
        self.assertTrue(res["saved"], res)
        text = self.cfg_path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        m = data["model"]
        self.assertEqual((m["id"], m["weights"], m["yaml"]), (os.path.splitext(NAME)[0], NAME, "model.yaml"))
        self.assertEqual(m["dir"], str(self.mdir))
        self.assertNotIn("yaml_dir", m)
        self.assertEqual(m["expected"], {"weights_sha256": hashlib.sha256(CKPT).hexdigest(), "weights_size": len(CKPT),
                                         "yaml_sha256": hashlib.sha256(self.y.read_bytes()).hexdigest()})
        old = yaml.safe_load(before.decode("utf-8"))
        for key in old:
            if key != "model":
                self.assertEqual(data[key], old[key], key)                           # ほかの節は変わらない
        self.assertIn("この行のコメントは保存後も残ること", text)
        self.assertIn("末尾のコメント", text)
        prod_config.parse_config(data)                                              # 設定全体として、そのまま使える
        self.assertTrue(res["config_complete"])
        backups = list(self.root.glob("prod_server.bak-*.local.yaml"))
        self.assertEqual([b.name for b in backups], [res["backup_file"]])
        self.assertEqual(backups[0].read_bytes(), before)                           # 既存の設定のバックアップ
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])
        blob = json.dumps(res, ensure_ascii=False)
        for secret in (hashlib.sha256(CKPT).hexdigest(), str(self.root), str(self.mdir)):
            self.assertNotIn(secret, blob)

    def test_save_keeps_crlf_line_endings(self):
        self.write_config(newline="\r\n")
        self.admin.save(self.body())
        raw = self.cfg_path.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"\r\n", raw)

    def test_save_with_yaml_in_another_folder_records_yaml_dir_and_the_server_can_verify_it(self):
        other = self.root / "yamls"
        other.mkdir()
        (other / "pair.yaml").write_bytes(yaml_text().encode("utf-8"))
        self.y.unlink()
        self.admin.save({"weights_path": str(self.w), "yaml_path": str(other / "pair.yaml"), "model_id": "my_model_1"})
        cfg = prod_config.load_config(str(self.cfg_path))
        self.assertEqual((cfg.model.id, cfg.model.yaml_name, os.path.normcase(cfg.model.yaml_dir)), ("my_model_1", "pair.yaml", os.path.normcase(str(other))))
        ident, summary = prod_server._check_model_in_place(cfg)
        self.assertEqual(ident.weights_size, len(CKPT))
        self.assertEqual(summary["device"], "cuda:0")
        out = self.root / "staging"
        out.mkdir()
        ident2, staged = prod_model.stage_and_verify(cfg.model.id, cfg.model.dir, cfg.model.weights, cfg.model.yaml_name, cfg.model.weights_sha256,
                                                     cfg.model.weights_size, cfg.model.yaml_sha256, str(out), yaml_dir=cfg.model.yaml_dir)
        try:
            self.assertTrue((Path(staged) / NAME).is_file() and (Path(staged) / "pair.yaml").is_file())
        finally:
            prod_model.remove_staging(staged)

    def test_refused_when_validation_fails_and_the_config_is_unchanged(self):
        self.y.write_bytes(yaml_text("best.pt").encode("utf-8"))
        before = self.cfg_path.read_bytes()
        res = self.admin.save(self.body())
        self.assertFalse(res["saved"])
        self.assertFalse(res["inspection"]["pair"]["ok"])
        self.assertEqual(self.cfg_path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("prod_server.bak-*")), [])

    def test_save_is_atomic_when_the_replace_fails(self):
        before = self.cfg_path.read_bytes()
        with mock.patch("os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.admin.save(self.body())
        self.assertEqual(self.cfg_path.read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])

    def test_only_local_yaml_files_can_be_written(self):
        sample = self.root / "prod_server.sample.yaml"
        sample.write_bytes(self.cfg_path.read_bytes())
        with self.assertRaises(prod_admin.AdminError):
            prod_admin.ModelAdmin(sample).save(self.body())
        other = self.root / "server.yaml"
        other.write_text("host: {}\n", encoding="utf-8")
        with self.assertRaises(prod_admin.AdminError):
            prod_admin.ModelAdmin(other).save(self.body())
        self.assertEqual(other.read_text(encoding="utf-8"), "host: {}\n")
        with self.assertRaises(prod_admin.AdminError):
            prod_admin.ModelAdmin(self.root / "missing.local.yaml").save(self.body())

    def test_model_id_is_validated(self):
        with self.assertRaises(prod_admin.AdminError):
            self.admin.save(self.body(model_id="bad id/../x"))

    def test_config_without_a_model_section_gets_one_appended(self):
        self.cfg_path.write_text("mode: production\n", encoding="utf-8")
        res = self.admin.save(self.body())
        self.assertTrue(res["saved"])
        self.assertFalse(res["config_complete"])                                    # 設定全体は未完了(その旨を返す)
        self.assertEqual(yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))["model"]["weights"], NAME)

    def test_unset_sections_stay_unset(self):
        # 未設定(空欄)の項目は、保存で埋めない: 判断が必要な値を推測で確定しない
        self.cfg_path.write_text("mode: production\nbind:\n  host:\n  port:\nmodel:\n  id:\n  dir:\n", encoding="utf-8")
        self.admin.save(self.body())
        d = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(d["bind"], {"host": None, "port": None})


class BrowseTest(AdminBase):
    def test_a_chosen_file_is_returned_and_validated(self):
        self.picked = (str(self.w) + "\r\n").encode("utf-8")
        r = self.admin.browse({"kind": "weights"})
        self.assertEqual((r["path"], r["cancelled"]), (str(self.w), False))
        self.assertEqual(self.last_env["PA_FILTER"].split("|")[1], "*.pt")
        self.picked = prod_admin.DEFAULT_WEIGHTS_PATH.encode("utf-8")                # UNC も扱える
        self.assertEqual(self.admin.browse({"kind": "weights"})["path"], prod_admin.DEFAULT_WEIGHTS_PATH)
        self.picked = str(self.y).encode("utf-8")
        self.assertEqual(self.admin.browse({"kind": "yaml"})["path"], str(self.y))

    def test_cancel_returns_none(self):
        self.picked = b""
        self.assertEqual(self.admin.browse({"kind": "weights"}), {"ok": True, "path": None, "cancelled": True})

    def test_wrong_kind_of_file_is_refused(self):
        self.picked = str(self.y).encode("utf-8")                                    # .pt を選ぶ場面で .yaml
        with self.assertRaises(prod_admin.AdminError):
            self.admin.browse({"kind": "weights"})
        self.picked = str(self.w).encode("utf-8")
        with self.assertRaises(prod_admin.AdminError):
            self.admin.browse({"kind": "yaml"})
        with self.assertRaises(prod_admin.AdminError):
            self.admin.browse({"kind": "other"})

    def test_failure_and_busy(self):
        picker = prod_admin.FilePicker(runner=lambda a, e: mock.Mock(returncode=1, stdout=b""))
        with self.assertRaises(prod_admin.AdminError):
            picker.pick("weights")
        picker = prod_admin.FilePicker(runner=mock.Mock(side_effect=OSError("secret")))
        with self.assertRaises(prod_admin.AdminError) as cm:
            picker.pick("weights")
        self.assertNotIn("secret", str(cm.exception))
        picker = prod_admin.FilePicker(runner=self.fake_runner)
        picker._lock.acquire()
        with self.assertRaises(prod_admin.PickerBusy):
            picker.pick("weights")

    def test_picker_starts_in_the_folder_of_the_current_value_and_nothing_is_copied(self):
        self.picked = b""
        self.admin.browse({"kind": "weights", "current": str(self.w)})
        self.assertEqual(os.path.normcase(self.last_env["PA_INIT"]), os.path.normcase(str(self.mdir)))
        self.assertEqual(sorted(p.name for p in self.mdir.iterdir()), sorted([NAME, "model.yaml"]))


class CheckTest(AdminBase):
    def test_check_after_save_is_read_only_and_never_starts_the_api(self):
        if not prod_server._waitress_importable():
            self.skipTest("waitress が無い環境")
        self.admin.save(self.body())
        before = {p: p.read_bytes() for p in (self.cfg_path, self.w, self.y)}
        tree = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        with mock.patch.object(prod_server, "serve", side_effect=AssertionError("serve は呼ばない")) as serve, \
                mock.patch("prod_publish.publish", side_effect=AssertionError("公開しない")) as pub:
            res = self.admin.check()
        self.assertEqual((res["stage"], res["check_ok"]), ("check", True), res)
        self.assertTrue(res["result"]["model"]["verified"])
        serve.assert_not_called()
        pub.assert_not_called()
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), tree)       # 何も作られない
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertFalse(self.server_yaml.exists())
        with self.assertRaises(OSError):
            socket.create_connection((HOST, self.port), timeout=1).close()                                # 待ち受けていない
        blob = json.dumps(res, ensure_ascii=False)
        for secret in (hashlib.sha256(CKPT).hexdigest(), str(self.root)):
            self.assertNotIn(secret, blob)

    def test_incomplete_config_stops_at_the_config_stage_with_a_fixed_message(self):
        self.cfg_path.write_text("mode: production\nbind:\n  host:\n  port:\n", encoding="utf-8")
        res = self.admin.check()
        self.assertEqual((res["stage"], res["check_ok"]), ("config", False))
        self.assertIn("bind", res["error"])

    def test_check_reports_a_model_that_changed_after_saving(self):
        if not prod_server._waitress_importable():
            self.skipTest("waitress が無い環境")
        self.admin.save(self.body())
        self.w.write_bytes(CKPT + b"tampered")
        res = self.admin.check()
        self.assertFalse(res["check_ok"])
        self.assertFalse(res["result"]["model"]["verified"])


class HttpTest(AdminBase):
    def setUp(self):
        super().setUp()
        self.server = prod_admin.make_server(self.admin, port=0)
        self.port_admin = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.port_admin}"
        self.hdr = {"X-Admin-Request": "1", "Content-Type": "application/json"}

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, body=None, headers=None):
        return requests.post(self.base + path, data=json.dumps(body or {}), headers=headers if headers is not None else self.hdr,
                             timeout=60, proxies=NO_PROXY)

    def test_page_and_state(self):
        r = requests.get(self.base + "/", timeout=5, proxies=NO_PROXY)
        self.assertEqual(r.status_code, 200)
        self.assertIn("図面解析 AI モデル管理", r.text)
        self.assertIn("参照…", r.text)
        s = requests.get(self.base + "/api/state", timeout=5, proxies=NO_PROXY).json()
        self.assertEqual(s["default_weights_path"], prod_admin.DEFAULT_WEIGHTS_PATH)
        self.assertEqual(s["current"]["config_file"], "prod_server.local.yaml")
        self.assertNotIn(str(self.root), json.dumps(s))
        self.assertEqual(requests.get(self.base + "/nope", timeout=5, proxies=NO_PROXY).status_code, 404)

    def test_full_flow_over_http_without_leaking_values_or_logging(self):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            ins = self.post("/api/model/inspect", self.body()).json()
            self.assertTrue(ins["inspection"]["ok"])
            sv = self.post("/api/model/save", self.body(model_id="")).json()
            self.assertTrue(sv["saved"])
            st = requests.get(self.base + "/api/state", timeout=5, proxies=NO_PROXY).json()
            self.assertEqual(st["current"]["model"]["weights"], NAME)
            self.assertEqual(st["current"]["model"]["weights_sha256_12"], hashlib.sha256(CKPT).hexdigest()[:12])
            self.post("/api/model/save", self.body(weights_path=str(self.mdir / "missing.pt")))
        self.assertEqual((err.getvalue(), out.getvalue()), ("", ""))                        # アクセスログ・出力を出さない
        for blob in (json.dumps(ins), json.dumps(sv), json.dumps(st)):
            for secret in (hashlib.sha256(CKPT).hexdigest(), str(self.root), str(self.mdir)):
                self.assertNotIn(secret, blob)

    def test_errors_are_fixed_messages(self):
        r = self.post("/api/model/save", self.body(weights_path=str(self.mdir / "missing.pt")))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["saved"])
        r = self.post("/api/model/browse", {"kind": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn(str(self.root), r.text)
        with mock.patch.object(prod_admin.ModelAdmin, "state", side_effect=RuntimeError(str(self.root))):
            r = requests.get(self.base + "/api/state", timeout=5, proxies=NO_PROXY)
            self.assertEqual(r.status_code, 500)
            self.assertNotIn(str(self.root), r.text)

    def test_guards_on_changing_operations(self):
        self.assertEqual(self.post("/api/model/inspect", self.body(), headers={"Content-Type": "application/json"}).status_code, 403)   # カスタムヘッダなし
        self.assertEqual(self.post("/api/model/inspect", self.body(), headers={"X-Admin-Request": "1", "Content-Type": "text/plain"}).status_code, 403)
        self.assertEqual(self.post("/api/model/inspect", self.body(), headers={**self.hdr, "Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.post("/api/model/inspect", self.body(), headers={**self.hdr, "Origin": f"http://127.0.0.1:{self.port_admin}"}).status_code, 200)
        self.assertEqual(self.post("/api/nope").status_code, 404)
        try:                                   # 大きすぎる本文は読まずに拒否する(応答前に接続が閉じられることもある)
            big = requests.post(self.base + "/api/model/inspect", data=b"x" * (prod_admin.MAX_BODY + 10), headers=self.hdr, timeout=10, proxies=NO_PROXY)
            self.assertIn(big.status_code, (400, 403))
        except requests.ConnectionError:
            pass
        bad = requests.post(self.base + "/api/model/inspect", data=b"[1]", headers=self.hdr, timeout=10, proxies=NO_PROXY)
        self.assertEqual(bad.status_code, 400)

    def test_lan_access_is_refused(self):
        # 1. LAN のホスト名・アドレスでの Host(DNS リバインディング・プロキシ経由)
        for host in ("192.0.2.5", f"192.0.2.5:{self.port_admin}", "evil.example", "10.1.2.3"):
            with self.subTest(host=host):
                r = requests.get(self.base + "/api/state", headers={"Host": host}, timeout=5, proxies=NO_PROXY)
                self.assertEqual(r.status_code, 403)
                r = self.post("/api/model/save", self.body(), headers={**self.hdr, "Host": host})
                self.assertEqual(r.status_code, 403)
        # 2. LAN の接続元(peer)
        with mock.patch.object(prod_admin, "peer_is_loopback", return_value=False):
            self.assertEqual(requests.get(self.base + "/", timeout=5, proxies=NO_PROXY).status_code, 403)
            self.assertEqual(self.post("/api/check").status_code, 403)
        self.assertTrue(prod_admin.peer_is_loopback("127.0.0.1") and prod_admin.peer_is_loopback("::1"))
        for addr in ("192.0.2.5", "10.0.0.1", "0.0.0.0", "::2", ""):
            self.assertFalse(prod_admin.peer_is_loopback(addr), addr)
        # 3. 待ち受けは 127.0.0.1 だけ(LAN 側のアドレスには接続できない)・ワイルドカードでは作れない
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for bad_host in ("0.0.0.0", "", "192.0.2.5", "::"):
            with self.assertRaises(ValueError):
                prod_admin.make_server(self.admin, port=0, host=bad_host)
        try:
            lan = [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET) if not i[4][0].startswith("127.")]
        except OSError:
            lan = []
        if lan:
            with self.assertRaises(OSError):
                socket.create_connection((lan[0], self.port_admin), timeout=2).close()

    def test_nothing_was_written_outside_the_designated_files(self):
        before = sorted(p.name for p in self.root.rglob("*") if p.is_file())
        self.post("/api/model/inspect", self.body())
        self.assertEqual(sorted(p.name for p in self.root.rglob("*") if p.is_file()), before)        # 検証は読み取りだけ(コピー・一時ファイルなし)


class StructureTest(unittest.TestCase):
    SRC = (Path(prod_admin.__file__)).read_text(encoding="utf-8")

    def test_the_admin_never_starts_or_publishes(self):
        for forbidden in ("prod_publish", ".serve(", "create_server", "publish_config", "signal.signal"):
            self.assertNotIn(forbidden, self.SRC, forbidden)

    def test_no_upload_or_copy_of_models(self):
        for forbidden in ("shutil", "copyfile", "copy2", "multipart", "request.files", "os.rename", "os.remove"):
            self.assertNotIn(forbidden, self.SRC, forbidden)

    def test_requirements_real_pins_the_verified_torch(self):
        reqs = (Path(prod_admin.__file__).resolve().parent / "requirements-real.txt").read_text(encoding="utf-8")
        self.assertIn("torch==2.5.1+cu124", reqs)
        self.assertIn("torchvision==0.20.1+cu124", reqs)
        self.assertNotIn("2.6.0", reqs.replace("torch 2.6.0+cu124", "").replace("torch 2.6", ""))   # 失敗する組み合わせの注記以外に、2.6.0 の指定を残さない
        self.assertTrue(all(ord(c) < 128 for c in reqs))                                              # pip(cp932 環境)で読めるよう ASCII だけ

    def test_yaml_dir_config_rules(self):
        base = {"id": "m1", "dir": "C:\\m", "weights": "a.pt", "yaml": "m.yaml",
                "expected": {"weights_sha256": "0" * 64, "weights_size": 1, "yaml_sha256": "1" * 64}}
        self.assertEqual(prod_config._model(base).yaml_dir, "")
        self.assertEqual(prod_config._model({**base, "yaml_dir": "D:\\y"}).yaml_dir, "D:\\y")
        for bad in ("relative", 5, ["x"]):
            with self.subTest(bad=bad):
                with self.assertRaises(prod_config.ConfigError):
                    prod_config._model({**base, "yaml_dir": bad})


# ----------------------------------------------------------------------------- 指定モデル(重みと同じフォルダに model.yaml が無く、現行 model.yaml の my_model が別名)
SRC_YAML = ("# 現行サーバーの model.yaml(テスト用の合成コピー)\r\nmodel:\r\n  use_model_type: yolov8\r\n  my_model: best.pt   # 重みの参照名\r\n  threshold: 0.81\r\n"
            "  use_device: cuda:0\r\nsahi:\r\n  slice_higth: 640\r\n  slice_width: 640\r\n  over_lap_higth: 0.2\r\n  over_lap_width: 0.2\r\n")
DESIGNATED = "estimatic_a_20250826.pt"
COPY_NAME = DESIGNATED[:-3] + ".model.local.yaml"


class RemapBase(AdminBase):
    def setUp(self):
        super().setUp()
        self.share = self.root / "share_model"                    # 共有上のモデルフォルダの代わり(重みだけ。model.yaml は無い)
        self.share.mkdir()
        self.dw = self.share / DESIGNATED
        self.dw.write_bytes(CKPT)
        self.src = self.root / "current" / "model.yaml"           # 現行の model.yaml(my_model: best.pt)
        self.src.parent.mkdir()
        self.src.write_bytes(SRC_YAML.encode("utf-8"))
        self.local_dir = self.root / "local_config"
        self.admin = prod_admin.ModelAdmin(self.cfg_path, picker=prod_admin.FilePicker(runner=self.fake_runner), local_dir=self.local_dir)

    def snap(self):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for d in (self.share, self.src.parent) for p in d.rglob("*") if p.is_file()}

    def rbody(self, **kw):
        return {"weights_path": str(self.dw), "yaml_path": str(self.src), "remap": True, **kw}


class RemapYamlTest(unittest.TestCase):
    def test_only_the_my_model_line_changes(self):
        r = prod_admin.remap_yaml(SRC_YAML, DESIGNATED)
        old, new = SRC_YAML.split("\r\n"), r["text"].split("\r\n")
        self.assertEqual(len(old), len(new))
        self.assertEqual([i for i, (a, b) in enumerate(zip(old, new)) if a != b], [3])
        self.assertEqual(new[3], "  my_model: " + DESIGNATED + "   # 重みの参照名")                 # インデント・コメント・改行コードを保つ
        self.assertEqual(r["diff"], {"before": "my_model: best.pt   # 重みの参照名", "after": "my_model: " + DESIGNATED + "   # 重みの参照名"})
        a, b = yaml.safe_load(SRC_YAML), yaml.safe_load(r["text"])
        self.assertEqual(b["model"]["my_model"], DESIGNATED)
        a["model"].pop("my_model")
        b["model"].pop("my_model")
        self.assertEqual(a, b)                                                                     # 構造・閾値・デバイス・スライス条件は同一
        self.assertIn("model.threshold = 0.81", r["unchanged"])
        self.assertIn("model.use_device = cuda:0", r["unchanged"])
        self.assertIn("sahi.slice_higth = 640", r["unchanged"])
        self.assertFalse(any("my_model" in u for u in r["unchanged"]))

    def test_quoted_value_and_lf(self):
        r = prod_admin.remap_yaml("model:\n  my_model: 'best.pt'\n  threshold: 0.5\nsahi:\n  slice_higth: 1\n", "x.pt")
        self.assertEqual(r["text"], "model:\n  my_model: 'x.pt'\n  threshold: 0.5\nsahi:\n  slice_higth: 1\n")

    def test_refuses_when_the_line_cannot_be_pinned_down(self):
        for text in ("model:\n  threshold: 1\n", "model:\n  my_model: a.pt\n  my_model: b.pt\n", "other:\n  my_model: a.pt\nmodel:\n  threshold: 1\n",
                     "model: {my_model: a.pt}\n"):
            with self.subTest(text=text):
                with self.assertRaises(prod_admin.AdminError):
                    prod_admin.remap_yaml(text, "x.pt")


class RemapInspectTest(RemapBase):
    def test_without_the_explicit_choice_it_is_refused_but_the_diff_is_shown(self):
        res, internal = prod_admin.inspect_model(str(self.dw), str(self.src), remap=False, local_dir=self.local_dir)
        self.assertFalse(res["ok"])
        self.assertIsNone(internal)
        self.assertTrue(res["remap"]["available"] and not res["remap"]["selected"])
        self.assertEqual((res["remap"]["from"], res["remap"]["to"]), ("best.pt", DESIGNATED))
        self.assertIn("設定用コピーを作る", res["pair"]["message"])
        self.assertFalse(self.local_dir.exists())                                                  # 検証では何も書かない

    def test_with_the_choice_the_pair_is_accepted_and_nothing_is_written(self):
        before = self.snap()
        res, internal = prod_admin.inspect_model(str(self.dw), str(self.src), remap=True, local_dir=self.local_dir)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["pair"]["ok"])
        self.assertEqual(res["yaml"]["name"], COPY_NAME)
        self.assertEqual(res["yaml"]["generated_from"], "model.yaml")
        self.assertEqual(res["remap"]["diff"]["after"].split("#")[0].strip(), "my_model: " + DESIGNATED)
        self.assertEqual(internal["yaml_sha256"], hashlib.sha256(internal["yaml_generated"]).hexdigest())
        self.assertEqual(internal["weights_sha256"], hashlib.sha256(CKPT).hexdigest())
        self.assertFalse(self.local_dir.exists())
        self.assertEqual(self.snap(), before)
        blob = json.dumps(res, ensure_ascii=False)
        for secret in (hashlib.sha256(CKPT).hexdigest(), str(self.root)):
            self.assertNotIn(secret, blob)

    def test_a_yaml_that_cannot_be_remapped_stays_refused(self):
        self.src.write_bytes(b"model:\n  threshold: 1\nsahi:\n  a: 1\n")
        res, internal = prod_admin.inspect_model(str(self.dw), str(self.src), remap=True, local_dir=self.local_dir)
        self.assertFalse(res["ok"])
        self.assertIsNone(internal)


class RemapSaveTest(RemapBase):
    def test_designated_model_is_saved_with_a_local_copy_and_the_pair_is_accepted(self):
        before = self.snap()
        res = self.admin.save(self.rbody())
        self.assertTrue(res["saved"], res)
        copy = self.local_dir / COPY_NAME
        self.assertEqual(res["yaml_copy_file"], COPY_NAME)
        self.assertTrue(copy.is_file())
        # 共有側(重み・元の model.yaml)は何も変わらない。重みは複製・改名されない
        self.assertEqual(self.snap(), before)
        self.assertEqual(sorted(p.name for p in self.share.iterdir()), [DESIGNATED])
        # コピーは、my_model の 1 行だけが違う
        old, new = SRC_YAML.split("\r\n"), copy.read_bytes().decode("utf-8").split("\r\n")
        self.assertEqual([i for i, (a, b) in enumerate(zip(old, new)) if a != b], [3])
        a, b = yaml.safe_load(SRC_YAML), yaml.safe_load(copy.read_text(encoding="utf-8"))
        self.assertEqual(b["model"]["my_model"], DESIGNATED)
        a["model"].pop("my_model")
        b["model"].pop("my_model")
        self.assertEqual(a, b)
        # 設定: 重みは指定の場所を直接参照。model.yaml はローカルのコピー。期待値は実ファイルから
        d = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))["model"]
        self.assertEqual((d["dir"], d["weights"], d["yaml_dir"], d["yaml"]), (str(self.share), DESIGNATED, str(self.local_dir), COPY_NAME))
        self.assertEqual(d["expected"], {"weights_sha256": hashlib.sha256(CKPT).hexdigest(), "weights_size": len(CKPT),
                                         "yaml_sha256": hashlib.sha256(copy.read_bytes()).hexdigest()})
        cfg = prod_config.load_config(str(self.cfg_path))
        ident, summary = prod_server._check_model_in_place(cfg)                                   # 組み合わせが受理される
        self.assertEqual((ident.weights_size, summary["device"], summary["model_type"]), (len(CKPT), "cuda:0", "yolov8"))
        staged_parent = self.root / "staging"
        staged_parent.mkdir()
        _, staged = prod_model.stage_and_verify(cfg.model.id, cfg.model.dir, cfg.model.weights, cfg.model.yaml_name, cfg.model.weights_sha256,
                                                cfg.model.weights_size, cfg.model.yaml_sha256, str(staged_parent), yaml_dir=cfg.model.yaml_dir)
        try:                                                                                       # SahiBackend が読む my_model の名前の重みが、ステージングにある
            my = yaml.safe_load((Path(staged) / COPY_NAME).read_text(encoding="utf-8"))["model"]["my_model"]
            self.assertTrue((Path(staged) / my).is_file())
        finally:
            prod_model.remove_staging(staged)
        blob = json.dumps(res, ensure_ascii=False)
        for secret in (hashlib.sha256(CKPT).hexdigest(), str(self.root)):
            self.assertNotIn(secret, blob)

    def test_check_after_saving_accepts_the_pair(self):
        if not prod_server._waitress_importable():
            self.skipTest("waitress が無い環境")
        self.admin.save(self.rbody())
        with mock.patch.object(prod_server, "serve", side_effect=AssertionError("起動しない")):
            res = self.admin.check()
        self.assertEqual((res["stage"], res["check_ok"]), ("check", True), res)
        self.assertTrue(res["result"]["model"]["verified"])
        self.assertEqual(res["result"]["model"]["weights_sha256_12"], hashlib.sha256(CKPT).hexdigest()[:12])

    def test_other_settings_in_the_config_are_kept(self):
        before = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.admin.save(self.rbody())
        after = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        for key in before:
            if key != "model":
                self.assertEqual(after[key], before[key], key)
        self.assertIn("この行のコメントは保存後も残ること", self.cfg_path.read_text(encoding="utf-8"))

    def test_saving_twice_is_idempotent_and_a_changed_copy_is_backed_up(self):
        self.admin.save(self.rbody())
        copy = self.local_dir / COPY_NAME
        first = copy.read_bytes()
        self.admin.save(self.rbody())
        self.assertEqual(copy.read_bytes(), first)
        self.assertEqual(list(self.local_dir.glob("*.bak-*")), [])                                 # 同じ内容なら退避しない
        copy.write_bytes(b"hand edited\n")
        self.admin.save(self.rbody())
        self.assertEqual(copy.read_bytes(), first)
        self.assertEqual([p.read_bytes() for p in self.local_dir.glob("*.bak-*")], [b"hand edited\n"])

    def test_without_the_explicit_choice_nothing_is_written(self):
        before_cfg = self.cfg_path.read_bytes()
        res = self.admin.save(self.rbody(remap=False))
        self.assertFalse(res["saved"])
        self.assertEqual(self.cfg_path.read_bytes(), before_cfg)
        self.assertFalse(self.local_dir.exists())
        self.assertEqual(list(self.root.glob("prod_server.bak-*")), [])

    def test_failure_while_saving_keeps_the_existing_config_and_the_copy_state(self):
        before_cfg, before = self.cfg_path.read_bytes(), self.snap()
        real = prod_admin.ModelAdmin._atomic_write
        calls = []

        def flaky(path, data):
            calls.append(Path(path).name)
            if len(calls) == 2:                                   # 1 回目: 設定用コピー、2 回目: 設定ファイルの置換で失敗
                raise PermissionError("locked")
            return real(path, data)
        with mock.patch.object(prod_admin.ModelAdmin, "_atomic_write", staticmethod(flaky)):
            with self.assertRaises(PermissionError):
                self.admin.save(self.rbody())
        self.assertEqual(self.cfg_path.read_bytes(), before_cfg)                                  # 既存の設定は壊れない
        self.assertEqual(list(self.local_dir.glob("*.model.local.yaml")), [])                      # 新規に作ったコピーも残さない
        self.assertEqual([p.name for p in self.root.rglob("*.tmp")], [])
        self.assertEqual(self.snap(), before)
        # 既存のコピーがあったときは、保存前の内容へ戻す
        self.local_dir.mkdir(exist_ok=True)
        pre = self.local_dir / COPY_NAME
        pre.write_bytes(b"previous copy\n")
        calls.clear()
        with mock.patch.object(prod_admin.ModelAdmin, "_atomic_write", staticmethod(flaky)):
            with self.assertRaises(PermissionError):
                self.admin.save(self.rbody())
        self.assertEqual(pre.read_bytes(), b"previous copy\n")
        self.assertEqual(self.cfg_path.read_bytes(), before_cfg)

    def test_copy_is_never_written_to_a_share_or_a_non_local_yaml(self):
        before_cfg = self.cfg_path.read_bytes()
        unc = prod_admin.ModelAdmin(self.cfg_path, local_dir="\\\\server\\share\\cfg")
        with self.assertRaises(prod_admin.AdminError):
            unc.save(self.rbody())
        self.assertEqual(self.cfg_path.read_bytes(), before_cfg)
        res, internal = prod_admin.inspect_model(str(self.dw), str(self.src), remap=True, local_dir=self.local_dir)
        internal["yaml_name"] = "model.yaml"                                                       # *.local.yaml でない名前は書かない(防御)
        with mock.patch.object(prod_admin, "inspect_model", return_value=(res, internal)):
            with self.assertRaises(prod_admin.AdminError):
                self.admin.save(self.rbody())

    def test_the_copy_directory_is_outside_git_tracking(self):
        import subprocess
        repo = Path(prod_admin.__file__).resolve().parent.parent
        target = prod_admin.HERE / prod_admin.LOCAL_DIR_NAME / COPY_NAME
        try:
            r = subprocess.run(["git", "check-ignore", "-q", str(target)], cwd=repo, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git が使えない環境")
        if r.returncode == 128:
            self.skipTest("Git のリポジトリではない")
        self.assertEqual(r.returncode, 0, "設定用コピーが Git 管理の対象になります")


class RemapHttpTest(RemapBase):
    def setUp(self):
        super().setUp()
        self.server = prod_admin.make_server(self.admin, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.hdr = {"X-Admin-Request": "1", "Content-Type": "application/json"}

    def test_state_inspect_and_save_over_http(self):
        st = requests.get(self.base + "/api/state", timeout=5, proxies=NO_PROXY).json()
        self.assertEqual(st["default_yaml_path"], prod_admin.DEFAULT_YAML_PATH)
        page = requests.get(self.base + "/", timeout=5, proxies=NO_PROXY).text
        self.assertIn("設定用コピーを作って使う", page)
        ins = requests.post(self.base + "/api/model/inspect", data=json.dumps(self.rbody(remap=False)), headers=self.hdr, timeout=60, proxies=NO_PROXY).json()
        self.assertFalse(ins["inspection"]["ok"])
        self.assertTrue(ins["inspection"]["remap"]["available"])
        sv = requests.post(self.base + "/api/model/save", data=json.dumps(self.rbody()), headers=self.hdr, timeout=60, proxies=NO_PROXY).json()
        self.assertTrue(sv["saved"], sv)
        self.assertEqual(sv["yaml_copy_file"], COPY_NAME)
        self.assertNotIn(str(self.root), json.dumps(sv))


# ----------------------------------------------------------------------------- 書き込み先はローカルディスク内に限る(読み取り元の UNC とは別)
def make_junction(link: Path, target: Path) -> bool:
    import subprocess
    if os.name != "nt":
        return False
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    return r.returncode == 0 and link.exists()


def real_drive_types():
    """A〜Z の各ドライブの種類を、実際の GetDriveTypeW で調べる(モックを使わない)。"""
    import ctypes
    return {c: ctypes.windll.kernel32.GetDriveTypeW(c + ":\\") for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}


class StorageGuardTest(RemapBase):
    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))

    def assertFixed(self, fn, *a, **kw):
        with self.assertRaises(prod_admin.AdminError) as cm:
            fn(*a, **kw)
        self.assertEqual(str(cm.exception), prod_admin.STORAGE_MESSAGE)                 # 固定文(パス・例外メッセージを含めない)
        return cm.exception

    @unittest.skipUnless(os.name == "nt", "Windows のドライブ判定")
    def test_a_normal_local_folder_is_accepted_with_the_real_drive_type(self):
        d = self.root / "plain"
        d.mkdir()
        if real_drive_types()[os.path.splitdrive(str(d))[0][0].upper()] != prod_admin.DRIVE_FIXED:
            self.skipTest("一時フォルダが固定ディスクではない環境")
        self.assertEqual(os.path.normcase(prod_admin.require_local_dir(d)), os.path.normcase(str(d)))
        self.assertEqual(os.path.normcase(prod_admin.require_local_dir(d / "not" / "yet" / "created")), os.path.normcase(str(d / "not" / "yet" / "created")))

    def test_unc_forms_are_rejected(self):
        for p in ("\\\\server\\share\\cfg", "//server/share/cfg", "\\\\?\\UNC\\server\\share\\cfg", "\\\\?\\C:\\cfg", "\\\\127.0.0.1\\C$\\x", "relative\\x" if os.name != "nt" else "\\\\x"):
            with self.subTest(p=p):
                self.assertFixed(prod_admin.require_local_dir, p)

    def test_network_and_other_non_fixed_drives_are_rejected_by_drive_type(self):
        for kind in (0, 1, 2, 4, 5, 6):                                                 # 不明・なし・取り外し可能・ネットワーク・CD・RAM
            with self.subTest(kind=kind):
                self.assertFixed(prod_admin.require_local_dir, self.root / "x", lambda r, k=kind: k)

    @unittest.skipUnless(os.name == "nt", "Windows のドライブ判定")
    def test_real_non_fixed_drives_are_rejected_without_mocks(self):
        types = real_drive_types()
        others = [c for c, t in types.items() if t in (2, 4)]                           # 実在する取り外し可能・ネットワークドライブ
        if not others:
            self.skipTest("取り外し可能・ネットワークのドライブがない環境")
        for c in others:
            with self.subTest(drive=c, kind=types[c]):
                self.assertFixed(prod_admin.require_local_dir, f"{c}:\\prod_admin_test\\cfg")
        missing = [c for c, t in types.items() if t == 1]                               # 存在しないドライブ文字
        self.assertFixed(prod_admin.require_local_dir, f"{missing[0]}:\\cfg")

    @unittest.skipUnless(os.name == "nt", "Windows のジャンクション")
    def test_a_junction_in_the_path_is_rejected_even_when_it_points_to_a_local_folder(self):
        target = self.root / "elsewhere"
        target.mkdir()
        link = self.root / "link"
        if not make_junction(link, target):
            self.skipTest("ジャンクションを作れない環境")
        self.assertFixed(prod_admin.require_local_dir, link)
        self.assertFixed(prod_admin.require_local_dir, link / "sub")                  # まだ無い階層でも、親がジャンクションなら拒否
        self.assertFixed(prod_admin.require_local_dir, link / "a" / "b")
        (target / "real_sub").mkdir()
        self.assertFixed(prod_admin.require_local_dir, link / "real_sub")             # 解決先に実在する下位フォルダでも拒否

    def test_a_symlink_in_the_path_is_rejected(self):
        target = self.root / "elsewhere2"
        target.mkdir()
        link = self.root / "symlink"
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("シンボリックリンクを作る権限がない環境")
        self.assertFixed(prod_admin.require_local_dir, link)
        self.assertFixed(prod_admin.require_local_dir, link / "sub")

    @unittest.skipUnless(os.name == "nt", "Windows のジャンクション")
    def test_a_directory_that_resolves_to_another_drive_or_a_share_is_rejected(self):
        # realpath が UNC / 別ドライブへ解決される場合(SUBST・マップ)を、実体の判定で拒否する(realpath と GetDriveTypeW を差し替えて再現)
        d = self.root / "plain2"
        d.mkdir()
        with mock.patch("os.path.realpath", return_value="\\\\server\\share\\x"):
            self.assertFixed(prod_admin.require_local_dir, d)
        other = "Z:\\x" if not str(d).upper().startswith("Z:") else "Y:\\x"
        with mock.patch("os.path.realpath", return_value=other):
            self.assertFixed(prod_admin.require_local_dir, d, lambda r: 3)
        with mock.patch("os.path.realpath", side_effect=OSError("secret")):             # 確認できない = 証明できない(例外メッセージを出さない)
            exc = self.assertFixed(prod_admin.require_local_dir, d)
        self.assertNotIn("secret", str(exc))

    # ---- 管理画面(ModelAdmin)の動作: 書き込みの前に固定文で拒否し、何も作らない
    def test_unc_config_path_is_refused_before_anything_is_written(self):
        unc = prod_admin.ModelAdmin("\\\\server\\share\\prod_server.local.yaml", local_dir=self.local_dir)
        before = self.tree()
        self.assertFixed(unc.save, self.rbody())
        self.assertEqual(self.tree(), before)
        st = unc.state()
        self.assertEqual(st["current"]["storage_error"], prod_admin.STORAGE_MESSAGE)
        self.assertFalse(st["current"]["exists"])
        self.assertIsNone(st["current"]["model"])
        unc2 = prod_admin.ModelAdmin(self.cfg_path, local_dir="\\\\server\\share\\local_config")
        self.assertFixed(unc2.save, self.rbody())

    def test_network_drive_config_is_refused_before_backup_or_copy(self):
        net = prod_admin.ModelAdmin(self.cfg_path, local_dir=self.local_dir, drive_type=lambda r: 4)
        cfg_before, before = self.cfg_path.read_bytes(), self.tree()
        self.assertFixed(net.save, self.rbody())
        self.assertEqual(self.cfg_path.read_bytes(), cfg_before)
        self.assertEqual(self.tree(), before)                                          # バックアップ・一時ファイル・設定用コピーのフォルダを作らない
        self.assertEqual(net.state()["current"]["storage_error"], prod_admin.STORAGE_MESSAGE)

    @unittest.skipUnless(os.name == "nt", "Windows のジャンクション")
    def test_junction_local_config_is_refused_and_nothing_is_written_through_it(self):
        target = self.root / "elsewhere3"
        target.mkdir()
        link = self.root / "local_config_link"
        if not make_junction(link, target):
            self.skipTest("ジャンクションを作れない環境")
        admin = prod_admin.ModelAdmin(self.cfg_path, local_dir=link)
        cfg_before = self.cfg_path.read_bytes()
        self.assertFixed(admin.save, self.rbody())
        self.assertEqual(self.cfg_path.read_bytes(), cfg_before)
        self.assertEqual(list(target.iterdir()), [])                                   # 解決先へ何も書いていない
        self.assertEqual(list(self.root.glob("prod_server.bak-*")), [])
        admin2 = prod_admin.ModelAdmin(self.cfg_path, local_dir=link / "sub")          # まだ無い下位フォルダ
        self.assertFixed(admin2.save, self.rbody())
        self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows のジャンクション")
    def test_config_folder_through_a_junction_is_refused(self):
        real_cfg_dir = self.root / "real_cfg"
        real_cfg_dir.mkdir()
        (real_cfg_dir / "prod_server.local.yaml").write_bytes(self.cfg_path.read_bytes())
        link = self.root / "cfg_link"
        if not make_junction(link, real_cfg_dir):
            self.skipTest("ジャンクションを作れない環境")
        admin = prod_admin.ModelAdmin(link / "prod_server.local.yaml", local_dir=self.local_dir)
        before = (real_cfg_dir / "prod_server.local.yaml").read_bytes()
        self.assertFixed(admin.save, self.rbody())
        self.assertEqual((real_cfg_dir / "prod_server.local.yaml").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in real_cfg_dir.iterdir()), ["prod_server.local.yaml"])

    def test_a_config_file_that_is_itself_a_link_is_refused(self):
        real = self.root / "real_file.local.yaml"
        real.write_bytes(self.cfg_path.read_bytes())
        link = self.root / "linked.local.yaml"
        try:
            os.symlink(str(real), str(link))
        except (OSError, NotImplementedError):
            self.skipTest("シンボリックリンクを作る権限がない環境")
        before = real.read_bytes()
        self.assertFixed(prod_admin.ModelAdmin(link, local_dir=self.local_dir).save, self.rbody())
        self.assertEqual(real.read_bytes(), before)

    def test_recheck_after_creating_the_copy_folder_removes_it_and_keeps_the_config(self):
        cfg_before = self.cfg_path.read_bytes()
        real = prod_admin.require_local_dir
        calls = []

        def flaky(path, drive_type=prod_admin.default_drive_type):
            calls.append(str(path))
            if Path(path) == self.local_dir and self.local_dir.exists():              # 作成した後の再確認で、実体が別の場所だと分かった想定
                raise prod_admin.AdminError(prod_admin.STORAGE_MESSAGE)
            return real(path, drive_type)
        with mock.patch.object(prod_admin, "require_local_dir", flaky):
            self.assertFixed(self.admin.save, self.rbody())
        self.assertFalse(self.local_dir.exists())                                      # 作ったフォルダを残さない
        self.assertEqual(self.cfg_path.read_bytes(), cfg_before)

    def test_reading_from_a_unc_path_is_still_allowed_while_writing_is_local_only(self):
        self.assertTrue(prod_admin.validate_model_path(prod_admin.DEFAULT_WEIGHTS_PATH, prod_admin.WEIGHTS_EXTS, "x").endswith("estimatic_a_20250826.pt"))
        if os.name != "nt":
            self.skipTest("Windows の管理共有")
        drive, rest = os.path.splitdrive(str(self.dw))
        unc_w = "\\\\127.0.0.1\\" + drive[0] + "$" + rest
        drive, rest = os.path.splitdrive(str(self.src))
        unc_y = "\\\\127.0.0.1\\" + drive[0] + "$" + rest
        if not os.path.isfile(unc_w):
            self.skipTest("管理共有(C$)にアクセスできない環境")
        res = self.admin.save({"weights_path": unc_w, "yaml_path": unc_y, "remap": True})   # 読み取り元は UNC、書き込み先はローカル
        self.assertTrue(res["saved"], res)
        self.assertTrue((self.local_dir / COPY_NAME).is_file())
        self.assertTrue(yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))["model"]["dir"].startswith("\\\\"))

    def test_startup_with_an_invalid_save_location_is_refused_with_a_fixed_message(self):
        err = io.StringIO()
        with mock.patch.object(prod_admin, "make_server", side_effect=AssertionError("待ち受けない")), contextlib.redirect_stderr(err):
            code = prod_admin.main(["--config", "\\\\server\\share\\secret_dir\\prod_server.local.yaml", "--port", "0"])
        self.assertEqual(code, 2)
        self.assertIn(prod_admin.STORAGE_MESSAGE, err.getvalue())
        for leak in ("secret_dir", "server", "share"):
            self.assertNotIn(leak, err.getvalue().replace("サーバー", ""))

    def test_http_save_is_refused_with_the_fixed_message_for_a_bad_location(self):
        unc = prod_admin.ModelAdmin("\\\\server\\share\\prod_server.local.yaml", local_dir=self.local_dir)
        server = prod_admin.make_server(unc, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        base = f"http://127.0.0.1:{server.server_address[1]}"
        hdr = {"X-Admin-Request": "1", "Content-Type": "application/json"}
        r = requests.post(base + "/api/model/save", data=json.dumps(self.rbody()), headers=hdr, timeout=30, proxies=NO_PROXY)
        self.assertEqual((r.status_code, r.json()["error"]), (400, prod_admin.STORAGE_MESSAGE))
        st = requests.get(base + "/api/state", timeout=5, proxies=NO_PROXY).json()
        self.assertEqual(st["current"]["storage_error"], prod_admin.STORAGE_MESSAGE)
        self.assertNotIn("share", json.dumps(st, ensure_ascii=False).replace("shared", ""))


class RelativeConfigPathTest(RemapBase):
    def test_a_relative_config_path_is_resolved_once_and_saves_with_absolute_paths(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.root)
        admin = prod_admin.ModelAdmin("prod_server.local.yaml")                      # 相対の --config(起動時の作業フォルダ基準)
        self.assertTrue(admin.config_path.is_absolute() and admin.local_dir.is_absolute())
        os.chdir(self.root / "share_model")                                           # 後で作業フォルダが変わっても、保存先は変わらない
        res = admin.save(self.rbody())
        self.assertTrue(res["saved"], res)
        d = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))["model"]
        self.assertTrue(os.path.isabs(d["yaml_dir"]) and os.path.isabs(d["dir"]))
        self.assertEqual(os.path.normcase(d["yaml_dir"]), os.path.normcase(str(self.root / "local_config")))
        prod_config.load_config(str(self.cfg_path))                                   # 設定として読める

    def test_unc_paths_are_kept_as_is_and_still_refused_for_writing(self):
        admin = prod_admin.ModelAdmin("\\\\server\\share\\prod_server.local.yaml", local_dir="\\\\server\\share\\cfg")
        self.assertTrue(str(admin.config_path).startswith("\\\\"))
        with self.assertRaises(prod_admin.AdminError):
            admin.save(self.rbody())


if __name__ == "__main__":
    unittest.main()
