"""本番推論 API: Waitress 経由(別スレッド・loopback・一時設定・FakeBackend)の実通信テスト + ログの非漏えい。

実モデル・GPU・共有フォルダ・本番 server.yaml・実製番は使わない。ソケットは 127.0.0.1 のみ。Waitress が入っていない環境ではスキップする。
"""
import os
import signal
import socket
import subprocess
import sys
import io
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

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
import prod_publish
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


def wait_inflight(run, n=1, timeout=10):
    """実行中の推論が n 件に達するまで待つ(sleep の固定時間に頼らない)。"""
    end = time.time() + timeout
    while time.time() < end:
        if run.info["s"].inflight >= n:
            return
        time.sleep(0.02)
    raise AssertionError("推論が始まりませんでした")


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
            try:                    # 上限超過の本文は、サーバーが応答を返す前に接続を閉じることがある(Windows の接続中断)。どちらも「拒否された」
                self.assertIn(run.post({**self.form(), "pad": "x" * 40000}).status_code, (400, 413))
            except requests.ConnectionError:
                pass
            try:
                r = run.get("/health", headers={"X-Big": "y" * 20000})
                self.assertIn(r.status_code, (400, 431, 413))
            except requests.ConnectionError:
                pass
            self.assertEqual(run.get("/health").status_code, 200)       # 拒否の後も、サーバーは正常に応答する
        finally:
            run.stop()


@unittest.skipUnless(HAVE_WAITRESS, "waitress が無い環境")
class WaitressLimitsTest(ProdBase):
    def test_health_stays_responsive_while_an_inference_is_running(self):
        run = Running(self, self.cfg(), backend=fake(delay=4.0)).start()
        try:
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("r", run.post(self.form())), daemon=True)
            t.start()
            wait_inflight(run)
            self.assertEqual(run.get("/health").status_code, 200)             # 推論中でも /health に答える(スレッドを残している)
            self.assertTrue(t.is_alive(), "/health に答えた時点で、推論はまだ実行中のはず")
            t.join(15)
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
            wait_inflight(run)
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
            wait_inflight(run)
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
        wait_inflight(run)
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
        wait_inflight(run)
        began = time.monotonic()
        code = run.stop(timeout=20)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - began, 12)                          # 実行中の推論を待ち続けない(猶予 1 秒 + 後始末)
        self.assertIn("猶予時間内に完了しなかった", (self.root / "logs" / "prod_api.log").read_text(encoding="utf-8"))

    def test_second_signal_stops_without_waiting_for_the_running_inference(self):
        d = self.data()
        d["server"] = {**d["server"], "shutdown_grace_sec": 120}
        run = Running(self, prod_config.parse_config(d), backend=fake(delay=10.0)).start()
        t = threading.Thread(target=lambda: run.post(self.form(), timeout=20), daemon=True)
        t.start()
        wait_inflight(run)
        began = time.monotonic()
        run.info["h"].signal()
        time.sleep(0.3)
        self.assertTrue(run.thread.is_alive(), "1 回目は猶予まで待つ")
        run.info["h"].signal()                                                  # 2 回目: 猶予が長くても、待たずに止まる
        run.thread.join(8)
        self.assertFalse(run.thread.is_alive())
        self.assertLess(time.monotonic() - began, 8)
        self.assertEqual(run.code["v"], 0)
        self.assertIn("2 回目の停止要求", (self.root / "logs" / "prod_api.log").read_text(encoding="utf-8"))

    def test_keep_alive_health_reports_stopping_while_draining(self):
        run = Running(self, self.cfg(), backend=fake(delay=3.0)).start()
        session = requests.Session()
        session.trust_env = False
        self.addCleanup(session.close)
        self.assertEqual(session.get(run.url("/health"), timeout=5).status_code, 200)    # keep-alive の接続を確立する
        t = threading.Thread(target=lambda: run.post(self.form()), daemon=True)
        t.start()
        wait_inflight(run)
        run.info["h"].shutdown()
        status, end = None, time.time() + 5
        while time.time() < end and status != "stopping":                      # 既存の接続には、停止中であることを返す(新規の接続は拒否される)
            try:
                r = session.get(run.url("/health"), timeout=2)
                status = r.json()["status"] if r.status_code == 503 else None
            except requests.RequestException:
                break
            time.sleep(0.05)
        self.assertEqual(status, "stopping")
        t.join(10)
        run.thread.join(10)
        self.assertEqual(run.code["v"], 0)

    def test_stop_while_loading_does_not_publish_or_become_ready(self):
        gate = threading.Event()
        cfg = self.cfg()

        def slow_factory(c, d):
            gate.wait(10)
            return fake()
        info, code = {}, {}
        th = threading.Thread(target=lambda: code.setdefault("v", prod_server.serve(
            cfg, publish_config=True, backend_factory=slow_factory, on_started=lambda h, s: info.update(h=h, s=s))), daemon=True)
        th.start()
        end = time.time() + 20
        while time.time() < end and not info:
            time.sleep(0.02)
        self.assertTrue(info, "起動しませんでした")
        info["h"].shutdown()                                                    # モデルの読み込み中に停止を要求する
        gate.set()                                                              # その後で、読み込みが終わる
        th.join(20)
        self.assertFalse(th.is_alive())
        self.assertEqual(code["v"], 0)
        self.assertFalse(info["s"].ready)
        self.assertFalse(self.server_yaml.exists())                             # 停止要求の後で、接続設定を公開しない
        self.assertIn("公開と ready を行いません", (self.root / "logs" / "prod_api.log").read_text(encoding="utf-8"))

    def test_try_begin_is_refused_once_stopping(self):
        import types
        st = prod_server.ProdState(self.cfg(), "x" * 32, types.SimpleNamespace(public=lambda: {}))
        self.assertTrue(st.try_begin())
        st.begin_stop()
        self.assertFalse(st.try_begin())                                        # 停止処理の後に、新しい推論を登録しない
        self.assertEqual(st.inflight, 1)

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

    def test_a_normal_start_without_the_publish_option_never_touches_an_existing_server_yaml(self):
        # prod_ctl start は、公開オプションが無ければ `serve`(--publish-config なし)を起動する。その通常起動・稼働・停止で、共有の server.yaml を変えない
        self.server_yaml.write_text("host:\n  ip: 10.0.0.9\n  pc_ip: 10.0.0.9\n  port: 7108\nproxy:\n  host: proxy.example\n  port: 80\n", encoding="utf-8")
        before, tree = self.server_yaml.read_bytes(), sorted(p.name for p in self.pub_dir.iterdir())
        mtime = self.server_yaml.stat().st_mtime_ns
        run = Running(self, self.cfg()).start()
        self.assertEqual(run.get("/health").status_code, 200)
        self.assertEqual(run.stop(), 0)
        self.assertEqual(self.server_yaml.read_bytes(), before)
        self.assertEqual(self.server_yaml.stat().st_mtime_ns, mtime)                            # 書き換えも(同じ内容での上書きも)していない
        self.assertEqual(sorted(p.name for p in self.pub_dir.iterdir()), tree)                  # ロック・一時ファイルも作らない
        self.assertFalse(self.backup_dir.exists() and any(self.backup_dir.iterdir()))           # 退避も作らない

    def test_load_failure_and_publish_failure_still_stop_cleanly_with_waitress(self):
        def boom(c, d):
            raise RuntimeError("CUDA failed")
        self.assertEqual(prod_server.serve(self.cfg(), backend_factory=boom), prod_server.EXIT_LOAD)
        with mock.patch.object(prod_publish, "publish", side_effect=prod_publish.PublishRefused("changed", "固定文")):
            self.assertEqual(prod_server.serve(self.cfg(), publish_config=True, backend_factory=lambda c, d: fake()), prod_server.EXIT_PUBLISH)
        with mock.patch.object(prod_publish, "publish", side_effect=RuntimeError(r"\\secret-host\share")):
            self.assertEqual(prod_server.serve(self.cfg(), publish_config=True, backend_factory=lambda c, d: fake()), prod_server.EXIT_PUBLISH)
        self.assertNotIn("secret-host", (self.root / "logs" / "prod_api.log").read_text(encoding="utf-8"))
        self.assertFalse(self.server_yaml.exists())


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

    def test_control_characters_are_replaced_and_long_lines_are_truncated(self):
        out = prod_logging.redact("GET /x\r\n2026-01-01 00:00:00 INFO forged line\x00\x1b[31m")
        for ch in ("\r", "\n", "\x00", "\x1b"):
            self.assertNotIn(ch, out)
        long = prod_logging.redact("a" * 5000)
        self.assertLessEqual(len(long), prod_logging.MAX_LINE + 20)

    def test_hex_and_addresses_next_to_underscores_are_hidden(self):
        out = prod_logging.redact("sha_" + "ab" * 20 + "_x peer_192.0.2.7_y D:\\x\\y")
        self.assertNotIn("ab" * 20, out)
        self.assertNotIn("192.0.2.7", out)

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
        self.assertNotIn(__import__("hashlib").sha256(WEIGHTS_BYTES).hexdigest(), text)           # ハッシュ全文は出さない

    def test_rotation_respects_size_and_generation_limits(self):
        handle = prod_logging.configure_logging(str(self.root / "rot" / "r.log"), 65536, 2, "INFO", stream=io.StringIO())
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

    def test_rotation_failure_prints_one_fixed_line_without_the_path(self):
        import contextlib
        target = self.root / "rotfail" / "r.log"
        handle = prod_logging.configure_logging(str(target), 65536, 2, "INFO", stream=io.StringIO())
        err = io.StringIO()
        try:
            with mock.patch("os.rename", side_effect=PermissionError(13, "locked", str(target))), contextlib.redirect_stderr(err):
                for i in range(1500):
                    prod_server.log.info("rotation failure line %05d %s", i, "x" * 60)
        finally:
            handle.close()
        text = err.getvalue()
        self.assertIn(prod_logging.LOG_ERROR_NOTICE, text)
        self.assertEqual(text.count(prod_logging.LOG_ERROR_NOTICE), 1)             # 洪水にしない(一定間隔に 1 回)
        for secret in (str(self.root), "rotfail", "Traceback", "PermissionError"):
            self.assertNotIn(secret, text)

    def test_denied_clients_cannot_forge_or_flood_the_log(self):
        cfg = self.cfg(allowed_clients=["203.0.113.0/24"])                       # loopback を許可しない
        run = Running(self, cfg).start(want=403)
        try:
            for i in range(30):
                s = socket.create_connection((HOST, cfg.port), timeout=3)
                s.sendall(b"GET /%0d%0a2026-01-01%2000:00:00%20INFO%20forged%20line%20" + b"A" * 3000 + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                s.recv(4096)
                s.close()
            for i in range(30):
                run.get("/health")
        finally:
            run.stop()
        text = self.log_text()
        self.assertNotIn("forged", text)
        self.assertNotIn("AAAA", text)
        denied = [ln for ln in text.splitlines() if "許可リストに無いため拒否" in ln]
        self.assertLessEqual(len(denied), 2)                                      # 60 件の拒否が、数行にまとまる
        self.assertGreaterEqual(len(denied), 1)
        for ln in text.splitlines():
            self.assertRegex(ln, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (INFO|WARNING|ERROR) ")     # 偽造された行・分断された行がない
            self.assertLessEqual(len(ln), 700)

    def test_unhandled_flask_error_goes_to_the_log_without_traceback_or_values(self):
        run = Running(self, self.cfg()).start()
        try:
            with mock.patch.object(prod_config, "client_allowed", side_effect=RuntimeError(r"\\secret-host\share\A1TEST0001")):
                self.assertEqual(run.get("/health").status_code, 500)
        finally:
            run.stop()
        text = self.log_text()
        for secret in ("secret-host", "A1TEST0001", "Traceback"):
            self.assertNotIn(secret, text)

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
        run = Running(self, self.cfg()).start()
        run.stop()
        for name in ("prod_inference_server", "waitress", "prod_server", "werkzeug"):
            ours = [h for h in logging.getLogger(name).handlers if any(isinstance(f, prod_logging.RedactingFilter) for f in h.filters)]
            self.assertEqual(ours, [], name)


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

    def test_connection_limit_must_cover_the_threads_and_waitress_internal_sockets(self):
        self.err(server=self.srv(threads=8, max_pending_predict=2, connection_limit=6))
        self.err(server=self.srv(threads=8, max_pending_predict=4, connection_limit=8))     # /health が飢える構成(実効は 8-2)
        self.err(server=self.srv(threads=8, max_pending_predict=4, connection_limit=9))
        self.assertEqual(self.cfg(server=self.srv(threads=8, max_pending_predict=4, connection_limit=10)).limits.connection_limit, 10)

    def test_logging_section_is_strict(self):
        base = self.data()["logging"]
        for bad in ({"file": "relative.log"}, {"file": "\\\\fileserver\\share\\x.log"}, {"file": str(self.out / "x.log")},
                    {"file": str(self.server_yaml)}, {"file": str(self.root / ".." / "x.log")},
                    {"file": str(self.backup_dir / "x.log")}, {"file": str(self.model_dir / "x.log")}, {"level": "DEBUG"}, {"level": "TRACE"},
                    {"max_bytes": 100}, {"max_bytes": 2 * 1024 ** 3}, {"backup_count": 0}, {"backup_count": 101}):
            with self.subTest(bad=bad):
                self.err(logging={**base, **bad})
        d = self.data()
        del d["logging"]
        with self.assertRaises(prod_config.ConfigError):
            prod_config.parse_config(d)
        self.assertEqual(self.cfg(logging={**base, "level": "warning"}).log.level, "WARNING")

    @unittest.skipUnless(os.name == "nt", "Windows のドライブ無しの絶対パス")
    def test_logging_file_must_have_a_drive_on_windows(self):
        self.err(logging={**self.data()["logging"], "file": "\\logs\\x.log"})

    def test_probe_timeout_must_be_long_enough_for_windows_refusals(self):
        # 判定不能(タイムアウト)を「古い設定(接続拒否)」と誤らないため、下限は 3 秒(§6.5 の運用ログ・停止とは別の、Windows の接続拒否の遅延への対応)
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
