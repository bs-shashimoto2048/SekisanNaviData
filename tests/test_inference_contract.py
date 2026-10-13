# 推論サーバー統合の契約テスト(S6)。docs/inference_contract.md の契約を、データ作成側のテストで固定する。
# - InferenceClientTest: pipeline.inference_client(image_mod への依存点)の単体テスト。
# - ClientServerContractTest: 「クライアントが送る内容 = サーバーが受理する内容」「サーバーが書く検出ファイル = 後続処理が読める形式」。
#   サーバーは flask を import せずに使える部分(safety・_format_detections)だけを使う。HTTP 通信・外部接続はしない(requests.post はモック)。
# (サーバーを実際に起動した通しの確認は tests/test_inference_e2e.py、サーバー単体は inference_server/tests/)
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "inference_server"))

import estimate_code_processer as est_mod  # noqa: E402
from bs_pkg import image_mod  # noqa: E402
from pipeline import inference_client  # noqa: E402

import safety as server_safety  # noqa: E402  (inference_server/safety.py)
import server as inference_server_module  # noqa: E402  (inference_server/server.py。flask は create_app の中で import される)

API_YAML = {"host": {"pc_ip": "127.0.0.1", "port": 5555}}
API_PATH = "dummy_api_server.yaml"


class InferenceClientTest(unittest.TestCase):
    def test_log_text_is_normalized_to_str(self):
        self.assertEqual(inference_client.normalize_log_text("INF: ok"), inference_client.DetectionResponse("INF: ok", None))
        self.assertEqual(inference_client.normalize_log_text(None), inference_client.DetectionResponse("", "API request log is None."))
        self.assertEqual(inference_client.normalize_log_text(bytes("推論完了".encode("utf-8"))).log_text, "推論完了")
        self.assertEqual(inference_client.normalize_log_text(bytearray(b"abc")).log_text, "abc")
        self.assertEqual(inference_client.normalize_log_text(b"\xff\xfe").log_text, "��")   # 復号できなくても例外にしない
        res = inference_client.normalize_log_text(123)
        self.assertEqual(res.log_text, "123")
        self.assertIn("Unexpected API request log type", res.warning)

    def test_timeout_is_passed_only_when_given(self):
        with mock.patch.object(image_mod, "slice_prediction_request", return_value="INF: ok") as req:
            res = inference_client.request_detections("d", ["21"], "api.yaml", use_proxy=False)
            self.assertEqual(req.call_args.args, ("d", ["21"], "api.yaml"))
            self.assertEqual(req.call_args.kwargs, {"use_proxy": False})          # 省略時は timeout を渡さない(従来どおり待ち続ける)
            inference_client.request_detections("d", ["21"], "api.yaml", timeout=30.0)
            self.assertEqual(req.call_args.kwargs, {"use_proxy": True, "timeout": 30.0})
            inference_client.request_detections("d", ["21"], "api.yaml", timeout=None)
            self.assertEqual(req.call_args.kwargs, {"use_proxy": True, "timeout": None})   # 明示した None は渡す(検証設定で request_timeout 未指定)
        self.assertEqual(res.log_text, "INF: ok")

    def test_failure_propagates_unchanged(self):
        err = image_mod.InferenceRequestError("NG", ["22"], "ERR: x")
        with mock.patch.object(image_mod, "slice_prediction_request", side_effect=err):
            with self.assertRaises(image_mod.InferenceRequestError) as cm:
                inference_client.request_detections("d", ["22"], "api.yaml")
        self.assertIs(cm.exception, err)

    def test_format_log_is_empty_for_empty_text(self):
        with mock.patch.object(image_mod, "log_formater", return_value="FORMATTED") as fmt:
            self.assertEqual(inference_client.format_log(""), "")
            self.assertEqual(fmt.call_count, 0)
            self.assertEqual(inference_client.format_log("INF: a"), "FORMATTED")

    def test_describe_failure_only_for_inference_errors_with_a_log(self):
        with mock.patch.object(image_mod, "log_formater", side_effect=lambda t: f"<{t}>"):
            self.assertEqual(inference_client.describe_failure(image_mod.InferenceRequestError("NG", ["1"], "ERR: a")), "<ERR: a>")
            self.assertIsNone(inference_client.describe_failure(image_mod.InferenceRequestError("NG", ["1"], "")))
            self.assertIsNone(inference_client.describe_failure(ValueError("x")))

    def test_product_flow_depends_on_image_mod_for_images_only(self):
        # 推論に関する image_mod の機能(slice_prediction_request / log_formater / InferenceRequestError)は inference_client だけが使う
        text = (ROOT / "pipeline" / "product_flow.py").read_text(encoding="utf-8")
        for name in ("slice_prediction_request", "log_formater", "InferenceRequestError"):
            self.assertNotIn(f"image_mod.{name}", text, name)


class ClientServerContractTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="infer_contract_")
        self.addCleanup(td.cleanup)
        self.root = Path(td.name).resolve()
        self.work = self.root / "A1BN0001"
        self.work.mkdir()

    def capture_requests(self, pages=("21", "22")):
        """image_mod.slice_prediction_request を実際に動かし、requests.post に渡された内容を記録する(通信なし)。"""
        for p in pages:
            (self.work / f"{p}_detected.txt").write_text("", encoding="utf-8")   # サーバーが書いたはずの検出ファイル
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"text_file_path": "x", "detections": 0}
        post = mock.Mock(return_value=resp)
        no_net = mock.Mock(side_effect=AssertionError("実ネットワーク接続"))
        with mock.patch.object(image_mod.config_loader, "load_yaml",
                               side_effect=lambda p: API_YAML if p == API_PATH else {"processor": {"extention": "png"}}), \
                mock.patch.object(image_mod.requests, "post", post), mock.patch.object(image_mod.time, "sleep"), \
                mock.patch.object(socket.socket, "connect", no_net):
            inference_client.request_detections(str(self.work), list(pages), API_PATH, use_proxy=False)
        return post

    def test_request_is_a_form_post_to_predict_with_three_fields(self):
        post = self.capture_requests()
        self.assertEqual(post.call_count, 2)                                  # ページごとに 1 リクエスト
        for call, page in zip(post.call_args_list, ("21", "22")):
            self.assertEqual(call.args[0], "http://127.0.0.1:5555/predict")
            self.assertEqual(set(call.kwargs["data"]), {"work_dir", "page_nmb", "extension"})
            self.assertEqual(call.kwargs["data"]["work_dir"], str(self.work))
            self.assertEqual(call.kwargs["data"]["page_nmb"], page)
            self.assertEqual(call.kwargs["data"]["extension"], "png")

    def test_server_accepts_what_the_client_sends(self):
        post = self.capture_requests()
        for call in post.call_args_list:
            data = call.kwargs["data"]
            (self.work / f"{data['page_nmb']}.png").write_bytes(b"x")
            real_dir, page, ext, image = server_safety.validate_request(
                data["work_dir"], data["page_nmb"], data["extension"], str(self.root))
            self.assertEqual((Path(real_dir), page, ext), (self.work, data["page_nmb"], "png"))
            self.assertEqual(Path(image), self.work / f"{data['page_nmb']}.png")

    def test_server_rejects_work_dir_outside_its_output_root(self):
        post = self.capture_requests()
        data = post.call_args.kwargs["data"]
        (self.work / "22.png").write_bytes(b"x")
        other_root = self.root / "other_root"
        other_root.mkdir()
        with self.assertRaises(server_safety.SafetyError):
            server_safety.validate_request(data["work_dir"], data["page_nmb"], data["extension"], str(other_root))

    def test_server_output_is_readable_by_the_detection_consumer(self):
        # サーバーが書く {page}_detected.txt の形式(UTF-8、1 行 1 検出)を、後続の create_detected_csv が読める
        dets = [SimpleNamespace(name="panel", score=0.91, minx=100.0, miny=200.0, maxx=300.0, maxy=400.0),
                SimpleNamespace(name="トランス", score=0.8, minx=10.0, miny=20.0, maxx=50.0, maxy=60.0),
                SimpleNamespace(name="a|b$c", score=0.5, minx=0.0, miny=0.0, maxx=4.0, maxy=2.0)]
        text = inference_server_module._format_detections(dets)
        (self.work / "21_detected.txt").write_text(text, encoding="utf-8", newline="\n")
        pd.DataFrame({"PAGE": [21], "SCALE_X": [2.0], "SCALE_Y": [2.0], "FRAME_ORG_Y": [1000.0]}) \
            .to_csv(self.work / "product_df.csv", index=False, encoding="cp932")
        df, path, pages = est_mod.create_detected_csv(str(self.work))
        self.assertEqual(pages, ["21"])
        self.assertEqual(list(df["DEVICE"]), ["panel", "トランス", "a_b_c"])        # 区切り文字を含むクラス名は _ に置換される
        self.assertEqual(list(df["YOLO_INDEX"]), [0, 1, 2])
        self.assertEqual(list(df["SCORE"]), [0.91, 0.8, 0.5])
        # 画像座標(左上 100,200 / 右下 300,400、尺度 2.0、図枠高さ 1000)→ CAD 座標。中心 = 画像中心 × 尺度(Y は図枠高さから引く)
        first = df.iloc[0]
        self.assertEqual((first["CENTER_X"], first["CENTER_Y"]), (400, 400))
        self.assertTrue(Path(path).read_bytes().decode("cp932"))                    # 日本語クラス名が CP932 で書ける

    def test_empty_detection_file_means_no_detections_for_the_page(self):
        (self.work / "22_detected.txt").write_text(inference_server_module._format_detections([]), encoding="utf-8")
        pd.DataFrame({"PAGE": [22], "SCALE_X": [2.0], "SCALE_Y": [2.0], "FRAME_ORG_Y": [1000.0]}) \
            .to_csv(self.work / "product_df.csv", index=False, encoding="cp932")
        df, _, pages = est_mod.create_detected_csv(str(self.work))
        self.assertEqual((len(df), pages), (0, []))


if __name__ == "__main__":
    unittest.main()
