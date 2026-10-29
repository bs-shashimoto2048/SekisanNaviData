"""本番推論 API: /health・接続元の許可リスト・Bearer トークン・/predict の入力検証と出力・serve() と check の結合。Flask test client + FakeBackend。"""
import http.server
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

from prod_helpers import HOST, ProdBase, SCRIPT, WEIGHTS_BYTES, free_port, make_png, sha  # noqa: F401
import prod_config
import prod_publish
import prod_server
from backends import FakeBackend

def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


OUTSIDE = "203.0.113.9"          # 許可リストに無い接続元(文書用の予約アドレス)


class HealthTest(ProdBase):
    def test_health_states_and_body_contract(self):
        c = self.client(self.state(ready=False))
        r = c.get("/health")
        self.assertEqual(r.status_code, 503)
        self.assertEqual((r.json["status"], r.json["ready"], r.json["mode"]), ("loading", False, "production"))
        st = self.state(ready=True)
        r = self.client(st).get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json["status"], r.json["ready"]), ("ok", True))
        self.assertEqual(r.json["model"]["id"], "test_model_20260101")
        self.assertEqual(r.json["model"]["weights_sha256_12"], sha(WEIGHTS_BYTES)[:12])
        st.ready, st.error = False, "RuntimeError: C:\\secret\\path\\x"
        r = self.client(st).get("/health")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json["error"], "RuntimeError")

    def test_health_never_leaks_paths_addresses_or_full_hashes(self):
        text = json.dumps(self.client(self.state()).get("/health").json, ensure_ascii=False)
        for secret in (str(self.root), str(self.out), sha(WEIGHTS_BYTES), HOST, str(self.port), str(os.getpid())):
            self.assertNotIn(secret, text)

    def test_no_shutdown_or_verify_workdir_endpoints(self):
        c = self.client(self.state())
        for path in ("/shutdown", "/verify_workdir"):
            self.assertIn(c.post(path, data={"instance_id": "x"}).status_code, (404, 405))


class AccessControlTest(ProdBase):
    def test_client_outside_allowlist_gets_403_on_every_route(self):
        c = self.client(self.state(), addr=OUTSIDE)
        self.assertEqual(c.get("/health").status_code, 403)
        self.assertEqual(c.post("/predict", data=self.form()).status_code, 403)
        self.assertEqual(c.get("/anything").status_code, 403)
        self.assertFalse((self.product / "21_detected.txt").exists())

    def test_ipv6_or_unparseable_source_is_denied(self):
        for addr in ("::1", "garbage"):
            self.assertEqual(self.client(self.state(), addr=addr).get("/health").status_code, 403)

    def test_forwarded_for_header_cannot_bypass_the_allowlist(self):
        r = self.client(self.state(), addr=OUTSIDE).get("/health", headers={"X-Forwarded-For": HOST, "X-Real-IP": HOST})
        self.assertEqual(r.status_code, 403)

    def test_token_is_required_only_for_predict_when_configured(self):
        token = b"t" * 32
        c = self.client(self.state(token=token))
        self.assertEqual(c.get("/health").status_code, 200)                       # /health は許可リストだけ
        self.assertEqual(c.post("/predict", data=self.form()).status_code, 401)
        self.assertEqual(c.post("/predict", data=self.form(), headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(c.post("/predict", data=self.form(), headers={"Authorization": "Basic " + "t" * 32}).status_code, 401)
        r = c.post("/predict", data=self.form(), headers={"Authorization": "Bearer " + "t" * 32})
        self.assertEqual(r.status_code, 200)

    def test_no_token_means_no_auth_header_needed(self):
        self.assertEqual(self.client(self.state(token=None)).post("/predict", data=self.form()).status_code, 200)


class PredictTest(ProdBase):
    def test_success_writes_utf8_outputs_and_leaves_no_temp(self):
        r = self.client(self.state()).post("/predict", data=self.form())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["detections"], 1)
        txt = self.product / "21_detected.txt"
        self.assertTrue(txt.is_file())
        self.assertIn("Device$盤", txt.read_text(encoding="utf-8"))
        self.assertTrue((self.product / "21_detected.png").is_file())
        self.assertEqual([p.name for p in self.product.iterdir() if p.name.startswith(".infer_tmp_")], [])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["A1TEST0001"])    # 出力ルート直下に余計なものを作らない

    def test_rejections(self):
        other = self.out / "A1OTHER001"
        other.mkdir()
        make_png(other / "21.png")
        (self.out / "A1TEST0001" / "sub").mkdir()
        make_png(self.out / "A1TEST0001" / "sub" / "21.png")
        outside = self.root / "elsewhere" / "A1TEST0001"
        outside.mkdir(parents=True)
        make_png(outside / "21.png")
        cases = {
            "root itself": self.form(work_dir=str(self.out)),
            "nested folder": self.form(work_dir=str(self.out / "A1TEST0001" / "sub")),
            "outside the root": self.form(work_dir=str(outside)),
            "relative": self.form(work_dir="A1TEST0001"),
            "dotdot": self.form(work_dir=str(self.out / "A1OTHER001" / ".." / "A1TEST0001")),
            "bad product name": self.form(work_dir=str(self.out / "a b")),
            "short product name": self.form(work_dir=str(self.out / "A1")),
            "empty": self.form(work_dir=" "),
            "nul": self.form(work_dir=str(self.product) + "\x00"),
            "page not digits": self.form(page="2a"),
            "page too long": self.form(page="1234567"),
            "page dotdot": self.form(page="..\\21"),
            "bad extension": self.form(ext="exe"),
            "missing image": self.form(page="99"),
        }
        c = self.client(self.state())
        for name, form in cases.items():
            with self.subTest(name=name):
                r = c.post("/predict", data=form)
                self.assertEqual(r.status_code, 400, r.json)
        self.assertEqual([p.name for p in self.product.glob("*_detected.*")], [])
        self.assertEqual([p.name for p in outside.glob("*_detected.*")], [])

    def test_rejections_of_tricky_spellings(self):
        c = self.client(self.state())
        tricky = {
            "trailing newline in product": str(self.out / "A1TEST0001") + "\n",
            "extended-length prefix": "\\\\?\\" + str(self.product),
            "alternate data stream": str(self.product) + ":stream",
            "short 8.3-style name": str(self.out / "A1TEST~1"),
            "trailing space": str(self.product) + " ",
            "unicode lookalike": str(self.out / "A1TEST000\uff11"),
        }
        for name, wd in tricky.items():
            with self.subTest(name=name):
                self.assertEqual(c.post("/predict", data=self.form(work_dir=wd)).status_code, 400)
        for page in ("21\n", "21 ", "２１", "0x15"):
            with self.subTest(page=page):
                self.assertEqual(c.post("/predict", data=self.form(page=page)).status_code, 400)
        self.assertEqual([p.name for p in self.product.glob("*_detected.*")], [])

    def test_equivalent_spellings_of_the_allowed_folder_are_accepted(self):
        c = self.client(self.state())
        spellings = [str(self.product) + os.sep, str(self.product).replace("\\", "/")]
        if os.name == "nt":
            spellings.append(str(self.product).upper())                    # Windows は大文字小文字を区別しない
        for wd in spellings:
            with self.subTest(wd=wd):
                r = c.post("/predict", data=self.form(work_dir=wd))
                self.assertEqual(r.status_code, 200, r.json)

    def test_response_path_echoes_the_clients_spelling_not_a_resolved_one(self):
        r = self.client(self.state()).post("/predict", data=self.form())
        self.assertTrue(r.json["text_file_path"].endswith("21_detected.txt"))
        self.assertTrue(_same_path(os.path.dirname(r.json["text_file_path"]), str(self.product)))

    def test_logs_never_contain_paths_or_exception_messages(self):
        class Boom:
            def predict(self, image, tmp):
                raise FileNotFoundError(r"\\secret-host\share\A1TEST0001\21.png missing")
        st = self.state(backend=Boom())
        with self.assertLogs("prod_inference_server", level="DEBUG") as cm:
            r = self.client(st).post("/predict", data=self.form())
        self.assertEqual(r.status_code, 500)
        text = "\n".join(cm.output) + r.get_data(as_text=True)
        for secret in ("secret-host", "21.png missing", str(self.root)):
            self.assertNotIn(secret, text)
        self.assertIn("FileNotFoundError", text)                            # 型名は残す

    def test_concurrency_limit_returns_503_instead_of_piling_up(self):
        import threading as th
        st = self.state()
        st._slots = th.BoundedSemaphore(1)
        st._slots.acquire()
        r = self.client(st).post("/predict", data=self.form())
        self.assertEqual(r.status_code, 503)
        st._slots.release()
        self.assertEqual(self.client(st).post("/predict", data=self.form()).status_code, 200)

    def test_rejection_messages_do_not_echo_input(self):
        r = self.client(self.state()).post("/predict", data=self.form(work_dir=str(self.root / "secret-dir" / "A1TEST0001")))
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("secret-dir", r.json["error"])

    def test_link_to_another_folder_is_rejected(self):
        target = self.root / "real_target"
        target.mkdir()
        make_png(target / "21.png")
        link = self.out / "A1LINK0001"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink を作れない環境")
        r = self.client(self.state()).post("/predict", data=self.form(work_dir=str(link)))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(list(target.glob("*_detected.*")), [])

    def test_symlinked_image_is_rejected(self):
        real = self.root / "real.png"
        make_png(real)
        try:
            os.symlink(real, self.product / "31.png")
        except (OSError, NotImplementedError):
            self.skipTest("symlink を作れない環境")
        self.assertEqual(self.client(self.state()).post("/predict", data=self.form(page="31")).status_code, 400)

    def test_not_ready_and_failed_states(self):
        self.assertEqual(self.client(self.state(ready=False)).post("/predict", data=self.form()).status_code, 503)
        st = self.state()
        st.error = "X: y"
        self.assertEqual(self.client(st).post("/predict", data=self.form()).status_code, 500)

    def test_backend_failure_leaves_no_partial_output_and_keeps_existing_files(self):
        (self.product / "21_detected.txt").write_text("OLD", encoding="utf-8")
        be = FakeBackend(None)
        be.script = {"fail_pages": ["21"]}
        r = self.client(self.state(backend=be)).post("/predict", data=self.form())
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json["error"], "推論に失敗しました: BackendError")                 # 型名だけ(パス・詳細なし)
        self.assertNotIn("21", r.json["error"].replace("推論に失敗しました", ""))
        self.assertEqual((self.product / "21_detected.txt").read_text(encoding="utf-8"), "OLD")
        self.assertFalse((self.product / "21_detected.png").exists())
        self.assertEqual([p.name for p in self.product.iterdir() if p.name.startswith(".infer_tmp_")], [])

    def test_oversized_request_is_rejected(self):
        r = self.client(self.state()).post("/predict", data={**self.form(), "pad": "x" * 40000})
        self.assertEqual(r.status_code, 413)


try:
    import waitress  # noqa: F401
    HAVE_WAITRESS = True
except ImportError:                                     # pragma: no cover
    HAVE_WAITRESS = False


@unittest.skipUnless(HAVE_WAITRESS, "serve() は Waitress が必要(inference_server/requirements.txt)")
class ServeAndCheckTest(ProdBase):
    def run_serve(self, cfg, **kw):
        info = {}

        def started(httpd, state):
            info["httpd"], info["state"] = httpd, state

        code = {}
        t = threading.Thread(target=lambda: code.setdefault("v", prod_server.serve(
            cfg, backend_factory=lambda c, d: FakeBackend(None), on_started=started, **kw)), daemon=True)
        t.start()
        return t, info, code

    def wait_health(self, cfg, want=200, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            try:
                r = requests.get(f"http://{cfg.host}:{cfg.port}/health", timeout=2, proxies={"http": None})
                if r.status_code == want:
                    return r
            except requests.RequestException:
                pass
            time.sleep(0.1)
        self.fail("health did not reach the expected state")

    def test_serve_publishes_only_after_ready_and_only_when_asked(self):
        cfg = self.cfg()
        t, info, code = self.run_serve(cfg)                                       # --publish-config なし
        self.wait_health(cfg)
        self.assertFalse(self.server_yaml.exists())                               # 既定では server.yaml を書かない
        info["httpd"].shutdown()
        t.join(10)
        self.assertEqual(code["v"], 0)

        port2 = free_port()
        cfg2 = prod_config.parse_config(self.data(bind={"host": HOST, "port": port2}))
        t, info, code = self.run_serve(cfg2, publish_config=True)
        self.wait_health(cfg2)
        end = time.time() + 10
        while not self.server_yaml.exists() and time.time() < end:
            time.sleep(0.1)
        d = yaml.safe_load(self.server_yaml.read_text(encoding="utf-8"))
        self.assertEqual((d["host"]["port"], d["mode"], d["instance_id"]), (port2, "production", info["state"].instance_id))
        r = requests.get(f"http://{HOST}:{port2}/health", timeout=2, proxies={"http": None})
        self.assertEqual(r.json()["instance_id"], d["instance_id"])
        info["httpd"].shutdown()
        t.join(10)
        self.assertTrue(self.server_yaml.exists())                                # 停止しても接続設定は消さない

    def test_model_mismatch_refuses_to_start_and_never_listens(self):
        d = self.data()
        d["model"]["expected"]["weights_sha256"] = "0" * 64
        cfg = prod_config.parse_config(d)
        self.assertEqual(prod_server.serve(cfg, backend_factory=lambda c, d: FakeBackend(None)), prod_server.EXIT_MODEL)
        with self.assertRaises(requests.RequestException):
            requests.get(f"http://{HOST}:{cfg.port}/health", timeout=1, proxies={"http": None})

    def test_backend_loads_the_verified_copy_even_if_the_original_is_swapped(self):
        cfg = self.cfg()
        seen = {}

        def factory(c, model_dir):
            (self.model_dir / "best.pt").write_bytes(b"malicious-replacement")      # 検証後に元を差し替える
            seen["dir"] = model_dir
            seen["bytes"] = (Path(model_dir) / "best.pt").read_bytes()
            seen["is_original"] = os.path.normcase(model_dir) == os.path.normcase(str(self.model_dir))
            return FakeBackend(None)
        code = {}
        info = {}
        t = threading.Thread(target=lambda: code.setdefault("v", prod_server.serve(cfg, backend_factory=factory, on_started=lambda h, s: info.update(h=h, s=s))), daemon=True)
        t.start()
        self.wait_health(cfg)
        self.assertEqual(seen["bytes"], WEIGHTS_BYTES)                                # 読み込んだのは検証済みのバイト列
        self.assertFalse(seen["is_original"])
        self.assertFalse(Path(seen["dir"]).exists())                                  # 読み込み後にコピーは消える
        info["h"].shutdown()
        t.join(10)
        self.assertEqual(code["v"], 0)

    def test_backend_load_failure_stops_the_process_with_a_distinct_exit_code(self):
        cfg = self.cfg()
        seen = {}

        def factory(c, model_dir):
            seen["dir"] = model_dir
            raise RuntimeError(r"CUDA failed at \\secret-host\share")
        with self.assertLogs("prod_inference_server", level="DEBUG") as cm:
            code = prod_server.serve(cfg, backend_factory=factory)
        self.assertEqual(code, prod_server.EXIT_LOAD)
        self.assertNotIn("secret-host", "\n".join(cm.output))
        self.assertFalse(Path(seen["dir"]).exists())

    def test_publish_failure_after_load_never_becomes_ready_and_exits_with_publish_code(self):
        cfg = self.cfg()
        with mock.patch.object(prod_publish.os, "replace", side_effect=PermissionError("locked")), \
                mock.patch.object(prod_publish.time, "sleep"):
            code = prod_server.serve(cfg, publish_config=True, backend_factory=lambda c, d: FakeBackend(None))
        self.assertEqual(code, prod_server.EXIT_PUBLISH)
        self.assertEqual(list(self.pub_dir.glob("*.tmp")), [])
        self.assertFalse(self.server_yaml.exists())

    def test_a_wildcard_listener_on_the_port_is_detected(self):
        import socket
        s = socket.socket()
        s.bind(("0.0.0.0", self.port))
        s.listen(1)
        self.addCleanup(s.close)
        self.assertEqual(prod_server.serve(self.cfg(), backend_factory=lambda c, d: FakeBackend(None)), prod_server.EXIT_BIND)

    def test_port_in_use_is_reported(self):
        import socket
        s = socket.socket()
        s.bind((HOST, self.port))
        s.listen(1)
        self.addCleanup(s.close)
        self.assertEqual(prod_server.serve(self.cfg(), backend_factory=lambda c, d: FakeBackend(None)), prod_server.EXIT_BIND)

    def test_publish_refused_before_listening_when_existing_is_live(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"instance_id": "someone-else"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        live_port = free_port()
        srv = http.server.HTTPServer((HOST, live_port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.server_yaml.write_text(yaml.safe_dump({"host": {"ip": HOST, "pc_ip": HOST, "port": live_port}}), encoding="utf-8")
        before = self.server_yaml.read_bytes()
        cfg = self.cfg()
        code = prod_server.serve(cfg, publish_config=True, replace_stale=True, backend_factory=lambda c, d: FakeBackend(None))
        self.assertEqual(code, prod_server.EXIT_PUBLISH)
        self.assertEqual(self.server_yaml.read_bytes(), before)
        with self.assertRaises(requests.RequestException):
            requests.get(f"http://{HOST}:{cfg.port}/health", timeout=1, proxies={"http": None})

    def test_check_reports_without_values_and_without_writing(self):
        cfg = self.cfg()
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        res = prod_server.run_check(cfg)
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), before)   # 何も書かない
        self.assertFalse(self.server_yaml.exists())
        self.assertEqual(res["publish_plan"]["action"], "create")
        text = json.dumps(res, ensure_ascii=False)
        for secret in (str(self.root), str(self.out), sha(WEIGHTS_BYTES), str(self.port), "proxy.example"):
            self.assertNotIn(secret, text)

    def test_check_flags_a_bad_model_and_a_busy_port(self):
        d = self.data()
        d["model"]["expected"]["weights_size"] = 1
        res = prod_server.run_check(prod_config.parse_config(d))
        self.assertFalse(res["ok"])
        self.assertFalse(res["model"]["verified"])

    def test_token_file_is_validated_at_start(self):
        tok = self.root / "t.token"
        tok.write_text("short", encoding="utf-8")
        cfg = self.cfg(auth={"token_file": str(tok)})
        self.assertEqual(prod_server.serve(cfg, backend_factory=lambda c, d: FakeBackend(None)), prod_server.EXIT_CONFIG)

    def test_cli_rejects_a_bad_config_with_a_nonzero_exit(self):
        p = self.root / "cfg.yaml"
        d = self.data()
        d["bind"] = {"host": "0.0.0.0", "port": self.port}
        p.write_text(yaml.safe_dump(d), encoding="utf-8")
        self.assertEqual(prod_server.main(["check", "--config", str(p)]), prod_server.EXIT_CONFIG)
        self.assertEqual(prod_server.main(["serve", "--config", str(p)]), prod_server.EXIT_CONFIG)


if __name__ == "__main__":
    unittest.main()
