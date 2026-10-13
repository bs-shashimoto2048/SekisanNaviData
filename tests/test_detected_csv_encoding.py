# detected_df.csv の文字コード(CP932・BOMなし)のテスト。一時ディレクトリ内だけで完結し、外部接続はしない。
# SekisanNavi は detected_df.csv を CP932 で読む(app/services/detected_df.py)ため、それに合わせる。
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import estimate_code_processer as est_mod  # noqa: E402

EXPECTED_COLUMNS = [
    "PAGE", "YOLO_INDEX", "SCORE", "DEVICE",
    "LEFT_TOP_X", "LEFT_TOP_Y", "RIGHT_TOP_X", "RIGHT_TOP_Y",
    "LEFT_BOTTOM_X", "LEFT_BOTTOM_Y", "RIGHT_BOTTOM_X", "RIGHT_BOTTOM_Y",
    "CENTER_X", "CENTER_Y",
]
COORDS = "10.0,20.0:30.0,20.0:10.0,40.0:30.0,40.0"
BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")


def _txt(devices):
    return "".join(f"Index${i}|Device${d}|Score$0.9|Coordinates${COORDS}\n" for i, d in enumerate(devices))


class DetectedCsvEncodingTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="sekisan_enc_")
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)
        # create_detected_csv が参照する product_df.csv (cp932)
        pd.DataFrame({"PAGE": [21], "SCALE_X": [2.0], "SCALE_Y": [2.0], "FRAME_ORG_Y": [1000.0]}) \
            .to_csv(self.dir / "product_df.csv", index=False, encoding="cp932")
        self.out = self.dir / "detected_df.csv"

    def _run(self, devices):
        (self.dir / "21_detected.txt").write_text(_txt(devices), encoding="utf-8")
        return est_mod.create_detected_csv(str(self.dir))

    def _read_strict_cp932(self):
        raw = self.out.read_bytes()
        self.assertFalse(raw.startswith(BOMS), "BOM が付いている")
        text = raw.decode("cp932", errors="strict")  # 失敗すれば UnicodeDecodeError
        return raw, list(csv.DictReader(io.StringIO(text, newline="")))

    def test_ascii_class_names_roundtrip_cp932_strict(self):
        devices = ["panel", "transformer", "roof_fan", "sidedoor_l"]
        df, path, pages = self._run(devices)
        self.assertEqual(Path(path), self.out)
        raw, rows = self._read_strict_cp932()
        self.assertEqual([r["DEVICE"] for r in rows], devices)
        self.assertEqual(list(rows[0].keys()), EXPECTED_COLUMNS)  # 列構成は従来どおり
        self.assertTrue(raw.isascii())  # ASCII のみなら従来の UTF-8 出力とバイト列も同一
        self.assertEqual(pages, ["21"])

    def test_japanese_class_name_roundtrip_cp932_strict(self):
        devices = ["panel", "トランス", "換気扇"]
        self._run(devices)
        raw, rows = self._read_strict_cp932()
        self.assertEqual([r["DEVICE"] for r in rows], devices)  # 文字化けせず一致
        # CP932 のバイト列で入っており、UTF-8 のバイト列は入っていない
        self.assertIn("トランス".encode("cp932"), raw)
        self.assertNotIn("トランス".encode("utf-8"), raw)
        self.assertEqual(list(rows[0].keys()), EXPECTED_COLUMNS)

    def test_unencodable_class_name_raises_and_writes_nothing(self):
        # CP932 で表現できない文字(絵文字)。文字化け出力も UTF-8 への切替もせず、明確なエラーにする
        self.out.write_bytes(b"sentinel")
        with self.assertRaises(ValueError) as cm:
            self._run(["panel", "🙂パネル"])
        msg = str(cm.exception)
        self.assertIn("CP932", msg)
        self.assertIn("🙂パネル", msg)
        self.assertIsInstance(cm.exception.__cause__, UnicodeEncodeError)
        self.assertEqual(self.out.read_bytes(), b"sentinel")  # 既存ファイルは変更されない

    def test_unencodable_class_name_does_not_create_file(self):
        self.assertFalse(self.out.exists())
        with self.assertRaises(ValueError):
            self._run(["🙂"])
        self.assertFalse(self.out.exists())

    def test_other_csv_encoding_and_columns_unchanged(self):
        # estcode_df.csv は従来どおり cp932 で、28列
        src = pd.DataFrame({"BAN_MENNO": [1], "BAN_NO": [1], "BAN_MEISYOU": ["テスト盤"], "ZUMEI": ["基礎図"],
                            "KITEN_X": [1.0], "BAN_H1": [2300], "BAN_W": [800], "BAN_D": [1500]})
        _, path = est_mod.create_estcode_df_and_csv("OS2K", src, str(self.dir))
        raw = Path(path).read_bytes()
        text = raw.decode("cp932", errors="strict")
        header = next(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(len(header), 28)
        self.assertEqual(header[:4], ["MODEL", "BAN_MENNO", "BAN_NO", "BAN_MEISYOU"])
        self.assertIn("テスト盤".encode("cp932"), raw)


if __name__ == "__main__":
    unittest.main()
