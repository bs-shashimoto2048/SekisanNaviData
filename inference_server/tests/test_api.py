# 推論サーバー API のテスト(Flask test client + FakeBackend。モデル・GPU・外部通信なし。ソケットも開かない)。
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

os.environ["SEKISAN_PRODUCTION_HOSTS"] = "prod-host-example"  # 本番ホスト名はリポジトリに書かない。テスト用の架空ホストを本番扱いにする
import safety  # noqa: E402
import server  # noqa: E402
from backends import FakeBackend  # noqa: E402


def make_png(path, w=200, h=100):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff" * w for _ in range(h))
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class ApiBase(unittest.TestCase):
    SCRIPT = {"pages": {
        "21": [{"name": "panel", "score": 0.9, "cx": 0.5, "cy": 0.5, "w": 0.1, "h": 0.2},
               {"name": "トランス", "score": 0.8, "cx": 0.25, "cy": 0.25, "w": 0.1, "h": 0.1}],
        "22": [{"name": "a|b$c", "score": 0.7, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}],
        "23": []}}

    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="infer_api_")
        self.addCleanup(td.cleanup)
        self.tmp = os.path.realpath(td.name)
        self.root = os.path.join(self.tmp, "root")
        self.work = os.path.join(self.root, "A1BN0001")
        os.makedirs(self.work)
        for p in ("21", "22", "23"):
            make_png(os.path.join(self.work, f"{p}.png"))
        # 出力ルートの外に置く囮(読み書きされてはいけない)
        self.decoy = os.path.join(self.tmp, "decoy")
        os.makedirs(self.decoy)
        make_png(os.path.join(self.decoy, "21.png"))
        self.decoy_before = self._tree(self.decoy)
        self.script_path = os.path.join(self.tmp, "script.json")
        self.write_script(self.SCRIPT)
        self.state = server.ServerState(self.root, "inst-1", "fake")
        self.state.backend = FakeBackend(self.script_path)
        self.state.ready = True
        self.app = server.create_app(self.state)
        self.client = self.app.test_client()

    def write_script(self, d):
        Path(self.script_path).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        if hasattr(self, "state"):
            self.state.backend = FakeBackend(self.script_path)

    @staticmethod
    def _tree(root):
        return {str(p.relative_to(root)): sha(p) for p in sorted(Path(root).rglob("*")) if p.is_file()}

    def post(self, work_dir=None, page="21", ext="png", client=None, **extra):
        data = {"work_dir": self.work if work_dir is None else work_dir, "page_nmb": page, "extension": ext}
        data.update(extra)
        return (client or self.client).post("/predict", data=data)

    def assert_no_tmp_left(self):
        d = os.path.join(self.root, server.TMP_DIRNAME)
        self.assertTrue(not os.path.isdir(d) or os.listdir(d) == [], "一時ディレクトリが残っている")


class HealthTest(ApiBase):
    def test_ready_loading_failed(self):
        r = self.client.get("/health")
        self.assertEqual((r.status_code, r.json["ready"], r.json["status"], r.json["instance_id"]), (200, True, "ok", "inst-1"))
        self.state.ready = False
        r = self.client.get("/health")
        self.assertEqual((r.status_code, r.json["ready"], r.json["status"]), (503, False, "loading"))
        self.assertEqual(self.post().status_code, 503)  # 準備中は推論を受けない
        self.state.error = "FileNotFoundError: model"
        r = self.client.get("/health")
        self.assertEqual((r.status_code, r.json["status"]), (500, "failed"))
        self.assertIn("model", r.json["error"])


class PredictTest(ApiBase):
    def test_success_outputs_and_format(self):
        r = self.post()
        self.assertEqual(r.status_code, 200, r.json)
        txt = os.path.join(self.work, "21_detected.txt")
        self.assertEqual(os.path.realpath(r.json["text_file_path"]), os.path.realpath(txt))
        lines = Path(txt).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^Index\$0\|Device\$panel\|Score\$0\.9\|Coordinates\$[-0-9.e]+,[-0-9.e]+(:[-0-9.e]+,[-0-9.e]+){3}$")
        self.assertIn("Device$トランス", lines[1])           # UTF-8 で日本語クラス名が残る
        self.assertTrue(os.path.isfile(os.path.join(self.work, "21_detected.png")))
        self.assertFalse(os.path.exists(os.path.join(self.work, "prediction_visual.png")))
        self.assert_no_tmp_left()
        # 座標: 200x100 の画像で cx=.5,cy=.5,w=.1,h=.2 -> x1=90,y1=40,x4=110,y4=60
        c = lines[0].split("Coordinates$")[1].split(":")
        self.assertEqual([tuple(map(float, p.split(","))) for p in c], [(90.0, 40.0), (110.0, 40.0), (90.0, 60.0), (110.0, 60.0)])

    def test_delimiter_in_class_name_is_sanitized(self):
        self.assertEqual(self.post(page="22").status_code, 200)
        line = Path(self.work, "22_detected.txt").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(line.count("|"), 3)
        self.assertIn("Device$a_b_c", line)

    def test_zero_detections_writes_empty_file(self):
        self.assertEqual(self.post(page="23").status_code, 200)
        self.assertEqual(Path(self.work, "23_detected.txt").read_text(encoding="utf-8"), "")

    def test_rejected_requests_do_not_touch_anything(self):
        before_root = self._tree(self.root)
        outside_file = os.path.join(self.decoy, "21.png")
        junction = os.path.join(self.work, "junc")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", junction, self.decoy], capture_output=True).returncode == 0
        cases = [
            dict(work_dir="A1BN0001"), dict(work_dir=os.path.join(self.work, "..", "..", "decoy")),
            dict(work_dir=self.decoy), dict(work_dir=self.root), dict(work_dir=self.tmp), dict(work_dir=""),
            dict(page="../21"), dict(page="21x"), dict(page=""), dict(ext="exe"), dict(ext=""), dict(page="99"),
            dict(work_dir=r"\\prod-host-example\ShareData\exampleroot\a_product\output\A1BN0001"),
        ]
        if made:
            cases.append(dict(work_dir=junction))  # ルート内のジャンクションで外へ出る
        for c in cases:
            r = self.post(**c)
            self.assertEqual(r.status_code, 400, (c, r.json))
            self.assertIn("error", r.json)
        r = self.client.post("/predict", data={})  # 項目なし
        self.assertEqual(r.status_code, 400)
        if made:
            os.rmdir(junction)  # ジャンクション自体のみ(リンク先は消えない)
        self.assertEqual(self._tree(self.root), before_root)
        self.assertEqual(self._tree(self.decoy), self.decoy_before)
        self.assertTrue(os.path.isfile(outside_file))

    def test_rerun_replaces_existing_outputs(self):
        txt, png = os.path.join(self.work, "21_detected.txt"), os.path.join(self.work, "21_detected.png")
        Path(txt).write_text("OLD\n", encoding="utf-8")
        Path(png).write_bytes(b"OLD")
        self.assertEqual(self.post().status_code, 200)
        self.assertNotIn("OLD", Path(txt).read_text(encoding="utf-8"))
        self.assertNotEqual(Path(png).read_bytes(), b"OLD")
        first = sha(txt)
        self.assertEqual(self.post().status_code, 200)   # 2回目(既存あり)も成功し、同じ内容
        self.assertEqual(sha(txt), first)
        self.assert_no_tmp_left()

    def test_failure_midway_keeps_existing_outputs(self):
        self.write_script({**self.SCRIPT, "fail_pages": ["22"]})
        txt, png = os.path.join(self.work, "22_detected.txt"), os.path.join(self.work, "22_detected.png")
        Path(txt).write_text("KEEP\n", encoding="utf-8")
        Path(png).write_bytes(b"KEEP")
        r = self.post(page="22")
        self.assertEqual(r.status_code, 500)
        self.assertIn("推論に失敗しました", r.json["error"])
        self.assertEqual(Path(txt).read_text(encoding="utf-8"), "KEEP\n")
        self.assertEqual(Path(png).read_bytes(), b"KEEP")
        self.assert_no_tmp_left()
        self.assertEqual(self.post(page="21").status_code, 200)  # 失敗後も他ページは処理できる

    def test_failure_without_existing_leaves_no_partial_files(self):
        self.write_script({**self.SCRIPT, "fail_pages": ["22"]})
        self.assertEqual(self.post(page="22").status_code, 500)
        self.assertFalse(os.path.exists(os.path.join(self.work, "22_detected.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.work, "22_detected.png")))

    def test_concurrent_pages_and_same_page(self):
        self.write_script({**self.SCRIPT, "delay_sec": 0.05})
        results, lock = [], threading.Lock()

        def worker(page):
            c = self.app.test_client()
            r = self.post(page=page, client=c)
            with lock:
                results.append((page, r.status_code))

        threads = [threading.Thread(target=worker, args=(p,)) for p in ("21", "22", "23", "21", "22", "23", "21", "21")]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual(len(results), 8)
        self.assertTrue(all(s == 200 for _, s in results), results)
        self.assertEqual(len(Path(self.work, "21_detected.txt").read_text(encoding="utf-8").splitlines()), 2)  # 混ざらない
        self.assertEqual(len(Path(self.work, "22_detected.txt").read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(Path(self.work, "23_detected.txt").read_text(encoding="utf-8"), "")
        self.assert_no_tmp_left()
        self.assertEqual(self._tree(self.decoy), self.decoy_before)


class VerifyWorkDirTest(ApiBase):
    def test_probe_visible_and_not_visible(self):
        nonce = "ab12cd34ef56ab12"
        r = self.client.post("/verify_workdir", data={"work_dir": self.work, "nonce": nonce})
        self.assertEqual(r.status_code, 409)           # 目印ファイルが無い = 別の場所を見ている
        Path(self.work, f".probe_{nonce}").write_text("")
        r = self.client.post("/verify_workdir", data={"work_dir": self.work, "nonce": nonce})
        self.assertEqual((r.status_code, r.json["ok"]), (200, True))

    def test_rejects_bad_input(self):
        for d in ({"work_dir": self.decoy, "nonce": "ab12cd34ef56ab12"}, {"work_dir": self.work, "nonce": "../x"},
                  {"work_dir": self.work, "nonce": ""}, {"work_dir": r"\\prod-host-example\x", "nonce": "ab12cd34ef56ab12"}):
            self.assertEqual(self.client.post("/verify_workdir", data=d).status_code, 400, d)


class StartupSafetyTest(unittest.TestCase):
    """起動引数の検証。書き込み・bind が起きる前に拒否されること。"""

    def _args(self, **kw):
        base = dict(output_root=os.path.join(tempfile.gettempdir(), "infer_x_root"), port=18999, host="127.0.0.1",
                    config_out=os.path.join(tempfile.gettempdir(), "infer_x_root", "api", "server.yaml"))
        base.update(kw)
        argv = ["--output-root", base["output_root"], "--port", str(base["port"]), "--host", base["host"],
                "--config-out", base["config_out"], "--backend", "fake"]
        return server.parse_args(argv)

    def test_refusals_happen_before_any_write(self):
        bad = [
            dict(output_root=r"\\prod-host-example\ShareData\exampleroot\a_product", config_out=r"\\prod-host-example\ShareData\exampleroot\a_product\server.yaml"),
            dict(output_root=r"\\prod-host-example\ShareData\exampleroot\a_product\output"),
            dict(config_out=r"\\prod-host-example\ShareData\exampleroot\a_product\server.yaml"),   # 設定出力先だけが本番
            dict(output_root=r"\\other-host\verify"),                                    # UNC は既定で拒否
            dict(host="0.0.0.0"), dict(host="192.0.2.5"), dict(port=0), dict(port=80),
            dict(config_out=os.path.join(tempfile.gettempdir(), "elsewhere", "server.yaml")),  # 出力ルートの外
        ]
        for kw in bad:
            with mock.patch("os.makedirs", side_effect=AssertionError("書き込み前に拒否されるはず")), \
                    mock.patch("builtins.open", side_effect=AssertionError("書き込み前に拒否されるはず")), \
                    mock.patch("os.replace", side_effect=AssertionError), \
                    mock.patch("socket.socket.bind", side_effect=AssertionError("bind 前に拒否されるはず")):
                args = self._args(**kw)
                with self.assertRaises(safety.SafetyError, msg=str(kw)):
                    server.prepare(args)
                self.assertEqual(server.main(["--output-root", kw.get("output_root", self._args().output_root),
                                              "--port", str(kw.get("port", 18999)), "--host", kw.get("host", "127.0.0.1"),
                                              "--config-out", kw.get("config_out", self._args().config_out), "--backend", "fake"]), 2)

    def test_valid_args_accepted(self):
        with tempfile.TemporaryDirectory() as t:
            root = os.path.join(t, "r")
            args = server.parse_args(["--output-root", root, "--port", "18999", "--config-out", os.path.join(root, "api", "server.yaml"),
                                      "--backend", "fake"])
            r, cfg, iid = server.prepare(args)
            self.assertEqual(Path(r), Path(root))
            self.assertTrue(iid)

    def test_server_never_references_production_share_in_code_paths(self):
        # ソース中に本番共有への書き込み経路(shutil.copy 等)が無いこと
        src = Path(server.__file__).read_text(encoding="utf-8")
        for token in ("shutil.copy(", "shutil.copyfile(", "shutil.move("):
            self.assertNotIn(token, src)
        self.assertNotIn("//prod-host-example", src.lower())   # 本番共有のパスを設定値として持たない(docstring の説明のみ)


class NoNetworkTest(ApiBase):
    def test_predict_opens_no_sockets_or_subprocess(self):
        with mock.patch("socket.socket.connect", side_effect=AssertionError("外部接続")) as c, \
                mock.patch("subprocess.Popen", side_effect=AssertionError("サブプロセス")) as p:
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual((c.call_count, p.call_count), (0, 0))


if __name__ == "__main__":
    unittest.main()
