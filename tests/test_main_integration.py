# app.main(config_path=...) の統合テスト(外部処理はすべてモック)
#
# 方針:
# - 入出力はテスト用一時ディレクトリ(tempfile)内だけ。app_test.yaml を読み、出力先等を一時ディレクトリへ上書きした
#   YAML を作って main(config_path=...) に渡す。
# - モック対象: ホスト照会 / 設計サーバー探索 / DWG使用中確認 / CAD(IJCAD)印刷 / DWG->DXF変換 / 推論API送信 / 待機(sleep)
# - 外部アクセスの検出: requests / subprocess / socket / os.system / win32com を「呼ばれたら記録」するガードに差し替え、
#   テスト後に呼び出し0件を検証する。
# - shutil.rmtree は記録用モックに差し替え、実削除は行わない。
# - 接続情報(host_mod.local.yaml)は読み込み・接続とも行わない。値がログ/標準出力に出ていないことも検証する。
import builtins
import contextlib
import hashlib
import io
import logging
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ezdxf
import fitz  # PyMuPDF
import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

PRODUCT = "A1BN0001"
SERIAL = "S001"
BOX_TYPE = "OS2K"
PAGES = ["21", "22", "23"]                # 推論対象(.baninf のあるページ。従来どおりの工程を通る)
PDF_ONLY_PAGES = ["24", "3A"]             # .baninf が無い図面: PDF印刷だけ行う
EXCLUDED_PAGES = ["X1", "x2", "Xtra"]     # 先頭が X/x の図面: 印刷も後続処理も行わない
PRINT_PAGES = sorted(PAGES + PDF_ONLY_PAGES)  # 印刷対象(従来のページ並び=文字列の昇順)
FRAME_W, FRAME_H = 4200, 2970  # 擬似DXFの図枠(CAD座標)

# .baninf (cp932, 1行=1盤情報, key$value を '|' 区切り)
_BANINF = {
    "21": [
        dict(BAN_MENNO=1, BAN_NO=1, PAGE=21, ZUMEI="内部機器配置図(1-1)", BAN_MEISYOU="テスト盤1", BAN_TYPE="正面図",
             BAN_H1=2300, BAN_H2=2300, BAN_W=800, BAN_D=1500, KITEN_X=500, KITEN_Y=500),
    ],
    "22": [
        dict(BAN_MENNO=2, BAN_NO=2, PAGE=22, ZUMEI="内部機器配置図(2-1)", BAN_MEISYOU="テスト盤2", BAN_TYPE="側面図",
             BAN_H1=2300, BAN_H2=2300, BAN_W=800, BAN_D=1500, KITEN_X=2000, KITEN_Y=500),
    ],
    "23": [
        dict(BAN_MENNO=1, BAN_NO=1, PAGE=23, ZUMEI="基礎図", BAN_MEISYOU="テスト盤1", BAN_TYPE="基礎図",
             BAN_H1=2300, BAN_H2=2300, BAN_W=800, BAN_D=1500, KITEN_X=3000, KITEN_Y=3055),
        dict(BAN_MENNO=2, BAN_NO=2, PAGE=23, ZUMEI="基礎図", BAN_MEISYOU="テスト盤2", BAN_TYPE="基礎図",
             BAN_H1=2300, BAN_H2=2300, BAN_W=800, BAN_D=1500, KITEN_X=3900, KITEN_Y=3055),
    ],
}

# 擬似検出: (PAGE, DEVICE, CAD座標の中心X, 中心Y)  ※領域内/外を混在させる
_DETECTIONS = [
    ("21", "panel", 900, 1500),        # 21: 正面図 領域(500-1300, 500-2800)内 -> PANEL +1 (BAN_MENNO=1)
    ("21", "panel", 3900, 1500),       # 21: 領域外 -> 数えない
    ("22", "transformer", 2500, 1200), # 22: 側面図 領域(2000-3500, 500-2800)内 -> TRANS +1 (BAN_MENNO=2)
    ("22", "transformer", 3000, 2000), # 22: 領域内 -> TRANS +1
    ("22", "transformer", 3900, 1200), # 22: 領域外 -> 数えない
]


# 設計データの design.csv(ページ番号 .db)/ material.csv(.boxspec)の擬似入力。値はすべて架空。
# どちらも無いと製番は失敗になるため、統合テストの既定の設計データには必ず含める。
def page_db_lines(page, serial=SERIAL, product=PRODUCT):
    """ページ番号 .db の 24 行(1 行 = 1 値)。行位置は design.csv の列順(2=整理番号, 3=製番, 4=ページ番号, ...)。"""
    return ["", serial, product, str(page), "", f"図名{page}", "1", "2", "設計者A", "26", "10", "01", "照査者B", "26", "10", "02",
            "製作者C", "26", "10", "03", "照査者D", "26", "10", "04"]


def write_page_db(folder, name, lines):
    Path(folder, name).write_bytes(("\r\n".join(lines) + "\r\n").encode("cp932"))


DEFAULT_BOXSPEC = ["シンボル名$BSP-CABTYPEA|CABTYPE$ZZ1|", "シンボル名$BSP-STEELA-H|STEEL-BASE$無し|STEEL-SIDE$TEST-S 1.0|"]


class ExternalCallError(AssertionError):
    pass


def _under(path, base) -> bool:
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except ValueError:
        return False


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MainIntegrationBase(unittest.TestCase):
    """一時ディレクトリ上に擬似の設計サーバー/出力先/設定を用意し、外部処理をモックして main() を実行する"""
    boxspec_lines = DEFAULT_BOXSPEC   # None なら .boxspec を置かない(= material.csv を作れず製番は失敗)
    with_page_db = True               # False ならページ番号 .db を置かない(= design.csv を作れず製番は失敗)

    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="sekisan_it_")
        self.addCleanup(td.cleanup)          # テスト用一時ディレクトリのみ削除(後に登録した下記が先に実行される)
        self.addCleanup(self._close_logging)  # Windows ではログファイルを開いたままだと一時ディレクトリを消せない
        self.tmp = Path(td.name).resolve()
        self.design_serial = self.tmp / "design" / SERIAL          # search_server が返す擬似設計サーバー
        self.design_product = self.design_serial / PRODUCT
        self.out_root = self.tmp / "out"
        self.output_dir = self.out_root / "a_product" / "output"
        self.product_out = self.output_dir / PRODUCT

        self._make_design_data()
        self.config_path = self._make_config()
        self.design_hash = {p: _sha(p) for p in self.design_product.rglob("*") if p.is_file()}
        self.app_yaml_hash = _sha(ROOT / "app.yaml")
        self.app_test_yaml_hash = _sha(ROOT / "app_test.yaml")

        self.open_log = []   # (path, mode)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    @staticmethod
    def _close_logging():
        lg = logging.getLogger("app")  # util_mod.Logger の既定名
        for h in list(lg.handlers):
            h.close()
            lg.removeHandler(h)

    # ---------------------------------------------------------------- fixtures
    def _make_design_data(self):
        self.design_product.mkdir(parents=True)
        for page, rows in _BANINF.items():
            lines = ["|".join(f"{k}${v}" for k, v in r.items()) for r in rows]
            (self.design_product / f"{page}.baninf").write_text("\n".join(lines) + "\n", encoding="cp932")
            (self.design_product / f"{page}.dwg").write_bytes(b"dummy-dwg-" + page.encode())
        for page in PDF_ONLY_PAGES + EXCLUDED_PAGES:  # .baninf の無い図面(拡張子の大文字小文字は問わない)
            (self.design_product / f"{page}.DWG").write_bytes(b"dummy-dwg-" + page.encode())
        if self.with_page_db:
            for page in list(_BANINF) + PDF_ONLY_PAGES + EXCLUDED_PAGES:   # 印刷対象の判定は design.csv の「ページ番号」で行うため、すべての図面に .db を置く
                write_page_db(self.design_product, f"{page}.db", page_db_lines(page))
        if self.boxspec_lines is not None:
            write_page_db(self.design_product, "301.boxspec", self.boxspec_lines)   # CP932・CRLF で書く(内容は .boxspec の行)

    def _make_config(self) -> Path:
        cfg = yaml.safe_load((ROOT / "app_test.yaml").read_text(encoding="utf-8"))
        cfg["data"]["output"] = str(self.out_root)
        cfg["api"]["dir_venv"] = str(self.tmp / "api")
        cfg["dwg_conversion"]["converter"] = str(self.tmp / "fake_converter" / "ODAFileConverter.exe")
        path = self.tmp / "app_test_tmp.yaml"
        path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        for key in (cfg["data"]["output"], cfg["api"]["dir_venv"], cfg["dwg_conversion"]["converter"]):
            self.assertTrue(_under(key, self.tmp), key)
        return path

    # ---------------------------------------------------------------- mocks
    def _fake_output_print_script(self, page_list, dir_target, dir_product):
        """IJCAD 印刷の代わりに、A3 相当の枠付きPDFを出力先へ作る"""
        self.print_script_calls.append((list(page_list), str(dir_target), str(dir_product)))
        for page in page_list:
            doc = fitz.open()
            pg = doc.new_page(width=1191, height=842)
            pg.draw_rect(fitz.Rect(30, 30, 1161, 812), color=(0, 0, 0), width=3)
            doc.save(os.path.join(dir_product, f"{page}.pdf"))
            doc.close()
        return os.path.join(dir_product, "WIRING.SCR")

    def _fake_convert_dwg2dxf(self, input_dir, output_dir, converter_path="", version=""):
        """ODAFileConverter の代わりに、AUX層の赤い図枠を持つ DXF を出力先へ作る"""
        self.convert_calls.append((str(input_dir), str(output_dir), str(converter_path), str(version)))
        for dwg in sorted(Path(input_dir).glob("*.dwg")):
            doc = ezdxf.new("R2010")
            doc.layers.add("AUX", color=1)
            doc.modelspace().add_lwpolyline(
                [(0, 0), (FRAME_W, 0), (FRAME_W, FRAME_H), (0, FRAME_H)], close=True,
                dxfattribs={"layer": "AUX", "color": 1})
            doc.saveas(os.path.join(output_dir, f"{dwg.stem}.dxf"))

    def _fake_slice_prediction_request(self, dir_product, page_list, api_cnf_path, use_proxy=True):
        """推論API送信の代わりに、後続処理が読む最小限の *_detected.txt を出力先へ作る"""
        self.api_calls.append((str(dir_product), list(page_list), str(api_cnf_path), use_proxy))
        pdf = pd.read_csv(os.path.join(dir_product, "product_df.csv"), encoding="cp932")
        pdf["PAGE"] = pdf["PAGE"].astype(str)
        for page in page_list:
            row = pdf[pdf["PAGE"] == str(page)].iloc[0]
            sx, sy, fo_y = float(row["SCALE_X"]), float(row["SCALE_Y"]), float(row["FRAME_ORG_Y"])
            lines = []
            for i, (pg, dev, cx, cy) in enumerate(d for d in _DETECTIONS if d[0] == str(page)):
                # create_detected_csv の逆変換: CAD座標(cx,cy) -> 画像座標
                x, y = cx / sx, (fo_y - cy) / sy
                pts = [(x - 5, y - 5), (x + 5, y - 5), (x - 5, y + 5), (x + 5, y + 5)]
                coords = ":".join(f"{px},{py}" for px, py in pts)
                lines.append(f"Index${i}|Device${dev}|Score$0.9|Coordinates${coords}")
            Path(dir_product, f"{page}_detected.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return "".join(
            f"INF: APIリクエスト| API-URL:http://mock/predict, 推論対象:{dir_product}\\{p}.jpeg\n"
            f"INF: 推論完了 -> '{dir_product}\\{p}_detected.txt'" for p in page_list)

    def _fake_preflight(self, config_data, **kw):
        """推論 API の事前確認の代わり(実接続しない)。self.preflight_reason が None なら使える、文字列なら使えない理由"""
        self.preflight_calls.append(1)
        return self.preflight_reason

    # ---------------------------------------------------------------- run
    def run_main(self, *, in_use=False, convert_error=None, **main_kwargs):
        self.print_script_calls, self.convert_calls, self.api_calls, self.in_use_calls = [], [], [], []
        self.host_calls = []
        self.preflight_calls = []
        self.preflight_reason = getattr(self, "preflight_reason", None)
        real_open = builtins.open

        def logged_open(file, mode="r", *a, **kw):
            if isinstance(file, (str, os.PathLike)):
                self.open_log.append((str(file), mode))
            return real_open(file, mode, *a, **kw)

        def guard(name):
            def _raise(*a, **kw):
                raise ExternalCallError(f"外部呼び出し: {name}")
            return mock.Mock(side_effect=_raise, name=name)

        self.guards = {
            "requests.post": guard("requests.post"),
            "requests.get": guard("requests.get"),
            "requests.request": guard("requests.request"),
            "requests.Session.request": guard("requests.Session.request"),
            "subprocess.Popen": guard("subprocess.Popen"),
            "subprocess.run": guard("subprocess.run"),
            "subprocess.call": guard("subprocess.call"),
            "os.system": guard("os.system"),
            "socket.socket.connect": guard("socket.socket.connect"),
            "win32com.client.Dispatch": guard("win32com.client.Dispatch"),
        }
        import win32com.client
        targets = {
            "requests.post": (requests, "post"), "requests.get": (requests, "get"),
            "requests.request": (requests, "request"), "requests.Session.request": (requests.Session, "request"),
            "subprocess.Popen": (subprocess, "Popen"), "subprocess.run": (subprocess, "run"),
            "subprocess.call": (subprocess, "call"), "os.system": (os, "system"),
            "socket.socket.connect": (socket.socket, "connect"), "win32com.client.Dispatch": (win32com.client, "Dispatch"),
        }

        def fake_in_use(path):
            self.in_use_calls.append(str(path))
            return in_use

        def host_guard(name, ret):
            def _f(*a, **kw):
                self.host_calls.append((name, a))
                return ret
            return _f

        self.rmtree = mock.Mock(name="shutil.rmtree")  # 実削除しない(呼び出し記録のみ)
        convert = (mock.Mock(side_effect=convert_error) if convert_error else self._fake_convert_dwg2dxf)

        with contextlib.ExitStack() as st:
            for key, (obj, attr) in targets.items():
                st.enter_context(mock.patch.object(obj, attr, self.guards[key]))
            st.enter_context(mock.patch.object(builtins, "open", logged_open))
            # ---- 外部処理のモック ----
            st.enter_context(mock.patch.object(app.host_mod, "create_n3_design_list", host_guard("create_n3_design_list", [PRODUCT])))
            st.enter_context(mock.patch.object(app.host_mod, "get_property_data", host_guard("get_property_data", (SERIAL, BOX_TYPE))))
            st.enter_context(mock.patch.object(app.design_mod, "search_server", host_guard("search_server", str(self.design_serial))))
            st.enter_context(mock.patch.object(app.print_support, "output_print_script", self._fake_output_print_script))
            st.enter_context(mock.patch.object(app.print_support, "print_dwg_for_pdf", mock.Mock(name="print_dwg_for_pdf", return_value=app.print_support.PrintOutcome(app.print_support.COMPLETED, 0))))
            st.enter_context(mock.patch.object(app.cad_mod, "is_file_in_use", fake_in_use))
            st.enter_context(mock.patch.object(app.cad_mod, "countdown", mock.Mock(name="countdown")))
            st.enter_context(mock.patch.object(app.cad_mod, "convert_dwg2dxf", convert))
            st.enter_context(mock.patch.object(app.image_mod, "slice_prediction_request", self._fake_slice_prediction_request))
            st.enter_context(mock.patch.object(app.shutil, "rmtree", self.rmtree))
            st.enter_context(mock.patch.object(app.api_preflight, "unavailable_reason", self._fake_preflight))  # 既定は「使える」
            st.enter_context(contextlib.redirect_stdout(self.stdout))
            st.enter_context(contextlib.redirect_stderr(self.stderr))  # Logger の StreamHandler は stderr
            self.results = app.main(config_path=self.config_path, **main_kwargs)   # {completed, failed, skipped}

    # ---------------------------------------------------------------- common checks
    def assert_no_external_calls(self):
        for name, g in self.guards.items():
            self.assertEqual(g.call_count, 0, f"{name} が呼ばれた")

    def assert_inputs_untouched(self):
        # 擬似設計サーバー側が不変であること(DWG の追記オープン等で変更されていない)
        now = {p: _sha(p) for p in self.design_product.rglob("*") if p.is_file()}
        self.assertEqual(now, self.design_hash)
        self.assertEqual(_sha(ROOT / "app.yaml"), self.app_yaml_hash)
        self.assertEqual(_sha(ROOT / "app_test.yaml"), self.app_test_yaml_hash)
        # 書き込み/追記モードの open は一時ディレクトリ内に限り、擬似設計サーバー配下(DWG含む)への書込み/追記は0件
        # (出力先へのDWGコピー(copy2)による書込みは一時ディレクトリ内なので対象外)
        for path, mode in self.open_log:
            if any(c in mode for c in "wax+"):
                self.assertTrue(_under(path, self.tmp), f"一時ディレクトリ外への書込み: {path}")
                self.assertFalse(_under(path, self.design_serial.parent), f"設計サーバー側を書込みモードで開いた: {path}")

    def assert_no_secret_leak(self):
        secrets = []
        local = ROOT / "packages" / "bs_pkg" / "bs_pkg" / "config" / "host_mod.local.yaml"
        if local.exists():
            d = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
            secrets = [str(v) for k, v in d.items() if k in ("host_name", "password") and len(str(v)) >= 6]
        blob = self.stdout.getvalue() + self.stderr.getvalue()
        for p in self.tmp.rglob("*"):
            if p.is_file() and p.suffix in (".log", ".txt", ".csv", ".yaml"):
                blob += p.read_text(encoding="cp932", errors="ignore")
        for s in secrets:
            self.assertNotIn(s, blob)


class MainSuccessTest(MainIntegrationBase):
    def test_success_runs_through_aggregation(self):
        self.run_main()

        # --- 外部呼び出し0件 / 実削除なし ---
        self.assert_no_external_calls()
        self.assertEqual(self.rmtree.call_count, 0, self.rmtree.call_args_list)
        self.assert_inputs_untouched()
        self.assert_no_secret_leak()

        # --- モックの呼ばれ方 ---
        self.assertEqual([c[0] for c in self.host_calls], ["create_n3_design_list", "get_property_data", "search_server"])
        self.assertEqual([Path(c).stem for c in self.in_use_calls], PRINT_PAGES)  # 追記オープンの代わりに確認のみ
        self.assertEqual(len(self.print_script_calls), 1)
        self.assertEqual(self.print_script_calls[0][0], PRINT_PAGES)  # X/x始まりを除く全ページを印刷
        self.assertTrue(_under(self.print_script_calls[0][2], self.tmp))
        self.assertEqual(len(self.convert_calls), 1)
        self.assertTrue(_under(self.convert_calls[0][0], self.tmp) and _under(self.convert_calls[0][1], self.tmp))
        self.assertTrue(_under(self.convert_calls[0][2], self.tmp))  # converter は一時ディレクトリ内の偽パス
        self.assertEqual(len(self.api_calls), 1)
        self.assertEqual(self.api_calls[0][1], PAGES)
        self.assertTrue(_under(self.api_calls[0][2], self.tmp))      # API設定も一時ディレクトリ内(読まれない)
        self.assertIs(self.api_calls[0][3], False)                   # app_test.yaml の api.use_proxy: false が渡る

        # --- 期待する出力が出力先(一時ディレクトリ)に生成されている ---
        out = self.product_out
        for name in [f"{PRODUCT}_df.csv", f"{PRODUCT}_df_cubicle.csv", f"{PRODUCT}_df_foundation.csv",
                     f"{PRODUCT}_df_outline.csv", "product_df.csv", "detected_df.csv", "estcode_df.csv"]:
            self.assertTrue((out / name).is_file(), name)
        for p in PAGES:
            for ext in (".pdf", ".jpeg", ".png", ".dxf", "_detected.txt"):
                self.assertTrue((out / f"{p}{ext}").is_file(), f"{p}{ext}")
            self.assertTrue((out / "dwg" / f"{p}.dwg").is_file())
        self.assertTrue(list((self.out_root / "a_product" / "logs").glob("*.log")))

        # --- 推論対象外はPDFと別名の表示用PNG。推論成果物は出さず、X/x始まりは何も出力しない ---
        for p in PDF_ONLY_PAGES:
            self.assertTrue((out / f"{p}.pdf").is_file(), p)
            preview = out / f"{p}__preview.png"
            self.assertTrue(preview.is_file(), p)
            self.assertEqual(preview.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            for ext in (".jpeg", ".png", ".dxf", "_detected.txt", "_detected.png"):
                self.assertFalse((out / f"{p}{ext}").exists(), f"{p}{ext}")
            self.assertFalse((out / "dwg" / f"{p}.dwg").exists())
        for p in EXCLUDED_PAGES:
            self.assertEqual([f.name for f in out.rglob("*") if f.name.lower().startswith(p.lower())], [], p)
        self.assertEqual(sorted(f.stem for f in out.glob("*.dxf")), PAGES)   # DXF変換・以降の対象は推論対象のみ
        self.assertEqual(sorted(f.stem for f in out.glob("*_detected.txt")), [f"{p}_detected" for p in PAGES])
        self.assertEqual(sorted(f.stem for f in out.glob("*.pdf")), PRINT_PAGES)
        self.assertEqual(self.api_calls[0][1], PAGES)

        # --- 図枠・縮尺が product_df に反映されている ---
        product_df = pd.read_csv(out / "product_df.csv", encoding="cp932")
        self.assertTrue((product_df["FRAME_ORG_X"] == FRAME_W).all())
        self.assertTrue((product_df["FRAME_ORG_Y"] == FRAME_H).all())
        self.assertTrue((product_df["SCALE_X"] > 0).all())

        # --- 検出集計: 領域内のみ数えられる ---
        detected = pd.read_csv(out / "detected_df.csv", encoding="utf-8")
        self.assertEqual(len(detected), len(_DETECTIONS))
        est = pd.read_csv(out / "estcode_df.csv", encoding="cp932").set_index("BAN_MENNO")
        self.assertEqual(list(est.index), [1, 2])
        self.assertEqual(est.loc[1, "PANEL"], 1)
        self.assertEqual(est.loc[2, "TRANS"], 2)
        self.assertEqual(est.loc[1, "PANEL"] + est.loc[2, "PANEL"], 1)
        self.assertEqual(est.loc[1, "TRANS"], 0)
        self.assertEqual(est.loc[1, "MODEL"], BOX_TYPE)
        self.assertEqual(est.loc[1, "BAN_CONNECT"], "箱･左右(L)")
        self.assertEqual(est.loc[2, "BAN_CONNECT"], "箱･左右(R)")


class MainFailureTest(MainIntegrationBase):
    def _assert_rmtree_only_in_tmp(self):
        self.assertGreaterEqual(self.rmtree.call_count, 1)
        for call in self.rmtree.call_args_list:
            target = call.args[0]
            self.assertTrue(_under(target, self.tmp), f"一時ディレクトリ外への削除要求: {target}")
            self.assertTrue(_under(target, self.output_dir), f"出力先外への削除要求: {target}")
        # 記録のみで実削除されていない
        self.assertTrue(self.product_out.is_dir())
        self.assertTrue(self.design_product.is_dir())

    def test_conversion_error_requests_cleanup_but_does_not_delete(self):
        self.run_main(convert_error=RuntimeError("DWG→DXF変換に失敗(テスト用)"))
        self.assert_no_external_calls()
        self._assert_rmtree_only_in_tmp()
        self.assertEqual([Path(c.args[0]).resolve() for c in self.rmtree.call_args_list], [self.product_out])
        self.assertEqual(self.api_calls, [])  # 変換失敗後はAPI送信まで進まない
        self.assert_inputs_untouched()
        self.assert_no_secret_leak()

    def test_drawing_in_use_skips_product_and_requests_cleanup(self):
        self.run_main(in_use=True)
        self.assert_no_external_calls()
        self._assert_rmtree_only_in_tmp()
        self.assertEqual(self.rmtree.call_args_list[0].kwargs.get("ignore_errors"), True)
        self.assertEqual(self.print_script_calls, [])  # 使用中なら印刷にも進まない
        self.assertEqual(self.api_calls, [])
        self.assert_inputs_untouched()
        self.assert_no_secret_leak()


class MainPrintTargetsTest(MainIntegrationBase):
    """印刷対象(X/x始まりを除く全ページ)と、0件・推論対象なしの扱い"""

    def _remove_design_pages(self, names):
        for f in [f for f in self.design_product.iterdir() if f.suffix.lower() == ".dwg"]:
            if f.stem in names:
                f.unlink()
        self.design_hash = {p: _sha(p) for p in self.design_product.rglob("*") if p.is_file()}

    def test_inference_page_missing_from_design_folder_is_failure_without_printing(self):
        self._remove_design_pages(["22"])
        self.run_main()
        self.assert_no_external_calls()
        self.assertEqual(self.print_script_calls, [])   # 推論対象の図面が無い製番は印刷にも進まない
        self.assertEqual(self.api_calls, [])
        self.assertEqual([Path(c.args[0]).resolve() for c in self.rmtree.call_args_list], [self.product_out])

    def test_only_excluded_drawings_means_zero_print_targets_and_safe_exit(self):
        self._remove_design_pages(set(PAGES) | set(PDF_ONLY_PAGES))   # 残るのは X/x始まりだけ
        for f in self.design_product.glob("*.baninf"):
            f.unlink()
        self.design_hash = {p: _sha(p) for p in self.design_product.rglob("*") if p.is_file()}
        self.run_main()                                  # 例外なく終了する
        self.assert_no_external_calls()
        self.assertEqual(self.print_script_calls, [])
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.convert_calls, [])

    def _add_drawing(self, name, with_db=True, db_page=None):
        (self.design_product / f"{name}.DWG").write_bytes(b"dummy-dwg-" + name.encode())
        if with_db:
            write_page_db(self.design_product, f"{name}.db", page_db_lines(db_page if db_page is not None else name))
        self.design_hash = {p: _sha(p) for p in self.design_product.rglob("*") if p.is_file()}

    def test_only_drawings_with_a_qualifying_design_csv_page_number_are_printed(self):
        # 6文字ちょうどは対象、7文字以上・design.csv に無いもの・X/x始まりは、印刷もPDFも作らない
        self._add_drawing("ABCDEF")
        self._add_drawing("ABCDEFG")
        self._add_drawing("屋外キュービクル")
        self._add_drawing("NODB", with_db=False)
        self.run_main()
        printed = sorted(p for call in self.print_script_calls for p in call[0])   # 5枚ずつの塊をすべて連結
        self.assertEqual(printed, sorted(PRINT_PAGES + ["ABCDEF"]))
        for name in ("ABCDEFG", "屋外キュービクル", "NODB", "X1", "x2", "Xtra"):
            self.assertNotIn(name, printed)
        self.assertEqual(self.api_calls[0][1], PAGES)                         # 推論対象は従来どおり
        self.assertFalse([f for f in self.product_out.glob("*") if f.stem in ("ABCDEFG", "屋外キュービクル", "NODB")])

    def test_matching_with_design_csv_ignores_case_and_keeps_leading_zeros(self):
        self._add_drawing("Mk01", db_page="mK01")      # 大文字小文字が違っても照合できる
        self._add_drawing("01")                       # design.csv の 01 とだけ照合する
        (self.design_product / "1.DWG").write_bytes(b"dummy-dwg-1")   # design.csv に「1」は無い(先頭ゼロの 01 とは別のページ)
        self.design_hash = {p: _sha(p) for p in self.design_product.rglob("*") if p.is_file()}
        self.run_main()
        printed = sorted(p for call in self.print_script_calls for p in call[0])   # 5枚ずつの塊をすべて連結
        self.assertIn("Mk01", printed)
        self.assertIn("01", printed)
        self.assertNotIn("1", printed)

    def test_unreadable_design_csv_fails_the_product_without_printing(self):
        # design.csv を読み取れないときは、条件なしで全図面を印刷せず、製番を失敗にして後片付けする
        with mock.patch.object(app.cad_mod, "read_design_page_ids", side_effect=app.cad_mod.DesignDataError("読み取れません")):
            self.run_main()
        self.assert_no_external_calls()
        self.assertEqual(self.print_script_calls, [])
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.convert_calls, [])
        self.assertEqual([Path(c.args[0]).resolve() for c in self.rmtree.call_args_list], [self.product_out])
    def test_x_prefixed_baninf_is_never_an_inference_page(self):
        # X始まりの .baninf があっても推論対象に入らない(既存の読込規則 + 防御的な除外)
        lines = "|".join(f"{k}${v}" for k, v in _BANINF["21"][0].items())
        (self.design_product / "X9.baninf").write_text(lines + chr(10), encoding="cp932")
        (self.design_product / "X9.dwg").write_bytes(b"dummy-dwg-X9")
        self.design_hash = {p: _sha(p) for p in self.design_product.rglob("*") if p.is_file()}
        self.run_main()
        self.assertEqual(self.api_calls[0][1], PAGES)
        self.assertNotIn("X9", self.print_script_calls[0][0])
        self.assertFalse(list(self.product_out.glob("X9*")))


if __name__ == "__main__":
    unittest.main()
