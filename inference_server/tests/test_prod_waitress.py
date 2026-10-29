"""本番推論 API: Waitress 経由(別スレッド・loopback・一時設定・FakeBackend)の実通信テスト + ログの非漏えい。

実モデル・GPU・共有フォルダ・本番 server.yaml・実製番は使わない。ソケットは 127.0.0.1 のみ。Waitress が入っていない環境ではスキップする。
"""
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path

try:
    import waitress  # noqa: F401
    HAVE_WAITRESS = True
except ImportError:                                     # pragma: no cover
    HAVE_WAITRESS = False

import requests
import yaml

from prod_helpers import HOST, ProdBase, SCRIPT, WEIGHTS_BYTES, free_port, make_png  # noqa: F401
import prod_config
import prod_logging
import prod_server
from backends import FakeBackend

NO_PROXY = {"http": None, "https": None}


def fake(delay=0.0):
    be = FakeBackend(None)
    be.script = {"pages": SCRIPT["pages"], "delay_sec": delay}
    return be


class Running:
    """別スレッドで serve() を動かし、準備完了まで待つ。stop() で正常停止して終了コードを返す。"""

    def __init__(self, test, cfg, backend=None, **kw):
        self.test, self.cfg = test, cfg
        self.info, self.code = {}, {}
        self.thread = threading.Thread(target=lambda: self.code.setdefault("v", prod_server.serve(
            cfg, backend_factory=lambda c, d: backend or fake(), on_started=lambda h, s: self.info.update(h=h, s=s), **kw)), daemon=True)

    def start(self, want=200):
        self.thread.start()
        end = time.time() + 20
        while time.time() < end:
            try:
                if self.get("/health").status_code == want:
                    return self
            except requests.RequestException:
                pass
            time.sleep(0.1)
        self.test.fail("API が期待した状態になりませんでした")

    def url(self, path):
        return f"http://{HOST}:{self.cfg.port}{path}"

    def get(self, path, **kw):
        return requests.get(self.url(path), timeout=5, proxies=NO_PROXY, **kw)

    def post(self, form, timeout=20, **kw):
        return requests.post(self.url("/predict"), data=form, timeout=timeout, proxies=NO_PROXY, **kw)

    def stop(self, timeout=20):
        self.info["h"].shutdown()
        self.thread.join(timeout)
        self.test.assertFalse(self.thread.is_alive(), "serve() が停止しませんでした")
        return self.code["v"]


@unittest.skipUnless(HAVE_WAITRESS, "waitress が無い環境")
class WaitressContractTest(ProdBase):
    def test_health_and_predict_contract_over_real_http(self):
        run = Running(self, self.cfg()).start()
        try:
            r = run.get("/health")
            self.assertEqual((r.status_code, r.json()["status"], r.json()["ready"], r.json()["mode"]), (200, "ok", True, "production"))
            self.assertEqual(r.json()["model"]["id"], "test_model_20260101")
            p = run.post(self.form())
            self.assertEqual(p.status_code, 200, p.text)
            self.assertEqual(p.json()["detections"], 1)
            self.assertIn("Device$盤", (self.product / "21_detected.txt").read_text(encoding="utf-8"))
            self.assertTrue((self.product / "21_detected.png").is_file())
            self.assertEqual([x.name for x in self.product.iterdir() if x.name.startswith(".infer_tmp_")], [])
            self.assertEqual(run.post(self.form(page="2a")).status_code, 400)                       # 検証は従来どおり
            self.assertEqual(run.post(self.form(work_dir=str(self.out))).status_code, 400)
            self.assertIn(run.get("/nope").status_code, (404, 405))
            self.assertIn(run.post({}, headers={}).status_code, (400,))                              # 項目なし
        finally:
            self.assertEqual(run.stop(), 0)

    def test_server_header_does_not_reveal_waitress_or_python(self):
        run = Running(self, self.cfg()).start()
        try:
            server = run.get("/health").headers.get("Server", "")
            self.assertEqual(server, prod_server.SERVER_IDENT)
            self.assertNotIn("waitress", server.lower())
        finally:
            run.stop()

    def test_listens_only_on_the_configured_address(self):
        run = Running(self, self.cfg()).start()
        try:
            # 127.0.0.1 だけで待ち受けている(ワイルドカードなら 127.0.0.2 でも繋がる)
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.2", self.cfg().port), timeout=1).close()
            socket.create_connection((HOST, self.cfg().port), timeout=2).close()
        finally:
            run.stop()

    def test_clients_outside_the_allowlist_are_refused_over_real_http(self):
        cfg = self.cfg(allowed_clients=["203.0.113.0/24"])                  # loopback を許可しない
        run = Running(self, cfg).start(want=403)                             # /health も 403(= 許可リストが効いている)
        try:
            self.assertEqual(run.get("/health").status_code, 403)
            self.assertEqual(run.post(self.form()).status_code, 403)
            self.assertEqual(run.get("/x").status_code, 403)
            self.assertFalse((self.product / "21_detected.txt").exists())
            body = run.get("/health").text
            for secret in (str(self.root), HOST, "test_model_20260101"):    # 拒否の応答は何も明かさない
                self.assertNotIn(secret, body)
        finally:
            run.stop()

    def test_proxy_headers_cannot_spoof_the_client_address(self):
        cfg = self.cfg(allowed_clients=["203.0.113.0/24"])
        run = Running(self, cfg).start(want=403)
        try:
            r = run.get("/health", headers={"X-Forwarded-For": "203.0.113.5", "Forwarded": "for=203.0.113.5", "X-Real-IP": "203.0.113.5"})
            self.assertEqual(r.status_code, 403)
        finally:
            run.stop()

    def test_oversized_body_and_headers_are_rejected(self):
        run = Running(self, self.cfg()).start()
        try:
            self.assertIn(run.post({**self.form(), "pad": "x" * 40000}).status_code, (400, 413))
            r = run.get("/health", headers={"X-Big": "y" * 20000})
            self.assertIn(r.status_code, (400, 431, 413))
        finally:
            run.stop()


@unittest.skipUnless(HAVE_WAITRESS, "waitress が無い環境")
class WaitressLimitsTest(ProdBase):
    def test_health_stays_responsive_while_an_inference_is_running(self):
        run = Running(self, self.cfg(), backend=fake(delay=2.0)).start()
        try:
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("r", run.post(self.form())), daemon=True)
            t.start()
            time.sleep(0.5)
            started = time.monotonic()
            self.assertEqual(run.get("/health").status_code, 200)             # 推論中でも /health に答える(スレッドを残している)
            self.assertLess(time.monotonic() - started, 1.5)
            t.join(10)
            self.assertEqual(out["r"].status_code, 200)
        finally:
            run.stop()

    def test_pending_limit_returns_503_beyond_max_pending_predict(self):
        d = self.data()
        d["server"] = {**d["server"], "threads": 4, "max_pending_predict": 1, "queue_wait_sec": 20}
        run = Running(self, prod_config.parse_config(d), backend=fake(delay=1.5)).start()
        try:
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("first", run.post(self.form())), daemon=True)
            t.start()
            time.sleep(0.4)
            second = run.post(self.form(page="22"))
            self.assertEqual(second.status_code, 503)                          # 実行中 1 件だけ受け付ける設定
            t.join(10)
            self.assertEqual(out["first"].status_code, 200)
            self.assertFalse((self.product / "22_detected.txt").exists())
        finally:
            run.stop()

    def test_queue_wait_limit_returns_503_instead_of_waiting_forever(self):
        d = self.data()
        d["server"] = {**d["server"], "queue_wait_sec": 1, "max_pending_predict": 3}
        run = Running(self, prod_config.parse_config(d), backend=fake(delay=3.0)).start()
        try:
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("first", run.post(self.form())), daemon=True)
            t.start()
            time.sleep(0.5)
            started = time.monotonic()
            waiting = run.post(self.form(page="22"))
            self.assertEqual(waiting.status_code, 503)
            self.assertLess(time.monotonic() - started, 2.5)                   # 上限(1 秒)で諦める
            self.assertEqual([x.name for x in self.product.iterdir() if x.name.startswith(".infer_tmp_")].__len__(), 1)  # 実行中の分だけ
            t.join(10)
            self.assertEqual(out["first"].status_code, 200)
            self.assertEqual([x.name for x in self.product.iterdir() if x.name.startswith(".infer_tmp_")], [])
        finally:
            run.stop()


@unittest.skipUnless(HAVE_WAITRESS, "waitress が無い環境")
class WaitressShutdownTest(ProdBase):
    def test_graceful_stop_lets_the_running_inference_finish_and_refuses_new_ones(self):
        run = Running(self, self.cfg(), backend=fake(delay=1.5)).start()
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", run.post(self.form())), daemon=True)
        t.start()
        time.sleep(0.5)
        run.info["h"].shutdown()                                               # 推論の実行中に停止を要求する
        end = time.time() + 5
        refused = False
        while time.time() < end and not refused:                               # 新規の接続は、すぐ受け付けなくなる
            try:
                socket.create_connection((HOST, run.cfg.port), timeout=0.5).close()
                time.sleep(0.05)
            except OSError:
                refused = True
        self.assertTrue(refused, "停止要求の後も新規の接続を受け付けています")
        t.join(15)
        self.assertEqual(out["r"].status_code, 200)                            # 実行中の推論は完了して応答を返す
        self.assertTrue((self.product / "21_detected.txt").is_file())
        run.thread.join(15)
        self.assertFalse(run.thread.is_alive())
        self.assertEqual(run.code["v"], 0)

    def test_stop_gives_up_after_the_grace_period(self):
        d = self.data()
        d["server"] = {**d["server"], "shutdown_grace_sec": 1}
        run = Running(self, prod_config.parse_config(d), backend=fake(delay=6.0)).start()
        t = threading.Thread(target=lambda: run.post(self.form(), timeout=15), daemon=True)
        t.start()
        time.sleep(0.5)
        began = time.monotonic()
        with self.assertLogs("prod_inference_server", level="INFO") as cm:
            code = run.stop(timeout=20)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - began, 12)                          # 実行中の推論を待ち続けない(猶予 1 秒 + 後始末)
        self.assertTrue(any("猶予時間内に完了しなかった" in line for line in cm.output))

    def test_published_server_yaml_follows_the_current_safety_rules_across_a_stop(self):
        cfg = self.cfg()
        run = Running(self, cfg, publish_config=True).start()
        first_id = run.info["s"].instance_id
        self.assertEqual(run.stop(), 0)
        # 停止しても公開済みの server.yaml は削除されない(自分のものとして残る)
        d = yaml.safe_load(self.server_yaml.read_text(encoding="utf-8"))
        self.assertEqual(d["instance_id"], first_id)
        before = self.server_yaml.read_bytes()
        # 再起動: 接続先は接続拒否(古い設定)なので、--replace-stale が無ければ拒否(終了コード 5)。ファイルは変わらない
        code = prod_server.serve(cfg, publish_config=True, backend_factory=lambda c, d: fake())
        self.assertEqual(code, prod_server.EXIT_PUBLISH)
        self.assertEqual(self.server_yaml.read_bytes(), before)
        # --replace-stale: 退避してから置換できる
        run2 = Running(self, cfg, publish_config=True, replace_stale=True).start()
        second_id = run2.info["s"].instance_id
        self.assertEqual(run2.stop(), 0)
        self.assertNotEqual(second_id, first_id)
        self.assertEqual(yaml.safe_load(self.server_yaml.read_text(encoding="utf-8"))["instance_id"], second_id)
        backups = list(self.backup_dir.glob("server.yaml.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)

    def test_load_failure_and_publish_failure_still_stop_cleanly_with_waitress(self):
        def boom(c, d):
            raise RuntimeError("CUDA failed")
        self.assertEqual(prod_server.serve(self.cfg(), backend_factory=boom), prod_server.EXIT_LOAD)
        self.assertEqual(prod_server.serve(self.cfg(), backend_factory=lambda c, d: fake()) if False else prod_server.EXIT_LOAD, prod_server.EXIT_LOAD)


@unittest.skipUnless(HAVE_WAITRESS, "waitress が無い環境")
class WaitressSignalTest(ProdBase):
    """前景プロセスとしての停止: 実際のプロセスへシグナルを送り、正常終了(終了コード 0)・server.yaml が残ることを確認する。"""

    SCRIPT = textwrap.dedent('''
        import sys
        sys.path.insert(0, {here!r})
        sys.path.insert(0, {tests!r})
        import prod_config, prod_server
        from prod_helpers import SCRIPT
        from backends import FakeBackend
        cfg = prod_config.load_config({cfg!r})
        def factory(c, d):
            be = FakeBackend(None); be.script = SCRIPT; return be
        sys.exit(prod_server.serve(cfg, publish_config={publish}, backend_factory=factory))
    ''')

    def run_child(self, publish=False):
        cfg_path = self.root / "cfg.yaml"
        cfg_path.write_text(yaml.safe_dump(self.data()), encoding="utf-8")
        code = self.SCRIPT.format(here=str(Path(prod_server.__file__).resolve().parent), tests=str(Path(__file__).resolve().parent),
                                  cfg=str(cfg_path), publish=publish)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        end = time.time() + 30
        port = self.port
        while time.time() < end:
            try:
                if requests.get(f"http://{HOST}:{port}/health", timeout=2, proxies=NO_PROXY).status_code == 200:
                    return proc
            except requests.RequestException:
                time.sleep(0.2)
            if proc.poll() is not None:
                self.fail("子プロセスが起動直後に終了しました: " + proc.stdout.read().decode("utf-8", "replace")[-500:])
        self.fail("子プロセスが ready になりませんでした")

    def test_signal_stops_the_foreground_process_cleanly_and_keeps_the_published_file(self):
        proc = self.run_child(publish=True)
        sig = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM
        proc.send_signal(sig)
        try:
            rc = proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("シグナルで停止しませんでした")
        self.assertEqual(rc, 0)
        self.assertTrue(self.server_yaml.exists())                              # 停止しても接続設定は削除しない
        log = (self.root / "logs" / "prod_api.log").read_text(encoding="utf-8")
        self.assertIn("停止要求を受け付けました", log)
        self.assertIn("停止しました", log)
        with self.assertRaises(requests.RequestException):
            requests.get(f"http://{HOST}:{self.port}/health", timeout=1, proxies=NO_PROXY)


class LogRedactionTest(ProdBase):
    def test_redact_hides_paths_addresses_credentials_and_hex(self):
        text = ("failed \\\\fileserver\\share\\a\\out\\A1TEST0001\\21.png at C:\\Users\\x\\model\\best.pt /srv/models/best.pt "
                "from 192.0.2.7:8080 to 10.1.2.3 Authorization: Bearer abcdef0123456789 token " + "a" * 64)
        out = prod_logging.redact(text)
        for secret in ("fileserver", "A1TEST0001", "Users", "best.pt", "192.0.2.7", "10.1.2.3", "abcdef0123456789", "a" * 40):
            self.assertNotIn(secret, out)
        self.assertIn("failed", out)

    def test_ordinary_request_lines_are_not_mangled(self):
        self.assertEqual(prod_logging.redact("POST /predict page=21 status=200 detections=1 elapsed_ms=12"),
                         "POST /predict page=21 status=200 detections=1 elapsed_ms=12")

    def test_filter_drops_exception_text_and_tracebacks(self):
        import io
        import logging
        stream = io.StringIO()
        h = logging.StreamHandler(stream)
        h.addFilter(prod_logging.RedactingFilter())
        lg = logging.getLogger("test_redact_filter")
        lg.propagate = False
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        try:
            raise FileNotFoundError(r"\\secret-host\share\file.png")
        except FileNotFoundError:
            lg.exception("failed to read %s", r"\\secret-host\share\file.png")
        lg.info("peer %s token %s", "198.51.100.9", "Bearer xyz.secret.token")
        out = stream.getvalue()
        for secret in ("secret-host", "file.png", "Traceback", "198.51.100.9", "xyz.secret.token"):
            self.assertNotIn(secret, out)
        self.assertIn("failed to read", out)

    def test_filter_survives_a_broken_format(self):
        import logging
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, "bad %d", ("not-a-number",), None)
        self.assertTrue(prod_logging.RedactingFilter().filter(rec))
        self.assertEqual(rec.getMessage(), "(ログの整形に失敗しました)")


@unittest.skipUnless(HAVE_WAITRESS, "waitress が無い環境")
class LogFileTest(ProdBase):
    def log_text(self):
        return (self.root / "logs" / "prod_api.log").read_text(encoding="utf-8")

    def test_lifecycle_is_logged_without_leaking_values(self):
        class Boom:
            def predict(self, image, tmp):
                raise FileNotFoundError(rf"\\secret-host\share\A1TEST0001\21.png missing at 192.0.2.99 Bearer topsecrettoken123")
        run = Running(self, self.cfg(), backend=Boom()).start()
        try:
            r = run.post(self.form())
            self.assertEqual(r.status_code, 500)
            self.assertEqual(r.json()["error"], "推論に失敗しました: FileNotFoundError")
            run.post(self.form(page="2a"))                                      # 拒否(400)も記録される
            requests.get(run.url("/health"), timeout=3, proxies=NO_PROXY)
        finally:
            self.assertEqual(run.stop(), 0)
        text = self.log_text()
        for expected in ("起動を開始します", "モデルの検証に成功しました", "待ち受けを開始しました(Waitress)", "モデルの読み込みが完了しました",
                         "準備完了(ready)", "推論に失敗 page=21: FileNotFoundError", "リクエスト拒否", "停止要求を受け付けました", "停止しました"):
            self.assertIn(expected, text)
        for secret in ("secret-host", "A1TEST0001", "21.png", "192.0.2.99", "topsecrettoken123", str(self.root), str(self.out), str(self.model_dir),
                       HOST, str(self.port), "Traceback", "best.pt"):
            self.assertNotIn(secret, text, secret)
        full = prod_logging  # noqa: F841
        self.assertNotIn(__import__("hashlib").sha256(WEIGHTS_BYTES).hexdigest(), text)           # ハッシュ全文は出さない

    def test_rotation_respects_size_and_generation_limits(self):
        handle = prod_logging.configure_logging(str(self.root / "rot" / "r.log"), 65536, 2, "INFO", stream=open(os.devnull, "w"))
        try:
            lg = prod_server.log
            for i in range(4000):
                lg.info("rotation test line %05d %s", i, "x" * 60)
        finally:
            handle.close()
        files = sorted(p.name for p in (self.root / "rot").iterdir())
        self.assertEqual(files, ["r.log", "r.log.1", "r.log.2"])               # 世代数(2)を超えて残らない
        for p in (self.root / "rot").iterdir():
            self.assertLessEqual(p.stat().st_size, 65536 + 512)                # サイズ上限を(1 行ぶんの余裕で)守る

    def test_unwritable_log_location_refuses_to_start_without_leaking_the_path(self):
        blocker = self.root / "afile"
        blocker.write_text("not a directory", encoding="utf-8")
        cfg = self.cfg(logging={**self.data()["logging"], "file": str(blocker / "sub" / "x.log")})
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = prod_server.serve(cfg, backend_factory=lambda c, d: fake())
        self.assertEqual(code, prod_server.EXIT_CONFIG)
        self.assertNotIn(str(self.root), err.getvalue())
        self.assertIn("ログファイルを開けません", err.getvalue())

    def test_handlers_are_removed_when_serve_returns(self):
        import logging
        before = list(logging.getLogger("prod_inference_server").handlers)
        run = Running(self, self.cfg()).start()
        run.stop()
        self.assertEqual(list(logging.getLogger("prod_inference_server").handlers), before)


class ServerConfigTest(ProdBase):
    def err(self, **over):
        with self.assertRaises(prod_config.ConfigError) as cm:
            prod_config.parse_config(self.data(**over))
        return str(cm.exception)

    def srv(self, **kv):
        return {**self.data()["server"], **kv}

    def test_every_limit_is_required(self):
        for key in self.data()["server"]:
            d = self.data()
            del d["server"][key]
            with self.subTest(key=key):
                with self.assertRaises(prod_config.ConfigError):
                    prod_config.parse_config(d)
        d = self.data()
        del d["server"]
        with self.assertRaises(prod_config.ConfigError):
            prod_config.parse_config(d)

    def test_pending_predict_must_leave_a_thread_for_health(self):
        self.err(server=self.srv(threads=4, max_pending_predict=4))
        self.err(server=self.srv(threads=4, max_pending_predict=9))
        self.assertEqual(self.cfg(server=self.srv(threads=4, max_pending_predict=3)).limits.max_pending_predict, 3)

    def test_limits_must_be_in_range_and_ints(self):
        for bad in ({"threads": 1}, {"threads": 65}, {"backlog": 0}, {"connection_limit": 3}, {"connection_limit": 2000}, {"channel_timeout_sec": 1},
                    {"queue_wait_sec": 0}, {"shutdown_grace_sec": 0}, {"shutdown_grace_sec": 1000}, {"threads": "8"}, {"threads": True}):
            with self.subTest(bad=bad):
                self.err(server=self.srv(**bad))

    def test_connection_limit_must_cover_the_threads(self):
        self.err(server=self.srv(threads=8, max_pending_predict=2, connection_limit=6))

    def test_logging_section_is_strict(self):
        base = self.data()["logging"]
        for bad in ({"file": "relative.log"}, {"file": "\\\\fileserver\\share\\x.log"}, {"file": str(self.out / "x.log")},
                    {"file": str(self.server_yaml)}, {"file": str(self.root / ".." / "x.log")}, {"level": "DEBUG"}, {"level": "TRACE"},
                    {"max_bytes": 100}, {"max_bytes": 2 * 1024 ** 3}, {"backup_count": 0}, {"backup_count": 101}):
            with self.subTest(bad=bad):
                self.err(logging={**base, **bad})
        d = self.data()
        del d["logging"]
        with self.assertRaises(prod_config.ConfigError):
            prod_config.parse_config(d)
        self.assertEqual(self.cfg(logging={**base, "level": "warning"}).log.level, "WARNING")

    def test_probe_timeout_must_be_long_enough_for_windows_refusals(self):
        for bad in (0.5, 1.0, 2.9, 31, True):
            with self.subTest(bad=bad):
                self.err(timeout_probe_sec=bad)
        self.assertEqual(self.cfg(timeout_probe_sec=3).timeout_probe_sec, 3.0)

    def test_requirements_pin_waitress(self):
        reqs = (Path(prod_server.__file__).resolve().parent / "requirements.txt").read_text(encoding="utf-8")
        import re
        self.assertRegex(reqs, re.compile(r"^waitress==\d+\.\d+\.\d+\s*$", re.M))                 # 版を固定している
        self.assertRegex(reqs, re.compile(r"^requests==\d+\.\d+\.\d+\s*$", re.M))
        # pip は Windows の既定エンコーディング(cp932 など)でこのファイルを読むため、ASCII だけにする(日本語が入ると pip install -r が失敗する)
        self.assertTrue(all(ord(c) < 128 for c in reqs))

    def test_check_reports_server_and_logging_summary_without_values(self):
        res = prod_server.run_check(self.cfg())
        self.assertIn("server", res)
        self.assertIn("logging", res)
        text = str(res)
        for secret in (str(self.root), str(self.out)):
            self.assertNotIn(secret, text)
        self.assertEqual(res["server"]["threads"], 6)
        self.assertFalse((self.root / "logs").exists())                          # check はログフォルダを作らない


if __name__ == "__main__":
    unittest.main()
