"""PDF-only図面・書類の表示用PNGを作る。

推論PNG(`{ページ}.png`)とは別名(`{PDF名}__preview.png`)で保存し、
推論画像や元PDFを変更しない。クロップはPDF内の描画・文字の外接範囲に余白を足し、
内容範囲を取得できない場合はページ全体を使う。
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Callable, Iterable

import fitz
from PIL import Image

PREVIEW_SUFFIX = "__preview.png"
RENDER_DPI = 150
CONTENT_MARGIN_PT = 18.0  # 150dpiで約38px。薄い線・寸法線の外側に余白を残す


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
            render_pdf_preview(resolved_source, root / target_name)
            targets.append(target_name)
    except DisplayPreviewError:
        raise
    except Exception as exc:  # noqa: BLE001 - 呼び出し側には例外本文やパスを渡さない
        raise DisplayPreviewError(type(exc).__name__) from None
    return targets


def _safe_content_clip(page: fitz.Page) -> fitz.Rect:
    """PDFの可視描画・文字の外接範囲に余白を足したclip。検出できない場合はページ全体。"""
    page_rect = page.rect
    content: fitz.Rect | None = None
    try:
        for _kind, bbox in page.get_bboxlog():
            rect = fitz.Rect(bbox)
            if rect.is_empty or rect.is_infinite:
                continue
            content = rect if content is None else content | rect
    except Exception:  # noqa: BLE001 - bbox取得失敗は安全側に全ページ描画
        return page_rect
    if content is None:
        return page_rect
    content = (content + (-CONTENT_MARGIN_PT, -CONTENT_MARGIN_PT,
                          CONTENT_MARGIN_PT, CONTENT_MARGIN_PT)) & page_rect
    return content if not content.is_empty else page_rect


def render_pdf_preview(pdf_path: str | os.PathLike[str], png_path: str | os.PathLike[str]) -> None:
    """PDFの先頭ページを最大150dpiで描画し、PNGを上書きせずに保存する。

    仕様書PDFが複数ページでも、一覧カード用画像は先頭ページだけを描画する。
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
            page = document.load_page(0)
            scale = RENDER_DPI / 72.0
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                clip=_safe_content_clip(page),
                alpha=False,
                annots=True,
            )
            png_bytes = pixmap.tobytes("png")
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
