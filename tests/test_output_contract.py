# 出力契約テスト(特性化テスト): 製番フォルダに出るファイル名・CSV の列と文字コードを現行のまま固定する。
# SekisanNavi が読み込む CSV/PDF/PNG の仕様は、根拠なく変えない。改修でここが失敗したら、
# 意図した契約変更かどうかを docs/data_pipeline_overview.md の「出力契約」と合わせて確認すること。
# 外部アクセスは test_main_integration と同じガード・モックで 0 件(擬似の設計データ・一時ディレクトリのみ)。
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_main_integration import PAGES, PDF_ONLY_PAGES, PRINT_PAGES, PRODUCT, MainIntegrationBase  # noqa: E402

COMMON_FRAME = ["BAN_MENNO", "BAN_NO", "PAGE", "ZUMEI", "BAN_MEISYOU", "BAN_TYPE"]
DETECT_FRAME = ["DETECT_AREA_X", "DETECT_AREA_Y", "FRAME_ORG_X", "FRAME_ORG_Y", "FRAME_MINI_X", "FRAME_MINI_Y", "SCALE_X", "SCALE_Y"]
PRODUCT_DF_COLUMNS = COMMON_FRAME + ["BAN_H1", "BAN_H2", "BAN_W", "BAN_D", "KITEN_X", "KITEN_Y"] + DETECT_FRAME

# CSV 名 -> 列(順序も契約)
EXPECTED_COLUMNS = {
    f"{PRODUCT}_df.csv": ["読込ファイル名", "BAN_MENNO", "BAN_NO", "PAGE", "ZUMEI", "BAN_MEISYOU", "BAN_TYPE",
                          "BAN_H1", "BAN_H2", "BAN_W", "BAN_D", "KITEN_X", "KITEN_Y"],
    f"{PRODUCT}_df_cubicle.csv": PRODUCT_DF_COLUMNS,
    f"{PRODUCT}_df_foundation.csv": COMMON_FRAME + ["KITEN_X", "KITEN_Y"] + DETECT_FRAME,
    f"{PRODUCT}_df_outline.csv": PRODUCT_DF_COLUMNS,
    "product_df.csv": PRODUCT_DF_COLUMNS,
    "detected_df.csv": ["PAGE", "YOLO_INDEX", "SCORE", "DEVICE", "LEFT_TOP_X", "LEFT_TOP_Y", "RIGHT_TOP_X", "RIGHT_TOP_Y",
                        "LEFT_BOTTOM_X", "LEFT_BOTTOM_Y", "RIGHT_BOTTOM_X", "RIGHT_BOTTOM_Y", "CENTER_X", "CENTER_Y"],
    "estcode_df.csv": ["MODEL", "BAN_MENNO", "BAN_NO", "BAN_MEISYOU", "BAN_H", "BAN_W", "BAN_D", "BAN_CONNECT",
                       "PANEL", "TRANS", "IN_PANEL", "SHIELD", "DOOR_FRONT", "DOOR_BACK", "DOOR_STACK", "DOOR_SIDE",
                       "DOOR_SMALL", "FAN_ROOF", "FAN_DOOR", "MAIN_LINE", "WIRE_MESH", "STACK_PLATE", "DRAWER_DEVICE",
                       "VCT_STAND", "BUS_DUCT", "PASSAGE", "INPUT_CU_COEFF", "SORT_ORDER"],
}


class OutputContractTest(MainIntegrationBase):
    def setUp(self):
        super().setUp()
        self.run_main()
        self.out = self.product_out

    def test_file_set_of_a_successful_product(self):
        expected = set(EXPECTED_COLUMNS) | {"dwg"}
        for p in PAGES:
            expected |= {f"{p}.pdf", f"{p}.jpeg", f"{p}.png", f"{p}.dxf", f"{p}_detected.txt"}
        for p in PDF_ONLY_PAGES:
            expected.add(f"{p}.pdf")  # 推論対象外は PDF だけ
        self.assertEqual({f.name for f in self.out.iterdir()}, expected)
        self.assertEqual(sorted(f.stem for f in self.out.glob("*.pdf")), PRINT_PAGES)
        self.assertEqual(sorted(f.name for f in (self.out / "dwg").iterdir()), [f"{p}.dwg" for p in PAGES])

    def test_csv_columns_and_order(self):
        for name, columns in EXPECTED_COLUMNS.items():
            df = pd.read_csv(self.out / name, encoding="cp932")
            self.assertEqual(list(df.columns), columns, name)

    def test_csv_are_cp932_without_bom(self):
        # SekisanNavi は CP932 で読む。BOM なし。UTF-8 としては読めない日本語を含むファイルがあること自体が契約。
        for name in EXPECTED_COLUMNS:
            raw = (self.out / name).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{name}: BOM あり")
            raw.decode("cp932")  # 例外にならない
        for name in (f"{PRODUCT}_df.csv", "product_df.csv", "estcode_df.csv"):
            with self.assertRaises(UnicodeDecodeError, msg=name):
                (self.out / name).read_bytes().decode("utf-8")

    def test_detected_txt_is_utf8_lines_of_index_device_score_coordinates(self):
        for p in PAGES:
            for line in (self.out / f"{p}_detected.txt").read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue  # 検出0件のページは空
                keys = [kv.split("$", 1)[0] for kv in line.split("|")]
                self.assertEqual(keys, ["Index", "Device", "Score", "Coordinates"], line)

    def test_pdf_png_naming_follows_page_name(self):
        for p in PAGES:
            self.assertEqual((self.out / f"{p}.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual((self.out / f"{p}.pdf").read_bytes()[:5], b"%PDF-")
            self.assertEqual((self.out / f"{p}.jpeg").read_bytes()[:3], b"\xff\xd8\xff")


if __name__ == "__main__":
    unittest.main()
