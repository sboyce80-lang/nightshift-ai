#!/usr/bin/env python3
"""normalize_pdf.py — pre-process pathologically-large PDF pages.

For PDFs where individual pages serialize to > THRESHOLD MB (e.g. dense
DD-scale architectural sheets), re-render those pages as JPEG images
embedded in a new single-page PDF. Pages under the threshold are kept
as-is.

The output PDF is functionally equivalent for the takeoff pipeline:
- Same page count, same page order
- Each oversized page becomes a rasterized image (lossy) at LOW_DPI
- All other pages preserved exactly

This makes downstream chunking + native-PDF API mode tractable: every
page is under Anthropic's 32 MB request limit.

Usage:
    python3 scripts/normalize_pdf.py SRC.pdf [--out OUT.pdf]
                                              [--threshold-mb 25]
                                              [--low-dpi 150]
                                              [--quality 85]
                                              [--report]
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image
from PyPDF2 import PdfReader, PdfWriter

Image.MAX_IMAGE_PIXELS = None  # architectural sheets exceed PIL's default cap


def _serialize_single_page_mb(pdf_path: str, page_idx: int) -> float:
    """Write page_idx to a temp single-page PDF, return its size in MB."""
    reader = PdfReader(pdf_path)
    w = PdfWriter()
    w.add_page(reader.pages[page_idx])
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        w.write(tmp)
        tmp.flush()
        return os.path.getsize(tmp.name) / (1024 * 1024)


def _render_page_to_jpeg(
    pdf_path: str, page_idx: int, dpi: int, quality: int
) -> bytes:
    """Render page_idx of pdf_path at given DPI, return JPEG bytes."""
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_idx]
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        # Cap dimensions at Claude's 8000 px image limit (preserve aspect)
        MAX_DIM = 7800
        if max(img.size) > MAX_DIM:
            scale = MAX_DIM / max(img.size)
            new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
            img = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    finally:
        doc.close()


def _jpeg_to_pdf_page_bytes(jpeg_bytes: bytes) -> bytes:
    """Wrap a JPEG into a single-page PDF preserving aspect ratio."""
    img = Image.open(io.BytesIO(jpeg_bytes))
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150.0)  # PIL embeds the JPEG losslessly into PDF
    return buf.getvalue()


def normalize_pdf(
    src: Path,
    out: Path,
    threshold_mb: float,
    low_dpi: int,
    quality: int,
    verbose: bool = True,
) -> dict:
    """Walk pages, swap out oversized ones, write normalized PDF.

    Returns {pages, oversized_count, swapped: [{idx, before_mb, after_mb}, ...]}.
    """
    src_path = str(src)
    src_reader = PdfReader(src_path)
    total = len(src_reader.pages)

    if verbose:
        print(f"Source: {src_path}  ({total} pages)")
        print(f"Threshold: {threshold_mb} MB/page")
        print(f"Re-render dense pages at: {low_dpi} DPI, JPEG q={quality}")

    out_writer = PdfWriter()
    swapped = []

    for i in range(total):
        size_mb = _serialize_single_page_mb(src_path, i)
        if size_mb > threshold_mb:
            jpeg = _render_page_to_jpeg(src_path, i, dpi=low_dpi, quality=quality)
            new_pdf_bytes = _jpeg_to_pdf_page_bytes(jpeg)
            new_reader = PdfReader(io.BytesIO(new_pdf_bytes))
            out_writer.add_page(new_reader.pages[0])
            new_size_mb = len(new_pdf_bytes) / (1024 * 1024)
            swapped.append({"idx": i, "before_mb": size_mb, "after_mb": new_size_mb})
            if verbose:
                print(f"  page {i+1:>4}: {size_mb:>6.1f} MB → {new_size_mb:>5.2f} MB (rasterized)")
        else:
            out_writer.add_page(src_reader.pages[i])
            if verbose and (i + 1) % 50 == 0:
                print(f"  page {i+1:>4}: {size_mb:>6.1f} MB (kept)")

    out_path = str(out)
    with open(out_path, "wb") as f:
        out_writer.write(f)

    out_size_mb = os.path.getsize(out_path) / (1024 * 1024)
    src_size_mb = os.path.getsize(src_path) / (1024 * 1024)
    if verbose:
        print(f"\n✓ Wrote {out_path}")
        print(f"  Pages: {total}  |  Swapped: {len(swapped)}")
        print(f"  Size: {src_size_mb:.1f} MB → {out_size_mb:.1f} MB "
              f"(reduction: {100 * (1 - out_size_mb / src_size_mb):.0f}%)")

    return {
        "total_pages": total,
        "oversized_count": len(swapped),
        "swapped": swapped,
        "src_size_mb": src_size_mb,
        "out_size_mb": out_size_mb,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("src", type=Path)
    p.add_argument("--out", type=Path, default=None,
                   help="Output path (default: <src_stem>_normalized.pdf next to source)")
    p.add_argument("--threshold-mb", type=float, default=25.0,
                   help="Re-render any page whose single-page PDF exceeds this size")
    p.add_argument("--low-dpi", type=int, default=150,
                   help="DPI for rasterizing oversized pages")
    p.add_argument("--quality", type=int, default=85,
                   help="JPEG quality for rasterized pages (1-100)")
    p.add_argument("--report", action="store_true",
                   help="Just analyze + report; do not write output")
    args = p.parse_args()

    if not args.src.exists():
        print(f"ERROR: not found: {args.src}", file=sys.stderr)
        sys.exit(1)

    out = args.out or args.src.with_name(f"{args.src.stem}_normalized.pdf")

    if args.report:
        # Just count oversized pages
        reader = PdfReader(str(args.src))
        total = len(reader.pages)
        n_over = 0
        max_mb = 0.0
        for i in range(total):
            sz = _serialize_single_page_mb(str(args.src), i)
            if sz > args.threshold_mb:
                n_over += 1
            max_mb = max(max_mb, sz)
        print(f"{args.src.name}: {total} pages, {n_over} oversized (>{args.threshold_mb}MB), "
              f"max page size = {max_mb:.1f} MB")
        return

    normalize_pdf(args.src, out, args.threshold_mb, args.low_dpi, args.quality)


if __name__ == "__main__":
    main()
