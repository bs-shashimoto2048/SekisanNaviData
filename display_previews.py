"""PDF-only図面・書類の表示用PNGを作る。

推論PNG(`{ページ}.png`)とは別名(`{PDF名}__preview.png`)で保存し、
推論画像や元PDFを変更しない。

クロップ(表示用PNGだけ): ページを描画した画素から、印刷内容(図枠・タイトル欄・印字・寸法線)の範囲を
ページごとに検出し、その周囲に上下左右で等しい余白を付ける(内容が用紙端に近いときは白で足す)。
孤立した小さなノイズは範囲に含めない。白紙・図枠を検出できないページは、切り詰めずページ全体を使い、状況をログに残す。
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Callable, Iterable

import cv2
import fitz
import numpy as np
from PIL import Image

PREVIEW_SUFFIX = "__preview.png"
RENDER_DPI = 150

# --- 内容範囲の検出(150dpiの画素で判定。値は画素数) ---
INK_THRESHOLD = 247       # RGBの最小チャンネルがこれ未満なら「描画あり」。薄い線・薄い色も拾う(紙の白は255)
JOIN_KERNEL_PX = 15       # 近接する描画(文字の筆画・点線)を1つの塊にまとめる膨張幅(約2.5mm)
NOISE_MAX_INK_PX = 40     # 描画画素がこの数未満で
NOISE_MAX_EXTENT_PX = 24  # かつ縦横の広がりがこの値未満の孤立した塊はノイズとして範囲に含めない
FRAME_MIN_FRACTION = 0.35  # 内容範囲が用紙の幅・高さのこの割合以上なら、図枠(または図面全体)を検出できたとみなす
MARGIN_MIN_PX = 30        # 上下左右に等しく付ける余白(内容範囲の長辺の割合と、この下限のうち大きい方)
MARGIN_FRACTION = 0.025
MAX_RENDER_LONG_SIDE_PX = 8000  # 極端に大きな用紙でも、描画のメモリを抑える上限(超えるときだけ解像度を下げる)

STATUS_CROPPED = "cropped"
STATUS_BLANK = "blank"
STATUS_NO_FRAME = "no_frame"


class DisplayPreviewError(Exception):
    """パスやPDF本文を含まない、表示用PNG作成失敗。"""


def preview_filename(pdf_path: str | os.PathLike[str]) -> str:
    return f"{Path(pdf_path).stem}{PREVIEW_SUFFIX}"


def create_product_previews(
    product_dir: str | os.PathLike[str],
    print_pages: Iterable[str],
    inference_pages: Iterable[str],
    page_pdf_name: Callable[[str], str],
    spec_pdf_name: str,
    check_sheet_pdf_name: str,
    log: Callable[[str], None] | None = None,
    warn: Callable[[str], None] | None = None,
) -> list[str]:
    """PDF-only図面と存在する固定書類のプレビューを生成し、作成対象名を返す。

    推論対象ページのPNGには触れない。設計作図完了シートは、ページ「0」が推論対象でも
    書類カード用に生成する。PDFとPNGは上書きしない。
    """
    root = Path(product_dir).resolve(strict=False)
    inference = {str(page) for page in inference_pages}
    pdf_names = [page_pdf_name(str(page)) for page in print_pages if str(page) not in inference]
    for document_name in (spec_pdf_name, check_sheet_pdf_name):
        if (root / document_name).is_file():
            pdf_names.append(document_name)
    targets: list[str] = []
    try:
        for pdf_name in dict.fromkeys(pdf_names):
            source = root / pdf_name
            if source.is_symlink():
                raise DisplayPreviewError("source_link")
            resolved_source = source.resolve(strict=False)
            if resolved_source.parent != root or not resolved_source.is_file():
                raise DisplayPreviewError("source_missing")
            target_name = preview_filename(pdf_name)
            render_pdf_preview(resolved_source, root / target_name, log=log, warn=warn)
            targets.append(target_name)
    except DisplayPreviewError:
        raise
    except Exception as exc:  # noqa: BLE001 - 呼び出し側には例外本文やパスを渡さない
        raise DisplayPreviewError(type(exc).__name__) from None
    return targets


def detect_content_box(rgb: np.ndarray) -> tuple[tuple[int, int, int, int] | None, str]:
    """描画した画素(H×W×3, uint8)から、印刷内容の範囲(x0, y0, x1, y1。x1・y1は含まない)を返す。

    - 描画画素: RGBの最小チャンネルが INK_THRESHOLD 未満(薄い線・薄い色も含む)。
    - 近接する描画をまとめ、画素数も広がりも小さい孤立した塊(ノイズ)は範囲に含めない。
    - 範囲が用紙の幅・高さの FRAME_MIN_FRACTION 未満なら、図枠を検出できないとして None(全体表示に戻す)。
    戻り値の2番目は状況(cropped / blank / no_frame)。
    """
    height, width = rgb.shape[:2]
    ink = rgb.min(axis=2) < INK_THRESHOLD
    if not ink.any():
        return None, STATUS_BLANK
    joined = cv2.dilate(ink.astype(np.uint8), np.ones((JOIN_KERNEL_PX, JOIN_KERNEL_PX), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    ink_pixels = np.bincount(labels[ink], minlength=count)
    # 膨張した塊の外接矩形から、膨張で広がった分を引いて、描画自体の広がりを見積もる
    extent = np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]) - (JOIN_KERNEL_PX - 1)
    keep = (ink_pixels >= NOISE_MAX_INK_PX) | (extent >= NOISE_MAX_EXTENT_PX)
    keep[0] = False  # 背景
    mask = ink & keep[labels]
    if not mask.any():
        return None, STATUS_BLANK
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    x0, x1, y0, y1 = int(cols[0]), int(cols[-1]) + 1, int(rows[0]), int(rows[-1]) + 1
    if (x1 - x0) < FRAME_MIN_FRACTION * width or (y1 - y0) < FRAME_MIN_FRACTION * height:
        return None, STATUS_NO_FRAME
    return (x0, y0, x1, y1), STATUS_CROPPED


def crop_with_equal_margin(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """内容範囲 box の上下左右に、等しい余白を付けた画像を返す。余白が用紙の外にはみ出す分は白で足す。"""
    x0, y0, x1, y1 = box
    margin = max(MARGIN_MIN_PX, round(MARGIN_FRACTION * max(x1 - x0, y1 - y0)))
    out = Image.new("RGB", (x1 - x0 + 2 * margin, y1 - y0 + 2 * margin), (255, 255, 255))
    out.paste(image.crop((x0, y0, x1, y1)), (margin, margin))
    return out


def _render_page(page: fitz.Page) -> Image.Image:
    scale = RENDER_DPI / 72.0
    long_side_px = max(page.rect.width, page.rect.height) * scale
    if long_side_px > MAX_RENDER_LONG_SIDE_PX:
        scale *= MAX_RENDER_LONG_SIDE_PX / long_side_px
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False, annots=True)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def render_pdf_preview(
    pdf_path: str | os.PathLike[str],
    png_path: str | os.PathLike[str],
    log: Callable[[str], None] | None = None,
    warn: Callable[[str], None] | None = None,
) -> None:
    """PDFの先頭ページを最大150dpiで描画し、内容範囲に等しい余白を付けたPNGを、上書きせずに保存する。

    ページごとに内容範囲を検出する(別の図面・別のページの範囲は流用しない)。白紙・図枠を検出できないページは
    ページ全体を保存し、状況(`log`)を残す。仕様書PDFが複数ページでも、一覧カード用画像は先頭ページだけを描画する。
    PDF本体の閲覧は既存のPDF経路を維持する。
    """
    source = Path(pdf_path)
    target = Path(png_path)
    created_target = False
    if target.exists() or target.is_symlink():
        raise DisplayPreviewError("target_exists")
    try:
        with fitz.open(source) as document:
            if not document.is_pdf or document.page_count < 1:
                raise DisplayPreviewError("invalid_pdf")
            page_image = _render_page(document.load_page(0))
        box, status = detect_content_box(np.asarray(page_image))
        output = crop_with_equal_margin(page_image, box) if box is not None else page_image
        if status == STATUS_CROPPED:
            if log is not None:
                log(f"[表示用PNG] {source.name}: 内容範囲でクロップしました({page_image.width}x{page_image.height} -> {output.width}x{output.height})")
        else:   # 切り詰めずページ全体を使った場合は、警告として残す(`warn` が無ければ `log`)
            reason = "白紙(描画を検出できません)" if status == STATUS_BLANK else "図枠・内容範囲を検出できません"
            (warn or log or (lambda _message: None))(f"[表示用PNG] {source.name}: {reason}のため、ページ全体を使いました")
        buffer = io.BytesIO()
        output.save(buffer, format="PNG")
        png_bytes = buffer.getvalue()
        with Image.open(io.BytesIO(png_bytes)) as image:
            image.verify()
        target.parent.mkdir(parents=False, exist_ok=True)
        # xbで作成し、既存ファイルの上書きをOSレベルで防ぐ。
        with target.open("xb") as stream:
            created_target = True
            stream.write(png_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    except DisplayPreviewError:
        raise
    except Exception as exc:  # noqa: BLE001 - 呼び出し側へ本文・パスを渡さない
        try:
            if created_target and target.is_file():
                target.unlink()
        except OSError:
            pass
        raise DisplayPreviewError(type(exc).__name__) from None
