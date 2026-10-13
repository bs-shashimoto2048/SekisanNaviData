r"""合成DWGだけで IJCAD の印刷位置(オフセット 5,5 と C)を比べる手動検証ツール。本番DWG・共有・設計サーバーには触れない。

使い方: .venv\Scripts\python.exe tools\verify_print_centering.py [出力フォルダ] [--no-style]
  出力フォルダの既定は output_test\print_centering(Git 管理外)。ローカルの IJCAD と ODA File Converter を使う(IJCAD の画面が出る)。
  --no-style: 印刷スタイルのテーブル名を「.(なし)」にする。thickness.ctb が無い検証環境用(位置・尺度の比較には影響しない)。

処理:
  1. ezdxf で合成DXFを作る(図枠の位置・縦横比が異なる4種。LIMITS=図枠)→ ODA File Converter で DWG に変換
  2. 本番と同じ印刷スクリプト(print_support.build_script_lines)でオフセットだけを変えて PDF を出力し、IJCAD を終了させる
     (タイムアウト時に終了するのは、このツールが起動した IJCAD のプロセスだけ)
  3. PDF の描画範囲を測り、用紙サイズ・尺度(mm/図面単位)・左右上下の余白を表示。合成DWGの SHA256 が印刷前後で変わらないことも確認
"""
import hashlib
import subprocess
import sys
import time
from pathlib import Path

import ezdxf
import fitz
import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import print_support  # noqa: E402

ODA = r"C:\Program Files\ODA\ODAFileConverter 26.10.0\ODAFileConverter.exe"
GCAD = r"C:\Program Files\ITJP\IJCAD 2024 STD\gcad.exe"
NO_STYLE = "--no-style" in sys.argv
ROOT = Path(next((a for a in sys.argv[1:] if not a.startswith("--")), str(ROOT_DIR / "output_test" / "print_centering")))
TIMEOUT = 300

# 名前: (図枠の左下x, y, 幅, 高さ, 印刷範囲Limitsの左下x, y, 幅, 高さ)
#   A3横に近い / 細長い横で遠い位置 / 縦長で負の座標 / 小さい図枠 / 図枠がLimitsの中で偏っている(Limits=図枠でない場合の確認)
# ※ ODA File Converter は DXF のヘッダ($LIMMIN/$LIMMAX)を DWG に残さないため、Limits は印刷スクリプト内の LIMITS コマンドで設定する。
CASES = {
    "c1_a3_origin": (0, 0, 4200, 2970, 0, 0, 4200, 2970),
    "c2_wide_far": (10000, 5000, 4200, 2000, 10000, 5000, 4200, 2000),
    "c3_portrait": (-3000, 2000, 2000, 4200, -3000, 2000, 2000, 4200),
    "c4_small": (500, 500, 1200, 1500, 500, 500, 1200, 1500),
    "c5_frame_off_center_in_limits": (800, 300, 3000, 2000, 0, 0, 6000, 3000),
}

src, dwg_dir, out_dir = ROOT / "dxf", ROOT / "dwg", ROOT / "pdf"
for d in (src, dwg_dir, out_dir):
    d.mkdir(parents=True, exist_ok=True)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_dwgs():
    for name, (x, y, w, h, *_limits) in CASES.items():
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True, dxfattribs={"color": 7})
        msp.add_line((x, y), (x + w, y + h), dxfattribs={"color": 7})
        doc.saveas(src / f"{name}.dxf")
    subprocess.run([ODA, str(src), str(dwg_dir), "ACAD2018", "DWG", "0", "1"], check=True, capture_output=True, timeout=120)


def write_script(offset, tag) -> Path:
    lines = []
    for name in CASES:
        one = print_support.build_script_lines(str(dwg_dir / f"{name}.dwg"), str(out_dir / f"{name}_{tag}.pdf"), offset)
        lx, ly, lw, lh = CASES[name][4:]
        one[1:1] = ["limits", f"{lx},{ly}", f"{lx + lw},{ly + lh}"]  # open の直後に Limits を設定(保存はしない: close y)
        if NO_STYLE:  # ctb 名を「.(なし)」に置き換える(IJCAD は「スタイルを使用」N でもテーブル名を尋ねるため、Y + "." にする)
            i = one.index("thickness.ctb")
            one[i] = "."
        lines += one
    lines.append("quit y")
    p = ROOT / f"WIRING_{tag}.SCR"
    p.write_text("\r\n".join(lines) + "\r\n", encoding="shift-jis")
    return p


def run(offset, tag):
    scr = write_script(offset, tag)
    t = time.time()
    proc = subprocess.Popen(f'"{GCAD}" /b "{scr}"')
    try:
        proc.wait(TIMEOUT)
        print(f"[{tag}] IJCAD 終了コード={proc.returncode} {time.time() - t:.0f}s")
    except subprocess.TimeoutExpired:
        print(f"[{tag}] タイムアウト({TIMEOUT}s)。起動した IJCAD(PID {proc.pid})だけを終了します")
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"], capture_output=True)


def measure(pdf: Path):
    page = fitz.open(pdf)[0]
    dpi = 200
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    ys, xs = np.where(a.min(axis=2) < 200)
    if len(xs) == 0:
        return None
    mm = 25.4 / dpi
    return dict(paper=(round(page.rect.width * 25.4 / 72), round(page.rect.height * 25.4 / 72)),
                left=xs.min() * mm, right=(pix.width - 1 - xs.max()) * mm, top=ys.min() * mm, bottom=(pix.height - 1 - ys.max()) * mm,
                w=(xs.max() - xs.min()) * mm, h=(ys.max() - ys.min()) * mm)


def detect_frames():
    """PDF を既存の画像化・図枠検出(specified_frame_detection)に通し、検出した図枠と縮尺(DXF幅/画像図枠幅)を表示する(bs_pkg が必要)。
    A3 に近い大きさの図枠だけが対象(検出は「画像の半分以上の長さの線」から矩形を探すため、小さい図枠の合成図面は検出できない)。"""
    import shutil
    from bs_pkg import image_mod
    import cad_data_process as cad
    print("\n図枠検出(既存の画像化→図枠検出・クロップ。A3 に近い合成図面のみ意味がある):")
    for tag in ("old", "center"):
        d = ROOT / "img" / tag
        d.mkdir(parents=True, exist_ok=True)
        pages = []
        for name in CASES:
            if (out_dir / f"{name}_{tag}.pdf").exists():
                shutil.copy(out_dir / f"{name}_{tag}.pdf", d / f"{name}.pdf")
                pages.append(name)
        image_mod.pdf_to_images(str(d), pages)
        image_mod.process_images_from_jpeg(str(d), pages)
        for name in pages:
            try:  # 検出は画像を切り抜いて上書きする(コピーに対して1回だけ実行)
                fd = cad.specified_frame_detection(str(d / f"{name}.png"))
                fw, fh = fd["frame_size"]
                print(f"  {tag:7}{name:32} 図枠(px)={int(fw)}x{int(fh)}  縮尺 DXF幅/画像幅={CASES[name][2] / fw:.4f}")
            except Exception as e:  # noqa: BLE001
                print(f"  {tag:7}{name:32} 検出失敗: {type(e).__name__}")


def main():
    make_dwgs()
    before = {n: sha(dwg_dir / f"{n}.dwg") for n in CASES}
    for offset, tag in (("5,5", "old"), ("C", "center")):
        run(offset, tag)
    print()
    print(f"{'case':14}{'mode':8}{'paper(mm)':>12}{'scale mm/unit':>15}{'drawn w x h':>16}{'L':>7}{'R':>7}{'T':>7}{'B':>7}{'|L-R|':>8}{'|T-B|':>8}")
    for name, (x, y, w, h, *_limits) in CASES.items():
        for tag in ("old", "center"):
            f = out_dir / f"{name}_{tag}.pdf"
            m = measure(f) if f.exists() else None
            if not m:
                print(f"{name:14}{tag:8}  PDFなし/描画なし")
                continue
            scale = m["w"] / w
            print(f"{name:14}{tag:8}{str(m['paper']):>12}{scale:>15.5f}{m['w']:>8.1f}x{m['h']:<7.1f}{m['left']:>7.1f}{m['right']:>7.1f}{m['top']:>7.1f}{m['bottom']:>7.1f}"
                  f"{abs(m['left'] - m['right']):>8.1f}{abs(m['top'] - m['bottom']):>8.1f}")
    after = {n: sha(dwg_dir / f"{n}.dwg") for n in CASES}
    print("\n合成DWGのSHA256 印刷前後:", "変化なし" if before == after else f"変化あり {[n for n in CASES if before[n] != after[n]]}")
    try:
        detect_frames()
    except ImportError as e:
        print("図枠検出はスキップ:", e)


if __name__ == "__main__":
    main()
