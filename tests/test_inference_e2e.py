# 推論サーバー統合のテスト(実プロセス。別venvのサーバーを loopback・検証専用ポート・一時ディレクトリで起動する)
# - モデルは FakeBackend に差し替え(GPU・重い依存関係・外部通信なし)。通信は 127.0.0.1 のみ。
# - IJCAD・ホスト照会・設計サーバー・DWG変換はモックのまま(test_main_integration と同じ)。
import builtins
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ["SEKISAN_PRODUCTION_HOSTS"] = "prod-host-example"  # 本番ホスト名はリポジトリに書かない。テスト用の架空ホストを本番扱いにする
import app  # noqa: E402
import inference_support as isup  # noqa: E402
import test_main_integration as T  # noqa: E402  (モジュールとして参照。テストクラスは再登録しない)

INFER_PY = ROOT / "inference_server" / ".venv" / "Scripts" / "python.exe"
PRODUCTION_ROOT = r"\\prod-host-example\ShareData\exampleroot\a_product"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         encoding="cp932", errors="ignore").stdout
    return f'"{pid}"' in out


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_until(pred, timeout=15.0, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


# CAD座標(4200x2970 の図枠)での検出位置 -> 画像に対する比率
def frac(cx, cy, w=4200, h=2970):
    return {"cx": cx / w, "cy": (h - cy) / h, "w": 0.01, "h": 0.01}


FAKE_SCRIPT = {"pages": {
    "21": [{"name": "panel", "score": 0.9, **frac(900, 1500)}, {"name": "panel", "score": 0.9, **frac(3900, 1500)}],
    "22": [{"name": "transformer", "score": 0.9, **frac(2500, 1200)}, {"name": "transformer", "score": 0.9, **frac(3000, 2000)},
           {"name": "transformer", "score": 0.9, **frac(3900, 1200)}],
    "23": []}}


@unittest.skipUnless(INFER_PY.is_file(), "推論サーバー用の別venvがありません(inference_server/.venv)")
class ManagerTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="infer_mgr_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(os.path.realpath(td.name))
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.script = self.tmp / "fake.json"
        self.script.write_text(json.dumps(FAKE_SCRIPT), encoding="utf-8")
        self.port = free_port()
        self.managers = []
        self.addCleanup(lambda: [m.stop() for m in self.managers])

    def make(self, **kw):
        cfg = dict(enabled=True, port=self.port, backend="fake", fake_script=str(self.script), startup_timeout=60, stop_timeout=10)
        cfg.update(kw)
        s = isup.InferenceServerSettings.from_config(cfg)
        m = isup.InferenceServerManager(s, str(self.root), str(self.root / "api" / "server.yaml"), str(self.root / "logs"))
        self.managers.append(m)
        return m

    def test_start_ready_and_stop_only_own_child(self):
        # 無関係の別プロセス(同じ python)。停止処理で巻き込まれてはいけない。
        bystander = subprocess.Popen([str(INFER_PY), "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (bystander.kill(), bystander.wait(5)))
        m = self.make()
        m.start()
        pid = m.proc.pid
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"{m.base_url}/health", timeout=3) as r:
            body = json.loads(r.read())
        self.assertEqual((body["ready"], body["instance_id"], body["backend"]), (True, m.instance_id, "fake"))
        cfg = yaml.safe_load((self.root / "api" / "server.yaml").read_text(encoding="utf-8"))
        self.assertEqual((cfg["host"]["ip"], cfg["host"]["port"], cfg["mode"]), ("127.0.0.1", self.port, "test"))
        self.assertNotIn("proxy", cfg)
        self.assertEqual(Path(cfg["directory"]["output"]), self.root)
        m.stop()
        self.assertEqual(m.returncode, 0)                          # 正常停止(強制終了ではない)
        self.assertTrue(wait_until(lambda: not pid_alive(pid)))
        self.assertFalse(port_open(self.port))
        self.assertFalse((self.root / "api" / "server.yaml").exists())  # 自分の設定ファイルは片付ける
        self.assertIsNone(bystander.poll(), "無関係のプロセスを停止してはいけない")
        self.assertTrue((self.root / "logs" / f"inference_server_{self.port}.log").is_file())

    def test_context_manager_and_double_stop(self):
        with self.make() as m:
            self.assertIsNone(m.proc.poll())
        m.stop()  # 2回目は何もしない
        self.assertFalse(port_open(self.port))

    def test_not_ready_backend_failure_reports_cause_and_leaves_no_child(self):
        model_dir = self.tmp / "model"
        model_dir.mkdir()
        m = self.make(backend="sahi", model_dir=str(model_dir), fake_script=None)
        with self.assertRaises(isup.InferenceServerError) as cm:
            m.start()
        self.assertIn("準備に失敗", str(cm.exception))             # 原因(バックエンドの読み込み失敗)が分かる
        self.assertIsNone(m.proc)
        self.assertFalse(wait_until(lambda: port_open(self.port), timeout=2))

    def test_startup_timeout_stops_child(self):
        self.script.write_text(json.dumps({**FAKE_SCRIPT, "load_delay_sec": 30}), encoding="utf-8")
        m = self.make(startup_timeout=2)
        with self.assertRaises(isup.InferenceServerError) as cm:
            m.start()
        self.assertIn("2 秒以内に準備完了しません", str(cm.exception))
        self.assertIn("準備中(status=503)", str(cm.exception))      # 最後の状態(読み込み中)が原因として分かる
        self.assertIsNone(m.proc)
        self.assertTrue(wait_until(lambda: not port_open(self.port)))

    def test_port_in_use_is_refused_without_spawning(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", self.port))
            s.listen(1)
            with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("起動されないはず")):
                with self.assertRaises(isup.InferenceServerError) as cm:
                    self.make().start()
        self.assertIn("既に使用中", str(cm.exception))

    def test_unsafe_settings_are_refused_before_any_write_or_spawn(self):
        cases = [
            dict(output_root=PRODUCTION_ROOT, config_out=os.path.join(PRODUCTION_ROOT, "server.yaml")),
            dict(output_root=str(self.root), config_out=PRODUCTION_ROOT + r"\server.yaml"),
            dict(output_root=str(self.root), config_out=str(self.tmp / "elsewhere" / "server.yaml")),
        ]
        for c in cases:
            s = isup.InferenceServerSettings.from_config(dict(port=self.port, backend="fake", fake_script=str(self.script)))
            m = isup.InferenceServerManager(s, c["output_root"], c["config_out"], str(self.root / "logs"))
            with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("起動されないはず")), \
                    mock.patch.object(builtins, "open", side_effect=AssertionError("書き込まれないはず")):
                with self.assertRaises(isup.InferenceServerError, msg=str(c)):
                    m.start()
        for bad_port in (0, 80, None):
            s = isup.InferenceServerSettings.from_config(dict(port=bad_port, backend="fake"))
            with self.assertRaises(isup.InferenceServerError):
                isup.InferenceServerManager(s, str(self.root), str(self.root / "api" / "server.yaml"), str(self.root / "logs")).start()

    def test_missing_python_or_script_reports_cause(self):
        m = self.make(python=str(self.tmp / "nope" / "python.exe"))
        with self.assertRaises(isup.InferenceServerError) as cm:
            m.start()
        self.assertIn("別venv", str(cm.exception))

    def test_verify_shared_work_dir(self):
        work = self.root / "A1BN0001"
        work.mkdir()
        m = self.make()
        m.start()
        isup.verify_shared_work_dir(m.base_url, str(work))
        self.assertEqual([p.name for p in work.iterdir()], [])        # 目印ファイルは片付ける
        outside = self.tmp / "outside"
        outside.mkdir()
        with self.assertRaises(isup.InferenceServerError) as cm:
            isup.verify_shared_work_dir(m.base_url, str(outside))      # 出力ルート外は共有確認も拒否
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertEqual([p.name for p in outside.iterdir()], [])

    def test_child_exits_when_parent_dies(self):
        # 親(データ作成側)が強制終了しても、推論サーバーが残らない(--parent-pid の監視)
        helper = self.tmp / "helper.py"
        pidfile = self.tmp / "child.pid"
        helper.write_text(textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(ROOT)!r})
            import inference_support as isup
            s = isup.InferenceServerSettings.from_config(dict(port={self.port}, backend="fake", fake_script={str(self.script)!r}, startup_timeout=60))
            m = isup.InferenceServerManager(s, {str(self.root)!r}, {str(self.root / 'api' / 'server.yaml')!r}, {str(self.root / 'logs')!r})
            m.start()
            open({str(pidfile)!r}, "w").write(str(m.proc.pid))
            time.sleep(300)
        """), encoding="utf-8")
        parent = subprocess.Popen([sys.executable, str(helper)])
        self.addCleanup(lambda: parent.poll() is None and (parent.kill(), parent.wait(5)))
        self.assertTrue(wait_until(pidfile.exists, timeout=60))
        child = int(pidfile.read_text())
        self.assertTrue(pid_alive(child))
        parent.kill()                                                  # 強制終了(atexit も走らない)
        parent.wait(10)
        self.assertTrue(wait_until(lambda: not pid_alive(child), timeout=15), "親が消えても推論サーバーが残った")
        self.assertFalse(port_open(self.port))


@unittest.skipUnless(INFER_PY.is_file(), "推論サーバー用の別venvがありません(inference_server/.venv)")
class MainE2ETest(T.MainIntegrationBase):
    """app.main(config_path) から推論サーバーを起動し、ready 確認後に処理し、終了時に停止する(外部処理はモック、推論は実HTTP)。"""

    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.fake = self.tmp / "fake.json"
        self.write_fake(FAKE_SCRIPT)

    def write_fake(self, d):
        self.fake.write_text(json.dumps(d), encoding="utf-8")

    def _make_config(self):
        path = super()._make_config()
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        cfg["api"]["dir_venv"] = str(self.out_root / "api")   # app_test.yaml と同じく、server.yaml は出力ルート配下に置く
        cfg["inference_server"] = dict(enabled=True, port=self.port if hasattr(self, "port") else free_port(), backend="fake",
                                       fake_script=str(self.tmp / "fake.json"), startup_timeout=60, stop_timeout=10)
        path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        return path

    def set_infer(self, **kw):
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        cfg["inference_server"].update(kw)
        cfg["inference_server"]["port"] = self.port
        self.config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    def run_e2e(self):
        self.set_infer()
        self.host_calls, self.in_use_calls, self.print_script_calls, self.convert_calls = [], [], [], []
        self.http_calls, self.popen_calls, self.bad_connects = [], [], []
        real_open, real_post, real_popen, real_connect = builtins.open, requests.post, subprocess.Popen, socket.socket.connect

        def logged_open(file, mode="r", *a, **kw):
            if isinstance(file, (str, os.PathLike)):
                self.open_log.append((str(file), mode))
            return real_open(file, mode, *a, **kw)

        def spy_post(url, *a, **kw):
            self.http_calls.append((url, kw.get("proxies"), kw.get("data")))
            return real_post(url, *a, **kw)

        def spy_popen(cmd, *a, **kw):
            self.popen_calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)])
            return real_popen(cmd, *a, **kw)

        def spy_connect(sock, address, *a, **kw):
            if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
                self.bad_connects.append(address)
                raise AssertionError(f"loopback 以外への接続: {address}")
            return real_connect(sock, address, *a, **kw)

        def host_guard(name, ret):
            def _f(*a, **kw):
                self.host_calls.append((name, a))
                return ret
            return _f

        self.rmtree = mock.Mock(name="shutil.rmtree")
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(builtins, "open", logged_open))
            st.enter_context(mock.patch.object(requests, "post", spy_post))
            st.enter_context(mock.patch.object(subprocess, "Popen", spy_popen))
            st.enter_context(mock.patch.object(socket.socket, "connect", spy_connect))
            st.enter_context(mock.patch.object(app.host_mod, "create_n3_design_list", host_guard("create_n3_design_list", [T.PRODUCT])))
            st.enter_context(mock.patch.object(app.host_mod, "get_property_data", host_guard("get_property_data", (T.SERIAL, T.BOX_TYPE))))
            st.enter_context(mock.patch.object(app.design_mod, "search_server", host_guard("search_server", str(self.design_serial))))
            st.enter_context(mock.patch.object(app.print_support, "output_print_script", self._fake_output_print_script))
            st.enter_context(mock.patch.object(app.print_support, "print_dwg_for_pdf", mock.Mock(name="print_dwg_for_pdf", return_value=app.print_support.PrintOutcome(app.print_support.COMPLETED, 0))))
            st.enter_context(mock.patch.object(app.cad_mod, "is_file_in_use", lambda p: self.in_use_calls.append(str(p)) or False))
            st.enter_context(mock.patch.object(app.cad_mod, "countdown", mock.Mock(name="countdown")))
            st.enter_context(mock.patch.object(app.cad_mod, "convert_dwg2dxf", self._fake_convert_dwg2dxf))
            st.enter_context(mock.patch.object(app.shutil, "rmtree", self.rmtree))
            st.enter_context(contextlib.redirect_stdout(self.stdout))
            st.enter_context(contextlib.redirect_stderr(self.stderr))
            # 推論API(slice_prediction_request)はモックしない: 実HTTPで別プロセスのサーバーへ依頼する
            return app.main(config_path=self.config_path)

    def assert_server_gone(self):
        self.assertTrue(wait_until(lambda: not port_open(self.port), timeout=10), "推論サーバーが残っている")

    def assert_only_loopback_and_own_child(self):
        self.assertEqual(self.bad_connects, [])
        for url, proxies, _ in self.http_calls:
            self.assertTrue(url.startswith(f"http://127.0.0.1:{self.port}/"), url)
            self.assertEqual(proxies, {"http": None, "https": None})   # 検証モードはプロキシなし
        self.assertEqual(len(self.popen_calls), 1)                      # 起動したのは推論サーバー(別venv)の1つだけ
        self.assertEqual(Path(self.popen_calls[0][0]).resolve(), INFER_PY.resolve())
        self.assertNotIn("prod-host-example", " ".join(self.popen_calls[0]).lower())
        self.assertFalse(any("gcad" in " ".join(c).lower() for c in self.popen_calls))   # IJCAD は起動していない

    def test_success_starts_server_processes_and_stops_it(self):
        res = self.run_e2e()
        self.assertEqual((res["completed"], res["failed"]), ([T.PRODUCT], {}))
        self.assert_server_gone()
        self.assert_only_loopback_and_own_child()
        self.assertEqual(self.rmtree.call_count, 0)
        self.assert_inputs_untouched()
        self.assert_no_secret_leak()
        out = self.product_out
        for p in T.PAGES:  # サーバーが作った推論結果が出力先にあり、後続集計に読み込まれている
            self.assertTrue((out / f"{p}_detected.txt").is_file())
            self.assertTrue((out / f"{p}_detected.png").is_file())
            self.assertFalse(list(out.glob(".probe_*")))
        detected = pd.read_csv(out / "detected_df.csv", encoding="cp932")
        self.assertEqual(len(detected), 5)
        est = pd.read_csv(out / "estcode_df.csv", encoding="cp932").set_index("BAN_MENNO")
        self.assertEqual((est.loc[1, "PANEL"], est.loc[2, "TRANS"], est.loc[2, "PANEL"], est.loc[1, "TRANS"]), (1, 2, 0, 0))
        log = next((self.out_root / "a_product" / "logs").glob("app_*.log")).read_text(encoding="utf-8")
        self.assertLess(log.index("推論サーバー準備完了"), log.index("製番処理開始"))   # ready 確認の後に処理開始
        self.assertIn("work_dir をサーバーと共有できていることを確認", log)
        self.assertIn("推論サーバーを停止: 終了コード=0", log)
        self.assertTrue(list((self.out_root / "a_product" / "logs").glob("inference_server_*.log")))

    def test_japanese_class_name_flows_to_cp932_csv_readable_by_navi_spec(self):
        # サーバー(UTF-8 の検出テキスト) -> データ作成側 -> detected_df.csv(CP932・BOMなし) -> Navi と同じ cp932 厳密読込
        script = json.loads(json.dumps(FAKE_SCRIPT))
        script["pages"]["22"][0]["name"] = "トランス"
        self.write_fake(script)
        res = self.run_e2e()
        self.assertEqual(res["completed"], [T.PRODUCT])
        txt = (self.product_out / "22_detected.txt").read_bytes()
        self.assertIn("Device$トランス".encode("utf-8"), txt)
        raw = (self.product_out / "detected_df.csv").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("cp932", errors="strict")
        self.assertIn("トランス", text)
        self.assertNotIn("トランス".encode("utf-8"), raw)
        self.assert_server_gone()

    def test_api_failure_page_marks_product_failed_not_success(self):
        self.write_fake({**FAKE_SCRIPT, "fail_pages": ["22"]})
        res = self.run_e2e()
        self.assertEqual(res["completed"], [])
        self.assertIn(T.PRODUCT, res["failed"])
        self.assertIn("InferenceRequestError", res["failed"][T.PRODUCT])
        self.assert_server_gone()
        self.assert_only_loopback_and_own_child()
        self.assertFalse((self.product_out / "estcode_df.csv").exists())        # 集計まで進んでいない
        self.assertFalse((self.product_out / "detected_df.csv").exists())
        # 削除要求は出力ルート配下の製番フォルダのみ(モックなので実削除なし)
        self.assertEqual([Path(c.args[0]).resolve() for c in self.rmtree.call_args_list], [self.product_out])
        self.assertTrue(self.product_out.is_dir())
        log = next((self.out_root / "a_product" / "logs").glob("app_*.log")).read_text(encoding="utf-8")
        self.assertIn("status=500", log)
        self.assertIn("失敗した製番", log)

    def test_rerun_over_existing_detection_files(self):
        res1 = self.run_e2e()
        self.assertEqual(res1["completed"], [T.PRODUCT])
        # 同じ製番を再実行できる状態(出力フォルダを残した状態では処理済みとして扱われる)の代わりに、
        # サーバーAPIの再実行(既存の検出ファイルを置き換える)を同じ設定で直接確認する
        m = isup.InferenceServerManager(isup.InferenceServerSettings.from_config(dict(port=self.port, backend="fake", fake_script=str(self.fake))),
                                        str(self.out_root), str(self.out_root / "api" / "server.yaml"), str(self.out_root / "logs"))
        before = (self.product_out / "21_detected.txt").read_bytes()
        (self.product_out / "21_detected.txt").write_text("OLD\n", encoding="utf-8")
        with m:
            r = requests.post(f"{m.base_url}/predict", data={"work_dir": str(self.product_out), "page_nmb": "21", "extension": "png"},
                              proxies={"http": None, "https": None}, timeout=30)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((self.product_out / "21_detected.txt").read_bytes(), before)  # 置き換わり、同じ内容

    def test_server_not_ready_does_not_start_processing(self):
        model_dir = self.tmp / "model"
        model_dir.mkdir()
        self.set_infer(backend="sahi", model_dir=str(model_dir))
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        cfg["inference_server"].pop("fake_script", None)
        self.config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        self.host_calls, self.popen_calls = [], []
        with mock.patch.object(app.host_mod, "create_n3_design_list", side_effect=AssertionError("開始しないはず")), \
                mock.patch.object(app.design_mod, "search_server", side_effect=AssertionError("開始しないはず")), \
                contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            with self.assertRaises(isup.InferenceServerError) as cm:
                app.main(config_path=self.config_path)
        self.assertIn("準備に失敗", str(cm.exception))
        self.assertFalse(self.output_dir.exists() and any(self.output_dir.iterdir()))   # 製番フォルダは作られていない
        log = next((self.out_root / "a_product" / "logs").glob("app_*.log")).read_text(encoding="utf-8")
        self.assertIn("積算処理を開始しません", log)                                       # 原因がログに残る
        self.assertIn("準備に失敗", log)
        self.assert_server_gone()

    def test_production_root_in_config_is_refused_before_starting(self):
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        cfg["data"]["output"] = PRODUCTION_ROOT
        cfg["api"]["dir_venv"] = PRODUCTION_ROOT
        self.config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        real_open, real_makedirs, real_exists = builtins.open, os.makedirs, os.path.exists

        def watch(real, label):
            def f(path, *a, **kw):  # 設定ファイル等は通常どおり扱い、本番共有への操作だけを禁止する
                if "prod-host-example" in str(path).lower():
                    raise AssertionError(f"本番共有を {label} した: {path}")
                return real(path, *a, **kw)
            return f

        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("起動されないはず")), \
                mock.patch.object(app.host_mod, "create_n3_design_list", side_effect=AssertionError("開始しないはず")), \
                mock.patch.object(os, "makedirs", watch(real_makedirs, "makedirs")), \
                mock.patch.object(builtins, "open", watch(real_open, "open")), \
                mock.patch.object(os.path, "exists", watch(real_exists, "exists")):
            with self.assertRaises(isup.InferenceServerError) as cm:
                app.main(config_path=self.config_path)
        self.assertIn("本番共有", str(cm.exception))

    def test_production_converter_in_config_is_refused_before_starting(self):
        # 検証モードでは CAD 変換ソフトも本番共有上のものを使わない(起動・開始・書き込みの前に拒否する)
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        cfg["dwg_conversion"]["converter"] = "//prod-host-example/ShareData/exampleroot/ODAFileConverter 26.10.0/ODAFileConverter.exe"
        self.config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("起動されないはず")),                 mock.patch.object(app.host_mod, "create_n3_design_list", side_effect=AssertionError("開始しないはず")):
            with self.assertRaises(isup.InferenceServerError) as cm:
                app.main(config_path=self.config_path)
        self.assertIn("本番共有", str(cm.exception))
        self.assertIn("dwg_conversion.converter", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
