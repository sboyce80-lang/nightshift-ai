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


def _render_page_to_jpeg(pdf_path: str, page_idx: int, dpi: int,
                         quality: int, max_dim: int) -> bytes:
    """Render a PDF page to JPEG bytes at the given DPI, capped at max_dim
    pixels on the long edge (to stay under Anthropic's 8000 px image
    dimension limit with safety margin)."""
    import fitz
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None  # architectural sheets exceed PIL's default cap

    doc = fitz.open(pdf_path)
    try:
        page = doc[page_idx]
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        if max(img.size) > max_dim:
            scale = max_dim / max(img.size)
            new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
            img = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    finally:
        doc.close()


def _jpeg_to_pdf_page_bytes(jpeg_bytes: bytes) -> bytes:
    """Embed a JPEG into a single-page PDF, preserving aspect ratio."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    img = Image.open(io.BytesIO(jpeg_bytes))
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150.0)
    return buf.getvalue()


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
          f"at {dpi} DPI, JPEG q={quality}...", flush=True)

    out_writer = PdfWriter()
    oversized_set = {idx for idx, _ in oversized}
    for i in range(total):
        if i in oversized_set:
            jpeg = _render_page_to_jpeg(src_path, i, dpi=dpi, quality=quality, max_dim=max_dim)
            new_pdf_bytes = _jpeg_to_pdf_page_bytes(jpeg)
            new_reader = PdfReader(io.BytesIO(new_pdf_bytes))
            out_writer.add_page(new_reader.pages[0])
        else:
            out_writer.add_page(reader.pages[i])

    with open(dst_path, "wb") as f:
        out_writer.write(f)

    dst_size_mb = os.path.getsize(dst_path) / (1024 * 1024)
    print(f"   ✓ Normalized: {src_size_mb:.1f} MB → {dst_size_mb:.1f} MB "
          f"({len(oversized)} pages rasterized)", flush=True)

    return {
        "did_normalize": True, "src_path": src_path, "dst_path": dst_path,
        "pages_normalized": len(oversized), "total_pages": total,
        "src_size_mb": src_size_mb, "dst_size_mb": dst_size_mb,
        "threshold_mb": threshold_mb, "dpi": dpi, "quality": quality,
    }
