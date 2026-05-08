"""PDF preprocessing — normalize oversized pages before the takeoff engine sees them.

Background: Render's heavy worker is preempted (`warm shut down requested`) ~2-3
minutes into jobs whose pages are vector-dense (e.g., DD-scale architectural
sheets with 100K+ vector drawings per page). Investigation on 2026-05-08
ruled out memory as the trigger — RSS stayed at 15-17% of plan throughout.
The most likely cause is shared-CPU throttling on sustained high CPU load
during PyMuPDF rendering.

This module rasterizes any page whose single-page-serialized size exceeds a
threshold (default 25 MB) to a JPEG-embedded PDF page at a low DPI.
Pages under threshold are preserved as-is. The result: a normalized PDF
where every page is small and lightweight, eliminating the dense-vector
CPU spike that triggers Render's preemption.

Tradeoff: rasterized pages lose their PDF text layer, so dimension labels
and room IDs become image content rather than searchable text. Claude's
extraction quality degrades on those pages. Mitigation paths (e.g.,
multi-modal text injection) are planned as follow-up work.

Toggle via env var NIGHTSHIFT_DISABLE_PDF_NORMALIZE=1 to bypass entirely.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from typing import Optional

logger = logging.getLogger("nightshift.pdf_preprocess")


def _normalize_disabled() -> bool:
    return os.environ.get("NIGHTSHIFT_DISABLE_PDF_NORMALIZE", "").strip() in ("1", "true", "True")


def _serialize_single_page_mb(reader, page_idx: int) -> float:
    """Write page_idx as a standalone PDF and return its size in MB.

    Uses an in-memory buffer (no temp file) to avoid extra disk IO. The
    measured size is what Anthropic would see if we sent that single page
    natively, so it's the right threshold to gate on.
    """
    from PyPDF2 import PdfWriter
    w = PdfWriter()
    w.add_page(reader.pages[page_idx])
    buf = io.BytesIO()
    w.write(buf)
    return buf.tell() / (1024 * 1024)


def _render_page_to_jpeg(src_page, dpi: int, quality: int,
                         max_dim: int) -> bytes:
    """Render a PyMuPDF page object to JPEG bytes at the given DPI, capped
    at max_dim pixels on the long edge (Anthropic's 8000 px image dimension
    limit with safety margin)."""
    import fitz
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None  # architectural sheets exceed PIL's default cap

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pix = src_page.get_pixmap(matrix=matrix)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    if max(img.size) > max_dim:
        scale = max_dim / max(img.size)
        new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
        img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _build_searchable_page(out_doc, src_page, jpeg_bytes: bytes) -> None:
    """Append a new page to out_doc that visually shows jpeg_bytes (the
    rasterized image of src_page) AND has src_page's original text layer
    embedded as invisible text (PDF render_mode=3).

    Why both: Anthropic's PDF parser reads BOTH rendered images AND embedded
    text streams when processing a PDF. Embedding the original PyMuPDF-
    extracted text as invisible content means dimension labels, room IDs,
    and notes survive the rasterization step losslessly — Claude reads the
    text from the embedded stream while still using the image for spatial
    layout. Verified empirically on 2026-05-08 with a 3-token test PDF
    (VISIBLE_/INVISIBLE_/SECRET_): Claude returned all three.
    """
    import fitz

    new_page = out_doc.new_page(
        width=src_page.rect.width,
        height=src_page.rect.height,
    )
    # Background: the rasterized image of the original page
    new_page.insert_image(new_page.rect, stream=jpeg_bytes)

    # Foreground (invisible): every text span from src_page, at its
    # original position. PDF render_mode=3 means "neither fill nor stroke"
    # — the text is in the PDF stream but never displayed visually.
    text_dict = src_page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:  # 0 = text block; skip image blocks
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                bbox = span.get("bbox", [0, 0, 0, 0])
                # PyMuPDF insert_text point is the BASELINE, lower-left of
                # the text. The bbox we have is (x0, y0_top, x1, y1_bottom)
                # in user space. Use bbox[0] for x and bbox[3] (the bottom
                # in PyMuPDF's coordinate convention) as the baseline y.
                try:
                    new_page.insert_text(
                        fitz.Point(bbox[0], bbox[3]),
                        text,
                        fontsize=max(span.get("size", 8), 4),  # avoid degenerate sizes
                        render_mode=3,  # invisible
                        color=(0, 0, 0),
                    )
                except Exception:
                    # Some unicode glyphs may not be available in the default
                    # font. Skip those spans rather than failing the whole
                    # page — Claude still gets the bulk of the text layer.
                    pass


def normalize_oversized_pages(
    src_path: str,
    dst_path: Optional[str] = None,
    threshold_mb: float = 25.0,
    dpi: int = 150,
    quality: int = 85,
    max_dim: int = 7800,
) -> dict:
    """Rasterize any pages of src whose single-page PDF would exceed threshold_mb.

    Args:
        src_path: input PDF path
        dst_path: output path; if None, derived from src_path with `_normalized` suffix
        threshold_mb: per-page size threshold (single-serialized) above which to rasterize
        dpi: rendering DPI for rasterized pages — lower = faster + smaller but less readable.
            150 is the proven-fast default. Raise to 300 for quality at the cost of
            roughly 4× longer rendering time.
        quality: JPEG quality (1-100). 85 is a good default for line drawings.
        max_dim: cap on the long edge in pixels. Anthropic's image limit is 8000px;
            7800 leaves a safety margin.

    Returns:
        dict with:
            did_normalize (bool): True if any page was rasterized
            src_path (str): unchanged input path
            dst_path (str): output path (== src_path if did_normalize is False)
            pages_normalized (int): count of rasterized pages
            total_pages (int)
            src_size_mb (float)
            dst_size_mb (float)
            threshold_mb / dpi / quality (echoed for log inspection)
    """
    from PyPDF2 import PdfReader, PdfWriter

    src_size_mb = os.path.getsize(src_path) / (1024 * 1024)
    reader = PdfReader(src_path)
    total = len(reader.pages)

    if _normalize_disabled():
        logger.info("normalize_oversized_pages: disabled by env, returning source unchanged")
        return {
            "did_normalize": False, "src_path": src_path, "dst_path": src_path,
            "pages_normalized": 0, "total_pages": total,
            "src_size_mb": src_size_mb, "dst_size_mb": src_size_mb,
            "threshold_mb": threshold_mb, "dpi": dpi, "quality": quality,
        }

    # Walk pages once to identify oversized ones. Single-serialize each page
    # to detect — cheap relative to the actual rendering work that follows.
    print(f"   📐 Scanning {total} pages for oversized content (threshold={threshold_mb} MB/page)...", flush=True)
    oversized = []
    for i in range(total):
        sz = _serialize_single_page_mb(reader, i)
        if sz > threshold_mb:
            oversized.append((i, sz))

    if not oversized:
        print(f"   ✓ No pages exceed {threshold_mb} MB threshold — source is already lean", flush=True)
        return {
            "did_normalize": False, "src_path": src_path, "dst_path": src_path,
            "pages_normalized": 0, "total_pages": total,
            "src_size_mb": src_size_mb, "dst_size_mb": src_size_mb,
            "threshold_mb": threshold_mb, "dpi": dpi, "quality": quality,
        }

    # Default destination: insert _normalized before the extension
    if dst_path is None:
        base, ext = os.path.splitext(src_path)
        dst_path = f"{base}_normalized{ext or '.pdf'}"

    print(f"   📐 Normalizing {len(oversized)}/{total} oversized pages "
          f"at {dpi} DPI, JPEG q={quality} (with invisible-text preservation)...", flush=True)

    # Hybrid assembly:
    # - PyPDF2's PdfWriter is the output container — non-recursive add_page,
    #   never crashes on large multi-page docs
    # - For LEAN pages: writer.add_page(reader.pages[i]) — fast, lossless,
    #   preserves vector text + drawings
    # - For RASTERIZED pages: PyMuPDF builds a single-page PDF in memory
    #   (JPEG + invisible-text-layer via render_mode=3), serialize to bytes,
    #   read into PyPDF2 and append.
    #
    # This avoids PyMuPDF's insert_pdf, which hit a `code=5: exception stack
    # overflow!` on the 583-page Waverly source PDF — likely deeply-nested
    # xref recursion when copying through PyMuPDF on a doc that large.
    import fitz
    src_doc = fitz.open(src_path)
    oversized_set = {idx for idx, _ in oversized}
    text_spans_preserved = 0
    out_writer = PdfWriter()
    try:
        for i in range(total):
            if i in oversized_set:
                src_page = src_doc[i]
                jpeg = _render_page_to_jpeg(
                    src_page, dpi=dpi, quality=quality, max_dim=max_dim,
                )
                # Build a one-page in-memory PDF (JPEG + invisible text)
                tmp_doc = fitz.open()
                try:
                    _build_searchable_page(tmp_doc, src_page, jpeg)
                    page_bytes = tmp_doc.tobytes(garbage=4, deflate=True)
                finally:
                    tmp_doc.close()
                tmp_reader = PdfReader(io.BytesIO(page_bytes))
                out_writer.add_page(tmp_reader.pages[0])
                # Tally spans for the run summary
                td = src_page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
                for blk in td.get("blocks", []):
                    if blk.get("type") != 0:
                        continue
                    for ln in blk.get("lines", []):
                        for sp in ln.get("spans", []):
                            if sp.get("text", "").strip():
                                text_spans_preserved += 1
            else:
                # Lean page — copy through losslessly (preserves vector text + drawings)
                out_writer.add_page(reader.pages[i])
        with open(dst_path, "wb") as f:
            out_writer.write(f)
    finally:
        src_doc.close()

    dst_size_mb = os.path.getsize(dst_path) / (1024 * 1024)
    print(f"   ✓ Normalized: {src_size_mb:.1f} MB → {dst_size_mb:.1f} MB "
          f"({len(oversized)} pages rasterized, {text_spans_preserved} text spans preserved)", flush=True)

    return {
        "did_normalize": True, "src_path": src_path, "dst_path": dst_path,
        "pages_normalized": len(oversized), "total_pages": total,
        "text_spans_preserved": text_spans_preserved,
        "src_size_mb": src_size_mb, "dst_size_mb": dst_size_mb,
        "threshold_mb": threshold_mb, "dpi": dpi, "quality": quality,
    }
