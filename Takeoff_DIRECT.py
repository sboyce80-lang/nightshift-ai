#!/usr/bin/env python3
"""
Nightshift AI - Construction Document Analyzer (No Poppler Required)
====================================================================
Sends PDF directly to Claude for analysis
Extracts measurements from architectural drawings
Works without image conversion libraries

Usage:
    Single file:
        python3 Takeoff_DIRECT.py --rfp_file "file.pdf" --contact_name "Name" --contact_email "email"

    Folder of split PDFs (same project):
        python3 Takeoff_DIRECT.py --rfp_dir "/path/to/folder/" --contact_name "Name" --contact_email "email"
"""

import sys
import json
import glob
import math
import re
import io
import tempfile
import time
import hashlib
from pathlib import Path
import PyPDF2
from config import CLAUDE_API_KEY, PRICING_MODEL, SMALL_COMMERCIAL_RATES, PCA_CONSTANTS, HARD_NUMBERS_ONLY
from will_synthesis import run_will_synthesis
import anthropic
import base64
from datetime import datetime
import os


# Multi-modal extraction toggle. When True (default), pages that fail native
# PDF mode with a "Request exceeds the maximum size" (413) error are retried
# via _multimodal_chunk_retry — Claude gets a rendered JPEG of each page plus
# the PyMuPDF-extracted text layer in a single API call. Set
# NIGHTSHIFT_DISABLE_MULTIMODAL=1 in the env to fall back to the legacy
# per-page validation retry only.
_MULTIMODAL_DENSE_PAGES_ENABLED = (
    os.environ.get("NIGHTSHIFT_DISABLE_MULTIMODAL", "").strip() not in ("1", "true", "True")
)


class TruncatedResponseError(Exception):
    """The model's response hit max_tokens and was cut off mid-output.

    Before this existed, truncated chunk responses were recorded as
    "succeeded" and silently died at JSON-parse time inside the merge —
    an entire chunk's floors vanished with no log, no chunks_failed entry,
    and no manual-review flag. Raised by the streaming call sites when
    stop_reason == "max_tokens" so the chunk-failure ladder can route the
    chunk to page-level retry (single pages produce small responses that
    cannot truncate). Resending the identical chunk is pointless at
    temperature 0 — it truncates again.
    """


def _release_memory(label=""):
    """Force the Python heap and the C allocator to return freed memory to
    the OS at a phase boundary.

    Investigation of the 2026-05-08 Waverly Part 1B preemption pattern
    showed the worker dying after roughly 2 minutes despite peak transient
    memory of only ~1.2 GB on an 8 GB plan — i.e. allocator fragmentation,
    not absolute usage. Python's gc.collect() reclaims unreferenced objects
    but glibc's malloc keeps freed pages in the process heap by default.
    `malloc_trim(0)` instructs glibc to release as many free pages as
    possible back to the kernel, dropping RSS without affecting
    application state. Logs the RSS before/after so we can SEE whether
    each phase boundary is actually shedding memory.
    """
    import gc
    import resource
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KB on Linux
    gc.collect()
    try:
        import ctypes
        # Linux glibc: returns 1 if memory was released, 0 if none could be
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass  # Not on glibc (macOS dev box) — gc.collect() alone has to suffice
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tag = f" [{label}]" if label else ""
    print(f"   🧹 mem-release{tag}: RSS peak {rss_before/1024:.0f} MB "
          f"(maxrss; not pre/post — Linux ru_maxrss is monotonic)", flush=True)


# Silence MuPDF's xref/format warnings on the console and drain them to
# logs/mupdf.log instead. PyMuPDF emits these to stdout via C, so a Python-level
# stderr redirect won't help — disable display and drain the internal buffer.
try:
    import fitz as _fitz_log_setup
    import atexit as _atexit
    _fitz_log_setup.TOOLS.mupdf_display_errors(False)
    _MUPDF_LOG_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'logs', 'mupdf.log'
    )
    os.makedirs(os.path.dirname(_MUPDF_LOG_PATH), exist_ok=True)

    def _drain_mupdf_warnings():
        try:
            msgs = _fitz_log_setup.TOOLS.mupdf_warnings(reset=True)
            if msgs:
                with open(_MUPDF_LOG_PATH, 'a') as _f:
                    _f.write(f"\n=== {datetime.now().isoformat()} pid={os.getpid()} ===\n")
                    _f.write(msgs)
                    if not msgs.endswith('\n'):
                        _f.write('\n')
        except Exception:
            pass

    _atexit.register(_drain_mupdf_warnings)
except Exception:
    pass


# ---------------------------------------------------------------------------
# Job progress tracking (for Streamlit UI)
# ---------------------------------------------------------------------------
_PROGRESS_FILE = None  # Set by run_analysis() when called from Streamlit

def _update_progress(step, total_steps, label, detail="", pct=None):
    """Write progress to a JSON file so the Streamlit UI can display it."""
    if not _PROGRESS_FILE:
        return
    try:
        progress = {
            "step": step,
            "total_steps": total_steps,
            "label": label,
            "detail": detail,
            "pct": pct if pct is not None else round(step / total_steps * 100),
            "updated": datetime.now().isoformat(),
        }
        with open(_PROGRESS_FILE, "w") as f:
            json.dump(progress, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Unit-level templates for multi-family residential estimation
# ---------------------------------------------------------------------------
# Each unit type defines: wall_sqft, ceiling_sqft, doors, trim_lf, windows
# Derived from Edgehill validated data (24 units, $107K) and ROOM_TYPE_ESTIMATES
UNIT_TEMPLATES = {
    "studio":  {"wall_sqft": 900,  "ceiling_sqft": 400,  "doors": 3, "trim_lf": 90,  "windows": 2},
    "1br":     {"wall_sqft": 1600, "ceiling_sqft": 650,  "doors": 5, "trim_lf": 150, "windows": 3},
    "2br":     {"wall_sqft": 2200, "ceiling_sqft": 900,  "doors": 7, "trim_lf": 210, "windows": 5},
    "3br":     {"wall_sqft": 2800, "ceiling_sqft": 1100, "doors": 9, "trim_lf": 270, "windows": 6},
}
# Default unit mix when specific breakdown is unavailable
UNIT_MIX_DEFAULT = {"studio": 0.25, "1br": 0.40, "2br": 0.25, "3br": 0.10}

# Secondary space templates — estimated dimensions for closets, halls, and entries
# that the AI consistently misses during floor plan extraction.
# Wall sqft assumes 9.5ft ceiling height × perimeter, minus door openings.
SECONDARY_SPACE_TEMPLATES = {
    "closet":         {"wall_sqft": 190, "ceiling_sqft": 24,  "trim_lf": 20, "doors": 1},
    "walk_in_closet": {"wall_sqft": 266, "ceiling_sqft": 48,  "trim_lf": 28, "doors": 1},
    "entry_hall":     {"wall_sqft": 228, "ceiling_sqft": 48,  "trim_lf": 28, "doors": 0},
    "unit_hall":      {"wall_sqft": 380, "ceiling_sqft": 80,  "trim_lf": 40, "doors": 0},
}

# Expected total rooms and secondary space breakdown per unit type.
# Primary rooms (LDK, bed, bath) are almost always extracted; secondary
# spaces (closets, halls, entries) are what Claude consistently misses.
EXPECTED_ROOMS_PER_UNIT = {
    "studio": {"total_rooms": 4,  "secondary": [("closet", 1), ("entry_hall", 1)]},
    "1br":    {"total_rooms": 7,  "secondary": [("closet", 2), ("walk_in_closet", 1), ("entry_hall", 1)]},
    "2br":    {"total_rooms": 9,  "secondary": [("closet", 2), ("walk_in_closet", 1), ("entry_hall", 1), ("unit_hall", 1)]},
    "3br":    {"total_rooms": 11, "secondary": [("closet", 3), ("walk_in_closet", 1), ("entry_hall", 1), ("unit_hall", 1)]},
}

# Footprint-based estimation constants. Two efficiency values are kept because
# the painter's scope of work determines which one applies:
#
#  * RESIDENTIAL_EFFICIENCY_UNITS_ONLY (0.63) — apartments only, commons
#    NOT painted. Original Rider Painting / Chestnut calibration, valid for
#    projects where corridors, lobbies, common rooms are out of scope.
#
#  * RESIDENTIAL_EFFICIENCY_FULL_INTERIOR (0.97) — apartments PLUS painted
#    commons (corridors, lobbies, common rooms, common baths, laundry).
#    Validated against KonstructIQ's Ridgeview takeoff (42,923 SF ceiling =
#    100% of GSF since Rider painted all commons there) and Rider's manual
#    measurement of 42,900 SF on the same project. Use this for supportive
#    housing, dorms, and projects whose finish schedule shows painted GYP
#    on corridor/lobby/common-area ceilings.
#
# The default is FULL_INTERIOR because (a) most NY supportive housing /
# multifamily projects we see do paint commons, and (b) the failure mode of
# the FULL value being wrong (bid runs ~30% high, gets adjusted in review)
# is recoverable, while the failure mode of UNITS_ONLY being wrong
# (Ridgeview's 24% under-bid, hard to detect from the proposal alone) is
# not. Per-org override via project_info['_residential_efficiency'] or
# pricing_overrides.residential_efficiency.
RESIDENTIAL_EFFICIENCY_UNITS_ONLY = 0.63
RESIDENTIAL_EFFICIENCY_FULL_INTERIOR = 0.97
RESIDENTIAL_EFFICIENCY = RESIDENTIAL_EFFICIENCY_UNITS_ONLY  # legacy alias
                                                            # (footprint
                                                            # fallback only)
# Wall sqft per ceiling/floor sqft — accounts for interior partitions
# Wall sqft per ceiling/floor sqft — accounts for interior partitions
# (closets, bathrooms, hallways within units).
WALL_TO_FLOOR_RATIO = 3.3


# ---------------------------------------------------------------------------
# Caching system — deterministic results + instant re-runs
# ---------------------------------------------------------------------------

CACHE_DIR = Path(__file__).parent / ".cache" / "pdfs"


def _pdf_hash(pdf_path):
    """SHA256 of PDF file content — used as cache key."""
    h = hashlib.sha256()
    with open(pdf_path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def _code_hash():
    """Hash of Takeoff_DIRECT.py + config.py — invalidates cache when code changes."""
    h = hashlib.sha256()
    for fp in [Path(__file__), Path(__file__).parent / "config.py"]:
        try:
            h.update(fp.read_bytes())
        except FileNotFoundError:
            pass
    return h.hexdigest()


def _cache_dir_for(pdf_path):
    """Return (cache_dir Path, pdf_hash string) for a given PDF."""
    ph = _pdf_hash(pdf_path)
    d = CACHE_DIR / ph
    return d, ph


def _cache_valid(cache_dir):
    """Check if cache exists and was generated with the current code version."""
    meta_path = cache_dir / "metadata.json"
    if not meta_path.exists():
        return False
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        return meta.get("code_hash") == _code_hash()
    except (json.JSONDecodeError, OSError):
        return False


def _init_cache(cache_dir, pdf_path, pdf_hash):
    """Create cache directory and write metadata."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "chunks").mkdir(exist_ok=True)
    meta = {
        "pdf_path": str(pdf_path),
        "pdf_hash": pdf_hash,
        "file_size_bytes": os.path.getsize(pdf_path),
        "code_hash": _code_hash(),
        "created_at": datetime.utcnow().isoformat(),
    }
    with open(cache_dir / "metadata.json", 'w') as f:
        json.dump(meta, f, indent=2)
    return meta


def _save_cache(cache_dir, filename, data):
    """Save JSON data to a cache file."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(cache_dir / filename, 'w') as f:
        json.dump(data, f, indent=2)


def _load_cache(cache_dir, filename):
    """Load JSON data from a cache file, or return None if missing/corrupt."""
    p = cache_dir / filename
    if not p.exists():
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# PDF loading helper — handles Bluebeam / problematic pages
# ---------------------------------------------------------------------------

def _compute_chunk_plan(pdf_path):
    """
    Compute DETERMINISTIC chunk boundaries based on page count and file size.

    Uses only file-level metadata (page count, file size on disk) — never runtime
    page serialization, which can vary between runs and cause non-determinism.
    Same PDF always produces the same chunk plan.

    Returns dict: {pages_per_chunk, total_pages, chunks: [{start, end, id}, ...]}
    Page numbers are 0-based.
    """
    reader = PyPDF2.PdfReader(pdf_path)
    total_pages = len(reader.pages)
    file_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    avg_mb = file_mb / max(1, total_pages)

    # Sample page dimensions to detect large-format (DD-scale) sheets. Even a
    # vector-light DD-scale sheet (≈0.9 MB) packs an entire floor of a multi-unit
    # building; bundling 5–8 of them into one API call routinely truncates
    # Claude's room extraction on the middle pages (silent under-extraction).
    LARGE_FORMAT_PT = 2000  # ≈28" — matches LARGE_FORMAT_THRESHOLD_PT used elsewhere
    sample_indices = sorted({0, total_pages // 4, total_pages // 2,
                              (3 * total_pages) // 4, total_pages - 1})
    has_large_format = False
    for si in sample_indices:
        if si >= total_pages:
            continue
        try:
            mb = reader.pages[si].mediabox
            if max(float(mb.width), float(mb.height)) >= LARGE_FORMAT_PT:
                has_large_format = True
                break
        except Exception:
            continue

    # Heuristic: target ≤5 MB per chunk (base64 expands ~33%, so ~6.5 MB encoded).
    # Large-format architectural PDFs (DD-scale, 42"×30") with complex vectors
    # frequently cause API 500 errors at higher sizes. Keep chunks small.
    TARGET_MB = 5
    if avg_mb > 3:
        ppc = max(1, int(TARGET_MB / avg_mb))
    elif avg_mb > 1:
        ppc = max(2, int(TARGET_MB / avg_mb))
    else:
        ppc = 8

    # Cap chunk size on DD-scale sheets regardless of MB-per-page. A floor plan
    # of a multi-unit building is dense enough that Claude only reliably
    # extracts rooms when each plan page gets its own attention budget.
    if has_large_format and ppc > 2:
        ppc = 2

    chunks = []
    for start in range(0, total_pages, ppc):
        end = min(start + ppc, total_pages)
        chunks.append({
            "start": start,       # 0-based inclusive
            "end": end,           # 0-based exclusive
            "id": f"chunk_{len(chunks) + 1:03d}"
        })

    return {
        "pages_per_chunk": ppc,
        "total_pages": total_pages,
        "file_size_mb": round(file_mb, 2),
        "has_large_format": has_large_format,
        "chunks": chunks,
    }


def _split_pdf_from_plan(pdf_path, chunk_plan):
    """
    Split a PDF into chunk files on disk using a pre-computed chunk plan.

    Uses the deterministic chunk plan (from _compute_chunk_plan) so that
    the same PDF always produces identical chunks.

    Returns list of (temp_file_path, start_page_1based) tuples.
    """
    reader = PyPDF2.PdfReader(pdf_path)
    results = []

    for chunk_info in chunk_plan["chunks"]:
        start = chunk_info["start"]  # 0-based inclusive
        end = chunk_info["end"]      # 0-based exclusive
        writer = PyPDF2.PdfWriter()
        for i in range(start, end):
            try:
                writer.add_page(reader.pages[i])
            except Exception:
                pass  # skip corrupt pages
        if len(writer.pages) == 0:
            continue
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            writer.write(tmp)
            results.append((tmp.name, start + 1))  # 1-based page offset

    return results


def _retry_chunk_without_bad_pages(chunk_path, call_api_fn, chunk_label="",
                                   dropped_pages_out=None):
    """
    When a multi-page chunk fails with 'Could not process PDF', test each
    page individually, discard bad pages, reassemble the good ones, and retry.

    Args:
        chunk_path:   Path to the chunk PDF that failed.
        call_api_fn:  Callable(base64_str, label="") -> response_text.
        chunk_label:  Human-readable label for logging.
        dropped_pages_out: optional list; 1-based (chunk-relative) page
                      numbers that were permanently removed are appended so
                      the caller can record them in _chunk_tracking instead
                      of the drop existing only in console output.

    Returns:
        str or None — API response text from the cleaned chunk,
                      or None if no good pages remain.

    Error taxonomy (the old behavior marked a page bad on ANY exception —
    a 2-second network blip during the probe permanently deleted a page
    from the takeoff): only BadRequestError means the page content itself
    is unprocessable. Every other exception is about the connection or the
    response, not the page — the page is KEPT (untested) and the
    reassembled-chunk call exercises it again with its own retry logic.
    """
    try:
        reader = PyPDF2.PdfReader(chunk_path)
    except Exception:
        print(f"   ⚠️  Could not read chunk for page-level retry")
        return None

    total_pages = len(reader.pages)
    if total_pages <= 1:
        print(f"   ⚠️  Single-page chunk failed — skipping this page")
        if dropped_pages_out is not None and total_pages == 1:
            dropped_pages_out.append(1)
        return None

    print(f"   🔍 {chunk_label}: Testing {total_pages} pages individually ...")

    good_pages = []
    bad_page_nums = []

    for i in range(total_pages):
        writer = PyPDF2.PdfWriter()
        try:
            writer.add_page(reader.pages[i])
        except Exception:
            bad_page_nums.append(i + 1)
            continue

        single_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                writer.write(tmp)
                single_path = tmp.name

            with open(single_path, 'rb') as f:
                single_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

            # Quick validation — just ask Claude to acknowledge the page
            call_api_fn(single_b64, label="")
            good_pages.append((i, reader.pages[i]))
            print(f"      Page {i+1}: ✅")

        except anthropic.BadRequestError:
            # The API rejected the page content itself — genuinely bad.
            bad_page_nums.append(i + 1)
            print(f"      Page {i+1}: ❌ skipped (unreadable)")

        except Exception as e:
            # Transient/connection/truncation error — NOT evidence the page
            # is bad. Keep it; the reassembled chunk call retries transients
            # internally. (Old behavior dropped the page here permanently.)
            good_pages.append((i, reader.pages[i]))
            print(f"      Page {i+1}: ⚠️  probe inconclusive, keeping page "
                  f"({type(e).__name__}: {str(e)[:80]})")

        finally:
            if single_path:
                try:
                    os.unlink(single_path)
                except Exception:
                    pass

    if bad_page_nums:
        print(f"   🗑️  Removed bad pages: {bad_page_nums}")
        if dropped_pages_out is not None:
            dropped_pages_out.extend(bad_page_nums)

    if not good_pages:
        print(f"   ⚠️  No usable pages remain in {chunk_label}")
        return None

    # Reassemble good pages into a cleaned chunk
    writer = PyPDF2.PdfWriter()
    for _, page in good_pages:
        writer.add_page(page)

    clean_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            writer.write(tmp)
            clean_path = tmp.name

        with open(clean_path, 'rb') as f:
            clean_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

        page_nums = [i + 1 for i, _ in good_pages]
        print(f"   ✅ Retrying {chunk_label} with {len(good_pages)} good pages: {page_nums}")
        result = call_api_fn(clean_b64, label=f"Retrying cleaned {chunk_label} ({len(clean_b64)/1024:.0f} KB)")
        return result

    except (anthropic.BadRequestError, TruncatedResponseError) as e:
        # BadRequest: cleaned chunk still unprocessable as a whole.
        # Truncated: combined output overflows max_tokens (the reason the
        # chunk was routed here in the first place) — per-page responses
        # are individually small and cannot truncate.
        print(f"   ⚠️  Cleaned chunk still fails — {e}")
        # Last resort: send each page individually and combine results
        print(f"   🔄 Falling back to single-page processing for {chunk_label}")
        page_results = []
        recovered_page_nums = []
        for i, (page_idx, page_obj) in enumerate(good_pages):
            single_writer = PyPDF2.PdfWriter()
            single_writer.add_page(page_obj)
            single_tmp = None
            try:
                with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                    single_writer.write(tmp)
                    single_tmp = tmp.name
                with open(single_tmp, 'rb') as f:
                    single_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
                result = call_api_fn(single_b64,
                    label=f"  Page {page_idx+1} individually ({len(single_b64)/1024:.0f} KB)")
                page_results.append(result)
                recovered_page_nums.append(page_idx + 1)
            except Exception as page_err:
                print(f"      Page {page_idx+1}: ❌ {page_err}")
                if dropped_pages_out is not None:
                    dropped_pages_out.append(page_idx + 1)
            finally:
                if single_tmp:
                    try:
                        os.unlink(single_tmp)
                    except Exception:
                        pass
        if page_results:
            print(f"   ✅ Recovered {len(page_results)}/{len(good_pages)} pages individually")
            if len(page_results) == 1:
                return page_results[0]
            # Merge into ONE valid JSON document. The old "\n".join produced
            # concatenated JSON objects that the downstream regex+json.loads
            # could never parse — the whole recovered chunk then silently
            # dropped at merge time, defeating this entire fallback.
            return _merge_chunk_responses(page_results,
                                          page_offsets=recovered_page_nums)
        return None

    finally:
        if clean_path:
            try:
                os.unlink(clean_path)
            except Exception:
                pass


def _load_pdf_for_api(pdf_path, _client_for_validation=None):
    """
    Load a PDF and return base64-encoded data ready for the Claude API.

    Smart page filtering:
      0. Pre-scan pages with PyMuPDF to classify by discipline (A/S/M/E/P…).
         Create a filtered PDF with only painting-relevant pages.

    Some PDFs (especially from Bluebeam Revu) contain pages or features
    that Claude's PDF parser cannot handle, returning "Could not process PDF".

    Strategy:
      1. Try sending the (filtered) file.  If it's small enough (<4 MB b64) just use it.
      2. For large files, split into 5-page chunks.  Return the FIRST chunk's b64
         and also write the remaining chunks to temp files so the caller can
         make multiple API calls.

    Returns:
        str — base64 data of the (possibly filtered & chunked) PDF
    Also sets module-level ``_pending_chunks``, ``_chunk_page_offsets``,
    and ``_page_index_map`` for downstream source_page correction.
    """
    global _pending_chunks, _chunk_page_offsets, _page_index_map
    _pending_chunks = []
    _chunk_page_offsets = []
    _page_index_map = None

    MAX_B64_BYTES = 4 * 1024 * 1024  # ~3 MB raw

    # --- Step 0: Smart page filtering by discipline ---
    classifications = _classify_pdf_pages(pdf_path)
    effective_path = pdf_path  # will change if we create a filtered PDF
    _filtered_tmp_path = None  # track temp file for cleanup

    if classifications:
        included = [c for c in classifications if c['include']]
        excluded = [c for c in classifications if not c['include']]
        total = len(classifications)
        kept = len(included)

        if excluded and kept > 0:
            print(f"   🔍 Page filter: {kept}/{total} pages are painting-relevant")
            print(f"      Excluded: {len(excluded)} pages ({_summarize_excluded(excluded)})")

            # Build page index mapping: filtered_index → original_index
            page_indices = [c['page_index'] for c in included]
            _page_index_map = {new_idx: orig_idx
                               for new_idx, orig_idx in enumerate(page_indices)}

            # Create filtered PDF as a temp file
            filtered_bytes = _create_filtered_pdf(pdf_path, page_indices)
            _filtered_tmp_path = tempfile.NamedTemporaryFile(
                suffix='.pdf', delete=False, prefix='nsai_filtered_'
            ).name
            with open(_filtered_tmp_path, 'wb') as f:
                f.write(filtered_bytes)
            effective_path = _filtered_tmp_path
            print(f"      Filtered PDF: {len(filtered_bytes)/1024:.0f} KB "
                  f"(original {os.path.getsize(pdf_path)/1024:.0f} KB)")
        elif kept == 0:
            # No pages matched — include all (safe fallback)
            print(f"   🔍 Page filter: no recognized sheet numbers — including all {total} pages")
        else:
            print(f"   🔍 Page filter: all {total} pages are painting-relevant")
    # else: PyMuPDF not available or empty result — no filtering

    # --- Quick path: check raw file size BEFORE base64 encoding ---
    # base64 inflates by ~33%, so 3MB raw ≈ 4MB encoded.
    # Check file size first to avoid MemoryError on large PDFs.
    raw_size = os.path.getsize(effective_path)
    raw_size_b64_est = raw_size * 4 / 3  # estimated base64 size

    if raw_size_b64_est <= MAX_B64_BYTES:
        # Small enough — safe to load and encode in one shot
        with open(effective_path, 'rb') as f:
            raw = f.read()
        pdf_data = base64.standard_b64encode(raw).decode("utf-8")
        print(f"✅ PDF loaded ({len(pdf_data)/1024/1024:.1f} MB encoded)")
        _cleanup_filtered_tmp(_filtered_tmp_path)
        return pdf_data

    # --- Large file: split into chunks (do NOT base64-encode the whole thing) ---
    try:
        reader = PyPDF2.PdfReader(effective_path)
        total_pages = len(reader.pages)
    except Exception:
        # Can't read with PyPDF2 — have to load raw as last resort
        with open(effective_path, 'rb') as f:
            raw = f.read()
        pdf_data = base64.standard_b64encode(raw).decode("utf-8")
        print(f"✅ PDF loaded ({len(pdf_data)/1024/1024:.1f} MB encoded, could not split)")
        _cleanup_filtered_tmp(_filtered_tmp_path)
        return pdf_data

    raw_mb = raw_size_b64_est / 1024 / 1024
    print(f"📐 PDF is large ({total_pages} pages, {raw_mb:.1f} MB)")

    # Deterministic chunk plan — same PDF always produces same chunks
    chunk_plan = _compute_chunk_plan(effective_path)
    ppc = chunk_plan["pages_per_chunk"]
    avg_page_mb = raw_mb / max(1, total_pages)
    print(f"   Splitting into ~{ppc}-page chunks (avg {avg_page_mb:.1f} MB/page) ...")

    chunk_info = _split_pdf_from_plan(effective_path, chunk_plan)
    _cleanup_filtered_tmp(_filtered_tmp_path)

    if not chunk_info:
        print(f"   ⚠️  Could not split — sending original")
        with open(effective_path, 'rb') as f:
            return base64.standard_b64encode(f.read()).decode("utf-8")

    # First chunk is returned for the primary API call
    first_path, first_offset = chunk_info[0]
    with open(first_path, 'rb') as f:
        first_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    os.unlink(first_path)

    # Remaining chunks are stored for the caller to process
    _pending_chunks = chunk_info[1:]  # list of (path, start_page_1based)
    _chunk_page_offsets = [first_offset] + [off for _, off in chunk_info[1:]]

    # Stash the full chunk plan ranges (1-based, inclusive) so downstream
    # validation can detect per-page under-extraction within a chunk.
    global _chunk_plan_ranges
    _chunk_plan_ranges = [
        {"chunk_idx": i + 1,
         "page_start": c["start"] + 1,
         "page_end": c["end"]}  # end is exclusive 0-based, so equals last page 1-based
        for i, c in enumerate(chunk_plan["chunks"])
    ]

    print(f"   ✅ Split into {len(chunk_info)} chunks")
    print(f"   📄 Processing chunk 1/{len(chunk_info)} ({len(first_b64)/1024:.0f} KB)")
    return first_b64


def _cleanup_filtered_tmp(path):
    """Remove the temporary filtered PDF if it exists."""
    if path:
        try:
            os.unlink(path)
        except Exception:
            pass


# Module-level list of remaining PDF chunks after splitting: [(path, start_page_1based), ...]
_pending_chunks = []
# Module-level list of page offsets for each chunk (1-based start page)
_chunk_page_offsets = []
# Module-level list of {chunk_idx, page_start, page_end} (1-based inclusive)
# for the FULL chunk plan, used by validation to spot per-page under-extraction.
_chunk_plan_ranges = []
# Module-level page index map: {filtered_page_0based: original_page_0based} or None
_page_index_map = None


# ---------------------------------------------------------------------------
# Schedule Page Identification & Image Rendering (PyMuPDF)
# ---------------------------------------------------------------------------

def _identify_schedule_pages(pdf_path):
    """
    Scan PDF pages using PyMuPDF text extraction (fast, no API cost) to
    identify which pages contain door schedules, window schedules, and
    floor plans.  Returns dict with 0-based page numbers.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("   ⚠️  PyMuPDF not installed — skipping schedule page scan")
        return {"door_schedule_pages": [], "window_schedule_pages": [],
                "floor_plan_pages": []}

    doc = fitz.open(pdf_path)
    result = {"door_schedule_pages": [], "window_schedule_pages": [],
              "floor_plan_pages": []}

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text().lower()

        # Door schedule detection — require title + multiple table header keywords
        # to avoid false positives on cover sheets that merely list sheet names
        has_door_title = any(kw in text for kw in (
            "door schedule", "door and frame schedule", "door & frame schedule",
        ))
        # Count how many table-header keywords match (need ≥2 for a real schedule)
        door_table_hits = sum(1 for kw in (
            "mark\nwidth", "width\nheight", "material\nframe",
            "firerating", "fire rating", "fire-rating",
            "self-closing", "3'-0\"", "7'-0\"",
        ) if kw in text)
        if has_door_title and door_table_hits >= 2:
            result["door_schedule_pages"].append(page_num)

        # Floor plan detection (do this before window check so we can exclude FPs)
        is_floor_plan = any(kw in text for kw in (
            "floor plan", "foundation plan", "basement plan",
            "level plan", "unit plan",
        ))
        if is_floor_plan:
            result["floor_plan_pages"].append(page_num)

        # Window schedule detection — or dedicated storefront detail sheets
        # (but NOT floor plan pages that happen to show storefronts)
        has_win_title = any(kw in text for kw in (
            "window schedule", "glazing schedule",
        ))
        # Require window schedule pages to have actual window data, not just
        # a title on a cover/index sheet.  Check for dimension patterns.
        has_win_data = any(kw in text for kw in (
            "window type", "window mark", "sill height",
            "frame material", "frame type",
        )) or bool(re.search(r"\d+'-\d+\"", text))  # dimension pattern like 3'-0"
        # Storefront detail pages: high density (4+ mentions) + SF mark numbers
        # Exclude floor plan pages that merely reference storefronts
        storefront_count = text.count("storefront")
        has_storefront_detail = (storefront_count >= 4 and
                                 any(f"sf{d}" in text for d in "12345")
                                 and not is_floor_plan)
        if (has_win_title and has_win_data) or has_storefront_detail:
            result["window_schedule_pages"].append(page_num)

    doc.close()
    return result


def _detect_index_pages(pdf_path):
    """
    Scan PDF pages using PyMuPDF text extraction (fast, no API cost) to
    identify pages that contain drawing indices or building lists.

    Returns dict or None:
        {
            "index_pages": [0, 1, 2],        # 0-based page indices
            "index_text": "...",              # concatenated text from index pages
            "has_building_list": True,        # True if building-related keywords found
            "building_keywords_found": ["villa", "duplex"]
        }
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None

    doc = fitz.open(pdf_path)
    index_pages = []
    index_text_parts = []

    # Index keywords (must appear on the page)
    _INDEX_KEYWORDS = (
        "drawing index", "drawing list", "sheet index", "sheet list",
        "table of contents", "project directory", "building index",
        "drawing schedule", "index of drawings",
    )

    # Building keywords (signal that the index lists multiple buildings)
    _BUILDING_KEYWORDS = (
        "building", "bldg", "villa", "duplex", "cottage", "carriage",
        "tower", "wing", "pavilion", "townhouse", "townhome",
        "clubhouse", "community", "residence", "phase",
    )

    # Scan first 15 pages (indices are always near the front)
    scan_limit = min(len(doc), 15)
    for page_num in range(scan_limit):
        page = doc[page_num]
        text = page.get_text()
        text_lower = text.lower()

        is_index = any(kw in text_lower for kw in _INDEX_KEYWORDS)

        # Also check for high density of sheet number patterns (A-101, A1.02, etc.)
        # which indicates a drawing index even without explicit "drawing index" title.
        # Accept both the 3-digit convention (A101) and the dotted convention (A1.02);
        # require the dot for short numbers so bare "A1" mentions aren't counted.
        sheet_refs = re.findall(r'[A-Z]{1,2}\s*[-.]?\s*(?:\d{2,3}|\d{1,2}\.\d{1,2})', text)
        if len(sheet_refs) >= 10:
            is_index = True

        # Detect building summary / unit mix pages that contain building counts
        # even without being traditional drawing indices
        if not is_index:
            _BLDG_SUMMARY_KEYWORDS = (
                "unit mix", "bldg mix", "building mix",
                "unit count", "bldg count", "building count",
                "building summary", "project summary",
                "building schedule", "bldg schedule",
            )
            has_bldg_summary = any(kw in text_lower for kw in _BLDG_SUMMARY_KEYWORDS)
            # Patterns like "4 BLDGS", "6 BLDG", "16 buildings"
            has_bldg_count = bool(re.search(r'\d+\s*(?:bldg|building)s?\b', text_lower))
            if has_bldg_summary or has_bldg_count:
                is_index = True

        if is_index:
            index_pages.append(page_num)
            index_text_parts.append(text)

    doc.close()

    if not index_pages:
        return None

    full_text = "\n".join(index_text_parts).lower()

    # Check for building-related keywords
    bldg_keywords_found = [kw for kw in _BUILDING_KEYWORDS if kw in full_text]

    # Also check for numbered building patterns: "Building 1", "Bldg. 4", "16 buildings"
    has_numbered_buildings = bool(re.search(
        r'(?:building|bldg)[s.]?\s*(?:#?\s*)?\d+', full_text
    )) or bool(re.search(
        r'\d+\s+(?:identical\s+)?(?:building|bldg|villa|duplex|cottage)', full_text
    ))

    has_building_list = len(bldg_keywords_found) >= 1 or has_numbered_buildings

    return {
        "index_pages": index_pages,
        "index_text": "\n".join(index_text_parts),
        "has_building_list": has_building_list,
        "building_keywords_found": bldg_keywords_found,
    }


# ---------------------------------------------------------------------------
# Smart Page Filtering — Discipline-based page classification
# ---------------------------------------------------------------------------

# Division 9 / painting-related keywords (lowercase) to detect finish schedule pages
_DIVISION_9_KEYWORDS = [
    'finish schedule', 'finishing schedule', 'paint schedule', 'color schedule',
    'room finish', 'interior finish', 'division 9', 'division 09', 'section 09',
    '09 91', '09 90', 'paint color', 'wall finish', 'ceiling finish',
    # Alternative finish-table labels seen on retail/commercial sets (e.g. B&N
    # used "Finish Legend" rather than "Finish Schedule" — pages with these
    # labels must still be retained for finish extraction).
    'finish legend', 'finishes legend', 'finish notes', 'paint legend',
    'material legend',
]

# Sheet prefix → discipline mapping (order matters: check longer prefixes first)
#
# 2026-05-29 expansion driven by the Urban Air Adventure Park investigation:
# a 169.6 MB / 73-page bid set produced only 12 rooms (just sheet FS101) and
# a $24K subtotal — flagged for manual review but historically shipped to
# the customer anyway. Forensics on the annotated PDF showed 72 of 73 pages
# tagged "Not referenced in takeoff (category unknown)" because the title-
# block sheet number used prefixes (FS, MD, FE, AR, ...) that weren't in
# this map. The exclusion default ("unknown → drop") is what caused the
# silent under-extraction. Specialty-architectural prefixes belong here so
# the architect's choice of sheet-naming convention doesn't decide whether
# the painter gets a real estimate or a fragmentary one.
_DISCIPLINE_MAP = [
    # Included disciplines (painting-relevant)
    ('AD', 'Architectural Demo', True),
    ('AI', 'Architectural Interiors', True),
    ('AR', 'Architectural (alt prefix)', True),   # 2026-05-29 add — some firms (esp. retail/franchise)
    ('AS', 'Architectural Site', True),           # 2026-05-29 add
    ('FS', 'Food Service', True),                 # 2026-05-29 add — interior fit-out floor plans
    ('FE', 'Furniture / Equipment', True),        # 2026-05-29 add — has rooms + walls
    ('FF', 'Furniture / Fixtures', True),         # 2026-05-29 add
    ('ID', 'Interior Design', True),
    ('IN', 'Interior', True),                     # 2026-05-29 add
    ('LS', 'Life Safety / Egress', True),         # 2026-05-29 add — shows full floor plan
    ('SK', 'Sketch / Field Coord', True),         # 2026-05-29 add — often the actual issued plan
    ('WP', 'Waterproofing', True),                # 2026-05-29 add — has room boundaries
    ('AV', 'Audio-Visual', True),                 # 2026-05-29 add — has room boundaries
    ('A',  'Architectural', True),
    ('G',  'General', True),
    ('T',  'Title', True),
    # Excluded disciplines
    ('FP', 'Fire Protection', False),
    ('FA', 'Fire Alarm', False),
    ('MD', 'Mechanical Demo', False),             # 2026-05-29 add — engineering demo
    ('SD', 'Structural Demo', False),             # 2026-05-29 add
    ('ED', 'Electrical Demo', False),             # 2026-05-29 add
    ('PD', 'Plumbing Demo', False),               # 2026-05-29 add
    ('S',  'Structural', False),
    ('M',  'Mechanical', False),
    ('E',  'Electrical', False),
    ('P',  'Plumbing', False),
    ('C',  'Civil', False),
    ('L',  'Landscape', False),
]

# Regex to match sheet numbers like A101, A-101, A 101, A1.01, AD101, FP-101, etc.
_SHEET_NUMBER_RE = re.compile(
    r'\b([A-Z]{1,2})\s*[-.]?\s*(\d{1,3}(?:\.\d{1,2})?)\b',
    re.IGNORECASE,
)


def _parse_building_count_from_filename(filename):
    """
    Parse building count from PDF filename conventions used in construction docs.

    Examples:
      "BLDG 1-3.pdf"            → (3, [1, 2, 3])
      "BLDG 5-7.pdf"            → (3, [5, 6, 7])
      "Building 4.pdf"          → (1, [4])
      "VOL#1_BLDG 1-3.pdf"     → (3, [1, 2, 3])
      "Buildings 1 & 3.pdf"     → (2, [1, 3])  -- non-contiguous
      "BLDG_1_thru_3.pdf"       → (3, [1, 2, 3])
      "No match here.pdf"       → (1, [])

    Returns (building_count, building_ids) where building_ids is a list of ints.
    """
    name = os.path.splitext(filename)[0]  # strip .pdf

    # Pattern 1: Range  — "BLDG 1-3", "Buildings 5-7", "Bldg. 1–3", "BLDG 1 thru 3"
    range_pat = re.compile(
        r'(?:BLDG[S.]?|BUILDING[S]?)[_\s#]*'   # prefix
        r'(\d+)[\s_]*(?:[-–—&]|thru|through)[\s_]*'  # start number + separator
        r'(\d+)',                                      # end number
        re.IGNORECASE
    )
    m = range_pat.search(name)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if end >= start:
            ids = list(range(start, end + 1))
            return (len(ids), ids)

    # Pattern 2: Comma/ampersand list — "BLDG 1, 3 & 5"
    list_pat = re.compile(
        r'(?:BLDG[S.]?|BUILDING[S]?)[_\s#]*'
        r'((?:\d+\s*[,&]\s*)+\d+)',
        re.IGNORECASE
    )
    m = list_pat.search(name)
    if m:
        ids = [int(x) for x in re.findall(r'\d+', m.group(1))]
        if ids:
            return (len(ids), ids)

    # Pattern 3: Single building — "BLDG 4", "Building 4"
    single_pat = re.compile(
        r'(?:BLDG[S.]?|BUILDING[S]?)[_\s#]*(\d+)(?!\s*[-–—]|\s*thru)',
        re.IGNORECASE
    )
    m = single_pat.search(name)
    if m:
        return (1, [int(m.group(1))])

    # No building pattern found — assume single building
    return (1, [])


def _is_floor_plan_file(filename):
    """
    Heuristic check: does this filename suggest it contains floor plans?
    Used to decide whether to retry extraction when 0 rooms are returned.

    Matches patterns like:
      A-101-FIRST-FLOOR-PLAN-Rev.1.pdf
      A-102-FLOOR-PLAN.pdf
      A1.01 Floor Plan.pdf
      FP-101-LEVEL-1-PLAN.pdf

    Does NOT match:
      A-600-DOOR-SCHEDULES.pdf
      A-300-BUILDING-SECTIONS.pdf
      G-003-WALL-TYPES.pdf
      A-112-REFLECTED-CEILING-PLAN.pdf
    """
    name_upper = filename.upper()

    # Exclusion keywords — these are NOT floor plans even if they match A-1xx
    _EXCLUDE_KEYWORDS = (
        "CEILING", "RCP", "REFLECTED", "FURNITURE", "ROOF",
        "ELEVATION", "SECTION", "DETAIL", "SCHEDULE",
    )
    if any(kw in name_upper for kw in _EXCLUDE_KEYWORDS):
        return False

    # Direct keyword match in filename
    if any(kw in name_upper for kw in ("FLOOR-PLAN", "FLOOR_PLAN", "FLOOR PLAN",
                                        "FLOORPLAN", "UNIT-PLAN", "UNIT_PLAN",
                                        "FINISH-PLAN", "FINISH_PLAN")):
        return True

    # Architectural sheet number in A-1xx range (AIA convention for floor plans)
    match = _SHEET_NUMBER_RE.search(name_upper)
    if match:
        prefix = match.group(1).upper()
        number_str = match.group(2)
        try:
            number = float(number_str)
        except ValueError:
            number = 0
        # A-100 through A-199 are conventionally floor plans
        if prefix == 'A' and 100 <= number < 200:
            return True
        # A-700 through A-799 are finish plans (often contain room layouts)
        if prefix == 'A' and 700 <= number < 800:
            return True

    return False


def _extract_known_sheet_ids_from_index(pdf_path):
    """Pre-scan the index pages for sheet-ID patterns to build an
    authoritative set of sheet IDs that exist on THIS project.

    Used by `_classify_pdf_pages` to validate per-page sheet-ID detection
    against the drawing index. Without this, the classifier can be fooled by
    equipment callouts like "EQ2" or "FA-201" on an architectural sheet —
    those get parsed as the page's own ID and the page is wrongly excluded as
    Electrical / Fire Alarm.

    Strategy:
      1. Use _detect_index_pages() to find the project's drawing-index page(s).
      2. From the index_text, extract every disciplinary sheet ID — these are
         authoritative because the drawing index lists every sheet exactly once.
      3. Fallback: if no index found, scan the first 3 pages and accept
         whatever disciplinary IDs appear there (one of them is almost
         always the cover page / index).

    Returns a set of normalized sheet IDs (e.g. {"A100", "A101", "A105", ...}).
    Empty set on failure — caller treats as "no validation available".
    """
    candidates = set()

    # Try the proper index detector first
    try:
        idx = _detect_index_pages(pdf_path)
    except Exception:
        idx = None

    if idx and idx.get("index_text"):
        for m in _SHEET_NUMBER_RE.finditer(idx["index_text"]):
            prefix = m.group(1).upper()
            number = m.group(2)
            if any(prefix == dp or prefix.startswith(dp)
                   for dp, _, _ in _DISCIPLINE_MAP):
                candidates.add(f"{prefix}{number}")

    if candidates:
        return candidates

    # Fallback: scan first few pages directly
    try:
        import fitz
        doc = fitz.open(pdf_path)
    except Exception:
        return set()

    try:
        scan_limit = min(len(doc), 3)
        for pg_0 in range(scan_limit):
            try:
                txt = doc[pg_0].get_text() or ""
            except Exception:
                continue
            for m in _SHEET_NUMBER_RE.finditer(txt):
                prefix = m.group(1).upper()
                number = m.group(2)
                if any(prefix == dp or prefix.startswith(dp)
                       for dp, _, _ in _DISCIPLINE_MAP):
                    candidates.add(f"{prefix}{number}")
    finally:
        doc.close()

    return candidates


def _classify_pdf_pages(pdf_path):
    """
    Pre-scan all pages using PyMuPDF (zero API cost) to classify each page
    by architectural discipline.  Returns a list of classification dicts:

        [{page_index, sheet_number, discipline, include, reason}, ...]

    Included pages: Architectural (A), Interior Design (ID), General (G/T first 3),
                    Division 9 finish schedules, and unknown/image-only pages.
    Excluded pages: Structural (S), Mechanical (M), Electrical (E), Plumbing (P),
                    Civil (C), Landscape (L), Fire Protection (FP/FA).
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("   ⚠️  PyMuPDF not installed — skipping page classification")
        return []  # Empty = no filtering, send all pages

    # Build the set of sheet IDs that actually exist on this project — used
    # to filter out equipment-callout false positives below.
    known_sheet_ids = _extract_known_sheet_ids_from_index(pdf_path)

    doc = fitz.open(pdf_path)
    classifications = []
    g_t_count = 0  # Track how many General/Title pages we've included

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_rect = page.rect
        page_w = page_rect.width
        page_h = page_rect.height

        # --- Find sheet number ---
        # Empirically (validated across multiple architect templates):
        # the actual title-block sheet ID is reliably the LARGEST-font text
        # on the page that matches the sheet-number pattern. It's typically
        # 25-35pt, while incidental callouts ("see A-300/2") are 6-8pt and
        # grid line labels are 5pt. Position-based clipping is fragile
        # (architects place title blocks anywhere; some PDF coordinate systems
        # put the title block at negative or off-page positions due to
        # rotation metadata), but font size is consistent.
        #
        # Strategy: scan the WHOLE page for sheet-ID candidates with their
        # font sizes. Prefer the largest-font candidate that's confirmed
        # present in the drawing index. Fall back to the absolute largest if
        # none are index-confirmed.
        full_text = page.get_text().strip()
        full_text_lower = full_text.lower()

        sheet_number = None
        discipline_prefix = None

        candidates_with_size = []
        try:
            td = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        except Exception:
            td = {"blocks": []}
        for block in td.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = (span.get("text") or "").strip()
                    if not t:
                        continue
                    sz = float(span.get("size", 0))
                    for m in _SHEET_NUMBER_RE.finditer(t):
                        prefix = m.group(1).upper()
                        number = m.group(2)
                        if any(prefix == dp or prefix.startswith(dp)
                               for dp, _, _ in _DISCIPLINE_MAP):
                            candidates_with_size.append(
                                (f"{prefix}{number}", prefix, sz)
                            )
        candidates_with_size.sort(key=lambda x: -x[2])  # largest font first

        # Prefer largest-font candidate that's in the drawing index
        if known_sheet_ids:
            for sid, prefix, sz in candidates_with_size:
                if sid in known_sheet_ids:
                    sheet_number = sid
                    discipline_prefix = prefix
                    break

        # Fallback: absolute largest-font candidate (any disciplinary ID)
        if not sheet_number and candidates_with_size:
            sheet_number, discipline_prefix, _ = candidates_with_size[0]

        # --- Classify by discipline ---
        include = True
        discipline = "Unknown"
        reason = "no sheet number detected — included by default"

        if discipline_prefix:
            # Find matching discipline
            for dp, disc_name, inc in _DISCIPLINE_MAP:
                if discipline_prefix == dp or discipline_prefix.startswith(dp):
                    discipline = disc_name
                    include = inc
                    if inc:
                        reason = f"sheet {sheet_number} — {disc_name} (painting-relevant)"
                    else:
                        reason = f"sheet {sheet_number} — {disc_name} (excluded)"
                    break

            # Limit General/Title pages to first 3
            if discipline in ('General', 'Title'):
                g_t_count += 1
                if g_t_count > 3:
                    include = False
                    reason = f"sheet {sheet_number} — {discipline} (excluded: >3 G/T pages)"

        # --- Division 9 override: if full text has finishing keywords, always include ---
        if not include and any(kw in full_text_lower for kw in _DIVISION_9_KEYWORDS):
            include = True
            reason = f"sheet {sheet_number or '?'} — Division 9 finish schedule (override)"
            discipline = "Division 9 Override"

        # --- Image-only pages (no text at all) → include by default ---
        if len(full_text) < 20:
            include = True
            reason = "image-only page (no extractable text) — included by default"
            discipline = "Unknown (image-only)"

        classifications.append({
            "page_index": page_idx,
            "sheet_number": sheet_number,
            "discipline": discipline,
            "include": include,
            "reason": reason,
        })

    doc.close()

    # One-line summary log per page so the next "why was this PDF only
    # partially extracted?" investigation is a single grep instead of a
    # forensics dig through the annotated PDF banners. Output mirrors a
    # CSV-friendly format so it pastes cleanly into a spreadsheet.
    #
    # Format: PageClassify | p{idx} | sheet={s} | disc={d} | include={I}
    # Search the worker logs for "PageClassify" to enumerate every
    # included/excluded decision for a given submission.
    try:
        kept = sum(1 for c in classifications if c.get("include"))
        total = len(classifications)
        print(f"📋 PageClassify summary: {kept}/{total} pages included for "
              f"room extraction; {total - kept} excluded")
        for c in classifications:
            inc = "Y" if c.get("include") else "N"
            sn = c.get("sheet_number") or "?"
            disc = (c.get("discipline") or "?")[:24]
            print(f"   PageClassify | p{c['page_index']+1:<3} | "
                  f"sheet={sn:<8} | disc={disc:<24} | include={inc}")
    except Exception:
        # Logging must never break the classifier; swallow any I/O glitch
        pass

    return classifications


# Excluded patterns that mark a hit as a cross-reference, not a schedule
# sheet title. "A13 Finish Schedule & Details" on a sheet-index list looks
# like a finish-schedule mention but isn't the schedule itself. The "&
# details/detail" suffix is the giveaway because Coppola-style schedule
# sheets are named e.g. "FINISH SCHEDULE & DETAILS" in the title block but
# the SPAN extracted from the rotated title block contains JUST the title
# phrase ("FINISH SCHEDULE"), while the T1-style sheet-list reference
# extracts as a longer span that pulls in the sheet number and ampersand.
_SCHEDULE_REFERENCE_EXCLUDES = ("& detail", "& details", "see sheet",
                                "see drawing", "refer to sheet")


def _has_schedule_sheet_title(page, title_phrases, excludes=_SCHEDULE_REFERENCE_EXCLUDES):
    """Return True if any text span on the page contains a title phrase as
    a standalone sheet title (not a cross-reference in a list).

    PyMuPDF's "dict" extraction gives us spans, not just concatenated text.
    A schedule sheet's title block emits a span like 'FINISH SCHEDULE';
    a sheet-index list entry on T1 emits 'A13 Finish Schedule & Details'.
    Filtering on excluded suffixes ("& details") cleanly separates them.
    """
    title_l = [p.lower() for p in title_phrases]
    excl_l = [e.lower() for e in excludes]
    try:
        d = page.get_text("dict")
    except Exception:
        return False
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = (span.get("text") or "").strip().lower()
                if not txt:
                    continue
                if not any(p in txt for p in title_l):
                    continue
                if any(e in txt for e in excl_l):
                    continue
                return True
    return False


def _detect_schedule_in_pdf(pdf_path, title_phrases, table_tokens=()):
    """Generic schedule detector. Two passes per page:
      1. Span-level: title phrase appears as a standalone sheet title.
         Works when the schedule table itself is vector art (no extractable
         text inside the table) — the only thing PyMuPDF sees is the
         rotated title-block span. Catches Coppola's A12 (DOOR SCHEDULE),
         A13 (FINISH SCHEDULE), etc.
      2. Concatenated-text + table_tokens: title phrase appears anywhere
         AND 2+ column-header tokens appear. Works for projects where the
         schedule table content is real PDF text. Kept for back-compat
         with the projects this function was originally tuned for.

    Returns True / False if scanned, None if the PDF could not be opened.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return None
    try:
        for page in doc:
            if _has_schedule_sheet_title(page, title_phrases):
                return True
            if table_tokens:
                t = page.get_text().lower()
                if any(p.lower() in t for p in title_phrases) \
                        and sum(1 for tok in table_tokens if tok in t) >= 2:
                    return True
        return False
    finally:
        doc.close()


_FINISH_TITLE_PHRASES = ("room finish schedule", "finish schedule",
                         "interior finish schedule", "finish legend",
                         "room finish legend")
_FINISH_TABLE_TOKENS = ("wall finish", "ceiling finish", "base finish",
                        "floor finish", "room finish", "room name",
                        "room no", "room number")
_DOOR_TITLE_PHRASES = ("door schedule", "door & window schedule",
                       "door and window schedule", "door schedule:")
_DOOR_TABLE_TOKENS = ("door no", "door number", "door type", "door size",
                      "frame type", "hardware set", "fire rating",
                      "head detail", "jamb detail")
_WINDOW_TITLE_PHRASES = ("window schedule", "window & door schedule",
                         "glazing schedule", "window schedule:")
_WINDOW_TABLE_TOKENS = ("window no", "window number", "window type",
                        "window mark", "rough opening", "unit size",
                        "glazing type", "sill height")


def _detect_finish_schedule(pdf_path):
    """Detect a Room Finish Schedule in the PDF (text-only, no API calls).
    See _detect_schedule_in_pdf for the dual-pass strategy.
    """
    return _detect_schedule_in_pdf(pdf_path, _FINISH_TITLE_PHRASES,
                                   _FINISH_TABLE_TOKENS)


def _detect_door_schedule(pdf_path):
    """Detect a Door Schedule in the PDF (text-only, no API calls)."""
    return _detect_schedule_in_pdf(pdf_path, _DOOR_TITLE_PHRASES,
                                   _DOOR_TABLE_TOKENS)


def _detect_window_schedule(pdf_path):
    """Detect a Window Schedule in the PDF (text-only, no API calls)."""
    return _detect_schedule_in_pdf(pdf_path, _WINDOW_TITLE_PHRASES,
                                   _WINDOW_TABLE_TOKENS)


def _set_finish_schedule_flag(analysis, pdf_paths):
    """Set analysis['has_finish_schedule'] from a zero-cost PDF text scan.

    Honors a finish schedule already detected/extracted upstream (the
    schedule-estimation path populates room_finish_schedule). Leaves the flag
    unset if no PDF could be scanned, so the RFI's `is False` check does not
    fire on an inconclusive scan.
    """
    if analysis.get("has_finish_schedule") is True or analysis.get("room_finish_schedule"):
        analysis["has_finish_schedule"] = True
        return
    found = False
    scanned = False
    for p in pdf_paths or []:
        d = _detect_finish_schedule(p)
        if d is None:
            continue
        scanned = True
        if d:
            found = True
            break
    if scanned:
        analysis["has_finish_schedule"] = found
        print(f"   📋 Finish schedule detection: "
              f"{'found in upload' if found else 'none found'}")


def _set_door_schedule_flag(analysis, pdf_paths):
    """Set analysis['has_door_schedule'] from a zero-cost PDF text scan.

    Honors an upstream True from LLM extraction. Only flips False -> True
    when the text scan finds explicit evidence (the LLM is the source of
    truth when it has actually read the page). The LLM is wrong often
    enough on schedule-sheet recognition for Coppola-style projects that
    a text-based safety net is worth running unconditionally.
    """
    if analysis.get("has_door_schedule") is True:
        return
    for p in pdf_paths or []:
        d = _detect_door_schedule(p)
        if d:
            analysis["has_door_schedule"] = True
            print("   🚪 Door schedule detection: found in upload "
                  "(text-scan override)")
            return


def _set_window_schedule_flag(analysis, pdf_paths):
    """Set analysis['has_window_schedule'] from a zero-cost PDF text scan.
    Mirrors _set_door_schedule_flag.
    """
    if analysis.get("has_window_schedule") is True:
        return
    for p in pdf_paths or []:
        d = _detect_window_schedule(p)
        if d:
            analysis["has_window_schedule"] = True
            print("   🪟 Window schedule detection: found in upload "
                  "(text-scan override)")
            return


def _normalize_sheet_token(s):
    """Normalize a sheet number for comparison: uppercase, drop separators.
    'A-101' / 'A 101' / 'A1.01' all normalize to 'A101'."""
    return re.sub(r'[^A-Z0-9]', '', str(s).upper())


def _detect_sheet_id_on_page(page, known_prefixes):
    """Return the most likely sheet ID rendered on this page's title block,
    or None if undetectable. Picks raw text (not normalized) so the caller
    can preserve the architect's convention.

    Strategy: look at every text span, keep ones that are either rotated
    (vertical-running text, typical of architectural title-block sidebars)
    or rendered at >=24pt (sheet numbers are usually the largest text on
    a page). Filter to spans matching _SHEET_NUMBER_RE with a known
    discipline prefix. If multiple candidates remain, pick the largest
    (the actual sheet number is bigger than any cross-reference callouts).

    Tested against Coppola's Ridgeview set (14 sheets, T1 + A1..A13): the
    old bottom-strip clip approach missed 5 of 14 pages because Coppola's
    title block lives in a rotated right-edge strip whose y-coordinates
    extend past page.height; the new approach identifies all 14.
    """
    try:
        d = page.get_text("dict")
    except Exception:
        return None
    best = None  # (size, raw_id)
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            dir_ = line.get("dir", (1.0, 0.0))
            is_rotated = abs(dir_[1]) > 0.5
            for span in line.get("spans", []):
                txt = (span.get("text", "") or "").strip()
                if not txt or len(txt) > 8:
                    continue
                size = span.get("size", 0) or 0
                if not (is_rotated or size >= 24):
                    continue
                for m in _SHEET_NUMBER_RE.finditer(txt):
                    prefix = m.group(1).upper()
                    if not any(prefix == p or prefix.startswith(p)
                               for p in known_prefixes):
                        continue
                    raw = prefix + m.group(2)
                    if best is None or size > best[0]:
                        best = (size, raw)
    return best[1] if best else None


def _build_page_to_sheet_map(pdf_paths):
    """Return {(pdf_path, page_idx_0based): raw_sheet_id} for every page
    where a sheet ID is detectable. raw_sheet_id is the EXACT string from
    the title block (not normalized) so source_sheet substitutions preserve
    the architect's convention.

    Used by _canonicalize_source_sheets to override LLM-emitted source_sheet
    values that don't match what's actually printed on the page.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return {}
    known_prefixes = tuple(dp for dp, _, _ in _DISCIPLINE_MAP)
    out = {}
    for path in pdf_paths or []:
        try:
            doc = fitz.open(path)
        except Exception:
            continue
        try:
            for i, page in enumerate(doc):
                raw = _detect_sheet_id_on_page(page, known_prefixes)
                if raw:
                    out[(path, i)] = raw
        finally:
            doc.close()
    return out


def _canonicalize_source_sheets(analysis, pdf_paths):
    """Override room.source_sheet with the actual sheet ID printed on the
    page indicated by room.source_page.

    The 2026-05-28 Ridgeview run had the model emit rooms tagged
    'A-101', 'A-102', 'A-103' (ANSI-style) for what were actually pages
    on sheets 'A2' and 'A3' (Coppola-style). The downstream pipeline
    treats different source_sheet strings as different sheets, so the
    same physical floor plan got extracted twice under two sheet IDs,
    creating phantom floors. The regex dedup catches the symptom; this
    pass prevents the bad data from being created in the first place.

    For each room with a numeric source_page, look up the sheet ID
    actually rendered on that page (via the page→sheet map built from
    the PDF title blocks). If different from room.source_sheet, override
    it and stash the original under '_source_sheet_llm' for audit.

    Idempotent via analysis['_source_sheets_canonicalized'].
    """
    if not isinstance(analysis, dict):
        return analysis
    if analysis.get("_source_sheets_canonicalized"):
        return analysis

    page_map = _build_page_to_sheet_map(pdf_paths)
    if not page_map:
        analysis["_source_sheets_canonicalized"] = True
        return analysis

    # Multi-PDF case: each room knows its PDF via bbox.source_pdf. Single-
    # PDF case (most common): just use the only PDF path. Group page_map
    # by PDF for fast lookup, and also build a single-PDF fallback.
    by_pdf = {}
    for (path, idx), sid in page_map.items():
        by_pdf.setdefault(path, {})[idx] = sid
    single_pdf = pdf_paths[0] if (pdf_paths and len(pdf_paths) == 1) else None

    overrides = 0
    unmapped = 0
    for floor in analysis.get("floors", []) or []:
        for room in floor.get("rooms", []) or []:
            sp = room.get("source_page")
            if sp is None:
                continue
            try:
                page_idx = int(sp) - 1  # source_page is 1-based
            except (TypeError, ValueError):
                continue
            if page_idx < 0:
                continue
            # Find which PDF this room came from. Resolution order:
            #   1. bbox.source_pdf if that exact path is in by_pdf (live run)
            #   2. by basename match against by_pdf keys (re-processing a
            #      stored result where bbox.source_pdf points at a stale
            #      worker /tmp path that no longer exists)
            #   3. single PDF fallback when only one was supplied
            room_pdf = None
            bbox = room.get("bbox") or {}
            bbox_path = bbox.get("source_pdf") if isinstance(bbox, dict) else None
            if bbox_path and bbox_path in by_pdf:
                room_pdf = bbox_path
            elif bbox_path:
                bbox_base = os.path.basename(bbox_path)
                for k in by_pdf:
                    if os.path.basename(k) == bbox_base:
                        room_pdf = k
                        break
            if room_pdf is None and single_pdf:
                room_pdf = single_pdf
            if room_pdf is None:
                continue
            true_sheet = by_pdf.get(room_pdf, {}).get(page_idx)
            if not true_sheet:
                unmapped += 1
                continue
            current = str(room.get("source_sheet", "") or "").strip()
            if current == true_sheet:
                continue
            # Override — stash original for audit.
            room["_source_sheet_llm"] = current
            room["source_sheet"] = true_sheet
            overrides += 1

    if overrides:
        note = (f"[Source Sheet Canonicalization] Overrode {overrides} "
                f"room source_sheet values with the actual sheet IDs "
                f"printed on the PDF pages. Prevents phantom-floor "
                f"duplication caused by the LLM emitting normalized / "
                f"convention-substituted sheet IDs (e.g. 'A-102' for a "
                f"page actually marked 'A2').")
        if unmapped:
            note += f" {unmapped} additional rooms had source_page values " \
                    f"with no detectable sheet ID in the title block."
        existing_notes = analysis.get("notes") or []
        if not isinstance(existing_notes, list):
            existing_notes = [existing_notes] if existing_notes else []
        analysis["notes"] = list(existing_notes) + [note]
        print(f"   🪪 {note}", flush=True)

    analysis["_source_sheets_canonicalized"] = True
    return analysis


def _collect_upload_sheet_numbers(pdf_paths):
    """Zero-API-cost scan: collect every sheet number physically present
    across the uploaded PDFs (normalized).

    Reads only the title-block / bottom-strip regions so a cross-reference
    ("see A-101") on a floor plan is not mistaken for sheet A-101 being in
    the set. Used so RFIs don't request sheets that are already uploaded.
    Returns a set; empty if PyMuPDF is unavailable.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return set()
    sheets = set()
    known_prefixes = tuple(dp for dp, _, _ in _DISCIPLINE_MAP)
    for path in pdf_paths or []:
        try:
            doc = fitz.open(path)
        except Exception:
            continue
        try:
            for page in doc:
                r = page.rect
                for clip in (fitz.Rect(r.width * 0.60, r.height * 0.80, r.width, r.height),
                             fitz.Rect(0, r.height * 0.70, r.width, r.height)):
                    for m in _SHEET_NUMBER_RE.finditer(page.get_text(clip=clip)):
                        prefix = m.group(1).upper()
                        if any(prefix == p or prefix.startswith(p) for p in known_prefixes):
                            sheets.add(_normalize_sheet_token(prefix + m.group(2)))
        finally:
            doc.close()
    return sheets


def _sheets_in_text(text, upload_sheets):
    """Find sheet numbers named in `text` and split them by whether each is
    present in `upload_sheets`. Returns (referenced, present, missing)."""
    referenced, present, missing = [], [], []
    for m in _SHEET_NUMBER_RE.finditer(str(text)):
        prefix = m.group(1).upper()
        if not any(prefix == p or prefix.startswith(p) for p, _, _ in _DISCIPLINE_MAP):
            continue
        norm = _normalize_sheet_token(prefix + m.group(2))
        if norm in referenced:
            continue
        referenced.append(norm)
        (present if norm in upload_sheets else missing).append(norm)
    return referenced, present, missing


def _filter_pdfs_to_sheets(pdf_paths, sheet_hint):
    """Re-run targeting: return pdf paths narrowed to only the pages whose
    sheet number matches `sheet_hint`.

    `sheet_hint` may be a raw string ("A-101, A-201") or a list of tokens. A
    file with no matching sheet is passed through UNCHANGED — an unmatched or
    mistyped hint can never blank a job. Returns (paths, summary_str).
    """
    if not sheet_hint:
        return list(pdf_paths), ""
    raw = re.split(r"[,;]+", sheet_hint) if isinstance(sheet_hint, str) else list(sheet_hint)
    want = {_normalize_sheet_token(t) for t in raw if str(t).strip()}
    want.discard("")
    if not want:
        return list(pdf_paths), ""

    out = []
    matched_pages = 0
    filtered_files = 0
    for p in pdf_paths:
        try:
            cls = _classify_pdf_pages(p)
        except Exception:
            cls = []
        match_idx = sorted(
            c["page_index"] for c in cls
            if c.get("sheet_number")
            and _normalize_sheet_token(c["sheet_number"]) in want
        )
        if match_idx:
            try:
                data = _create_filtered_pdf(p, match_idx)
                tf = tempfile.NamedTemporaryFile(
                    suffix=".pdf", delete=False, prefix="nsai_sheethint_")
                tf.write(data)
                tf.close()
                out.append(tf.name)
                matched_pages += len(match_idx)
                filtered_files += 1
                continue
            except Exception:
                pass  # fall through to the whole file
        out.append(p)  # no match (or filter failed) → analyze the file whole

    if matched_pages:
        summary = (f"sheet hint {sorted(want)} → {matched_pages} matching "
                   f"page(s) across {filtered_files} file(s)")
    else:
        summary = (f"sheet hint {sorted(want)} matched no sheets — "
                   f"analyzing the uploaded file(s) in full")
    print(f"   🎯 Re-run targeting: {summary}")
    return out, summary


def _create_filtered_pdf(pdf_path, page_indices):
    """Create a new PDF containing only the specified pages.  Returns bytes.

    Uses PyPDF2 (same library as the chunking pipeline) to ensure the
    filtered PDF is compatible with Claude's PDF parser.  PyMuPDF's
    insert_pdf can produce larger/incompatible files that cause
    'Could not process PDF' errors.
    """
    reader = PyPDF2.PdfReader(pdf_path)
    writer = PyPDF2.PdfWriter()
    for idx in sorted(page_indices):
        if idx < len(reader.pages):
            writer.add_page(reader.pages[idx])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _summarize_excluded(excluded):
    """Summarize excluded pages by discipline for log output."""
    from collections import Counter
    counts = Counter(c["discipline"] for c in excluded)
    parts = [f"{count} {disc}" for disc, count in sorted(counts.items())]
    return ", ".join(parts)


def _render_pages_to_images(pdf_path, page_numbers, dpi=250,
                             output_format="png", jpeg_quality=85):
    """
    Render specific PDF pages to images at the given DPI using PyMuPDF.
    Returns list of (page_num_0based, base64_string) tuples.

    output_format: "png" (default, lossless) or "jpeg". JPEG at q=85 is
    visually equivalent to PNG for Claude's vision input (which downscales
    everything to 1568px anyway) but compresses ~5-10× better on dense
    architectural raster content — critical for staying under Claude's
    5 MB per-image base64 cap on DD-scale pages.

    At 250 DPI a letter page is ~2080×2690 px (~300-500 KB PNG, ~80-150 KB JPEG).
    """
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    images = []
    zoom = dpi / 72  # PyMuPDF default is 72 DPI
    matrix = fitz.Matrix(zoom, zoom)

    for page_num in page_numbers:
        if page_num >= len(doc):
            continue
        page = doc[page_num]
        pix = page.get_pixmap(matrix=matrix)
        if output_format == "jpeg":
            img_bytes = pix.tobytes("jpeg", jpg_quality=jpeg_quality)
            label = "JPEG"
        else:
            img_bytes = pix.tobytes("png")
            label = "PNG"
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        images.append((page_num, b64))
        print(f"      📸 Rendered page {page_num + 1} → "
              f"{pix.width}×{pix.height} px ({len(img_bytes)/1024:.0f} KB {label})")

    doc.close()
    return images


def _extract_page_text_layer(pdf_path, page_index):
    """
    Extract all text from a single PDF page using PyMuPDF's vector text extraction.

    Returns structured text data with bounding boxes for each text span.
    This is zero-cost (no API call) and sub-second per page.

    Args:
        pdf_path: path to the PDF file
        page_index: 0-based page index

    Returns:
        dict with keys:
            page_rect: {width, height} in points
            blocks: list of {text, bbox: [x0, y0, x1, y1], size, font}
            raw_text: concatenated text string
        or None on failure
    """
    try:
        import fitz
    except ImportError:
        return None

    try:
        doc = fitz.open(pdf_path)
        if page_index >= len(doc):
            doc.close()
            return None

        page = doc[page_index]
        page_rect = {"width": page.rect.width, "height": page.rect.height}

        # Extract with full detail — gives us font info, positions, sizes
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

        blocks = []
        raw_parts = []

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:  # type 0 = text block
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text:
                        continue
                    blocks.append({
                        "text": text,
                        "bbox": list(span.get("bbox", [0, 0, 0, 0])),
                        "size": span.get("size", 0),
                        "font": span.get("font", ""),
                    })
                    raw_parts.append(text)

        doc.close()

        return {
            "page_rect": page_rect,
            "blocks": blocks,
            "raw_text": " ".join(raw_parts),
        }

    except Exception as e:
        print(f"      ⚠️  Text layer extraction failed for page {page_index + 1}: {e}")
        return None


def _parse_floor_plan_text(text_layer):
    """
    Classify extracted text spans from a floor plan page into categories:
    dimensions, room labels, room IDs, and annotations.

    Args:
        text_layer: dict from _extract_page_text_layer()

    Returns:
        dict with keys:
            dimensions: list of {value, bbox}
            room_labels: list of {label, bbox}
            room_ids: list of {id, bbox}
            annotations: list of {text, bbox}
            page_width_pt: float
            page_height_pt: float
        or None if text_layer is None/empty
    """
    if not text_layer or not text_layer.get("blocks"):
        return None

    # Patterns for architectural dimensions: 15'-4", 6'-0", 23.50, 11'-1 1/2"
    dim_pattern = re.compile(
        r"^\d{1,3}['\u2019]\s*-?\s*\d{1,2}\s*\"?"  # 15'-4" or 6'-0
        r"|^\d{1,3}['\u2019]\s*-?\s*\d{1,2}\s+\d/\d"  # 11'-1 1/2
        r"|^\d{1,3}\.\d{1,2}$"  # 23.50 (decimal feet)
        r"|^\d{1,4}['\u2019]\s*-?\s*\d{1,2}['\u2019]?\"?"  # 118'-8"
    )

    # Room label patterns (common architectural room names)
    room_label_words = {
        "LIVING", "KITCHEN", "BEDROOM", "BATHROOM", "BATH", "CLOSET",
        "DINING", "ENTRY", "FOYER", "HALL", "HALLWAY", "CORRIDOR",
        "LAUNDRY", "PANTRY", "STORAGE", "GARAGE", "PARKING", "LOBBY",
        "OFFICE", "STUDY", "DEN", "MASTER", "GREAT ROOM", "FAMILY",
        "MECHANICAL", "ELECTRICAL", "UTILITY", "STAIRWELL", "STAIR",
        "ELEVATOR", "VESTIBULE", "MUDROOM", "PORCH", "BALCONY", "DECK",
        "POWDER", "WIC", "W.I.C.", "WALK-IN", "NOOK", "BREAKFAST",
        "BONUS", "LOFT", "TERRACE", "PATIO", "SUNROOM",
    }

    # Room ID patterns: V-101, C1-A, APT 201, UNIT A, etc.
    room_id_pattern = re.compile(
        r"^[A-Z]{1,3}\s*[-]?\s*\d{2,4}[A-Z]?$"  # V-101, C1A, A201
        r"|^APT\s*\d{2,4}"  # APT 201
        r"|^UNIT\s+[A-Z0-9]"  # UNIT A, UNIT 1
        r"|^[A-Z]\d{1,2}\s*[-]?\s*[A-Z]$"  # C1-A, V2-B
    )

    # Annotation patterns: CLG HT: 9'-0", ceiling heights, etc.
    annotation_pattern = re.compile(
        r"CLG|CEIL|HT:|HEIGHT|A\.F\.F\.|AFF|T\.O\.\s*SLAB"
        r"|FINISH\s*FLOOR|F\.F\.|SIM\.|TYP\."
        r"|NOTE:|REF\.|SEE\s+DETAIL",
        re.IGNORECASE
    )

    dimensions = []
    room_labels = []
    room_ids = []
    annotations = []

    for block in text_layer["blocks"]:
        text = block["text"].strip()
        bbox = block["bbox"]
        upper = text.upper()

        # Check dimensions first (most common on floor plans)
        if dim_pattern.match(text):
            dimensions.append({"value": text, "bbox": bbox})
            continue

        # Check room IDs
        if room_id_pattern.match(upper):
            room_ids.append({"id": text, "bbox": bbox})
            continue

        # Check room labels (exact or contained)
        is_label = False
        for label_word in room_label_words:
            if label_word in upper and len(upper) < 40:
                room_labels.append({"label": text, "bbox": bbox})
                is_label = True
                break
        if is_label:
            continue

        # Check annotations
        if annotation_pattern.search(text) and len(text) < 60:
            annotations.append({"text": text, "bbox": bbox})

    return {
        "dimensions": dimensions,
        "room_labels": room_labels,
        "room_ids": room_ids,
        "annotations": annotations,
        "page_width_pt": text_layer["page_rect"]["width"],
        "page_height_pt": text_layer["page_rect"]["height"],
    }


def _enhance_image_for_extraction(image_bytes, output_format="PNG",
                                   jpeg_quality=85):
    """
    Apply contrast enhancement and sharpening to a rendered PDF page image.
    Architectural drawings benefit from increased contrast (thin lines on white)
    and slight sharpening to make text/dimensions more legible.

    Args:
        image_bytes: raw image bytes (PNG or JPEG — PIL auto-detects)
        output_format: "PNG" (default, lossless) or "JPEG". Use JPEG when
            the caller will send the result to Claude's vision API on a
            dense page that would otherwise exceed the 5 MB base64 cap.
        jpeg_quality: q for JPEG output (ignored for PNG)
    Returns:
        enhanced image bytes in the requested format
    """
    try:
        from PIL import Image, ImageEnhance
        import io

        Image.MAX_IMAGE_PIXELS = None  # architectural sheets are large
        img = Image.open(io.BytesIO(image_bytes))

        # Increase contrast by 1.3x — makes thin architectural lines pop
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.3)

        # Sharpen slightly — helps dimension text legibility
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(1.5)

        buf = io.BytesIO()
        if output_format.upper() == "JPEG":
            # JPEG can't encode alpha; flatten if needed
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        else:
            img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except (ImportError, Exception):
        # PIL not available or image processing failed — return original
        return image_bytes


def _tile_page(pdf_path, page_index, grid=(2, 2), dpi=300, overlap_pct=0.05):
    """
    Render a PDF page as a grid of tile images, each covering a sub-region.

    Instead of one full-page image (which gets downscaled to 1568px by Claude),
    splitting into tiles means each tile covers less area and dimension text
    stays readable even after downscaling.

    Args:
        pdf_path: path to the PDF
        page_index: 0-based page index
        grid: (rows, cols) tuple for tile grid
        dpi: rendering DPI
        overlap_pct: fractional overlap between tiles (0.05 = 5%)

    Returns:
        list of (tile_label, base64_png) tuples, or empty list on failure
    """
    try:
        import fitz
    except ImportError:
        return []

    try:
        doc = fitz.open(pdf_path)
        if page_index >= len(doc):
            doc.close()
            return []

        page = doc[page_index]
        page_rect = page.rect
        pw = page_rect.width
        ph = page_rect.height

        rows, cols = grid
        # Tile dimensions with overlap
        tile_w = pw / cols
        tile_h = ph / rows
        overlap_w = tile_w * overlap_pct
        overlap_h = tile_h * overlap_pct

        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        tiles = []
        for r in range(rows):
            for c in range(cols):
                # Calculate clip rect with overlap
                x0 = max(0, c * tile_w - overlap_w)
                y0 = max(0, r * tile_h - overlap_h)
                x1 = min(pw, (c + 1) * tile_w + overlap_w)
                y1 = min(ph, (r + 1) * tile_h + overlap_h)

                clip = fitz.Rect(x0, y0, x1, y1)
                pix = page.get_pixmap(matrix=matrix, clip=clip)
                png_bytes = pix.tobytes("png")

                # Apply image enhancement (contrast + sharpness)
                enhanced = _enhance_image_for_extraction(png_bytes)

                # Ensure tile stays under 3.5MB raw (< 5MB base64)
                MAX_TILE_BYTES = 3500 * 1024  # 3.5 MB raw → ~4.7 MB base64
                tile_media = "image/png"
                if len(enhanced) > MAX_TILE_BYTES:
                    try:
                        from PIL import Image as _PILTile
                        from io import BytesIO as _BIOTile
                        img = _PILTile.open(_BIOTile(enhanced))
                        buf = _BIOTile()
                        img.save(buf, format="JPEG", quality=70)
                        enhanced = buf.getvalue()
                        tile_media = "image/jpeg"
                    except Exception:
                        pass  # keep PNG if PIL unavailable

                b64 = base64.standard_b64encode(enhanced).decode("utf-8")
                label = f"R{r+1}C{c+1}"
                tiles.append((label, b64, tile_media))

                print(f"      📐 Tile {label}: {pix.width}×{pix.height}px "
                      f"({len(enhanced)/1024:.0f} KB)")

        doc.close()
        return tiles

    except Exception as e:
        print(f"      ⚠️  Tiling failed for page {page_index + 1}: {e}")
        return []


def _format_text_layer_context(parsed_pages):
    """
    Format parsed text layer data into a prompt context string.

    Takes a dict of {page_index: parsed_data} and creates a human-readable
    summary of all extracted dimensions, room labels, room IDs, and annotations
    that Claude can use as primary reference data.

    Args:
        parsed_pages: dict mapping page_index → _parse_floor_plan_text() result

    Returns:
        str: formatted context string to prepend to extraction prompt,
             or empty string if no useful data found
    """
    if not parsed_pages:
        return ""

    parts = [
        "\n═══════════════════════════════════════════════════════════",
        "PRE-EXTRACTED TEXT LAYER (from PDF vector data — exact values):",
        "═══════════════════════════════════════════════════════════",
    ]

    has_content = False
    for page_idx in sorted(parsed_pages.keys()):
        parsed = parsed_pages[page_idx]
        if not parsed:
            continue

        dims = parsed.get("dimensions", [])
        labels = parsed.get("room_labels", [])
        ids = parsed.get("room_ids", [])
        annots = parsed.get("annotations", [])

        if not (dims or labels or ids or annots):
            continue

        has_content = True
        parts.append(f"\n--- Page {page_idx + 1} ---")

        if dims:
            dim_vals = [d["value"] for d in dims]
            parts.append(f"  Dimensions found ({len(dims)}): "
                        f"{', '.join(dim_vals[:30])}"
                        + (f" ... (+{len(dims)-30} more)" if len(dims) > 30 else ""))
        if labels:
            label_vals = list(dict.fromkeys(l["label"] for l in labels))  # dedupe
            parts.append(f"  Room labels: {', '.join(label_vals[:20])}")
        if ids:
            id_vals = list(dict.fromkeys(i["id"] for i in ids))  # dedupe
            parts.append(f"  Room IDs: {', '.join(id_vals[:20])}")
        if annots:
            annot_vals = list(dict.fromkeys(a["text"] for a in annots))[:10]
            parts.append(f"  Annotations: {', '.join(annot_vals)}")

    if not has_content:
        return ""

    parts.append("")
    parts.append(
        "IMPORTANT: Use these dimension values as your PRIMARY source for room measurements.")
    parts.append(
        "The tiled images show visual layout — cross-reference with extracted text above.")
    parts.append(
        "When a dimension value from the text layer conflicts with what you see in images, "
        "TRUST the text layer values (they are extracted directly from the PDF vector data).")
    parts.append(
        "═══════════════════════════════════════════════════════════\n")

    return "\n".join(parts)


def _is_large_format_page(pdf_path, page_index, threshold_pt=2000):
    """
    Check if a PDF page is large-format (architectural D/E size).

    Large-format pages have dimensions that cause Claude's 1568px downscale
    to make dimension text unreadable. These need tile-based extraction.

    Args:
        pdf_path: path to the PDF
        page_index: 0-based page index
        threshold_pt: minimum page dimension in points to qualify as large-format
                      (2000pt ≈ 28 inches)

    Returns:
        (is_large, width_pt, height_pt) tuple
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        if page_index >= len(doc):
            doc.close()
            return (False, 0, 0)
        page = doc[page_index]
        w = page.rect.width
        h = page.rect.height
        doc.close()
        return (max(w, h) >= threshold_pt, w, h)
    except Exception:
        return (False, 0, 0)


def _render_page_to_jpeg_b64(pdf_path, page_index, dpi=300, quality=90,
                              max_dim=7800):
    """Render a single PDF page as a base64-encoded JPEG.

    DPI is the requested rendering resolution; if the resulting image's
    long edge exceeds max_dim, the image is downscaled (preserving aspect
    ratio) so the long edge equals max_dim. This keeps us under Anthropic's
    8000 px image dimension limit while pushing toward the highest fidelity
    a single image can carry.

    Returns (jpeg_b64_str, width_px, height_px) or None on failure.
    """
    try:
        import fitz
        from PIL import Image as _PILImage
        import io as _io
    except ImportError:
        return None

    try:
        doc = fitz.open(pdf_path)
        try:
            if page_index >= len(doc):
                return None
            page = doc[page_index]
            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=matrix)
            img = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
        finally:
            doc.close()

        if max(img.size) > max_dim:
            scale = max_dim / max(img.size)
            new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
            img = img.resize(new_size, _PILImage.LANCZOS)

        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
        return (b64, img.size[0], img.size[1])
    except Exception as e:
        print(f"      ⚠️  Render failed for page {page_index + 1}: {e}")
        return None


def _format_page_text_for_prompt(text_layer, max_chars=20000):
    """Convert _extract_page_text_layer output into a compact text block
    suitable for a Claude content block.

    Includes positional hints (top-left x/y in points) so Claude can correlate
    text spans with the image. Truncates at max_chars to stay within prompt
    budgets on extremely text-dense sheets.
    """
    if not text_layer or not text_layer.get("blocks"):
        return ""

    rect = text_layer.get("page_rect", {})
    pw = rect.get("width", 0)
    ph = rect.get("height", 0)

    lines = [
        f"PAGE TEXT LAYER (vector text extracted via PyMuPDF — read these losslessly):",
        f"Page size (points): {pw:.0f} × {ph:.0f}",
        f"",
        f"Format: <x>,<y>: <text>  where x,y is the top-left of the text span in PDF points.",
        f"",
    ]

    blocks = text_layer["blocks"]
    blocks_sorted = sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))

    out = []
    used = 0
    for b in blocks_sorted:
        bx, by = b["bbox"][0], b["bbox"][1]
        line = f"{bx:.0f},{by:.0f}: {b['text']}"
        if used + len(line) + 1 > max_chars:
            out.append(f"... [{len(blocks) - len(out)} more text spans truncated]")
            break
        out.append(line)
        used += len(line) + 1

    return "\n".join(lines + out)


def _analyze_page_multimodal(client, pdf_path, page_index, prompt_text,
                              label="", dpi=300, jpeg_quality=90,
                              max_retries=5, base_delay=30):
    """Multi-modal extraction for a single oversized PDF page.

    Sends Claude a single API call containing:
      1. A rendered JPEG of the page (capped at 7800 px long edge)
      2. A text content block with the PyMuPDF-extracted vector text layer
         (positions + text spans), so dimension labels and room IDs are
         readable losslessly even when JPEG compression blurs them.
      3. The standard extraction prompt.

    Mirrors `_call_api`'s retry behavior for rate-limit / 5xx / timeout.

    Returns the response text (Claude's JSON output as a string) on success,
    or None on unrecoverable failure.
    """
    rendered = _render_page_to_jpeg_b64(pdf_path, page_index, dpi=dpi,
                                         quality=jpeg_quality)
    if rendered is None:
        print(f"      ⚠️  Multi-modal: could not render page {page_index + 1}")
        return None
    img_b64, img_w, img_h = rendered

    text_layer = _extract_page_text_layer(pdf_path, page_index)
    text_block = _format_page_text_for_prompt(text_layer) if text_layer else ""

    img_kb = len(img_b64) * 3 // 4 // 1024  # rough decoded size
    text_chars = len(text_block)
    print(f"      🔀 Multi-modal page {page_index + 1}: image {img_w}×{img_h}px "
          f"(~{img_kb} KB), text layer {text_chars} chars")

    content_blocks = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": img_b64,
            },
        }
    ]
    if text_block:
        content_blocks.append({
            "type": "text",
            "text": text_block,
        })
    content_blocks.append({
        "type": "text",
        "text": prompt_text,
    })

    if label:
        print(f"      📨 {label}")

    for attempt in range(max_retries):
        try:
            result_parts = []
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=64000,
                temperature=0,
                timeout=300.0,
                messages=[{"role": "user", "content": content_blocks}],
            ) as stream:
                for text in stream.text_stream:
                    result_parts.append(text)
            return "".join(result_parts)
        except anthropic.RateLimitError:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"      ⏳ Multi-modal rate limit — waiting {delay}s "
                      f"(attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                print(f"      ❌ Multi-modal: rate limit exhausted on page {page_index + 1}")
                return None
        except anthropic.InternalServerError:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"      ⏳ Multi-modal API overloaded — waiting {delay}s "
                      f"(attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                print(f"      ❌ Multi-modal: server errors exhausted on page {page_index + 1}")
                return None
        except anthropic.APITimeoutError:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"      ⏳ Multi-modal timeout — waiting {delay}s "
                      f"(attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                print(f"      ❌ Multi-modal: timeouts exhausted on page {page_index + 1}")
                return None
        except anthropic.BadRequestError as e:
            print(f"      ❌ Multi-modal: bad request on page {page_index + 1} — {str(e)[:120]}")
            return None
        except Exception as e:
            print(f"      ❌ Multi-modal: unexpected error on page {page_index + 1} — "
                  f"{type(e).__name__}: {str(e)[:120]}")
            return None
    return None


def _multimodal_chunk_retry(client, chunk_path, prompt_text, chunk_label=""):
    """Retry a chunk that failed native-PDF mode (typically 413) by sending
    each page through `_analyze_page_multimodal` and concatenating responses.

    Returns the merged response text, or None if no pages produced output.
    """
    try:
        reader = PyPDF2.PdfReader(chunk_path)
    except Exception as e:
        print(f"   ⚠️  Multi-modal: could not open {chunk_label} — {e}")
        return None

    total_pages = len(reader.pages)
    print(f"   🔀 {chunk_label}: multi-modal retry across {total_pages} page(s)")

    page_responses = []
    for i in range(total_pages):
        resp = _analyze_page_multimodal(
            client, chunk_path, i, prompt_text,
            label=f"{chunk_label} page {i+1}/{total_pages}",
        )
        if resp:
            page_responses.append(resp)
        else:
            print(f"      ⚠️  Multi-modal: page {i+1} produced no output")

    if not page_responses:
        print(f"   ❌ Multi-modal: no pages produced output for {chunk_label}")
        return None

    if len(page_responses) == 1:
        return page_responses[0]

    try:
        return _merge_chunk_responses(page_responses)
    except Exception as e:
        print(f"   ⚠️  Multi-modal merge failed ({e}) — returning first page only")
        return page_responses[0]


def _analyze_floor_plan_as_images(client, pdf_path, scope_notes="",
                                   schedule_hints=None, building_inventory=None,
                                   project_overview=None):
    """
    Image-based fallback for floor plan extraction.

    When native PDF parsing returns 0 rooms after retries, this function
    renders each page as a high-resolution PNG image and sends image content
    blocks to Claude instead of a PDF document block.

    This gives Claude a different visual representation that may work better
    for certain architectural drawings with complex/layered vector data.

    Returns:
        (pdf_path, analysis_dict) tuple or None on failure.
        Same format as analyze_and_parse() for seamless integration.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("   ❌ Image fallback requires PyMuPDF — skipping")
        return None

    try:
        from config import IMAGE_FALLBACK_DPI, IMAGE_FALLBACK_ENHANCE
    except ImportError:
        IMAGE_FALLBACK_DPI = 300
        IMAGE_FALLBACK_ENHANCE = True

    filename = os.path.basename(pdf_path)

    # Determine page count
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    doc.close()

    # Render all pages as JPEG (q=85). PNG of DD-scale pages routinely
    # exceeded Claude's 5 MB per-image cap; JPEG q=85 is visually equivalent
    # for Claude's downscaled-to-1568px vision input but ~5-10× smaller.
    page_numbers = list(range(total_pages))
    print(f"\n   🖼️  IMAGE FALLBACK: Rendering {total_pages} page(s) "
          f"at {IMAGE_FALLBACK_DPI} DPI (JPEG q=85)...")
    images = _render_pages_to_images(pdf_path, page_numbers,
                                      dpi=IMAGE_FALLBACK_DPI,
                                      output_format="jpeg", jpeg_quality=85)

    if not images:
        print(f"   ❌ Image fallback: no pages rendered")
        return None

    # Safety check 1: auto-reduce DPI if any page exceeds Claude's 8000px limit
    MAX_DIMENSION = 7999
    current_dpi = IMAGE_FALLBACK_DPI
    for page_num, b64_data in images:
        raw_bytes = base64.standard_b64decode(b64_data)
        try:
            from PIL import Image as _PILImage
            import io as _io
            _PILImage.MAX_IMAGE_PIXELS = None  # architectural sheets are large
            img = _PILImage.open(_io.BytesIO(raw_bytes))
            w, h = img.size
            if max(w, h) > MAX_DIMENSION:
                # Re-render at reduced DPI
                scale = MAX_DIMENSION / max(w, h)
                current_dpi = int(IMAGE_FALLBACK_DPI * scale)
                print(f"      ⚠️  Page {page_num + 1} is {w}×{h}px — "
                      f"re-rendering at {current_dpi} DPI")
                images = _render_pages_to_images(pdf_path, page_numbers,
                                                  dpi=current_dpi,
                                                  output_format="jpeg",
                                                  jpeg_quality=85)
                break  # re-rendered all pages at lower DPI
        except ImportError:
            pass  # can't check dimensions without PIL, proceed anyway

    # Safety check 2: ensure no image exceeds Claude's 5 MB base64 cap.
    # JPEG q=85 should keep us under, but on very dense pages a step-down
    # to q=70 + lower DPI may still be needed. Iterate up to 3 reductions.
    MAX_B64_BYTES = 5 * 1024 * 1024
    for _attempt in range(3):
        oversized = [(p, len(b64)) for p, b64 in images
                     if len(b64.encode("ascii")) > MAX_B64_BYTES]
        if not oversized:
            break
        # Reduce DPI by 25% and quality to 70, re-render everything
        current_dpi = max(120, int(current_dpi * 0.75))
        print(f"      ⚠️  {len(oversized)} page(s) exceed 5 MB cap — "
              f"re-rendering all at {current_dpi} DPI, JPEG q=70")
        images = _render_pages_to_images(pdf_path, page_numbers,
                                          dpi=current_dpi,
                                          output_format="jpeg",
                                          jpeg_quality=70)

    # Optional image enhancement — preserve JPEG format (we render JPEG
    # to stay under Claude's 5 MB cap; PNG output here would re-inflate).
    if IMAGE_FALLBACK_ENHANCE:
        enhanced_images = []
        for page_num, b64_data in images:
            raw_bytes = base64.standard_b64decode(b64_data)
            enhanced_bytes = _enhance_image_for_extraction(
                raw_bytes, output_format="JPEG", jpeg_quality=85)
            enhanced_b64 = base64.standard_b64encode(
                enhanced_bytes).decode("utf-8")
            enhanced_images.append((page_num, enhanced_b64))
        images = enhanced_images

    # Build the same extraction prompt used by the PDF path
    effective_prompt = _build_extraction_prompt(
        scope_notes=scope_notes, schedule_hints=schedule_hints,
        building_inventory=building_inventory,
        project_overview=project_overview)

    # Batch images to avoid 413 Payload Too Large errors.
    # Max ~6 full-page images per API call (each ~1-3 MB at 190 DPI).
    MAX_IMAGES_PER_CALL = 6
    image_batches = []
    for i in range(0, len(images), MAX_IMAGES_PER_CALL):
        image_batches.append(images[i:i + MAX_IMAGES_PER_CALL])

    all_batch_results = []
    # Per-batch tracking so a dropped image batch is visible downstream
    # (same rationale as the enhanced/tiled path's _chunk_tracking).
    _img_total_batches = len(image_batches)
    _img_ok_batches = []

    for batch_idx, batch_images in enumerate(image_batches):
        if len(image_batches) > 1:
            print(f"   🔍 Batch {batch_idx + 1}/{len(image_batches)}: "
                  f"sending {len(batch_images)} page image(s)...")
            if batch_idx > 0:
                time.sleep(15)  # cooldown between batches
        else:
            print(f"   🔍 Sending {len(batch_images)} page image(s) to Claude...")

        content_blocks = []
        for page_num, b64_data in batch_images:
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64_data
                }
            })
        content_blocks.append({
            "type": "text",
            "text": effective_prompt
        })

        result_parts = []
        max_retries = 5
        base_delay = 30

        for attempt in range(max_retries):
            try:
                with client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=64000,
                    temperature=0,
                    timeout=300.0,
                    messages=[{"role": "user", "content": content_blocks}]
                ) as stream:
                    for text in stream.text_stream:
                        result_parts.append(text)
                break
            except anthropic.RateLimitError:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"   ⏳ Rate limit — waiting {delay}s "
                          f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    print(f"   ❌ Image fallback: rate limit exhausted")
                    continue
            except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"   ⏳ API error — waiting {delay}s "
                          f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    print(f"   ❌ Image fallback batch failed: {e}")
                    continue
            except Exception as e:
                print(f"   ❌ Image fallback error: {e}")
                break

        result_text = "".join(result_parts)
        if not result_text:
            continue

        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            try:
                analysis = json.loads(json_match.group())
                rooms = analysis.get('project_info', {}).get(
                    'total_rooms_found', 0)
                if rooms > 0:
                    all_batch_results.append(analysis)
                    _img_ok_batches.append(batch_idx + 1)
                    print(f"   🖼️  Batch {batch_idx + 1}: extracted {rooms} rooms")
            except json.JSONDecodeError:
                pass

    # Merge batch results
    if not all_batch_results:
        print(f"   ❌ Image fallback: no rooms found in any batch")
        return None

    if len(all_batch_results) == 1:
        final = all_batch_results[0]
    else:
        # Merge with proper floor dedup + template overlap detection
        final = _merge_batch_results(all_batch_results)

    rooms = final.get('project_info', {}).get('total_rooms_found', 0)
    print(f"   🖼️  Image fallback extracted {rooms} rooms total")
    _attach_bbox_anchors(final, pdf_path)
    _img_failed = [b for b in range(1, _img_total_batches + 1)
                   if b not in _img_ok_batches]
    final["_chunk_tracking"] = {
        "mode": "image_fallback",
        "total_chunks": _img_total_batches,
        "chunks_succeeded": list(_img_ok_batches),
        "chunks_failed": _img_failed,
        "chunk_page_ranges": [],
    }
    return (pdf_path, final)


def _analyze_with_enhanced_extraction(client, pdf_path, scope_notes="",
                                       schedule_hints=None, building_inventory=None,
                                       page_indices=None, project_overview=None):
    """
    Enhanced extraction for large-format (DD-scale) architectural PDFs.

    Combines two techniques to read dimensions that Claude can't see at
    native resolution:

    1. Text Layer Pre-Extraction: PyMuPDF extracts all dimension text, room
       labels, and room IDs directly from the PDF vector data (zero API cost).

    2. Page Tiling: Each page is split into a 2×2 or 3×3 grid of tiles.
       Each tile covers a smaller area, so after Claude's 1568px downscale,
       dimension text is 2-3× larger and readable.

    Args:
        page_indices: Optional list of 0-based page indices to process.
                     If None, processes all pages. For combined-volume PDFs,
                     pass only painting-relevant pages to avoid wasting tokens.

    Returns:
        (pdf_path, analysis_dict) tuple or None on failure.
        Same format as analyze_and_parse() for seamless pipeline integration.
    """
    try:
        import fitz
    except ImportError:
        print("   ❌ Enhanced extraction requires PyMuPDF — skipping")
        return None

    try:
        from config import (ENHANCED_TILE_DPI, ENHANCED_TILE_GRID,
                            ENHANCED_TILE_GRID_LARGE, LARGE_FORMAT_THRESHOLD_PT,
                            ENHANCED_TILE_OVERLAP_PCT)
    except ImportError:
        ENHANCED_TILE_DPI = 300
        ENHANCED_TILE_GRID = (2, 2)
        ENHANCED_TILE_GRID_LARGE = (3, 3)
        LARGE_FORMAT_THRESHOLD_PT = 2000
        ENHANCED_TILE_OVERLAP_PCT = 0.05

    filename = os.path.basename(pdf_path)

    # Determine which pages to process
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    doc.close()

    if page_indices is None:
        # No page list provided — use all pages
        page_indices = list(range(total_pages))
    else:
        # Filter out any indices beyond the actual page count
        page_indices = [i for i in page_indices if 0 <= i < total_pages]

    if not page_indices:
        print(f"   ❌ Enhanced extraction: no valid pages to process")
        return None

    # Phase 1: Extract text layer from ALL provided pages (zero API cost)
    # This helps us identify which pages are actually floor plans (have dimensions)
    print(f"   📝 Phase 1: Extracting text layer from {len(page_indices)} pages...")
    parsed_pages = {}
    total_dims = 0
    total_labels = 0
    total_ids = 0

    for pg_idx in page_indices:
        text_layer = _extract_page_text_layer(pdf_path, pg_idx)
        if text_layer:
            parsed = _parse_floor_plan_text(text_layer)
            if parsed:
                parsed_pages[pg_idx] = parsed
                n_dims = len(parsed.get("dimensions", []))
                n_labels = len(parsed.get("room_labels", []))
                n_ids = len(parsed.get("room_ids", []))
                total_dims += n_dims
                total_labels += n_labels
                total_ids += n_ids

    print(f"   📝 Text layer: {total_dims} dimensions, {total_labels} labels, "
          f"{total_ids} IDs across {len(parsed_pages)} pages")

    # Filter to pages that look like floor plans: have dimensions OR room labels/IDs
    # This prevents tiling 47 painting-relevant pages when only 10 are floor plans
    floor_plan_pages = []
    for pg_idx in page_indices:
        parsed = parsed_pages.get(pg_idx)
        if parsed:
            n_dims = len(parsed.get("dimensions", []))
            n_labels = len(parsed.get("room_labels", []))
            n_ids = len(parsed.get("room_ids", []))
            # A floor plan page typically has dimensions AND (labels or IDs)
            if n_dims >= 3 or (n_dims >= 1 and (n_labels >= 1 or n_ids >= 1)):
                floor_plan_pages.append(pg_idx)

    # --- Robustness: a sparse text layer must not drop rasterized plan sheets ---
    # Large-format architectural sheets are frequently rasterized scans whose
    # DRAWING has no extractable text — so the dims/labels filter above scores
    # the real plan sheets 0 and we historically tiled only the 1-2 incidental
    # text-bearing pages. This was the primary under-extraction cause on Five
    # Below: 1 of 19 painting-relevant pages had a parseable dims text layer, so
    # the A1.0 floor plan, A1.1 fixture plan, and A2.0 RCP were never tiled —
    # 6 rooms / 6,542 SF walls vs an ~11,000 SF manual takeoff.
    #
    # The TITLE-BLOCK text survives rasterization (it's how page classification
    # reads the sheet number), so classify each painting-relevant page as a plan
    # sheet by its title text and tile those. This recovers scanned plan sheets
    # WITHOUT tiling elevations / sections / details / schedules (which waste
    # tokens and can fabricate rooms from elevation dimensions).
    def _is_plan_sheet(pg_idx):
        tl = _extract_page_text_layer(pdf_path, pg_idx)
        txt = (tl or {}).get("raw_text", "") if isinstance(tl, dict) else ""
        txt = str(txt).lower()
        if not txt:
            return False
        STRONG_PLAN = (
            "floor plan", "reflected ceiling", "ceiling plan", "finish plan",
            "enlarged plan", "fixture plan", "dimension plan", "partition plan",
            "furniture plan", "equipment plan", "roof plan", "slab plan",
            "life safety plan", "demolition plan", "overall plan",
        )
        NON_PLAN = ("elevation", "section", "detail", "schedule")
        if any(k in txt for k in STRONG_PLAN):
            return True
        # Bare "plan" (e.g. a key/code plan) counts only when the sheet isn't
        # dominated by elevation/section/detail/schedule content.
        if "plan" in txt and not any(k in txt for k in NON_PLAN):
            return True
        return False

    _plan_sheets = [pg_idx for pg_idx in page_indices if _is_plan_sheet(pg_idx)]
    _added = sorted(set(_plan_sheets) - set(floor_plan_pages))
    if _added:
        print(f"   🔬 Plan-sheet recovery: text-layer dims identified "
              f"{len(floor_plan_pages)} page(s); title-block text identifies "
              f"{len(_plan_sheets)} plan sheet(s) — adding {len(_added)} rasterized "
              f"plan sheet(s) {[p + 1 for p in _added]} so they get measured "
              f"(under-extraction guard).")
        floor_plan_pages = sorted(set(floor_plan_pages) | set(_plan_sheets))

    if not floor_plan_pages:
        # Fall back to all large-format pages if text layer didn't help
        for pg_idx in page_indices:
            is_large, _, _ = _is_large_format_page(
                pdf_path, pg_idx, LARGE_FORMAT_THRESHOLD_PT)
            if is_large:
                floor_plan_pages.append(pg_idx)

    if not floor_plan_pages:
        print(f"   ❌ Enhanced extraction: no floor plan pages identified")
        return None

    # Cap at 12 pages max to keep API payload reasonable (12 × 4 = 48 tiles)
    MAX_PAGES_PER_CALL = int(os.environ.get("NIGHTSHIFT_MAX_TILE_PAGES", "12"))
    if len(floor_plan_pages) > MAX_PAGES_PER_CALL:
        # Prioritize pages with most dimensions (they have the most measurable rooms)
        floor_plan_pages.sort(
            key=lambda pg: len(parsed_pages.get(pg, {}).get("dimensions", [])),
            reverse=True)
        floor_plan_pages = sorted(floor_plan_pages[:MAX_PAGES_PER_CALL])

    print(f"   🔬 ENHANCED EXTRACTION: Tiling {len(floor_plan_pages)} floor plan page(s) "
          f"(of {len(page_indices)} painting-relevant, {total_pages} total)")

    # Log the selected pages
    for pg_idx in floor_plan_pages:
        parsed = parsed_pages.get(pg_idx, {})
        n_dims = len(parsed.get("dimensions", []))
        n_labels = len(parsed.get("room_labels", []))
        n_ids = len(parsed.get("room_ids", []))
        print(f"      Page {pg_idx + 1}: {n_dims} dims, {n_labels} labels, {n_ids} IDs")

    # Format text context for prompt injection (all parsed pages, not just tiled ones)
    text_context = _format_text_layer_context(parsed_pages)

    # Phase 2: Render tiles for floor plan pages only
    print(f"   📐 Phase 2: Rendering page tiles...")
    all_tiles = []  # list of (page_idx, tile_label, base64_png)

    for pg_idx in floor_plan_pages:
        is_large, w_pt, h_pt = _is_large_format_page(
            pdf_path, pg_idx, LARGE_FORMAT_THRESHOLD_PT)

        if is_large:
            w_in = w_pt / 72.0
            h_in = h_pt / 72.0
            if max(w_in, h_in) > 36:
                grid = ENHANCED_TILE_GRID_LARGE
            else:
                grid = ENHANCED_TILE_GRID
            print(f"      Page {pg_idx + 1}: {w_in:.1f}\" × {h_in:.1f}\" "
                  f"(large format) → {grid[0]}×{grid[1]} tiles")
        else:
            grid = ENHANCED_TILE_GRID
            w_in = w_pt / 72.0
            h_in = h_pt / 72.0
            print(f"      Page {pg_idx + 1}: {w_in:.1f}\" × {h_in:.1f}\" → "
                  f"{grid[0]}×{grid[1]} tiles")

        tiles = _tile_page(pdf_path, pg_idx, grid=grid, dpi=ENHANCED_TILE_DPI,
                           overlap_pct=ENHANCED_TILE_OVERLAP_PCT)
        for tile_data in tiles:
            if len(tile_data) == 3:
                label, b64, media = tile_data
            else:
                label, b64 = tile_data
                media = "image/png"
            all_tiles.append((pg_idx, label, b64, media))

    if not all_tiles:
        print(f"   ❌ Enhanced extraction: no tiles rendered")
        return None

    print(f"   📐 Rendered {len(all_tiles)} tiles across {len(floor_plan_pages)} pages")

    # Phase 3: Send tiles to Claude in batches if needed
    # Max ~20 tiles per API call (each ~1-2MB base64 → ~5MB per tile in request)
    MAX_TILES_PER_CALL = 9  # one full 3×3 page or two 2×2 pages per call
    tile_batches = []
    for i in range(0, len(all_tiles), MAX_TILES_PER_CALL):
        tile_batches.append(all_tiles[i:i + MAX_TILES_PER_CALL])

    # Free the master list now — batches hold references, all_tiles just held duplicates
    del all_tiles

    all_analysis_results = []
    # Tile-batch tracking — the enhanced (tiled) path is the large-format
    # equivalent of the chunked-vector path's _chunk_tracking. Without it,
    # large-format jobs (which route here precisely because native vector
    # failed) ship chunk_tracking=null, which (a) blinds the >=50%-chunks-
    # failed manual-review trigger and (b) makes the multi-pass fallback's
    # "fewest dropped chunks" tiering meaningless. Record which batches yielded
    # rooms so a dropped tile batch is visible downstream.
    _enh_total_batches = len(tile_batches)
    _enh_ok_batches = []

    for batch_idx, batch_tiles in enumerate(tile_batches):
        if len(tile_batches) > 1:
            print(f"\n   🔍 Batch {batch_idx + 1}/{len(tile_batches)}: "
                  f"sending {len(batch_tiles)} tiles...")
            if batch_idx > 0:
                time.sleep(15)  # cooldown between batches
        else:
            print(f"   🔍 Sending {len(batch_tiles)} tiles + text context to Claude...")

        effective_prompt = _build_extraction_prompt(
            scope_notes=scope_notes, schedule_hints=schedule_hints,
            building_inventory=building_inventory,
            text_layer_context=text_context,
            project_overview=project_overview)

        # Build content blocks
        content_blocks = []
        current_page = -1
        for tile_data in batch_tiles:
            if len(tile_data) == 4:
                pg_idx, tile_label, b64_data, tile_media = tile_data
            else:
                pg_idx, tile_label, b64_data = tile_data
                tile_media = "image/png"
            if pg_idx != current_page:
                content_blocks.append({
                    "type": "text",
                    "text": f"[Page {pg_idx + 1} — tiled for detail. "
                            f"Cross-reference with pre-extracted text layer above.]"
                })
                current_page = pg_idx
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": tile_media,
                    "data": b64_data
                }
            })
        content_blocks.append({
            "type": "text",
            "text": effective_prompt
        })

        # Make the API call
        result_parts = []
        max_retries = 5
        base_delay = 30

        for attempt in range(max_retries):
            try:
                with client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=64000,
                    temperature=0,
                    timeout=600.0,
                    messages=[{"role": "user", "content": content_blocks}]
                ) as stream:
                    for text in stream.text_stream:
                        result_parts.append(text)
                break
            except anthropic.RateLimitError:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"   ⏳ Rate limit — waiting {delay}s "
                          f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    print(f"   ❌ Enhanced extraction: rate limit exhausted")
                    continue
            except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"   ⏳ API error — waiting {delay}s "
                          f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    print(f"   ❌ Enhanced extraction batch failed: {e}")
                    continue
            except Exception as e:
                print(f"   ❌ Enhanced extraction error: {e}")
                break

        result_text = "".join(result_parts)

        # Empty-response retry: a successful API call that returned no text
        # is almost always a transient model issue, not a real "nothing to
        # extract." Retry once before giving up on the batch.
        if not result_text:
            print(f"   ⚠️  Batch {batch_idx + 1}: empty API response — retrying once")
            time.sleep(15)
            try:
                result_parts = []
                with client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=64000,
                    temperature=0,
                    timeout=600.0,
                    messages=[{"role": "user", "content": content_blocks}]
                ) as stream:
                    for text in stream.text_stream:
                        result_parts.append(text)
                result_text = "".join(result_parts)
            except Exception as e:
                print(f"   ❌ Batch {batch_idx + 1} empty-response retry failed: {e}")
            if not result_text:
                print(f"   ❌ Batch {batch_idx + 1}: still empty after retry — skipping")
                tile_batches[batch_idx] = None
                continue

        # Parse JSON response
        analysis = None
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            try:
                analysis = json.loads(json_match.group())
            except json.JSONDecodeError:
                print(f"   ❌ Enhanced extraction batch: could not parse JSON")

        rooms = (analysis or {}).get('project_info', {}).get('total_rooms_found', 0)
        print(f"   🔬 Batch {batch_idx + 1}: extracted {rooms} rooms")

        # Suspicious-empty retry: 0 rooms on a multi-tile batch (≥4 tiles)
        # from architectural pages is almost never correct — multi-page
        # plan tiles virtually always contain labeled spaces. This is the
        # silent-empty pattern observed across multiple Albany B&N reruns.
        # Retry once with a sharpened prompt directing Claude to be
        # exhaustive about identifying ANY labeled space.
        if rooms == 0 and len(batch_tiles) >= 4:
            print(f"   ⚠️  Batch {batch_idx + 1}: 0 rooms on "
                  f"{len(batch_tiles)}-tile batch is suspicious — "
                  f"retrying with sharpened prompt")
            time.sleep(15)
            sharpened_blocks = list(content_blocks[:-1]) + [{
                "type": "text",
                "text": (
                    effective_prompt + "\n\n"
                    "RETRY DIRECTIVE: A prior attempt at this exact batch "
                    "returned ZERO rooms. That is almost never correct for a "
                    "multi-tile batch of architectural plan pages. Re-examine "
                    "each tile carefully and identify EVERY labeled space — "
                    "rooms, corridors, stairs, vestibules, mechanical / "
                    "electrical / storage areas, back-of-house, restrooms, "
                    "sales floors, dining areas. Return each one in the "
                    "rooms array with a unique room_id and any dimensions "
                    "you can read. If a space has no readable label, still "
                    "include it with a generic room_name like 'Unlabeled "
                    "Space (Tile R1C2)' so it can be reviewed downstream."
                )
            }]
            try:
                result_parts2 = []
                with client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=64000,
                    temperature=0,
                    timeout=600.0,
                    messages=[{"role": "user", "content": sharpened_blocks}]
                ) as stream:
                    for text in stream.text_stream:
                        result_parts2.append(text)
                result_text2 = "".join(result_parts2)
                json_match2 = re.search(r'\{.*\}', result_text2, re.DOTALL)
                if json_match2:
                    try:
                        analysis2 = json.loads(json_match2.group())
                        rooms2 = analysis2.get('project_info', {}).get(
                            'total_rooms_found', 0)
                        print(f"   🔬 Batch {batch_idx + 1} retry: "
                              f"extracted {rooms2} rooms")
                        if rooms2 > 0:
                            analysis = analysis2
                            rooms = rooms2
                    except json.JSONDecodeError:
                        print(f"   ❌ Batch {batch_idx + 1} retry: "
                              f"could not parse JSON")
            except Exception as e:
                print(f"   ❌ Batch {batch_idx + 1} sharpened retry failed: {e}")

        if rooms > 0 and analysis is not None:
            all_analysis_results.append(analysis)
            _enh_ok_batches.append(batch_idx + 1)

        # Free this batch's tile data after sending (saves ~5-10MB per batch)
        tile_batches[batch_idx] = None

    # Free the batches list entirely
    del tile_batches

    # Merge batch results if multiple batches
    if not all_analysis_results:
        print(f"   ❌ Enhanced extraction: no rooms found in any batch")
        return None

    if len(all_analysis_results) == 1:
        final_analysis = all_analysis_results[0]
    else:
        # Merge with proper floor dedup + template overlap detection
        final_analysis = _merge_batch_results(all_analysis_results)

    total_rooms = final_analysis.get('project_info', {}).get('total_rooms_found', 0)
    print(f"   🔬 Enhanced extraction found {total_rooms} rooms total")
    _attach_bbox_anchors(final_analysis, pdf_path)
    # Synthetic chunk tracking for the tiled path (see init comment above).
    _enh_failed = [b for b in range(1, _enh_total_batches + 1)
                   if b not in _enh_ok_batches]
    final_analysis["_chunk_tracking"] = {
        "mode": "enhanced_tiled",
        "total_chunks": _enh_total_batches,
        "chunks_succeeded": list(_enh_ok_batches),
        "chunks_failed": _enh_failed,
        "chunk_page_ranges": [],
    }
    return (pdf_path, final_analysis)


# Focused prompt for image-based schedule row counting
_SCHEDULE_IMAGE_PROMPT = """You are reading DOOR and WINDOW SCHEDULE tables from architectural drawings.
Your job is to count EVERY row in each schedule table and classify each entry.

═══════════════════════════════════════════════════════════
DOOR SCHEDULE
═══════════════════════════════════════════════════════════
Go through the door schedule table ROW BY ROW.  For EACH door mark:
1. Read the door mark / number (e.g., 101, 102A, B01)
2. Read the PANEL material and FRAME material columns
3. Classify — ONLY count rows from the "DOOR AND FRAME SCHEDULE" table
   (do NOT count storefront entries like SF100, SF101 — those are glazing, not doors):
   • "full_paint" — ANY of these:
     - Wood (WD) door with ANY frame
     - Glass (GLAS) door with Wood (WD) frame → interior glass doors, painted
     - Material columns are blank, "--", or "N/A" → DEFAULT TO FULL PAINT
       (blank material = standard interior apartment door = wood = full paint)
     - Any door whose panel material is NOT explicitly "HM"
   • "hm_panel"  — Hollow Metal (HM) panel with ANY frame type (HM, AL, ALUM)
     The panel gets painted regardless of frame material.
   • EXCLUDE — do NOT count as full_paint or hm_panel: any door whose schedule
     entry marks it prefinished, pre-finished, factory-finished, "PF", stained,
     clear-coat, anodized, or clad. These are factory-finished, not field-painted.
   CRITICAL: Blank/-- material is VERY COMMON for residential apartment doors.
   A 20-unit building typically has 150+ doors with blank material columns.
   These are ALL full_paint.  Do NOT skip or ignore doors with blank materials.
   If you find fewer than 100 full_paint doors for a 20-unit building, re-check.
4. Assign to a floor using the door mark prefix:
   • 0xx or B-series = Basement/Ground
   • 1xx = 1st Floor, 2xx = 2nd Floor, 3xx = 3rd Floor, etc.
   • If mark has letters (e.g., A, B, C), look at the schedule's "Floor" or "Location" column.

COUNT EVERY ROW.  If the schedule spans multiple pages, count across ALL pages.
A typical 20-unit residential building has 150–200 doors.  If you count fewer than 100, re-check.

═══════════════════════════════════════════════════════════
WINDOW SCHEDULE
═══════════════════════════════════════════════════════════
Go through the window schedule table ROW BY ROW.  For EACH window mark:
1. Read the window mark / type (e.g., W1, W2, A, B)
2. Read the QTY column if present (some schedules list quantity per type)
3. Read the FRAME material, TYPE, FINISH, JAMB DETAIL, HEAD DETAIL, SILL DETAIL,
   and NOTES columns carefully — also cross-reference any window TYPE detail
   drawings (often on the schedule sheet) showing casing, return, sill, apron,
   and drywall return construction.
4. Identify which window-trim COMPONENTS are present and paintable per window type:
   • CASING — wood/MDF trim around the window opening (head + jambs ± sill)
   • APRON — horizontal trim board below the sill
   • STOOL / SILL — interior sill board (separate from exterior sill)
   • RETURN — wood return (jamb extension wrapping back to wall) requiring paint
   • DRYWALL RETURN — gypsum return at jamb (paintable like a wall, no casing)
     (drywall returns are typically NOT counted as window trim — they're already
     part of the wall paint scope; flag presence only)
   • SASH — operable sash (factory-finished on virtually all modern windows)
5. Determine sash paint status:
   • COMMERCIAL JOBS: ASSUME sashes are NOT painted (factory-finished). Do not
     count sashes as painted unless the schedule EXPLICITLY says "field paint sash"
     or "paint operable sash".
   • Storefronts (SF-prefix), aluminum, vinyl, fiberglass, clad, fire-rated,
     pre-finished/factory-painted/shop-painted = sash NOT painted.
6. Determine per-component paint status (casing/apron/sill/return) from the
   schedule, type detail, and any explicit paint callouts. Components are paintable
   when they exist as wood/MDF trim — they do NOT need to be called out as "PT-x"
   to be painted; wood trim is painted by default unless marked stained/clear.

IMPORTANT: Do NOT assume residential windows have painted sashes. Modern windows
are factory-finished. Casings/aprons/stools, when present as wood trim, ARE
painted. If the schedule does not show jamb/head/sill details that confirm
component construction, leave components UNKNOWN rather than guessing.

═══════════════════════════════════════════════════════════
STAIR INFO (if visible on these pages)
═══════════════════════════════════════════════════════════
If you see stair details or a stair schedule, count total stair FLIGHT SECTIONS
(one run between landings).  A typical 3-story building has 8-12 flight sections.

═══════════════════════════════════════════════════════════
OUTPUT FORMAT — Return ONLY this JSON, no other text:
═══════════════════════════════════════════════════════════
{
  "has_schedules": true,
  "door_schedule": {
    "total_doors_full_paint": <int>,
    "total_doors_hm_panel": <int>,
    "doors_by_floor": {
      "basement": {"full_paint": 0, "hm_panel": 0},
      "1": {"full_paint": 0, "hm_panel": 0},
      "2": {"full_paint": 0, "hm_panel": 0},
      "3": {"full_paint": 0, "hm_panel": 0}
    },
    "door_marks_counted": ["list", "every", "mark", "you", "read"],
    "notes": "any observations about the schedule"
  },
  "window_schedule": {
    "total_windows": <int>,
    "windows_painted_interior": <int>,
    "window_types": [
      {"mark": "W1", "qty": 10, "frame": "wood", "painted_interior": true,
       "has_casing": true, "has_apron": true, "has_stool_sill": true,
       "has_wood_return": false, "has_drywall_return": false,
       "sash_painted": false},
      {"mark": "W2", "qty": 5, "frame": "aluminum", "painted_interior": false,
       "has_casing": false, "has_apron": false, "has_stool_sill": false,
       "has_wood_return": false, "has_drywall_return": true,
       "sash_painted": false}
    ],
    "windows_with_casing": <int>,
    "windows_with_apron": <int>,
    "windows_with_stool_sill": <int>,
    "windows_with_wood_return": <int>,
    "windows_with_drywall_return": <int>,
    "windows_with_painted_sash": <int>,
    "window_paint_spec": "description of interior paint spec if found",
    "notes": ""
  },
  "stair_info": {
    "total_stair_sections": 0,
    "notes": ""
  },
  "notes": []
}

CRITICAL: List EVERY door mark in "door_marks_counted" — this allows verification.
If you cannot read a schedule clearly, say so in notes rather than guessing.
"""


def analyze_schedule_images(client, pdf_path, schedule_page_nums):
    """
    Extract door/window/stair counts from schedule pages by sending them
    as a mini-PDF to Claude with a focused schedule-counting prompt.
    PDF document type preserves vector text which is far more readable
    than rasterised images for table data.
    Returns schedule data dict compatible with _apply_schedule_overrides(),
    or None if extraction fails.
    """
    if not schedule_page_nums:
        return None

    print(f"\n   📋 TARGETED SCHEDULE EXTRACTION")
    print(f"   Extracting {len(schedule_page_nums)} schedule page(s) as mini-PDF...")

    # Build a mini-PDF containing only the schedule pages
    try:
        reader = PyPDF2.PdfReader(pdf_path)
        writer = PyPDF2.PdfWriter()
        for pg in sorted(schedule_page_nums):
            if pg < len(reader.pages):
                writer.add_page(reader.pages[pg])
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            writer.write(tmp)
            tmp_path = tmp.name
        with open(tmp_path, 'rb') as f:
            pdf_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
        os.unlink(tmp_path)
        print(f"   Mini-PDF: {len(pdf_b64)*3/4/1024:.0f} KB, "
              f"{len(schedule_page_nums)} page(s)")
    except Exception as e:
        print(f"   ⚠️  Could not build mini-PDF: {e}")
        return None

    # Build content: PDF document + focused prompt
    content = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_b64,
            }
        },
        {
            "type": "text",
            "text": _SCHEDULE_IMAGE_PROMPT,
        }
    ]

    print(f"   Sending schedule PDF to Claude for row-by-row counting...")

    # Call the API
    result_parts = []
    try:
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            temperature=0,
            timeout=300.0,  # 5 min timeout
            messages=[{"role": "user", "content": content}],
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)
    except Exception as e:
        print(f"   ❌ Schedule image API call failed: {e}")
        _release_memory("after schedule API failure")
        return None

    raw = "".join(result_parts)

    # Parse JSON from the response
    try:
        # Find JSON in the response (may have markdown fences)
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            schedule_data = json.loads(json_match.group())
        else:
            print(f"   ❌ No JSON found in schedule image response")
            return None
    except json.JSONDecodeError as e:
        print(f"   ❌ Could not parse schedule image JSON: {e}")
        return None

    # Print summary
    ds = schedule_data.get("door_schedule", {})
    ws = schedule_data.get("window_schedule", {})
    si = schedule_data.get("stair_info", {})
    total_doors = (ds.get("total_doors_full_paint", 0) or 0) + \
                  (ds.get("total_doors_hm_panel", 0) or 0)
    marks = ds.get("door_marks_counted", [])

    print(f"   ✅ Schedule extraction complete:")
    print(f"      Doors: {ds.get('total_doors_full_paint', 0)} full paint + "
          f"{ds.get('total_doors_hm_panel', 0)} HM panel = {total_doors} total "
          f"({len(marks)} marks listed)")
    print(f"      Windows: {ws.get('total_windows', 0)} total, "
          f"{ws.get('windows_painted_interior', 0)} painted interior")
    if si.get("total_stair_sections"):
        print(f"      Stairs: {si['total_stair_sections']} flight sections")

    _release_memory("after schedule extraction")
    return schedule_data


def _normalize_floor_key(name):
    """
    Extract a canonical floor key from a floor name so that different chunk
    labels for the same physical floor are matched together.

    IMPORTANT: Template / typical unit groups that span multiple floors must
    NOT collapse into a single floor number.  "Typical 1BR Units (Floors 2&3)"
    is a *template group*, not "1st Floor" or "2nd Floor".

    Examples:
      "Basement"                              -> "B"
      "Foundation/Basement"                   -> "B"
      "1st Floor Commercial"                  -> "1"
      "1st Floor"                             -> "1"
      "2nd Floor"                             -> "2"
      "3rd Floor Residential"                 -> "3"
      "Typical 1BR Units (Floors 2&3)"        -> "T_1br"  (template group)
      "Typical 2BR Units (Floors 2-3)"        -> "T_2br"  (template group)
      "Typical Studio Units"                  -> "T_studio"
      "Common Areas (Floors 2-3)"             -> "T_common" (cross-floor group)
      "Stairwells (All Floors)"               -> "T_stairwells"
    """
    n = name.lower().strip()

    # --- Template / cross-floor groups (check BEFORE floor number extraction) ---
    # These are groups that span multiple floors and should NOT be merged with
    # a specific physical floor.
    is_template = (
        "typical" in n or "template" in n
        or re.search(r'\(floors?\s*[\d&,\-–]+\)', n)   # "(Floors 2&3)", "(Floors 2-3)"
        or re.search(r'\(all\s+floors?\)', n)           # "(All Floors)"
    )
    if is_template:
        # Build a descriptive key from the unit type
        if "studio" in n:
            return "T_studio"
        if "2br" in n or "2-br" in n or "two.?bed" in n or "2 bed" in n:
            return "T_2br"
        if "1br" in n or "1-br" in n or "one.?bed" in n or "1 bed" in n:
            return "T_1br"
        if "3br" in n or "3-br" in n or "three.?bed" in n or "3 bed" in n:
            return "T_3br"
        if "stair" in n:
            return "T_stairwells"
        if "common" in n or "corridor" in n or "hallway" in n:
            return "T_common"
        # Generic template: use the full name to avoid collisions
        return "T_" + re.sub(r'[^a-z0-9]+', '_', n).strip('_')

    # Basement / foundation / lower level
    if "basement" in n or "foundation" in n or "lower level" in n:
        return "B"
    # Sub-basement
    if "sub-basement" in n or "subbasement" in n or "cellar" in n:
        return "SB"
    # Word-to-number mapping for written-out ordinals (e.g., "First Floor" → "1")
    _WORD_TO_NUM = {
        "first": "1", "second": "2", "third": "3", "fourth": "4",
        "fifth": "5", "sixth": "6", "seventh": "7", "eighth": "8",
        "ninth": "9", "tenth": "10", "eleventh": "11", "twelfth": "12",
    }
    for word, num in _WORD_TO_NUM.items():
        if word in n:
            return num
    # Extract floor number from ordinal or cardinal patterns
    ordinal = re.search(r'(\d+)\s*(?:st|nd|rd|th)\s*(?:floor|flr|fl|story|storey)?', n)
    if ordinal:
        return ordinal.group(1)
    cardinal = re.search(r'(?:floor|flr|fl|level|story|storey)\s*(\d+)', n)
    if cardinal:
        return cardinal.group(1)
    # "Ground Floor" -> "1"
    if "ground" in n:
        return "1"
    # "Roof" -> "R"
    if "roof" in n:
        return "R"
    # "Mezzanine" -> "M"
    if "mezzanine" in n or "mezz" in n:
        return "M"
    # "Penthouse" -> "PH"
    if "penthouse" in n:
        return "PH"
    # Fallback: return the original name lowercased
    return n


_INCOMPLETE_PLAN_FLAGS = (
    "no_floor_plans_found",
    "no_detailed_floor_plans_found",
    "no_complete_floor_plans_found",
)


def _model_flagged_no_plans(analysis):
    """True if the model self-reported missing floor plans under any known
    flag name. The extraction prompt has used three different names over
    time, so downstream rescue triggers normalize through this helper."""
    if not isinstance(analysis, dict):
        return False
    return any(bool(analysis.get(k)) for k in _INCOMPLETE_PLAN_FLAGS)


def _extraction_likely_incomplete(analysis):
    """True when extraction returned synthetic templates instead of real
    physical floor data. Common DD-scale failure: model summarizes repeated
    unit floors as "Typical Units (Floors 2-3)" rather than extracting each
    physical floor.

    Fires when the model flagged no plans, OR when total_stories exceeds the
    number of physical (non-template) floors extracted with at least one
    template floor present."""
    if not isinstance(analysis, dict):
        return False
    if _model_flagged_no_plans(analysis):
        return True
    floors = analysis.get("floors") or []
    if not floors:
        return False
    physical_count = 0
    template_count = 0
    for f in floors:
        if _normalize_floor_key(f.get("floor_name", "")).startswith("T_"):
            template_count += 1
        else:
            physical_count += 1
    pi = analysis.get("project_info") or {}
    raw = pi.get("total_stories") or 0
    try:
        total_stories = int(raw) if str(raw).strip().isdigit() else 0
    except (ValueError, TypeError):
        total_stories = 0
    return (
        template_count > 0
        and total_stories > 0
        and physical_count < total_stories
    )


def _floor_room_count(floor):
    """Total effective rooms on a floor accounting for unit_multiplier."""
    total = 0
    for r in floor.get("rooms", []):
        mult = max(1, int(_num(r.get("unit_multiplier", 1))))
        total += mult
    return total


def _floor_total_wall_area(floor):
    """Total wall area on a floor accounting for unit_multiplier."""
    total = 0
    for r in floor.get("rooms", []):
        mult = max(1, int(_num(r.get("unit_multiplier", 1))))
        total += _num(r.get("dimensions", {}).get("wall_area_sqft", 0)) * mult
    return total


def _resolve_batch_template_overlap(analysis):
    """
    Detect template floors from different extraction batches that describe
    the same physical floor space (same source pages).  When two batch
    groups both produce template floors derived from the same source page(s),
    keep the group with MORE distinct template floor entries (= better unit
    type identification) and remove the other.

    This fixes the common case where one batch extracts specific unit types
    (e.g. "Typical 2BR", "Typical 1BR Type A", "Typical Studio" — 5 floors)
    while another batch extracts a single generic template
    (e.g. "Typical Residential Floor" — 1 floor) from the same drawing page.
    """
    floors = analysis.get("floors", [])

    # Identify template floors and their batch origin + source pages
    template_info = []  # (floor_idx, batch_set, source_pages)
    for i, fl in enumerate(floors):
        key = _normalize_floor_key(fl.get("floor_name", ""))
        if not key.startswith("T_"):
            continue
        batches = set()
        pages = set()
        for r in fl.get("rooms", []):
            b = r.get("_batch_idx")
            if b is not None:
                batches.add(b)
            sp = r.get("source_page")
            if isinstance(sp, (int, float)):
                pages.add(int(sp))
        if batches and pages:
            template_info.append((i, batches, pages))

    if len(template_info) <= 1:
        return

    # Group template floors by their predominant batch
    batch_groups = {}  # batch_idx -> [(floor_idx, source_pages)]
    for idx, batches, pages in template_info:
        fl = floors[idx]
        primary_batch = max(batches, key=lambda b: sum(
            1 for r in fl.get("rooms", []) if r.get("_batch_idx") == b))
        batch_groups.setdefault(primary_batch, []).append((idx, pages))

    if len(batch_groups) <= 1:
        return  # All templates from same batch — no cross-batch overlap

    # Check for source page overlap between batch groups
    batch_list = list(batch_groups.items())
    to_remove_indices = set()

    for i in range(len(batch_list)):
        batch_i, floors_i = batch_list[i]
        pages_i = set()
        for _, p in floors_i:
            pages_i.update(p)

        for j in range(i + 1, len(batch_list)):
            batch_j, floors_j = batch_list[j]
            pages_j = set()
            for _, p in floors_j:
                pages_j.update(p)

            overlap = pages_i & pages_j
            if not overlap:
                continue

            # These batch groups produced template floors from the same pages.
            # Keep the one with more distinct template floors (= better unit type ID).
            count_i = len(floors_i)
            count_j = len(floors_j)

            if count_i >= count_j:
                for idx, _ in floors_j:
                    to_remove_indices.add(idx)
            else:
                for idx, _ in floors_i:
                    to_remove_indices.add(idx)

    if to_remove_indices:
        removed_names = [floors[i].get("floor_name", "?") for i in to_remove_indices]
        analysis["floors"] = [fl for i, fl in enumerate(floors) if i not in to_remove_indices]
        print(f"   🔧 Batch template dedup: removed {len(to_remove_indices)} redundant "
              f"template floor(s)")
        for name in removed_names:
            print(f"      - {name}")


def _merge_batch_results(all_results):
    """
    Merge multiple batch extraction results (parsed analysis dicts) with
    proper floor deduplication.

    Phase 1: Standard floor-key dedup using _merge_chunk_responses logic —
             catches same-floor entries (e.g. two "1st Floor Commercial").
    Phase 2: Template overlap detection — catches cross-batch redundant
             template groups (e.g. "Typical 2BR" + "Typical 1BR" from one
             batch vs "Typical Residential Floor" from another batch).
    """
    if not all_results:
        return None
    if len(all_results) == 1:
        return all_results[0]

    # Tag rooms with batch origin for template overlap detection
    for batch_idx, result in enumerate(all_results):
        for fl in result.get("floors", []):
            for r in fl.get("rooms", []):
                r["_batch_idx"] = batch_idx

    # Phase 1: convert to JSON strings and reuse _merge_chunk_responses
    json_texts = [json.dumps(r) for r in all_results]
    merged_text = _merge_chunk_responses(json_texts)
    final = json.loads(merged_text)

    # Phase 2: resolve overlapping template groups across batches
    _resolve_batch_template_overlap(final)

    # Recalculate room count
    total_rooms = sum(len(fl.get("rooms", []))
                      for fl in final.get("floors", []))
    final.setdefault("project_info", {})["total_rooms_found"] = total_rooms

    return final


def _merge_chunk_responses(texts, page_offsets=None, chunk_indices=None,
                           parse_failures=None):
    """
    Merge multiple JSON response texts (from chunked PDF processing) into
    a single combined JSON string.  Each chunk may contain partial floor/room
    data; this function combines all floors and rooms, sums aggregated totals,
    and returns the merged JSON as a string.

    When two chunks describe the same physical floor with different names
    (e.g. "Basement" vs "Foundation/Basement"), the merger keeps the version
    with more rooms/data (usually from the actual floor plan sheet rather than
    a demolition or code compliance sheet).

    page_offsets: optional list of 1-based page offsets, indexed by chunk
                  NUMBER (page_offsets[k] is the offset of chunk k+1), used
                  to fix source_page values (which Claude reports relative
                  to each chunk rather than the original PDF).
    chunk_indices: optional list parallel to `texts` giving each text's
                  1-based chunk number. Without it, offsets used to be
                  applied POSITIONALLY over the successfully parsed texts —
                  so one failed/unparseable chunk shifted every later
                  chunk's rooms onto the wrong source pages, corrupting the
                  very coverage checks meant to detect the failure.
    parse_failures: optional list; chunk numbers whose text could not be
                  parsed as JSON are appended (previously a silent `pass` —
                  the chunk's rooms vanished and the chunk stayed recorded
                  as "succeeded").
    """
    parsed = []             # successfully parsed chunk dicts
    parsed_chunk_nums = []  # parallel: 1-based chunk number per parsed dict
    for pos, t in enumerate(texts):
        if chunk_indices and pos < len(chunk_indices):
            chunk_num = chunk_indices[pos]
        else:
            chunk_num = pos + 1
        m = re.search(r'\{.*\}', t, re.DOTALL)
        if m:
            try:
                parsed.append(json.loads(m.group()))
                parsed_chunk_nums.append(chunk_num)
                continue
            except json.JSONDecodeError:
                pass
        print(f"   ⚠️  Chunk {chunk_num}: response could not be parsed as JSON "
              f"({len(t)} chars) — its rooms are NOT in the merged result")
        if parse_failures is not None:
            parse_failures.append(chunk_num)

    if not parsed:
        return texts[0]  # nothing could be parsed — return first raw text

    # Apply page offsets to source_page values (Claude reports page numbers
    # relative to each chunk; we need them relative to the original PDF).
    # Indexed by true chunk number so a failed earlier chunk cannot shift
    # later chunks onto the wrong pages.
    if page_offsets:
        for chunk_num, chunk_data in zip(parsed_chunk_nums, parsed):
            if not (1 <= chunk_num <= len(page_offsets)):
                continue
            offset = page_offsets[chunk_num - 1] - 1  # convert to 0-based addition
            if offset > 0:
                for floor in chunk_data.get("floors", []):
                    for room in floor.get("rooms", []):
                        sp = room.get("source_page")
                        if isinstance(sp, (int, float)) and sp > 0:
                            room["source_page"] = int(sp) + offset

    if len(parsed) == 1:
        return json.dumps(parsed[0])

    # Start with the first result as the base
    combined = parsed[0]

    for extra in parsed[1:]:
        # Skip "no floor plans" chunks
        if extra.get("no_floor_plans_found") or extra.get("no_detailed_floor_plans_found"):
            # But carry notes
            for n in extra.get("notes", []):
                combined.setdefault("notes", []).append(n)
            continue

        # Build a map of existing floors keyed by normalized floor key
        existing_by_key = {}  # normalized_key -> floor dict
        for f in combined.get("floors", []):
            key = _normalize_floor_key(f.get("floor_name", ""))
            existing_by_key[key] = f

        for floor in extra.get("floors", []):
            fname = floor.get("floor_name", "")
            fkey = _normalize_floor_key(fname)

            if fkey in existing_by_key:
                existing = existing_by_key[fkey]
                # Same physical floor from two different chunks.
                # Keep the version with more detail (more rooms/wall area).
                existing_wall = _floor_total_wall_area(existing)
                new_wall = _floor_total_wall_area(floor)
                existing_rooms = _floor_room_count(existing)
                new_rooms = _floor_room_count(floor)

                if new_wall > existing_wall or (new_wall == existing_wall and new_rooms > existing_rooms):
                    # New chunk has better data — replace existing floor
                    idx = combined["floors"].index(existing)
                    combined["floors"][idx] = floor
                    existing_by_key[fkey] = floor
                # else: keep existing (it has more/equal data)
                # In either case, merge any unique rooms the other version has
                # that aren't in the winner (by room_id).
                # For rooms with the SAME room_id, merge element counts by
                # taking the MAX of each element (one chunk may have had
                # access to door/window schedules that the other didn't).
                winner = existing_by_key[fkey]
                loser = floor if winner is not floor else existing
                winner_ids = {r.get("room_id", ""): r for r in winner.get("rooms", [])}
                for room in loser.get("rooms", []):
                    rid = room.get("room_id", "")
                    if not rid:
                        continue
                    if rid not in winner_ids:
                        winner.setdefault("rooms", []).append(room)
                    else:
                        # Same room_id in both versions — merge element counts
                        w_room = winner_ids[rid]
                        w_elems = w_room.get("elements", {})
                        l_elems = room.get("elements", {})
                        for ekey in ("doors_full_paint", "doors_hm_panel",
                                     "windows_total", "windows_painted_interior",
                                     "stair_sections", "gyp_between_stairs_sqft",
                                     "concrete_floor_sqft", "wallcovering_sqft",
                                     "stained_wood_sqft", "soffit_sqft"):
                            w_val = w_elems.get(ekey, 0)
                            l_val = l_elems.get(ekey, 0)
                            if l_val > w_val:
                                w_elems[ekey] = l_val
            else:
                combined.setdefault("floors", []).append(floor)
                existing_by_key[fkey] = floor

        # Merge project info
        epi = extra.get("project_info", {})
        cpi = combined.setdefault("project_info", {})
        for key in ("project_name", "location", "architect", "drawing_date", "building_type"):
            if epi.get(key) and not cpi.get(key):
                cpi[key] = epi[key]

        # Prevailing wage merge: a "yes" finding from any chunk wins; otherwise
        # union the indicators / source pages so Will sees the full evidence trail.
        epw = epi.get("prevailing_wage") if isinstance(epi.get("prevailing_wage"), dict) else None
        if epw:
            cpw = cpi.setdefault("prevailing_wage", {
                "applies": "unknown", "county": None,
                "wage_schedule_basis": None, "indicators": [], "source_pages": []
            })
            applies_priority = {"yes": 2, "no": 1, "unknown": 0}
            if applies_priority.get(str(epw.get("applies", "unknown")).lower(), 0) > \
               applies_priority.get(str(cpw.get("applies", "unknown")).lower(), 0):
                cpw["applies"] = epw.get("applies", "unknown")
            if epw.get("county") and not cpw.get("county"):
                cpw["county"] = epw["county"]
            if epw.get("wage_schedule_basis") and not cpw.get("wage_schedule_basis"):
                cpw["wage_schedule_basis"] = epw["wage_schedule_basis"]
            existing_inds = set(cpw.get("indicators", []) or [])
            for ind in (epw.get("indicators") or []):
                if ind and ind not in existing_inds:
                    cpw.setdefault("indicators", []).append(ind)
                    existing_inds.add(ind)
            existing_pages = set(cpw.get("source_pages", []) or [])
            for pg in (epw.get("source_pages") or []):
                if pg not in existing_pages:
                    cpw.setdefault("source_pages", []).append(pg)
                    existing_pages.add(pg)

        # Merge notes (deduplicate)
        existing_notes = set(combined.get("notes", []))
        for n in extra.get("notes", []):
            if n not in existing_notes:
                combined.setdefault("notes", []).append(n)
                existing_notes.add(n)

        # Merge exterior — keep the MAX of each numeric field (not sum,
        # since chunks may re-estimate the same exterior)
        ext = extra.get("exterior", {})
        if ext:
            cext = combined.setdefault("exterior", {})
            for key, val in ext.items():
                if isinstance(val, (int, float)):
                    existing = cext.get(key, 0)
                    # Prior chunk may have stringified the same field (LLM
                    # type drift across chunks) — replace rather than crash
                    # on max(str, int).
                    if not isinstance(existing, (int, float)):
                        cext[key] = val
                    else:
                        cext[key] = max(existing, val)
                elif not cext.get(key):
                    cext[key] = val

    # Don't sum aggregated_totals — let _recalculate_totals do it from room data
    # Just update the room/floor counts
    all_rooms = 0
    for floor in combined.get("floors", []):
        all_rooms += len(floor.get("rooms", []))
    combined.setdefault("project_info", {})["total_rooms_found"] = all_rooms
    combined["project_info"]["total_floors_analyzed"] = len(combined.get("floors", []))

    return json.dumps(combined)


def _build_extraction_prompt(scope_notes="", schedule_hints=None,
                              building_inventory=None, text_layer_context=None,
                              project_overview=None):
    """
    Build the full room extraction prompt with optional schedule hints,
    building inventory context, and pre-extracted text layer data.

    This is the same prompt used by analyze_construction_pdf() for native PDF,
    _analyze_floor_plan_as_images() for image fallback, and
    _analyze_with_enhanced_extraction() for tiled large-format PDFs.

    Args:
        scope_notes: Optional scope/notes text to prepend
        schedule_hints: Pre-extracted door/window/stair schedule data
        building_inventory: Pre-extracted building inventory from index pages
        text_layer_context: Pre-extracted text layer from PyMuPDF (dimensions,
                           room labels, IDs) formatted as prompt context string
        project_overview: Optional dict from _extract_project_overview(). When
                          prefer_plan_state == 'proposed', a viewport-selector
                          directive is prepended so multi-plan sheets are read
                          from the PROPOSED viewport only.

    Returns:
        str: the complete prompt text (with context prepended if available)
    """
    # Build schedule hints preamble if pre-extracted schedule data is available
    schedule_hint_text = ""
    if schedule_hints:
        ds = schedule_hints.get("door_schedule", {})
        ws = schedule_hints.get("window_schedule", {})
        si = schedule_hints.get("stair_info", {})
        hint_parts = [
            "\n═══════════════════════════════════════════════════════════",
            "KNOWN SCHEDULE DATA (pre-extracted from schedule pages):",
            "═══════════════════════════════════════════════════════════",
        ]
        d_fp = ds.get("total_doors_full_paint", 0) or 0
        d_hm = ds.get("total_doors_hm_panel", 0) or 0
        if d_fp or d_hm:
            hint_parts.append(f"- Total doors (full paint): {d_fp}")
            hint_parts.append(f"- Total doors (HM panel only): {d_hm}")
            hint_parts.append(f"- Total doors overall: {d_fp + d_hm}")
        w_total = ws.get("total_windows", 0) or 0
        w_painted = ws.get("windows_painted_interior", 0) or 0
        if w_total:
            hint_parts.append(f"- Total windows: {w_total}")
            hint_parts.append(f"- Windows with painted SASHES: {w_painted}")
            for ck, lbl in (
                ("windows_with_casing", "with casing"),
                ("windows_with_apron", "with apron"),
                ("windows_with_stool_sill", "with stool/sill"),
                ("windows_with_wood_return", "with wood return"),
                ("windows_with_drywall_return", "with drywall return"),
            ):
                v = ws.get(ck, 0) or 0
                if v:
                    hint_parts.append(f"- Windows {lbl}: {v}")
        s_total = si.get("total_stair_sections", 0) or 0
        if s_total:
            hint_parts.append(f"- Total stair flight sections: {s_total}")
        hint_parts.append(
            "Use these totals as REFERENCE when assigning doors/windows to rooms.")
        hint_parts.append(
            "Your per-room counts should approximately SUM to these schedule totals.")
        rfs = schedule_hints.get("room_finish_schedule") or []
        if rfs:
            hint_parts.append("")
            hint_parts.append(
                "ROOM FINISH SCHEDULE (pre-extracted — authoritative per-room "
                "finishes). Match each room below to its floor-plan room by "
                "number/name and set that room's finishes from this data:")
            hint_parts.append(
                "- A wall finish of WC-x / 'wallcovering' / 'vinyl wallcovering' "
                "means those walls get wallcovering_sqft, NOT paint — reduce "
                "wall_area_sqft accordingly (see the Wallcovering instructions below).")
            hint_parts.append(
                "- Use the ceiling and base finishes here as positive evidence for "
                "ceiling_painted / ceiling material and base_trim_lf.")
            for r in rfs[:200]:
                num = str(r.get("room_number", "") or "").strip()
                nm = str(r.get("room_name", "") or "").strip()
                wf = str(r.get("wall_finish", "") or "?").strip()
                cf = str(r.get("ceiling_finish", "") or "?").strip()
                bf = str(r.get("base_finish", "") or "?").strip()
                ut = str(r.get("unit_type", "") or "").strip()
                lbl = " ".join(x for x in (num, nm) if x) or "(unnamed room)"
                ut_s = f" [{ut}]" if ut else ""
                hint_parts.append(
                    f"  - {lbl}{ut_s}: wall={wf}; ceiling={cf}; base={bf}")
            if len(rfs) > 200:
                hint_parts.append(
                    f"  - ...and {len(rfs) - 200} more rooms in the schedule")
        hint_parts.append(
            "═══════════════════════════════════════════════════════════\n")
        schedule_hint_text = "\n".join(hint_parts)

    prompt = """You are analyzing ARCHITECTURAL/CONSTRUCTION DRAWINGS (Permit Set PDF) for a PAINTING ESTIMATE.

NOTE: This PDF has been pre-filtered to include only painting-relevant pages
(Architectural drawings "A-series", finish schedules, and general legend pages).
Non-painting disciplines (Structural, Mechanical, Electrical, Plumbing, Civil,
Landscape, Fire Protection) have been excluded to help you focus on paintable surfaces.

IMPORTANT — UNIQUE ROOM IDENTIFICATION:
- Assign each room a UNIQUE room_id: "F{floor}-{unit_or_room}" e.g. "F1-COMM1", "F2-APT201", "F2-APT201-BED1", "F0-STOR001"
- Use unit numbers from the drawings (e.g. "APT 201") when visible
- NEVER create summary entries like "Multiple Residential Units" — always list individual rooms
- Every room must have numeric dimensions; skip rooms you cannot measure

STEP 0: CLASSIFY THE BUILDING (do this FIRST)
Before extracting any rooms, scan ALL pages and determine:
- Building type: single-family, multi-family, mixed-use, commercial
- Number of stories — count OCCUPIED HUMAN FLOOR LEVELS only, from a building section
  or elevation. Default for retail/big-box and most commercial tenant fit-outs is 1.
  DO COUNT: ground floor, second floor, third floor, etc. — anywhere people occupy.
  DO NOT COUNT: the roof, the parapet, a clerestory, a foundation/crawlspace, a
    mechanical penthouse, or a mezzanine that is less than 50% of the floor area below it.
  DO NOT INFER stories from seeing both a "Floor Plan" sheet and a "Roof Plan" sheet —
    that is still a single-story building.
  If the only floor plan is sheet A-101 (or equivalent first-floor plan) and there is
    no A-102/A-201/etc. showing rooms on a higher level, total_stories = 1.
- Number of units/apartments (from unit schedules, floor plans, light/ventilation tables)
- Total building footprint (from site plan or floor plan dimensions, e.g. 133' x 70')
- Presence of commercial/retail spaces on ground floor
Report this in project_info as additional fields: "building_type", "total_stories",
"total_units", "footprint_sqft"
This classification is CRITICAL — a 20-unit mixed-use building requires extracting 100+
rooms across multiple floors, not just 10-20 rooms like a single-family home.

STEP 0b: PREVAILING WAGE / PUBLIC WORKS DETECTION (do this with STEP 0)
Scan the title sheet, general notes, specifications cover, front-end docs, and any
addenda for indicators that this is a prevailing-wage / public-works project:
- Owner is a government agency, municipality, school district, housing authority,
  state university, MTA/transit, federal building, military, NYCHA, DASNY, etc.
- Explicit mentions: "prevailing wage", "Davis-Bacon", "Davis-Bacon Act",
  "NYS Labor Law §220", "Section 220", "public work", "public works", "PW",
  "wage schedule", "wage determination", "certified payroll", "PLA",
  "project labor agreement", "apprenticeship requirements"
- County / locality references tied to a wage schedule (e.g. "Westchester County
  Wage Schedule", "NYC DOL Schedule", "USDOL Wage Determination")
Report findings in project_info.prevailing_wage with this shape:
  "prevailing_wage": {
    "applies": "yes" | "no" | "unknown",
    "county": "<county/jurisdiction or null>",
    "wage_schedule_basis": "<e.g. NYS DOL §220, USDOL Davis-Bacon, or null>",
    "indicators": ["<short snippet 1>", "<short snippet 2>"],
    "source_pages": [<page numbers>]
  }
Default to "unknown" if no indicators are found — do NOT guess "no" unless the
documents explicitly state it is private / non-prevailing-wage work. This flag
materially impacts labor cost; missing it is the single biggest pricing risk.

STEP 1: IDENTIFY ALL PAGES (these pages have been pre-filtered for painting relevance)
- Floor plans (Basement, 1st, 2nd, 3rd…) — typically A-100 through A-199 series
- Door/window schedules (A-500 series) — CRITICAL for accurate door counts
- Building elevations and exterior details (A-200, A-700 series)
- Finish schedules (Division 9) — paint colors, room finishes
- Wall type legends

STEP 2: READ LEGENDS / MATERIALS
FIRST: Check for a ROOM FINISH SCHEDULE table (usually on sheet A-601 or within finish pages).
  This table has columns like: Room #, Room Name, Floor Finish, Wall Finish, Ceiling Finish.
  The "Wall Finish" column tells you EXACTLY what each room's walls get:
  - PT-1, PT-2, P-1, etc. = PAINT → walls = "GYP"
  - WC-1, WC-3, WC-5, WC-6 = WALLCOVERING → record wallcovering_sqft for that room
  - CMU, Block = BLOCK WALLS → walls = "CMU"
  The "Ceiling Finish" column tells you the ceiling type per room.
  The "Floor Finish" column tells you if concrete (sealed concrete, epoxy) → concrete_floor_sqft.
  Use this schedule as your PRIMARY SOURCE for material assignments per room.

Wall types:
  • GYP / GWB = Gypsum board → PAINTABLE
  • 1HR GYP = 1-hour rated gypsum → PAINTABLE
  • CMU = Concrete masonry → CHECK SPECS:
    - If specs reference: painted CMU, sealed CMU, block filler, block primer,
      CMU paint system, "paint all CMU surfaces" → PAINTABLE (set walls = "CMU")
    - If specs reference: exposed architectural block, split-face block,
      burnished block, glazed CMU, "no finish" → NOT paintable
    - If specs are silent on CMU finish → DEFAULT to paintable with walls = "CMU"
      (most commercial CMU walls receive paint — it's standard practice)
    - IMPORTANT: In commercial buildings, check the ROOM FINISH SCHEDULE "Wall Finish"
      column for CMU, block, or masonry designations PER ROOM. Service areas, service bays,
      mechanical rooms, garages, parts rooms, and utility areas commonly have CMU walls
      even when adjacent office/showroom areas have GYP. Do NOT assume all walls are GYP —
      check each room's finish schedule entry individually.
Ceiling types — CRITICAL DISTINCTION:
  • GYP / GWB ceiling → PAINTED (ceiling_painted = true)
  • ACT (Acoustic Ceiling Tile) → NOT painted (ceiling_painted = false)
  • Drop / suspended ceiling → NOT painted (ceiling_painted = false)
  • Exposed structure / open ceiling / exposed deck → CHECK SPECS:
    - If specs reference: dryfall, spray-applied ceiling coating, painted exposed structure,
      painted ceiling deck, "paint all exposed surfaces above ceiling grid", PT-1/PT-2 on ceiling,
      painted joists/beams, "paint deck and structure" → PAINTABLE as "DRYFALL"
      (set ceiling = "DRYFALL", ceiling_painted = true)
    - If NO paint trigger found → NOT painted (ceiling_painted = false)
    - DEFAULT for commercial exposed ceilings: paintable as DRYFALL unless explicitly excluded
  Defaults when ceiling material is NOT explicitly noted:
  • RESIDENTIAL rooms (bedrooms, bathrooms, living rooms, kitchens, closets, dens) →
    DEFAULT to ceiling_painted = true (residential units almost always have GYP ceilings)
  • COMMERCIAL retail spaces → DEFAULT to ceiling_painted = false (typically ACT or exposed)
  • Corridors, hallways, lobbies, common areas, utility/mechanical rooms →
    ceiling_painted = false unless GYP/GWB is EXPLICITLY shown on the REFLECTED CEILING PLAN (RCP).
    CHECK THE RCP: ACT ceilings appear as a grid pattern; GYP ceilings appear smooth.
    Public hallways and common corridors ALMOST ALWAYS have ACT ceilings — do NOT assume painted.
    If no RCP is available, DEFAULT to ceiling_painted = false for all corridors/lobbies/hallways.
  • Back-of-house rooms (offices, break rooms) → ceiling_painted = true (typically GYP)

WHITEBOX / PRIME ONLY DETECTION:
- If a room or tenant space is labeled "whitebox", "white box", "prime only",
  "shell condition", "white shell", "vanilla box", or "warm shell":
  * These spaces receive PRIMER ONLY — they are NOT fully painted
  * Set "prime_only": true in the room's elements
  * Still measure wall_area_sqft (we need surface area for priming reference)
  * Set ceiling_painted = false (whitebox ceilings are typically not painted)
  * Add a note: "Whitebox/prime only — excluded from full paint scope"
- Retail/tenant spaces with no finish specifications are often whitebox — check carefully

STEP 3: FOR EACH FLOOR PLAN — EXTRACT EVERY ROOM
Include ALL spaces: apartments, bedrooms, bathrooms, kitchens, living rooms,
closets, HALLWAYS, CORRIDORS, LOBBIES, COMMON AREAS, storage, laundry, mech rooms.
Do NOT skip hallways or common areas — they have walls, ceilings, trim, and doors.

CRITICAL — BREAK EACH UNIT INTO INDIVIDUAL ROOMS:
  ✓ CORRECT: APT 201 Living/Kitchen, APT 201 Bedroom 1, APT 201 Bedroom 2, APT 201 Bath, APT 201 Closet
  ✗ WRONG:   APT 201 Living/Dining/Kitchen (single entry — missing bedrooms, baths, closets!)
Every apartment MUST include ALL its rooms: living area, EACH bedroom, EACH bathroom, closets, laundry.
If you only list "Living/Dining/Kitchen" for a 2BR unit, you are missing 60%+ of the paintable area.

For each room:
- Read dimension callouts (e.g. 20'-0" × 15'-6")
- Ceiling heights — PULL FROM BUILDING SECTIONS (PRIMARY SOURCE). MAKE NO ASSUMPTIONS.
  Building Sections show a vertical cut through the building with floor-to-ceiling
  and floor-to-floor dimensions labeled per level. IDENTIFY THEM BY CONTENT, not by
  sheet number — different jobs use different numbering conventions. Look for any
  sheet whose title block, drawing title, or detail label reads "BUILDING SECTION",
  "BUILDING SECTIONS", "WALL SECTION", "TRANSVERSE SECTION", "LONGITUDINAL SECTION",
  "CROSS SECTION", "SECTION A", "SECTION B", or simply shows a vertical cut through
  the building with stacked floors and ceiling lines. Sheet numbers vary by job
  (A-300 series, A-400 series, A-500 series, AS-101, A2.10, X-201, etc.) — do NOT
  assume a specific number; use whatever sheet on this job actually contains the
  Building Sections.
  PRIORITY ORDER for determining each room's ceiling height:
    1. Building Section drawing — read the labeled ceiling height (or floor-to-ceiling
       dimension) for the floor/area where that room lives. Examples of labels you
       will see: "9'-0" CLG", "CLG HT 10'-0"", "T.O. SLAB to U/S CLG = 9'-6"",
       "FIN. CLG.", or a vertical dimension string between Finish Floor and Ceiling.
       USE THIS VALUE EXACTLY.
    2. RCP / floor plan CLG HT callout for that specific room (e.g. "CLG HT: 9'-0"").
       USE THE CALLOUT VALUE EXACTLY.
    3. If no labeled ceiling height appears anywhere, MEASURE IT from a Building
       Section using the drawing's scale. Find the title block scale (e.g. 1/4" = 1'-0",
       3/8" = 1'-0", 1/8" = 1'-0") or the graphic scale bar on that section. Measure
       the vertical distance from finish floor to underside of ceiling on the section,
       convert using the scale, and record that value. Note in the room's "notes"
       field that the ceiling height was scaled from a section (which sheet).
  DO NOT estimate, round, or assume a default ceiling height (no defaulting to 8',
  9', 9'-6", or 10'). DO NOT use the "typical residential" assumption. Every ceiling
  height must trace back to a labeled section, a labeled CLG HT callout, or a
  scale-based measurement off a Building Section.
  If multiple rooms share the same floor and the section shows one floor-to-ceiling
  dimension for that floor, apply that exact value to each room on that floor —
  unless the section/RCP shows a different height (drop ceiling, vault, soffit) for
  a specific room.
- Calculate: Wall Area = WALL PERIMETER × Wall Height
  WALL PERIMETER (LF) is the field "perimeter_lf" on each room and is the foundation
  of every wall calculation downstream (paint sqft, base trim LF, wallcovering split).
  Get it wrong and every wall number for the room is wrong.
- LINEAR PATH METHOD — how to derive WALL PERIMETER (REQUIRED — do NOT shortcut to 2×(L+W)):
  Trace the WALL PERIMETER as a sequence of linear paths from point A to point B along
  each wall segment that bounds the room, then sum the segment lengths. The sum IS
  the room's wall perimeter in linear feet. This is the ONLY correct way to measure
  non-rectangular rooms.
  * Walk every wall segment: along the long wall, around each jog/alcove/bay/notch,
    around closet bump-outs, into and out of every recess, and back to the start.
    Each segment is one linear path; the wall perimeter is their sum.
  * For an L-shaped or T-shaped room, the wall perimeter is the sum of ALL outer-edge
    segments (typically 6+ segments), NOT 2×(bounding-box length + width).
  * For a room with an alcove (e.g. window seat, built-in nook), include the two
    side walls and the back wall of the alcove in the wall perimeter — do NOT cut
    the corner across the alcove opening.
  * Final calc chain (all three numbers must come from the plans, NEVER estimated):
      sum of linear paths  =  WALL PERIMETER (LF)
      WALL PERIMETER × Wall Height  =  Wall Area (sqft)
    Record the LF total as "perimeter_lf" and use the same LF for base_trim_lf (see the base trim rule below — flag the room if the base material is resilient or unconfirmed).
- SHARED INTERIOR WALLS — COUNT IN BOTH ROOMS' WALL PERIMETERS:
  An interior partition wall has TWO painted faces, one in each adjoining room.
  Each room's WALL PERIMETER must include the full length of every wall that bounds
  it, even when the wall is shared with another room. Do NOT split a shared wall's
  LF between the two rooms, and do NOT count it in only one room.
  * Example: a 12'-long wall between Bedroom 1 and the Hallway contributes 12 LF
    to Bedroom 1's wall perimeter AND 12 LF to the Hallway's wall perimeter
    (24 LF of paintable wall surface total at that wall, since both faces are painted).
  * This applies to: unit demising walls, bedroom/bath partitions, closet walls,
    corridor walls, kitchen/living separators — every interior partition.
  * Exception: exterior walls and walls bounding non-paintable space (mech shafts,
    elevator shafts) only contribute to the one interior room they bound.
- Calculate: Ceiling Area = Length × Width (only if ceiling_painted = true)
- Base material + base trim LF: record the base/baseboard material you observe in
  "materials"."base" (e.g. "Painted Wood Base", "Rubber Base", "Vinyl Cove Base",
  "Tile Base", or "Unconfirmed" if the drawings/finish schedule don't say).
  * If the base is a PAINTED base (painted wood, MDF, or the finish schedule
    schedules the base to be painted): set base_trim_lf = room perimeter.
  * If the base is resilient / vinyl / rubber / cove base / tile, OR cannot be
    confirmed from the drawings or finish schedule on a COMMERCIAL / RETAIL space:
    set base_trim_lf = 0 (resilient cove base is the retail default and is NOT
    field-painted) and add to "notes": "Base material unverified/resilient —
    confirm paintable vs. resilient cove base (RFI)".
  Do NOT silently treat the base as paintable just because the room has gyp walls —
  vinyl and resilient cove base are common and are not field-painted. Pricing
  base trim that isn't painted is a fabrication; when in doubt on a commercial
  job, set 0 and flag rather than defaulting to perimeter.
- Level 5 finish: Check the FINISH SCHEDULE, wall type legends, and room notes for "Level 5",
  "Level 5 skim coat", "L5", "smooth finish", or "skim coat" specifications on any wall or ceiling.
  Common locations: entryways, foyers, hallways, great rooms, formal dining rooms (especially in
  high-end single-family homes). If L5 applies to ALL room walls and ceiling, set
  "level_5_finish_sqft" = wall_area_sqft + ceiling_area_sqft. If L5 applies to walls only, set
  it to wall_area_sqft. If L5 applies to ceiling only, set it to ceiling_area_sqft. If L5 is
  specified but you cannot tell the surface scope, set it to wall_area_sqft + ceiling_area_sqft
  (assume both). Set to 0 if not specified anywhere in the documents. Do NOT use a placeholder
  of 1 — Level 5 is priced per square foot ($0.55/sf), so a value of 1 yields nearly nothing.
- Concrete floor sealer: ONLY record concrete_floor_sqft when the specs EXPLICITLY call out
  sealcoating, concrete sealer, epoxy coating, or floor coating. A bare concrete floor alone
  does NOT qualify — the project must specifically require a sealer/coating application.
  * Check the FINISH SCHEDULE "Floor Finish" column for: "sealed concrete", "concrete sealer",
    "epoxy", "epoxy coating", "floor coating", "sealcoat", "floor sealer"
  * Do NOT assume concrete floors need sealer — only include when specs explicitly state it
  * If the finish schedule just says "concrete" or "conc." with no sealer reference, set to 0
  Set concrete_floor_sqft = floor_area_sqft ONLY for rooms with explicit sealer spec. Set to 0 otherwise.

Columns: Count painted structural columns visible on floor plans.
  - ONLY count columns marked with paint references (PT-?, "painted columns",
    "paint all exposed columns", column finish schedule showing paint)
  - Do NOT count columns inside walls or columns with no paint callout
  - Record as "painted_columns_ea" per room (set to 0 if none)

Wallcovering: CRITICAL — wallcovering_sqft must come ONLY from HARD NUMBERS on the drawings.
  - The ONLY valid sources for wallcovering area are:
    (a) the ROOM FINISH SCHEDULE "Wall Finish" column showing WC-1/WC-2/WC-3/WC-5/WC-6,
        "wallcovering", or "vinyl wallcovering" for that specific room, OR
    (b) an explicit WC-x label / wallcovering callout drawn on that room in the plans or
        interior elevations.
  - DO NOT infer wallcovering from the free-text SCOPE NOTES. A scope note such as
    "wallpaper removal on all corridors and guest rooms" tells you which rooms are IN SCOPE
    (see STEP 8B) — it does NOT tell you how many square feet of wall are wallcovered, nor
    which walls. NEVER set wallcovering_sqft = perimeter × height for a room just because the
    scope note mentions wallpaper.
  - If NO finish schedule and NO explicit WC label exists, you CANNOT determine the
    wallcovering area: set wallcovering_sqft = 0 for every room and add a note like
    "Wallcovering extent unconfirmed — no finish schedule; RFI required." This becomes an RFI.
  - When a finish schedule / explicit WC label DOES confirm wallcovering walls:
    * Calculate: wallcovering_sqft = confirmed wallcovering wall LF × wall height
    * If ALL walls in that room are confirmed wallcovered: wallcovering_sqft = perimeter ×
      ceiling height, and REDUCE wall_area_sqft by the same amount (those walls are NOT painted)
    * If SOME walls are wallcovered and some painted: split accordingly
  - SUBTRACT confirmed wallcovering walls from wall_area_sqft — never double-count as both paint and WC.
  - Removal vs install: if the scope says "remove/strip wallpaper", the confirmed area is a
    REMOVAL quantity, not new install — still record it as wallcovering_sqft (pricing applies the
    correct removal rate); do not invent install scope.

Stained Wood / Clear-Coat Panels: Check finish schedules and interior elevations for:
  - Stained wood panels, wood veneer panels, clear-coated wood
  - Finish codes like WD-1, WD-2, ST-1, "stain", "clear coat", "natural finish", "oak panel"
  - These are NOT painted — they require stain/clear-coat application ($6/sqft)
  - Calculate: stained_wood_sqft = panel area (height × width per panel × count)
  - SUBTRACT stained wood walls from wall_area_sqft — do NOT double-count as painted
  - Record as "stained_wood_sqft" per room (set to 0 if none)

Faux / Specialty Wall Finishes (Plaster, Lyme Wash): Check finish schedules and spec sections
  for specialty wall coatings that are NOT standard paint:
  - Plaster: "plaster", "Venetian plaster", "decorative plaster", "smooth plaster",
    "skim plaster", finish codes like PL-1, VP-1
  - Lyme wash / Lime wash: "lyme wash", "lime wash", "limewash", "mineral wash",
    finish codes like LW-1
  - When a room's wall finish is plaster or lyme wash, set materials.walls to that finish
    string EXACTLY as written ("Plaster", "Lyme Wash") — the pricing pipeline routes to
    the correct rate by reading materials.walls. Do NOT translate to "Paint" or "GYP".
  - Wall area for the room stays in wall_area_sqft as normal; the material string drives
    the pricing bucket downstream.
  - Common locations: lobbies, elevator lobbies, accent walls, feature walls

Soffits & Ceiling Types from RCP (REFLECTED CEILING PLAN):
  - Check REFLECTED CEILING PLANS (RCP) for:
    1. Ceiling TYPE per room: ACT grid pattern = NOT painted, GYP/GWB = painted
    2. Soffits: GYP drywall drops above the ceiling grid line
    3. Common area / corridor ceiling types — these are almost always ACT (not painted)
  - If RCP is not included in the drawing set, note it as missing data
  - Calculate soffit area: soffit LF × soffit depth (typically 1-3 feet).
  - Record as "soffit_sqft" per room (set to 0 if no soffits visible).
  - Interior soffits are painted the same as GYP walls — they are drywall surfaces.

Exterior Painting: If BUILDING ELEVATION drawings are visible:
  - Look for exterior paint designations (EP-1, EP-2, EP-3, EP-4, EX-PNL, "exterior paint")
  - Calculate total exterior paint area from elevations (width × height of painted areas)
  - Record in the "exterior" section as "exterior_paint_sqft"
  - Common items: masonry paint, EIFS paint, metal panel paint, precast panel paint
  - Exclude: glass curtain wall, storefront glazing, unpainted precast, ACM panels

CRITICAL WALL LINEAR FOOTAGE VALIDATION:
- For each residential floor, expect 2,500-4,000 LF of interior walls for a 10,000-15,000 sqft floor.
- Include ALL partition walls: unit demising walls, corridor walls, closet partitions, bathroom walls,
  kitchen walls, and entry vestibule walls. Every wall segment on the plan counts.
- A 20-unit building with 10,000 sqft floors typically has 3,000-3,500 LF of interior walls per floor.
- If your per-floor total wall LF is under 1,500 for a multi-unit residential floor, you are
  UNDER-MEASURING. Go back and count every wall segment including closets and bathrooms.
- COMMON ERROR: Measuring only the unit bounding box perimeter instead of all interior walls.
  A 2BR apartment with living room, 2 bedrooms, 2 bathrooms, kitchen, and closets has
  significantly MORE wall LF than just the unit's outer perimeter.
- DOUBLE-COUNTING IS REQUIRED FOR SHARED INTERIOR WALLS — NOT AN ERROR:
  When summing per-room perimeters, a wall between two rooms WILL appear in both
  rooms' perimeter totals. This is correct: each face of that wall is a separate
  paintable surface. Do NOT "deduplicate" shared walls when totaling LF or wall sqft.
  The only walls that should appear once are exterior walls and walls bounding
  non-paintable space (shafts, chases). Floor totals naturally exceed the floor's
  outer envelope by 2-3× because of this — that is the expected ratio.

SOURCE TRACKING — for each room, also provide:
- "source_page": the PDF page number (1-based) where this room's floor plan appears
- "source_sheet": the drawing sheet ID shown in the title block (e.g., "A-101", "A-201")
  If you cannot determine the sheet ID, use "Unknown".

STEP 3B: TYPICAL / REPEATED UNIT TYPES
Multi-unit residential buildings often have IDENTICAL floor plans repeated across units and floors.
- FIRST: Identify all unique unit TYPES (e.g., "1BR Type A = 876 SF", "Studio Type B = 456 SF")
- Determine the TOTAL COUNT of each unit type from the drawings, schedules, or unit mix tables.
- Look for unit counts in: Light & Ventilation schedules, unit mix tables, apartment number series
  (e.g., units 201-208 on 2nd floor = 8 units), door schedule unit groups, or key plans.
- CROSS-FLOOR MULTIPLICATION — explicit formula:
  unit_multiplier(type T) = (count of T on a typical floor) × (number of typical residential floors)
  * Example: 364 Main has unit numbers 201-210 (2nd floor) AND 301-310 (3rd floor) = 10 units/floor
    × 2 typical residential floors = 20 total residential units. If all 20 are the same type,
    one template with unit_multiplier=20.
  * Example: building has 4 studios + 3 1BR + 3 2BR per floor across 2 typical floors → multipliers
    are Studio=8, 1BR=6, 2BR=6, summing to 20 (= total_units).
  * Verification: sum(unit_multipliers across all residential templates) MUST equal
    total_units_in_project_info. If your sum is LESS than total_units, you forgot to multiply
    by the number of typical floors — fix before emitting.
- Extract ONE TEMPLATE unit of each type with FULL room-by-room detail (living, ALL bedrooms,
  ALL baths, closets).
- For each template room, add a "unit_multiplier" field set to the TOTAL number of identical
  units of that type ACROSS THE ENTIRE BUILDING (not per floor).
  Example: if there are 8 studios on Floor 2 and 8 studios on Floor 3, set unit_multiplier=16
  (total across both floors), NOT 8 per floor.
- Also add "unit_type": "Studio" or "unit_type": "1BR Type A" to each template room for clarity.
- DO NOT create 28 or 31 separate room entries — just ONE set of template rooms per unit type
  with the correct "unit_multiplier" value. The multiplication is applied automatically during
  post-processing.
- If a unit type appears only ONCE, set "unit_multiplier": 1 (or omit the field).
- CRITICAL: Do NOT pre-multiply the dimensions. Report dimensions for ONE unit only.

CRITICAL — TEMPLATE GROUPING RULES:
- Create ONE group per unit type that spans ALL floors: "Typical 1BR Units (Floors 2&3)" with
  the TOTAL count across all floors as the multiplier.
- DO NOT create separate template groups for each floor (e.g., do NOT create "2nd Floor Studios"
  with unit_multiplier=8 AND "3rd Floor Studios" with unit_multiplier=8 — this would double-count).
- DO NOT create both per-floor entries AND a combined entry for the same unit type.
  Either use one combined template OR per-floor templates, NEVER both.
- Use descriptive floor names for template groups: "Typical Studio Units (Floors 2-3)",
  "Typical 1BR Units (Floors 2&3)", NOT actual floor numbers like "2nd Floor".
- The sum of all unit_multiplier values across all unit types should approximately equal
  total_units from project_info. Cross-check this.
- If Floor 2 and Floor 3 have the same layout but are NOT part of a repeated unit type,
  list ALL rooms on BOTH floors separately.
- If 2nd floor plans aren't shown but are noted as "typical" to 3rd floor, duplicate 3rd floor rooms for 2nd floor

CRITICAL — MISSING FLOOR DETECTION:
- Count how many floors the building has from BUILDING SECTIONS, DRAWING INDEX, NOTES,
  DOOR SCHEDULE (door marks 200-series = 2nd floor, 300-series = 3rd floor), or
  LIGHT/VENTILATION SCHEDULES (unit numbers 201-210 = 2nd floor, 301-310 = 3rd floor)
- If a floor plan is NOT provided but the floor EXISTS in the building:
  * Check if it is marked "TYPICAL" or "TYP" to another floor
  * If so, DUPLICATE all rooms from the typical floor with the correct floor prefix
  * E.g., if 3rd Floor plan is shown and "2nd Floor typical to 3rd Floor":
    create all 3rd floor rooms again under "2nd Floor" with F2- prefixes
  * Include ALL rooms, doors, windows, trim — everything from the typical floor
  * IMPORTANT: If floor plans show identical layouts on floors 2 and 3 (same unit types,
    same apartment numbers ending in 01-10), use unit_multiplier: 2 on the template rooms
    OR create separate entries for both floors
- NEVER output fewer floors than the building actually has
- Cross-check: if the DOOR SCHEDULE has doors numbered 200-282 (2nd floor) AND 300-382 (3rd floor),
  BOTH floors must appear in the output even if only one floor plan drawing is provided
- In the "notes" array, state which floors were duplicated and why
  (e.g., "2nd Floor duplicated from 3rd Floor — typical layout per drawing notes")

FOUNDATION PLANS = BASEMENT:
- A "Foundation Plan" or "Foundation/Basement Plan" sheet shows the BASEMENT level.
  Treat it as a BASEMENT FLOOR PLAN and extract all rooms from it.
- Even if walls are shown as foundation/retaining walls on the exterior, the INTERIOR
  partitions are typically painted gyp walls (storage rooms, laundry, mechanical, corridors).
- DO NOT skip foundation plan sheets — they contain paintable spaces.
- Common basement rooms: storage, mechanical/boiler, laundry, bicycle storage, corridors,
  electrical/utility rooms. These all have paintable walls and often base trim.

STEP 4: DOORS — CLASSIFY BY TYPE
Check the DOOR AND FRAME SCHEDULE first (sheets A-501, A-502, etc.). The schedule is AUTHORITATIVE.
CRITICAL: If a door schedule exists, COUNT EVERY SINGLE DOOR listed in it.
- HM (Hollow Metal) panel with ALUM frame → "doors_hm_panel" (paint panel only, NOT frame)
- HM frame listed WITHOUT a door panel, OR frame listed with glass/aluminum panel
  that is NOT painted → "doors_frame_only" (paint frame only, no panel)
  Common patterns: "HM FR ONLY", "FRAME ONLY", HM frame with "-" or "NONE" for panel,
  or where schedule shows frame but panel column is empty/glass.
- Wood door (WD) with wood frame → "doors_full_paint" (door + frame both painted)
- Glass panel (GLAS) with wood frame (WD) → "doors_full_paint" (paint frame + panel)
- GLAS door with ALUM frame → do NOT count for painting (storefront/commercial, no paint)
- Doors with NO material listed (just width/height) → classify as "doors_full_paint"
  (these are typically interior residential doors with wood frames)
- If type is unclear → classify as "doors_full_paint"
- EXCLUDE prefinished doors: if a door's schedule entry, finish column, or notes
  mark it prefinished / pre-finished / factory-finished / "PF" / stained /
  clear-coat / anodized / clad, do NOT count it in any doors_* field — it is
  factory-finished, not field-painted.
- DOUBLE DOOR entries count as 1 door opening for painting purposes
Assign door counts BY FLOOR based on door mark numbers:
  - 000-099 = basement/common, 100-series = 1st floor, 200-series = 2nd floor, 300-series = 3rd floor
Count ALL doors on every floor, including unit entry doors, closet doors, and bathroom doors.
A 20-unit building typically has 150-200 doors total — if you find fewer than 50, re-check the schedule.

CRITICAL DOOR/WINDOW RULE FOR TEMPLATE ROOMS WITH UNIT_MULTIPLIER:
When you use unit_multiplier on template rooms, doors and windows are ALREADY multiplied
by the system. So set door/window counts PER SINGLE UNIT, not per floor total.
Example: If 10 identical apartments each have 4 doors → set doors_full_paint=4 on the
template room with unit_multiplier=10. The system will calculate 4×10=40 doors for the floor.
HOWEVER: If a door/window schedule exists, CROSS-CHECK your per-unit counts against the
schedule totals for that floor. If the schedule shows 45 doors on floor 2 and you have
10 units × 4 doors = 40 from templates, the remaining 5 doors are in corridors/common areas.
Do NOT double-count doors that appear both in template rooms AND in corridor/common rooms.

STEP 5: WINDOWS — REQUIRES WINDOW SCHEDULE
- Count ALL windows visible as "windows_total"
- "windows_painted_interior" = window SASHES requiring field paint (NOT casings/aprons)
- COMMERCIAL JOBS: ASSUME no window sashes are painted. windows_painted_interior = 0
  unless the WINDOW SCHEDULE explicitly says "field paint sash" or "paint operable sash".
- Factory-finished aluminum/vinyl/clad/storefront sashes are NEVER field-painted.
- WINDOW SCHEDULE IS REQUIRED to determine paint scope. The window TYPE detail
  shows whether each window has a casing, apron, stool/sill, wood return, or
  drywall return — these are the actual paintable trim components.
- IF NO WINDOW SCHEDULE EXISTS:
  * DO NOT assume any window paint scope (no casings, no aprons, no sashes)
  * Set windows_painted_interior = 0 for ALL rooms
  * Add a note: "No window schedule — window paint scope cannot be determined; RFI required"
  * Window TYPE is needed to know what trim components exist; without it, do not guess.

APRON DETECTION (call out if seen):
- Aprons can be called out on the FINISH SCHEDULE (a separate "Apron" or trim
  column), in WALL SECTIONS, or in INTERIOR ELEVATIONS showing apron trim below
  window sills.
- If an apron is shown in any of these sources, those windows have painted aprons.
- Apron COUNT comes from the WINDOW SCHEDULE (one apron per window of that type).
  Do NOT count aprons per room — record the global presence in notes:
  "Aprons called out in [finish schedule|wall sections|interior elevations] —
  count from window schedule"
- If aprons are shown in wall sections / interior elevations but no window
  schedule exists, still flag them in notes — they require RFI for accurate count.

CRITICAL WINDOW SCHEDULE RULE: If a WINDOW SCHEDULE exists, the total window
count across the ENTIRE building comes from that schedule — do NOT estimate or
guess window counts per room. Count totals from the schedule, then distribute
across rooms.

STEP 6: STAIRS — COUNT ACROSS ENTIRE BUILDING
- Count TOTAL stair flight sections in the entire building (1 section = one run between landings)
- A stair running from Floor 1 to Floor 3 has MULTIPLE sections (typically 2 per floor transition)
- A 4-story building with 2 stairwells typically has 2 stairs × ~3 floor transitions × ~2 flights = ~8-12 sections
- Only count stairs with painted components (wood treads, risers, railings, stringers)
- Estimate gyp wall area between/around ALL stair runs as "gyp_between_stairs_sqft"
- Include landings in the gyp wall area calculation
- INTERIOR HANDRAILS / GUARDRAILS: when stairs have a painted handrail, balcony
  rail, or open-stringer guardrail, record the rail run in LINEAR FEET on the
  stair room as "painted_railing_lf". Measure the rail length along the slope
  (stairs) or along the run (balcony/landing). Record on the room that contains
  the railing. Set to 0 when rails are stained/clear-coated, factory-finished
  metal, or glass — those are not painted.
STAIR ESTIMATION HINT: If this is a multi-story building with N floors and S stairwells:
- Typical stair sections ≈ S × (N-1) × 2 (two flights per floor transition)
- A 4-story building with 2 stairwells ≈ 2 × 3 × 2 = 12 sections
- Count what you can see, but if you only see partial floors, ESTIMATE the total
  based on stairwell count × floor count from building sections or drawing index
- Gyp between stairs: estimate ~80 sqft per stair section (walls around each run)

STEP 7: EXTERIOR ELEMENTS
Look at building elevations (A-201, A-202) and exterior detail sheets:
- Cornice / crown molding / fypon brackets → estimate total LINEAR FEET
  (typically = building perimeter at roofline; measure from elevation drawings)
- Exterior window trim → ONLY include if the elevation drawings or exterior details
  EXPLICITLY show painted wood trim around windows. Do NOT auto-estimate from interior
  window counts alone. Many projects have vinyl/aluminum-clad windows that need no painting.
  If painted exterior trim IS shown, measure total LINEAR FEET (head + sill + 2 jambs per window).
  Store this in "window_trim_lf". Set to 0 if not explicitly visible in exterior details.
- Soffits (if painted) → estimate sqft
- Railings on balconies, decks, roof areas → estimate LF as "railing_lf".
  Use this field for PAINTED rails (metal or wood). For natural-wood STAINED
  rails (cedar/Ipe/etc. with stain or clear coat), record under "stain_railing_lf"
  instead. Glass and factory-finished aluminum rails are excluded.
- SIDING / CLADDING MATERIALS — identify from elevation notes, details, and material callouts:
  * Hardie / fiber cement siding → measure total SQFT from elevation drawings ("hardie_siding_sqft")
  * Azek / PVC trim boards → measure total LF of trim runs ("azek_trim_lf")
  * Corner boards (Azek or wood) → count building corners × height, total LF ("corner_board_lf")
  * Steel exposed lintels above windows/doors → count and measure total LF ("steel_lintel_lf")
  * Set "exterior_siding_type" to primary cladding name (e.g. "hardie", "vinyl", "wood", "stucco")
  * When material-specific siding is identified, do NOT also include that area in exterior_paint_sqft
    (exterior_paint_sqft is ONLY for generic painted surfaces not covered by a specific material item)
  * If the drawings, finish schedule, or material callouts indicate any extracted siding/trim
    material is FACTORY-FINISHED, PRE-FINISHED, or otherwise NOT field-painted (e.g. vinyl siding,
    metal panels, factory-pre-finished Hardie ColorPlus), ADD an explicit note to "notes" using
    one of these phrases: "factory finish", "pre-finish", or "does not require paint". The cost
    engine assumes extracted siding IS in paint scope by default; without these keywords it will
    price them at the standard rates. Only suppress with these explicit phrases when warranted.
- Note if a lift/scaffold is required (3+ story buildings typically need a lift)
- Set lift_required = 1 if the building is 3+ stories and has ANY exterior paint scope

STEP 8: AGGREGATE TOTALS
Sum across ALL floors from the individual room data.
Only count rooms that are "in_scope" (see below) when computing aggregated totals.

""" + (f"""
STEP 8B: SCOPE FILTERING
The client has provided these SCOPE NOTES for this project:
  "{scope_notes}"

For EACH room you extract, evaluate whether it falls within the described scope.
- Add "in_scope": true if the room IS within the painting scope
- Add "in_scope": false if the room is NOT within scope
- Add "scope_exclusion_reason": "" if in_scope is true,
  or a brief explanation (e.g. "Basement excluded per scope notes") if false

IMPORTANT RULES:
- Still extract ALL rooms with full dimensions — do NOT skip any rooms
- Use your best judgment to interpret the scope notes:
  * "floors 2-4 only" → exclude all rooms on other floors
  * "residential only" → exclude commercial/retail spaces
  * "common areas only" → exclude individual tenant/apartment units
  * "skip mechanical rooms" → exclude mechanical/electrical/boiler rooms
  * "interior only" → set exterior values to 0 and note it
- If ambiguous, default to in_scope: true and add a note explaining the ambiguity
- SCOPE NOTES SET INCLUSION ONLY — NOT QUANTITIES. The scope notes determine which rooms are
  in/out of scope. They must NEVER be used to invent or change surface quantities. In
  particular, a note mentioning wallpaper/wallcovering does NOT make any wall wallcovered —
  wallcovering_sqft still comes only from a finish schedule or explicit WC label (see the
  Wallcovering instructions). Same for paint, ceilings, trim: quantities come from the drawings,
  not the scope text.
- Exterior elements: if scope says "interior only", set exterior values to 0
""" if scope_notes else """
""") + """Return this JSON structure:
{
  "project_info": {
    "total_floors_analyzed": 0,
    "total_rooms_found": 0,
    "scale_notation": "1/4 in = 1 ft",
    "building_type": "mixed-use",
    "total_stories": 3,
    "total_units": 20,
    "footprint_sqft": 10000,
    "prevailing_wage": {
      "applies": "unknown",
      "county": null,
      "wage_schedule_basis": null,
      "indicators": [],
      "source_pages": []
    }
  },
  "floors": [
    {
      "floor_name": "1st Floor",
      "rooms": [
        {
          "room_id": "F1-APT101-LIV",
          "room_name": "Living Room",
          "source_page": 3,
          "source_sheet": "A-102",
          "unit_multiplier": 1,
          "unit_type": "",""" + ("""
          "in_scope": true,
          "scope_exclusion_reason": "",""" if scope_notes else "") + """
          "dimensions": {
            "length_feet": 20,
            "width_feet": 15,
            "ceiling_height_feet": 9,
            "floor_area_sqft": 300,
            "perimeter_lf": 70,
            "wall_area_sqft": 630,
            "ceiling_area_sqft": 300
          },
          "materials": {
            "walls": "GYP",
            "ceiling": "GYP",
            "ceiling_painted": true,
            "base": "Painted Wood Base"
          },
          "elements": {
            "doors_full_paint": 2,
            "doors_hm_panel": 0,
            "doors_frame_only": 0,
            "windows_total": 3,
            "windows_painted_interior": 1,
            "base_trim_lf": 70,
            "stair_sections": 0,
            "gyp_between_stairs_sqft": 0,
            "level_5_finish_sqft": 0,
            "concrete_floor_sqft": 0,
            "painted_columns_ea": 0,
            "wallcovering_sqft": 0,
            "stained_wood_sqft": 0,
            "soffit_sqft": 0,
            "painted_railing_lf": 0
          },
          "notes": ""
        }
      ]
    }
  ],
  "aggregated_totals": {
    "total_paintable_wall_sqft": 0,
    "total_paintable_ceiling_sqft": 0,
    "total_cmu_wall_sqft": 0,
    "total_lymewash_wall_sqft": 0,
    "total_plaster_wall_sqft": 0,
    "total_dryfall_ceiling_sqft": 0,
    "total_base_trim_lf": 0,
    "total_doors_full_paint": 0,
    "total_doors_hm_panel": 0,
    "total_doors_frame_only": 0,
    "total_windows_painted_interior": 0,
    "total_windows_all": 0,
    "aprons_called_out": false,
    "aprons_callout_source": "",
    "total_stair_sections": 0,
    "total_gyp_between_stairs_sqft": 0,
    "total_level_5_finish_sqft": 0,
    "total_concrete_floor_sqft": 0,
    "total_painted_columns_ea": 0,
    "total_wallcovering_sqft": 0,
    "total_stained_wood_sqft": 0,
    "total_soffit_sqft": 0,
    "total_painted_railing_lf": 0
  },
  "exterior": {
    "cornice_lf": 0,
    "window_trim_lf": 0,
    "soffit_sqft": 0,
    "railing_lf": 0,
    "lift_required": false,
    "interior_lift_required": false,
    "exterior_paint_sqft": 0,
    "hardie_siding_sqft": 0,
    "azek_trim_lf": 0,
    "corner_board_lf": 0,
    "steel_lintel_lf": 0,
    "exterior_siding_type": "",
    "notes": ""
  },
  "material_legend": [
    {"code": "GYP", "description": "Gypsum board", "paintable": true}
  ],
  "notes": [
    "Important notes affecting the estimate"
  ]
}

CRITICAL RULES:
- Extract ACTUAL measurements from dimension callouts — do not guess
- If dimensions aren't clearly marked, note it and skip the room
- Only count PAINTABLE surfaces (gypsum/GWB walls, CMU walls with paint spec, wood trim/doors)
- CMU walls: classify as paintable when specs indicate paint, sealer, or block filler.
  Record wall material as "CMU" (distinct from "GYP") so pricing can apply CMU-specific rates.
- Exposed/open ceilings: classify as "DRYFALL" when specs indicate dryfall or spray-applied coating.
- Ceilings only count if ceiling_painted = true
- Windows: REQUIRES window schedule to determine TYPE and paintable components
  (casing/apron/stool/return/drywall return). Without a window schedule, set
  windows_painted_interior=0 in all rooms and flag for RFI — do NOT assume.
- Commercial jobs: assume window sashes are NOT painted (factory-finished)
- Door schedules override floor plan counts
- Include ALL hallways, corridors, lobbies, and common areas
- Base trim: record base_trim_lf = perimeter ONLY when the base is a confirmed painted base (painted wood/MDF or scheduled-to-paint); set base_trim_lf = 0 for resilient/vinyl/cove base or unconfirmed base on commercial/retail spaces, record the base material in materials.base, and flag it in the room notes (see the base trim rule above) so the estimator can confirm paint scope
- Break EVERY apartment into individual rooms — never list just "Living/Dining/Kitchen"
- Extract ALL closets as separate rooms: linen closets, coat closets, pantry closets,
  utility closets, walk-in closets, storage closets. These are commonly missed but
  contribute meaningful ceiling and wall area. Each closet needs its own dimensions,
  with ceiling_painted and base_trim_lf set per the ceiling and base trim rules above.
- For repeated unit types, create ONE template set per type with "unit_multiplier" set to total count
- Count stairs across the ENTIRE building, not just per-enclosure
- If you find NO floor plans, return:
  {"no_floor_plans_found": true, "pages_reviewed": "description of what pages contain",
   "has_door_schedule": true/false, "has_window_schedule": true/false}

Be thorough — analyze ALL pages of the PDF. Completeness is more important than brevity."""

    # Inject pre-extracted schedule counts as a preamble to the prompt
    if schedule_hint_text:
        prompt = schedule_hint_text + "\n" + prompt

    # Inject building inventory context as a preamble
    if building_inventory and isinstance(building_inventory.get("buildings"), list) and building_inventory["buildings"]:
        inv_parts = [
            "\n═══════════════════════════════════════════════════════════",
            "BUILDING INVENTORY (pre-extracted from drawing index pages):",
            "═══════════════════════════════════════════════════════════",
            f"This project contains {building_inventory['total_buildings']} buildings:",
        ]
        for b in building_inventory["buildings"]:
            code = b.get("building_type_code", "?")
            count = b.get("count", 1)
            name = b.get("building_name", "Unknown")
            units = b.get("units_per_building", "?")
            inv_parts.append(
                f"  - {name} [{code}]: {count} building(s), "
                f"{units} unit(s) per building"
            )
        inv_parts.append("")
        inv_parts.append(
            "CRITICAL: Set unit_multiplier values to reflect ALL identical "
            "buildings/units of each type.")
        inv_parts.append(
            "For example, if a room template represents a unit type that exists in 8 "
            "identical buildings, set unit_multiplier=8 (or 16 if there are 2 units "
            "per building × 8 buildings).")
        inv_parts.append(
            "Also report this building inventory in project_info fields: "
            "\"total_buildings\", \"building_inventory\".")
        inv_parts.append(
            "═══════════════════════════════════════════════════════════\n")
        inventory_text = "\n".join(inv_parts)
        prompt = inventory_text + "\n" + prompt

    # Inject pre-extracted text layer context (from PyMuPDF vector data)
    if text_layer_context:
        prompt = text_layer_context + "\n" + prompt

    # Viewport / plan-state directive for renovation jobs. When the cover-sheet
    # parse flagged this as a renovation (demolition + proposed-partition scope),
    # any sheet that shows EXISTING + PROPOSED side by side must be measured
    # from the PROPOSED viewport only. Prepended (not appended) so it is the
    # first instruction Claude sees about how to choose between viewports.
    if project_overview and project_overview.get("prefer_plan_state") == "proposed":
        viewport_directive = (
            "\n═══════════════════════════════════════════════════════════\n"
            "VIEWPORT / PLAN-STATE SELECTION  (CRITICAL — RENOVATION JOB)\n"
            "═══════════════════════════════════════════════════════════\n"
            "The cover sheet on this project describes a RENOVATION scope\n"
            "(demolition of existing partitions, installation of new partitions,\n"
            "reconfiguration of the interior layout). Treat the proposed plan\n"
            "as the only buildable scope.\n"
            "\n"
            "Many renovation sheets show TWO OR MORE viewports on a single page\n"
            "with title strips below each viewport, for example:\n"
            "    1  EXISTING FLOOR PLAN     |    2  PROPOSED FLOOR PLAN\n"
            "    DEMO PLAN                  |    NEW WORK PLAN\n"
            "    AS-BUILT                   |    PROPOSED\n"
            "\n"
            "When you encounter such a sheet you MUST:\n"
            "  - Identify the viewport whose title contains PROPOSED, NEW WORK,\n"
            "    NEW CONSTRUCTION, or RENOVATED. Extract rooms ONLY from that\n"
            "    viewport.\n"
            "  - Treat the EXISTING / AS-BUILT / DEMO viewport as REFERENCE\n"
            "    ONLY. Do NOT emit any rooms from it. Walls there are about\n"
            "    to be demolished or already exist outside the work scope.\n"
            "  - When a room appears in both viewports (e.g. a Toilet that\n"
            "    survives the renovation), take its measurements from the\n"
            "    PROPOSED viewport so the bounding walls reflect the NEW\n"
            "    partition layout.\n"
            "  - If you cannot determine which viewport is which, set\n"
            "    'viewport_resolution': 'ambiguous' on the floor and list the\n"
            "    viewport titles you saw. Do NOT silently pick one.\n"
            "\n"
            "Viewport title strips are usually centered UNDER each plan, with a\n"
            "leader circle (\"1\", \"2\", etc.), the title in ALL CAPS, and a scale\n"
            "below (e.g. SCALE: 1/4\" = 1'-0\").\n"
            "═══════════════════════════════════════════════════════════\n"
        )
        prompt = viewport_directive + "\n" + prompt

    return prompt


def analyze_construction_pdf(client, pdf_path, scope_notes="", schedule_hints=None,
                             building_inventory=None, project_overview=None):
    """
    Send PDF directly to Claude for analysis.
    Claude can read PDFs natively without conversion.
    """

    print(f"\n📄 Reading PDF file...")
    pdf_data = _load_pdf_for_api(pdf_path, _client_for_validation=client)

    prompt = _build_extraction_prompt(scope_notes=scope_notes, schedule_hints=schedule_hints,
                                      building_inventory=building_inventory,
                                      project_overview=project_overview)
    if schedule_hints:
        print(f"   📋 Schedule hints injected into extraction prompt")
    if building_inventory:
        print(f"   🏗️  Building inventory injected into extraction prompt")

    print("\n🔍 Sending PDF to Claude for analysis...")
    print("   (This may take 60-90 seconds for complex drawings)")
    
    def _call_api(data, label="", extra_context=""):
        """Send one base64 PDF to the API and return the response text.

        extra_context: optional preamble prepended to the prompt (used to pass
                       already-extracted room IDs to subsequent chunks).
        """
        if label:
            print(f"   📄 {label}")
        effective_prompt = extra_context + prompt if extra_context else prompt

        # Retry with exponential backoff on rate-limit (429) errors
        max_retries = 5
        base_delay = 30  # seconds

        for attempt in range(max_retries):
            try:
                result_parts = []
                with client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=64000,
                    temperature=0,
                    timeout=300.0,  # 5 min per chunk — fail fast rather than hang
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": data
                                }
                            },
                            {
                                "type": "text",
                                "text": effective_prompt
                            }
                        ]
                    }]
                ) as stream:
                    for text in stream.text_stream:
                        result_parts.append(text)
                    final_msg = stream.get_final_message()
                # Truncation detection: a response that hit max_tokens cut
                # off mid-JSON. Before this check, truncated chunks were
                # recorded as "succeeded" and their rooms silently vanished
                # at parse time — the single biggest source of run-to-run
                # room-count variance (53 rooms vs 15 on identical plans).
                # Raise so the caller's chunk-failure ladder retries/records
                # it instead of shipping a partial extraction.
                if getattr(final_msg, "stop_reason", None) == "max_tokens":
                    raise TruncatedResponseError(
                        f"Response truncated at max_tokens"
                        f"{' (' + label + ')' if label else ''} — "
                        f"{len(''.join(result_parts))} chars received"
                    )
                return "".join(result_parts)
            except anthropic.RateLimitError:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)  # 30s, 60s, 120s, 240s
                    print(f"   ⏳ Rate limit hit — waiting {delay}s before retry "
                          f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    raise  # exhausted all retries
            except anthropic.InternalServerError:
                # Catches 500 and 529 (overloaded) errors
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"   ⏳ API overloaded/500 — waiting {delay}s before retry "
                          f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    raise  # exhausted all retries
            except anthropic.APITimeoutError:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"   ⏳ API timeout — waiting {delay}s before retry "
                          f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    raise  # exhausted all retries

    try:
        # --- Primary call with the first (or only) chunk ---
        result_text = None
        # Chunk-relative page numbers permanently removed by page-level
        # retries, keyed by chunk number. Persisted into _chunk_tracking so
        # a dropped page exists somewhere other than the console log.
        pages_dropped_by_chunk = {}
        try:
            result_text = _call_api(pdf_data)
        except anthropic.BadRequestError as e:
            err_str = str(e)
            is_too_large = (
                "Request exceeds the maximum size" in err_str
                or "request_too_large" in err_str.lower()
                or "413" in err_str
            )
            if _MULTIMODAL_DENSE_PAGES_ENABLED and is_too_large:
                print(f"   🔀 First chunk too large for native PDF — trying multi-modal retry")
                raw_bytes = base64.standard_b64decode(pdf_data)
                first_tmp = None
                try:
                    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                        tmp.write(raw_bytes)
                        first_tmp = tmp.name
                    result_text = _multimodal_chunk_retry(
                        client, first_tmp, prompt, chunk_label="chunk 1"
                    )
                finally:
                    if first_tmp:
                        try:
                            os.unlink(first_tmp)
                        except Exception:
                            pass

                if result_text is None and not _pending_chunks:
                    raise  # multi-modal didn't recover, no fallback chunks

            elif "Could not process" in err_str:
                print(f"   ⚠️  First chunk failed — attempting page-level retry")
                # Reconstruct a temp file from base64 so retry helper can read pages
                raw_bytes = base64.standard_b64decode(pdf_data)
                first_tmp = None
                try:
                    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                        tmp.write(raw_bytes)
                        first_tmp = tmp.name
                    result_text = _retry_chunk_without_bad_pages(
                        first_tmp, _call_api, chunk_label="chunk 1",
                        dropped_pages_out=pages_dropped_by_chunk.setdefault(1, [])
                    )
                finally:
                    if first_tmp:
                        try:
                            os.unlink(first_tmp)
                        except Exception:
                            pass

                if result_text is None and not _pending_chunks:
                    raise  # No other chunks to fall back on
            else:
                raise  # Not a recoverable BadRequestError

        # --- Process remaining chunks (if PDF was split) ---
        if _pending_chunks:
            all_texts = [result_text] if result_text else []
            total_chunks = len(_pending_chunks) + 1
            chunks_succeeded = [1] if result_text else []  # track which chunk #s succeeded
            chunks_failed = [] if result_text else [1]     # track which chunk #s failed

            def _extract_building_context_from_texts(texts):
                """Parse JSON from response texts and build a rich context summary
                including room IDs, building info, and unit types found so far."""
                ids = []
                unit_types = set()
                building_type = ""
                total_stories = 0
                total_units = 0
                floors_seen = []
                for t in texts:
                    m = re.search(r'\{.*\}', t, re.DOTALL)
                    if m:
                        try:
                            data = json.loads(m.group())
                            # Gather project info
                            pi = data.get("project_info", {})
                            if pi.get("building_type"):
                                building_type = pi["building_type"]
                            if pi.get("total_stories") and str(pi["total_stories"]).strip().isdigit():
                                total_stories = max(total_stories, int(pi["total_stories"]))
                            if pi.get("total_units") and str(pi["total_units"]).isdigit():
                                total_units = max(total_units, int(pi["total_units"]))
                            for fl in data.get("floors", []):
                                floors_seen.append(fl.get("floor_name", ""))
                                for rm in fl.get("rooms", []):
                                    rid = rm.get("room_id", "")
                                    if rid:
                                        ids.append(rid)
                                    ut = rm.get("unit_type", "")
                                    if ut:
                                        unit_types.add(ut)
                        except (json.JSONDecodeError, AttributeError):
                            pass
                return {
                    "room_ids": ids,
                    "unit_types": sorted(unit_types),
                    "building_type": building_type,
                    "total_stories": total_stories,
                    "total_units": total_units,
                    "floors_seen": floors_seen,
                }

            def _resend_chunk_on_transient(chunk_path, idx, total_chunks,
                                           chunk_context, reason,
                                           attempts=2, backoff=20):
                """Re-send an entire chunk after a transient failure rather
                than silently dropping its pages. The chunk file still exists
                at this point (the loop's `finally` unlink runs after the
                except block). Returns the response text, or None if every
                retry fails.

                Added 2026-06-08 after Aliante (Wingstop): chunk 3 (A1.1 floor
                plan + A2.0 RCP) hit a connection reset that fell through to
                the generic except with no retry, so the two most scope-
                critical sheets were dropped from 2 of 3 passes. _call_api's
                internal retries cover mid-call hiccups; this covers the case
                where the whole call errored out and the chunk would otherwise
                be lost.
                """
                for attempt in range(1, attempts + 1):
                    wait = backoff * attempt
                    print(f"   🔁 Chunk {idx}/{total_chunks} {reason} — "
                          f"resend attempt {attempt}/{attempts} after {wait}s")
                    time.sleep(wait)
                    try:
                        with open(chunk_path, 'rb') as _rf:
                            _b64 = base64.standard_b64encode(
                                _rf.read()).decode("utf-8")
                        return _call_api(
                            _b64,
                            label=(f"Retry chunk {idx}/{total_chunks} "
                                   f"({len(_b64)/1024:.0f} KB)"),
                            extra_context=chunk_context)
                    except Exception as _re:
                        print(f"   ⚠️  Chunk {idx}/{total_chunks} resend "
                              f"attempt {attempt} failed: {str(_re)[:100]}")
                return None

            for idx, chunk_info in enumerate(_pending_chunks, 2):
                chunk_path, _chunk_start = chunk_info if isinstance(chunk_info, tuple) else (chunk_info, 1)

                # Release memory between chunks — Python's heap grows as chunks
                # are loaded/encoded/sent, and glibc malloc holds onto freed
                # pages by default. The 15-sec pause is a natural boundary to
                # call malloc_trim and shed RSS before the next chunk grows it
                # again. Critical for dense-vector PDFs where worker preemption
                # was triggering ~2 min into chunked extraction (2026-05-08
                # Waverly investigation).
                _release_memory(f"before chunk {idx}/{total_chunks}")

                # Preventive delay between chunks to stay under rate limit
                print(f"   ⏱️  Pausing 15s between chunks to respect rate limit...")
                time.sleep(15)

                # Build rich context of what's been extracted from prior chunks
                ctx = _extract_building_context_from_texts(all_texts)
                chunk_context = ""
                if ctx["room_ids"]:
                    chunk_context = (
                        f"\nIMPORTANT — BUILDING CONTEXT FROM PREVIOUS PAGES:\n"
                    )
                    if ctx["building_type"]:
                        chunk_context += f"Building type: {ctx['building_type']}\n"
                    if ctx["total_stories"]:
                        chunk_context += f"Total stories: {ctx['total_stories']}\n"
                    if ctx["total_units"]:
                        chunk_context += f"Total units in building: {ctx['total_units']}\n"
                    if ctx["unit_types"]:
                        chunk_context += f"Unit types identified so far: {', '.join(ctx['unit_types'])}\n"
                    if ctx["floors_seen"]:
                        chunk_context += f"Floors already extracted: {', '.join(ctx['floors_seen'])}\n"
                    chunk_context += (
                        f"\nThe following {len(ctx['room_ids'])} rooms have been measured from earlier pages.\n"
                        f"DO NOT re-extract these rooms. If you see the same room, SKIP it.\n"
                        f"Already extracted room_ids: {json.dumps(ctx['room_ids'][:200])}\n"
                        f"If you find ADDITIONAL info about an already-extracted room "
                        f"(e.g., door schedule entry), note it but do NOT create a new room entry.\n"
                        f"IMPORTANT: Maintain consistency with the unit types and counts above.\n"
                        f"If earlier chunks identified Studios, 1BRs, and 2BRs, these same types "
                        f"should appear in your extraction if this chunk contains those floor plans.\n\n"
                    )

                try:
                    with open(chunk_path, 'rb') as f:
                        chunk_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
                    txt = _call_api(chunk_b64,
                                    label=f"Processing chunk {idx}/{total_chunks} ({len(chunk_b64)/1024:.0f} KB)",
                                    extra_context=chunk_context)
                    all_texts.append(txt)
                    chunks_succeeded.append(idx)
                except anthropic.BadRequestError as e:
                    err_str = str(e)
                    is_too_large = (
                        "Request exceeds the maximum size" in err_str
                        or "request_too_large" in err_str.lower()
                        or "413" in err_str
                    )
                    chunk_prompt = (chunk_context + prompt) if chunk_context else prompt
                    if _MULTIMODAL_DENSE_PAGES_ENABLED and is_too_large:
                        print(f"   🔀 Chunk {idx}/{total_chunks} too large for native PDF — trying multi-modal retry")
                        retry_result = _multimodal_chunk_retry(
                            client, chunk_path, chunk_prompt,
                            chunk_label=f"chunk {idx}/{total_chunks}"
                        )
                        if retry_result:
                            all_texts.append(retry_result)
                            chunks_succeeded.append(idx)
                        else:
                            print(f"   ⚠️  Chunk {idx}/{total_chunks} — multi-modal recovered nothing")
                            chunks_failed.append(idx)
                    elif "Could not process" in err_str or "500" in err_str:
                        print(f"   ⚠️  Chunk {idx}/{total_chunks} failed ({err_str[:120]}) — attempting page-level retry")
                        retry_result = _retry_chunk_without_bad_pages(
                            chunk_path, _call_api, chunk_label=f"chunk {idx}/{total_chunks}",
                            dropped_pages_out=pages_dropped_by_chunk.setdefault(idx, [])
                        )
                        if retry_result:
                            all_texts.append(retry_result)
                            chunks_succeeded.append(idx)
                        else:
                            print(f"   ⚠️  Chunk {idx}/{total_chunks} — no usable pages recovered")
                            chunks_failed.append(idx)
                    else:
                        print(f"   ⚠️  Chunk {idx}/{total_chunks} failed: {e}")
                        chunks_failed.append(idx)
                except TruncatedResponseError as chunk_err:
                    # Response overflowed max_tokens — resending the same
                    # chunk truncates again (temperature 0). Page-level
                    # retry sends each page alone: small responses, no
                    # truncation possible.
                    print(f"   ✂️  Chunk {idx}/{total_chunks} truncated at max_tokens — "
                          f"retrying page-by-page ({chunk_err})")
                    retry_result = _retry_chunk_without_bad_pages(
                        chunk_path, _call_api, chunk_label=f"chunk {idx}/{total_chunks}",
                        dropped_pages_out=pages_dropped_by_chunk.setdefault(idx, [])
                    )
                    if retry_result:
                        all_texts.append(retry_result)
                        chunks_succeeded.append(idx)
                    else:
                        print(f"   ⚠️  Chunk {idx}/{total_chunks} — page-level retry after truncation recovered nothing")
                        chunks_failed.append(idx)
                except anthropic.InternalServerError as chunk_err:
                    # Overloaded or 500 after all retries exhausted — try one
                    # more full resend before giving up, then skip (don't crash).
                    recovered = _resend_chunk_on_transient(
                        chunk_path, idx, total_chunks, chunk_context,
                        reason="API overloaded")
                    if recovered is not None:
                        all_texts.append(recovered)
                        chunks_succeeded.append(idx)
                    else:
                        print(f"   ⚠️  Chunk {idx}/{total_chunks} skipped (API overloaded after retries): {str(chunk_err)[:100]}")
                        chunks_failed.append(idx)
                except Exception as chunk_err:
                    # Transient (connection reset, read timeout, etc.) — resend
                    # the whole chunk before dropping its pages.
                    recovered = _resend_chunk_on_transient(
                        chunk_path, idx, total_chunks, chunk_context,
                        reason="transient error")
                    if recovered is not None:
                        all_texts.append(recovered)
                        chunks_succeeded.append(idx)
                    else:
                        print(f"   ⚠️  Chunk {idx}/{total_chunks} failed: {chunk_err}")
                        chunks_failed.append(idx)
                finally:
                    try:
                        os.unlink(chunk_path)
                    except Exception:
                        pass

            # Report chunk processing results
            print(f"\n   📊 Chunk results: {len(chunks_succeeded)}/{total_chunks} succeeded")
            if chunks_failed:
                print(f"   ⚠️  FAILED chunks: {chunks_failed} — data from these pages is MISSING")
                print(f"       This may cause the estimate to be significantly lower than expected.")

            # Merge all successful chunk responses (with page offsets for source tracking)
            if len(all_texts) > 1:
                _parse_failed_chunks = []
                result_text = _merge_chunk_responses(
                    all_texts,
                    page_offsets=_chunk_page_offsets,
                    chunk_indices=chunks_succeeded,
                    parse_failures=_parse_failed_chunks,
                )
                # A chunk whose response failed to parse contributed zero
                # rooms — it FAILED, whatever the API status said. Reconcile
                # the ledgers so downstream warnings/triggers see the loss.
                if _parse_failed_chunks:
                    chunks_succeeded = [c for c in chunks_succeeded
                                        if c not in _parse_failed_chunks]
                    chunks_failed = sorted(set(chunks_failed) | set(_parse_failed_chunks))
                    print(f"   ⚠️  {len(_parse_failed_chunks)} chunk(s) reclassified "
                          f"as FAILED (unparseable response): {_parse_failed_chunks}")
            elif len(all_texts) == 1:
                result_text = all_texts[0]
            else:
                raise RuntimeError("All PDF chunks failed — no data could be extracted")

            # Inject chunk tracking metadata into the merged result.
            # chunk_page_ranges (set in _load_pdf_for_api) lets downstream
            # validation flag pages that returned 0 rooms inside a chunk
            # whose siblings yielded data — i.e., silent truncation.
            try:
                merged_data = json.loads(re.search(r'\{.*\}', result_text, re.DOTALL).group())
                merged_data["_chunk_tracking"] = {
                    "total_chunks": total_chunks,
                    "chunks_succeeded": chunks_succeeded,
                    "chunks_failed": chunks_failed,
                    "chunk_page_ranges": list(_chunk_plan_ranges),
                    "pages_dropped": {k: v for k, v in pages_dropped_by_chunk.items() if v},
                }
                result_text = json.dumps(merged_data)
            except (json.JSONDecodeError, AttributeError) as _parse_err:
                # Don't break the flow, but make the loss visible — without this
                # warning, chunk_tracking silently drops to None in the output and
                # debugging which chunks succeeded/failed becomes guesswork.
                print(f"   ⚠️  Could not inject chunk_tracking into merged response "
                      f"({type(_parse_err).__name__}: {str(_parse_err)[:120]}); "
                      f"chunks_succeeded={chunks_succeeded}, chunks_failed={chunks_failed}")

        if result_text is None:
            raise RuntimeError("PDF analysis produced no results")

        # --- Remap source_page from filtered PDF indices → original PDF indices ---
        if _page_index_map:
            try:
                data = json.loads(re.search(r'\{.*\}', result_text, re.DOTALL).group())
                remapped = 0
                for floor in data.get("floors", []):
                    for room in floor.get("rooms", []):
                        sp = room.get("source_page")
                        if isinstance(sp, (int, float)) and sp > 0:
                            # sp is 1-based in the filtered PDF
                            filtered_0 = int(sp) - 1
                            orig_0 = _page_index_map.get(filtered_0)
                            if orig_0 is not None:
                                room["source_page"] = orig_0 + 1  # back to 1-based
                                remapped += 1
                if remapped:
                    print(f"   🔢 Remapped {remapped} source_page values to original PDF page numbers")
                result_text = json.dumps(data)
            except (json.JSONDecodeError, AttributeError):
                pass  # don't break flow if parsing fails

        return result_text

    except Exception as e:
        # Clean up any remaining chunk files on error
        for cp in _pending_chunks:
            try:
                os.unlink(cp)
            except Exception:
                pass
        print(f"\n❌ Error analyzing PDF: {e}")
        raise

def analyze_schedule_pdf(client, pdf_path):
    """
    Second-pass analysis for PDFs that contain door/window schedules but no floor plans.
    Extracts schedule data (door counts by type, window paint specs, stair info)
    that can be applied as overrides to the merged room-level analysis.
    """
    print(f"\n📋 Re-analyzing for schedule data: {os.path.basename(pdf_path)}")

    pdf_data = _load_pdf_for_api(pdf_path, _client_for_validation=client)

    schedule_prompt = """You are analyzing ARCHITECTURAL DRAWING SHEETS that contain SCHEDULES and DETAILS (not floor plans).

Look for these specific items and extract counts:

1. DOOR SCHEDULE (usually sheet A-501 or A-502):
   - Count doors by TYPE — ONLY count doors that require FIELD PAINTING:
     * "doors_full_paint": Hollow Metal (HM) doors where BOTH the panel AND frame are painted on-site
       (HM1, HM2 types — these are the primary painted doors in commercial buildings)
     * "doors_hm_panel": HM panel doors where ONLY the panel is painted (frame is factory-finished)
     * "doors_frame_only": HM frames where only the FRAME is painted (panel is pre-finished or glass)
   - EXCLUDE these door types — they are NOT field-painted:
     * Storefront doors (AD1, AL1, SD-1) — aluminum, factory-finished
     * Overhead/rolling doors (OHD1, OHD2, OHD3, RD-1) — factory-finished
     * Wood doors (WD types) — typically factory pre-finished; also exclude any
       door the schedule's finish/remarks column marks prefinished, factory-
       finished, "PF", stained, clear-coat, anodized, or clad
     * Glass doors (GL1, GL-1) — not painted
     * Doors marked "NOT USED" or "NIC" — skip entirely
   - Count ALL qualifying doors in the schedule across ALL floors
   - If the schedule has columns for "Material" or "Type", use those to classify
   - For commercial buildings, most painted doors are HM (Hollow Metal) type

2. WINDOW SCHEDULE — extract per-type paintable components:
   - Count total windows in the schedule
   - For EACH window type/mark, read the TYPE/FRAME/FINISH columns AND any
     window TYPE detail drawings to determine which trim components are present:
     * CASING — wood/MDF trim around the opening (head + jambs)
     * APRON — horizontal trim below the interior sill
     * STOOL / SILL — interior sill board
     * WOOD RETURN — wood jamb extension wrapping back to wall
     * DRYWALL RETURN — gypsum return at jamb (no casing — wall paint scope)
     * SASH — operable sash (almost always factory-finished)
   - SASH PAINT (windows_painted_interior = painted sashes only):
     * COMMERCIAL JOBS: ASSUME sashes are NOT painted. Set windows_painted_interior=0
       unless schedule EXPLICITLY says "field paint sash" or "paint operable sash".
     * Storefront/aluminum/vinyl/fiberglass/clad/fire-rated/pre-finished = NOT painted.
     * Only count sashes as painted with explicit field-paint callout.
   - PER-COMPONENT COUNTS — sum quantity-per-type across the schedule:
     * windows_with_casing — windows whose type detail shows wood/MDF casing
     * windows_with_apron — windows whose type detail shows an apron
     * windows_with_stool_sill — windows whose type detail shows a wood stool/sill
     * windows_with_wood_return — windows with wood jamb returns
     * windows_with_drywall_return — windows with gypsum returns (informational)
   - If the schedule doesn't clearly show component construction, set those
     counts to 0 and note "unable to determine components from schedule"
   - Note the paint specification if found (e.g., "Black painted interior")

3. STAIR INFORMATION (from sections, details, or notes):
   - Count total stair SECTIONS (flights between landings) across the entire building
   - Note stair materials (wood painted, metal, etc.)

4. WALL TYPE SPECIFICATIONS:
   - List partition types with materials (GYP, GWB, CMU, etc.)

Return ONLY this JSON:
{
  "has_schedules": true,
  "door_schedule": {
    "total_doors_full_paint": 0,
    "total_doors_hm_panel": 0,
    "total_doors_frame_only": 0,
    "doors_by_floor": {},
    "door_marks_counted": [],
    "notes": ""
  },
  "window_schedule": {
    "total_windows": 0,
    "windows_painted_interior": 0,
    "windows_with_casing": 0,
    "windows_with_apron": 0,
    "windows_with_stool_sill": 0,
    "windows_with_wood_return": 0,
    "windows_with_drywall_return": 0,
    "window_paint_spec": "",
    "notes": ""
  },
  "stair_info": {
    "total_stair_sections": 0,
    "notes": ""
  },
  "wall_types": [],
  "notes": []
}

If a schedule is NOT present in this PDF, set its counts to 0 and note "not found".
Be precise — count every entry in each schedule row by row."""

    try:
        result_parts = []
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            temperature=0,
            timeout=300.0,  # 5 min timeout
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_data
                        }
                    },
                    {
                        "type": "text",
                        "text": schedule_prompt
                    }
                ]
            }]
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)

        result_text = "".join(result_parts)
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            schedule_data = json.loads(json_match.group())
            if schedule_data.get("has_schedules"):
                ds = schedule_data.get("door_schedule", {})
                ws = schedule_data.get("window_schedule", {})
                si = schedule_data.get("stair_info", {})
                print(f"   ✅ Schedule data found:")
                print(f"      Doors: {_num(ds.get('total_doors_full_paint', 0)):.0f} full paint"
                      f" + {_num(ds.get('total_doors_hm_panel', 0)):.0f} HM panel")
                print(f"      Windows: {_num(ws.get('windows_painted_interior', 0)):.0f} painted interior"
                      f" / {_num(ws.get('total_windows', 0)):.0f} total")
                print(f"      Stairs: {_num(si.get('total_stair_sections', 0)):.0f} sections")
                return schedule_data
            else:
                print(f"   ⚠️  No schedules found in this PDF")
                return None
        else:
            print(f"   ⚠️  Could not parse schedule response")
            return None

    except Exception as e:
        print(f"   ❌ Error extracting schedules: {e}")
        return None


def _extract_room_finish_schedule(client, pdf_path):
    """
    Extract Room Finish Schedule data from PDFs that have schedules but no floor plans.
    Unlike analyze_schedule_pdf() which gets door/window/stair counts, this function
    extracts room-level detail from Room Finish Schedules (A1.04, etc.) to enable
    schedule-based wall/ceiling estimation when floor plans are missing.

    Returns dict with 'room_finish_schedule' list and 'building_info' dict, or None.
    """
    print(f"\n📊 Extracting Room Finish Schedule: {os.path.basename(pdf_path)}")

    pdf_data = _load_pdf_for_api(pdf_path, _client_for_validation=client)

    room_finish_prompt = """You are analyzing ARCHITECTURAL DRAWING SHEETS that contain SCHEDULES (not floor plans).

Your task: Extract the ROOM FINISH SCHEDULE and BUILDING INFORMATION from this document.

1. ROOM FINISH SCHEDULE (usually sheet A1.04, A1.04A, or similar "Room Finish" pages):
   For EACH room listed in the Room Finish Schedule, extract:
   - room_name: The room name/type (e.g., "Living Room", "Bedroom 1", "Kitchen", "Bathroom", "Hallway", "Closet")
   - room_number: The room number if shown
   - wall_finish: What's specified for walls (e.g., "Paint", "PT-1", "Wallcovering", "Tile", "CMU Paint",
     "Plaster", "Venetian Plaster", "Lyme Wash", "Lime Wash"). Preserve the exact spec wording —
     specialty finishes like plaster and lyme wash are priced separately from standard paint.
   - ceiling_finish: What's specified for ceiling (e.g., "GWB - Paint", "ACT", "Exposed")
   - base_finish: What's specified for base/baseboard (e.g., "Wood Base", "WD-1", "Rubber Base", "Tile Base", "None")
   - floor_finish: What's specified for floor (e.g., "Hardwood", "Carpet", "Tile", "Concrete", "VCT")
   - unit_type: If rooms are grouped by unit type (e.g., "2BR Type A", "3BR Type B", "Studio"), include this
   - floor_level: Which floor level (e.g., "1", "2", "Basement", "Ground")

   IMPORTANT: List rooms for ONE REPRESENTATIVE UNIT of each type. Don't repeat the same room list
   for each identical unit — we'll use unit_types counts to multiply.

2. BUILDING INFORMATION (from cover sheets, title blocks, general notes, drawing indices):
   - building_name: The building name (e.g., "Residence Building", "Amenities Building")
   - total_identical_buildings: How many IDENTICAL copies of this building exist in the project?
     Look for notes like "Buildings 1-6" or "6 identical buildings" or building numbering.
     If this PDF covers just ONE building type with no indication of multiples, use 1.
   - unit_types: List of apartment/unit types with counts PER BUILDING
     e.g., [{"type": "2BR", "count_per_building": 6}, {"type": "3BR", "count_per_building": 6}]
   - total_units_per_building: Total residential units per building
   - floors_per_building: Number of OCCUPIED HUMAN FLOOR LEVELS in the building, counted
     from a building section or elevation. The default for retail/big-box and most commercial
     tenant fit-outs is 1.
     DO COUNT: ground floor, second floor, third floor, etc. — anywhere people occupy.
     DO NOT COUNT: the roof, the parapet, a clerestory, a foundation/crawlspace, a mechanical
       penthouse, or a mezzanine that is less than 50% of the floor area below it.
     DO NOT INFER from the count of unique values in the schedule's "Floor Level" column —
       that column often contains values like "1" and "Roof" or "1" and "Mezzanine" which
       must NOT bump the count to 2.
     DO NOT INFER from seeing both a "Floor Plan" sheet and a "Roof Plan" sheet — that is
       still a single-story building.
     If the only floor plan is sheet A-101 (or equivalent first-floor plan), and there is
     no A-102/A-201/etc. showing rooms on a higher level, the answer is 1.
   - has_garage: Whether the building has a parking garage (true/false)
   - garage_floor_area_sqft: If garage area is noted anywhere, include it (0 if unknown)
   - has_pool: Whether this is a pool/amenities building
   - ceiling_height_ft: Ceiling height in feet — PULL FROM BUILDING SECTIONS.
     Identify Building Sections by their CONTENT, not by a fixed sheet number — they
     may live on A-300, A-400, AS-101, A2.10, or any other sheet on this job. Look
     for any sheet whose title block, drawing title, or detail label reads
     "BUILDING SECTION(S)", "WALL SECTION", "TRANSVERSE SECTION", "LONGITUDINAL
     SECTION", "CROSS SECTION", "SECTION A/B/etc.", or that shows a vertical cut
     through the building with stacked floors and ceiling lines.
     Read the labeled ceiling height (e.g. "9'-0" CLG", "CLG HT 10'-0"",
     "T.O. SLAB to U/S CLG = 9'-6"") and convert to decimal feet.
     If no ceiling height label is present anywhere in the document, MEASURE IT from a
     Building Section using the drawing's scale (1/4" = 1'-0", 1/8" = 1'-0", etc.) or
     the graphic scale bar — measure floor-to-underside-of-ceiling vertically and convert.
     DO NOT default to 8', 9', or 9'-6". Do NOT assume. If you cannot read a label and
     cannot measure from a section, set this to 0 and add a note explaining why.

3. COMMON AREA ROOMS: Also extract common area rooms (lobbies, corridors, stairwells, mechanical rooms,
   trash rooms, storage rooms) that appear in the finish schedule. These exist once per floor, not per unit.
   Mark their unit_type as "common_area".

4. STRUCTURAL / EXPOSED-SURFACE FINISH CALLOUTS (CRITICAL FOR COMMERCIAL/RETAIL):
   Many finish schedules — especially on big-box retail, warehouses, and tenant fit-outs — include
   ROWS THAT ARE NOT ROOMS. They specify finishes for exposed building elements that are painted
   throughout the space. Examples:
     - "Exposed structure, roof deck, joists, beams: Semi-Gloss Enamel"
     - "Exposed HVAC ductwork, conduits, piping: Semi-Gloss"
     - "Open ceiling — paint deck and structure to match"
     - "All exposed MEP above ceiling grid: Dryfall, white"
   These rows describe SURFACES, not rooms. DO NOT try to fit them into the room_finish_schedule.
   Instead, list them in a separate top-level array "structural_finish_scope" with this shape:
     {
       "surfaces": ["roof deck", "structure", "ducts", "conduits", "piping", "joists", "beams"],
       "finish": "Semi-Gloss Enamel" or "Dryfall" or whatever the schedule specifies,
       "applies_to": "all" or "open-ceiling areas" or list of specific room names,
       "color": "<color or null>",
       "note": "<verbatim text snippet from the schedule>"
     }
   Capture EVERY such row. These are typically the single largest dollar item on a retail/commercial
   project — missing them causes proposals to come in 30-50% low.

Return ONLY this JSON:
{
  "room_finish_schedule": [
    {
      "room_name": "Living Room",
      "room_number": "101",
      "wall_finish": "Paint",
      "ceiling_finish": "GWB - Paint",
      "base_finish": "Wood Base",
      "floor_finish": "Hardwood",
      "unit_type": "2BR",
      "floor_level": "1",
      "is_common_area": false
    }
  ],
  "structural_finish_scope": [
    {
      "surfaces": ["roof deck", "structure", "ducts", "conduits"],
      "finish": "Semi-Gloss Enamel",
      "applies_to": "all open-ceiling areas",
      "color": null,
      "note": "Exposed structure, roof deck, HVAC, ducts, electrical, conduits to be Semi-Gloss enameled"
    }
  ],
  "building_info": {
    "building_name": "",
    "total_identical_buildings": 1,
    "unit_types": [],
    "total_units_per_building": 0,
    "floors_per_building": 1,
    "has_garage": false,
    "garage_floor_area_sqft": 0,
    "has_pool": false,
    "ceiling_height_ft": 0
  },
  "notes": []
}

If no Room Finish Schedule is found, return {"room_finish_schedule": [], "structural_finish_scope": [], "building_info": {}, "notes": ["No Room Finish Schedule found"]}.
Be precise — extract every room listed in the schedule, and capture every structural-surface finish callout."""

    try:
        result_parts = []
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            temperature=0,
            timeout=300.0,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_data
                        }
                    },
                    {
                        "type": "text",
                        "text": room_finish_prompt
                    }
                ]
            }]
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)

        result_text = "".join(result_parts)
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            rfs_data = json.loads(json_match.group())
            rooms = rfs_data.get("room_finish_schedule", [])
            bi = rfs_data.get("building_info", {})
            struct_scope = rfs_data.get("structural_finish_scope", []) or []
            if rooms:
                print(f"   ✅ Room Finish Schedule extracted: {len(rooms)} room types")
                n_buildings = bi.get("total_identical_buildings", 1)
                unit_types = bi.get("unit_types", [])
                if unit_types:
                    ut_str = ", ".join(f"{ut.get('type', '?')}: {ut.get('count_per_building', '?')}/bldg"
                                       for ut in unit_types)
                    print(f"      Unit types: {ut_str}")
                if n_buildings > 1:
                    print(f"      Identical buildings: {n_buildings}")
                if struct_scope:
                    print(f"      Structural finish callouts: {len(struct_scope)}")
                return rfs_data
            elif struct_scope:
                # No room rows but finish-schedule did contain structural-surface callouts
                # (common on retail/big-box where the schedule is just a legend +
                # paint-to-deck note). Preserve so downstream can still apply scope.
                print(f"   ⚠️  No room rows, but {len(struct_scope)} structural finish callout(s) captured")
                return rfs_data
            else:
                print(f"   ⚠️  No Room Finish Schedule found in this PDF")
                return None
        else:
            print(f"   ⚠️  Could not parse Room Finish Schedule response")
            return None

    except Exception as e:
        print(f"   ❌ Error extracting Room Finish Schedule: {e}")
        return None


# ---------------------------------------------------------------------------
# Exterior Scope Extraction — Dedicated pass on elevation sheets
# ---------------------------------------------------------------------------

def _identify_elevation_pages(pdf_path):
    """Return 0-based page indices that look like exterior-elevation sheets.

    Heuristics:
      1. Sheet number with prefix 'A' and number in 200-399 range (typical
         elevation/section sheet block). Examples: A-201, A-205, A2.10.
      2. Full-text contains an elevation cue: "elevation", "north elevation",
         "south elevation", "east elevation", "west elevation",
         "exterior elevation", "front elevation", "rear elevation".

    Designed to be cheap (PyMuPDF only, no API calls).
    """
    try:
        import fitz
    except ImportError:
        return []

    elevation_indices = []
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    elevation_cues = (
        "exterior elevation", "exterior elevations",
        "north elevation", "south elevation",
        "east elevation", "west elevation",
        "front elevation", "rear elevation", "side elevation",
        "building elevation", "building elevations",
    )

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        full_text = page.get_text() or ""
        lower = full_text.lower()

        # Cue-based match (most reliable)
        if any(cue in lower for cue in elevation_cues):
            elevation_indices.append(page_idx)
            continue

        # Sheet-number-based match: A-2xx or A-3xx range
        for m in _SHEET_NUMBER_RE.finditer(full_text):
            prefix = m.group(1).upper()
            number = m.group(2)
            if prefix == 'A':
                # Strip decimal portion (A2.10 → "2"); take leading digits
                base = number.split('.')[0]
                try:
                    n = int(base)
                except ValueError:
                    continue
                # A-200..A-399 typically holds exterior elevations & sections
                if (200 <= n < 400) or (n in (2, 3) and '.' in number):
                    # A2.x and A3.x conventions also map here
                    if "elevation" in lower or "section" not in lower:
                        elevation_indices.append(page_idx)
                        break

    doc.close()
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for idx in elevation_indices:
        if idx not in seen:
            seen.add(idx)
            unique.append(idx)
    return unique


def _extract_exterior_scope(client, pdf_path):
    """Extract exterior painting scope from elevation sheets only.

    Mirrors _extract_room_finish_schedule() but focuses on building elevations
    (A-200/A-300 series). Sends a filtered PDF containing only elevation pages
    to keep the LLM's attention on exterior scope.

    Returns dict with shape compatible with the analysis 'exterior' section,
    or None if no elevation pages were found / call failed.
    """
    print(f"\n🏛  Extracting exterior scope: {os.path.basename(pdf_path)}")

    elevation_indices = _identify_elevation_pages(pdf_path)
    if not elevation_indices:
        print(f"   ⚠️  No elevation pages identified — skipping exterior pass")
        return None
    print(f"   📄 Elevation pages: {[i + 1 for i in elevation_indices]}")

    # Build a filtered PDF containing only elevation pages so the LLM is
    # not distracted by floor plans / schedules.
    try:
        filtered_bytes = _create_filtered_pdf(pdf_path, elevation_indices)
    except Exception as e:
        print(f"   ❌ Could not build filtered elevation PDF: {e}")
        return None

    # Size guard: Claude's per-document base64 limit ~5 MB.
    if len(filtered_bytes) > 5 * 1024 * 1024:
        print(f"   ⚠️  Filtered elevation PDF is "
              f"{len(filtered_bytes)/1024/1024:.1f} MB — truncating to first "
              f"few elevation pages")
        # Truncate to first 4 pages (typically the 4 cardinal elevations)
        elevation_indices = elevation_indices[:4]
        try:
            filtered_bytes = _create_filtered_pdf(pdf_path, elevation_indices)
        except Exception as e:
            print(f"   ❌ Could not build truncated elevation PDF: {e}")
            return None

    pdf_b64 = base64.standard_b64encode(filtered_bytes).decode("utf-8")

    exterior_prompt = """You are analyzing BUILDING ELEVATION DRAWINGS for exterior painting scope.

Your task: Extract every painted exterior surface visible in these elevations.

WHAT TO MEASURE:
1. PAINT SURFACES (record sqft):
   - Painted CMU / masonry / EIFS / stucco / metal panel / precast walls
   - Painted fascia, soffit, canopy, sign band, parapet cap
   - Painted exterior trim and corner boards
   Estimate AREA = width × height, subtracting glazing/storefront/doors.

2. EXTERIOR DOORS (count):
   - Service doors, rear doors, mechanical doors, hollow-metal doors that
     are scheduled or noted as "PAINT" or "PT" or have a painted finish.
   - Storefront/glazed doors are NOT included unless explicitly painted.

3. LINEAR ITEMS (record LF):
   - Cornice / parapet cap LF (perimeter of the painted top edge)
   - Painted exterior window trim LF (head + sill + 2 jambs per window)
   - Painted bollards (count, not LF)

4. SIDING TYPE:
   - exterior_siding_type = primary cladding name: "hardie", "stucco",
     "eifs", "cmu", "masonry", "metal panel", "precast", "vinyl", "wood".
   - If material-specific (hardie, EIFS), set hardie_siding_sqft separately
     and DO NOT also include in exterior_paint_sqft.

5. LIFT REQUIRED:
   - Set lift_required = true if the building is 3+ stories OR if any painted
     surface is above ~14 ft (typical extension-ladder reach).

CRITICAL — RETAIL / TENANT-FIT-OUT GOTCHAS:
   - Many retail boxes (e.g. Barnes & Noble, target tenant fit-outs) have
     SMALL exterior scope: just rear doors, fascia/sign band touch-up, and
     bollards. DO NOT default to 0 — capture every painted item, however small.
   - If the elevations show storefront glazing on the front and CMU/painted
     metal on rear/sides, count ONLY the rear and side painted areas.

Return ONLY this JSON (one object, no commentary):
{
  "exterior_paint_sqft": 0,
  "hardie_siding_sqft": 0,
  "cornice_lf": 0,
  "window_trim_lf": 0,
  "soffit_sqft": 0,
  "railing_lf": 0,
  "azek_trim_lf": 0,
  "corner_board_lf": 0,
  "steel_lintel_lf": 0,
  "exterior_door_count": 0,
  "bollard_count": 0,
  "exterior_siding_type": "",
  "lift_required": false,
  "notes": "<1-2 sentences describing what was painted and why your numbers reflect that>",
  "source_sheets": ["A-201", "A-202"]
}

If the elevations show no painted exterior surfaces, return all zeros with a
note explaining why (e.g. "Storefront glazing on all four sides; no painted
exterior scope visible.")."""

    try:
        result_parts = []
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            temperature=0,
            timeout=300.0,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": exterior_prompt,
                    },
                ],
            }],
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)

        result_text = "".join(result_parts)
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if not json_match:
            print(f"   ⚠️  Could not parse exterior scope response")
            return None

        ext_data = json.loads(json_match.group())
        ext_data["source_pages"] = [i + 1 for i in elevation_indices]

        sqft = _num(ext_data.get("exterior_paint_sqft", 0))
        hardie = _num(ext_data.get("hardie_siding_sqft", 0))
        cornice = _num(ext_data.get("cornice_lf", 0))
        doors = _num(ext_data.get("exterior_door_count", 0))
        print(f"   ✅ Exterior scope extracted: "
              f"{sqft:,.0f} sqft paint, {hardie:,.0f} sqft hardie, "
              f"{cornice:,.0f} LF cornice, {doors:.0f} ext doors")
        return ext_data

    except Exception as e:
        print(f"   ❌ Error extracting exterior scope: {e}")
        return None


def _maybe_run_exterior_pass(client, pdf_path, analysis_result):
    """If `analysis_result` is missing exterior scope on a commercial job,
    run a dedicated elevation-only extraction and merge the results.

    Idempotent and side-effect-free if the analysis already has exterior
    sqft or the building isn't commercial. Mutates `analysis_result`.
    Emits an RFI note when no elevation pages are found OR the dedicated
    pass also returns 0 sqft on a commercial job.
    """
    if not isinstance(analysis_result, dict):
        return

    pi = analysis_result.get("project_info", {}) or {}
    bt = str(pi.get("building_type", "")).lower()
    is_commercial = any(
        kw in bt for kw in (
            "commercial", "auto", "industrial", "warehouse",
            "retail", "dealership"
        )
    )
    if not is_commercial:
        return

    exterior = analysis_result.get("exterior", {}) or {}
    if _num(exterior.get("exterior_paint_sqft", 0)) > 0:
        return  # Already have exterior scope — don't burn an extra API call

    print(f"   🏛  Commercial job with 0 exterior sqft — running dedicated "
          f"elevation pass...")
    time.sleep(10)
    ext_data = _extract_exterior_scope(client, pdf_path)
    if ext_data:
        # Merge non-zero numeric fields into the existing exterior dict
        # (preserve any prior values; only fill gaps).
        merged = dict(exterior)
        for key in (
            "exterior_paint_sqft", "hardie_siding_sqft", "cornice_lf",
            "window_trim_lf", "soffit_sqft", "railing_lf", "azek_trim_lf",
            "corner_board_lf", "steel_lintel_lf",
        ):
            if _num(merged.get(key, 0)) == 0 and _num(ext_data.get(key, 0)) > 0:
                merged[key] = ext_data[key]
        if not merged.get("exterior_siding_type") and ext_data.get("exterior_siding_type"):
            merged["exterior_siding_type"] = ext_data["exterior_siding_type"]
        if ext_data.get("lift_required") and not merged.get("lift_required"):
            merged["lift_required"] = True
        # Append-only notes
        ext_note = ext_data.get("notes", "")
        if ext_note:
            existing = merged.get("notes", "")
            merged["notes"] = (existing + " | " if existing else "") + str(ext_note)
        merged["source_pages"] = ext_data.get("source_pages", [])
        analysis_result["exterior"] = merged

        # If pass returned all zeros on a commercial job, emit an RFI rather
        # than silently dropping exterior scope.
        if _num(merged.get("exterior_paint_sqft", 0)) == 0 \
                and _num(merged.get("hardie_siding_sqft", 0)) == 0 \
                and _num(merged.get("cornice_lf", 0)) == 0:
            analysis_result.setdefault("notes", []).append(
                "[RFI: Exterior Scope] Commercial building with 0 sqft "
                "exterior paint extracted from elevation pages "
                f"({merged.get('source_pages', [])}). Confirm with owner "
                "whether any exterior painting is required (storefront, "
                "rear/service doors, fascia, soffit, bollards, sign band, "
                "exposed CMU). Do not assume zero without confirmation."
            )
    else:
        # No elevation pages found at all — most likely the PDF is a
        # tenant-fit-out / interior-only set, but flag it as an RFI so
        # Rider can confirm.
        analysis_result.setdefault("notes", []).append(
            "[RFI: Exterior Scope] No exterior elevation sheets identified "
            "in this PDF, and 0 sqft exterior paint extracted from the "
            "main pass. Confirm with owner whether exterior painting is "
            "in scope; if so, request elevation drawings."
        )


def _maybe_run_schedule_recovery_pass(client, pdf_path, analysis_result):
    """When the unified extraction found floor plans but missed the
    finish-schedule's structural-surface callouts (paint exposed deck /
    structure / MEP — typically the largest line item on retail / big-box
    jobs), run a dedicated schedule pass to recover them.

    Trigger conditions (ALL must hold):
      - building_type is commercial / retail / industrial / warehouse
      - footprint_sqft > 1000 (real building, not a synthetic test)
      - aggregated_totals.total_dryfall_ceiling_sqft < footprint × 0.5
        (the actual recovered dryfall is suspiciously low relative to
        what the building's footprint implies for an open-ceiling space)
      - structural_finish_scope is not already present (idempotency)

    This complements the existing "no floor plans found" branch, which
    only fires for schedule-only PDFs. Here we fire AFTER the LLM has
    already produced rooms, to fill in scope it missed.

    Mutates analysis_result in place.
    """
    if not isinstance(analysis_result, dict):
        return

    pi = analysis_result.get("project_info", {}) or {}
    bt = str(pi.get("building_type", "")).lower()
    is_commercial = any(
        kw in bt for kw in (
            "commercial", "auto", "industrial", "warehouse",
            "retail", "dealership"
        )
    )
    if not is_commercial:
        return

    footprint = _num(pi.get("footprint_sqft", 0))
    if footprint <= 1000:
        return

    if analysis_result.get("structural_finish_scope"):
        return  # already captured (e.g. by the no-plans branch)

    agg = analysis_result.get("aggregated_totals", {}) or {}
    dryfall = _num(agg.get("total_dryfall_ceiling_sqft", 0))
    if dryfall >= footprint * 0.5:
        return  # dryfall already substantial — no recovery needed

    print(f"   📋 Commercial job with low dryfall ({dryfall:,.0f} sqft "
          f"vs {footprint:,.0f} sqft footprint) — running schedule "
          f"recovery pass...")
    time.sleep(10)
    rfs_data = _extract_room_finish_schedule(client, pdf_path)
    if not rfs_data:
        # No schedule found — let the existing manual-review flag carry the
        # signal. Don't synthesize anything.
        return

    struct_scope = rfs_data.get("structural_finish_scope") or []
    if not struct_scope:
        print(f"   📋 Schedule recovery pass found no structural-finish callouts")
        return

    analysis_result["structural_finish_scope"] = struct_scope
    print(f"   📋 Schedule recovery: captured {len(struct_scope)} structural-"
          f"finish callout(s); dryfall safety net will pick up on next pass")

    # Apply the dryfall scope-recovery directly here so it lands in
    # aggregated_totals before pricing runs. We don't rely on
    # _recalculate_totals re-firing because by this point the per-file
    # totals are already aggregated and downstream merge logic doesn't
    # know to re-trigger the safety net.
    #
    # HARD_NUMBERS_ONLY: footprint x 0.75 is an assumption, not a
    # measurement. This block was an ungated COPY of the dryfall safety
    # net in _recalculate_totals (which has always been gated) — the
    # callout evidence is real, but the quantity is invented. Under the
    # policy we capture the callouts (above) and flag the unpriced
    # exposure; the estimator confirms the area from the RCP instead of
    # the bid silently carrying ~$15k of assumed scope.
    est_dryfall = round(footprint * 0.75)
    gap = est_dryfall - dryfall
    if gap > 0 and HARD_NUMBERS_ONLY:
        analysis_result.setdefault("notes", []).append(
            f"[Dryfall Recovery Pass] Finish schedule contains "
            f"{len(struct_scope)} structural-finish callout(s) "
            f"(deck/structure/MEP paint-to-deck) but only {dryfall:,.0f} sqft "
            f"of dryfall was extracted from the drawings. RFI REQUIRED: "
            f"confirm exposed-deck area (footprint-based estimate would be "
            f"~{est_dryfall:,.0f} sqft — NOT priced under hard-numbers policy)."
        )
        print(f"   🔒 Dryfall recovery: callouts captured, +{gap:,.0f} sqft "
              f"NOT applied (HARD_NUMBERS_ONLY) — flagged for RFI")
    elif gap > 0:
        agg["total_dryfall_ceiling_sqft"] = dryfall + gap
        analysis_result["aggregated_totals"] = agg
        analysis_result.setdefault("notes", []).append(
            f"[Dryfall Recovery Pass] Added {gap:,.0f} sqft of dryfall scope "
            f"based on {len(struct_scope)} structural-finish callout(s) in the "
            f"finish schedule (deck/structure/MEP paint-to-deck). Total dryfall "
            f"now {dryfall + gap:,.0f} sqft (75% of {footprint:,.0f} sqft footprint). "
            f"Original LLM extraction had only {dryfall:,.0f} sqft tagged as EXPOSED."
        )
        print(f"   🔧 Dryfall recovery pass: +{gap:,.0f} sqft "
              f"(now {dryfall + gap:,.0f} sqft from {footprint:,.0f} sqft "
              f"footprint × 0.75)")


# ---------------------------------------------------------------------------
# Building Inventory Extraction — Lightweight Claude call on index pages
# ---------------------------------------------------------------------------

def _extract_building_inventory(client, pdf_path, index_page_indices, index_text=""):
    """
    Send ONLY the index/TOC pages of a PDF to Claude to extract a structured
    building inventory — how many buildings of each type exist in the project.

    This is a lightweight, focused call (~$0.01-0.05) that reads drawing
    indices, cover sheets, and unit-mix tables to determine the project's
    building breakdown.

    Args:
        client: Anthropic client instance
        pdf_path: Path to the full PDF
        index_page_indices: List of 0-based page indices for index/TOC pages
        index_text: Pre-extracted text from index pages (PyMuPDF). Provides
                    exact building count strings that may be unreadable in images.

    Returns:
        dict with building inventory, or None if extraction fails:
        {
            "buildings": [
                {"building_name": "Main Building", "building_type_code": "MAIN", "count": 1},
                {"building_name": "Villa Duplex Type C1", "building_type_code": "C1", "count": 8}
            ],
            "total_buildings": 17,
            "project_name": "...",
            "source_pdf": "filename.pdf",
            "source_pages": [0, 1, 2]
        }
    """
    print(f"\n🏗️  Extracting Building Inventory from index pages: {os.path.basename(pdf_path)}")
    print(f"   📄 Scanning pages: {[p + 1 for p in index_page_indices]}")

    # Create filtered PDF with only index pages
    # Try PDF first; if Claude can't process it, fall back to rendered images
    use_images = False
    try:
        filtered_pdf_bytes = _create_filtered_pdf(pdf_path, index_page_indices)
        # Check if the filtered PDF is too large (>5MB can cause issues)
        if len(filtered_pdf_bytes) > 5 * 1024 * 1024:
            print(f"   ⚠️  Filtered PDF is {len(filtered_pdf_bytes)/1024/1024:.1f} MB — "
                  f"using image fallback")
            use_images = True
        else:
            pdf_b64 = base64.standard_b64encode(filtered_pdf_bytes).decode("utf-8")
    except Exception as e:
        print(f"   ⚠️  Failed to create filtered PDF ({e}) — using image fallback")
        use_images = True

    # Image fallback: render index pages as JPEG images.
    # Use low DPI (72) since index pages are mostly text/tables, not detailed drawings.
    # Also limit to max 4 pages per call to avoid 413 Payload Too Large errors.
    # Individual images MUST be under 5MB (Claude's per-image limit).
    # Use config constants for DPI/quality (higher = more readable text in tables)
    try:
        from config import INVENTORY_IMAGE_DPI as _inv_dpi
    except ImportError:
        _inv_dpi = 150
    try:
        from config import INVENTORY_IMAGE_QUALITY as _inv_quality
    except ImportError:
        _inv_quality = 80
    MAX_INVENTORY_PAGES = 4
    MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4MB cap (base64 adds ~33%, must stay under 5MB)
    content_blocks = []

    # Step 3: Inject pre-extracted text as the FIRST content block.
    # This gives Claude exact building count strings (e.g., "4 BLDGS") that
    # may be unreadable in low-resolution images. Zero additional API cost.
    if index_text and index_text.strip():
        content_blocks.append({
            "type": "text",
            "text": (
                "PRE-EXTRACTED TEXT FROM INDEX PAGES (use as PRIMARY source "
                "for building counts — the images below may be low-resolution "
                "and harder to read):\n\n"
                + index_text.strip()
            )
        })
    if use_images:
        try:
            import fitz as _fitz_inv
            from io import BytesIO
            try:
                from PIL import Image as _PILImage
            except ImportError:
                _PILImage = None

            # Limit pages to prevent oversized payloads
            pages_to_render = index_page_indices[:MAX_INVENTORY_PAGES]
            if len(index_page_indices) > MAX_INVENTORY_PAGES:
                print(f"   📄 Limiting to {MAX_INVENTORY_PAGES} of "
                      f"{len(index_page_indices)} index pages (payload size)")

            doc = _fitz_inv.open(pdf_path)
            zoom = _inv_dpi / 72
            matrix = _fitz_inv.Matrix(zoom, zoom)
            for page_num in pages_to_render:
                if page_num >= len(doc):
                    continue
                page = doc[page_num]
                pix = page.get_pixmap(matrix=matrix)
                # Convert to JPEG for smaller file size
                if _PILImage:
                    img = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    buf = BytesIO()
                    img.save(buf, format="JPEG", quality=_inv_quality)
                    img_bytes = buf.getvalue()
                    media_type = "image/jpeg"
                    # If still too large, reduce quality further
                    if len(img_bytes) > MAX_IMAGE_BYTES:
                        buf = BytesIO()
                        img.save(buf, format="JPEG", quality=30)
                        img_bytes = buf.getvalue()
                else:
                    img_bytes = pix.tobytes("png")
                    media_type = "image/png"
                b64_data = base64.standard_b64encode(img_bytes).decode("utf-8")
                print(f"      📸 Rendered page {page_num + 1} → "
                      f"{pix.width}×{pix.height} px ({len(img_bytes)/1024:.0f} KB)")
                # Skip images that are still too large (shouldn't happen with JPEG q30)
                if len(img_bytes) > MAX_IMAGE_BYTES:
                    print(f"      ⚠️  Page {page_num + 1} still too large "
                          f"({len(img_bytes)/1024/1024:.1f} MB), skipping")
                    continue
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64_data,
                    }
                })
            doc.close()
        except Exception as e:
            print(f"   ❌ Image rendering failed: {e}")
            return None
    else:
        content_blocks.append({
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_b64,
            }
        })

    inventory_prompt = """You are analyzing INDEX PAGES / TABLE OF CONTENTS / COVER SHEETS from a construction project's architectural drawings.

Your ONE task: Identify ALL BUILDINGS in this project and how many of each type exist.

Look for:
- Drawing indices listing sheets by building (e.g., "Building 1", "Bldg. A", "Villa Type C1")
- Unit mix tables showing how many of each unit type exist
- Cover sheets with project descriptions mentioning building counts
- Sheet numbering patterns that reveal building types (e.g., sheets for "C1-A", "C2-B")
- Notes like "16 duplex buildings", "8 identical villas", "Buildings 1 through 6"
- Phase descriptions that specify building quantities

CRITICAL RULES:
1. Count DISTINCT PHYSICAL BUILDINGS, not units within buildings.
   - "16 duplex buildings" = 16 buildings (each duplex is ONE building with 2 units)
   - "8 villa buildings, each with 2 units" = 8 buildings
   - "3 apartment buildings, 12 units each" = 3 buildings
2. If drawings are organized by BUILDING TYPE (e.g., "Type C1", "Type C2"), determine how many
   physical buildings match each type.
3. A "main building" or "community building" is typically 1 building unless stated otherwise.
4. If you see "Buildings 1-3" or "Bldg 5-7", that means 3 buildings in the range.
5. If a duplex has two sides (e.g., "C1-A" and "C1-B"), that's still ONE building —
   the A and B are the two UNITS within the single duplex building.

Return ONLY this JSON (no other text):
{
  "buildings": [
    {
      "building_name": "Main Community Building",
      "building_type_code": "MAIN",
      "count": 1,
      "units_per_building": 1,
      "notes": "4-story main building with IL and AL units"
    },
    {
      "building_name": "Villa Duplex Type C1",
      "building_type_code": "C1",
      "count": 8,
      "units_per_building": 2,
      "notes": "8 identical duplex buildings, each with C1-A and C1-B units"
    }
  ],
  "total_buildings": 9,
  "total_units": 0,
  "project_name": "Project Name from cover sheet",
  "notes": "Any relevant context about building count determination"
}

If you cannot determine building counts from these pages, return:
{
  "buildings": [],
  "total_buildings": 0,
  "total_units": 0,
  "project_name": "",
  "notes": "Could not determine building inventory from index pages"
}"""

    try:
        # Add the prompt as the final content block
        content_blocks.append({
            "type": "text",
            "text": inventory_prompt
        })

        result_parts = []
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            temperature=0,
            timeout=180.0,  # 3 min timeout (images may be large)
            messages=[{
                "role": "user",
                "content": content_blocks
            }]
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)

        result_text = "".join(result_parts)

        # Empty-response retry: a successful API call that returned no text
        # is almost always a transient model issue. Retry once before
        # falling back to image rendering. We raise with the marker string
        # the outer except already handles ("Could not process PDF").
        if not result_text:
            print(f"   ⚠️  Building inventory: empty API response — retrying once")
            time.sleep(10)
            try:
                result_parts = []
                with client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=2000,
                    temperature=0,
                    timeout=180.0,
                    messages=[{"role": "user", "content": content_blocks}]
                ) as stream:
                    for text in stream.text_stream:
                        result_parts.append(text)
                result_text = "".join(result_parts)
            except Exception as e:
                print(f"   ❌ Building inventory empty-response retry failed: {e}")
            if not result_text:
                # Trigger the image-fallback branch in the outer except.
                raise RuntimeError(
                    "Could not process PDF — empty inventory response after retry"
                )

        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)

        if json_match:
            inventory = json.loads(json_match.group())
            buildings = inventory.get("buildings", [])

            # Empty-buildings retry: Claude sometimes returns a parseable
            # JSON shell with no buildings on the first pass even when the
            # index pages clearly list buildings. Retry once with a
            # sharpened prompt before accepting the empty result.
            if not buildings:
                print(f"   ⚠️  Building inventory: empty buildings list — "
                      f"retrying once with sharpened prompt")
                time.sleep(10)
                sharpened_blocks = list(content_blocks[:-1]) + [{
                    "type": "text",
                    "text": (
                        inventory_prompt + "\n\n"
                        "RETRY DIRECTIVE: A prior attempt returned an empty "
                        "buildings list. The index pages above contain a "
                        "drawing index, sheet schedule, or building schedule. "
                        "Re-examine carefully: list EVERY distinct building or "
                        "structure referenced (main building, additions, "
                        "outbuildings, mechanical buildings, etc.). If the "
                        "project is a single tenant fit-out within an existing "
                        "structure, return one building with count=1 and the "
                        "tenant name. Do NOT return an empty array unless you "
                        "are certain there are zero identifiable buildings."
                    )
                }]
                try:
                    result_parts2 = []
                    with client.messages.stream(
                        model="claude-sonnet-4-6",
                        max_tokens=2000,
                        temperature=0,
                        timeout=180.0,
                        messages=[{"role": "user", "content": sharpened_blocks}]
                    ) as stream:
                        for text in stream.text_stream:
                            result_parts2.append(text)
                    result_text2 = "".join(result_parts2)
                    json_match2 = re.search(r'\{.*\}', result_text2, re.DOTALL)
                    if json_match2:
                        inventory2 = json.loads(json_match2.group())
                        buildings2 = inventory2.get("buildings", [])
                        if buildings2:
                            inventory = inventory2
                            buildings = buildings2
                            print(f"   🔬 Building inventory retry: "
                                  f"recovered {len(buildings)} building(s)")
                except Exception as e:
                    print(f"   ❌ Building inventory sharpened retry failed: {e}")

            if not buildings:
                print(f"   ⚠️  No buildings identified from index pages")
                return None

            # Add source metadata
            inventory["source_pdf"] = os.path.basename(pdf_path)
            inventory["source_pages"] = index_page_indices

            # Recalculate total_buildings from individual counts
            total = sum(b.get("count", 1) for b in buildings)
            inventory["total_buildings"] = total

            # Print summary
            print(f"   ✅ Building Inventory detected: {total} buildings total")
            for b in buildings:
                code = b.get("building_type_code", "?")
                count = b.get("count", 1)
                name = b.get("building_name", "Unknown")
                units = b.get("units_per_building", "?")
                print(f"      • {name} [{code}]: {count} building(s), {units} unit(s) each")

            _release_memory("after building inventory")
            return inventory
        else:
            print(f"   ⚠️  Could not parse building inventory response")
            _release_memory("after building inventory (no parse)")
            return None

    except Exception as e:
        error_msg = str(e)
        if "Could not process PDF" in error_msg and not use_images:
            # Retry with image fallback
            print(f"   ⚠️  PDF processing failed — retrying with image rendering...")
            try:
                pages_to_render = index_page_indices[:MAX_INVENTORY_PAGES]
                images = _render_pages_to_images(pdf_path, pages_to_render,
                                                  dpi=_inv_dpi)
                if not images:
                    print(f"   ❌ Image rendering failed — no images produced")
                    return None
                img_blocks = []
                for page_num, b64_data in images:
                    img_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64_data,
                        }
                    })
                img_blocks.append({"type": "text", "text": inventory_prompt})
                result_parts = []
                with client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=2000,
                    temperature=0,
                    timeout=180.0,
                    messages=[{"role": "user", "content": img_blocks}]
                ) as stream:
                    for text in stream.text_stream:
                        result_parts.append(text)
                result_text = "".join(result_parts)
                json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
                if json_match:
                    inventory = json.loads(json_match.group())
                    buildings = inventory.get("buildings", [])
                    if buildings:
                        inventory["source_pdf"] = os.path.basename(pdf_path)
                        inventory["source_pages"] = index_page_indices
                        total = sum(b.get("count", 1) for b in buildings)
                        inventory["total_buildings"] = total
                        print(f"   ✅ Building Inventory (image fallback): {total} buildings")
                        for b in buildings:
                            code = b.get("building_type_code", "?")
                            count = b.get("count", 1)
                            name = b.get("building_name", "Unknown")
                            units = b.get("units_per_building", "?")
                            print(f"      • {name} [{code}]: {count} building(s), "
                                  f"{units} unit(s) each")
                        return inventory
            except Exception as retry_e:
                print(f"   ❌ Image fallback also failed: {retry_e}")
                return None
        print(f"   ❌ Error extracting building inventory: {e}")
        return None


def _merge_building_inventories(inventories):
    """
    Merge building inventories from multiple PDFs into a single combined inventory.

    Different PDF volumes may contain different building types (e.g., villas in Vol II,
    carriage homes in Vol III). This function combines them all, deduplicating by
    building_type_code — if the same code appears in multiple PDFs, keeps the entry
    with the highest count.

    Args:
        inventories: List of inventory dicts, each with "buildings" list

    Returns:
        Merged inventory dict, or None if no buildings found
    """
    if not inventories:
        return None

    merged = {}  # type_code -> building entry (keep highest count per code)
    project_name = ""
    source_pdfs = []

    for inv in inventories:
        if not inv or not inv.get("buildings"):
            continue
        if not project_name and inv.get("project_name"):
            project_name = inv["project_name"]
        if inv.get("source_pdf"):
            source_pdfs.append(inv["source_pdf"])

        for b in inv["buildings"]:
            code = b.get("building_type_code", "").upper().strip()
            if not code:
                code = b.get("building_name", "UNKNOWN").upper().replace(" ", "_")[:10]
            existing = merged.get(code)
            if not existing or b.get("count", 1) > existing.get("count", 1):
                merged[code] = dict(b)
                merged[code]["building_type_code"] = code  # normalize

    if not merged:
        return None

    buildings = list(merged.values())
    total = sum(b.get("count", 1) for b in buildings)
    total_units = sum(
        _num(b.get("count", 1)) * _num(b.get("units_per_building", 1))
        for b in buildings
    )

    result = {
        "buildings": buildings,
        "total_buildings": total,
        "total_units": total_units,
        "project_name": project_name,
        "source_pdfs": source_pdfs,
        "merged_from": len([i for i in inventories if i and i.get("buildings")]),
        "notes": f"Merged from {len(source_pdfs)} PDF volumes"
    }

    print(f"\n🏗️  Merged Building Inventory: {total} buildings total "
          f"(from {len(source_pdfs)} PDFs)")
    for b in buildings:
        code = b.get("building_type_code", "?")
        count = b.get("count", 1)
        name = b.get("building_name", "Unknown")
        units = b.get("units_per_building", "?")
        print(f"   • {name} [{code}]: {count} building(s), {units} unit(s) each")

    return result


# ---------------------------------------------------------------------------
# Phase 1 — Project Overview Extraction (G-series / coversheet pages)
# ---------------------------------------------------------------------------

def _extract_project_overview(client, pdf_paths, classifications_by_pdf=None):
    """
    Phase 1 of the Rider workflow: read General Notes / Coversheet (G/T-series)
    pages FIRST to establish project scope and scale before any measurement work.

    Sends only G-series and Title pages — identified via _classify_pdf_pages() —
    to Claude with a focused prompt for project_name, scope_summary, total_gsf,
    building_count, stories, occupancy, and the scope-of-work narrative typically
    found on coversheets.

    Args:
        client: Anthropic client
        pdf_paths: list of PDF file paths (multi-volume projects supported)
        classifications_by_pdf: optional dict {pdf_path: classifications}.
                                If None, classify on the fly.

    Returns:
        dict (or None on failure):
        {
            "project_name": str,
            "scope_summary": str,         # 1-3 sentence narrative
            "scope_of_work": str,         # raw scope notes from G-pages
            "total_gsf": int or None,
            "building_count": int or None,
            "stories": int or None,       # typical stories per building
            "occupancy_type": str,        # e.g. "R-2", "B", "A-3"
            "unit_count": int or None,
            "source_pdfs": [str],
            "source_pages": {pdf_path: [page_indices]},
        }
    """
    print(f"\n📘 Phase 1: Extracting Project Overview from G-series / coversheets...")

    # 1. Identify G/T-series pages across all PDFs (cap to first 3 per PDF)
    page_picks = {}  # pdf_path -> [page_indices]
    for pdf_path in pdf_paths:
        cls = (classifications_by_pdf or {}).get(pdf_path)
        if cls is None:
            try:
                cls = _classify_pdf_pages(pdf_path)
            except Exception:
                cls = []
        g_pages = []
        for entry in cls:
            disc = entry.get("discipline", "")
            if disc in ("General", "Title") and entry.get("include"):
                g_pages.append(entry["page_index"])
            if len(g_pages) >= 3:
                break
        if g_pages:
            page_picks[pdf_path] = g_pages

    if not page_picks:
        print(f"   ⚠️  No G-series / Title pages identified — skipping Phase 1")
        return None

    # 2. Pre-extract text from those pages (PyMuPDF, zero API cost) and render images
    try:
        import fitz
    except ImportError:
        fitz = None

    pretext_parts = []
    image_blocks = []
    sources_used = []

    MAX_PAGES_TOTAL = 6  # hard cap across all PDFs to keep payload < 5MB
    pages_added = 0

    try:
        from config import INVENTORY_IMAGE_DPI as _po_dpi
    except ImportError:
        _po_dpi = 150
    try:
        from config import INVENTORY_IMAGE_QUALITY as _po_quality
    except ImportError:
        _po_quality = 75
    MAX_IMAGE_BYTES = 4 * 1024 * 1024

    try:
        from PIL import Image as _PILImage
    except ImportError:
        _PILImage = None
    from io import BytesIO

    for pdf_path, page_indices in page_picks.items():
        if pages_added >= MAX_PAGES_TOTAL:
            break
        sources_used.append(os.path.basename(pdf_path))
        if fitz is None:
            continue
        try:
            doc = fitz.open(pdf_path)
        except Exception:
            continue
        zoom = _po_dpi / 72
        matrix = fitz.Matrix(zoom, zoom)
        for pidx in page_indices:
            if pages_added >= MAX_PAGES_TOTAL:
                break
            if pidx >= len(doc):
                continue
            page = doc[pidx]
            text = page.get_text().strip()
            if text:
                pretext_parts.append(
                    f"--- {os.path.basename(pdf_path)} page {pidx + 1} ---\n{text}"
                )
            try:
                pix = page.get_pixmap(matrix=matrix)
                if _PILImage:
                    img = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    buf = BytesIO()
                    img.save(buf, format="JPEG", quality=_po_quality)
                    img_bytes = buf.getvalue()
                    if len(img_bytes) > MAX_IMAGE_BYTES:
                        buf = BytesIO()
                        img.save(buf, format="JPEG", quality=40)
                        img_bytes = buf.getvalue()
                    media_type = "image/jpeg"
                else:
                    img_bytes = pix.tobytes("png")
                    media_type = "image/png"
                if len(img_bytes) > MAX_IMAGE_BYTES:
                    continue
                b64_data = base64.standard_b64encode(img_bytes).decode("utf-8")
                image_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64_data,
                    }
                })
                pages_added += 1
            except Exception as e:
                print(f"   ⚠️  Could not render page {pidx + 1} of "
                      f"{os.path.basename(pdf_path)}: {e}")
        doc.close()

    if not pretext_parts and not image_blocks:
        print(f"   ⚠️  Could not gather any text or images from G-pages")
        return None

    print(f"   📄 Sending {pages_added} G-page image(s) + "
          f"{len(pretext_parts)} text block(s) to Claude")

    # 3. Build the focused overview prompt
    overview_prompt = """You are reading the GENERAL NOTES / COVERSHEET / TITLE pages
(G-series and T-series) of a construction project. These pages establish project
scope and scale — the bulk of context that drives measurement decisions later.

Your ONLY task on this pass is to extract the project's SCOPE AND SCALE — not
room measurements. Look for:
- Project name (cover sheet, title block)
- Scope of work narrative (general notes, project description)
- Total Gross Square Footage (GSF) — often labeled "TOTAL BUILDING AREA",
  "PROJECT AREA", or in a code summary table
- Number of buildings (if multi-building)
- Number of stories / floors per building
- Occupancy classification (e.g., "R-2", "B", "A-3", "I-2")
- Unit count (for multifamily / senior living)
- Construction type (Type I-A, Type V-B, etc.) — useful context, not required

Use the PRE-EXTRACTED TEXT as the PRIMARY source — exact figures from text
extraction are more reliable than what you read from the images.

Return ONLY this JSON, no other commentary:
{
  "project_name": "...",
  "scope_summary": "1-3 sentence summary of the painting-relevant scope",
  "scope_of_work": "Direct quote or paraphrase of the scope-of-work general note",
  "total_gsf": 0,
  "building_count": 0,
  "stories": 0,
  "occupancy_type": "",
  "unit_count": 0,
  "construction_type": "",
  "notes": "Anything notable — phasing, exclusions, owner-furnished items, etc."
}

If a field is not stated on these pages, use 0 for numbers and "" for strings."""

    content_blocks = []
    if pretext_parts:
        content_blocks.append({
            "type": "text",
            "text": (
                "PRE-EXTRACTED TEXT FROM G-SERIES / TITLE PAGES (use as PRIMARY "
                "source — the images below may be lower resolution):\n\n"
                + "\n\n".join(pretext_parts)
            )
        })
    content_blocks.extend(image_blocks)
    content_blocks.append({"type": "text", "text": overview_prompt})

    try:
        result_parts = []
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            temperature=0,
            timeout=180.0,
            messages=[{"role": "user", "content": content_blocks}]
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)
        result_text = "".join(result_parts)
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if not json_match:
            print(f"   ⚠️  Could not parse project overview JSON")
            return None
        overview = json.loads(json_match.group())
        overview["source_pdfs"] = sources_used
        overview["source_pages"] = {
            os.path.basename(p): [i + 1 for i in idxs]
            for p, idxs in page_picks.items()
        }

        # Renovation-mode detection: scan the parsed scope text for verbs that
        # indicate this is a renovation / tenant fit-out / demo job. When set,
        # downstream architectural extraction must restrict measurement to the
        # PROPOSED viewport on any sheet that shows EXISTING + PROPOSED plans
        # side by side (see Dobbin Rd 2026-02 incident — pipeline measured the
        # EXISTING viewport and returned ~30% short on wall area).
        _RENO_VERBS = (
            "demolition", "demolish", "demo ",
            "proposed floor plan", "proposed plan",
            "reconfigure", "renovation", "remodel",
            "tenant fit-out", "tenant fit out", "tenant improvement",
            "existing partition", "new partition",
            "as-built", "new work plan",
        )
        scope_blob = " ".join([
            str(overview.get("scope_of_work") or ""),
            str(overview.get("scope_summary") or ""),
            str(overview.get("notes") or ""),
        ]).lower()
        overview["is_renovation"] = any(v in scope_blob for v in _RENO_VERBS)
        overview["prefer_plan_state"] = "proposed" if overview["is_renovation"] else None

        # Print summary
        print(f"   ✅ Project Overview extracted:")
        if overview.get("is_renovation"):
            print(f"      • 🔧 Renovation scope detected — prefer_plan_state='proposed' "
                  f"(viewport selector will restrict to PROPOSED on multi-plan sheets)")
        if overview.get("project_name"):
            print(f"      • Project: {overview['project_name']}")
        if overview.get("scope_summary"):
            print(f"      • Scope: {overview['scope_summary'][:120]}")
        if overview.get("total_gsf"):
            print(f"      • Total GSF: {overview['total_gsf']:,}")
        if overview.get("building_count"):
            print(f"      • Buildings: {overview['building_count']}")
        if overview.get("stories"):
            print(f"      • Stories: {overview['stories']}")
        if overview.get("occupancy_type"):
            print(f"      • Occupancy: {overview['occupancy_type']}")
        if overview.get("unit_count"):
            print(f"      • Units: {overview['unit_count']}")
        _release_memory("after project overview")
        return overview
    except Exception as e:
        print(f"   ❌ Project overview extraction failed: {e}")
        _release_memory("after project overview (failure)")
        return None


def _estimate_from_room_finish_schedule(room_schedule_data, schedule_data=None):
    """
    Convert Room Finish Schedule entries into synthetic room data with estimated
    dimensions, producing standard room JSON that feeds into the existing
    aggregation pipeline via _recalculate_totals().

    Args:
        room_schedule_data: dict from _extract_room_finish_schedule()
        schedule_data: dict from analyze_schedule_pdf() (doors/windows/stairs)

    Returns:
        list of floor dicts matching the standard extraction schema, or None.
    """
    try:
        from config import SCHEDULE_ESTIMATION_CONFIDENCE, ENABLE_BUILDING_MULTIPLIER
    except ImportError:
        SCHEDULE_ESTIMATION_CONFIDENCE = 0.85
        ENABLE_BUILDING_MULTIPLIER = True

    rooms_data = room_schedule_data.get("room_finish_schedule", [])
    bi = room_schedule_data.get("building_info", {})

    if not rooms_data:
        return None

    # Room type dimension estimates (length, width in feet, ceiling height in feet)
    # These are conservative residential estimates based on typical construction
    ROOM_TYPE_ESTIMATES = {
        "living":       {"length": 18, "width": 14, "ceiling_height": 9.5},
        "living room":  {"length": 18, "width": 14, "ceiling_height": 9.5},
        "family":       {"length": 16, "width": 14, "ceiling_height": 9.5},
        "family room":  {"length": 16, "width": 14, "ceiling_height": 9.5},
        "great room":   {"length": 20, "width": 16, "ceiling_height": 10},
        "bedroom":      {"length": 14, "width": 12, "ceiling_height": 9.5},
        "master":       {"length": 16, "width": 14, "ceiling_height": 9.5},
        "master bedroom": {"length": 16, "width": 14, "ceiling_height": 9.5},
        "kitchen":      {"length": 14, "width": 10, "ceiling_height": 9.5},
        "bathroom":     {"length": 8,  "width": 6,  "ceiling_height": 9.5},
        "bath":         {"length": 8,  "width": 6,  "ceiling_height": 9.5},
        "powder":       {"length": 5,  "width": 4,  "ceiling_height": 9.5},
        "powder room":  {"length": 5,  "width": 4,  "ceiling_height": 9.5},
        "hallway":      {"length": 20, "width": 4,  "ceiling_height": 9.5},
        "hall":         {"length": 20, "width": 4,  "ceiling_height": 9.5},
        "foyer":        {"length": 8,  "width": 6,  "ceiling_height": 9.5},
        "entry":        {"length": 8,  "width": 6,  "ceiling_height": 9.5},
        "closet":       {"length": 6,  "width": 4,  "ceiling_height": 9.5},
        "walk-in closet": {"length": 8, "width": 6, "ceiling_height": 9.5},
        "w.i.c.":       {"length": 8,  "width": 6,  "ceiling_height": 9.5},
        "wic":          {"length": 8,  "width": 6,  "ceiling_height": 9.5},
        "laundry":      {"length": 8,  "width": 6,  "ceiling_height": 9.5},
        "utility":      {"length": 8,  "width": 6,  "ceiling_height": 9.5},
        "mechanical":   {"length": 10, "width": 8,  "ceiling_height": 9.5},
        "dining":       {"length": 14, "width": 12, "ceiling_height": 9.5},
        "dining room":  {"length": 14, "width": 12, "ceiling_height": 9.5},
        "den":          {"length": 12, "width": 10, "ceiling_height": 9.5},
        "study":        {"length": 12, "width": 10, "ceiling_height": 9.5},
        "office":       {"length": 12, "width": 10, "ceiling_height": 9.5},
        "garage":       {"length": 40, "width": 20, "ceiling_height": 10},
        "parking":      {"length": 40, "width": 20, "ceiling_height": 10},
        "stairwell":    {"length": 12, "width": 8,  "ceiling_height": 18},
        "stair":        {"length": 12, "width": 8,  "ceiling_height": 18},
        "corridor":     {"length": 60, "width": 6,  "ceiling_height": 9.5},
        "lobby":        {"length": 20, "width": 15, "ceiling_height": 10},
        "vestibule":    {"length": 8,  "width": 6,  "ceiling_height": 10},
        "storage":      {"length": 10, "width": 8,  "ceiling_height": 9.5},
        "trash":        {"length": 8,  "width": 6,  "ceiling_height": 9.5},
        "mail":         {"length": 8,  "width": 6,  "ceiling_height": 9.5},
        "pool":         {"length": 40, "width": 30, "ceiling_height": 14},
        "fitness":      {"length": 30, "width": 20, "ceiling_height": 10},
        "gym":          {"length": 30, "width": 20, "ceiling_height": 10},
        "lounge":       {"length": 20, "width": 15, "ceiling_height": 10},
        "clubhouse":    {"length": 30, "width": 20, "ceiling_height": 10},
        "default":      {"length": 12, "width": 10, "ceiling_height": 9.5},
    }

    def _match_room_type(room_name):
        """Match a room name to the best dimension estimate."""
        name_lower = room_name.lower().strip()
        # Direct match first
        if name_lower in ROOM_TYPE_ESTIMATES:
            return ROOM_TYPE_ESTIMATES[name_lower]
        # Partial match — check if any key is in the room name
        for key, dims in ROOM_TYPE_ESTIMATES.items():
            if key in name_lower:
                return dims
        return ROOM_TYPE_ESTIMATES["default"]

    def _is_paintable_wall(wall_finish):
        """Determine if wall finish indicates paintable surface."""
        if not wall_finish:
            return True  # Default to paintable
        wf = wall_finish.lower()
        # Non-paintable finishes
        if any(kw in wf for kw in ("tile", "stone", "brick", "glass", "metal panel",
                                     "wallcovering", "wall covering", "wc-", "vinyl")):
            return False
        return True

    def _get_wall_material(wall_finish):
        """Determine wall material type from finish spec.

        Faux finishes (lyme wash, plaster) are checked before GYP so that
        a substrate-plus-finish spec like "GYP / Plaster" routes to the
        finish bucket — the labor rate reflects the finish, not the
        substrate.
        """
        if not wall_finish:
            return "GYP"
        wf = wall_finish.lower()
        if any(kw in wf for kw in ("cmu", "block", "masonry", "concrete")):
            return "CMU"
        if any(kw in wf for kw in (
                "lyme wash", "lyme-wash", "lymewash",
                "lime wash", "lime-wash", "limewash")):
            return "LYMEWASH"
        if "plaster" in wf:
            return "PLASTER"
        return "GYP"

    def _is_painted_ceiling(ceiling_finish):
        """Determine if ceiling gets paint. Requires positive evidence — a
        blank/unknown ceiling finish is NOT assumed to be painted gypsum."""
        if not ceiling_finish:
            return False
        cf = ceiling_finish.lower()
        # ACT (acoustic ceiling tile) is NOT painted
        if any(kw in cf for kw in ("act", "acoustic", "exposed", "none", "n/a")):
            return False
        return True

    def _get_ceiling_material(ceiling_finish):
        """Determine ceiling material type. Blank/unknown is reported as
        UNKNOWN rather than assumed gypsum — see _is_painted_ceiling."""
        if not ceiling_finish:
            return "UNKNOWN"
        cf = ceiling_finish.lower()
        if "dryfall" in cf:
            return "DRYFALL"
        return "GYP"

    def _has_base_trim(base_finish):
        """Determine if room has paintable base trim."""
        if not base_finish:
            return False
        bf = base_finish.lower()
        if any(kw in bf for kw in ("wood", "wd", "paint", "mdf", "pine", "poplar")):
            return True
        if any(kw in bf for kw in ("rubber", "vinyl", "tile", "none", "n/a", "carpet")):
            return False
        return True  # Base finish present but unrecognized — assume paintable

    def _is_concrete_floor(floor_finish):
        """Determine if floor needs concrete sealer.
        Only returns True when specs explicitly call out sealcoating/sealer/epoxy —
        a bare concrete floor alone does NOT qualify (paint scope only).
        """
        if not floor_finish:
            return False
        ff = floor_finish.lower()
        # Must have explicit sealer/coating spec — bare "concrete" alone is not enough
        return any(kw in ff for kw in (
            "sealed concrete", "concrete sealer", "seal concrete",
            "epoxy", "epoxy coating", "floor coating", "floor sealer",
            "concrete coating", "sealcoat", "seal coat"))

    # Determine building multiplier
    n_buildings = bi.get("total_identical_buildings", 1) if ENABLE_BUILDING_MULTIPLIER else 1
    unit_types = bi.get("unit_types", [])
    floors_per_building = max(bi.get("floors_per_building", 1), 1)
    ceiling_height_override = bi.get("ceiling_height_ft", 0)

    # Validation clamp: if the LLM reported floors_per_building > 1 but every
    # room in the schedule lives on the same floor_level, the LLM almost
    # certainly inflated the count (e.g. counted "Roof" or a mezzanine row).
    # Common failure on retail boxes — see B&N regression. Clamp to 1.
    if floors_per_building > 1 and rooms_data:
        # Normalize floor_level values: keep only labels that look like an
        # occupied floor ("1", "2", "Ground", "Basement"). Drop "Roof",
        # "Mezzanine", and blanks.
        seen_levels = set()
        for r in rooms_data:
            lvl = str(r.get("floor_level", "")).strip().lower()
            if not lvl:
                continue
            if lvl in ("roof", "mezz", "mezzanine", "penthouse", "attic"):
                continue
            seen_levels.add(lvl)
        if len(seen_levels) <= 1:
            print(f"   ⚠️  floors_per_building clamp: LLM said {floors_per_building}, "
                  f"but rooms only span {len(seen_levels)} occupied level(s) "
                  f"({sorted(seen_levels) or 'none'}). Clamping to 1.")
            floors_per_building = 1

    # Build a unit count map: {"2BR": 6, "3BR": 6}
    unit_count_map = {}
    for ut in unit_types:
        utype = ut.get("type", "")
        count = ut.get("count_per_building", 1)
        if utype:
            unit_count_map[utype.lower()] = count

    # Separate rooms into unit-type rooms and common-area rooms
    unit_rooms = {}  # {unit_type: [room_dicts]}
    common_rooms = []

    for room in rooms_data:
        room_name = room.get("room_name", "Unknown Room")
        unit_type = room.get("unit_type", "").strip()
        is_common = room.get("is_common_area", False)

        if is_common or unit_type.lower() in ("common", "common_area", "common area", ""):
            if not unit_type or is_common:
                common_rooms.append(room)
                continue

        # Group by unit type
        ut_key = unit_type.lower() if unit_type else "unknown"
        unit_rooms.setdefault(ut_key, []).append(room)

    # Generate synthetic rooms
    synthetic_rooms = []
    room_counter = 0

    # Process unit-type rooms (with multipliers)
    for ut_key, ut_room_list in unit_rooms.items():
        # Find the count for this unit type
        units_per_building = 1
        for map_key, count in unit_count_map.items():
            if map_key in ut_key or ut_key in map_key:
                units_per_building = count
                break

        # Total multiplier = units_per_building × n_buildings
        total_multiplier = units_per_building * n_buildings

        for room in ut_room_list:
            room_counter += 1
            room_name = room.get("room_name", "Unknown Room")
            dims = _match_room_type(room_name)
            length = dims["length"]
            width = dims["width"]
            ch = ceiling_height_override if ceiling_height_override > 0 else dims["ceiling_height"]

            perimeter = 2 * (length + width)
            wall_area = perimeter * ch * SCHEDULE_ESTIMATION_CONFIDENCE
            ceiling_area = length * width * SCHEDULE_ESTIMATION_CONFIDENCE
            floor_area = length * width

            wall_finish = room.get("wall_finish", "Paint")
            ceiling_finish = room.get("ceiling_finish", "")
            base_finish = room.get("base_finish", "")
            floor_finish = room.get("floor_finish", "")

            wall_mat = _get_wall_material(wall_finish)
            ceil_mat = _get_ceiling_material(ceiling_finish)
            paintable_wall = _is_paintable_wall(wall_finish)
            painted_ceiling = _is_painted_ceiling(ceiling_finish)
            has_trim = _has_base_trim(base_finish)
            concrete_floor = _is_concrete_floor(floor_finish)

            synthetic_room = {
                "room_id": f"SE-{ut_key.upper()}-R{room_counter:03d}",
                "room_name": f"{room_name} ({ut_key.upper()})",
                "source_page": 0,
                "source_sheet": "Room Finish Schedule",
                "unit_multiplier": total_multiplier,
                "unit_type": ut_key,
                "dimensions": {
                    "length_feet": round(length, 2),
                    "width_feet": round(width, 2),
                    "ceiling_height_feet": round(ch, 2),
                    "floor_area_sqft": round(floor_area, 2),
                    "perimeter_lf": round(perimeter, 2),
                    "wall_area_sqft": round(wall_area, 2) if paintable_wall else 0,
                    "ceiling_area_sqft": round(ceiling_area, 2) if painted_ceiling else 0,
                },
                "materials": {
                    "walls": wall_mat if paintable_wall else "N/A",
                    "ceiling": ceil_mat,
                    "ceiling_painted": painted_ceiling,
                },
                "elements": {
                    "doors_full_paint": 0,  # Handled by schedule overrides
                    "doors_hm_panel": 0,
                    "doors_frame_only": 0,
                    "windows_total": 0,
                    "windows_painted_interior": 0,
                    "base_trim_lf": round(perimeter * SCHEDULE_ESTIMATION_CONFIDENCE, 2) if has_trim else 0,
                    "stair_sections": 0,
                    "gyp_between_stairs_sqft": 0,
                    "level_5_finish_sqft": 0,
                    "concrete_floor_sqft": round(floor_area, 2) if concrete_floor else 0,
                    "painted_columns_ea": 0,
                    "wallcovering_sqft": 0,
                    "stained_wood_sqft": 0,
                    "soffit_sqft": 0,
                    "painted_railing_lf": 0,
                },
                "notes": f"Schedule-estimated room ({total_multiplier}x: {units_per_building} units/bldg × {n_buildings} buildings). "
                         f"Wall finish: {wall_finish}. Ceiling: {ceiling_finish or 'UNVERIFIED — not in finish schedule'}. Base: {base_finish or 'UNVERIFIED — not in finish schedule'}.",
                "source": "schedule_estimate",
                "estimated_dimensions": True,
                "in_scope": True,
                "scope_exclusion_reason": "",
            }
            synthetic_rooms.append(synthetic_room)

    # Process common area rooms (once per floor per building)
    for room in common_rooms:
        room_counter += 1
        room_name = room.get("room_name", "Unknown Room")
        dims = _match_room_type(room_name)
        length = dims["length"]
        width = dims["width"]
        ch = ceiling_height_override if ceiling_height_override > 0 else dims["ceiling_height"]

        perimeter = 2 * (length + width)
        wall_area = perimeter * ch * SCHEDULE_ESTIMATION_CONFIDENCE
        ceiling_area = length * width * SCHEDULE_ESTIMATION_CONFIDENCE
        floor_area = length * width

        wall_finish = room.get("wall_finish", "Paint")
        ceiling_finish = room.get("ceiling_finish", "")
        base_finish = room.get("base_finish", "")
        floor_finish = room.get("floor_finish", "")

        wall_mat = _get_wall_material(wall_finish)
        ceil_mat = _get_ceiling_material(ceiling_finish)
        paintable_wall = _is_paintable_wall(wall_finish)
        painted_ceiling = _is_painted_ceiling(ceiling_finish)
        has_trim = _has_base_trim(base_finish)
        concrete_floor = _is_concrete_floor(floor_finish)

        # Common areas: once per floor per building
        common_multiplier = floors_per_building * n_buildings

        synthetic_room = {
            "room_id": f"SE-COMMON-R{room_counter:03d}",
            "room_name": f"{room_name} (Common)",
            "source_page": 0,
            "source_sheet": "Room Finish Schedule",
            "unit_multiplier": common_multiplier,
            "unit_type": "common_area",
            "dimensions": {
                "length_feet": round(length, 2),
                "width_feet": round(width, 2),
                "ceiling_height_feet": round(ch, 2),
                "floor_area_sqft": round(floor_area, 2),
                "perimeter_lf": round(perimeter, 2),
                "wall_area_sqft": round(wall_area, 2) if paintable_wall else 0,
                "ceiling_area_sqft": round(ceiling_area, 2) if painted_ceiling else 0,
            },
            "materials": {
                "walls": wall_mat if paintable_wall else "N/A",
                "ceiling": ceil_mat,
                "ceiling_painted": painted_ceiling,
            },
            "elements": {
                "doors_full_paint": 0,
                "doors_hm_panel": 0,
                "doors_frame_only": 0,
                "windows_total": 0,
                "windows_painted_interior": 0,
                "base_trim_lf": round(perimeter * SCHEDULE_ESTIMATION_CONFIDENCE, 2) if has_trim else 0,
                "stair_sections": 0,
                "gyp_between_stairs_sqft": 0,
                "level_5_finish_sqft": 0,
                "concrete_floor_sqft": round(floor_area, 2) if concrete_floor else 0,
                "painted_columns_ea": 0,
                "wallcovering_sqft": 0,
                "stained_wood_sqft": 0,
                "soffit_sqft": 0,
                "painted_railing_lf": 0,
            },
            "notes": f"Schedule-estimated common area ({common_multiplier}x: {floors_per_building} floors × {n_buildings} buildings). "
                     f"Wall: {wall_finish}. Ceiling: {ceiling_finish or 'UNVERIFIED — not in finish schedule'}. Base: {base_finish or 'UNVERIFIED — not in finish schedule'}.",
            "source": "schedule_estimate",
            "estimated_dimensions": True,
            "in_scope": True,
            "scope_exclusion_reason": "",
        }
        synthetic_rooms.append(synthetic_room)

    # Handle garage concrete sealer if building has garage
    garage_sqft = bi.get("garage_floor_area_sqft", 0)
    if garage_sqft > 0 and n_buildings >= 1:
        room_counter += 1
        synthetic_rooms.append({
            "room_id": f"SE-GARAGE-R{room_counter:03d}",
            "room_name": "Parking Garage",
            "source_page": 0,
            "source_sheet": "Building Info",
            "unit_multiplier": n_buildings,
            "unit_type": "garage",
            "dimensions": {
                "length_feet": 0,
                "width_feet": 0,
                "ceiling_height_feet": 10,
                "floor_area_sqft": garage_sqft,
                "perimeter_lf": 0,
                "wall_area_sqft": 0,
                "ceiling_area_sqft": 0,
            },
            "materials": {
                "walls": "N/A",
                "ceiling": "N/A",
                "ceiling_painted": False,
            },
            "elements": {
                "doors_full_paint": 0,
                "doors_hm_panel": 0,
                "doors_frame_only": 0,
                "windows_total": 0,
                "windows_painted_interior": 0,
                "base_trim_lf": 0,
                "stair_sections": 0,
                "gyp_between_stairs_sqft": 0,
                "level_5_finish_sqft": 0,
                "concrete_floor_sqft": 0,  # Only if specs explicitly call out sealcoating
                "painted_columns_ea": 0,
                "wallcovering_sqft": 0,
                "stained_wood_sqft": 0,
                "soffit_sqft": 0,
                "painted_railing_lf": 0,
            },
            "notes": f"Parking garage ({n_buildings}x buildings × {garage_sqft:,.0f} sqft/building). Concrete sealer excluded unless specs explicitly require it.",
            "source": "schedule_estimate",
            "estimated_dimensions": True,
            "in_scope": True,
            "scope_exclusion_reason": "",
        })

    if not synthetic_rooms:
        return None

    # Wrap rooms in a single floor (schedule doesn't specify floor breakdown)
    floors = [{
        "floor_name": "Schedule-Estimated Rooms",
        "rooms": synthetic_rooms,
    }]

    # Summary
    total_effective_rooms = sum(
        r.get("unit_multiplier", 1) for r in synthetic_rooms
    )
    print(f"   📊 Generated {len(synthetic_rooms)} room templates → {total_effective_rooms} effective rooms")
    if n_buildings > 1:
        print(f"      Building multiplier: ×{n_buildings} identical buildings")
    if garage_sqft > 0:
        print(f"      Garage concrete: {garage_sqft * n_buildings:,.0f} sqft total")

    return floors


def _apply_building_multiplier(combined, building_info):
    """
    For multi-building projects, scale schedule-override quantities
    (doors, windows, stairs) by the building count.

    The per-room quantities are already scaled via unit_multiplier in the
    synthetic rooms. But schedule overrides (from _apply_schedule_overrides)
    represent a SINGLE building's counts and need separate scaling.

    This function is called AFTER _apply_schedule_overrides() and only
    affects the schedule-override quantities.
    """
    n_buildings = building_info.get("total_identical_buildings", 1)
    if n_buildings <= 1:
        return combined

    # The building multiplier metadata — don't apply to aggregated_totals
    # because the per-room unit_multiplier already handles wall/ceiling/trim scaling.
    # We just need to store the info for schedule override scaling in _apply_schedule_overrides.
    combined["building_info"] = building_info
    combined.setdefault("notes", []).append(
        f"[Building Info] {n_buildings} identical buildings detected — "
        f"schedule override quantities will be scaled accordingly"
    )

    return combined


def _purge_stale_schedule_notes(combined, *, doors=False, windows=False):
    """Drop free-text notes that claim a schedule is missing once that schedule
    has actually been detected and applied.

    The extraction LLM emits "no door schedule provided" style notes per chunk
    when a schedule isn't visible in that chunk. When the schedule later turns
    up in another chunk — or via targeted re-analysis — those notes become
    false, but they are plain LLM notes, not [bracketed] pipeline markers, so
    the normal dedup keeps them and they end up contradicting the estimate.
    """
    notes = combined.get("notes")
    if not notes or not (doors or windows):
        return

    # Phrases that, sitting just after a schedule term, mean the note is
    # asserting that schedule does not exist. Kept narrow on purpose so a note
    # like "door schedule lists 49 doors, not all HM" is not caught.
    _ABSENCE_AFTER = (
        "not provided", "not found", "not included", "not present",
        "not available", "not located", "not shown", "not detected",
        "not in the set", "not in the drawing", "not part of",
        "was not", "were not", "wasn't", "weren't", "is missing",
        "missing", "unavailable", "absent", "none provided", "none found",
    )

    terms = []
    if doors:
        terms += ["door schedule", "door/frame schedule", "door and frame schedule"]
    if windows:
        terms += ["window schedule"]

    def _denies_schedule(text_lower):
        for term in terms:
            idx = text_lower.find(term)
            while idx != -1:
                before = text_lower[max(0, idx - 14):idx]
                if "no " in before or "without" in before or "lack" in before:
                    return True
                after = text_lower[idx + len(term):idx + len(term) + 45]
                if any(a in after for a in _ABSENCE_AFTER):
                    return True
                idx = text_lower.find(term, idx + 1)
        return False

    kept = []
    removed = 0
    for n in notes:
        s = str(n)
        stripped = s.strip()
        is_bracketed = stripped.startswith("[") and "]" in stripped
        if not is_bracketed and _denies_schedule(s.lower()):
            removed += 1
            continue
        kept.append(n)

    if removed:
        combined["notes"] = kept
        print(f"   🧹 Purged {removed} stale 'schedule missing' note(s) "
              f"that contradict a detected schedule")


def _apply_schedule_overrides(combined):
    """
    Apply authoritative schedule data (from door/window schedule PDFs)
    to override room-summed counts in aggregated_totals.

    Schedules are AUTHORITATIVE — they list EVERY door/window in the project.
    Schedule values ALWAYS replace room-level counts (both up AND down), because:
    - Room extraction double-counts doors (every room shows doors → duplicates)
    - Room extraction may miss HM doors entirely (they're in corridors, not rooms)
    - Window painted counts from room extraction are unreliable
    The schedule is the single source of truth for counts.
    """
    schedule_data = combined.get("schedule_data", {})
    if not schedule_data:
        return combined

    agg = combined.get("aggregated_totals", {})
    pi = combined.get("project_info", {})
    overrides_applied = []

    # --- Building multiplier for schedule overrides ---
    # When schedule data comes from ONE building but the project has N identical buildings,
    # scale the schedule counts by the building count.
    # (Per-room wall/ceiling areas are already scaled via unit_multiplier in synthetic rooms.)
    building_info = combined.get("building_info", {})
    is_schedule_estimated = combined.get("schedule_estimated", False)
    try:
        from config import ENABLE_BUILDING_MULTIPLIER
    except ImportError:
        ENABLE_BUILDING_MULTIPLIER = True
    building_count = building_info.get("total_identical_buildings", 1) if (
        is_schedule_estimated and ENABLE_BUILDING_MULTIPLIER
    ) else 1
    schedule_scale = building_count if building_count > 1 else 1

    # --- Door schedule overrides (AUTHORITATIVE — always replace) ---
    ds = schedule_data.get("door_schedule", {})
    sched_doors_fp = _num(ds.get("total_doors_full_paint", 0)) * schedule_scale
    sched_doors_hm = _num(ds.get("total_doors_hm_panel", 0)) * schedule_scale
    sched_doors_frame = _num(ds.get("total_doors_frame_only", 0)) * schedule_scale
    sched_doors_total = sched_doors_fp + sched_doors_hm + sched_doors_frame

    if sched_doors_total > 0:
        room_doors_fp = _num(agg.get("total_doors_full_paint", 0))
        room_doors_hm = _num(agg.get("total_doors_hm_panel", 0))

        # Schedule is authoritative — always use its values
        agg["total_doors_full_paint"] = sched_doors_fp
        agg["total_doors_hm_panel"] = sched_doors_hm
        agg["total_doors_frame_only"] = sched_doors_frame

        # Safety check: for commercial buildings, if the schedule says 0 HM doors
        # but room-level extraction found significant HM doors, the schedule may
        # be misclassifying HM frames (which ARE paintable) as non-paint doors.
        # In that case, preserve the room-level HM count.
        _pi = combined.get("project_info", {})
        building_type_str = str(_pi.get("building_type", "")).lower()
        if ("commercial" in building_type_str and sched_doors_hm == 0
                and room_doors_hm >= 5):
            agg["total_doors_hm_panel"] = room_doors_hm
            overrides_applied.append(
                f"Doors SET by schedule: {sched_doors_fp:.0f} full paint + {sched_doors_hm:.0f} HM panel"
                f" (room-level had {room_doors_fp:.0f} + {room_doors_hm:.0f})"
                f" — commercial HM override: kept room-level {room_doors_hm:.0f} HM doors"
                f" (schedule likely misclassified HM frames as non-paint)"
            )
        else:
            overrides_applied.append(
                f"Doors SET by schedule: {sched_doors_fp:.0f} full paint + {sched_doors_hm:.0f} HM panel"
                f" + {sched_doors_frame:.0f} frame only"
                f" (room-level had {room_doors_fp:.0f} + {room_doors_hm:.0f})"
            )

    # --- Residential interior door supplement ---
    # Architectural door schedules in residential buildings often omit "typical"
    # interior doors (closet, linen, pantry) that are not individually scheduled.
    # When room-level extraction found MORE doors than the schedule, the room-level
    # count is likely more accurate because it captures closet doors visible on
    # floor plans. Use max(schedule, room_level) for residential buildings.
    if sched_doors_total > 0:
        _pi_doors = combined.get("project_info", {})
        _bt_doors = str(_pi_doors.get("building_type", "")).lower()
        _is_residential_doors = any(kw in _bt_doors for kw in (
            "residential", "mixed", "multi", "apartment", "condo", "senior", "living"))
        _total_units_doors = _num(_pi_doors.get("total_units", 0))

        if _is_residential_doors and _total_units_doors >= 4:
            room_total_doors = room_doors_fp + room_doors_hm
            if room_total_doors > sched_doors_total:
                raw_supplement = round(room_total_doors - sched_doors_total)
                # Cap supplement: larger schedules are more complete and need less supplement.
                # Calibrated from:
                #   Fishkill: schedule=120, manual=159 → 33% supplement (small schedule)
                #   364 Main: schedule=153, manual=155 → 1.3% supplement (large schedule)
                # Scale: ≤100 doors → 35% cap, 150+ doors → 15% cap
                if sched_doors_total <= 100:
                    _supp_pct = 0.35
                elif sched_doors_total >= 150:
                    _supp_pct = 0.15
                else:
                    # Linear interpolation: 100→35%, 150→15%
                    _supp_pct = 0.35 - (sched_doors_total - 100) / 50 * 0.20
                max_supplement = round(sched_doors_total * _supp_pct)
                supplement = min(raw_supplement, max_supplement)
                if HARD_NUMBERS_ONLY:
                    # The schedule is the authoritative count; the room-level
                    # count is a noisy measurement this same function's
                    # docstring admits can double-count. The delta is
                    # EVIDENCE of possibly-unscheduled closet doors, not a
                    # quantity — surface it for confirmation, price the
                    # schedule.
                    overrides_applied.append(
                        f"[Door Count Check] Room-level extraction found "
                        f"{room_total_doors:.0f} doors vs door schedule "
                        f"{sched_doors_total:.0f}. RFI REQUIRED: confirm "
                        f"whether typical interior doors (closet/linen/"
                        f"pantry) are omitted from the door schedule — "
                        f"~{supplement} doors NOT priced under hard-numbers "
                        f"policy."
                    )
                else:
                    agg["total_doors_full_paint"] = agg.get("total_doors_full_paint", 0) + supplement
                    overrides_applied.append(
                        f"[Door Supplement] Residential building: room-level extraction found "
                        f"{room_total_doors:.0f} doors vs schedule {sched_doors_total:.0f}. "
                        f"Added {supplement} interior doors (closets/pantries likely omitted "
                        f"from architectural door schedule)."
                        + (f" (capped from {raw_supplement} to {supplement})" if raw_supplement > max_supplement else "")
                    )

    # --- Storefront door mark filter ---
    # LLM sometimes counts storefront/glazing entries (100A-100M, SF101, AD1 types)
    # as painted doors. If door_marks_counted contains letter-suffixed marks from
    # the same base number (e.g., 100A, 100B, ... 100M) and schedule notes reference
    # storefront/AD1, those marks are storefront glazing panels, NOT painted doors.
    door_marks = ds.get("door_marks_counted", [])
    door_notes_text = str(ds.get("notes", "")).lower()
    if door_marks and any(kw in door_notes_text for kw in (
            "storefront", "ad1", "ad-1", "aluminum door", "glazing",
            "curtain wall", "glass door")):
        # Find letter-suffixed marks: e.g., "100A", "100B", "100C", etc.
        # Pattern: digits followed by a single letter (A-Z)
        storefront_marks = []
        for mark in door_marks:
            mark_str = str(mark).strip()
            # Match patterns like "100A", "200B", "SF-101", "AD1-01"
            if re.match(r'^\d+[A-Za-z]$', mark_str):
                storefront_marks.append(mark_str)
            elif re.match(r'^(SF|AD|SD|GL)\d*', mark_str, re.IGNORECASE):
                storefront_marks.append(mark_str)

        # Only filter if we found a cluster of letter-suffixed marks from same base
        # (e.g., 100A through 100M = 13 marks from base "100")
        if storefront_marks:
            # Group by base number
            base_groups = {}
            for m in storefront_marks:
                base_match = re.match(r'^(\d+)[A-Za-z]$', m)
                if base_match:
                    base = base_match.group(1)
                    base_groups.setdefault(base, []).append(m)

            # Filter out groups with 3+ letter-suffixed marks (strong storefront signal)
            sf_count = 0
            sf_filtered = []
            for base, marks_list in base_groups.items():
                if len(marks_list) >= 3:
                    sf_count += len(marks_list)
                    sf_filtered.extend(marks_list)

            # Also count SF/AD/SD/GL prefixed marks
            for m in storefront_marks:
                if re.match(r'^(SF|AD|SD|GL)\d*', m, re.IGNORECASE):
                    sf_count += 1
                    sf_filtered.append(m)

            if sf_count > 0:
                # Subtract storefront marks from full_paint (most likely category)
                current_fp = _num(agg.get("total_doors_full_paint", 0))
                adjusted_fp = max(0, current_fp - sf_count)

                # Safety floor: don't let storefront filter eliminate ALL full_paint
                # doors for commercial buildings that clearly have interior doors.
                # Count rooms that are obviously interior spaces with doors.
                building_type_sf = str(pi.get("building_type", "")).lower()
                is_commercial_sf = any(kw in building_type_sf for kw in (
                    "commercial", "dealership", "retail", "office", "industrial"))

                if adjusted_fp == 0 and is_commercial_sf:
                    INTERIOR_ROOM_KW = (
                        "office", "break", "toilet", "restroom", "locker",
                        "janitor", "conference", "it ", "dispatch", "f&i",
                        "lunch", "storage", "closet")
                    interior_door_rooms = 0
                    for floor in combined.get("floors", []):
                        for room in floor.get("rooms", []):
                            if not room.get("in_scope", True):
                                continue
                            rname = str(room.get("room_name", "")).lower()
                            if any(kw in rname for kw in INTERIOR_ROOM_KW):
                                dr = _num(room.get("elements", {}).get(
                                    "doors_full_paint", 0))
                                if dr > 0:
                                    mult = max(1, int(_num(
                                        room.get("unit_multiplier", 1))))
                                    interior_door_rooms += mult

                    if interior_door_rooms > 0:
                        # Use interior room count as floor, but don't exceed
                        # the pre-filter schedule count (schedule is authoritative)
                        door_floor = min(interior_door_rooms, current_fp)
                        if door_floor > 0:
                            adjusted_fp = door_floor
                            overrides_applied.append(
                                f"Storefront filter safety: restored {door_floor} "
                                f"full_paint doors for interior rooms (offices, "
                                f"break rooms, toilets, etc.). Filter would have "
                                f"set count to 0.")
                            print(f"   🔧 Storefront safety: restored {door_floor} "
                                  f"interior doors (from {interior_door_rooms} "
                                  f"interior rooms)")

                agg["total_doors_full_paint"] = adjusted_fp
                overrides_applied.append(
                    f"Storefront door filter: removed {sf_count} storefront marks "
                    f"({', '.join(sf_filtered[:8])}{'...' if len(sf_filtered) > 8 else ''}) "
                    f"from full_paint count ({current_fp:.0f} → {adjusted_fp:.0f}). "
                    f"Schedule notes reference storefront/AD1.")
                print(f"   🔧 Storefront filter: -{sf_count} doors "
                      f"({', '.join(sf_filtered[:5])}{'...' if len(sf_filtered) > 5 else ''})")

    # --- Window schedule overrides (AUTHORITATIVE for painted count) ---
    ws = schedule_data.get("window_schedule", {})
    sched_win_painted = _num(ws.get("windows_painted_interior", 0)) * schedule_scale
    sched_win_total = _num(ws.get("total_windows", 0)) * schedule_scale
    has_win_schedule = combined.get("has_window_schedule", False)

    if sched_win_total > 0:
        room_win_painted = _num(agg.get("total_windows_painted_interior", 0))
        room_win_total = _num(agg.get("total_windows_all", 0))

        # For painted windows: schedule is authoritative
        # If schedule says 0 painted, that means NO windows need interior paint
        # (they're factory-finished, vinyl, etc.)
        agg["total_windows_painted_interior"] = sched_win_painted
        agg["total_windows_all"] = max(sched_win_total, room_win_total)
        overrides_applied.append(
            f"Windows SET by schedule: {sched_win_painted:.0f} painted interior"
            f" out of {sched_win_total:.0f} total"
            f" (room-level had {room_win_painted:.0f} painted, {room_win_total:.0f} total)"
        )
        spec = ws.get("window_paint_spec", "")
        if spec:
            overrides_applied.append(f"Window paint spec: {spec}")

    elif has_win_schedule and sched_win_total == 0 and ws:
        # Schedule was found but reports 0 total windows — ALL windows are
        # aluminum/factory-finished (not painted).  Zero out room-level painted
        # window counts which are unreliable (room extraction can hallucinate
        # painted windows from window labels on floor plans).
        room_win_painted = _num(agg.get("total_windows_painted_interior", 0))
        if room_win_painted > 0:
            agg["total_windows_painted_interior"] = 0
            overrides_applied.append(
                f"Windows ZEROED by schedule: schedule found but reports 0 total windows "
                f"(all aluminum/factory-finished). Removed {room_win_painted:.0f} phantom "
                f"painted windows from room-level extraction."
            )
            print(f"   🔧 Window schedule override: zeroed {room_win_painted:.0f} phantom "
                  f"painted windows (schedule says 0 total)")

    # --- Window component overrides (apron/casing/stool/return) ---
    # Pull per-component counts directly from the schedule. These represent
    # paintable trim components that exist regardless of whether the sash is
    # field-painted. Components called out in the schedule are authoritative —
    # do NOT estimate or apply heuristics when missing; flag for RFI instead.
    apron_ct = _num(ws.get("windows_with_apron", 0)) * schedule_scale
    casing_ct = _num(ws.get("windows_with_casing", 0)) * schedule_scale
    stool_ct = _num(ws.get("windows_with_stool_sill", 0)) * schedule_scale
    wood_return_ct = _num(ws.get("windows_with_wood_return", 0)) * schedule_scale
    drywall_return_ct = _num(ws.get("windows_with_drywall_return", 0)) * schedule_scale

    if sched_win_total > 0:
        agg["total_window_aprons_painted"] = apron_ct
        agg["total_window_casings_painted"] = casing_ct
        agg["total_window_stools_painted"] = stool_ct
        agg["total_window_wood_returns_painted"] = wood_return_ct
        agg["total_window_drywall_returns"] = drywall_return_ct
        if (apron_ct + casing_ct + stool_ct + wood_return_ct) > 0:
            overrides_applied.append(
                f"Window components from schedule: aprons={apron_ct:.0f}, "
                f"casings={casing_ct:.0f}, stools={stool_ct:.0f}, "
                f"wood returns={wood_return_ct:.0f}, drywall returns={drywall_return_ct:.0f}"
            )

    # --- Door count sanity check for multi-unit residential ---
    building_type = str(combined.get("project_info", {}).get("building_type", "")).lower()
    total_units = _num(combined.get("project_info", {}).get("total_units", 0))
    if sched_doors_total > 0 and total_units > 0:
        expected_min_doors = total_units * 7  # Minimum ~7 doors per unit
        if sched_doors_total < expected_min_doors * 0.85:
            overrides_applied.append(
                f"Door count ({sched_doors_total:.0f}) may be low for {total_units:.0f} units "
                f"(expected >={expected_min_doors:.0f}). Schedule may not account for "
                f"typical unit door multipliers. RFI recommended."
            )

    # --- Stair schedule overrides (only override upward — stairs aren't in schedule) ---
    si = schedule_data.get("stair_info", {})
    sched_stairs = _num(si.get("total_stair_sections", 0)) * schedule_scale
    room_stairs = _num(agg.get("total_stair_sections", 0))

    if sched_stairs > room_stairs:
        agg["total_stair_sections"] = sched_stairs
        overrides_applied.append(
            f"Stairs overridden by schedule/details: {sched_stairs:.0f} sections"
            f" (room-level had {room_stairs:.0f})"
        )

    if schedule_scale > 1:
        overrides_applied.append(
            f"Building multiplier applied: schedule counts × {schedule_scale} "
            f"({building_count} identical buildings)"
        )

    # --- Exterior painting safety net for commercial buildings ---
    # LLM sometimes returns 0 exterior_paint_sqft even when building has EIFS/masonry
    # that clearly needs painting. If commercial building notes reference exterior
    # paint materials (EIFS, masonry, stucco) but exterior_paint_sqft is 0,
    # estimate from building envelope as a safety net.
    exterior = combined.get("exterior", {})
    ext_paint = _num(exterior.get("exterior_paint_sqft", 0))
    is_commercial = any(kw in building_type for kw in (
        "commercial", "auto", "industrial", "warehouse", "retail", "dealership"))
    if (not HARD_NUMBERS_ONLY) and ext_paint == 0 and is_commercial:
        all_notes = " ".join(str(n) for n in combined.get("notes", []))
        # Also scan material legend for exterior paint indicators
        for entry in combined.get("material_legend", []):
            all_notes += " " + str(entry.get("description", ""))
            all_notes += " " + str(entry.get("code", ""))
        # Also scan exterior.notes — LLM often puts EIFS/masonry references here
        ext_notes = str(exterior.get("notes", ""))
        all_notes += " " + ext_notes
        all_notes_lower = all_notes.lower()
        has_ext_paint_refs = any(kw in all_notes_lower for kw in (
            "eifs", "exterior paint", "ext paint", "ep-", "masonry paint",
            "stucco", "exterior finish", "ext. paint", "painted masonry",
            "paint masonry", "elastomeric", "acm", "metal panel",
            "precast", "precast panel",
            # Retail/storefront callouts that are commonly the ONLY exterior
            # paint scope on a tenant-fit-out (e.g. B&N — fascia, soffit,
            # rear/service door, bollards) and don't trigger the masonry/EIFS
            # keywords above:
            "fascia", "soffit", "rear door", "service door", "sign band",
            "cmu paint", "painted cmu", "painted metal", "bollard", "bollards",
            "exterior door", "ext door", "ext. door",
            "exterior trim", "ext trim", "canopy paint", "painted canopy"))
        if has_ext_paint_refs:
            # Estimate exterior paint from building footprint and stories
            _pi = combined.get("project_info", {})
            footprint = _num(_pi.get("footprint_sqft", 0))
            stories = max(1, _num(_pi.get("total_stories", 1)))
            avg_story_ht = 14  # Commercial average
            if footprint > 0:
                # Rough perimeter from footprint (assumes ~2:1 rectangle)
                long_side = math.sqrt(footprint * 2)
                short_side = footprint / long_side
                perimeter = 2 * (long_side + short_side)
                # Total envelope = perimeter × height × stories
                # Subtract ~30% for glass/storefront/doors
                envelope = perimeter * avg_story_ht * stories * 0.70
                ext_paint_est = round(envelope)
                exterior["exterior_paint_sqft"] = ext_paint_est
                if not exterior.get("lift_required") and stories >= 2:
                    exterior["lift_required"] = True
                overrides_applied.append(
                    f"Exterior painting safety net: estimated {ext_paint_est:,.0f} sqft "
                    f"from building envelope ({footprint:,.0f} sqft footprint × "
                    f"{stories:.0f} stories). Notes reference exterior paint materials "
                    f"but LLM returned 0 sqft.")
                print(f"   🔧 Exterior paint safety net: {ext_paint_est:,.0f} sqft "
                      f"(from {footprint:,.0f} sqft footprint × {stories:.0f} stories)")
            else:
                # No footprint — flag as high-severity warning
                overrides_applied.append(
                    "Exterior painting: notes reference EIFS/masonry/exterior paint but "
                    "0 sqft extracted AND no footprint available for estimation. "
                    "RFI recommended to confirm exterior painting scope.")
    combined["exterior"] = exterior

    # A detected schedule makes any earlier "no <X> schedule" LLM note false.
    # Purge those before emitting override notes so the two don't contradict.
    _purge_stale_schedule_notes(
        combined,
        doors=bool(ds) or bool(combined.get("has_door_schedule")),
        windows=bool(ws) or bool(combined.get("has_window_schedule")),
    )

    if overrides_applied:
        combined.setdefault("notes", [])
        for note in overrides_applied:
            combined["notes"].append(f"[Schedule Override] {note}")
        print(f"\n📋 Schedule overrides applied:")
        for note in overrides_applied:
            print(f"   • {note}")

    combined["aggregated_totals"] = agg
    return combined


def _detect_unit_mix(analysis):
    """Extract unit type mix from room-level unit_type fields, or fall back to UNIT_MIX_DEFAULT."""
    type_counts = {}
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            if not room.get("in_scope", True):
                continue
            ut = str(room.get("unit_type", "")).strip().lower()
            if not ut or ut in ("common", "common_area", "common area"):
                continue
            mult = max(1, int(_num(room.get("unit_multiplier", 1))))
            # Normalize to template keys
            if "studio" in ut or "efficiency" in ut:
                key = "studio"
            elif "3" in ut or "three" in ut:
                key = "3br"
            elif "2" in ut or "two" in ut:
                key = "2br"
            else:
                key = "1br"
            # Count unique unit types (not rooms — we want unit type proportions)
            type_counts[key] = type_counts.get(key, 0) + mult

    if not type_counts:
        # Fallback: check notes for unit type mentions
        all_notes = " ".join(str(n) for n in analysis.get("notes", [])).lower()
        has_types = {}
        for kw, key in [("studio", "studio"), ("1br", "1br"), ("1 br", "1br"),
                        ("one bedroom", "1br"), ("2br", "2br"), ("2 br", "2br"),
                        ("two bedroom", "2br"), ("3br", "3br"), ("3 br", "3br")]:
            if kw in all_notes:
                has_types[key] = 1
        if has_types:
            total = len(has_types)
            return {k: 1.0 / total for k in has_types}
        return dict(UNIT_MIX_DEFAULT)

    total = sum(type_counts.values()) or 1
    return {k: v / total for k, v in type_counts.items()}


def _supplement_missing_secondary_spaces(analysis):
    """
    Detect missing secondary spaces (closets, entry halls, unit hallways)
    using rooms-per-unit density. When density is low, supplement wall/ceiling/trim
    with estimated secondary space area per missing room.

    Safety: Only supplements when rooms-per-unit density confirms under-extraction.
    Caps total supplement at 45% of current extracted totals to prevent runaway.
    """
    pi = analysis.get("project_info", {})
    agg = analysis.get("aggregated_totals", {})

    # Gate: only for residential multi-family with 4+ units
    building_type = str(pi.get("building_type", "")).lower()
    is_residential = any(kw in building_type for kw in
                         ("residential", "mixed", "multi", "apartment", "condo"))
    total_units = int(_num(pi.get("total_units", 0)))
    if not is_residential or total_units < 4:
        return analysis

    # Skip if footprint fallback was used (already has full estimates)
    if analysis.get("_used_footprint_fallback"):
        return analysis

    # --- Count effective rooms and detect secondary spaces already extracted ---
    effective_rooms = 0
    secondary_found = 0
    SECONDARY_KW = ("closet", "clo", "wic", "walk-in", "hall", "entry",
                     "foyer", "vestibule", "mudroom", "pantry", "linen")

    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            if not room.get("in_scope", True):
                continue
            mult = max(1, int(_num(room.get("unit_multiplier", 1))))
            effective_rooms += mult
            rname = str(room.get("room_name", "")).lower()
            if any(kw in rname for kw in SECONDARY_KW):
                secondary_found += mult

    rooms_per_unit = effective_rooms / total_units if total_units > 0 else 0

    # --- Determine expected rooms from unit mix ---
    unit_mix = _detect_unit_mix(analysis)
    weighted_expected = sum(
        EXPECTED_ROOMS_PER_UNIT.get(utype, EXPECTED_ROOMS_PER_UNIT["1br"])["total_rooms"] * share
        for utype, share in unit_mix.items()
    )

    density_ratio = rooms_per_unit / weighted_expected if weighted_expected > 0 else 1.0
    secondary_per_unit = secondary_found / total_units if total_units > 0 else 0

    if density_ratio >= 0.85:
        analysis.setdefault("notes", []).append(
            f"[Secondary Space Check] Rooms/unit={rooms_per_unit:.1f}, "
            f"expected={weighted_expected:.1f}, density={density_ratio:.0%}. "
            f"No supplement needed."
        )
        print(f"   🏠 Secondary space check: {rooms_per_unit:.1f} rooms/unit "
              f"({density_ratio:.0%} density) — OK, no supplement")
        return analysis

    # --- Calculate supplement ---
    supplement_wall = 0
    supplement_ceil = 0
    supplement_trim = 0
    supplement_doors = 0
    supplement_details = []

    for utype, share in unit_mix.items():
        unit_count = round(total_units * share)
        if unit_count == 0:
            continue

        expected = EXPECTED_ROOMS_PER_UNIT.get(utype, EXPECTED_ROOMS_PER_UNIT["1br"])
        expected_total = expected["total_rooms"]

        # How many rooms extracted per unit of this type?
        extracted_per_unit = round(expected_total * density_ratio)
        missing_per_unit = max(0, expected_total - extracted_per_unit)

        # Reduce by secondary spaces already found
        expected_secondary_count = sum(c for _, c in expected["secondary"])
        if secondary_per_unit > 0 and expected_secondary_count > 0:
            already_ratio = min(1.0, secondary_per_unit / expected_secondary_count)
            missing_per_unit = max(0, missing_per_unit - round(expected_secondary_count * already_ratio))

        if missing_per_unit == 0:
            continue

        # Distribute missing rooms across secondary space types proportionally
        total_sec = sum(c for _, c in expected["secondary"])
        for space_type, count in expected["secondary"]:
            proportion = count / total_sec if total_sec > 0 else 0
            missing_of_type = max(1, round(missing_per_unit * proportion))
            tmpl = SECONDARY_SPACE_TEMPLATES[space_type]

            add_wall = tmpl["wall_sqft"] * missing_of_type * unit_count
            add_ceil = tmpl["ceiling_sqft"] * missing_of_type * unit_count
            add_trim = tmpl["trim_lf"] * missing_of_type * unit_count
            add_doors = tmpl["doors"] * missing_of_type * unit_count

            supplement_wall += add_wall
            supplement_ceil += add_ceil
            supplement_trim += add_trim
            supplement_doors += add_doors
            supplement_details.append(
                f"{missing_of_type}x {space_type}/unit × {unit_count} {utype}"
            )

    if supplement_wall == 0:
        return analysis

    # --- Safety cap: limit supplement to 45% of current extracted totals ---
    MAX_SUPPLEMENT_RATIO = 0.45
    current_wall = _num(agg.get("total_paintable_wall_sqft", 0))
    current_ceil = _num(agg.get("total_paintable_ceiling_sqft", 0))
    current_trim = _num(agg.get("total_base_trim_lf", 0))

    if current_wall > 0:
        wall_cap = round(current_wall * MAX_SUPPLEMENT_RATIO)
        if supplement_wall > wall_cap:
            scale = wall_cap / supplement_wall
            supplement_wall = wall_cap
            supplement_ceil = round(supplement_ceil * scale)
            supplement_trim = round(supplement_trim * scale)
            supplement_doors = round(supplement_doors * scale)
            supplement_details.append(f"Capped at {MAX_SUPPLEMENT_RATIO:.0%} of extracted")

    # HARD_NUMBERS_ONLY: the quantities below come from per-room TEMPLATES
    # ("a closet has 190 sqft of wall, 20 LF of trim, 1 door"), not from
    # anything measured on this project's drawings — this is exactly the
    # "prices items because other jobs have had that" failure customers
    # reported. Under the policy: record the density evidence + the
    # would-have-been quantities as an RFI, price nothing.
    if HARD_NUMBERS_ONLY:
        analysis["_secondary_space_supplement_suppressed"] = {
            "wall_would_add": supplement_wall,
            "ceil_would_add": supplement_ceil,
            "trim_would_add": supplement_trim,
            "doors_would_add": supplement_doors,
            "density_ratio": density_ratio,
            "rooms_per_unit": rooms_per_unit,
        }
        analysis.setdefault("notes", []).append(
            f"[Secondary Space Check] Room extraction density is low "
            f"({rooms_per_unit:.1f} rooms/unit vs ~{weighted_expected:.1f} "
            f"expected, {density_ratio:.0%}) — closets/entry halls are likely "
            f"missing from the extraction. RFI REQUIRED: confirm secondary "
            f"spaces (closets, entry halls, unit hallways) per unit on the "
            f"enlarged unit plans. Template-based supplement "
            f"(~{supplement_wall:,} wall sqft, ~{supplement_trim:,} trim LF, "
            f"~{supplement_doors} doors) NOT priced under hard-numbers policy."
        )
        print(f"   🔒 Secondary space supplement suppressed "
              f"(HARD_NUMBERS_ONLY): would have added +{supplement_wall:,} "
              f"wall sqft at {density_ratio:.0%} density — flagged for RFI")
        return analysis

    # --- Apply supplement ---
    agg["total_paintable_wall_sqft"] = current_wall + supplement_wall
    agg["total_paintable_ceiling_sqft"] = current_ceil + supplement_ceil
    agg["total_base_trim_lf"] = current_trim + supplement_trim
    agg["total_doors_full_paint"] = _num(agg.get("total_doors_full_paint", 0)) + supplement_doors
    analysis["aggregated_totals"] = agg

    analysis["_secondary_space_supplement"] = {
        "wall_added": supplement_wall,
        "ceil_added": supplement_ceil,
        "trim_added": supplement_trim,
        "doors_added": supplement_doors,
        "density_ratio": density_ratio,
        "rooms_per_unit": rooms_per_unit,
    }

    analysis.setdefault("notes", []).append(
        f"[Secondary Space Supplement] Rooms/unit={rooms_per_unit:.1f} vs "
        f"expected={weighted_expected:.1f} ({density_ratio:.0%} density). "
        f"Added estimated secondary spaces: +{supplement_wall:,} wall sqft, "
        f"+{supplement_ceil:,} ceiling sqft, +{supplement_trim:,} trim LF, "
        f"+{supplement_doors} doors. [{'; '.join(supplement_details)}]"
    )
    print(f"   🏠 Secondary space supplement: +{supplement_wall:,} wall, "
          f"+{supplement_ceil:,} ceil, +{supplement_trim:,} trim "
          f"({density_ratio:.0%} room density, {rooms_per_unit:.1f} rooms/unit)")

    return analysis


def _validate_and_boost_walls(analysis):
    """
    Cross-check wall area against building metrics and boost if under-extracted.
    Residential multi-family buildings have ~1.5-2.0 sqft of wall per sqft of floor area
    due to interior partitions (closets, bathrooms, corridors within units).

    Skips boosting when unit-count fallback used footprint-based estimation
    (already calibrated to Rider actuals — boost would over-inflate).

    Two modes (tried in order):
    1. Perimeter-based (preferred): uses per-room perimeter × height data from
       _validate_wall_area_by_perimeter(). More reliable than footprint.
    2. Footprint-based (fallback): uses building footprint × stories × 1.25 ratio.
       Footprint extraction has ±36% variance.
    """
    # Skip boost if unit-count fallback already used footprint-based estimation.
    # The footprint method (ceiling = footprint × stories × 0.63, walls = ceiling × 3.3)
    # is calibrated to Rider actuals and doesn't need additional boosting.
    if analysis.get("_used_footprint_fallback"):
        return analysis

    pi = analysis.get("project_info", {})
    agg = analysis.get("aggregated_totals", {})

    # --- Mode 1: Perimeter-based boost (preferred) ---
    # If _validate_wall_area_by_perimeter() already computed perimeter-derived totals,
    # use those as the ground truth instead of the unreliable footprint.
    pcc = analysis.get("_perimeter_cross_check")
    if pcc and pcc.get("perimeter_derived_paintable_sqft", 0) > 0:
        perimeter_wall = pcc["perimeter_derived_paintable_sqft"]
        current_wall = _num(agg.get("total_paintable_wall_sqft", 0))

        # If wallcovering was estimated via safety net and deducted from wall total,
        # also deduct it from the perimeter target to avoid re-inflating walls
        wc_sqft = _num(agg.get("total_wallcovering_sqft", 0))
        if wc_sqft > 0:
            perimeter_wall = max(0, perimeter_wall - wc_sqft)

        if current_wall > 0 and perimeter_wall > current_wall * 1.05:
            # Perimeter-derived total is higher — some wall area was lost in aggregation
            boost_factor = perimeter_wall / current_wall

            # Safety cap — elevated when secondary space supplement confirmed
            # under-extraction via room density (independent of footprint accuracy).
            MAX_BOOST_FACTOR = 1.30
            sec_supp = analysis.get("_secondary_space_supplement")
            if sec_supp and sec_supp.get("density_ratio", 1.0) < 0.70:
                MAX_BOOST_FACTOR = 1.60
            elif sec_supp and sec_supp.get("density_ratio", 1.0) < 0.85:
                MAX_BOOST_FACTOR = 1.45
            if boost_factor > MAX_BOOST_FACTOR:
                analysis.setdefault("notes", []).append(
                    f"[Perimeter Wall Boost Cap] Computed boost {boost_factor:.2f}x exceeds "
                    f"max {MAX_BOOST_FACTOR}x. Capping. Perimeter-derived: {perimeter_wall:,}, "
                    f"aggregated: {current_wall:,}."
                )
                boost_factor = MAX_BOOST_FACTOR

            if boost_factor > 1.05:
                boosted_wall = round(current_wall * boost_factor)
                current_trim = _num(agg.get("total_base_trim_lf", 0))
                # HARD_NUMBERS_ONLY: the wall correction is derived from
                # MEASURED per-room perimeter × height (repairing aggregation
                # loss against the same drawings) and stays. Trim, however,
                # is measured per-room in LF — multiplying it by a wall-area
                # ratio is not a measurement, so it is no longer scaled.
                if HARD_NUMBERS_ONLY:
                    boosted_trim = current_trim
                else:
                    boosted_trim = round(current_trim * boost_factor) if current_trim > 0 else current_trim

                agg["total_paintable_wall_sqft"] = boosted_wall
                agg["total_base_trim_lf"] = boosted_trim
                analysis["aggregated_totals"] = agg

                analysis.setdefault("notes", []).append(
                    f"[Perimeter Wall Boost] Aggregated walls ({current_wall:,} sqft) "
                    f"< perimeter-derived ({perimeter_wall:,} sqft). "
                    f"Boosted to {boosted_wall:,} sqft ({boost_factor:.2f}x). "
                    f"Trim {current_trim:,}->{boosted_trim:,} LF"
                    f"{' (trim not scaled — hard-numbers policy)' if HARD_NUMBERS_ONLY else ''}. "
                    f"Ceilings not boosted (no measurement basis)."
                )
                print(f"   📐 Perimeter wall boost: {current_wall:,} -> {boosted_wall:,} sqft "
                      f"({boost_factor:.2f}x, from perimeter data)")

            return analysis  # Perimeter boost applied; skip footprint-based

    # If perimeter data existed but didn't trigger a boost (e.g. secondary space
    # supplement raised aggregated above perimeter-derived), fall through to
    # footprint-based boost which uses an independent expected-ratio calculation.

    # --- Mode 2: Footprint-based boost (fallback) ---

    footprint = _num(pi.get("footprint_sqft", 0))
    total_stories = _num(pi.get("total_stories", 0))
    total_units = _num(pi.get("total_units", 0))
    building_type = str(pi.get("building_type", "")).lower()

    # "commercial/mixed-use" contains "mixed" but is commercial — commercial takes precedence
    # unless the type also explicitly mentions "residential"/"apartment"/"condo"
    _has_commercial = "commercial" in building_type
    _has_residential_kw = any(kw in building_type
                              for kw in ("residential", "apartment", "condo"))
    is_residential = (not _has_commercial or _has_residential_kw) and any(
        kw in building_type for kw in ("residential", "mixed", "multi", "apartment"))

    if not (is_residential and footprint > 0 and total_stories >= 2 and total_units >= 4):
        return analysis

    # Count above-grade floors only. Do NOT add +1 for basement — the calibration
    # ratio (1.25) was derived from total_stories alone (85,353 / (17,004 × 4) = 1.255).
    # Adding basement inflates expected walls when footprint is already unreliable.
    paintable_floors = total_stories

    total_floor_area = footprint * paintable_floors
    current_wall = _num(agg.get("total_paintable_wall_sqft", 0))

    # Expected ratio: residential multi-family = 1.25 sqft wall per sqft floor area
    # This accounts for perimeter walls + interior partitions + corridors + closets
    # Source: Rider Painting 364 Main takeoff — 85,353 wall sqft / (17,004 fp × 4 floors) = 1.255
    # This ratio accounts for the mix of dense residential floors (higher ratio ~1.4-1.6)
    # and less dense commercial/basement floors (lower ratio ~0.5-0.8).
    expected_wall_ratio = 1.25
    expected_wall = total_floor_area * expected_wall_ratio

    actual_ratio = current_wall / total_floor_area if total_floor_area > 0 else 0

    # Boost whenever extraction is meaningfully below expected ratio.
    # Threshold at expected_ratio * 0.92 (~1.15 for 1.25 expected) handles non-deterministic
    # extraction variance: same PDF can yield 0.87x one run and 1.05x the next.
    boost_threshold = expected_wall_ratio * 0.92
    if actual_ratio < boost_threshold:
        # HARD_NUMBERS_ONLY: unlike Mode 1, this mode's target is footprint
        # × stories × a 1.25 ratio calibrated from ONE reference job (364
        # Main) — an assumption, not a measurement from this project's
        # drawings, applied to a footprint with documented ±36% extraction
        # variance. Under the policy: flag the discrepancy for RFI, price
        # only what was measured.
        if HARD_NUMBERS_ONLY:
            analysis.setdefault("notes", []).append(
                f"[Wall Cross-Check] Extracted wall area ({current_wall:,.0f} "
                f"sqft) is {actual_ratio:.2f}x floor area — residential "
                f"multi-family typically runs ~{expected_wall_ratio}x; walls "
                f"may be under-extracted. RFI REQUIRED: verify wall scope on "
                f"the unit/floor plans (footprint-ratio correction to "
                f"~{expected_wall:,.0f} sqft NOT applied under hard-numbers "
                f"policy)."
            )
            print(f"   🔒 Footprint wall boost suppressed (HARD_NUMBERS_ONLY): "
                  f"ratio {actual_ratio:.2f}x < {boost_threshold:.2f}x — "
                  f"flagged for RFI")
            return analysis

        # Boost to expected ratio
        boost_target = expected_wall
        boost_factor = boost_target / current_wall if current_wall > 0 else 1.0

        # SAFETY CAP: Limit boost factor.
        # Default 1.30x because footprint extraction is unreliable (±36% variance).
        # Elevated when secondary space supplement confirmed under-extraction via
        # room density (independent signal — safe to allow larger correction).
        MAX_BOOST_FACTOR = 1.30
        sec_supp = analysis.get("_secondary_space_supplement")
        if sec_supp and sec_supp.get("density_ratio", 1.0) < 0.70:
            MAX_BOOST_FACTOR = 1.60
        elif sec_supp and sec_supp.get("density_ratio", 1.0) < 0.85:
            MAX_BOOST_FACTOR = 1.45
        if boost_factor > MAX_BOOST_FACTOR:
            analysis.setdefault("notes", []).append(
                f"[Wall Boost Cap] Computed boost factor {boost_factor:.2f}x exceeds "
                f"max {MAX_BOOST_FACTOR}x. Capping to prevent footprint extraction error "
                f"from inflating estimate. Raw walls: {current_wall:,}, footprint: {footprint:,.0f}, "
                f"floors: {paintable_floors}."
            )
            print(f"   ⚠️  Wall boost capped: {boost_factor:.2f}x -> {MAX_BOOST_FACTOR}x "
                  f"(footprint {footprint:,.0f} may be inaccurate)")
            boost_factor = MAX_BOOST_FACTOR

        if boost_factor > 1.05:  # Only boost if meaningfully under (>5%)
            boosted_wall = round(current_wall * boost_factor)
            # Trim is perimeter-based — tracks with wall extraction completeness.
            current_trim = _num(agg.get("total_base_trim_lf", 0))
            boosted_trim = round(current_trim * boost_factor) if current_trim > 0 else current_trim

            agg["total_paintable_wall_sqft"] = boosted_wall
            agg["total_base_trim_lf"] = boosted_trim
            analysis["aggregated_totals"] = agg

            analysis.setdefault("notes", []).append(
                f"[Wall Boost] Extracted wall area ({current_wall:,} sqft) was {actual_ratio:.2f}x "
                f"floor area — expected ~{expected_wall_ratio}x for residential multi-family. "
                f"Boosted to {boosted_wall:,} sqft (factor {boost_factor:.2f}x). "
                f"Trim {current_trim:,}->{boosted_trim:,} LF. Ceilings not boosted (no measurement basis)."
            )
            print(f"   📐 Wall boost: {current_wall:,} -> {boosted_wall:,} sqft "
                  f"({boost_factor:.2f}x factor, was {actual_ratio:.2f}x floor area)")

    return analysis


def _validate_unit_multipliers(analysis):
    """Sanity-check unit-template multipliers against project_info.total_units.

    Three checks, each emits a structured note + sets a project_info flag:

      1. SUM_MISMATCH — sum of all residential-template multipliers differs
         from total_units by >10%. Either too few templates (missed unit
         types) or too many (phantom floors slipped past dedup).

      2. SINGLE_TYPE_DOMINANCE — one unit type carries >95% of the total
         multiplier when 2+ unit types were extracted. Likely a missed
         second typology.

      3. RATIO_IMPLAUSIBLE — for residential building_type, the implied
         unit_mix has 2BR/3BR carrying >35% of unit count. Most supportive
         housing / dorm / senior-living mixes are 1BR-dominant (≥80% 1BR);
         the 2026-05-28 Ridgeview run reported 24% 2BR when reality was 7%
         and the bid would have run high on a per-unit doors/trim basis if
         downstream rates differ between typologies.

    None of these reshape the multipliers — see the function docstring
    why we don't: the over-/under-counts often compensate for per-room
    dimension extraction error, so a "rebalance" can make the bid less
    accurate, not more. The real fix is OCR/vision on the T1 unit-mix
    table; this validator surfaces the issue for estimator review in the
    meantime (tracked as task #16 in the Ridgeview release plan).

    Idempotent via project_info['_unit_multipliers_validated'].
    """
    if not isinstance(analysis, dict):
        return analysis
    pi = analysis.get("project_info") or {}
    if pi.get("_unit_multipliers_validated"):
        return analysis

    bt = str(pi.get("building_type", "")).lower()
    is_residential = any(kw in bt for kw in (
        "residential", "multifamily", "multi-family", "apartment", "condo",
        "dorm", "supportive housing", "senior living", "assisted living",
        "mixed-use residential",
    ))
    total_units = int(_num(pi.get("total_units", 0)))
    if not is_residential or total_units < 4:
        pi["_unit_multipliers_validated"] = True
        analysis["project_info"] = pi
        return analysis

    # Collect per-unit-type multipliers from the rooms. A unit type's
    # multiplier is the MAX multiplier of rooms tagged with that unit_type
    # (not sum — each room emits the SAME multiplier within a template).
    type_mults = {}  # unit_type -> max multiplier seen
    for floor in analysis.get("floors", []) or []:
        for room in floor.get("rooms", []) or []:
            ut = str(room.get("unit_type", "") or "").strip()
            if not ut:
                continue
            rn = str(room.get("room_name", "") or "").lower()
            # Skip non-apartment rooms (corridors, common areas tagged with
            # a "Typical Floor" label sometimes have unit_type set).
            if any(kw in rn for kw in ("corridor", "lobby", "hall ", "stair",
                                       "vestibule", "elevator")):
                continue
            mult = int(_num(room.get("unit_multiplier", 1)) or 1)
            type_mults[ut] = max(type_mults.get(ut, 0), mult)

    if not type_mults:
        pi["_unit_multipliers_validated"] = True
        analysis["project_info"] = pi
        return analysis

    sum_mults = sum(type_mults.values())
    warnings = []

    # Check 1: sum vs total_units
    if total_units > 0:
        gap_pct = abs(sum_mults - total_units) / total_units
        if gap_pct > 0.10:
            direction = "over" if sum_mults > total_units else "under"
            warnings.append(
                f"SUM_MISMATCH: sum of unit-template multipliers is "
                f"{sum_mults} but project_info.total_units is {total_units} "
                f"({direction} by {gap_pct:.0%}). "
                f"Per-type multipliers: {dict(sorted(type_mults.items()))}."
            )

    # Check 2: single-type dominance when 2+ types extracted
    if len(type_mults) >= 2 and sum_mults > 0:
        top_type, top_mult = max(type_mults.items(), key=lambda x: x[1])
        if top_mult / sum_mults > 0.95:
            warnings.append(
                f"SINGLE_TYPE_DOMINANCE: '{top_type}' carries "
                f"{top_mult}/{sum_mults} ({top_mult/sum_mults:.0%}) of all "
                f"unit-template multipliers. A second extracted unit type "
                f"has near-zero count — possible missed typology."
            )

    # Check 3: 2BR/3BR share warrants verification. Tuned to fire on the
    # Ridgeview case (24% 2BR, true 7%) — typical multifamily is 60-90%
    # 1BR, so >20% 2BR/3BR is high enough to warrant a "verify against
    # unit-mix table" note even if it's a legitimate family-housing project.
    # False-positive cost is one extra note; false-negative cost is bidding
    # several thousand $ off on per-unit doors / trim line items.
    big_unit_mult = sum(
        m for ut, m in type_mults.items()
        if any(kw in ut.lower() for kw in ("2br", "2 br", "two bedroom",
                                            "3br", "3 br", "three bedroom"))
    )
    if sum_mults > 0:
        big_share = big_unit_mult / sum_mults
        if big_share > 0.20:
            warnings.append(
                f"VERIFY_UNIT_MIX: 2BR/3BR units carry {big_share:.0%} "
                f"of total multiplier ({big_unit_mult}/{sum_mults}). Typical "
                f"multifamily is 60-90% 1BR; please confirm against the "
                f"unit-mix table on the title sheet — vector-rendered tables "
                f"often misread on first pass. Per-type counts: "
                f"{dict(sorted(type_mults.items()))}."
            )

    if warnings:
        existing_notes = analysis.get("notes") or []
        if not isinstance(existing_notes, list):
            existing_notes = [existing_notes] if existing_notes else []
        for w in warnings:
            existing_notes.append(f"[Unit Multiplier Check] {w}")
            print(f"   ⚠️  Unit multiplier: {w}", flush=True)
        analysis["notes"] = existing_notes
        pi["_unit_multiplier_warnings"] = warnings

    pi["_unit_multipliers_validated"] = True
    analysis["project_info"] = pi
    return analysis


def _apply_residential_ceiling_floor(analysis):
    """Apply a GSF-based floor to residential ceiling SF after extraction.

    Background: per-room ceiling extraction systematically under-counts on
    dense vector-rendered architectural sets. The 2026-05-28 Ridgeview run
    extracted 32,601 SF ceiling after all dedup / corridor / supplement
    fixes, but Rider's manual takeoff and KonstructIQ both measured 42,923
    SF (= the building's gross floor area). The gap is methodology, not a
    bug: extraction sums per-room geometry, while Rider's and Konstruct's
    scope-of-work treat the entire painted-ceiling envelope as the painted
    quantity.

    This pass computes an expected ceiling SF from footprint × stories ×
    efficiency, and if extracted < expected by >10%, bumps the aggregated
    ceiling total to the expected value. Door / trim / wall totals are
    untouched (those are measured per-room with reasonable accuracy and
    have their own boost paths).

    Efficiency factor:
      * Default: RESIDENTIAL_EFFICIENCY_FULL_INTERIOR (0.97) — apartments
        plus commons painted. Matches most NY supportive housing /
        multifamily contracts including Rider's Ridgeview scope.
      * Override via project_info['_residential_efficiency'] (float in
        [0.5, 1.0]) for projects where commons are excluded — e.g. set to
        0.63 (UNITS_ONLY) for a tenant-improvement-only scope.
      * Auto-downshift to UNITS_ONLY when the finish schedule was detected
        AND it lists ACT on corridor/lobby ceilings (commons not painted
        in that case). Requires has_finish_schedule == True from the
        schedule detector AND ACT evidence in any common-area room.

    Gate: only fires for residential buildings with footprint and stories
    known. Skips when _used_footprint_fallback is already set (the
    footprint path already produced the GSF-scaled answer).

    Idempotent via analysis['_residential_ceiling_floor_applied'].
    """
    if not isinstance(analysis, dict):
        return analysis
    if analysis.get("_residential_ceiling_floor_applied"):
        return analysis
    if analysis.get("_used_footprint_fallback"):
        analysis["_residential_ceiling_floor_applied"] = True
        return analysis

    pi = analysis.get("project_info") or {}
    bt = str(pi.get("building_type", "")).lower()
    is_residential = any(kw in bt for kw in (
        "residential", "multifamily", "multi-family", "apartment", "condo",
        "dorm", "supportive housing", "senior living", "assisted living",
        "mixed-use residential",
    ))
    if not is_residential:
        analysis["_residential_ceiling_floor_applied"] = True
        return analysis

    footprint = _num(pi.get("footprint_sqft", 0))
    stories = _num(pi.get("total_stories", 0))
    if footprint <= 0 or stories <= 0:
        analysis["_residential_ceiling_floor_applied"] = True
        return analysis

    # Resolve efficiency factor.
    override = pi.get("_residential_efficiency")
    if isinstance(override, (int, float)) and 0.5 <= float(override) <= 1.0:
        efficiency = float(override)
        efficiency_source = "project override"
    else:
        # Auto-downshift if the finish schedule says commons are ACT.
        has_fs = bool(analysis.get("has_finish_schedule"))
        commons_act = False
        if has_fs:
            for floor in analysis.get("floors", []) or []:
                for room in floor.get("rooms", []) or []:
                    rn = str(room.get("room_name", "")).lower()
                    if not any(kw in rn for kw in (
                            "corridor", "hallway", " hall", "lobby",
                            "vestibule", "common room", "common area")):
                        continue
                    mat = str((room.get("materials") or {}).get("ceiling", "")).upper()
                    if mat in ("ACT", "ACOUSTIC", "ACOUSTIC TILE",
                               "DROP", "SUSPENDED"):
                        commons_act = True
                        break
                if commons_act:
                    break
        if commons_act:
            efficiency = RESIDENTIAL_EFFICIENCY_UNITS_ONLY
            efficiency_source = "finish schedule shows ACT in commons"
        else:
            efficiency = RESIDENTIAL_EFFICIENCY_FULL_INTERIOR
            efficiency_source = "default (commons painted)"

    expected_ceil = round(footprint * stories * efficiency)
    agg = analysis.setdefault("aggregated_totals", {})
    current_ceil = _num(agg.get("total_paintable_ceiling_sqft", 0))

    # Only bump when extracted is materially under expected. >10% gap is
    # the trigger — smaller gaps fall within the noise of per-room
    # measurement and don't justify overriding actual extraction.
    if current_ceil >= expected_ceil * 0.90:
        analysis["_residential_ceiling_floor_applied"] = True
        return analysis

    added = expected_ceil - current_ceil
    agg["total_paintable_ceiling_sqft"] = expected_ceil

    note = (f"[Residential Ceiling Floor] Extracted ceiling SF "
            f"({current_ceil:,}) was below the GSF-based floor "
            f"({expected_ceil:,}) for this residential building "
            f"({footprint:,.0f} SF footprint × {stories:.0f} stories × "
            f"{efficiency:.2f} efficiency from {efficiency_source}). "
            f"Bumped ceiling total by +{added:,} SF to match the floor. "
            f"Per-room extraction systematically under-counts dense "
            f"vector-rendered floor plans; this floor catches the gap "
            f"without requiring a re-extraction.")
    existing_notes = analysis.get("notes") or []
    if not isinstance(existing_notes, list):
        existing_notes = [existing_notes] if existing_notes else []
    analysis["notes"] = list(existing_notes) + [note]
    print(f"   🪟 Residential ceiling floor: {current_ceil:,} → "
          f"{expected_ceil:,} SF (+{added:,}, efficiency "
          f"{efficiency:.2f} from {efficiency_source})", flush=True)

    analysis["_residential_ceiling_floor_applied"] = True
    return analysis


def _validate_wall_area_by_perimeter(analysis):
    """
    Cross-check total wall area using per-room perimeter × ceiling height.

    The LLM extraction produces perimeter_lf and ceiling_height_feet with
    99.4% accuracy (perimeter × height == wall_area_sqft in virtually all rooms).
    This function recomputes total wall area from those dimensions and compares
    to aggregated_totals to detect:

    a) Rooms with wall_area=0 but perimeter>0 (extraction gap)
    b) Rooms misclassified as non-paintable (wall material not recognized)
    c) Aggregation math errors

    Stores results in analysis["_perimeter_cross_check"] for downstream use
    by _validate_and_boost_walls().
    """
    floors = analysis.get("floors", [])
    agg = analysis.get("aggregated_totals", {})

    if not floors or not agg:
        return analysis

    # --- Pass 1: Compute perimeter-derived wall area per room ---
    perimeter_derived_paintable = 0
    perimeter_derived_cmu = 0
    perimeter_derived_total = 0  # all wall types
    rooms_with_gap = []  # rooms where perimeter*height > 0 but wall_area == 0

    for floor in floors:
        for room in floor.get("rooms", []):
            if not room.get("in_scope", True):
                continue

            dims = room.get("dimensions", {})
            mats = room.get("materials", {})

            perimeter = _num(dims.get("perimeter_lf", 0))
            ceiling_h = _num(dims.get("ceiling_height_feet", 0))
            wall_area = _num(dims.get("wall_area_sqft", 0))
            floor_area = _num(dims.get("floor_area_sqft", 0))
            multiplier = max(1, int(_num(room.get("unit_multiplier", 1))))

            # Geometric perimeter floor: a square is the minimum-perimeter
            # rectangle for a given area, so any enclosed room of floor_area has
            # an enclosing perimeter of at least 4*sqrt(area). When the extracted
            # perimeter falls below that, the shell was under-measured — e.g. a
            # retail sales floor where only the NEW interior partitions were
            # traced, not the full landlord-shell perimeter (Five Below: traced
            # 308 LF vs ~355 LF geometric floor on the 7,887 SF sales floor).
            # This is an assumption-free lower bound derived from the MEASURED
            # floor area, so it stays within the hard-numbers policy; the boost
            # that consumes this value is separately capped at 1.30x.
            geom_perimeter = (4.0 * (floor_area ** 0.5)) if floor_area > 0 else 0
            eff_perimeter = max(perimeter, geom_perimeter)

            expected_wall = eff_perimeter * ceiling_h

            if expected_wall <= 0:
                continue

            perimeter_derived_total += expected_wall * multiplier

            # Classify by wall material (same logic as _recalculate_totals)
            wall_mat = str(mats.get("walls", "")).lower()
            if "cmu" in wall_mat:
                perimeter_derived_cmu += expected_wall * multiplier
            elif any(kw in wall_mat for kw in ("gyp", "gwb", "gypsum", "paintable",
                                                "drywall", "1hr", "2hr")):
                perimeter_derived_paintable += expected_wall * multiplier

            # Detect gap: perimeter × height gives a wall area, but
            # the extracted wall_area_sqft is 0
            if wall_area == 0 and expected_wall > 0:
                rooms_with_gap.append({
                    "room_id": room.get("room_id", ""),
                    "room_name": room.get("room_name", ""),
                    "floor": floor.get("floor_name", ""),
                    "expected_wall": expected_wall,
                    "multiplier": multiplier,
                })

    # --- Pass 2: Compare to aggregated totals ---
    current_paintable = _num(agg.get("total_paintable_wall_sqft", 0))
    current_cmu = _num(agg.get("total_cmu_wall_sqft", 0))

    notes = []

    # Report rooms with wall_area gaps
    if rooms_with_gap:
        gap_total = sum(r["expected_wall"] * r["multiplier"] for r in rooms_with_gap)
        room_names = [f"{r['room_name']} ({r['floor']})" for r in rooms_with_gap[:5]]
        notes.append(
            f"[Perimeter Cross-Check] {len(rooms_with_gap)} room(s) have "
            f"perimeter × height > 0 but wall_area_sqft = 0 "
            f"(~{gap_total:,.0f} sqft missing): {', '.join(room_names)}"
        )
        print(f"   ⚠️  Perimeter cross-check: {len(rooms_with_gap)} rooms with "
              f"wall area gap (~{gap_total:,.0f} sqft)")

    # Compare perimeter-derived paintable vs aggregated paintable
    if perimeter_derived_paintable > 0 and current_paintable > 0:
        divergence = abs(perimeter_derived_paintable - current_paintable) / current_paintable

        if divergence > 0.05:  # >5% divergence
            direction = ("under" if perimeter_derived_paintable > current_paintable
                         else "over")
            notes.append(
                f"[Perimeter Cross-Check] Perimeter-derived paintable wall area "
                f"({perimeter_derived_paintable:,.0f} sqft) differs from aggregated "
                f"total ({current_paintable:,.0f} sqft) by {divergence:.1%}. "
                f"Aggregation may {direction}-count wall area."
            )
            print(f"   📐 Perimeter cross-check: {perimeter_derived_paintable:,.0f} vs "
                  f"{current_paintable:,.0f} sqft ({divergence:.1%} divergence)")

    # --- Pass 3: Store perimeter-derived totals for downstream use ---
    analysis["_perimeter_cross_check"] = {
        "perimeter_derived_paintable_sqft": round(perimeter_derived_paintable),
        "perimeter_derived_cmu_sqft": round(perimeter_derived_cmu),
        "perimeter_derived_total_sqft": round(perimeter_derived_total),
        "aggregated_paintable_sqft": round(current_paintable),
        "aggregated_cmu_sqft": round(current_cmu),
        "divergence_pct": round(
            abs(perimeter_derived_paintable - current_paintable) / current_paintable * 100, 1
        ) if current_paintable > 0 else 0,
        "rooms_with_wall_gap": len(rooms_with_gap),
    }

    for note in notes:
        analysis.setdefault("notes", []).append(note)

    return analysis


def _normalize_room_identity(room_id, room_name):
    """
    Normalize room identification for cross-file deduplication.
    Returns a canonical (unit_key, room_type) tuple.

    Examples:
      "F3-APT301-BED2", "APT 301 Bedroom 2"       -> ("301", "bed2")
      "F3-APT301-BED",  "Apartment 301 Bedroom"    -> ("301", "bed")
      "F2-APT201-LIV",  "Unit 201 Living Room"     -> ("201", "liv")
      "F1-COMM1",       "Commercial Space 1"        -> ("", "commercial space 1")
    """
    text = room_name.lower().strip()

    # Extract unit number: "APT 301" / "Apartment 301" / "Unit 301" / "Suite 301"
    unit_match = re.search(
        r'(?:apt|apartment|unit|suite)\s*[#]?\s*(\d+)',
        text
    )
    unit_key = unit_match.group(1) if unit_match else ""

    # If no unit found in name, try extracting from room_id (F{floor}-APT{num})
    if not unit_key:
        id_match = re.search(r'APT(\d+)', room_id, re.IGNORECASE)
        unit_key = id_match.group(1) if id_match else ""

    # Strip the unit prefix to get room type
    room_type = re.sub(
        r'(?:apt|apartment|unit|suite)\s*[#]?\s*\d+\s*[-:.]?\s*',
        '', text
    ).strip()

    # Canonical room type mapping
    _type_map = {
        'bedroom': 'bed', 'bed room': 'bed', 'bed': 'bed', 'br': 'bed',
        'primary bedroom': 'pbed', 'master bedroom': 'pbed', 'master bed': 'pbed',
        'primary bed': 'pbed',
        'bathroom': 'bath', 'bath room': 'bath', 'bath': 'bath', 'ba': 'bath',
        'half bath': 'hbath', 'powder room': 'hbath',
        'living': 'liv', 'living room': 'liv', 'living/dining': 'liv',
        'living/dining/kitchen': 'ldk', 'ldk': 'ldk',
        'kitchen': 'kit', 'kit': 'kit',
        'closet': 'clo', 'clo': 'clo', 'cl': 'clo', 'walk-in closet': 'wic',
        'dining': 'din', 'dining room': 'din',
        'den': 'den', 'study': 'den', 'office': 'den',
        'laundry': 'lau', 'laundry room': 'lau',
        'entry': 'entry', 'foyer': 'entry', 'vestibule': 'entry',
        'hallway': 'hall', 'hall': 'hall', 'corridor': 'hall',
    }

    # Extract trailing number (Bedroom 1, Bedroom 2, Closet 3)
    trail_match = re.search(r'(\d+)\s*$', room_type)
    trail_num = trail_match.group(1) if trail_match else ""
    base_type = re.sub(r'\s*\d+\s*$', '', room_type).strip()

    # Try to map to canonical type
    canonical = _type_map.get(base_type, base_type)

    return (unit_key, f"{canonical}{trail_num}")


def _detail_score(room):
    """Count non-zero/non-null dimension fields as a measure of extraction quality."""
    dims = room.get("dimensions", {})
    return sum(1 for v in dims.values()
               if v is not None and v != 0 and str(v).lower() != "various")


def _extract_unit_from_room_id(room_id):
    """
    Extract unit identifier from a room_id to prevent deduplication across
    different physical units.

    Examples:
      "F1-C1A-UNIT1-LIV"     -> "C1A-UNIT1"
      "F1-C1A-UNIT2-LIV"     -> "C1A-UNIT2"
      "F2-APT201-BED1"       -> "APT201"
      "F1-LOBBY"             -> ""  (no unit)
      "F3-UNIT301-LIV"       -> "UNIT301"
    """
    if not room_id:
        return ""
    # Match patterns like UNIT1, UNIT2, APT201, APT301, SUITE101, etc.
    m = re.search(r'((?:[A-Z0-9]+-)?(?:UNIT|APT|SUITE)\d+)', room_id, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def _deduplicate_rooms(rooms):
    """
    Remove duplicate rooms from a merged list.
    Five-tier deduplication:
    1. Exact room_id match → keep higher detail_score
    2. Normalized unit+type match with dimension similarity (±10%) → keep higher detail
    3. Exact name + similar area (within 50 sqft) → keep higher detail
    3b. Same normalized type + same floor + similar wall area (±20%) for non-unit rooms
        (catches cross-chunk duplicates in single-family homes where the same room
         is extracted with slightly different names/dimensions)
    4. Cross-sheet duplicate detection — same room name or type on different source sheets
       with similar wall area (±30%). Keeps version from sheet with more rooms.

    Returns: (deduplicated_rooms, dedup_log)
    """
    # ── Pre-pass: count rooms per source sheet and detect detail sheets ──
    sheet_room_counts = {}  # sheet_name -> count of rooms
    sheet_room_names = {}   # sheet_name -> set of lowercased room names
    for room in rooms:
        sheet = (room.get("source_sheet") or "").strip()
        if not sheet or sheet.lower() in ("unknown", ""):
            continue
        sheet_room_counts[sheet] = sheet_room_counts.get(sheet, 0) + 1
        sheet_room_names.setdefault(sheet, set()).add(
            room.get("room_name", "").lower().strip())

    # Detect detail/enlarged sheets. A GENUINE enlarged-detail sheet is tiny
    # (1-2 rooms — an enlarged restroom, an enlarged stair) and every one of
    # its rooms also appears on a SUBSTANTIALLY larger main-plan sheet.
    # Kept deliberately strict: over-flagging here drops real floor-plan rooms
    # — multi-file runs were losing ~half their rooms when 3-4 room real floor
    # plans got misread as "details" of a coincidentally larger sheet.
    detail_sheets = set()
    for sheet, names in sheet_room_names.items():
        count = sheet_room_counts.get(sheet, 0)
        if count > 2:
            continue  # 3+ rooms → treat as a real floor plan, not a detail
        # Check if all names from this sheet appear on a much bigger sheet
        for other_sheet, other_names in sheet_room_names.items():
            if other_sheet == sheet:
                continue
            if sheet_room_counts.get(other_sheet, 0) < count + 4:
                continue  # Other sheet is not a substantially larger main plan
            if names and names.issubset(other_names):
                detail_sheets.add(sheet)
                break
    if detail_sheets:
        print(f"   🔍 Dedup: detected detail sheets (deprioritized): {detail_sheets}")

    seen = {}  # dedup_key -> room dict
    seen_identity = {}  # dedup_key -> (unit_key, room_type) for tier 2 lookups
    dedup_log = []

    # Helper: normalize room type for fuzzy same-floor matching
    _TYPE_ALIASES = {
        "bath": "bathroom", "half bath": "bathroom", "powder room": "bathroom",
        "full bath": "bathroom", "guest bath": "bathroom", "guest bathroom": "bathroom",
        "primary bath": "primary bathroom", "master bath": "primary bathroom",
        "master bathroom": "primary bathroom",
        "entry": "entry", "mudroom": "entry", "foyer": "entry", "vestibule": "entry",
        "entry/mudroom": "entry",
        "hall": "hallway", "corridor": "hallway", "back hall": "hallway",
        "front hall": "hallway",
        "office": "studio/office", "study": "studio/office", "den": "studio/office",
        "studio": "studio/office", "studio/office": "studio/office",
        "closet": "closet", "primary closet": "primary closet",
        "walk-in closet": "primary closet", "walk in closet": "primary closet",
    }

    def _get_room_type_normalized(name):
        n = name.lower().strip()
        # Strip trailing numbers (e.g., "Bedroom 201" → "bedroom")
        n_base = re.sub(r'\s*\d+$', '', n)
        return _TYPE_ALIASES.get(n_base, n_base)

    def _get_floor_from_rid(rid):
        """Extract floor indicator from room_id (e.g., 'F1-...' → '1')."""
        m = re.match(r'F(\d+|B|SB|R|M|PH)', rid, re.IGNORECASE)
        return m.group(1).upper() if m else ""

    for room in rooms:
        rid = room.get("room_id", "")
        rname = room.get("room_name", "")
        dims = room.get("dimensions", {})
        floor_area = _num(dims.get("floor_area_sqft", 0))
        wall_area = _num(dims.get("wall_area_sqft", 0))

        # Skip summary/aggregate entries
        if any(word in rname.lower() for word in ("multiple", "various", "summary", "combined")):
            continue
        if "various" in str(dims.get("length_feet", "")).lower():
            continue

        score = _detail_score(room)

        # TIER 1: Exact room_id match
        if rid and rid in seen:
            old_score = _detail_score(seen[rid])
            if score > old_score:
                dedup_log.append({
                    "kept": rid,
                    "removed": seen[rid].get("room_id", ""),
                    "reason": f"exact room_id match, kept higher detail ({score} vs {old_score})"
                })
                seen[rid] = room
            else:
                dedup_log.append({
                    "kept": seen[rid].get("room_id", ""),
                    "removed": rid,
                    "reason": f"exact room_id match, kept existing ({old_score} vs {score})"
                })
            continue

        # TIER 2: Normalized unit+type match with dimension similarity
        unit_key, room_type = _normalize_room_identity(rid, rname)
        # Extract base type (strip trailing digits) for fuzzy matching
        room_type_base = re.sub(r'\d+$', '', room_type)
        matched = False
        if unit_key:  # Only for rooms belonging to a numbered unit
            for existing_key, existing in list(seen.items()):
                e_unit, e_type = seen_identity.get(existing_key, ("", ""))
                if unit_key != e_unit:
                    continue
                # Match if: exact type match OR same base type with dimension match
                e_type_base = re.sub(r'\d+$', '', e_type)
                types_match = (room_type == e_type)
                types_fuzzy = (room_type_base == e_type_base and room_type_base != "")
                if not types_match and not types_fuzzy:
                    continue
                e_area = _num(existing.get("dimensions", {}).get("floor_area_sqft", 0))
                # Check dimension similarity (±10%)
                if floor_area > 0 and e_area > 0:
                    ratio = min(floor_area, e_area) / max(floor_area, e_area)
                    if ratio >= 0.90:
                        old_score = _detail_score(existing)
                        kept_id = existing.get("room_id", existing_key)
                        removed_id = rid
                        match_reason = "exact type" if types_match else f"fuzzy type ({room_type} ~ {e_type})"
                        if score > old_score:
                            seen[existing_key] = room
                            seen_identity[existing_key] = (unit_key, room_type)
                            kept_id, removed_id = rid, kept_id
                        dedup_log.append({
                            "kept": kept_id,
                            "removed": removed_id,
                            "reason": f"unit {unit_key} {match_reason}, "
                                      f"area ratio {ratio:.2f}"
                        })
                        matched = True
                        break

        if matched:
            continue

        # TIER 3: Exact name + similar area (existing behavior)
        # Guard: do NOT dedup rooms that belong to different units based on room_id.
        # e.g., F1-C1A-UNIT1-LIV and F1-C1A-UNIT2-LIV are different physical units.
        if rid:
            rid_unit = _extract_unit_from_room_id(rid)
            for existing_key, existing in seen.items():
                # If both room_ids encode a unit number, they must match
                existing_rid = existing.get("room_id", existing_key)
                existing_rid_unit = _extract_unit_from_room_id(existing_rid)
                if rid_unit and existing_rid_unit and rid_unit != existing_rid_unit:
                    continue  # Different units — not duplicates

                existing_area = _num(existing.get("dimensions", {}).get("floor_area_sqft", 0))
                if (existing.get("room_name", "").lower() == rname.lower()
                        and abs(existing_area - floor_area) < 50
                        and floor_area > 0):
                    old_score = _detail_score(existing)
                    if score > old_score:
                        seen[existing_key] = room
                    dedup_log.append({
                        "kept": seen[existing_key].get("room_id", existing_key),
                        "removed": rid,
                        "reason": f"exact name match, area diff {abs(existing_area - floor_area):.0f} sqft"
                    })
                    matched = True
                    break

            # TIER 3b: Same normalized room TYPE + same floor + similar wall area (±20%)
            # Catches cross-chunk duplicates in single-family homes where the same
            # physical room is extracted with slightly different names or dimensions.
            # e.g., "Back Hall" (216 sqft) from chunk 1 and "Back Hall" (216 sqft) from chunk 2.
            # Also catches type-aliased matches like "Entry" + "Mudroom" → same entry area,
            # or "Bathroom" extracted twice from overlapping chunk regions.
            if not matched and not unit_key:
                room_floor = _get_floor_from_rid(rid)
                room_type_norm = _get_room_type_normalized(rname)
                for existing_key, existing in seen.items():
                    existing_rid = existing.get("room_id", existing_key)
                    # Must be on the same floor
                    e_floor = _get_floor_from_rid(existing_rid)
                    if not room_floor or not e_floor or room_floor != e_floor:
                        continue
                    # Must not be different units
                    existing_rid_unit = _extract_unit_from_room_id(existing_rid)
                    if rid_unit and existing_rid_unit and rid_unit != existing_rid_unit:
                        continue
                    # Check normalized room type match
                    e_type_norm = _get_room_type_normalized(existing.get("room_name", ""))
                    if room_type_norm != e_type_norm:
                        continue
                    # Check wall area similarity (±20%) — more lenient than Tier 3
                    e_wall = _num(existing.get("dimensions", {}).get("wall_area_sqft", 0))
                    if wall_area > 0 and e_wall > 0:
                        wall_ratio = min(wall_area, e_wall) / max(wall_area, e_wall)
                        if wall_ratio >= 0.80:
                            old_score = _detail_score(existing)
                            if score > old_score:
                                seen[existing_key] = room
                            dedup_log.append({
                                "kept": seen[existing_key].get("room_id", existing_key),
                                "removed": rid,
                                "reason": f"same-floor type match ({room_type_norm}), "
                                          f"wall ratio {wall_ratio:.2f}"
                            })
                            matched = True
                            break

            # TIER 4: Cross-sheet duplicate detection
            # Same room name or normalized type on a DIFFERENT source sheet,
            # with wall area within ±30%. Keeps the version from the sheet
            # with more rooms (primary floor plan, not detail/enlarged).
            if not matched:
                room_sheet = (room.get("source_sheet") or "").strip()
                room_name_lc = rname.lower().strip()
                room_type_norm = _get_room_type_normalized(rname)
                room_floor = _get_floor_from_rid(rid)
                for existing_key, existing in list(seen.items()):
                    e_sheet = (existing.get("source_sheet") or "").strip()
                    # Must be from different sheets
                    if not room_sheet or not e_sheet or room_sheet == e_sheet:
                        continue
                    # Must be on the same floor
                    existing_rid = existing.get("room_id", existing_key)
                    e_floor = _get_floor_from_rid(existing_rid)
                    if room_floor and e_floor and room_floor != e_floor:
                        continue
                    # Must not be different units
                    existing_rid_unit = _extract_unit_from_room_id(existing_rid)
                    if rid_unit and existing_rid_unit and rid_unit != existing_rid_unit:
                        continue
                    # Name or type must match
                    e_name_lc = existing.get("room_name", "").lower().strip()
                    e_type_norm = _get_room_type_normalized(existing.get("room_name", ""))
                    name_match = (room_name_lc == e_name_lc and room_name_lc != "")
                    type_match = (room_type_norm == e_type_norm and room_type_norm != "")
                    if not name_match and not type_match:
                        continue
                    # Wall area within ±30%
                    e_wall = _num(existing.get("dimensions", {}).get("wall_area_sqft", 0))
                    if wall_area <= 0 or e_wall <= 0:
                        continue
                    wall_ratio = min(wall_area, e_wall) / max(wall_area, e_wall)
                    if wall_ratio < 0.70:
                        continue
                    # Match found — decide which to keep:
                    # Prefer sheet with more rooms (primary plan), then detail_score
                    room_sheet_count = sheet_room_counts.get(room_sheet, 0)
                    e_sheet_count = sheet_room_counts.get(e_sheet, 0)
                    room_is_detail = room_sheet in detail_sheets
                    e_is_detail = e_sheet in detail_sheets
                    old_score = _detail_score(existing)
                    keep_new = False
                    if room_is_detail and not e_is_detail:
                        keep_new = False  # Existing is from primary sheet
                    elif e_is_detail and not room_is_detail:
                        keep_new = True  # New room is from primary sheet
                    elif room_sheet_count > e_sheet_count:
                        keep_new = True  # New room is from sheet with more rooms
                    elif room_sheet_count < e_sheet_count:
                        keep_new = False
                    else:
                        keep_new = score > old_score  # Tie: keep higher detail

                    match_type = "name" if name_match else "type"
                    if keep_new:
                        dedup_log.append({
                            "kept": rid,
                            "removed": existing.get("room_id", existing_key),
                            "reason": f"cross-sheet {match_type} match "
                                      f"({room_sheet} vs {e_sheet}), "
                                      f"wall ratio {wall_ratio:.2f}, "
                                      f"kept sheet with {room_sheet_count} rooms"
                        })
                        seen[existing_key] = room
                        seen_identity[existing_key] = (unit_key, room_type)
                    else:
                        dedup_log.append({
                            "kept": existing.get("room_id", existing_key),
                            "removed": rid,
                            "reason": f"cross-sheet {match_type} match "
                                      f"({room_sheet} vs {e_sheet}), "
                                      f"wall ratio {wall_ratio:.2f}, "
                                      f"kept sheet with {e_sheet_count} rooms"
                        })
                    matched = True
                    break

            if not matched:
                seen[rid] = room
                seen_identity[rid] = (unit_key, room_type)
        else:
            # No room_id — use name as key
            key = rname.lower() or f"unnamed_{len(seen)}"
            if key not in seen:
                seen[key] = room
                seen_identity[key] = (unit_key, room_type)

    return list(seen.values()), dedup_log


# ---------------------------------------------------------------------------
# Phase 4 — Provenance Audit (hallucination detection, count reconciliation)
# ---------------------------------------------------------------------------

# Regex to parse a sheet ID like "M-201" / "S101" / "ID-3.02" → discipline prefix
_PROVENANCE_SHEET_RE = re.compile(r'^\s*([A-Z]{1,3})\s*[-.]?\s*\d', re.IGNORECASE)

# Disciplines that should NEVER be the source of a painted room
_EXCLUDED_DISCIPLINE_PREFIXES = {"S", "M", "E", "P", "C", "L", "FP", "FA"}

# Synthetic / non-sheet provenance values that are valid (set by our own pipeline)
_SYNTHETIC_PROVENANCE = {
    "room finish schedule", "building info", "schedule", "estimated",
    "secondary space template", "unit template", "footprint estimate",
}


def _audit_room_provenance(analysis, project_overview=None):
    """
    Phase 4 of the Rider workflow: re-check the merged, deduplicated room data
    for likely hallucinations and cross-validate against the project overview.

    Conservative — flags only; does not auto-remove rooms. The flags surface
    on the analysis dict for inspection / manual review.

    Three checks:
      1. **Wrong-discipline source** — rooms whose source_sheet parses to an
         excluded discipline (S/M/E/P/C/L/FP/FA). These can't be painted rooms.
      2. **Outlier wall area** — rooms with absurd wall area (> 3000 sqft for
         a single room or < 20 sqft for non-closet rooms) get flagged.
      3. **Count reconciliation** — total room count vs project_overview's
         unit_count × typical rooms-per-unit. Out-of-band ratios get flagged.

    Args:
        analysis: the merged analysis dict (after dedup + recalc).
        project_overview: optional dict from _extract_project_overview().

    Returns:
        analysis (mutated in place) with new keys:
            - "provenance_audit": {
                "wrong_discipline": [...],
                "outliers": [...],
                "count_reconciliation": {...},
                "hallucination_suspects": [...]   # union, for quick view
              }
    """
    audit = {
        "wrong_discipline": [],
        "wrong_discipline_softfailed": False,
        "wrong_discipline_softfail_reason": "",
        "outliers": [],
        "count_reconciliation": {},
        "hallucination_suspects": [],
    }
    suspect_ids = set()
    # Collect wrong-discipline matches before mutating any room. If they would
    # exclude 100% of extracted rooms (typical of small-TI permit sets that
    # use non-standard sheet naming like S01 for the architectural floor plan)
    # we soft-fail the filter instead of zeroing the estimate.
    wrong_discipline_candidates = []
    total_rooms_seen = 0

    # ---- Check 1 + 2: walk every room ----
    for floor in analysis.get("floors", []):
        floor_name = floor.get("floor_name", "?")
        for room in floor.get("rooms", []):
            total_rooms_seen += 1
            rid = room.get("room_id") or room.get("room_name", "?")
            sheet = (room.get("source_sheet") or "").strip()
            sheet_lower = sheet.lower()

            # 1. Wrong-discipline check (candidates collected; applied after triage)
            is_synthetic = any(syn in sheet_lower for syn in _SYNTHETIC_PROVENANCE)
            if sheet and not is_synthetic:
                m = _PROVENANCE_SHEET_RE.match(sheet)
                if m:
                    prefix = m.group(1).upper()
                    # Strip trailing letters one at a time to find the longest
                    # prefix in the excluded set. ("FPA101" → FPA→FP→F)
                    matched_excl = None
                    for i in range(len(prefix), 0, -1):
                        sub = prefix[:i]
                        if sub in _EXCLUDED_DISCIPLINE_PREFIXES:
                            matched_excl = sub
                            break
                    if matched_excl:
                        wrong_discipline_candidates.append((room, rid, {
                            "room_id": rid,
                            "room_name": room.get("room_name", "?"),
                            "floor": floor_name,
                            "source_sheet": sheet,
                            "discipline_prefix": matched_excl,
                            "reason": (
                                f"Sheet {sheet} parses to discipline '{matched_excl}' "
                                f"which is excluded from painting scope"
                            ),
                        }))

            # 2. Outlier wall area. Wall area lives under room["dimensions"]
            # (the top-level key never exists in the extraction schema — a
            # prior version read it and the outlier checks below never fired).
            wall_area = _num((room.get("dimensions") or {}).get("wall_area_sqft", 0))
            rname_l = (room.get("room_name") or "").lower()
            is_closet_or_small = any(
                kw in rname_l for kw in
                ("closet", "wic", "pantry", "linen", "alcove")
            )
            if wall_area > 3000:
                audit["outliers"].append({
                    "room_id": rid,
                    "room_name": room.get("room_name", "?"),
                    "floor": floor_name,
                    "wall_area_sqft": wall_area,
                    "reason": f"Wall area {wall_area:.0f} sqft exceeds 3000 sqft "
                              f"single-room threshold",
                })
                suspect_ids.add(rid)
                room["_provenance_flag"] = room.get("_provenance_flag") or "outlier_high"
            elif 0 < wall_area < 20 and not is_closet_or_small:
                audit["outliers"].append({
                    "room_id": rid,
                    "room_name": room.get("room_name", "?"),
                    "floor": floor_name,
                    "wall_area_sqft": wall_area,
                    "reason": f"Wall area {wall_area:.1f} sqft is implausibly small "
                              f"for a non-closet room",
                })
                suspect_ids.add(rid)
                room["_provenance_flag"] = room.get("_provenance_flag") or "outlier_low"

    # ---- Apply wrong-discipline triage (Check 1, deferred) ----
    # If every extracted room maps to an excluded discipline, the sheet-prefix
    # filter is almost certainly wrong (e.g. a small-TI permit set that puts
    # the architectural floor plan on "S01"). Excluding 100% of rooms zeroes
    # the entire estimate, which is worse than leaving them in scope and
    # flagging for human review. Soft-fail in that case: still record the
    # candidates in the audit, but do not mutate in_scope; emit a HIGH note
    # so a reviewer verifies the sheet labeling.
    n_candidates = len(wrong_discipline_candidates)
    softfail = (
        n_candidates > 0
        and total_rooms_seen > 0
        and n_candidates == total_rooms_seen
    )
    if softfail:
        disc_prefixes = sorted({e["discipline_prefix"] for _, _, e in wrong_discipline_candidates})
        sheets_seen = sorted({e["source_sheet"] for _, _, e in wrong_discipline_candidates})
        audit["wrong_discipline_softfailed"] = True
        audit["wrong_discipline_softfail_reason"] = (
            f"All {total_rooms_seen} extracted room(s) source to sheet(s) "
            f"{sheets_seen} which parse to excluded discipline(s) "
            f"{disc_prefixes}. Soft-failing the filter to avoid a "
            f"false-positive $0 estimate — likely non-standard sheet naming "
            f"(e.g. 'S01' used as the architectural floor plan). Verify the "
            f"sheet labels and discipline mapping manually."
        )
        for _, _, entry in wrong_discipline_candidates:
            audit["wrong_discipline"].append(entry)
        print(
            f"   ⚠️  Discipline filter SOFT-FAIL: would have excluded all "
            f"{total_rooms_seen} rooms (sheets={sheets_seen}, "
            f"disc={disc_prefixes}); keeping rooms in scope — verify sheet labels."
        )
    else:
        for room, rid, entry in wrong_discipline_candidates:
            audit["wrong_discipline"].append(entry)
            suspect_ids.add(rid)
            room["_provenance_flag"] = "wrong_discipline"
            # Wrong-discipline rooms must be excluded from aggregated_totals.
            # _recalculate_totals() filters on in_scope, so set it here.
            room["in_scope"] = False
            room["scope_exclusion_reason"] = (
                f"Wrong-discipline source sheet ({entry['source_sheet']}, "
                f"discipline '{entry['discipline_prefix']}' excluded from painting scope)"
            )

    # ---- Check 3: count reconciliation against project overview ----
    pi = analysis.get("project_info", {})
    extracted_rooms = sum(
        len(f.get("rooms", [])) for f in analysis.get("floors", [])
    )

    expected_rooms = None
    expected_basis = ""

    overview_units = 0
    if project_overview:
        overview_units = int(_num(project_overview.get("unit_count", 0)))
    pi_units = int(_num(pi.get("total_units", 0)))
    units = overview_units or pi_units

    # The 7-rooms/unit heuristic is a residential studio→3BR mix average.
    # For commercial/retail/industrial, "units" can mean tenant spaces,
    # loading docks, or other non-rooms — applying the residential ratio
    # produces false-positive over-extraction signals (e.g. 14 rooms in a
    # B&N tenant fit-out flagged as 2× expected).
    _bt = str(pi.get("building_type", "")).lower()
    _is_residential_bt = any(kw in _bt for kw in (
        "residential", "multifamily", "multi-family", "apartment",
        "condo", "townhouse", "single family", "single-family",
        "senior", "assisted", "living"
    ))

    if units > 0 and _is_residential_bt:
        # Rough heuristic: 7 rooms/unit average across studio→3BR mix
        expected_rooms = units * 7
        expected_basis = f"{units} units × 7 rooms/unit (residential heuristic)"
    elif project_overview and project_overview.get("total_gsf"):
        # ~200 GSF per room as a very loose fallback
        gsf = int(_num(project_overview.get("total_gsf", 0)))
        if gsf > 0:
            expected_rooms = max(5, gsf // 200)
            expected_basis = f"{gsf:,} GSF ÷ 200 sqft/room"
    # Non-residential without total_gsf: leave expected_rooms = None.
    # The audit will fall through to the "no_baseline" branch below
    # rather than emit a misleading over-extraction status.

    if expected_rooms:
        ratio = extracted_rooms / expected_rooms if expected_rooms else 1.0
        status = "ok"
        message = ""
        if ratio > 1.8:
            status = "likely_over_extraction"
            message = (
                f"Extracted {extracted_rooms} rooms is {ratio:.1f}× expected "
                f"({expected_rooms} rooms from {expected_basis}) — "
                f"check for duplicate buildings or hallucinated rooms"
            )
        elif ratio < 0.4:
            status = "likely_under_extraction"
            message = (
                f"Extracted {extracted_rooms} rooms is only {ratio:.1f}× expected "
                f"({expected_rooms} rooms from {expected_basis}) — "
                f"floor plans may not have been fully read"
            )
        audit["count_reconciliation"] = {
            "extracted_rooms": extracted_rooms,
            "expected_rooms": expected_rooms,
            "ratio": round(ratio, 2),
            "basis": expected_basis,
            "status": status,
            "message": message,
        }
    else:
        audit["count_reconciliation"] = {
            "extracted_rooms": extracted_rooms,
            "status": "no_baseline",
            "message": "No unit count or GSF available to reconcile against",
        }

    audit["hallucination_suspects"] = sorted(suspect_ids)
    analysis["provenance_audit"] = audit

    # ---- Print summary + add to notes ----
    n_wrong = len(audit["wrong_discipline"])
    n_out = len(audit["outliers"])
    print(f"\n🔎 Phase 4: Provenance Audit")
    print(f"   • Rooms with wrong-discipline source sheet: {n_wrong}")
    print(f"   • Wall-area outliers: {n_out}")
    cr = audit["count_reconciliation"]
    if cr.get("expected_rooms"):
        print(f"   • Room count: extracted={cr['extracted_rooms']}, "
              f"expected≈{cr['expected_rooms']} ({cr['status']})")
    if cr.get("message"):
        print(f"     {cr['message']}")
    for item in audit["wrong_discipline"][:5]:
        print(f"     ⚠️  {item['room_id']}: {item['reason']}")
    for item in audit["outliers"][:5]:
        print(f"     ⚠️  {item['room_id']}: {item['reason']}")

    notes = analysis.setdefault("notes", [])
    if n_wrong or n_out or cr.get("status", "ok") not in ("ok", "no_baseline"):
        notes.append(
            f"[Provenance Audit] {n_wrong} wrong-discipline source(s), "
            f"{n_out} outlier(s), count status: {cr.get('status', 'ok')}"
        )
    if audit.get("wrong_discipline_softfailed"):
        notes.append(
            f"[HIGH] Discipline filter soft-failed — "
            f"{audit.get('wrong_discipline_softfail_reason', '')}"
        )

    return analysis


def _normalize_scope_fields(analysis):
    """Ensure every room has in_scope (default True) and scope_exclusion_reason fields."""
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            if "in_scope" not in room:
                room["in_scope"] = True
            if "scope_exclusion_reason" not in room:
                room["scope_exclusion_reason"] = ""
    return analysis


def _load_corrections(corrections_path=None):
    """
    Load user corrections from a corrections.json file.
    Returns the corrections dict or None if no file exists.

    corrections.json format:
    {
      "project_corrections": [
        {
          "room_id": "F2-APT201-BED1",
          "override_dimensions": {"wall_area_sqft": 480},
          "override_elements": {"doors_full_paint": 2},
          "note": "Verified on-site: walls are 480 sqft"
        }
      ],
      "global_corrections": {
        "exclude_rooms": ["F0-MECH1"],
        "force_in_scope": ["F1-LOBBY"]
      }
    }
    """
    if corrections_path is None:
        corrections_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "corrections.json"
        )
    if not os.path.exists(corrections_path):
        return None

    try:
        with open(corrections_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as exc:
        print(f"   ⚠️  Could not load corrections file: {exc}")
        return None


def _apply_corrections(analysis, corrections):
    """
    Apply user corrections as post-processing overrides.
    Called after merge/dedup and before final cost calculation.
    Returns the modified analysis dict.
    """
    if not corrections:
        return analysis

    corrections_applied = []

    # Room-level corrections
    room_overrides = {
        c["room_id"]: c for c in corrections.get("project_corrections", [])
        if "room_id" in c
    }

    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            rid = room.get("room_id", "")
            if rid in room_overrides:
                override = room_overrides[rid]
                # Apply dimension overrides
                for key, val in override.get("override_dimensions", {}).items():
                    old_val = room.get("dimensions", {}).get(key)
                    room.setdefault("dimensions", {})[key] = val
                    corrections_applied.append(
                        f"Room {rid}: {key} {old_val} → {val}"
                    )
                # Apply element overrides
                for key, val in override.get("override_elements", {}).items():
                    old_val = room.get("elements", {}).get(key)
                    room.setdefault("elements", {})[key] = val
                    corrections_applied.append(
                        f"Room {rid}: {key} {old_val} → {val}"
                    )
                # Apply unit_multiplier override
                if "unit_multiplier" in override:
                    old_mult = room.get("unit_multiplier", 1)
                    room["unit_multiplier"] = override["unit_multiplier"]
                    corrections_applied.append(
                        f"Room {rid}: unit_multiplier {old_mult} → {override['unit_multiplier']}"
                    )
                # Append correction note
                if override.get("note"):
                    existing_note = room.get("notes", "")
                    room["notes"] = f"{existing_note} [Correction: {override['note']}]".strip()

    # Global corrections
    global_corr = corrections.get("global_corrections", {})

    # Exclude rooms
    for exclude_id in global_corr.get("exclude_rooms", []):
        for floor in analysis.get("floors", []):
            for room in floor.get("rooms", []):
                if room.get("room_id") == exclude_id:
                    room["in_scope"] = False
                    room["scope_exclusion_reason"] = "Excluded by corrections file"
                    corrections_applied.append(f"Room {exclude_id}: excluded by corrections")

    # Force in-scope
    for force_id in global_corr.get("force_in_scope", []):
        for floor in analysis.get("floors", []):
            for room in floor.get("rooms", []):
                if room.get("room_id") == force_id:
                    room["in_scope"] = True
                    room["scope_exclusion_reason"] = ""
                    corrections_applied.append(f"Room {force_id}: forced in-scope by corrections")

    if corrections_applied:
        analysis.setdefault("notes", []).append(
            f"[Corrections] {len(corrections_applied)} correction(s) applied from corrections.json"
        )
        analysis["corrections_applied"] = corrections_applied
        print(f"\n📝 Corrections applied: {len(corrections_applied)}")
        for c in corrections_applied[:5]:
            print(f"   • {c}")
        if len(corrections_applied) > 5:
            print(f"   ... and {len(corrections_applied) - 5} more")

    return analysis


def _extract_multiplier_from_notes(room):
    """
    Extract a unit multiplier from a room's data.
    Priority:
      1. Explicit 'unit_multiplier' field (from prompt schema)
      2. Parse from notes field (fallback for older/non-compliant responses)
    Returns an integer >= 1.
    """
    # Priority 1: explicit field
    mult = room.get("unit_multiplier")
    if isinstance(mult, (int, float)) and mult > 1:
        return int(mult)

    # Priority 2: parse from notes text
    notes = str(room.get("notes", ""))
    patterns = [
        r'multipli\w+\s+by\s+(\d+)\s+units?',        # "multiplied by 28 units"
        r'[x\u00d7]\s*(\d+)\s+units?',                 # "x 28 units" or "× 28 units"
        r'(\d+)\s+(?:identical\s+)?units?\s+total',     # "28 units total"
        r'repeated\s+(?:across\s+)?(\d+)\s+units?',     # "repeated across 28 units"
    ]
    for pattern in patterns:
        match = re.search(pattern, notes, re.IGNORECASE)
        if match:
            val = int(match.group(1))
            if 1 < val <= 500:
                return val

    return 1


def _validate_extraction(analysis, file_room_counts=None, project_overview=None):
    """
    Post-extraction validation that checks for common extraction problems
    and prints warnings.  Returns the (possibly corrected) analysis dict.

    Checks:
    1. Building has expected number of floors based on total_stories
    2. Multiplied unit count roughly matches total_units from project_info
    3. Wall area is reasonable for the building footprint and story count
    4. No template groups accidentally merged with physical floors
    5. Chunk tracking data shows no failed chunks
    6. Room density (rooms per unit for multi-family, rooms per floor for commercial)
    7. Floor plan file coverage (how many floor plan files returned 0 rooms)

    Args:
        analysis: the merged/recalculated analysis dict
        file_room_counts: optional dict of {filename: rooms_found} for per-file checks
    """
    pi = analysis.get("project_info", {})
    floors = analysis.get("floors", [])
    warnings = []

    # --- Check 1: Floor count vs total_stories ---
    total_stories = pi.get("total_stories", 0)
    if isinstance(total_stories, (int, float)) and total_stories > 0:
        # Count only physical floors (not template groups)
        physical_floors = []
        template_floors = []
        for f in floors:
            fname = f.get("floor_name", "")
            fkey = _normalize_floor_key(fname)
            if fkey.startswith("T_"):
                template_floors.append(fname)
            else:
                physical_floors.append(fname)

        if len(physical_floors) < total_stories:
            warnings.append(
                f"Building has {total_stories} stories but only {len(physical_floors)} "
                f"physical floors extracted: {physical_floors}. "
                f"Missing floors may indicate incomplete extraction."
            )

    # --- Check 2: Unit count validation ---
    total_units_raw = pi.get("total_units", 0)
    total_units = 0
    if isinstance(total_units_raw, (int, float)):
        total_units = int(total_units_raw)
    elif isinstance(total_units_raw, str):
        um = re.search(r'(\d+)', str(total_units_raw))
        total_units = int(um.group(1)) if um else 0

    building_type = str(pi.get("building_type", "")).lower()
    is_multiunit = any(kw in building_type for kw in ("multi", "mixed", "apartment", "condo", "residential"))

    # Flag if a multi-unit building couldn't determine unit count
    if is_multiunit and total_units == 0:
        warnings.append(
            f"Multi-unit building ({pi.get('building_type', 'unknown')}) but total_units "
            f"could not be determined (got: {total_units_raw!r}). This may cause "
            f"incorrect unit_multiplier values in template rooms."
        )

    if isinstance(total_units, (int, float)) and total_units > 0:
        # Count effective multiplied units from template rooms
        multiplied_units = 0
        unit_types_found = {}
        for f in floors:
            for r in f.get("rooms", []):
                ut = r.get("unit_type", "")
                mult = r.get("unit_multiplier", 1)
                if isinstance(mult, (int, float)) and mult > 1 and ut:
                    # Only count the first room of each unit type (avoid double-counting
                    # bedrooms, bathrooms etc within same unit type)
                    if ut not in unit_types_found:
                        unit_types_found[ut] = int(mult)
                        multiplied_units += int(mult)

        if multiplied_units > 0:
            ratio = multiplied_units / total_units
            if ratio < 0.7:
                warnings.append(
                    f"Building has {total_units} total units but extraction found "
                    f"only {multiplied_units} multiplied units ({', '.join(f'{k}={v}' for k, v in unit_types_found.items())}). "
                    f"Some unit types may be missing from extraction."
                )
            elif ratio > 1.5:
                warnings.append(
                    f"Building has {total_units} total units but extraction produced "
                    f"{multiplied_units} multiplied units ({', '.join(f'{k}={v}' for k, v in unit_types_found.items())}). "
                    f"Possible double-counting of units across floors."
                )

    # --- Check 3: Wall area sanity check ---
    footprint = pi.get("footprint_sqft", 0)
    if isinstance(footprint, (int, float)) and footprint > 0 and isinstance(total_stories, (int, float)) and total_stories > 0:
        # Rough heuristic: total wall area should be at least
        # perimeter × ceiling_height × stories for the shell
        est_side = footprint ** 0.5
        est_perimeter = 4 * est_side
        est_min_wall = est_perimeter * 9 * total_stories * 0.5  # 50% of gross shell
        total_wall = sum(
            _num(r.get("dimensions", {}).get("wall_area_sqft", 0))
            * max(1, _num(r.get("unit_multiplier", 1)))
            for f in floors for r in f.get("rooms", [])
        )
        if total_wall > 0 and total_wall < est_min_wall * 0.3:
            warnings.append(
                f"Total extracted wall area ({total_wall:,.0f} sqft) seems very low "
                f"for a {total_stories}-story building with {footprint:,.0f} sqft footprint. "
                f"Expected at least {est_min_wall:,.0f} sqft."
            )

    # --- Check 4: Chunk tracking ---
    chunk_tracking = analysis.get("_chunk_tracking", {})
    if chunk_tracking.get("chunks_failed"):
        failed = chunk_tracking["chunks_failed"]
        total = chunk_tracking.get("total_chunks", "?")
        warnings.append(
            f"PDF chunks {failed} of {total} failed during processing. "
            f"Data from those pages is MISSING from this estimate."
        )

    # --- Check 5: Per-floor wall LF validation ---
    # For multi-unit residential buildings, each floor should have reasonable wall LF
    if is_multiunit and total_units > 0:
        for f in floors:
            fname = f.get("floor_name", "")
            fkey = _normalize_floor_key(fname)
            if fkey.startswith("T_"):
                continue  # skip template groups — check physical floors only

            floor_wall_lf = 0
            floor_rooms = 0
            for r in f.get("rooms", []):
                perimeter = _num(r.get("dimensions", {}).get("perimeter_lf", 0))
                mult = max(1, _num(r.get("unit_multiplier", 1)))
                floor_wall_lf += perimeter * mult
                floor_rooms += 1

            # Only flag floors that have rooms but suspiciously low wall LF
            # A multi-unit floor with 5+ rooms should have at least 800 LF
            if floor_rooms >= 3 and floor_wall_lf > 0 and floor_wall_lf < 800:
                warnings.append(
                    f"Floor '{fname}' has {floor_rooms} rooms but only {floor_wall_lf:,.0f} LF "
                    f"of walls (effective). Multi-unit floors typically have 2,500-4,000 LF. "
                    f"Rooms may be missing interior partitions (closets, bathrooms, corridors)."
                )

    # --- Check 6: Room density validation ---
    # For multi-unit residential: expect 5-7 rooms per unit
    # For commercial: expect >= 3 rooms per floor for larger buildings
    effective_rooms = 0
    for f in floors:
        for r in f.get("rooms", []):
            if r.get("in_scope", True):
                effective_rooms += max(1, int(_num(r.get("unit_multiplier", 1))))

    if is_multiunit and total_units >= 4 and effective_rooms > 0:
        rooms_per_unit = effective_rooms / total_units

        if rooms_per_unit < 3:
            warnings.append(
                f"[HIGH] Room density severely low: {effective_rooms} effective rooms / "
                f"{total_units} units = {rooms_per_unit:.1f} rooms/unit. "
                f"Expected 5-7 rooms/unit (living, kitchen, bedroom(s), bathroom, closets). "
                f"Many rooms likely missing from extraction."
            )
        elif rooms_per_unit < 4:
            warnings.append(
                f"[MEDIUM] Room density low: {effective_rooms} effective rooms / "
                f"{total_units} units = {rooms_per_unit:.1f} rooms/unit. "
                f"Expected 5-7 rooms/unit. Some rooms may be missing."
            )
        elif rooms_per_unit > 10:
            warnings.append(
                f"[LOW] Room density high: {effective_rooms} effective rooms / "
                f"{total_units} units = {rooms_per_unit:.1f} rooms/unit. "
                f"Possible over-extraction or double-counting."
            )

    elif not is_multiunit and isinstance(total_stories, (int, float)) and total_stories > 0:
        rooms_per_floor = effective_rooms / max(1, total_stories)
        footprint_check = _num(pi.get("footprint_sqft", 0))

        if footprint_check > 5000 and rooms_per_floor < 3:
            warnings.append(
                f"[MEDIUM] Commercial room density low: {effective_rooms} rooms / "
                f"{int(total_stories)} floors = {rooms_per_floor:.1f} rooms/floor "
                f"for {footprint_check:,.0f} sqft building. Rooms may be missing."
            )

    # --- Check 7: Floor plan file coverage ---
    # In multi-file mode, check how many floor plan files returned 0 rooms
    if file_room_counts:
        fp_files = {fn: cnt for fn, cnt in file_room_counts.items()
                    if _is_floor_plan_file(fn)}
        zero_room_fps = [fn for fn, cnt in fp_files.items() if cnt == 0]

        if fp_files and len(zero_room_fps) > len(fp_files) * 0.5:
            warnings.append(
                f"[HIGH] {len(zero_room_fps)}/{len(fp_files)} floor plan files "
                f"returned 0 rooms: {', '.join(sorted(zero_room_fps)[:5])}{'...' if len(zero_room_fps) > 5 else ''}. "
                f"Non-deterministic extraction likely under-counted this project."
            )
        elif zero_room_fps:
            warnings.append(
                f"[MEDIUM] {len(zero_room_fps)} floor plan file(s) returned 0 rooms: "
                f"{', '.join(sorted(zero_room_fps)[:3])}{'...' if len(zero_room_fps) > 3 else ''}. "
                f"Rooms from those files are missing."
            )

    # --- Check 8: Per-page under-extraction within a chunk ---
    # Catches the silent-truncation pattern where Claude returns rooms for
    # some pages of a multi-page chunk but skips others. Diagnoses cases like
    # King.pdf where 1st/2nd-floor plans yielded 0 rooms while their
    # chunk-mates (lower level, 3rd floor) yielded 26 and 14 rooms.
    chunk_ranges = chunk_tracking.get("chunk_page_ranges") or []
    if chunk_ranges:
        rooms_by_page = {}
        for f in floors:
            for r in f.get("rooms", []):
                sp = r.get("source_page")
                if isinstance(sp, (int, float)) and sp > 0:
                    rooms_by_page[int(sp)] = rooms_by_page.get(int(sp), 0) + 1

        suspect_pages = []
        for cr in chunk_ranges:
            ps = cr.get("page_start")
            pe = cr.get("page_end")
            if not isinstance(ps, int):
                continue
            if pe is None:
                pe = ps  # single-page or last chunk with unknown end
            chunk_page_list = list(range(ps, pe + 1))
            if len(chunk_page_list) < 2:
                continue
            yields = [(p, rooms_by_page.get(p, 0)) for p in chunk_page_list]
            with_rooms = [p for p, n in yields if n >= 3]
            zero_rooms = [p for p, n in yields if n == 0]
            if with_rooms and zero_rooms:
                suspect_pages.extend(zero_rooms)

        if suspect_pages:
            warnings.append(
                f"[HIGH] {len(suspect_pages)} page(s) returned 0 rooms while "
                f"their chunk-mates yielded data — likely Claude truncated room "
                f"extraction mid-chunk: pages {sorted(suspect_pages)[:10]}"
                f"{'...' if len(suspect_pages) > 10 else ''}. "
                f"Re-run with --image-fallback or smaller chunks for these pages."
            )

    # --- Check 9: Room SF sum vs cover-sheet declared work area ---
    # Safety net for the Dobbin Rd 2026-02 failure mode: when a renovation sheet
    # shows EXISTING + PROPOSED side by side and the viewport selector picks the
    # wrong one, the extracted rooms sum to a much smaller area than the cover
    # sheet declares. Tolerance is wide (85-120%) because partition thickness,
    # mechanical chases, and the difference between gross/net SF account for
    # routine ~10% drift. A miss outside that band is almost always a viewport
    # or floor-coverage problem, not a measurement-rounding problem.
    declared_area = 0
    declared_source = ""
    if project_overview:
        for key in ("total_gsf", "work_floor_area_sqft"):
            v = _num(project_overview.get(key, 0))
            if v > 0:
                declared_area = v
                declared_source = key
                break
    if declared_area > 0:
        extracted_area = sum(
            _num(r.get("dimensions", {}).get("floor_area_sqft", 0))
            * max(1, _num(r.get("unit_multiplier", 1)))
            for f in floors for r in f.get("rooms", [])
        )
        if extracted_area > 0:
            ratio = extracted_area / declared_area
            if ratio < 0.85 or ratio > 1.20:
                is_reno = bool(project_overview.get("is_renovation"))
                hint = (
                    "On renovation jobs this usually means the EXISTING viewport "
                    "was measured instead of the PROPOSED. Re-check viewport "
                    "selection on sheets titled 'EXISTING & PROPOSED'."
                    if is_reno and ratio < 0.85
                    else "Re-check floor-plan coverage and unit multipliers."
                )
                warnings.append(
                    f"[HIGH] Extracted room area ({extracted_area:,.0f} sqft) is "
                    f"{ratio:.0%} of cover-sheet {declared_source} "
                    f"({declared_area:,.0f} sqft). {hint}"
                )

    # Print all warnings
    if warnings:
        print(f"\n{'='*60}")
        print(f"⚠️  EXTRACTION VALIDATION — {len(warnings)} WARNING(S)")
        print(f"{'='*60}")
        for i, w in enumerate(warnings, 1):
            print(f"   {i}. {w}")
        print(f"{'='*60}")
        # Also add warnings to analysis notes
        for w in warnings:
            analysis.setdefault("notes", []).append(f"[VALIDATION WARNING] {w}")

    return analysis


def _validate_building_inventory(analysis, building_inventory, file_building_counts=None):
    """
    Post-analysis validation: compare extracted rooms against the building
    inventory from the index pages.  Auto-scales unit_multiplier for rooms
    whose building type appears N times in the inventory but whose current
    multiplier doesn't reflect that.

    IMPORTANT: Does NOT double-multiply rooms already scaled by the
    filename-based parser (file_building_counts).

    Args:
        analysis: merged analysis dict with floors/rooms
        building_inventory: dict from _extract_building_inventory()
        file_building_counts: dict {pdf_path: building_count} from filename parser

    Returns:
        analysis dict (possibly modified in-place)
    """
    if not building_inventory or not building_inventory.get("buildings"):
        return analysis

    if file_building_counts is None:
        file_building_counts = {}

    # Determine which building types have count > 1 from the inventory
    # Also aggregate sub-types: C1-A (2) + C1-B (2) → C1 (total 4 = 2 buildings × 2 units)
    inventory_types = {}  # type_code → {count, units_per_building, name}
    raw_types = {}        # exact code → count (before aggregation)
    for b in building_inventory["buildings"]:
        code = b.get("building_type_code", "").upper().strip()
        count = b.get("count", 1)
        units_per = b.get("units_per_building", 1) or 1
        if code:
            raw_types[code] = {
                "count": count,
                "units_per_building": units_per,
                "name": b.get("building_name", code),
            }

    # Aggregate duplex sub-types: if we have C1-A and C1-B (or C1A/C1B),
    # they're two halves of one duplex building type "C1"
    # Group by base type code (strip trailing -A/-B/A/B)
    base_groups = {}  # base_code → list of (code, info)
    for code, info in raw_types.items():
        # Strip trailing -A, -B, A, B to find base type
        base = re.sub(r'[-_]?[AB]$', '', code)
        if base not in base_groups:
            base_groups[base] = []
        base_groups[base].append((code, info))

    for base_code, variants in base_groups.items():
        if len(variants) >= 2:
            # Multiple sub-types (e.g., C1-A + C1-B) → aggregate as one building type
            # The total building count is the max of any sub-type count
            # (C1-A has 2 and C1-B has 2 means 2 duplex buildings, not 4)
            # But we need the per-unit multiplier: each unit type appears in each building
            max_count = max(v[1]["count"] for v in variants)
            total_units = sum(v[1]["count"] for v in variants)
            all_names = [v[1]["name"] for v in variants]
            combined_name = f"{base_code} Duplex ({', '.join(c for c, _ in variants)})"

            # Add the base type with the aggregated building count
            inventory_types[base_code] = {
                "count": max_count,
                "units_per_building": len(variants),
                "name": combined_name,
            }
            # Also keep each sub-type so rooms can match directly
            for code, info in variants:
                if info["count"] > 1:
                    inventory_types[code] = info
        else:
            # Single type (no A/B variants)
            code, info = variants[0]
            if info["count"] > 1:
                inventory_types[code] = info

    if not inventory_types:
        return analysis

    # Check if filename parser already applied building multipliers to any file
    any_file_scaled = any(c > 1 for c in file_building_counts.values())

    corrections_applied = []
    rooms_checked = 0

    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            rooms_checked += 1
            room_id = room.get("room_id", "")
            source_file = room.get("source_file", "")
            current_mult = room.get("unit_multiplier", 1)
            if not isinstance(current_mult, (int, float)) or current_mult < 1:
                current_mult = 1

            # Check if this room's source file was already scaled by filename parser
            source_path = None
            if source_file and file_building_counts:
                for path, count in file_building_counts.items():
                    if os.path.basename(path) == source_file:
                        source_path = path
                        break
                if source_path and file_building_counts.get(source_path, 1) > 1:
                    # Already scaled by filename parser — skip to avoid double-multiply
                    continue

            # Try to match this room to a building type from the inventory
            room_id_upper = room_id.upper() if room_id else ""
            # Normalize hyphens for matching (C1-A → C1A, C2-B → C2B)
            room_id_normalized = room_id_upper.replace("-", "")
            floor_name = floor.get("floor_name", "").upper()
            floor_name_normalized = floor_name.replace("-", "")
            matched_type = None

            # Sort type codes longest first so specific matches (C1-A) beat general (C1)
            sorted_codes = sorted(inventory_types.keys(), key=len, reverse=True)
            for type_code in sorted_codes:
                code_normalized = type_code.replace("-", "")
                # Match by type code in room_id (e.g., "C1" in "F1-C1A-UNIT1-LIV")
                if code_normalized in room_id_normalized or type_code in room_id_upper:
                    matched_type = type_code
                    break
                # Also check floor name (e.g., "VILLA TYPE C1")
                if code_normalized in floor_name_normalized or type_code in floor_name:
                    matched_type = type_code
                    break

            # Fallback: if no type-code match, try keyword matching from building names
            if not matched_type:
                for type_code, info in inventory_types.items():
                    bldg_name_upper = info.get("name", "").upper()
                    # Extract meaningful keywords from building name
                    # (skip generic words like "building", "type", "duplex")
                    _BLDG_KEYWORDS = ("VILLA", "CARRIAGE", "COTTAGE", "CLUBHOUSE",
                                      "TOWNHOME", "TOWNHOUSE", "MANOR", "LODGE",
                                      "BUNGALOW", "PENTHOUSE", "GARDEN")
                    for kw in _BLDG_KEYWORDS:
                        if kw in bldg_name_upper:
                            # Check if this keyword appears in room_id or floor_name
                            if kw in room_id_upper or kw in floor_name:
                                matched_type = type_code
                                break
                    if matched_type:
                        break

            if matched_type:
                info = inventory_types[matched_type]
                expected_building_count = info["count"]
                units_per = info["units_per_building"]

                # The room's unit_multiplier should reflect the total count of
                # this room across ALL identical buildings.
                # e.g., if there are 8 buildings each with 2 units (C1-A and C1-B),
                # a room in C1-A should have multiplier = 8 (one unit type per building).
                # The extraction should already handle units within a building via
                # separate room entries (C1A-UNIT1 vs C1B-UNIT1).
                #
                # If the current multiplier is 1 but there are 8 buildings,
                # it means extraction didn't account for identical buildings.
                if current_mult < expected_building_count:
                    new_mult = expected_building_count
                    room["unit_multiplier"] = new_mult
                    corrections_applied.append(
                        f"Room {room_id}: multiplier {current_mult} → {new_mult} "
                        f"({info['name']}: {expected_building_count} buildings)"
                    )

    if corrections_applied:
        print(f"\n🏗️  Building Inventory Validation: {len(corrections_applied)} room(s) scaled")
        for c in corrections_applied[:10]:
            print(f"   • {c}")
        if len(corrections_applied) > 10:
            print(f"   ... and {len(corrections_applied) - 10} more")
        analysis.setdefault("notes", []).append(
            f"[Building Inventory] Auto-scaled {len(corrections_applied)} rooms based on "
            f"index page inventory ({building_inventory['total_buildings']} buildings detected)"
        )
    else:
        print(f"\n🏗️  Building Inventory Validation: checked {rooms_checked} rooms — "
              f"all multipliers already correct")

    return analysis


_FLOOR_RANGE_RE = re.compile(
    r'(?:level|levels|floor|floors)\s*\(?\s*(\d+)\s*[-–to]+\s*(\d+)',
    re.IGNORECASE,
)
_FLOOR_SINGLE_RE = re.compile(
    r'(?:level|levels|floor|floors)\s+(\d+)',
    re.IGNORECASE,
)
# Ordinal-numeric in front: "1st Floor", "2nd Floor", "10th Floor".
# The original two regexes above only matched the word-first form
# ("Floor 2", "Floors 2-9") used by the Waverly architect. Coppola
# Associates and many other firms put the ordinal first ("2nd Floor"),
# which left ~all of Ridgeview unparseable — see incident_2026-05-28.
_FLOOR_ORDINAL_NUM_RE = re.compile(
    r'(\d+)(?:st|nd|rd|th)\s+(?:level|floor)',
    re.IGNORECASE,
)
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_FLOOR_ORDINAL_WORD_RE = re.compile(
    r'\b(' + '|'.join(_ORDINAL_WORDS.keys()) + r')\s+(?:level|floor)',
    re.IGNORECASE,
)
# Parenthetical multi-floor template names — e.g.
#   "Typical Residential Floors (2nd & 3rd)"
#   "(2nd, 3rd & 4th)"
# Match any parenthesized chunk that contains at least one ordinal-numeric
# ("Nth"), then pick out every ordinal-numeric inside it.
_FLOOR_PAREN_COMPOUND_RE = re.compile(
    r'\([^)]*?\d+(?:st|nd|rd|th)[^)]*\)',
    re.IGNORECASE,
)
_ORDINAL_NUM_BARE_RE = re.compile(r'(\d+)(?:st|nd|rd|th)', re.IGNORECASE)


def _parse_floor_range(name):
    """Return the set of physical-floor numbers a floor's name covers.

    Examples:
      'Typical Residential Floors (Levels 1-7)' → {1,2,3,4,5,6,7}
      'Typical Residential Levels (Floors 2-9)' → {2,3,4,5,6,7,8,9}
      'Level 0 - Dining'                        → {0}
      '1st Floor', '2nd Floor', '10th Floor'    → {1}, {2}, {10}
      'First Floor', 'Second Floor'             → {1}, {2}
      'Typical Residential Floors (2nd & 3rd)'  → {2, 3}
      '3rd Floor - Typical to 2nd Floor'        → {2, 3}
      'Penthouse', 'Basement'                   → set()  (unparseable, won't dedupe)

    Unparseable floor names return an empty set, which means the dedup
    pass leaves them alone — safer to keep a possibly-unique floor than
    to merge two semantically-different ones.
    """
    if not name:
        return set()
    found = set()
    # Parenthetical compounds like "(2nd & 3rd)" first — collect every
    # ordinal-numeric inside any paren chunk that has at least one.
    for m in _FLOOR_PAREN_COMPOUND_RE.finditer(name):
        for om in _ORDINAL_NUM_BARE_RE.finditer(m.group()):
            n = int(om.group(1))
            if 0 < n <= 50:
                found.add(n)
    # Word-first range, e.g. "Floors 2-9"
    if m := _FLOOR_RANGE_RE.search(name):
        a, b = int(m.group(1)), int(m.group(2))
        lo, hi = min(a, b), max(a, b)
        # Sanity: an architectural set with floors 1..50+ is plausible
        # but a parsed range >25 is more likely a misparse (e.g., room
        # numbers being treated as floor numbers). Bail.
        if hi - lo <= 25:
            found |= set(range(lo, hi + 1))
    # Word-first single, e.g. "Floor 2"
    if m := _FLOOR_SINGLE_RE.search(name):
        found.add(int(m.group(1)))
    # Ordinal-numeric, e.g. "2nd Floor", "10th Floor" — anywhere in the name.
    # Using finditer so "3rd Floor - Typical to 2nd Floor" yields {2, 3}.
    for m in _FLOOR_ORDINAL_NUM_RE.finditer(name):
        n = int(m.group(1))
        if 0 < n <= 50:
            found.add(n)
    # Ordinal-word, e.g. "First Floor", "Second Floor"
    for m in _FLOOR_ORDINAL_WORD_RE.finditer(name):
        found.add(_ORDINAL_WORDS[m.group(1).lower()])
    return found


_COMMON_AREA_ROOM_KEYWORDS = (
    "corridor", "hallway", " hall", "lobby", "vestibule",
    "common room", "common area", "elevator lobby",
)
# Building types that genuinely use ACT in corridors — keep model's call.
_COMMERCIAL_BUILDING_KEYWORDS = (
    "office", "retail", "commercial", "healthcare", "hospital", "clinic",
    "hospitality", "hotel", "warehouse", "industrial",
)


def _fix_residential_corridor_ceilings(analysis):
    """Flip ACT-defaulted corridor/lobby ceilings back to painted GYP for
    residential buildings when there is no explicit ACT evidence.

    The 2026-05-28 Ridgeview run lost ~2,900 sqft of corridor ceiling because
    the per-room extraction prompt told the model that public corridors
    "ALMOST ALWAYS have ACT ceilings — do NOT assume painted". That guidance
    is correct for commercial but wrong for residential supportive housing,
    multifamily, dorms, and similar building types where painted GYP is the
    corridor norm. The prompt has been corrected to branch on building_type,
    but this safety net catches:
      • Re-runs of result JSON produced before the prompt fix.
      • Any future prompt regression.
      • The case where the model defaults ACT before reading building_type.

    A corridor/lobby ceiling is "ACT-defaulted" when:
      1. building_type indicates residential AND
      2. materials.ceiling is empty/ACT AND ceiling_painted is false AND
      3. notes do NOT explicitly mention ACT/grid/RCP evidence AND
      4. dimensions.floor_area_sqft > 0 (so we have something to paint).

    When all four hold, flip ceiling_painted to true, set ceiling material
    to "GYP", populate ceiling_area_sqft from floor_area_sqft, and append
    an audit note. Wallcovering, stained wood, etc. are unaffected.

    Idempotent via analysis['_residential_corridor_ceiling_fixed'].
    """
    if not isinstance(analysis, dict):
        return analysis
    if analysis.get('_residential_corridor_ceiling_fixed'):
        return analysis

    pi = analysis.get('project_info') or {}
    bt = str(pi.get('building_type', '')).lower()
    is_residential = any(kw in bt for kw in (
        "residential", "multifamily", "multi-family", "apartment", "condo",
        "dorm", "supportive housing", "senior living", "assisted living",
        "mixed-use residential",
    ))
    is_commercial = any(kw in bt for kw in _COMMERCIAL_BUILDING_KEYWORDS) \
                    and not is_residential
    # Mixed-use that isn't clearly residential or commercial: don't touch.
    if not is_residential or is_commercial:
        analysis['_residential_corridor_ceiling_fixed'] = True
        return analysis

    fixed_count = 0
    fixed_sqft = 0
    for floor in analysis.get('floors', []) or []:
        for room in floor.get('rooms', []) or []:
            rn = str(room.get('room_name', '')).lower()
            if not any(kw in rn for kw in _COMMON_AREA_ROOM_KEYWORDS):
                continue
            materials = room.setdefault('materials', {})
            ceiling_mat = str(materials.get('ceiling', '')).upper().strip()
            already_painted = bool(materials.get('ceiling_painted'))
            if already_painted:
                continue
            # If the model has explicit ACT evidence in the notes, respect it.
            notes_lc = str(room.get('notes', '') or '').lower()
            has_explicit_act = (
                ('act ' in notes_lc and 'grid' in notes_lc)
                or 'reflected ceiling plan' in notes_lc
                or 'rcp shows act' in notes_lc
                or 'act per finish schedule' in notes_lc
            )
            if has_explicit_act:
                continue
            # Material must be empty / unknown / ACT (not e.g. DRYFALL, exposed).
            if ceiling_mat and ceiling_mat not in ("", "ACT", "ACOUSTIC",
                                                    "ACOUSTIC TILE", "DROP",
                                                    "SUSPENDED"):
                continue
            dims = room.setdefault('dimensions', {})
            floor_area = _num(dims.get('floor_area_sqft', 0))
            if floor_area <= 0:
                continue

            # Flip it.
            materials['ceiling'] = 'GYP'
            materials['ceiling_painted'] = True
            if _num(dims.get('ceiling_area_sqft', 0)) == 0:
                dims['ceiling_area_sqft'] = floor_area
            mult = room.get('unit_multiplier', 1) or 1
            fixed_count += 1
            fixed_sqft += floor_area * mult

    if fixed_count:
        note = (f"[Residential Corridor Ceiling Fix] {fixed_count} corridor/lobby "
                f"room(s) had ACT-defaulted ceilings overridden to painted GYP "
                f"(~{fixed_sqft:,.0f} sqft effective area added). "
                f"Residential corridors almost always use painted gypsum; "
                f"the original extraction defaulted to ACT due to a "
                f"commercial-biased prompt rule.")
        existing_notes = analysis.get('notes') or []
        if not isinstance(existing_notes, list):
            existing_notes = [existing_notes] if existing_notes else []
        analysis['notes'] = list(existing_notes) + [note]
        print(f"   🛠  {note}", flush=True)

    analysis['_residential_corridor_ceiling_fixed'] = True
    return analysis


def _dedupe_overlapping_template_floors(analysis):
    """Detect and merge template floors whose floor-name ranges overlap.

    Background — 2026-05-08 Waverly investigation: chunked extraction over
    a multi-sheet DD-scale PDF produces per-chunk template floors that
    look distinct by name but cover the same physical floors. Example
    from Waverly Final ($3.46M output, suspected 2-3× over-counted):

      'Typical Residential Floors (Levels 1-7)'  — 32 rooms (richest)
      'Typical Residential Levels (Floors 2-9)'  — 16 rooms (overlaps)
      'Typical Residential Units (Levels 1-10)'  — 4 rooms (overlaps)

    All three describe the same residential block; Claude saw it on
    sheets A1.04, A1.21, and A-102 respectively. The downstream
    aggregator sums their multipliers, inflating wall_sqft, doors,
    trim, etc. by ~3×.

    Heuristic:
      1. For each floor, parse a numeric floor-range from floor_name
      2. Pairwise Jaccard on those ranges; pairs with >50% overlap go
         in the same group (transitively)
      3. In each multi-floor group, KEEP the floor with the most rooms
         (Waverly: 32 > 16 > 4) and DROP the others entirely
      4. Append a 'notes' entry describing what was dropped, so the
         decision is auditable in the proposal output

    Idempotent — sets analysis['_template_floors_deduped'] = True after
    one run so it's safe to call from inside _recalculate_totals (which
    fires multiple times in some pipelines).

    Returns the analysis dict (mutated in place; also returned for chain).
    """
    if not isinstance(analysis, dict):
        return analysis
    if analysis.get('_template_floors_deduped'):
        return analysis

    floors = analysis.get('floors') or []
    if len(floors) < 2:
        analysis['_template_floors_deduped'] = True
        return analysis

    # Parse each floor's numeric range. Floors with no parseable range
    # are excluded from dedup consideration entirely.
    parsed = []
    for i, f in enumerate(floors):
        rng = _parse_floor_range(f.get('floor_name', ''))
        if rng:
            parsed.append((i, rng))

    if len(parsed) < 2:
        analysis['_template_floors_deduped'] = True
        return analysis

    # Build dedup groups by union-find: any two floors with Jaccard > 0.5
    # land in the same group (transitively).
    OVERLAP_THRESHOLD = 0.5
    parent = {i: i for i, _ in parsed}

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    for ai in range(len(parsed)):
        i, ri = parsed[ai]
        for aj in range(ai + 1, len(parsed)):
            j, rj = parsed[aj]
            if not ri or not rj:
                continue
            jaccard = len(ri & rj) / len(ri | rj)
            if jaccard > OVERLAP_THRESHOLD:
                _union(i, j)

    # Collect groups
    groups = {}
    for i, _ in parsed:
        root = _find(i)
        groups.setdefault(root, []).append(i)
    multi_floor_groups = [g for g in groups.values() if len(g) > 1]

    if not multi_floor_groups:
        analysis['_template_floors_deduped'] = True
        return analysis

    # In each group, pick canonical = floor with the most rooms (and as
    # tiebreaker, the floor whose multipliers sum highest — i.e., richest
    # extraction). Drop the others.
    floors_to_drop = set()
    dedup_notes = []
    for group in multi_floor_groups:
        scored = sorted(
            group,
            key=lambda i: (
                len(floors[i].get('rooms', []) or []),
                sum((r or {}).get('unit_multiplier', 1)
                    for r in (floors[i].get('rooms', []) or [])),
            ),
            reverse=True,
        )
        canonical_idx = scored[0]
        dropped_idxs = scored[1:]
        floors_to_drop.update(dropped_idxs)
        canonical_name = floors[canonical_idx].get('floor_name', '?')
        canonical_rooms = len(floors[canonical_idx].get('rooms', []) or [])
        dropped_summary = "; ".join(
            f"'{floors[i].get('floor_name','?')}' "
            f"({len(floors[i].get('rooms', []) or [])} rooms)"
            for i in dropped_idxs
        )
        dedup_notes.append(
            f"[dedup] Template floors with overlapping floor ranges merged: "
            f"kept '{canonical_name}' ({canonical_rooms} rooms); "
            f"dropped {dropped_summary}"
        )

    if floors_to_drop:
        analysis['floors'] = [
            f for i, f in enumerate(floors) if i not in floors_to_drop
        ]
        existing_notes = analysis.get('notes') or []
        if not isinstance(existing_notes, list):
            existing_notes = [existing_notes] if existing_notes else []
        analysis['notes'] = list(existing_notes) + dedup_notes
        for note in dedup_notes:
            print(f"   🪞 {note}", flush=True)

    analysis['_template_floors_deduped'] = True
    return analysis


def _base_confirmed_paintable(room, building_type):
    """Hard-numbers gate for base trim.

    The floor-plan extraction path sets base_trim_lf = room perimeter for
    EVERY room with no base-material check (the prompt's legacy default). On
    commercial/retail fit-outs that is a fabrication: resilient/vinyl cove
    base is the norm and is not field-painted (Five Below: we reported 590-848
    LF; the manual takeoff had 0). This returns whether the room's base trim is
    a CONFIRMED paintable (painted) base, so callers can zero it when it is not.

    Decision order:
      1. Explicit painted/wood base in any field/note  -> True  (keep)
      2. Explicit resilient/vinyl/cove/tile/none base   -> False (suppress)
      3. Unconfirmed: fall back to building-type default
           - commercial/retail/industrial fit-out -> False (resilient default)
           - residential / unknown                -> True  (painted-wood default)

    Step 3 keeps residential wood-base jobs intact (e.g. the 364 Main Street
    reference: 8,629 LF confirmed wood base) while excluding unconfirmed
    commercial base, matching how Rider takeoffs treat retail boxes.
    """
    mats = room.get("materials", {}) or {}
    el = room.get("elements", {}) or {}
    base_sig = " ".join(str(x) for x in (
        mats.get("base", ""), mats.get("base_finish", ""),
        el.get("base_finish", ""), room.get("base_finish", ""),
        room.get("notes", ""),
    )).lower()

    # Strip ADVISORY phrasing before keyword matching. The legacy prompt told
    # the LLM to add "Base material unverified — confirm paintable vs. resilient
    # cove base" to *every* room as a flag-to-confirm — that is NOT a statement
    # that the base IS resilient. Left in, its "resilient"/"cove base" tokens
    # would wrongly zero painted wood base in residential bedrooms/living rooms.
    # An "unverified/confirm" note means UNCONFIRMED -> building-type default.
    for _adv in (
        "base material unverified/resilient — confirm paintable vs. resilient cove base (rfi)",
        "base material unverified — confirm paintable vs. resilient cove base",
        "base material unverified - confirm paintable vs. resilient cove base",
        "confirm paintable vs. resilient cove base",
        "confirm paintable vs resilient cove base",
        "base material resilient/unconfirmed; perimeter default removed",
        "confirm painted-base scope",
        "base: unverified",
        "unverified — not in finish schedule",
        "unverified - not in finish schedule",
    ):
        base_sig = base_sig.replace(_adv, " ")

    # 1. Explicit painted / wood base -> confirmed paintable.
    if any(kw in base_sig for kw in (
            "paint base", "painted base", "base: paint", "base paint",
            "wood base", "wd base", "wd-", "mdf base", "painted wood")):
        return True
    # 2. Explicit resilient / non-paint base -> not paintable.
    if any(kw in base_sig for kw in (
            "rubber base", "vinyl base", "resilient base", "cove base",
            "tile base", "ceramic base", "no base", "base: none",
            "resilient", "rubber cove", "vinyl cove")):
        return False
    # 3. Unconfirmed -> building-type default.
    bt = (building_type or "").lower()
    is_commercial_box = any(kw in bt for kw in (
        "commercial", "retail", "auto", "industrial", "warehouse",
        "dealership", "fit-out", "fitout", "tenant"))
    if is_commercial_box:
        return False  # unconfirmed commercial base — hard-numbers excludes
    return True  # residential / unknown — painted wood base is the norm


def _dedupe_cross_sheet_rooms(analysis):
    """Count each room ONCE when the same room is extracted from multiple sheets
    (floor plan + fixture plan + RCP + code plan).

    Enhanced extraction now tiles several plan sheets to fix under-extraction
    (the plan-sheet-recovery guard). The same physical room then appears on 2-3
    sheets and ALL of its quantities (walls, ceiling, doors, base trim) are
    summed once per sheet. Five Below: the same Manager's Office / Corridor /
    ADA Toilet / Stockroom appeared on the floor plan AND the RCP AND a code
    plan, inflating gyp ceiling to 1,390 SF (vs a 532 SF manual takeoff) and
    walls to 11,690 SF (vs 11,364 — a coincidental match: cross-sheet
    duplication ~+21% offset a genuine shell-perimeter under-count ~-15%).

    We keep the single most-complete instance per room (largest wall area) and
    mark the other sheet instances out of scope, so every quantity is counted
    once. Ceiling painted/not-painted is decided by majority vote across the
    instances (so an open Stockroom mislabeled GYP on one low-detail sheet stays
    open). Walls then flow through the geometric perimeter floor +
    _validate_and_boost_walls to recover the real shell perimeter.

    Tightly gated to SINGLE-STORY COMMERCIAL jobs with no unit multipliers,
    where a repeated room NAME is a genuine cross-sheet duplicate. It must NEVER
    run on residential / multi-unit work, where many distinct rooms legitimately
    share a name ("Bedroom", "Bath"). Duplicates are only merged when they span
    >1 source page, so genuinely distinct same-name rooms drawn on one sheet are
    left alone. Idempotent via the _cross_sheet_rooms_deduped flag.
    """
    if analysis.get("_cross_sheet_rooms_deduped"):
        return
    pi = analysis.get("project_info", {}) or {}
    bt = str(pi.get("building_type", "")).lower()
    is_commercial = any(k in bt for k in (
        "commercial", "retail", "auto", "industrial", "warehouse",
        "dealership", "restaurant", "fitness", "recreational",
        "institutional", "entertain", "fit-out", "fitout", "tenant"))
    # Exclude anything residential/mixed — those repeat room names legitimately.
    if not is_commercial or any(k in bt for k in (
            "residential", "mixed", "multi", "apartment", "condo", "senior")):
        return
    rooms = [r for fl in analysis.get("floors", []) for r in fl.get("rooms", [])
             if r.get("in_scope", True)]
    if not rooms:
        return
    # Hard guard: a single repeated/template unit means this isn't a simple
    # single-tenant box — bail rather than risk merging real repeats.
    if _num(pi.get("total_units", 0)) > 1:
        return
    for r in rooms:
        if _extract_multiplier_from_notes(r) > 1 or _num(r.get("unit_multiplier", 1)) > 1:
            return

    import collections

    def _norm(n):
        return re.sub(r"[^a-z0-9]", "", str(n or "").lower())

    def _warea(r):
        return _num((r.get("dimensions", {}) or {}).get("wall_area_sqft", 0))

    groups = collections.defaultdict(list)
    for r in rooms:
        nm = _norm(r.get("room_name") or r.get("name"))
        if nm:
            groups[nm].append(r)

    removed = 0
    for nm, insts in groups.items():
        if len(insts) < 2:
            continue
        # Only treat as cross-sheet duplicates when they appear on >1 sheet.
        pages = {str(r.get("source_page")) for r in insts if r.get("source_page") is not None}
        if len(pages) < 2:
            continue
        # Keep the most-complete instance (largest measured wall area).
        keeper = max(insts, key=_warea)
        # Majority vote on whether this room's ceiling is painted (tie -> painted).
        painted = sum(1 for r in insts if (r.get("materials", {}) or {}).get("ceiling_painted", False))
        keep_painted = painted >= (len(insts) - painted) and painted > 0
        km = keeper.setdefault("materials", {})
        if not keep_painted and km.get("ceiling_painted", False):
            km["ceiling_painted"] = False
        # Zero only the DIMENSIONAL quantities on the non-keeper instances —
        # each sheet measures the full room geometry, so walls/ceiling/floor are
        # true duplicates. Doors/windows are left intact: each sheet tends to
        # enumerate DIFFERENT openings of the same room (partial counts that sum
        # to the real total — Five Below full-paint doors sum to 7, matching the
        # manual takeoff), so collapsing them would under-count.
        for r in insts:
            if r is keeper:
                continue
            d = r.setdefault("dimensions", {})
            for k in ("wall_area_sqft", "perimeter_lf", "ceiling_area_sqft", "floor_area_sqft"):
                d[k] = 0
            r.setdefault("elements", {})["base_trim_lf"] = 0
            r.setdefault("materials", {})["ceiling_painted"] = False
            r["notes"] = (str(r.get("notes", "") or "") +
                          " [Cross-sheet dedup] room geometry measured on another "
                          "sheet; walls/ceiling counted once on the most-complete "
                          "instance (doors/windows retained).").strip()
            removed += 1

    analysis["_cross_sheet_rooms_deduped"] = True
    if removed:
        print(f"   🪞 [cross-sheet room dedup] de-duplicated geometry on {removed} "
              f"duplicate room instance(s) across sheets (single-story commercial).")


def _recalculate_totals(analysis):
    """
    Recalculate aggregated_totals from individual room data.
    Applies ceiling_painted filter, door type split, window painted filter,
    and unit_multiplier for repeated/typical unit types.
    Works for both single-file and merged multi-file analyses.
    """
    # Dedupe template floors with overlapping ranges before any totals are
    # computed. Cross-chunk extraction can produce 'Levels 1-7' AND
    # 'Floors 2-9' AND 'Levels 1-10' for the same residential block;
    # without dedup their unit_multipliers all get summed and the totals
    # inflate ~3×. Idempotent via _template_floors_deduped flag.
    _dedupe_overlapping_template_floors(analysis)

    # Safety net: residential corridor / lobby ceiling correction.
    # The extraction prompt previously told the model that public corridors
    # "ALMOST ALWAYS have ACT ceilings". That's right for commercial but
    # wrong for residential supportive housing, multifamily, dorms, etc.,
    # where painted GYP is the corridor norm. The Ridgeview 2026-05-28 run
    # lost ~2,900 sqft of corridor ceiling this way. Idempotent via
    # _residential_corridor_ceiling_fixed flag.
    _fix_residential_corridor_ceilings(analysis)

    # Pre-pass: estimate missing wall area from floor area for rooms that have
    # floor_area but zero wall_area/perimeter (common with chunk-processing gaps)
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            dims = room.get("dimensions", {})
            floor_area = _num(dims.get("floor_area_sqft", 0))
            wall_area = _num(dims.get("wall_area_sqft", 0))
            perimeter = _num(dims.get("perimeter_lf", 0))
            ch = _num(dims.get("ceiling_height_feet", 0))

            if floor_area > 0 and wall_area == 0 and perimeter == 0 and ch > 0:
                if HARD_NUMBERS_ONLY:
                    # A square-room perimeter (4 × √area) is an assumption,
                    # not a measurement — and it used to silently set BASE
                    # TRIM = that invented perimeter too. Leave the room's
                    # walls/trim at zero and mark it; the Incomplete
                    # Dimensions RFI machinery surfaces zero-wall rooms.
                    note = room.get("notes", "")
                    room["notes"] = (note + " [Dimensions incomplete: floor "
                                     "area only — walls/trim NOT estimated "
                                     "under hard-numbers policy]").strip()
                    continue
                # Estimate perimeter from floor area assuming square-ish room
                est_side = floor_area ** 0.5
                est_perimeter = round(4 * est_side)
                est_wall_area = round(est_perimeter * ch)
                dims["perimeter_lf"] = est_perimeter
                dims["wall_area_sqft"] = est_wall_area
                # Also set base_trim_lf if missing
                elems = room.get("elements", {})
                if _num(elems.get("base_trim_lf", 0)) == 0:
                    elems["base_trim_lf"] = est_perimeter
                note = room.get("notes", "")
                room["notes"] = (note + " [Walls/trim estimated from floor area]").strip()

    # Collapse rooms extracted from multiple plan sheets so a room isn't counted
    # once per sheet for walls/ceiling/doors (single-story commercial only).
    _dedupe_cross_sheet_rooms(analysis)

    total_wall = 0
    total_ceiling = 0
    total_cmu_wall = 0
    total_lymewash_wall = 0
    total_plaster_wall = 0
    total_dryfall_ceiling = 0
    total_trim = 0
    # Hard-numbers base-trim suppression bookkeeping (see _base_confirmed_paintable)
    _bt_building_type = str(analysis.get("project_info", {}).get("building_type", ""))
    _bt_suppressed_lf = 0
    _bt_suppressed_rooms = 0
    total_doors_full = 0
    total_doors_hm = 0
    total_doors_frame = 0
    total_windows_painted = 0
    total_windows_all = 0
    total_stairs = 0
    total_gyp_stairs = 0
    total_level_5 = 0
    total_concrete_floor = 0
    total_painted_columns = 0
    total_wallcovering = 0
    total_stained_wood = 0
    total_soffit = 0
    total_painted_railing = 0

    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            # Skip out-of-scope rooms for totals calculation
            if not room.get("in_scope", True):
                continue

            dims = room.get("dimensions", {})
            elems = room.get("elements", {})
            mats = room.get("materials", {})

            # Extract unit multiplier (1 for normal rooms, N for template rooms)
            multiplier = _extract_multiplier_from_notes(room)

            # Walls — only if paintable material
            # PCA Rule #8: Deduct door openings from wall area.
            # Standard doors (3'x7' = 21 SF) are deducted from wall area.
            # Standard windows (< 100 SF) are NOT deducted per PCA Rule #8.
            # PCA Rule #9: Cabinets, tubs, showers are NOT deducted.
            _room_door_count = (
                _num(elems.get("doors_full_paint", 0)) +
                _num(elems.get("doors_hm_panel", 0)) +
                _num(elems.get("doors_frame_only", 0))
            )
            if "doors" in elems and "doors_full_paint" not in elems:
                _room_door_count += _num(elems.get("doors", 0))
            _door_opening_deduction = _room_door_count * PCA_CONSTANTS["std_door_opening_sf"]
            _raw_wall_area = _num(dims.get("wall_area_sqft", 0))
            _adjusted_wall_area = max(0, _raw_wall_area - _door_opening_deduction)

            wall_mat = str(mats.get("walls", "")).lower()
            if "cmu" in wall_mat:
                total_cmu_wall += _adjusted_wall_area * multiplier
            elif any(kw in wall_mat for kw in (
                    "lyme wash", "lyme-wash", "lymewash",
                    "lime wash", "lime-wash", "limewash")):
                total_lymewash_wall += _adjusted_wall_area * multiplier
            elif "plaster" in wall_mat:
                total_plaster_wall += _adjusted_wall_area * multiplier
            elif any(kw in wall_mat for kw in ("gyp", "gwb", "gypsum", "paintable")):
                total_wall += _adjusted_wall_area * multiplier

            # Ceilings — only if explicitly marked painted
            ceil_mat = str(mats.get("ceiling", "")).lower()

            # ACT ceiling safety net for common hallways/corridors
            # Public/common spaces almost always have ACT ceilings (not painted)
            rname_lower = str(room.get("room_name", "")).lower()
            _is_unit_room = any(kw in rname_lower for kw in (
                "apt", "unit", "suite", "bedroom", "bath", "kitchen", "living"))
            _is_common_space = (
                not _is_unit_room
                and any(kw in rname_lower for kw in (
                    "corridor", "hallway", "common hall", "common area",
                    "lobby", "vestibule", "public", "breezeway"))
            )
            if _is_common_space and mats.get("ceiling_painted", False):
                _has_explicit_gyp = any(kw in ceil_mat for kw in (
                    "gyp", "gwb", "gypsum", "drywall", "plaster", "dryfall"))
                if not _has_explicit_gyp:
                    mats["ceiling_painted"] = False
                    mats["ceiling"] = mats.get("ceiling", "") or "ACT (assumed)"
                    existing_note = str(room.get("notes", ""))
                    room["notes"] = (existing_note + " [ACT ceiling assumed for common space]").strip()

            if mats.get("ceiling_painted", False):
                if "dryfall" in ceil_mat:
                    ceil_area = _num(dims.get("ceiling_area_sqft", 0))
                    if ceil_area == 0:
                        # Dryfall = spray underside of deck above. Area ≈ floor area.
                        ceil_area = _num(dims.get("floor_area_sqft", 0))
                    total_dryfall_ceiling += ceil_area * multiplier
                else:
                    total_ceiling += _num(dims.get("ceiling_area_sqft", 0)) * multiplier

            # Base trim
            _room_bt = _num(elems.get("base_trim_lf", 0))
            if HARD_NUMBERS_ONLY and _room_bt > 0 and not _base_confirmed_paintable(
                    room, _bt_building_type):
                # Resilient/unconfirmed base on a commercial job — the perimeter
                # default is a fabrication, not a measurement. Zero it and flag
                # for an RFI instead of pricing trim that isn't there.
                _bt_suppressed_lf += _room_bt * multiplier
                _bt_suppressed_rooms += 1
                elems["base_trim_lf"] = 0
                _existing = str(room.get("notes", "") or "")
                if "[Hard Numbers] base trim excluded" not in _existing:
                    room["notes"] = (
                        _existing + " [Hard Numbers] base trim excluded — base "
                        "material resilient/unconfirmed; perimeter default removed "
                        "(confirm painted-base scope via RFI)."
                    ).strip()
                _room_bt = 0
            total_trim += _room_bt * multiplier

            # Doors — new schema: doors_full_paint / doors_hm_panel / doors_frame_only
            total_doors_full += _num(elems.get("doors_full_paint", 0)) * multiplier
            total_doors_hm += _num(elems.get("doors_hm_panel", 0)) * multiplier
            total_doors_frame += _num(elems.get("doors_frame_only", 0)) * multiplier
            # Backward compat: old "doors" key → treat as full_paint
            if "doors" in elems and "doors_full_paint" not in elems:
                total_doors_full += _num(elems.get("doors", 0)) * multiplier

            # Windows — only painted interior count
            # Safety net: skip painted windows if room notes indicate pre-treated/shop-finish
            room_notes_lower = str(room.get("notes", "")).lower()
            _win_not_painted_kw = ("pre-treated", "pretreated", "shop finish",
                                   "shop painted", "factory painted", "pre-finished",
                                   "prefinished", "factory finish")
            if any(kw in room_notes_lower for kw in _win_not_painted_kw):
                # Windows are pre-treated/shop-finished — do not count as painted
                pass
            else:
                total_windows_painted += _num(elems.get("windows_painted_interior", 0)) * multiplier
            total_windows_all += _num(elems.get("windows_total",
                                                elems.get("windows", 0))) * multiplier

            # Stairs
            total_stairs += _num(elems.get("stair_sections", 0)) * multiplier
            total_gyp_stairs += _num(elems.get("gyp_between_stairs_sqft", 0)) * multiplier

            # Level 5 finish
            # Fallback: expand legacy placeholder values (1-10) to the room's actual paint
            # surface, since L5 is priced per SF ($0.55/sf). A value of 1 yields nearly $0;
            # a flagged room should price the wall + ceiling area it actually covers.
            _l5_raw = _num(elems.get("level_5_finish_sqft", 0))
            if 0 < _l5_raw <= 10:
                _l5_walls = _num(dims.get("wall_area_sqft", 0))
                _l5_ceil = _num(dims.get("ceiling_area_sqft", 0))
                _l5_expanded = _l5_walls + _l5_ceil
                if _l5_expanded > 0:
                    _l5_raw = _l5_expanded
            total_level_5 += _l5_raw * multiplier

            # Concrete floor sealer — only when specs explicitly call out sealcoating.
            # A bare concrete floor does NOT qualify; must have sealer/epoxy/coating spec.
            _conc_sqft = _num(elems.get("concrete_floor_sqft", 0))
            if _conc_sqft > 0:
                total_concrete_floor += _conc_sqft * multiplier

            # Painted columns (commercial)
            total_painted_columns += _num(elems.get("painted_columns_ea", 0)) * multiplier

            # Wallcovering (labor-only install)
            total_wallcovering += _num(elems.get("wallcovering_sqft", 0)) * multiplier

            # Stained wood / clear-coat panels
            total_stained_wood += _num(elems.get("stained_wood_sqft", 0)) * multiplier

            # Interior soffits (GYP drywall drops)
            total_soffit += _num(elems.get("soffit_sqft", 0)) * multiplier

            # Painted interior railings (stair handrails, balcony rails)
            total_painted_railing += _num(elems.get("painted_railing_lf", 0)) * multiplier

    # ── PCA Room SF Cross-Check (informational) ──
    # Validate extracted room areas against PCA Section 4D formula:
    # Expected total = perimeter × ceiling_height + (length × width) for ceiling
    # Flag rooms with >25% deviation as potentially misread.
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            if not room.get("in_scope", True):
                continue
            dims = room.get("dimensions", {})
            _r_len = _num(dims.get("length_feet", 0))
            _r_wid = _num(dims.get("width_feet", 0))
            _r_clg = _num(dims.get("ceiling_height_feet", 0))
            if _r_len > 0 and _r_wid > 0 and _r_clg > 0:
                _pca_perimeter = 2 * (_r_len + _r_wid)
                _pca_wall = _pca_perimeter * _r_clg
                _pca_ceiling = _r_len * _r_wid
                _pca_expected = _pca_wall + _pca_ceiling
                _extracted_wall = _num(dims.get("wall_area_sqft", 0))
                _extracted_ceil = _num(dims.get("ceiling_area_sqft", 0))
                _extracted_total = _extracted_wall + _extracted_ceil
                if _extracted_total > 0 and _pca_expected > 0:
                    _deviation = abs(_extracted_total - _pca_expected) / _pca_expected
                    if _deviation > 0.25:
                        existing_note = str(room.get("notes", ""))
                        room["notes"] = (existing_note +
                            f" [PCA check: expected {_pca_expected:.0f} SF, got {_extracted_total:.0f} SF"
                            f" ({_deviation:.0%} deviation)]").strip()

    # ── Unit-Count Fallback Safety Net ──
    # When LLM extracted building metadata (total_units > 0) but failed to
    # read any room data (0 rooms), estimate from unit templates.
    # Typical cause: Planning Board PDFs at tiny scale (1/16"=1'-0"), no
    # finish/door/window schedules, image fallback also fails.
    proj_info = analysis.get("project_info", {})
    total_units_val = _num(proj_info.get("total_units", 0))
    all_rooms_list = [
        room for floor in analysis.get("floors", [])
        for room in floor.get("rooms", [])
        if room.get("in_scope", True)
    ]

    if (not HARD_NUMBERS_ONLY) and len(all_rooms_list) == 0 and total_units_val >= 4:
        # Determine unit mix from notes if possible
        all_notes_mix = " ".join(str(n) for n in analysis.get("notes", [])).lower()
        has_studio = any(kw in all_notes_mix for kw in ("studio", "efficiency"))
        has_1br = any(kw in all_notes_mix for kw in ("1br", "1 br", "1-br", "one bedroom", "1 bedroom"))
        has_2br = any(kw in all_notes_mix for kw in ("2br", "2 br", "2-br", "two bedroom", "2 bedroom"))
        has_3br = any(kw in all_notes_mix for kw in ("3br", "3 br", "3-br", "three bedroom", "3 bedroom"))

        # If at least 2 unit types mentioned, use detected mix; else default
        detected_types = sum([has_studio, has_1br, has_2br, has_3br])
        if detected_types >= 2:
            # Distribute proportionally among detected types
            type_share = 1.0 / detected_types
            unit_mix = {}
            if has_studio:
                unit_mix["studio"] = type_share
            if has_1br:
                unit_mix["1br"] = type_share
            if has_2br:
                unit_mix["2br"] = type_share
            if has_3br:
                unit_mix["3br"] = type_share
        else:
            unit_mix = dict(UNIT_MIX_DEFAULT)

        # --- Footprint-based estimation (preferred) vs unit-template fallback ---
        stories_uc = max(1, _num(proj_info.get("total_stories", 1)))
        footprint_uc = _num(proj_info.get("footprint_sqft", 0))
        use_footprint = footprint_uc > 0

        if use_footprint:
            # Ceiling = total paintable residential floor area
            # Walls = ceiling × wall-to-floor ratio (interior partitions)
            # Calibrated to Rider Painting / Chestnut actuals (0.1% error)
            unit_ceil = round(footprint_uc * stories_uc * RESIDENTIAL_EFFICIENCY)
            unit_wall = round(unit_ceil * WALL_TO_FLOOR_RATIO)
            estimation_method = "footprint"
        else:
            unit_wall = 0
            unit_ceil = 0
            estimation_method = "unit templates"

        # Doors and trim always come from unit templates (not footprint-derivable)
        unit_doors = 0
        unit_trim = 0
        unit_breakdown = []
        for utype, share in unit_mix.items():
            tmpl = UNIT_TEMPLATES.get(utype, UNIT_TEMPLATES["1br"])
            count = round(total_units_val * share)
            if count == 0:
                continue
            if not use_footprint:
                # Only use template wall/ceiling when no footprint available
                unit_wall += tmpl["wall_sqft"] * count
                unit_ceil += tmpl["ceiling_sqft"] * count
            unit_doors += tmpl["doors"] * count
            unit_trim += tmpl["trim_lf"] * count
            unit_breakdown.append(f"{count} {utype}")

        # No common areas — hallways, retail, lobbies, corridors are generally
        # NOT painted in multi-family residential (confirmed by Rider Painting).
        # The RESIDENTIAL_EFFICIENCY factor (0.63) already excludes these.
        total_wall = unit_wall
        total_ceiling = unit_ceil
        total_doors_full = unit_doors
        total_trim = unit_trim

        # New construction multi-family: windows are aluminum/vinyl frames,
        # NOT painted. Zero out windows. (Confirmed by Rider Painting.)
        total_windows_painted = 0
        total_windows_all = 0

        # Flag for downstream: skip wall boost when footprint-based
        # (already calibrated to Rider actuals — boost would over-inflate)
        if use_footprint:
            analysis["_used_footprint_fallback"] = True

        # Exterior estimation from building envelope if footprint available
        if footprint_uc > 0 and stories_uc >= 2:
            long_side = math.sqrt(footprint_uc * 2)
            short_side = footprint_uc / long_side if long_side > 0 else 0
            perimeter = 2 * (long_side + short_side)
            avg_story_ht = 10  # residential default
            envelope = perimeter * avg_story_ht * stories_uc * 0.70
            exterior_obj = analysis.get("exterior", {})
            if _num(exterior_obj.get("exterior_paint_sqft", 0)) == 0:
                exterior_obj["exterior_paint_sqft"] = round(envelope)
                exterior_obj["lift_required"] = stories_uc >= 3
                if _num(exterior_obj.get("cornice_lf", 0)) == 0:
                    exterior_obj["cornice_lf"] = round(perimeter)
                analysis["exterior"] = exterior_obj
                analysis.setdefault("notes", []).append(
                    f"[Unit-Count Fallback] Exterior estimated from envelope: "
                    f"{round(envelope):,} sqft paint, {round(perimeter):,} LF cornice "
                    f"({footprint_uc:,.0f} sqft footprint × {stories_uc} stories)")

        breakdown_str = ", ".join(unit_breakdown)
        analysis.setdefault("notes", []).append(
            f"[Unit-Count Fallback] Estimated from {int(total_units_val)} units "
            f"({breakdown_str}), method: {estimation_method}. "
            f"Walls: {total_wall:,} sqft, Ceilings: {total_ceiling:,} sqft, "
            f"Doors: {total_doors_full}, Windows: 0 (aluminum/vinyl, not painted), "
            f"Trim: {total_trim:,} LF. "
            f"LLM could not extract room data (resolution/scale too small).")
        print(f"   🔧 Unit-count fallback ({estimation_method}): "
              f"{int(total_units_val)} units ({breakdown_str}) → "
              f"{total_wall:,} sqft walls, {total_ceiling:,} sqft ceilings, "
              f"{total_doors_full} doors")

    # --- Dryfall safety net for "EXPOSED" ceilings ---
    # LLM inconsistency: sometimes labels exposed-structure ceilings as "DRYFALL",
    # other times as "EXPOSED" or "OPEN". In commercial buildings where specs mention
    # dryfall/spray-applied coating, EXPOSED ceilings in service/warehouse areas
    # should be reclassified as dryfall.
    if (not HARD_NUMBERS_ONLY) and total_dryfall_ceiling == 0:
        building_type = str(analysis.get("project_info", {}).get("building_type", "")).lower()
        is_commercial_bldg = any(kw in building_type for kw in (
            "commercial", "auto", "industrial", "warehouse", "retail", "dealership"))
        if is_commercial_bldg:
            all_notes = " ".join(str(n) for n in analysis.get("notes", []))
            # Also scan room-level notes for dryfall references
            for floor in analysis.get("floors", []):
                for room in floor.get("rooms", []):
                    room_note = str(room.get("notes", ""))
                    all_notes += " " + room_note
            # Also scan material legend descriptions
            for entry in analysis.get("material_legend", []):
                all_notes += " " + str(entry.get("description", ""))
            # Also scan captured structural_finish_scope rows from the
            # finish-schedule extraction — these are the most authoritative
            # source for "paint deck/structure/MEP" callouts.
            for sfs in analysis.get("structural_finish_scope", []) or []:
                all_notes += " " + str(sfs.get("note", ""))
                all_notes += " " + str(sfs.get("finish", ""))
                for surf in sfs.get("surfaces", []) or []:
                    all_notes += " " + str(surf)
            all_notes_lower = all_notes.lower()
            dryfall_in_notes = any(kw in all_notes_lower for kw in (
                "dryfall", "dry fall", "spray-applied", "spray applied",
                "paint exposed", "painted deck", "paint deck", "dry-fall",
                # Retail/commercial finish-schedule callouts that imply paint-to-deck
                # but don't use the literal word "dryfall":
                "paint to deck", "paint structure", "paint joists",
                "paint conduit", "paint conduits", "paint duct", "paint ducts",
                "paint hvac", "paint mep", "paint piping",
                "semi-gloss enamel", "semi gloss enamel",
                "enamel on exposed", "enamel exposed",
                "paint all exposed", "paint exposed structure",
                "open to structure", "open to deck"))

            # Secondary trigger: commercial building with exposed-ceiling rooms
            # at height ≥14ft. In commercial/industrial buildings, high exposed
            # ceilings are virtually always dryfall scope — the finish schedule
            # uses codes like P-10 that map to dryfall but aren't always in notes.
            if not dryfall_in_notes:
                has_high_exposed = False
                for floor in analysis.get("floors", []):
                    for room in floor.get("rooms", []):
                        if not room.get("in_scope", True):
                            continue
                        mats_chk = room.get("materials", {})
                        dims_chk = room.get("dimensions", {})
                        ceil_chk = str(mats_chk.get("ceiling", "")).lower()
                        ch_chk = _num(dims_chk.get("ceiling_height_feet", 0))
                        fa_chk = _num(dims_chk.get("floor_area_sqft", 0))
                        if (ceil_chk in ("exposed", "open", "exposed structure",
                                         "open structure", "exposed deck", "open deck")
                                and ch_chk >= 14 and fa_chk > 200):
                            has_high_exposed = True
                            break
                    if has_high_exposed:
                        break
                # Also check if notes reference "exposed structure" or "open ceiling"
                has_exposed_refs = any(kw in all_notes_lower for kw in (
                    "exposed structure", "exposed ceiling", "open ceiling",
                    "open structure", "exposed deck"))
                if has_high_exposed or has_exposed_refs:
                    dryfall_in_notes = True  # Trigger the reclassification

            if dryfall_in_notes:
                reclassified_sqft = 0
                for floor in analysis.get("floors", []):
                    for room in floor.get("rooms", []):
                        if not room.get("in_scope", True):
                            continue
                        mats_r = room.get("materials", {})
                        dims_r = room.get("dimensions", {})
                        ceil_r = str(mats_r.get("ceiling", "")).lower()
                        if ceil_r in ("exposed", "open", "exposed structure",
                                      "open structure", "exposed deck", "open deck"):
                            fa = _num(dims_r.get("floor_area_sqft", 0))
                            if fa > 200:  # Skip tiny rooms like closets
                                mult = _extract_multiplier_from_notes(room)
                                total_dryfall_ceiling += fa * mult
                                reclassified_sqft += fa * mult
                if reclassified_sqft > 0:
                    analysis.setdefault("notes", []).append(
                        f"[Dryfall Safety Net] Reclassified {reclassified_sqft:,.0f} sqft of "
                        f"EXPOSED ceilings as dryfall (commercial building with high exposed ceilings)")
                    print(f"   🔧 Dryfall safety net: reclassified {reclassified_sqft:,.0f} sqft "
                          f"EXPOSED → dryfall (commercial + high exposed ceilings)")

                # Footprint-based fallback: schedule called out structural-surface
                # painting (deck/structure/MEP) but no room had ceiling="exposed"
                # to reclassify. Common on retail boxes where the LLM extracted
                # only "office" / "stockroom" rooms and missed that the sales
                # floor is open-to-deck. Estimate dryfall from footprint.
                if reclassified_sqft == 0:
                    _pi_dr = analysis.get("project_info", {})
                    footprint_dr = _num(_pi_dr.get("footprint_sqft", 0))
                    has_struct_scope = bool(analysis.get("structural_finish_scope"))
                    if footprint_dr > 0 and has_struct_scope:
                        # Sales-floor / open area is typically 70-85% of a retail
                        # footprint (back-of-house and offices ceiling out at ACT).
                        # Use 0.75 as a defensible mid-point.
                        est_dryfall = round(footprint_dr * 0.75)
                        total_dryfall_ceiling += est_dryfall
                        analysis.setdefault("notes", []).append(
                            f"[Dryfall Safety Net] Estimated {est_dryfall:,.0f} sqft of "
                            f"dryfall (75% of {footprint_dr:,.0f} sqft footprint). "
                            f"Schedule has structural-finish callouts (paint exposed "
                            f"deck/structure/MEP) but no room ceilings were tagged "
                            f"EXPOSED for reclassification.")
                        print(f"   🔧 Dryfall safety net (footprint fallback): "
                              f"{est_dryfall:,.0f} sqft from {footprint_dr:,.0f} sqft "
                              f"footprint × 0.75 (structural_finish_scope present)")

    # --- Wallcovering estimation fallback ---
    # When finish schedule mentions WC-x codes but LLM extracted 0 wallcovering_sqft,
    # estimate wallcovering from customer-facing rooms (showroom, lobby, boutique, lounge).
    # Wallcovering is typically applied to accent walls (30-50% of wall area) in
    # customer-facing spaces.
    has_wc_refs = False
    if (not HARD_NUMBERS_ONLY) and total_wallcovering == 0:
        all_notes_wc = " ".join(str(n) for n in analysis.get("notes", []))
        # Also scan room-level notes
        for floor in analysis.get("floors", []):
            for room in floor.get("rooms", []):
                all_notes_wc += " " + str(room.get("notes", ""))
        all_notes_wc_lower = all_notes_wc.lower()
        # Only trigger when ACTUAL WC-x finish codes appear (not generic mentions
        # of "wallcovering" in notes which can be LLM commentary)
        wc_codes = set(re.findall(r'wc-?\d+', all_notes_wc_lower))
        has_wc_refs = len(wc_codes) > 0
        if has_wc_refs:
            num_wc_types = max(1, len(wc_codes))

            # Identify customer-facing rooms that would have wallcovering
            # Exclude "office" and "corridor" for mixed-use — those are painted GYP
            _pi_wcn = analysis.get("project_info", {})
            _bt_wcn = str(_pi_wcn.get("building_type", "")).lower()
            _is_mixed_wcn = any(kw in _bt_wcn for kw in ("mixed", "residential", "multi"))
            if _is_mixed_wcn:
                wc_room_names = (
                    "showroom", "lobby", "lounge", "boutique", "reception",
                    "waiting", "customer", "sales", "display")
            else:
                wc_room_names = (
                    "showroom", "lobby", "lounge", "boutique", "reception",
                    "waiting", "conference", "office", "customer", "sales",
                    "display", "retail", "vestibule", "entry", "corridor")
            wc_estimated = 0
            wc_rooms_found = []
            for floor in analysis.get("floors", []):
                for room in floor.get("rooms", []):
                    if not room.get("in_scope", True):
                        continue
                    rname = str(room.get("room_name", "")).lower()
                    # Check if this is a customer-facing room
                    if any(kw in rname for kw in wc_room_names):
                        dims_wc = room.get("dimensions", {})
                        wall_area = _num(dims_wc.get("wall_area_sqft", 0))
                        if wall_area > 0:
                            mult = _extract_multiplier_from_notes(room)
                            # Estimate 35% of wall area has wallcovering
                            wc_area = round(wall_area * 0.35) * mult
                            wc_estimated += wc_area
                            wc_rooms_found.append(room.get("room_name", ""))

            if wc_estimated > 0:
                total_wallcovering = wc_estimated
                # Subtract wallcovering area from painted wall area to avoid double-counting
                total_wall = max(0, total_wall - wc_estimated)
                analysis.setdefault("notes", []).append(
                    f"[Wallcovering Safety Net] Estimated {wc_estimated:,.0f} sqft wallcovering "
                    f"from {len(wc_rooms_found)} customer-facing rooms "
                    f"({', '.join(wc_rooms_found[:5])}). "
                    f"Finish schedule references {num_wc_types} WC type(s) "
                    f"({', '.join(sorted(wc_codes)[:4])}). "
                    f"Subtracted from GYP wall total to avoid double-counting.")
                print(f"   🔧 Wallcovering safety net: {wc_estimated:,.0f} sqft estimated "
                      f"from {len(wc_rooms_found)} rooms ({', '.join(sorted(wc_codes)[:4])})")

    # --- Missing finish schedule fallback for residential ---
    # When NO room finish schedule exists and no WC codes were found in notes,
    # residential buildings with bathrooms likely have wallcovering (above wainscot/tub).
    # Also scan for stained wood keywords in room notes.
    if (not HARD_NUMBERS_ONLY) and total_wallcovering == 0 and not has_wc_refs:
        has_finish_schedule = bool(analysis.get("room_finish_schedule"))
        if not has_finish_schedule:
            _pi_wc = analysis.get("project_info", {})
            _bt_wc = str(_pi_wc.get("building_type", "")).lower()
            _is_res_wc = any(kw in _bt_wc for kw in (
                "residential", "mixed", "multi", "apartment", "condo"))
            _units_wc = _num(_pi_wc.get("total_units", 0))

            if _is_res_wc and _units_wc >= 4:
                bath_wc_sqft = 0
                bath_count = 0
                for floor in analysis.get("floors", []):
                    for room in floor.get("rooms", []):
                        if not room.get("in_scope", True):
                            continue
                        rn = str(room.get("room_name", "")).lower()
                        if any(kw in rn for kw in ("bath", "powder", "lavatory")):
                            dims_bwc = room.get("dimensions", {})
                            wall_a = _num(dims_bwc.get("wall_area_sqft", 0))
                            mult_bwc = _extract_multiplier_from_notes(room)
                            if wall_a > 0:
                                bath_wc_sqft += round(wall_a * 0.50) * mult_bwc
                                bath_count += mult_bwc

                if bath_wc_sqft > 0:
                    total_wallcovering = bath_wc_sqft
                    total_wall = max(0, total_wall - bath_wc_sqft)
                    # Flag source so calculate_costs can use wallcovering_prep rate
                    analysis.setdefault("project_info", {})["_wallcovering_source"] = "bathroom_heuristic"
                    analysis.setdefault("notes", []).append(
                        f"[Missing Finish Schedule] No room finish schedule found. "
                        f"Estimated {bath_wc_sqft:,.0f} sqft wallcovering from "
                        f"{bath_count:.0f} bathrooms (50% of bathroom wall area). "
                        f"Wallcovering and specialty finishes may be underestimated. "
                        f"Recommend RFI for finish schedule."
                    )
                    print(f"   📋 Missing finish schedule: estimated {bath_wc_sqft:,.0f} sqft "
                          f"wallcovering from {bath_count:.0f} bathrooms")

    # --- Stained wood keyword detection fallback ---
    if (not HARD_NUMBERS_ONLY) and total_stained_wood == 0:
        stain_kw_sqft = 0
        for floor in analysis.get("floors", []):
            for room in floor.get("rooms", []):
                if not room.get("in_scope", True):
                    continue
                rn_sw = str(room.get("notes", "")).lower()
                rname_sw = str(room.get("room_name", "")).lower()
                combined_text = rn_sw + " " + rname_sw
                if any(kw in combined_text for kw in (
                        "oak panel", "stain", "wood veneer", "accent wall",
                        "wood panel", "wainscot", "wood wainscot")):
                    dims_sw = room.get("dimensions", {})
                    wall_a_sw = _num(dims_sw.get("wall_area_sqft", 0))
                    mult_sw = _extract_multiplier_from_notes(room)
                    if wall_a_sw > 0:
                        sw_area = round(wall_a_sw * 0.25) * mult_sw
                        stain_kw_sqft += sw_area

        if stain_kw_sqft > 0:
            total_stained_wood = stain_kw_sqft
            total_wall = max(0, total_wall - stain_kw_sqft)
            analysis.setdefault("notes", []).append(
                f"[Stained Wood Detected] Found stain/wood keywords in room notes. "
                f"Estimated {stain_kw_sqft:,.0f} sqft stained wood panels. "
                f"Subtracted from GYP wall total to avoid double-counting."
            )
            print(f"   🪵 Stained wood fallback: {stain_kw_sqft:,.0f} sqft from keyword detection")

    # --- Concrete floor area safety net for CMU rooms ---
    # In commercial buildings, rooms with CMU walls almost always have concrete floors.
    # If a CMU room has concrete_floor_sqft > 0 but it's much less than floor_area_sqft,
    # the LLM likely under-measured. Also, rooms with CMU walls and 0 concrete may
    # have been missed. Boost concrete to match floor area for CMU rooms.
    # NOTE: gated by HARD_NUMBERS_ONLY — assuming a CMU room's full floor is bare
    # sealed concrete (beyond the measured concrete_floor_sqft) is a material
    # assumption, not a measured quantity.
    if (not HARD_NUMBERS_ONLY) and total_concrete_floor > 0:
        building_type_conc = str(analysis.get("project_info", {}).get("building_type", "")).lower()
        is_commercial_conc = any(kw in building_type_conc for kw in (
            "commercial", "auto", "industrial", "warehouse", "retail", "dealership"))
        if is_commercial_conc:
            concrete_boost = 0
            for floor in analysis.get("floors", []):
                for room in floor.get("rooms", []):
                    if not room.get("in_scope", True):
                        continue
                    mats_c = room.get("materials", {})
                    dims_c = room.get("dimensions", {})
                    elems_c = room.get("elements", {})
                    wall_mat_c = str(mats_c.get("walls", "")).lower()
                    conc_sqft = _num(elems_c.get("concrete_floor_sqft", 0))
                    floor_area = _num(dims_c.get("floor_area_sqft", 0))
                    mult_c = _extract_multiplier_from_notes(room)

                    if "cmu" in wall_mat_c and floor_area > 0:
                        if conc_sqft < floor_area * 0.9:
                            # CMU room with under-counted concrete
                            add = (floor_area - conc_sqft) * mult_c
                            total_concrete_floor += add
                            concrete_boost += add
                            elems_c["concrete_floor_sqft"] = floor_area

            if concrete_boost > 0:
                analysis.setdefault("notes", []).append(
                    f"[Concrete Safety Net] Boosted concrete floor by {concrete_boost:,.0f} sqft "
                    f"in CMU rooms where concrete < floor area")
                print(f"   🔧 Concrete safety net: +{concrete_boost:,.0f} sqft "
                      f"(CMU rooms boosted to floor area)")

    # --- Interior lift detection ---
    # Required when commercial rooms have dryfall ceiling AND ceiling height > 14 ft
    max_interior_height = 0
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            if not room.get("in_scope", True):
                continue
            dims_r = room.get("dimensions", {})
            mats_r = room.get("materials", {})
            ch_r = _num(dims_r.get("ceiling_height_feet", 0))
            ceil_mat_r = str(mats_r.get("ceiling", "")).lower()
            # Check dryfall OR exposed ceilings (which may have been reclassified)
            if ch_r > max_interior_height and any(
                kw in ceil_mat_r for kw in ("dryfall", "exposed", "open")):
                max_interior_height = ch_r

    interior_lift_needed = 1 if max_interior_height > 14 else 0

    # Set interior_lift_required on exterior object
    exterior_obj = analysis.get("exterior", {})
    if interior_lift_needed:
        exterior_obj["interior_lift_required"] = True

    # --- Guard rail: if NO window schedule was found ---
    # No window schedule means we cannot determine window TYPE — without TYPE we
    # don't know whether casings, aprons, stools, returns, or drywall returns are
    # present. Do NOT assume any window paint scope. Zero painted windows
    # (regardless of residential vs commercial) and flag for RFI.
    has_win_sched = analysis.get("has_window_schedule")
    notes_text = " ".join(str(n) for n in analysis.get("notes", []))
    no_schedule_in_notes = (
        "no window schedule" in notes_text.lower()
        or "no door or window schedule" in notes_text.lower()
        or "window schedule not" in notes_text.lower()
    )
    if has_win_sched is False or (has_win_sched is None and no_schedule_in_notes):
        if total_windows_painted > 0:
            print(f"   ⚠️  No window schedule found — zeroing {total_windows_painted} "
                  f"assumed painted windows (RFI will be generated; no assumptions without schedule)")
            analysis.setdefault("notes", []).append(
                f"[Window Guard Rail] No window schedule found — zeroed {total_windows_painted} "
                f"assumed painted windows. Window TYPE is required to determine casing, apron, "
                f"sill, return, drywall return, or paintable area. RFI generated."
            )
            total_windows_painted = 0
            for floor in analysis.get("floors", []):
                for room in floor.get("rooms", []):
                    room.get("elements", {})["windows_painted_interior"] = 0
        # Make sure the flag is set for RFI generation
        analysis["has_window_schedule"] = False

    analysis["aggregated_totals"] = {
        "total_paintable_wall_sqft": total_wall,
        "total_paintable_ceiling_sqft": total_ceiling,
        "total_cmu_wall_sqft": total_cmu_wall,
        "total_lymewash_wall_sqft": total_lymewash_wall,
        "total_plaster_wall_sqft": total_plaster_wall,
        "total_dryfall_ceiling_sqft": total_dryfall_ceiling,
        "total_base_trim_lf": total_trim,
        "total_doors_full_paint": total_doors_full,
        "total_doors_hm_panel": total_doors_hm,
        "total_doors_frame_only": total_doors_frame,
        "total_windows_painted_interior": total_windows_painted,
        "total_windows_all": total_windows_all,
        "total_stair_sections": total_stairs,
        "total_gyp_between_stairs_sqft": total_gyp_stairs,
        "total_level_5_finish_sqft": total_level_5,
        "total_concrete_floor_sqft": total_concrete_floor,
        "total_painted_columns_ea": total_painted_columns,
        "total_wallcovering_sqft": total_wallcovering,
        "total_stained_wood_sqft": total_stained_wood,
        "total_soffit_sqft": total_soffit,
        "total_painted_railing_lf": total_painted_railing,
    }

    # --- Hard-numbers base-trim suppression summary + RFI flag ---
    if _bt_suppressed_rooms:
        analysis["_hard_numbers_base_trim_suppressed"] = {
            "rooms": _bt_suppressed_rooms,
            "lf": round(_bt_suppressed_lf),
        }
        analysis.setdefault("notes", []).append(
            f"[Hard Numbers] Base trim suppressed on {_bt_suppressed_rooms} room(s) "
            f"(~{round(_bt_suppressed_lf):,} LF of perimeter-default trim removed) — "
            f"base material is resilient/unconfirmed on a {_bt_building_type or 'commercial'} "
            f"job and is not field-painted by default. Excluded from paint scope; "
            f"confirm painted-base scope via RFI."
        )

    # --- Small space ceiling supplement for residential ---
    # Residential units typically have small closets (linen, coat, pantry, utility)
    # that may not be extracted as separate rooms. If ceiling SF is below expected
    # based on wall:ceiling ratio, add a supplement for missing small spaces.
    _pi_ceil = analysis.get("project_info", {})
    _bt_ceil = str(_pi_ceil.get("building_type", "")).lower()
    _units_ceil = _num(_pi_ceil.get("total_units", 0))
    _is_res_ceil = any(kw in _bt_ceil for kw in (
        "residential", "mixed", "multi", "apartment", "condo"))

    if _is_res_ceil and _units_ceil >= 4 and total_wall > 0 and total_ceiling > 0:
        expected_ceiling = total_wall / WALL_TO_FLOOR_RATIO
        ceiling_gap_pct = (expected_ceiling - total_ceiling) / expected_ceiling if expected_ceiling > 0 else 0

        if ceiling_gap_pct > 0.08:
            supplement = min(
                round(expected_ceiling - total_ceiling),
                round(total_ceiling * 0.15)
            )
            if supplement > 100 and HARD_NUMBERS_ONLY:
                # The expected value comes from a cross-job wall:ceiling
                # ratio, not from this project's drawings. Flag instead of
                # price. (The GSF-based residential ceiling floor — measured
                # footprint × stories — handles the systematic-undercount
                # case with a measurement basis.)
                analysis.setdefault("notes", []).append(
                    f"[Ceiling Check] Ceiling SF is ~{ceiling_gap_pct:.0%} "
                    f"below the wall:ceiling ratio expectation — small "
                    f"spaces (linen/coat closets, pantries) may be missing "
                    f"from extraction. RFI REQUIRED: confirm closet/small-"
                    f"space ceilings on the unit plans (~{supplement:,.0f} "
                    f"sqft NOT priced under hard-numbers policy)."
                )
                print(f"   🔒 Ceiling supplement suppressed (HARD_NUMBERS_ONLY): "
                      f"would have added +{supplement:,.0f} sqft — flagged for RFI")
            elif supplement > 100:
                total_ceiling += supplement
                analysis["aggregated_totals"]["total_paintable_ceiling_sqft"] = total_ceiling
                analysis.setdefault("notes", []).append(
                    f"[Ceiling Supplement] Added {supplement:,.0f} sqft ceiling for estimated "
                    f"missing small spaces (linen closets, pantries, utility closets). "
                    f"Wall:ceiling ratio was {total_wall/(total_ceiling - supplement):.1f}x "
                    f"vs expected ~{WALL_TO_FLOOR_RATIO}x."
                )
                print(f"   📐 Ceiling supplement: +{supplement:,.0f} sqft "
                      f"(missing small spaces in {_units_ceil:.0f} units)")

    # Update room/floor counts (template count vs effective count)
    template_rooms = sum(
        len(f.get("rooms", [])) for f in analysis.get("floors", [])
    )
    effective_rooms = 0
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            effective_rooms += _extract_multiplier_from_notes(room)

    analysis.setdefault("project_info", {})["total_rooms_found"] = effective_rooms
    analysis.setdefault("project_info", {})["template_rooms"] = template_rooms
    analysis.setdefault("project_info", {})["total_floors_analyzed"] = len(analysis.get("floors", []))

    # Track multiplication metadata for reporting
    multiplied_rooms = []
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            mult = _extract_multiplier_from_notes(room)
            if mult > 1:
                multiplied_rooms.append({
                    "room_id": room.get("room_id", ""),
                    "room_name": room.get("room_name", ""),
                    "unit_type": room.get("unit_type", ""),
                    "unit_multiplier": mult,
                    "floor": floor.get("floor_name", ""),
                })
    if multiplied_rooms:
        analysis["unit_multiplication"] = {
            "applied": True,
            "template_rooms": template_rooms,
            "effective_rooms": effective_rooms,
            "details": multiplied_rooms,
        }
    elif "unit_multiplication" in analysis:
        del analysis["unit_multiplication"]

    # Scope summary — track in-scope vs excluded rooms
    in_scope_count = 0
    excluded_count = 0
    excluded_rooms = []
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            multiplier = _extract_multiplier_from_notes(room)
            if room.get("in_scope", True):
                in_scope_count += multiplier
            else:
                excluded_count += multiplier
                excluded_rooms.append({
                    "room_id": room.get("room_id", ""),
                    "room_name": room.get("room_name", ""),
                    "floor": floor.get("floor_name", ""),
                    "reason": room.get("scope_exclusion_reason", ""),
                    "unit_multiplier": multiplier,
                })

    if excluded_count > 0:
        analysis["scope_summary"] = {
            "rooms_in_scope": in_scope_count,
            "rooms_excluded": excluded_count,
            "excluded_rooms": excluded_rooms,
        }

    return analysis


def _apply_whitebox_exclusion(analysis):
    """
    Detect whitebox / prime-only rooms and mark them out of full paint scope.
    These spaces only receive primer, not full paint — Rider does not include
    them in the paint estimate.
    """
    _whitebox_keywords = (
        "whitebox", "white box", "prime only", "prime-only",
        "shell condition", "white shell", "vanilla box", "warm shell",
    )

    excluded_count = 0
    excluded_rooms = []

    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            if not room.get("in_scope", True):
                continue

            rname = str(room.get("room_name", "")).lower()
            rnotes = str(room.get("notes", "")).lower()

            is_whitebox = (
                any(kw in rname for kw in _whitebox_keywords)
                or any(kw in rnotes for kw in _whitebox_keywords)
                or room.get("elements", {}).get("prime_only", False)
            )

            if is_whitebox:
                room["in_scope"] = False
                room["scope_exclusion_reason"] = (
                    "Whitebox/prime only — excluded from full paint scope "
                    "(primer only, no finish painting)"
                )
                multiplier = _extract_multiplier_from_notes(room)
                excluded_count += multiplier
                excluded_rooms.append(room.get("room_name", "Unknown"))

    if excluded_count > 0:
        detail = ", ".join(excluded_rooms[:5])
        if len(excluded_rooms) > 5:
            detail += f" and {len(excluded_rooms) - 5} more"
        analysis.setdefault("notes", []).append(
            f"[Whitebox Exclusion] {excluded_count} room(s) marked as whitebox/prime-only "
            f"and excluded from paint scope: {detail}"
        )
        print(f"   🏪 Whitebox exclusion: {excluded_count} room(s) excluded from paint scope")
        # Recalculate totals since we changed in_scope flags
        analysis = _recalculate_totals(analysis)

    return analysis


def _apply_commercial_window_exclusion(analysis):
    """
    For commercial (non-residential) buildings, zero out painted window SASHES.
    Commercial sashes (storefront, aluminum-frame, factory-finished) are not
    field-painted. Aprons, casings, stools, and wood returns called out in the
    window schedule, finish schedule, wall sections, or interior elevations are
    PRESERVED — they may still require paint.

    Adds RFI flag for estimator to confirm or override.
    """
    pi = analysis.get("project_info", {})
    agg = analysis.get("aggregated_totals", {})
    building_type = str(pi.get("building_type", "")).lower()

    _has_commercial = "commercial" in building_type
    _has_residential_kw = any(kw in building_type
                              for kw in ("residential", "apartment", "condo"))
    is_commercial_for_windows = _has_commercial and not _has_residential_kw
    # Also catch auto dealerships, industrial, warehouses, retail-only
    if not is_commercial_for_windows:
        is_commercial_for_windows = any(kw in building_type for kw in (
            "auto", "dealership", "industrial", "warehouse"))
    # Retail-only (no residential) counts as commercial
    if not is_commercial_for_windows and "retail" in building_type and not _has_residential_kw:
        is_commercial_for_windows = True
    # Mixed-use with residential keyword → NOT commercial for windows
    if is_commercial_for_windows and _has_residential_kw:
        is_commercial_for_windows = False

    if not is_commercial_for_windows:
        return analysis

    # Preserve painted-component counts (apron/casing/stool/wood return) — these
    # may be explicitly called out and still require field paint on commercial.
    apron_ct = _num(agg.get("total_window_aprons_painted", 0))
    casing_ct = _num(agg.get("total_window_casings_painted", 0))
    stool_ct = _num(agg.get("total_window_stools_painted", 0))
    return_ct = _num(agg.get("total_window_wood_returns_painted", 0))
    has_painted_components = (apron_ct + casing_ct + stool_ct + return_ct) > 0

    current_windows = _num(agg.get("total_windows_painted_interior", 0))
    if current_windows > 0:
        print(f"   ⚠️  Commercial building — zeroing {current_windows:.0f} painted window sashes "
              f"(commercial sashes assumed factory-finished, not field-painted)")
        if has_painted_components:
            print(f"      Preserving painted components: aprons={apron_ct:.0f}, "
                  f"casings={casing_ct:.0f}, stools={stool_ct:.0f}, wood returns={return_ct:.0f}")
        analysis.setdefault("notes", []).append(
            f"[Commercial Window Exclusion] Zeroed {current_windows:.0f} painted window sashes — "
            f"commercial sashes assumed factory-finished (storefront/aluminum/vinyl). "
            f"Painted aprons/casings/stools/returns preserved if explicitly called out. "
            f"RFI: If sashes require field paint, provide window schedule with finish specs."
        )
        agg["total_windows_painted_interior"] = 0
        analysis["aggregated_totals"] = agg
        # Also zero at room level for consistency (sashes only)
        for floor in analysis.get("floors", []):
            for room in floor.get("rooms", []):
                room.get("elements", {})["windows_painted_interior"] = 0

    analysis["_commercial_windows_excluded"] = True
    return analysis


def _check_wall_ceiling_ratio(analysis):
    """
    Final sanity check: wall sqft / ceiling sqft should approximate 3.3x
    for residential, with wider ranges for other building types.

    If ratio is outside acceptable bounds, flags a warning and RFI item
    so the estimator knows the numbers may be unreliable.
    Does NOT modify totals — informational warning only.
    """
    agg = analysis.get("aggregated_totals", {})
    pi = analysis.get("project_info", {})

    total_wall = _num(agg.get("total_paintable_wall_sqft", 0))
    total_ceiling = _num(agg.get("total_paintable_ceiling_sqft", 0))

    # Skip check if either value is 0 (already flagged by other validations)
    if total_ceiling == 0 or total_wall == 0:
        return analysis

    # Skip if footprint fallback was used (calibrated values, ratio is baked in)
    if analysis.get("_used_footprint_fallback"):
        return analysis

    ratio = total_wall / total_ceiling

    # Building-type-aware bounds
    building_type = str(pi.get("building_type", "")).lower()

    if any(kw in building_type for kw in ("single", "detached")):
        ratio_low, ratio_high = 2.0, 6.0
        expected = 3.4
    elif any(kw in building_type for kw in ("multi", "mixed", "apartment", "condo", "residential")):
        ratio_low, ratio_high = 1.8, 6.0
        expected = 3.3
    elif any(kw in building_type for kw in ("commercial", "retail", "auto", "dealership", "industrial")):
        ratio_low, ratio_high = 1.2, 12.0
        expected = 3.0
    else:
        ratio_low, ratio_high = 1.5, 10.0
        expected = 3.3

    if ratio < ratio_low or ratio > ratio_high:
        analysis.setdefault("notes", []).append(
            f"[Wall:Ceiling Ratio] ALERT: ratio is {ratio:.1f}x "
            f"(expected {ratio_low:.1f}x-{ratio_high:.1f}x for {pi.get('building_type', 'unknown')}). "
            f"Walls={total_wall:,.0f} sqft, Ceilings={total_ceiling:,.0f} sqft. "
            f"Extraction may be unreliable — verify before sending estimate."
        )
        analysis["_wall_ceiling_ratio_alert"] = {
            "ratio": round(ratio, 2),
            "expected_range": [ratio_low, ratio_high],
            "severity": "high",
        }
        print(f"   ⚠️  Wall:Ceiling ratio {ratio:.1f}x is outside expected range "
              f"({ratio_low:.1f}x-{ratio_high:.1f}x) — extraction may be unreliable")
    else:
        analysis.setdefault("notes", []).append(
            f"[Wall:Ceiling Ratio] OK: {ratio:.1f}x "
            f"(expected ~{expected}x for {pi.get('building_type', 'unknown')})"
        )
        print(f"   ✅ Wall:Ceiling ratio {ratio:.1f}x within expected range for {pi.get('building_type', 'unknown')}")

    return analysis


def _classify_rfi_topic(text):
    """Map an RFI question/action text to a normalized topic key.

    More-specific patterns are checked first so e.g. "missing ceiling
    heights" classifies as ceiling_heights, not building_sections.
    """
    t = (text or "").lower()
    if "ceiling height" in t:
        return "ceiling_heights"
    if "building section" in t:
        return "building_sections"
    if "door schedule" in t or "door classification" in t or "door type" in t:
        return "door_schedule"
    if "window schedule" in t or "window paint scope" in t or "window sash" in t:
        return "window_schedule"
    if "finish schedule" in t:
        return "finish_schedule"
    if "wall area" in t or "wall perimeter" in t or "wall dimension" in t:
        return "wall_dimensions"
    if "wallcovering" in t or "wallpaper" in t:
        return "wallcoverings"
    if "wall material" in t or "wall type" in t:
        return "material_specs"
    if "floor plan" in t:
        return "floor_plans"
    if "exterior" in t and ("elevation" in t or "scope" in t or "cornice" in t or "soffit" in t):
        return "exterior_scope"
    if "prevailing wage" in t:
        return "prevailing_wage"
    if "drawing set" in t and ("incomplete" in t or "missing" in t or "partial" in t):
        return "partial_drawing_set"
    return "other"


def _detect_answered_topics(analysis):
    """
    Inspect the aggregated analysis and decide which RFI topics have been
    resolved downstream — so we can suppress stale per-sheet RFIs.

    Returns (answered, fallback):
      answered: topics filled from a high-confidence source (Building Level
                Schedule, RCP dimension text, plan keynote, building section).
                Per-sheet RFIs on these topics are dropped.
      fallback: topics filled only by a typical-value fallback (e.g. 9'
                typical residential). Per-sheet RFIs on these are kept but
                rewritten as "Assumption Used" rather than "Critical".
    """
    answered = set()
    fallback = set()

    rooms = []
    for floor in analysis.get("floors", []):
        rooms.extend(floor.get("rooms", []))

    notes_text = " ".join(str(n).lower() for n in analysis.get("notes", []))
    for r in rooms:
        notes_text += " " + str(r.get("notes", "")).lower()
        notes_text += " " + str(r.get("dimensions", {}).get("notes", "")).lower()

    # ---- ceiling_heights / building_sections ----
    if rooms:
        with_ceil = [r for r in rooms
                     if _num(r.get("dimensions", {}).get("ceiling_height_feet", 0)) > 0]
        coverage = len(with_ceil) / len(rooms)
        real_source_phrases = (
            "building level schedule",
            "pre-extracted dimension",
            "plan notation",
            "per plan",
            "from building section",
            "section level differences",
            "level schedule differences",
        )
        fallback_phrases = (
            "typical residential",
            "typical commercial",
            "9' typical",
            "9'-0\" typical",
        )
        has_real = any(p in notes_text for p in real_source_phrases)
        has_fallback = any(p in notes_text for p in fallback_phrases)
        if coverage >= 0.80 and has_real:
            answered.add("ceiling_heights")
            answered.add("building_sections")
        elif coverage >= 0.80 and has_fallback:
            fallback.add("ceiling_heights")
            fallback.add("building_sections")

    # ---- wall_dimensions ----
    agg = analysis.get("aggregated_totals", {})
    if _num(agg.get("total_paintable_wall_sqft", 0)) > 0:
        answered.add("wall_dimensions")

    # ---- floor_plans ----
    if rooms:
        measurable = [r for r in rooms
                      if _num(r.get("dimensions", {}).get("wall_area_sqft", 0)) > 0
                      or _num(r.get("dimensions", {}).get("perimeter_lf", 0)) > 0]
        if len(measurable) / len(rooms) >= 0.80:
            answered.add("floor_plans")

    # ---- material_specs / finish_schedule ----
    if rooms:
        with_mat = [r for r in rooms
                    if str(r.get("materials", {}).get("walls", "")).strip().lower()
                    not in ("", "unknown")]
        if len(with_mat) / len(rooms) >= 0.80:
            answered.add("material_specs")
            answered.add("finish_schedule")

    return answered, fallback


def _reconcile_rfi_items(items, analysis):
    """
    Post-process the RFI list:
      1. Suppress per-sheet "Our review noted" / "Our analysis noted" RFIs
         whose topic has been answered by aggregation.
      2. Downgrade the tone of per-sheet RFIs whose topic was only filled by
         a typical fallback (kept visible as "Assumption Used", not Critical).
      3. Dedupe surviving per-sheet RFIs by topic — if multiple sheets flagged
         the same topic, keep the first and append the additional sheet refs.

    Dedicated RFIs (door schedule, window schedule, exterior scope, pricing,
    etc.) are never suppressed — those were already gated on aggregation
    checks at the call site.
    """
    answered, fallback = _detect_answered_topics(analysis)
    PER_SHEET_PREFIXES = ("Our review noted", "Our analysis noted")

    surviving = []
    suppressed = 0
    deduped = 0
    downgraded = 0
    seen_by_topic = {}

    for it in items:
        question = it.get("question") or ""
        is_per_sheet = question.startswith(PER_SHEET_PREFIXES)
        if not is_per_sheet:
            surviving.append(it)
            continue

        text = question + " " + (it.get("action_required") or "")
        topic = _classify_rfi_topic(text)

        if topic in answered:
            suppressed += 1
            continue

        if topic in fallback:
            it = dict(it)
            sheet_match = re.search(r'\[([^\]]+\.pdf)\]', question)
            sheet_ref = f" (per {sheet_match.group(1)})" if sheet_match else ""
            it["category"] = "Assumption Used"
            if topic in ("ceiling_heights", "building_sections"):
                it["question"] = (
                    f"Ceiling heights for some rooms were not shown on building "
                    f"sections; we assumed 9'-0\" typical residential{sheet_ref}. "
                    f"Please confirm or provide building sections for verification."
                )
                it["action_required"] = (
                    "Confirm typical 9'-0\" residential ceiling height, or "
                    "provide building sections / RCP dimension callouts."
                )
            downgraded += 1

        # Aliases: topics that ask the same question to the user collapse together.
        dedupe_key = {"building_sections": "ceiling_heights"}.get(topic, topic)

        if topic != "other" and dedupe_key in seen_by_topic:
            prior = seen_by_topic[dedupe_key]
            sheet_match = re.search(r'\[([^\]]+\.pdf)\]', question)
            if sheet_match:
                sheet_name = sheet_match.group(1)
                prior_q = prior.get("question") or ""
                if sheet_name not in prior_q:
                    if "(also referenced on:" in prior_q:
                        prior["question"] = prior_q.rstrip().rstrip(")") + f", {sheet_name})"
                    else:
                        prior["question"] = prior_q.rstrip() + f" (also referenced on: {sheet_name})"
            deduped += 1
            continue

        if topic != "other":
            seen_by_topic[dedupe_key] = it
        surviving.append(it)

    if suppressed or deduped or downgraded:
        print(f"   📋 RFI reconciliation: suppressed {suppressed} answered, "
              f"deduped {deduped} duplicates, downgraded {downgraded} to assumption")

    return surviving


def generate_rfi_items(analysis):
    """
    Scan the analysis dict for missing/incomplete data and return
    a list of RFI (Request For Information) item dicts.

    Each item:
        {"number": int, "category": str, "question": str, "action_required": str}

    Categories:
        "Missing Drawings", "Incomplete Dimensions", "Missing Schedules",
        "Material Specifications", "Clarification Needed", "Assumption Used"

    Returns [] if no issues found.
    """
    items = []

    # Sheet numbers physically present in the upload — used so RFIs don't
    # request drawings the client already provided.
    upload_sheets = set(analysis.get("_upload_sheet_numbers") or [])

    # --- 0. Hard-numbers base-trim suppression ---
    # When _recalculate_totals zeroed perimeter-default base trim on a
    # commercial job (resilient/unconfirmed base), surface it as an RFI so the
    # estimator confirms whether any base is actually painted.
    _bt_sup = analysis.get("_hard_numbers_base_trim_suppressed")
    if _bt_sup and _bt_sup.get("rooms"):
        items.append({
            "category": "Material Specifications",
            "question": (
                f"Base trim was excluded from the paint scope on {_bt_sup['rooms']} "
                f"room(s) (~{_bt_sup.get('lf', 0):,} LF). The drawings/finish schedule "
                f"do not confirm a painted base — commercial/retail spaces typically use "
                f"resilient or vinyl cove base, which is not field-painted. Is any base "
                f"trim painted on this project? If so, which rooms and what base material?"
            ),
            "action_required": (
                "Confirm whether base trim is painted and identify the base material "
                "(painted wood/MDF vs. resilient/vinyl cove base) per room or per the "
                "finish schedule."
            ),
        })

    # --- 0b. Hard-numbers suppressed scope ---
    # Heuristics gated by HARD_NUMBERS_ONLY (dryfall recovery, secondary-
    # space supplement, door supplement, wall boost, stair note parsing,
    # ceiling supplement) record the scope they would have added as a note
    # containing "RFI REQUIRED:". Surface each as an RFI item so the
    # unpriced exposure is visible to the estimator and customer instead
    # of existing only as a buried note. Notes (unlike custom underscore
    # keys) survive the multi-file merge, so this works on combined jobs.
    _seen_rfi_notes = set()
    for _n in analysis.get("notes", []) or []:
        if not (isinstance(_n, str) and "RFI REQUIRED:" in _n):
            continue
        _q = _n.split("RFI REQUIRED:", 1)[1].strip()
        if not _q or _q in _seen_rfi_notes:
            continue
        _seen_rfi_notes.add(_q)
        items.append({
            "category": "Clarification Needed",
            "question": _q,
            "action_required": (
                "Confirm the quantity from the drawings/schedules — it is "
                "NOT included in the priced takeoff under the hard-numbers "
                "policy."
            ),
        })

    # --- 1. No floor plans found ---
    if analysis.get("no_floor_plans_found") or analysis.get("no_detailed_floor_plans_found"):
        # Contradiction guard: dimensioned, non-synthetic rooms can only be
        # measured off floor plans — if we have them, the plans were uploaded.
        _measured_rooms = 0
        for _fl in analysis.get("floors", []):
            for _rm in _fl.get("rooms", []):
                if not _rm.get("in_scope", True) or _rm.get("source") == "schedule_estimate":
                    continue
                if _num(_rm.get("dimensions", {}).get("wall_area_sqft", 0)) > 0:
                    _measured_rooms += 1
        if _measured_rooms >= 3:
            print(f"   📋 RFI: suppressed 'no floor plans' — {_measured_rooms} "
                  f"dimensioned rooms were extracted (floor plans were present)")
        else:
            items.append({
                "category": "Missing Drawings",
                "question": (
                    "The provided drawing set does not include architectural floor plans with "
                    "dimensions. Can you provide the complete architectural plan sheets "
                    "(A1.x, A2.x series) so we can measure wall areas, ceiling areas, and "
                    "perimeter lengths for all rooms?"
                ),
                "action_required": "Provide architectural floor plan sheets with dimension callouts."
            })

    # --- 2. No door schedule ---
    if analysis.get("has_door_schedule") is False:
        items.append({
            "category": "Missing Schedules",
            "question": (
                "No door schedule was found in the provided documents. Door type "
                "(hollow metal vs. wood) determines our painting scope and pricing "
                "per unit. Without the schedule, door counts are estimated from the "
                "floor plans and commonly over-count field-painted doors — every "
                "room shows its doors, and prefinished doors cannot be ruled out. "
                "Treat the door quantities in this estimate as preliminary. Can you "
                "provide the door schedule sheets (typically A-501/A-502)?"
            ),
            "action_required": "Provide door schedule sheet(s) showing door types, materials, and frame specifications."
        })

    # --- 3. No window schedule ---
    if analysis.get("has_window_schedule") is False:
        items.append({
            "category": "Missing Schedules",
            "question": (
                "No window schedule was found in the provided documents. The window "
                "TYPE is required to determine paint scope — specifically whether each "
                "window has a casing, apron, stool/sill, wood return, drywall return, "
                "or any paintable interior trim. Without the window schedule and TYPE "
                "details we cannot estimate window paint scope and have set window "
                "paint quantities to zero. Can you provide the window schedule and "
                "associated window TYPE detail drawings?"
            ),
            "action_required": (
                "Provide window schedule with TYPE details showing frame material, "
                "casing, apron, stool/sill, wood return, drywall return, and "
                "field-paint specifications."
            )
        })

    # --- 3b. No finish schedule ---
    if analysis.get("has_finish_schedule") is False:
        _agg_fs = analysis.get("aggregated_totals", {})
        _bt_fs = _num(_agg_fs.get("total_base_trim_lf", 0))
        _bt_clause = (
            f" In particular, all {_bt_fs:,.0f} LF of base trim is priced as "
            f"paintable — if any rooms have resilient/vinyl/rubber cove base, that "
            f"footage is not field-painted and should be deducted."
            if _bt_fs > 0 else ""
        )
        items.append({
            "category": "Missing Schedules",
            "question": (
                "No room finish schedule was found in the provided documents. The "
                "finish schedule determines wall, ceiling, and base materials per "
                "room — which surfaces are paintable gypsum versus CMU, ACT, or "
                "resilient cove base. Without it the estimate relies on typical "
                f"assumptions.{_bt_clause} Can you provide the room finish schedule "
                "and finish legend sheets?"
            ),
            "action_required": (
                "Provide the room finish schedule / finish legend sheets showing "
                "wall, ceiling, and base finishes per room."
            )
        })

    # --- 4. Rooms with zero dimensions (grouped by floor) ---
    for floor in analysis.get("floors", []):
        floor_name = floor.get("floor_name", "Unknown Floor")
        zero_rooms = []
        for room in floor.get("rooms", []):
            dims = room.get("dimensions", {})
            if _num(dims.get("wall_area_sqft", 0)) == 0 and _num(dims.get("perimeter_lf", 0)) == 0:
                name = room.get("room_name", room.get("room_id", "Unknown"))
                zero_rooms.append(name)
        if zero_rooms:
            # Truncate long lists
            if len(zero_rooms) > 8:
                room_list = ", ".join(zero_rooms[:8]) + f", and {len(zero_rooms) - 8} more"
            else:
                room_list = ", ".join(zero_rooms)
            items.append({
                "category": "Incomplete Dimensions",
                "question": (
                    f"The following rooms on {floor_name} have no measurable dimensions: "
                    f"{room_list}. Updated plans with dimension callouts are needed to "
                    f"calculate wall and ceiling areas for these spaces."
                ),
                "action_required": f"Provide dimensioned floor plans for {floor_name} or confirm room sizes."
            })

    # --- 5. Unknown wall materials ---
    unknown_mat_rooms = []
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            wall_mat = str(room.get("materials", {}).get("walls", "")).strip()
            if wall_mat.lower() == "unknown":
                name = room.get("room_name", room.get("room_id", "Unknown"))
                unknown_mat_rooms.append(name)
    if unknown_mat_rooms:
        if len(unknown_mat_rooms) > 8:
            room_list = ", ".join(unknown_mat_rooms[:8]) + f", and {len(unknown_mat_rooms) - 8} more"
        else:
            room_list = ", ".join(unknown_mat_rooms)
        items.append({
            "category": "Material Specifications",
            "question": (
                f"Wall materials for the following rooms could not be determined from "
                f"the provided drawings: {room_list}. We need to know which walls are "
                f"gypsum board (paintable) versus CMU or other non-paintable surfaces."
            ),
            "action_required": "Provide finish schedule or wall type legend identifying wall materials for these spaces."
        })

    # --- 6. missing_for_painting_estimate list ---
    for item_text in analysis.get("missing_for_painting_estimate", []):
        # Don't echo a request for sheets the client already uploaded.
        referenced, present, missing = _sheets_in_text(item_text, upload_sheets)
        if referenced and not missing:
            print(f"   📋 RFI: skipped \"{str(item_text)[:55]}\" — referenced "
                  f"sheet(s) {', '.join(present)} are in the upload")
            continue
        items.append({
            "category": "Clarification Needed",
            "question": (
                f"Our analysis noted: \"{item_text}\". Can you provide this information "
                f"or the relevant drawing sheets?"
            ),
            "action_required": "Provide the referenced document or clarification."
        })

    # --- 7. drawings_referenced_but_not_included ---
    for sheet in analysis.get("drawings_referenced_but_not_included", []):
        # Skip if that sheet is actually in the upload (stale reference).
        if _normalize_sheet_token(sheet) in upload_sheets:
            print(f"   📋 RFI: skipped 'missing sheet {sheet}' — it is in the upload")
            continue
        items.append({
            "category": "Missing Drawings",
            "question": (
                f"Drawing sheet \"{sheet}\" is referenced in the document index but was "
                f"not included in the provided PDF set. This sheet may contain information "
                f"needed for our estimate."
            ),
            "action_required": f"Include {sheet} in the drawing set."
        })

    # --- 8. Notes with gap keywords (deduplicated) ---
    gap_keywords = ["missing", "not found", "not included", "cannot be completed",
                     "unclear", "not clearly"]
    existing_q_lower = " ".join(q["question"].lower() for q in items)
    for note in analysis.get("notes", []):
        note_lower = str(note).lower()
        if any(kw in note_lower for kw in gap_keywords):
            # Deduplicate: skip if core content already covered
            words = note_lower.split()
            already_covered = False
            for i in range(len(words) - 3):
                phrase = " ".join(words[i:i + 4])
                if phrase in existing_q_lower:
                    already_covered = True
                    break
            if not already_covered:
                items.append({
                    "category": "Clarification Needed",
                    "question": (
                        f"Our review noted: \"{note}\". Can you provide additional "
                        f"information to address this?"
                    ),
                    "action_required": "Provide clarification or the referenced documents."
                })
                existing_q_lower += " " + note_lower

    # --- 9. Zero windows when rooms exist ---
    agg = analysis.get("aggregated_totals", {})
    total_rooms = sum(len(f.get("rooms", [])) for f in analysis.get("floors", []))
    if (_num(agg.get("total_windows_all", 0)) == 0
            and total_rooms > 0
            and analysis.get("has_window_schedule") is False):
        items.append({
            "category": "Clarification Needed",
            "question": (
                "No windows were identified in the analysis. If this building has windows, "
                "please confirm whether any require interior painting and provide the "
                "window schedule."
            ),
            "action_required": "Confirm window count and interior painting requirements."
        })

    # --- 9b. Aprons called out without a window schedule ---
    # Aprons can show up in the finish schedule, wall sections, or interior
    # elevations. Without a window schedule we cannot compute an accurate apron
    # count (one apron per window), so flag for RFI.
    aprons_called_out = analysis.get("aprons_called_out", False)
    apron_source = analysis.get("aprons_callout_source", "")
    no_win_sched = analysis.get("has_window_schedule") is False
    sched_apron_ct = _num(agg.get("total_window_aprons_painted", 0))
    if aprons_called_out and no_win_sched and sched_apron_ct == 0:
        src_phrase = (
            f" in the {apron_source}" if apron_source else
            " in the finish schedule, wall sections, or interior elevations"
        )
        items.append({
            "category": "Clarification Needed",
            "question": (
                f"Apron trim is called out{src_phrase}, but no window schedule "
                f"was provided. We need accurate window counts from the window "
                f"schedule to compute the apron quantity (one apron per window). "
                f"Can you provide the window schedule?"
            ),
            "action_required": (
                "Provide window schedule so apron quantities can be computed "
                "from the per-type window counts."
            )
        })

    # --- 10. Exterior all zeros on multi-floor building ---
    ext = analysis.get("exterior", {})
    floors_count = len(analysis.get("floors", []))
    ext_all_zero = (
        _num(ext.get("cornice_lf", 0)) == 0
        and _num(ext.get("window_trim_lf", 0)) == 0
        and _num(ext.get("soffit_sqft", 0)) == 0
        and _num(ext.get("railing_lf", 0)) == 0
    )
    if ext_all_zero and floors_count > 1:
        items.append({
            "category": "Clarification Needed",
            "question": (
                "No exterior painting scope was identified. For a multi-story building, "
                "please confirm whether exterior elements (cornice, soffits, railings) "
                "require painting and provide building elevation sheets."
            ),
            "action_required": "Provide building elevation sheets and confirm exterior painting scope."
        })

    # --- 11. Commercial windows excluded ---
    if analysis.get("_commercial_windows_excluded"):
        items.append({
            "category": "Clarification Needed",
            "question": (
                "Our estimate assumes no window SASHES require field paint on this "
                "commercial project (sashes are factory-finished — storefront, aluminum, "
                "vinyl, or clad). Painted aprons, casings, stools, and wood returns "
                "called out in the window schedule, finish schedule, wall sections, or "
                "interior elevations are still included. If any sashes DO require field "
                "paint, please provide the window schedule with explicit field-paint "
                "specifications."
            ),
            "action_required": (
                "Confirm window sashes are factory-finished (no field paint), or "
                "provide window schedule with field-paint specs for any sashes that "
                "require painting."
            )
        })

    # --- 12. Wall:Ceiling ratio alert ---
    ratio_alert = analysis.get("_wall_ceiling_ratio_alert")
    if ratio_alert and ratio_alert.get("severity") == "high":
        items.append({
            "category": "Clarification Needed",
            "question": (
                f"Our automated validation detected an unusual wall-to-ceiling ratio "
                f"({ratio_alert['ratio']:.1f}x, expected {ratio_alert['expected_range'][0]:.1f}x-"
                f"{ratio_alert['expected_range'][1]:.1f}x). This may indicate missing rooms "
                f"or miscounted ceiling areas. Can you verify the drawing set is complete?"
            ),
            "action_required": "Confirm drawing set is complete and all floor plans are included."
        })

    # Reconcile against aggregated analysis: drop per-sheet RFIs whose data
    # was answered downstream (Building Level Schedule, RCP dimension text,
    # plan keynotes, material aggregation) and collapse duplicate per-topic
    # reports across sheets.
    items = _reconcile_rfi_items(items, analysis)

    # Assign sequential numbers
    for i, item in enumerate(items, 1):
        item["number"] = i

    return items


def merge_analyses(analyses, file_building_counts=None):
    """
    Merge multiple per-file analyses into one combined analysis.
    Deduplicates rooms and recalculates totals from room-level data
    (never sums per-file aggregated_totals — that causes double-counting).

    file_building_counts: optional dict mapping pdf_path → building count
        parsed from filenames (e.g., "BLDG 1-3" → 3). When a file covers
        multiple identical buildings, all room unit_multipliers from that
        file are scaled by the building count.
    """
    if file_building_counts is None:
        file_building_counts = {}
    combined = {
        "project_info": {
            "total_floors_analyzed": 0,
            "total_rooms_found": 0,
            "scale_notation": "",
            "source_files": []
        },
        "floors": [],
        "aggregated_totals": {},
        "exterior": {},
        "material_legend": [],
        "notes": []
    }

    seen_scales = set()
    seen_legend_codes = set()
    seen_floor_names = set()

    for file_path, analysis in analyses:
        filename = os.path.basename(file_path)
        combined["project_info"]["source_files"].append(filename)

        # Skip files with no floor plans — carry forward notes and schedule data
        if analysis.get("no_floor_plans_found") or analysis.get("no_detailed_floor_plans_found"):
            pages_note = analysis.get("pages_reviewed", "")
            if pages_note:
                combined["notes"].append(f"{filename}: {pages_note}")
            proj = analysis.get("project_info", {})
            for key in ("project_name", "location", "architect", "drawing_date", "building_type"):
                if proj.get(key) and key not in combined["project_info"]:
                    combined["project_info"][key] = proj[key]
            # Preserve schedule data extracted by analyze_schedule_pdf()
            for key in ("door_schedule", "window_schedule", "stair_info"):
                if analysis.get(key):
                    combined.setdefault("schedule_data", {})[key] = analysis[key]
            continue

        proj = analysis.get("project_info", {})

        # Carry forward descriptive project info
        for key in ("project_name", "location", "architect", "drawing_date", "building_type"):
            if proj.get(key) and key not in combined["project_info"]:
                combined["project_info"][key] = proj[key]

        scale = proj.get("scale_notation", "")
        if scale and scale not in seen_scales:
            seen_scales.add(scale)
            if combined["project_info"]["scale_notation"]:
                combined["project_info"]["scale_notation"] += f"; {scale}"
            else:
                combined["project_info"]["scale_notation"] = scale

        # Apply building multiplier from filename (e.g., "BLDG 1-3" → ×3)
        bldg_count = file_building_counts.get(file_path, 1)
        if bldg_count > 1:
            combined["notes"].append(
                f"[{filename}] Building count: {bldg_count} — "
                f"all room multipliers scaled ×{bldg_count}"
            )

        # Merge floors — combine rooms if same floor name appears in multiple files
        for floor in analysis.get("floors", []):
            floor_name = floor.get("floor_name", "Unknown")
            for room in floor.get("rooms", []):
                room["source_file"] = filename
                # Scale unit_multiplier by building count
                if bldg_count > 1:
                    current_mult = room.get("unit_multiplier", 1)
                    if not isinstance(current_mult, (int, float)) or current_mult < 1:
                        current_mult = 1
                    room["unit_multiplier"] = int(current_mult) * bldg_count

            if floor_name in seen_floor_names:
                for existing in combined["floors"]:
                    if existing["floor_name"] == floor_name:
                        existing["rooms"].extend(floor.get("rooms", []))
                        break
            else:
                seen_floor_names.add(floor_name)
                combined["floors"].append(floor)

        # Merge exterior — keep the most detailed (compare sum of all LF/sqft fields)
        ext = analysis.get("exterior", {})
        if ext:
            new_score = (_num(ext.get("cornice_lf", 0)) +
                         _num(ext.get("window_trim_lf", 0)) +
                         _num(ext.get("soffit_sqft", 0)) +
                         _num(ext.get("railing_lf", 0)))
            old_score = (_num(combined["exterior"].get("cornice_lf", 0)) +
                         _num(combined["exterior"].get("window_trim_lf", 0)) +
                         _num(combined["exterior"].get("soffit_sqft", 0)) +
                         _num(combined["exterior"].get("railing_lf", 0)))
            if new_score > old_score:
                combined["exterior"] = ext

        # Merge material legend (deduplicate by code)
        for entry in analysis.get("material_legend", []):
            code = entry.get("code", "")
            if code and code not in seen_legend_codes:
                seen_legend_codes.add(code)
                combined["material_legend"].append(entry)

        # Merge notes
        for note in analysis.get("notes", []):
            combined["notes"].append(f"[{filename}] {note}")

        # Carry forward schedule estimation metadata and building info
        if analysis.get("schedule_estimated"):
            combined["schedule_estimated"] = True
            bi = analysis.get("building_info", {})
            if bi:
                combined["building_info"] = bi

        # Carry forward room finish schedule (extend across files; later files win on duplicates)
        rfs = analysis.get("room_finish_schedule")
        if rfs:
            combined.setdefault("room_finish_schedule", []).extend(rfs)

        # Carry forward schedule data from schedule-estimated files
        # (these files now have synthetic floors, so they don't hit the no_floor_plans skip above)
        for key in ("door_schedule", "window_schedule", "stair_info"):
            if analysis.get(key):
                combined.setdefault("schedule_data", {})[key] = analysis[key]
            elif analysis.get("schedule_data", {}).get(key):
                combined.setdefault("schedule_data", {})[key] = analysis["schedule_data"][key]

    # Deduplicate rooms within each floor
    all_dedup_logs = []
    for floor in combined["floors"]:
        floor["rooms"], dedup_log = _deduplicate_rooms(floor.get("rooms", []))
        all_dedup_logs.extend(dedup_log)

    if all_dedup_logs:
        combined["deduplication_report"] = all_dedup_logs
        combined.setdefault("notes", []).append(
            f"[Deduplication] {len(all_dedup_logs)} duplicate room(s) resolved across files"
        )
        print(f"\n🔍 Deduplication: {len(all_dedup_logs)} duplicate(s) resolved")
        for entry in all_dedup_logs[:5]:
            print(f"   • Kept {entry['kept']}, removed {entry['removed']}: {entry['reason']}")
        if len(all_dedup_logs) > 5:
            print(f"   ... and {len(all_dedup_logs) - 5} more")

    # Normalize scope fields before recalculation
    combined = _normalize_scope_fields(combined)

    # Recalculate all totals from the merged, deduplicated room data
    combined = _recalculate_totals(combined)

    # Apply schedule overrides (door/window/stair counts from schedule PDFs)
    combined = _apply_schedule_overrides(combined)

    return combined


def _num(val):
    """Coerce a value to a number. Handles strings like '1,234' or '1234.5'."""
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        try:
            return float(val.replace(",", "").strip())
        except ValueError:
            return 0
    return 0


def _recover_area_fields(dims):
    """Fill missing/zero high-impact area fields from a room's geometry instead
    of letting a null coerce to a destructive 0.

    A degraded vision pass can return e.g. wall_area_sqft=null while still
    reporting length/width/height — coercing that null to 0 (the old behavior)
    permanently undercounts the room. When the geometric inputs exist we
    reconstruct the area; when they don't, the field stays 0 and the caller
    counts it toward the degraded-extraction gate.

    Only fills fields that are <=0 (null/absent/zero), so a legitimately
    extracted positive value is never overwritten — keeps the no-op property
    on well-formed data. Mutates `dims` in place; returns nothing.
    """
    L = _num(dims.get("length_feet"))
    W = _num(dims.get("width_feet"))
    H = _num(dims.get("ceiling_height_feet"))
    P = _num(dims.get("perimeter_lf"))
    if P <= 0 and L > 0 and W > 0:
        P = 2 * (L + W)
        dims["perimeter_lf"] = round(P)
    floor_area = _num(dims.get("floor_area_sqft"))
    if floor_area <= 0 and L > 0 and W > 0:
        floor_area = L * W
        dims["floor_area_sqft"] = round(floor_area)
    if _num(dims.get("wall_area_sqft")) <= 0 and P > 0 and H > 0:
        dims["wall_area_sqft"] = round(P * H)
    # Ceiling area tracks floor area for a flat ceiling — the standard
    # takeoff assumption already used elsewhere in the pipeline.
    if _num(dims.get("ceiling_area_sqft")) <= 0 and floor_area > 0:
        dims["ceiling_area_sqft"] = round(floor_area)


# Numeric fields whose corruption materially changes the estimate. When one of
# these arrives structurally-wrong (list/dict) or null/garbage, we don't just
# coerce silently — we route the whole job to manual review so a degraded
# extraction can't ship a wrong number to a customer (Wingstop Aliante,
# 2026-06-08: a null wall_area_sqft coerced to 0 would have undercounted).
_HIGH_IMPACT_NUMERIC = {
    "wall_area_sqft", "floor_area_sqft", "ceiling_area_sqft", "perimeter_lf",
    "total_stories", "total_units", "footprint_sqft",
}
_ROOM_DIM_NUM = ("wall_area_sqft", "floor_area_sqft", "ceiling_area_sqft",
                 "perimeter_lf", "ceiling_height_feet")
_ROOM_STR = ("room_name", "unit_type", "floor_name", "room_id", "source_sheet")


def _coerce_str(val):
    """Coerce a value to a stripped string. Joins lists — Eastern's bug was
    Claude returning `notes` as a list, which crashed re.search()."""
    if isinstance(val, str):
        return val.strip()
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        return " ".join(_coerce_str(x) for x in val).strip()
    return str(val).strip()


def _is_parseable_number(val):
    """True if val is numeric or a string that parses as a number."""
    if isinstance(val, (int, float)):
        return True
    if isinstance(val, str):
        try:
            float(val.replace(",", "").strip())
            return True
        except ValueError:
            return False
    return False


def _normalize_analysis(analysis):
    """Coerce a Claude-produced analysis dict to its expected schema BEFORE any
    consumer (_validate_extraction, calculate_costs, will_synthesis, the PDF
    builders) reads it. LLM JSON drifts off-type — null where a number is
    expected, a list where a string is expected — and historically that crashed
    the entire takeoff with a TypeError and shipped nothing.

    Coercion is silent for benign cases (missing key; numeric string "1,234").
    When a HIGH-IMPACT numeric field arrives structurally wrong (list/dict) or
    as null/garbage at a non-trivial rate, that signals a degraded extraction,
    so the job is flagged for manual review rather than silently shipping a
    smaller estimate.

    Touches only wrong-typed values, so it is a no-op on well-formed data and
    idempotent. Returns the (mutated) analysis dict.
    """
    if not isinstance(analysis, dict):
        return {"floors": [], "project_info": {},
                "manual_review_required": True,
                "manual_review_reason": (
                    f"Extraction returned a {type(analysis).__name__}, not a "
                    "dict — flagged for manual review.")}

    severe = []   # structurally-wrong (list/dict) high-impact values
    soft = 0      # null/unparseable high-impact values

    def _num_field(container, key, high_impact):
        nonlocal soft
        if key not in container:
            return  # absent -> read sites already default via _num()
        raw = container[key]
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return  # already a clean number — untouched (keeps no-op property)
        if high_impact:
            if isinstance(raw, (list, dict)):
                severe.append(key)
            elif raw is None or not _is_parseable_number(raw):
                soft += 1
        container[key] = _num(raw)

    def _ensure_str(container, key):
        if key in container and not isinstance(container[key], str):
            container[key] = _coerce_str(container[key])

    # --- project_info ---
    pi = analysis.get("project_info")
    if not isinstance(pi, dict):
        pi = {}
    for k in ("total_stories", "total_units", "footprint_sqft"):
        _num_field(pi, k, high_impact=True)
    _ensure_str(pi, "building_type")
    analysis["project_info"] = pi

    # --- floors -> rooms ---
    if not isinstance(analysis.get("floors"), list):
        analysis["floors"] = []
    for floor in analysis["floors"]:
        if not isinstance(floor, dict):
            continue
        _ensure_str(floor, "floor_name")
        if not isinstance(floor.get("rooms"), list):
            floor["rooms"] = []
        for room in floor["rooms"]:
            if not isinstance(room, dict):
                continue
            for sk in _ROOM_STR:
                _ensure_str(room, sk)
            _ensure_str(room, "notes")
            if "in_scope" in room and not isinstance(room["in_scope"], bool):
                room["in_scope"] = bool(room["in_scope"])
            _num_field(room, "unit_multiplier", high_impact=False)
            dims = room.get("dimensions")
            if not isinstance(dims, dict):
                dims = {}
                room["dimensions"] = dims
            # Track which high-impact area fields arrived null/unparseable so we
            # count only the UNRECOVERABLE ones toward the degraded gate below
            # (a null we can rebuild from geometry is not a degraded extraction).
            _recoverable = ("wall_area_sqft", "floor_area_sqft",
                            "ceiling_area_sqft", "perimeter_lf")
            # null or garbage-string (but not list/dict, which are severe below)
            _null_before = {
                k for k in _recoverable
                if k in dims and not _is_parseable_number(dims[k])
                and not isinstance(dims[k], (list, dict))
            }
            # Structurally-wrong (list/dict) high-impact values are always severe.
            for k in _recoverable:
                if isinstance(dims.get(k), (list, dict)):
                    severe.append(k)
            # Coerce every dimension (incl. length/width, needed for recompute)
            # to a number, then rebuild any missing area from geometry.
            for dk in _ROOM_DIM_NUM:
                if dk in dims:
                    dims[dk] = _num(dims[dk])
            for _g in ("length_feet", "width_feet"):
                if _g in dims:
                    dims[_g] = _num(dims[_g])
            _recover_area_fields(dims)
            # Count toward the soft (degraded-extraction) gate only the
            # high-impact area fields that were null AND stayed 0 after recovery.
            for k in _null_before:
                if k in _HIGH_IMPACT_NUMERIC and _num(dims.get(k)) <= 0:
                    soft += 1
            elems = room.get("elements")
            if not isinstance(elems, dict):
                room["elements"] = {}
            else:
                for ek in list(elems.keys()):
                    _num_field(elems, ek, high_impact=False)
            mats = room.get("materials")
            if not isinstance(mats, dict):
                room["materials"] = {}
            else:
                for mk in list(mats.keys()):
                    _ensure_str(mats, mk)

    # --- aggregated_totals (all numeric) ---
    agg = analysis.get("aggregated_totals")
    if isinstance(agg, dict):
        for ak in list(agg.keys()):
            _num_field(agg, ak, high_impact=(ak in _HIGH_IMPACT_NUMERIC))
    elif agg is not None:
        analysis["aggregated_totals"] = {}

    # --- manual-review gate on high-impact corruption ---
    if severe or soft:
        total_rooms = sum(len(f.get("rooms", [])) for f in analysis["floors"]
                          if isinstance(f, dict))
        # Always review structurally-wrong values; for null/garbage only when
        # the rate is non-trivial (a stray null is benign noise).
        threshold = max(5, int(0.10 * total_rooms))
        if severe or soft >= threshold:
            analysis["manual_review_required"] = True
            reason = (f"Normalization found malformed high-impact field(s) "
                      f"(structurally-wrong={sorted(set(severe))}, "
                      f"null/garbage={soft}) — likely a degraded extraction.")
            prior = analysis.get("manual_review_reason")
            analysis["manual_review_reason"] = (
                f"{prior} | {reason}" if prior else reason)

    return analysis


# ---------------------------------------------------------------------------
# Multi-pass extraction with per-room median merge
# ---------------------------------------------------------------------------
# Claude's vision encoder is non-deterministic on complex architectural PDFs
# even at temperature=0 — the same FP file, same prompts, can return
# wildly different room counts (observed on 364 Main: 510 / 264 / 83 rooms
# across three runs of identical code).
#
# Single-pass extraction is at the mercy of that variance: any individual
# run can land far from truth, and we have no signal to know whether we
# got the "good" run or the "bad" run.
#
# Strategy: run N extraction passes per FP file, then merge by taking the
# MEDIAN of each room-level measurement. Median is robust to one outlier
# in either direction (the min OR the max gets discarded), so the merged
# answer converges toward the central tendency across passes.
#
# Reverted f004a50 originally tried multi-pass with a "keep whichever
# pass found MORE rooms" combiner — that biased toward over-extraction.
# Per-room median fixes that asymmetry. See run_analysis for the loop.

_ROOM_NAME_NORMALIZE_RE_MP = re.compile(r"[^a-z0-9]+")


def _normalize_room_name_mp(name):
    """Strip punctuation/whitespace/case for room-name matching across passes.
    'Commercial Space 1' / 'commercial-space-1' / 'COMMERCIAL SPACE 1' all
    normalize to 'commercialspace1'."""
    return _ROOM_NAME_NORMALIZE_RE_MP.sub("", str(name or "").lower())


def _median_num(values):
    """Median of a list of numeric values. Skips Nones; returns 0 if empty.

    Zero handling: a 0 from a pass usually means "field not extracted",
    so a MINORITY of zeros is ignored (median of the nonzero values —
    one pass missing a dimension shouldn't drag the merged value down).
    But when zeros are the MAJORITY, the passes agree the value is 0 and
    a single nonzero outlier must not win: the old behavior turned
    [0, 0, 800] into 800, letting a one-pass hallucination through the
    consensus merge. Majority-zero now returns 0.
    """
    import statistics as _stats
    vals = [_num(v) for v in values if v is not None]
    if not vals:
        return 0
    nonzero = [v for v in vals if v != 0]
    if len(nonzero) * 2 < len(vals):
        return 0
    try:
        return _stats.median(nonzero or vals)
    except _stats.StatisticsError:
        return 0


# Per-room numeric fields we median across passes. Everything else
# (room_id, room_name, materials, notes, bbox) takes the value from the
# first pass that contributed to the merged room.
_MEDIAN_DIM_KEYS = (
    "length_feet", "width_feet", "ceiling_height_feet",
    "floor_area_sqft", "perimeter_lf", "wall_area_sqft",
    "ceiling_area_sqft",
)
_MEDIAN_ELEM_KEYS = (
    "doors_full_paint", "doors_hm_panel", "doors_frame_only",
    "windows_total", "windows_painted_interior", "base_trim_lf",
    "stair_sections", "gyp_between_stairs_sqft", "level_5_finish_sqft",
    "concrete_floor_sqft", "painted_columns_ea", "wallcovering_sqft",
    "stained_wood_sqft", "soffit_sqft", "painted_railing_lf",
)


def _merge_passes_with_median(pass_analyses, min_pass_presence=None):
    """Merge N extraction passes by per-room median.

    Args:
        pass_analyses: list of `analysis` dicts (the per-pass output of
            analyze_and_parse, NOT the (path, analysis) tuples).
        min_pass_presence: a room must appear in >= this many passes to
            survive the merge. Default ceil(N/2) — a room that only
            appears in 1/3 passes is treated as low-confidence and dropped.

    Returns:
        A merged analysis dict suitable for downstream aggregation.
        Aggregated totals are recomputed inside _recalculate_totals so
        the caller doesn't need to do it again.

    Matching key: (floor_name, source_sheet_upper, normalized_room_name).
    Rooms sharing that key across passes are considered the same physical
    room. Their numeric dimensions and element counts are medianed; their
    multiplier is medianed too (rounded). Categorical fields (materials,
    name, notes) come from the first contributing pass.

    Unit multipliers: medianed across passes (rounded to int). This catches
    cases like a corridor extracted as ×2 in one pass and ×3 in another
    — median picks the stable answer.
    """
    if not pass_analyses:
        return None
    if len(pass_analyses) == 1:
        return pass_analyses[0]

    import copy as _copy
    import math as _math
    N = len(pass_analyses)
    if min_pass_presence is None:
        # Default: a room must appear in a MAJORITY of passes to survive.
        # That structurally discards real rooms that Claude's non-deterministic
        # vision only happened to catch in one pass — biasing room counts low.
        # NIGHTSHIFT_MERGE_UNION=1 keeps any room seen in >=1 pass (union),
        # medianing its dimensions over only the passes that contributed it,
        # so single-pass real rooms aren't dropped. Default off pending
        # corpus A/B (it can also let a one-pass hallucination through, which
        # the downstream footprint/area sanity checks are expected to catch).
        if os.environ.get("NIGHTSHIFT_MERGE_UNION", "0") == "1":
            min_pass_presence = 1
        else:
            min_pass_presence = max(2, _math.ceil(N / 2))  # majority

    # Index every room by (floor_name_norm, sheet, name_norm). The
    # floor_name is normalized the same way as room names (lowercase,
    # alphanumeric-only) so that minor LLM-driven naming variations across
    # passes — "Typical Residential Units (Floors 2&3)" vs "Typical 2BR
    # Units (Floors 2&3)" vs "Typical Residential Floors 2-3" — still
    # collide on the same key. The displayed floor_name is preserved
    # separately in floor_meta_by_name below.
    rooms_by_key = {}  # key -> list of (floor_name_display, room)
    for analysis in pass_analyses:
        for floor in analysis.get("floors", []) or []:
            fname_display = str(floor.get("floor_name") or "")
            fname_norm = _normalize_room_name_mp(fname_display)
            for room in floor.get("rooms", []) or []:
                sheet = str(room.get("source_sheet") or "").strip().upper()
                name_norm = _normalize_room_name_mp(room.get("room_name"))
                if not name_norm:
                    continue
                key = (fname_norm, sheet, name_norm)
                rooms_by_key.setdefault(key, []).append(
                    (fname_display, room))

    # Build merged rooms, keyed by floor name
    merged_rooms_per_floor = {}
    dropped_count = 0
    kept_count = 0

    for (fname_norm, sheet, name_norm), instances in rooms_by_key.items():
        if len(instances) < min_pass_presence:
            dropped_count += 1
            continue

        # Base room = first instance's room dict (preserves room_id, name, materials)
        first_fname_display, first_room = instances[0]
        merged = _copy.deepcopy(first_room)
        room_dicts = [inst[1] for inst in instances]

        # Median dimensions
        dims = merged.setdefault("dimensions", {})
        for k in _MEDIAN_DIM_KEYS:
            vals = [r.get("dimensions", {}).get(k) for r in room_dicts]
            med = _median_num(vals)
            # Round integer-ish keys to int
            if k.endswith(("_sqft", "_lf")) or k == "ceiling_height_feet":
                dims[k] = round(med, 2) if k == "ceiling_height_feet" else round(med)
            else:
                dims[k] = round(med, 1)

        # Median element counts
        elems = merged.setdefault("elements", {})
        for k in _MEDIAN_ELEM_KEYS:
            vals = [r.get("elements", {}).get(k) for r in room_dicts]
            elems[k] = round(_median_num(vals))

        # Median unit_multiplier (rounded to int, floor 1)
        mults = [_num(r.get("unit_multiplier", 1) or 1) for r in room_dicts]
        merged["unit_multiplier"] = max(1, round(_median_num(mults)))

        # Audit annotation
        merged["_median_from_passes"] = len(instances)
        merged["_total_passes"] = N

        merged_rooms_per_floor.setdefault(first_fname_display, []).append(merged)
        kept_count += 1

    # SAFETY FALLBACK — if the per-room match is too strict and the merge
    # would discard most of the rooms, the downstream aggregation drops to
    # a footprint × stories × efficiency estimate. That estimate uses the
    # MAX-of-passes footprint, which is usually the wild-overshoot pass,
    # and the result is silently catastrophic — we observed this on the
    # 2026-05-29 Ridgeview re-run that triggered this fix: 3 passes
    # produced 54 candidate rooms, none matched across >=2 passes, all
    # got dropped, and the footprint fallback produced $335,558 / 113,400
    # SF ceiling on a 60,000 SF footprint that the LLM emitted in only
    # one of the three passes.
    #
    # When the merge keeps too few rooms relative to what each individual
    # pass found, we instead pick the SINGLE PASS whose room count is the
    # median of the three (or any N) and ship that pass unmodified. That's
    # the conservative interpretation of "median": median over runs, not
    # median over fields-within-a-broken-merge.
    #
    # "Too few" = kept fewer rooms than half of the minimum-non-zero
    # per-pass count. Tunable via NIGHTSHIFT_MULTI_PASS_KEEP_RATIO
    # (default 0.5).
    per_pass_room_counts = [
        sum(len(f.get("rooms", []) or [])
            for f in (a.get("floors", []) or []))
        for a in pass_analyses
    ]
    nonzero_pass_counts = [c for c in per_pass_room_counts if c > 0]
    try:
        keep_ratio = float(
            os.environ.get("NIGHTSHIFT_MULTI_PASS_KEEP_RATIO", "0.5"))
    except (ValueError, TypeError):
        keep_ratio = 0.5
    keep_ratio = max(0.0, min(1.0, keep_ratio))

    min_required = (int(min(nonzero_pass_counts) * keep_ratio)
                    if nonzero_pass_counts else 0)
    if kept_count < min_required:
        # Selection rule (revised 2026-06-08): the old rule shipped the pass
        # whose room count was closest to the median of ALL passes. That can
        # pick a pass that DROPPED chunks and is therefore missing real scope.
        # Aliante (Wingstop) hit exactly this: chunk 3 (A1.1 floor plan + A2.0
        # RCP) failed in 2 of 3 passes, per-pass counts were [31, 12, 26], and
        # the median rule shipped the 26-room pass (chunk 3 missing) while
        # discarding the clean 31-room pass — undercounting walls ~34% and
        # ceilings to near zero.
        #
        # Revised rule: FIRST restrict to the passes with the fewest dropped
        # chunks (most complete coverage), THEN apply the median within that
        # tier. The median step is still what guards against shipping a single
        # wild-overshoot pass (the 2026-05-29 Ridgeview case this fallback was
        # built for) — but we never prefer an incomplete pass over a complete
        # one. Ties prefer the lower room count (conservative vs. overshoot).
        import statistics as _stats

        def _pass_failed_chunks(a):
            ct = a.get("_chunk_tracking") or {}
            return len(ct.get("chunks_failed") or [])

        failed_by_pass = [_pass_failed_chunks(a) for a in pass_analyses]
        fewest_failed = min(failed_by_pass)
        eligible = [i for i in range(N) if failed_by_pass[i] == fewest_failed]

        # PREFER-COMPLETE rule (NIGHTSHIFT_MERGE_PREFER_COMPLETE=1, default off):
        # within the fewest-dropped-chunks tier, ship the pass with the MOST
        # rooms rather than the median. With Stage 1a making every pass use the
        # same extraction mode, the [52,11,12] mode-divergence that produced
        # under-counts is gone, so the dominant remaining failure is
        # under-extraction, not over-extraction — prefer coverage. The
        # Ridgeview overshoot the median rule guarded against is still blocked
        # by a FOOTPRINT sanity cap: a pass whose footprint_sqft is an outlier
        # (> overshoot_factor x the median footprint across eligible passes) is
        # excluded, because that runaway footprint is exactly what fed the
        # catastrophic footprint-fallback estimate ($335K / 113,400 SF on a
        # 60,000 SF footprint emitted in only one of three passes). If every
        # eligible pass is an outlier, we fall back to the median rule below.
        prefer_complete = (
            os.environ.get("NIGHTSHIFT_MERGE_PREFER_COMPLETE", "0") == "1")
        best_idx = None
        if prefer_complete and eligible:
            try:
                overshoot = float(os.environ.get(
                    "NIGHTSHIFT_MERGE_OVERSHOOT_FACTOR", "1.5"))
            except (ValueError, TypeError):
                overshoot = 1.5
            overshoot = max(1.0, overshoot)
            elig_fps = [_num(pass_analyses[i].get("project_info", {})
                             .get("footprint_sqft")) for i in eligible]
            nz_fps = [x for x in elig_fps if x > 0]
            med_fp = _stats.median(nz_fps) if nz_fps else 0

            def _fp_sane(i):
                fp = _num(pass_analyses[i].get("project_info", {})
                          .get("footprint_sqft"))
                # 0/absent footprint can't drive the footprint fallback blowup;
                # treat as sane. Outlier-high footprint is the overshoot signal.
                return not (med_fp > 0 and fp > overshoot * med_fp)

            sane = [i for i in eligible if _fp_sane(i)] or eligible
            # Most rooms among the sane, most-complete passes; ties → first.
            best_idx = max(sane, key=lambda i: per_pass_room_counts[i])

        if best_idx is None:
            # Default median rule: closest-to-median among the most-complete
            # passes; ties → fewer rooms (conservative vs. overshoot).
            if nonzero_pass_counts:
                target = _stats.median(
                    [per_pass_room_counts[i] for i in eligible])
            else:
                target = 0
            best_idx = min(
                eligible,
                key=lambda i: (abs(per_pass_room_counts[i] - target),
                               per_pass_room_counts[i]))
        chosen = _copy.deepcopy(pass_analyses[best_idx])
        note = (
            f"[Multi-Pass Median: FALLBACK to pass #{best_idx+1}] "
            f"Per-room merge kept only {kept_count} of "
            f"{sum(per_pass_room_counts)} candidate rooms across "
            f"N={N} passes (per-pass counts: {per_pass_room_counts}, "
            f"dropped-chunks per pass: {failed_by_pass}, "
            f"min required to ship merged: {min_required}). Room-name / "
            f"floor-name / sheet variation across passes prevented matching. "
            f"Shipping the most-complete pass (fewest dropped chunks = "
            f"{fewest_failed}), median-of-tier on ties "
            f"({per_pass_room_counts[best_idx]} rooms), instead of the empty "
            f"merge that would otherwise trigger the footprint fallback."
        )
        chosen.setdefault("notes", []).append(note)
        chosen["_extracted_with_median_of_passes"] = N
        chosen["_multi_pass_median_fallback"] = True
        chosen["_multi_pass_per_pass_room_counts"] = per_pass_room_counts
        chosen["_multi_pass_per_pass_failed_chunks"] = failed_by_pass
        chosen["_multi_pass_chosen_pass_index"] = best_idx
        print(f"   ⚠️  Multi-pass merge kept {kept_count}/{sum(per_pass_room_counts)} rooms "
              f"(< {min_required} required) — falling back to pass {best_idx+1} "
              f"({per_pass_room_counts[best_idx]} rooms; per-pass counts "
              f"{per_pass_room_counts}, dropped-chunks {failed_by_pass}, "
              f"chose fewest-dropped tier)")
        # Re-aggregate the chosen pass's totals before returning so the
        # caller gets a consistent analysis.
        _recalculate_totals(chosen)
        return chosen

    # Build merged analysis: start from a deep copy of the first pass for
    # all the non-floor metadata (project_info, building_inventory, etc.).
    merged_analysis = _copy.deepcopy(pass_analyses[0])

    # Replace the floors with merged rooms. Preserve floor metadata from
    # whichever pass had each floor first.
    floor_meta_by_name = {}
    for analysis in pass_analyses:
        for floor in analysis.get("floors", []) or []:
            fname = str(floor.get("floor_name") or "")
            if fname and fname not in floor_meta_by_name:
                floor_meta_by_name[fname] = {
                    k: v for k, v in floor.items() if k != "rooms"
                }

    new_floors = []
    for fname, rooms in merged_rooms_per_floor.items():
        meta = floor_meta_by_name.get(fname, {"floor_name": fname})
        new_floors.append({**meta, "rooms": rooms})
    merged_analysis["floors"] = new_floors

    # Median the project_info aggregates that are scalars across passes
    pi = merged_analysis.setdefault("project_info", {})
    pi_keys_to_median = ("total_rooms_found", "footprint_sqft",
                          "total_floors_analyzed", "template_rooms")
    for k in pi_keys_to_median:
        vals = [a.get("project_info", {}).get(k)
                for a in pass_analyses]
        med = _median_num(vals)
        if med:
            pi[k] = round(med)

    # Audit note
    note = (f"[Multi-Pass Median] Extracted via N={N} passes. Per-room "
            f"dimensions and element counts are the median across passes "
            f"that contained the same (floor, sheet, room_name). "
            f"Kept {kept_count} room(s) appearing in >= {min_pass_presence} "
            f"passes; dropped {dropped_count} low-confidence room(s) "
            f"(appearing in fewer passes).")
    merged_analysis.setdefault("notes", []).append(note)
    merged_analysis["_extracted_with_median_of_passes"] = N

    # Recompute aggregated totals from the merged room set
    _recalculate_totals(merged_analysis)

    return merged_analysis


def _apply_rate_overrides(rate_overrides):
    """Build a modified copy of PRICING_MODEL with rate/markup overrides applied.

    rate_overrides is a dict that supports:
      1. Shorthand keys: {"wall_rate": 1.50, "door_rate": 200}
      2. Direct PRICING_MODEL keys: {"gyp_walls": 1.50, "exterior_cornice": 25.00}
      3. Per-item markup: {"markup_gyp_walls": 0.08}  (prefix "markup_" + PM key)
      4. Global markup: {"markup": 0.08}  (applies to all items)
    """
    import copy
    pm = copy.deepcopy(PRICING_MODEL)

    # Map shorthand keys → PRICING_MODEL item keys
    _rate_map = {
        "wall_rate":     "gyp_walls",
        "ceiling_rate":  "gyp_ceilings",
        "door_rate":     "doors_full_paint",
        "window_rate":   "windows",
        "trim_rate":     "base_trim",
        "stair_rate":    "stairs",
        "cmu_rate":      "cmu_walls_full",
        "dryfall_rate":  "dryfall_ceiling",
        "concrete_rate": "concrete_sealer",
        "column_rate":   "painted_columns",
        "lymewash_rate": "lymewash",
        "plaster_rate":  "plaster",
    }

    for key, val in rate_overrides.items():
        # Skip non-rate keys
        if key == "markup":
            continue
        if key.startswith("markup_"):
            continue

        # Resolve to PRICING_MODEL key
        pm_key = _rate_map.get(key, key)  # try shorthand first, else use key directly
        if pm_key in pm:
            new_rate = float(val)
            for tier in pm[pm_key]["tiers"]:
                tier["rate"] = new_rate
            # Record explicit org overrides so calculate_costs' building-type
            # rate defaults don't silently clobber a negotiated rate.
            pm[pm_key]["_rate_overridden"] = True

    # Per-item markup overrides (markup_gyp_walls, markup_exterior_cornice, etc.)
    _markup_overridden = set()
    for key, val in rate_overrides.items():
        if key.startswith("markup_"):
            pm_key = key[len("markup_"):]
            if pm_key in pm:
                pm[pm_key]["markup"] = float(val)
                pm[pm_key]["_markup_overridden"] = True
                _markup_overridden.add(pm_key)

    # Global markup override (applies to all items not already overridden above)
    if "markup" in rate_overrides:
        new_markup = float(rate_overrides["markup"])
        for item_key in pm:
            if item_key in _markup_overridden:
                continue  # per-item override wins over the global one
            pm[item_key]["markup"] = new_markup
            pm[item_key]["_markup_overridden"] = True

    return pm


def _get_tiered_rate(item_config, quantity):
    """Return the unit rate for the tier matching the given quantity.

    item_config is a dict from PRICING_MODEL, e.g.:
        {"unit": "sqft", "markup": 0.08,
         "tiers": [{"min_qty": 0, "max_qty": 3499, "rate": 1.10},
                    {"min_qty": 3500, "max_qty": None, "rate": 0.80}]}
    """
    tiers = item_config.get("tiers", [])
    # Half-open ranges: a tier matches when min_qty <= q < next tier's
    # min_qty. The old inclusive-integer bounds (max_qty 3499 / min_qty
    # 3500) left a gap for fractional quantities — 3,499.5 sqft matched
    # NO tier and fell through to the last (cheapest, volume) tier.
    sorted_tiers = sorted(tiers, key=lambda t: t.get("min_qty", 0))
    for i, tier in enumerate(sorted_tiers):
        next_min = (sorted_tiers[i + 1].get("min_qty")
                    if i + 1 < len(sorted_tiers) else None)
        if tier.get("min_qty", 0) <= quantity and (next_min is None or quantity < next_min):
            return tier["rate"]
    # Fallback: below all tiers (negative qty / malformed config) — first tier
    if sorted_tiers:
        return sorted_tiers[0]["rate"]
    return 0


def _detect_single_family_from_rooms(analysis):
    """Detect single-family residential from room names.

    Use the actual room inventory to determine if this is a single-family home.
    BUT respect the LLM building_type when it clearly indicates institutional/commercial —
    senior living suites often have master bedrooms + kitchens that mimic single-family layouts.

    Criteria: Has typical single-family rooms (master bedroom, kitchen, dining room,
    foyer/entry) AND total_units <= 2 AND NOT a multi-unit layout (no unit multipliers > 1)
    AND building_type is NOT clearly institutional/commercial.
    """
    pi = analysis.get("project_info", {})
    building_type_str = str(pi.get("building_type", "")).lower()
    total_units_raw = pi.get("total_units", 0)
    total_units = _num(total_units_raw) if isinstance(total_units_raw, (int, float)) else 0

    # If the LLM building_type clearly indicates institutional/commercial, do NOT
    # override to single-family. IL/senior suites often look like SF at room level.
    _institutional_keywords = (
        "senior", "assisted", "nursing", "memory care", "independent living",
        "facility", "institutional", "hospital", "medical", "clinic",
        "school", "dormitor", "hotel", "motel", "resort",
        "office", "retail", "warehouse", "industrial",
    )
    if any(kw in building_type_str for kw in _institutional_keywords):
        # Exception: if the project_name also contains "home" or "residence" AND
        # building_type contains "expansion" or "renovation", it might truly be a
        # large custom home near an institutional campus. But by default, trust the LLM.
        project_name = str(pi.get("project_name", "")).lower()
        if not any(kw in project_name for kw in ("home", "residence", "house")):
            return False

    # Multi-unit buildings are never single-family
    if total_units > 4:
        return False

    # Check for unit multipliers > 1 (indicates multi-family template)
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            mult = room.get("unit_multiplier", 1)
            if isinstance(mult, (int, float)) and mult > 1:
                return False

    # Collect all room names
    all_rooms = []
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            all_rooms.append(room.get("room_name", "").lower())

    # Check for institutional room types that would disqualify SF
    _institutional_rooms = ("nurse", "activity", "common area", "lobby", "reception",
                            "conference", "office suite", "resident", "unit ")
    if any(kw in r for r in all_rooms for kw in _institutional_rooms):
        return False

    all_rooms_str = " ".join(all_rooms)

    # Single-family signature: has master bedroom + kitchen + at least 2 of (dining, foyer, closet, bathroom)
    has_master = any("master" in r or "primary bed" in r for r in all_rooms)
    has_kitchen = any("kitchen" in r for r in all_rooms)
    has_dining = any("dining" in r for r in all_rooms)
    has_foyer = any(kw in all_rooms_str for kw in ("foyer", "entry", "mudroom", "vestibule"))
    has_closet = any("closet" in r for r in all_rooms)
    has_bathroom = sum(1 for r in all_rooms if "bath" in r or "powder" in r) >= 1

    supporting = sum([has_dining, has_foyer, has_closet, has_bathroom])

    if has_master and has_kitchen and supporting >= 2:
        return True

    return False


def calculate_costs(aggregated_totals, exterior=None, building_type="", project_info=None,
                    analysis=None, pricing_model_override=None):
    """Calculate costs using Rider Painting pricing model from config.py.

    If pricing_model_override is provided (a dict with the same structure as
    PRICING_MODEL), it is used instead of the global PRICING_MODEL.
    """

    if exterior is None:
        exterior = {}
    if project_info is None:
        project_info = {}

    # Building-type-aware markup and rate overrides
    bt = str(building_type).lower()
    is_single_family = any(kw in bt for kw in ("single", "detached"))
    # Room-based override: if the LLM misclassified (e.g., "senior living expansion"
    # for a single-family home), detect from the actual room inventory.
    if not is_single_family and analysis:
        if _detect_single_family_from_rooms(analysis):
            is_single_family = True
            print(f"   🏠 Room-based detection: overriding '{building_type}' → single-family")
    is_commercial = "commercial" in bt

    # Sub-classify commercial by footprint: large (retail/warehouse) vs small (office/renovation)
    # Primary: footprint > 10K SF.  Secondary: total wall area > 10K SF (catches bad footprint
    # extraction — e.g., Mazda footprint=4K but showroom alone is 4,104 SF, walls=44K).
    _footprint = _num(project_info.get('footprint_sqft', 0))
    _total_wall_area = _num(aggregated_totals.get('total_paintable_wall_sqft', 0)) + \
                       _num(aggregated_totals.get('total_cmu_wall_sqft', 0)) + \
                       _num(aggregated_totals.get('total_lymewash_wall_sqft', 0)) + \
                       _num(aggregated_totals.get('total_plaster_wall_sqft', 0))
    is_large_commercial = is_commercial and (_footprint > 10000 or _total_wall_area > 10000)

    # Footprint sanity check: if extracted room floor area on any single floor exceeds
    # the footprint, the LLM mis-estimated footprint (e.g., Mazda: showroom alone=4,104 SF
    # but footprint_sqft=4,000).  Correct to max(footprint, largest_floor_area).
    if analysis and _footprint > 0:
        _floors = analysis.get('floors', [])
        for _fl in _floors:
            _fl_area = sum(_num(r.get('dimensions', {}).get('floor_area_sqft', 0))
                          for r in _fl.get('rooms', []))
            if _fl_area > _footprint * 1.5:  # >50% over = clearly wrong
                _old_fp = _footprint
                _footprint = round(_fl_area)
                print(f"   📐 Footprint corrected: {_old_fp:,.0f} → {_footprint:,.0f} SF "
                      f"(floor '{_fl.get('floor_name', '?')}' room area = {_fl_area:,.0f} SF)")
                # Re-evaluate large commercial with corrected footprint
                if is_commercial and _footprint > 10000:
                    is_large_commercial = True

    # Detect apartment vs non-apartment residential (senior living, renovations, expansions)
    # Apartment buildings have total_units >= 4 — they get volume rates ($0.80/SF) due to
    # spray-application efficiency (identical units, repetitive layout).
    # Non-apartment residential (senior living, care facilities, dormitories, expansions)
    # have higher labor density and don't get the same volume discount.
    _total_units_raw = project_info.get('total_units', 0)
    _total_units = _num(_total_units_raw) if isinstance(_total_units_raw, (int, float)) else 0
    _is_residential_type = any(kw in bt for kw in ("residential", "mixed", "multi", "apartment"))
    is_apartment = _is_residential_type and not is_commercial and _total_units >= 4
    is_non_apartment_residential = _is_residential_type and not is_commercial and not is_apartment
    _is_institutional = any(kw in bt for kw in
        ("senior", "assisted", "living", "facility", "institutional",
         "hospital", "medical", "nursing"))

    # Markup: single-family 8%, large commercial 5%, small commercial 8%, multi-family 6% (default)
    if is_single_family:
        markup_override = 0.08
    elif is_commercial:
        markup_override = 0.05 if is_large_commercial else 0.08
    else:
        markup_override = None  # None = use per-item default (6%)

    def _get_markup(item_key):
        """Return markup for an item.

        Precedence: explicit org override (set via _apply_rate_overrides,
        marked _markup_overridden) > building-type default > config default.
        The building-type default used to win unconditionally, silently
        discarding negotiated org markups on most jobs.
        """
        if pm.get(item_key, {}).get("_markup_overridden"):
            return pm[item_key]['markup']
        if markup_override is not None:
            return markup_override
        return pm[item_key]['markup']

    def _rate_locked(item_key):
        """True when the org explicitly overrode this item's rate — the
        building-type hardcoded defaults below must not clobber it."""
        return bool(pm.get(item_key, {}).get("_rate_overridden"))

    def _line(label, qty, unit_cost, markup_pct):
        cost = qty * unit_cost
        markup = cost * markup_pct
        return {"item": label, "qty": qty,
                "cost": round(cost, 2), "markup": round(markup, 2),
                "total": round(cost + markup, 2)}

    # Interior surfaces
    wall_sqft = _num(aggregated_totals.get('total_paintable_wall_sqft', 0))
    ceil_sqft = _num(aggregated_totals.get('total_paintable_ceiling_sqft', 0))
    cmu_wall_sqft = _num(aggregated_totals.get('total_cmu_wall_sqft', 0))
    lymewash_sqft = _num(aggregated_totals.get('total_lymewash_wall_sqft', 0))
    plaster_sqft = _num(aggregated_totals.get('total_plaster_wall_sqft', 0))
    dryfall_sqft = _num(aggregated_totals.get('total_dryfall_ceiling_sqft', 0))
    trim_lf = _num(aggregated_totals.get('total_base_trim_lf', 0))

    # Doors — new split types with backward compat
    doors_full = _num(aggregated_totals.get('total_doors_full_paint',
                      aggregated_totals.get('total_doors', 0)))
    doors_hm = _num(aggregated_totals.get('total_doors_hm_panel', 0))
    doors_frame = _num(aggregated_totals.get('total_doors_frame_only', 0))

    # Windows — painted interior only, with backward compat
    windows = _num(aggregated_totals.get('total_windows_painted_interior',
                   aggregated_totals.get('total_windows', 0)))

    # Stairs
    stair_sections = _num(aggregated_totals.get('total_stair_sections', 0))
    gyp_stairs = _num(aggregated_totals.get('total_gyp_between_stairs_sqft', 0))

    # Specialty finishes — Level 5 skim coat priced per SF (PCA Section 6D)
    level_5_sqft = _num(aggregated_totals.get('total_level_5_finish_sqft', 0))

    # Concrete sealer (garages, basements)
    concrete_sqft = _num(aggregated_totals.get('total_concrete_floor_sqft', 0))

    # Painted columns (commercial)
    columns_ea = _num(aggregated_totals.get('total_painted_columns_ea', 0))

    # Wallcovering install (labor only)
    wallcovering_sqft = _num(aggregated_totals.get('total_wallcovering_sqft', 0))

    # Stained wood / clear-coat panels
    stained_wood_sqft = _num(aggregated_totals.get('total_stained_wood_sqft', 0))

    # Interior soffits (GYP drywall drops)
    soffit_sqft = _num(aggregated_totals.get('total_soffit_sqft', 0))

    # Exterior
    cornice_lf = _num(exterior.get('cornice_lf', 0))
    window_trim_lf = _num(exterior.get('window_trim_lf', 0))
    ext_paint_sqft = _num(exterior.get('exterior_paint_sqft', 0))
    hardie_sqft = _num(exterior.get('hardie_siding_sqft', 0))
    azek_lf = _num(exterior.get('azek_trim_lf', 0))
    corner_lf = _num(exterior.get('corner_board_lf', 0))
    steel_lintel_lf_ext = _num(exterior.get('steel_lintel_lf', 0))

    # Painted railings — interior handrails + exterior porch/balcony/deck rails.
    # Sum both buckets into a single line item; stained-wood rails are priced
    # separately under exterior_stain_railing.
    interior_railing_lf = _num(aggregated_totals.get('total_painted_railing_lf', 0))
    exterior_railing_lf = _num(exterior.get('railing_lf', 0))
    total_painted_railing_lf = interior_railing_lf + exterior_railing_lf
    # When material-specific items present, subtract from generic exterior_paint_sqft
    if hardie_sqft > 0:
        ext_paint_sqft = max(0, ext_paint_sqft - hardie_sqft)

    # --- Data-driven exterior scope adjustments ---
    # Rather than blanket suppress by building type, check what the extraction
    # actually found. New construction siding doesn't need painting; only specialty
    # coatings (Hardie, Azek) that were explicitly specified get priced.
    _is_res_ext = _is_residential_type and not is_commercial

    # Exterior window trim: when Azek trim is present, window casings are already
    # covered by the Azek line item. Suppress to avoid double-counting.
    if azek_lf > 0 and window_trim_lf > 0:
        window_trim_lf = 0

    # --- Hardie/Azek/Lintel: price by default when extraction found them ---
    # If Claude measured Hardie sqft, Azek LF, corner boards, or steel lintels
    # from the elevation drawings, the painter is almost certainly bidding to
    # paint them. The Fishkill PDF round-tripped extracted Hardie + Azek + lintel
    # quantities that were suppressed because the notes didn't contain the
    # specific phrase "field paint" — a $35k swing on a single bid. Invert the
    # default: assume painting is in scope unless notes EXPLICITLY say otherwise.
    _ext_siding_type = str(exterior.get('exterior_siding_type', '')).lower()
    _ext_notes = str(exterior.get('notes', '')).lower()
    # Explicit factory-finish / non-paint signals — these still suppress.
    _siding_factory_finished = any(kw in _ext_notes for kw in (
        'cork siding', 'vinyl siding', 'metal siding', 'metal roofing',
        'aluminum siding', 'composite siding', 'factory finish',
        'pre-finish', 'prefinish', 'not require painting',
        'do not require paint', 'does not require paint',
        'no painting required', 'no paint required'))
    # Scope-notes override (user can force one way or the other)
    _scope_ext = str(project_info.get('_scope_notes', '')).lower()
    _scope_says_no_paint = any(kw in _scope_ext for kw in (
        'no exterior paint', 'no siding paint', 'exterior excluded',
        'siding excluded', 'do not paint siding'))
    _suppress_siding = _siding_factory_finished or _scope_says_no_paint

    if _suppress_siding:
        if hardie_sqft > 0:
            hardie_sqft = 0
        if azek_lf > 0:
            azek_lf = 0
        if corner_lf > 0:
            corner_lf = 0
        if steel_lintel_lf_ext > 0:
            steel_lintel_lf_ext = 0

    # --- Cornice: keep if extraction found it, suppress only if zero ---
    # Cornice/brackets are almost always field-painted, even on new construction.
    # Don't suppress — let the extraction decide.
    # FALLBACK: For buildings with 2+ stories and 0 cornice, estimate from building perimeter.
    # This handles non-deterministic chunk processing where elevation data is sometimes
    # missed between runs.
    _ext_footprint = _num(project_info.get('footprint_sqft', 0))
    _ext_stories = _num(project_info.get('total_stories', 0))
    if (not HARD_NUMBERS_ONLY) and cornice_lf == 0 and _ext_stories >= 2:
        if _ext_footprint > 0:
            # Estimate perimeter from footprint (assume ~1.5:1 aspect ratio)
            _ext_long = math.sqrt(_ext_footprint * 1.5)
            _ext_short = _ext_footprint / _ext_long if _ext_long > 0 else 0
            _ext_perimeter = 2 * (_ext_long + _ext_short)
            cornice_lf = round(_ext_perimeter)
            # For institutional: also estimate window trim and soffits if missing
            if _is_institutional and window_trim_lf == 0:
                # ~40 windows typical per floor for senior living, ~7 LF trim per window
                window_trim_lf = round(_ext_perimeter * _ext_stories * 0.5)
            if _is_institutional and soffit_sqft == 0:
                # Soffits typically 2ft wide along perimeter
                soffit_sqft = round(_ext_perimeter * 2)
            print(f"   📐 Exterior fallback: cornice {cornice_lf} LF, "
                  f"window trim {window_trim_lf} LF, soffits {soffit_sqft} SF "
                  f"(from {_ext_footprint:,.0f} SF footprint perimeter)")
        elif wall_sqft > 0:
            _est_ceiling = 9.0
            _est_perimeter = wall_sqft / (_est_ceiling * max(_ext_stories, 2))
            cornice_lf = round(_est_perimeter)

    # --- Base trim: data-driven, not building-type-driven ---
    # Only suppress base trim if the extraction found ZERO trim across all rooms.
    # Some apartment buildings have base trim (364 Main: $9,630); others include
    # it in the spray rate (Fishkill). Let the extraction data decide.
    # trim_lf is already set from aggregated_totals — leave it as-is.

    # --- CMU walls: only suppress if no CMU rooms found in extraction ---
    # Some mixed-use buildings (364 Main) have CMU stair towers and elevator shafts.
    # Only zero out if the extraction likely misclassified partitions.
    # Check: if CMU SF > 30% of total wall SF, it's probably misclassified.
    if _is_res_ext and cmu_wall_sqft > 0:
        _cmu_ratio = cmu_wall_sqft / max(1, wall_sqft)
        if _cmu_ratio > 0.30:
            # More than 30% CMU is unrealistic for residential — likely misclassified
            cmu_wall_sqft = 0

    # --- Exterior envelope validation ---
    # Cap siding/paint area against building envelope to prevent over-estimation.
    # Only applies when siding materials are being priced.
    _footprint_ext = _num(project_info.get('footprint_sqft', 0))
    _stories_ext = _num(project_info.get('total_stories', 0))
    if _footprint_ext > 0 and _stories_ext >= 2 and (hardie_sqft > 0 or ext_paint_sqft > 0):
        _long_side = math.sqrt(_footprint_ext * 2)
        _short_side = _footprint_ext / _long_side if _long_side > 0 else 0
        _perimeter = 2 * (_long_side + _short_side)
        _avg_ht = 10  # residential default
        _env_factor = 0.55 if _is_res_ext else 0.70
        _envelope = _perimeter * _avg_ht * _stories_ext * _env_factor
        if hardie_sqft > _envelope:
            hardie_sqft = round(_envelope)
        _remaining_envelope = max(0, _envelope - hardie_sqft)
        if ext_paint_sqft > _remaining_envelope:
            ext_paint_sqft = round(_remaining_envelope)
        _trim_factor = 0.7 if _is_res_ext else 1.2
        _max_trim_lf = round(_perimeter * _stories_ext * _trim_factor)
        if azek_lf > _max_trim_lf:
            azek_lf = _max_trim_lf
        _lintel_factor = 0.20 if _is_res_ext else 0.50
        _max_lintel_lf = round(_perimeter * _lintel_factor)
        if steel_lintel_lf_ext > _max_lintel_lf:
            steel_lintel_lf_ext = _max_lintel_lf

    lift_needed = 1 if exterior.get('lift_required', False) else 0
    int_lift_needed = 1 if exterior.get('interior_lift_required', False) else 0

    # Single-story buildings never need exterior lifts (ladders suffice)
    _total_stories_lift = _num(project_info.get('total_stories', 1))
    if _total_stories_lift <= 1 and lift_needed:
        lift_needed = 0

    # If any exterior scope exists, require exterior lift (unless single-family ≤3 stories)
    # Cornice work on 2+ story buildings requires a lift — ladders only suffice at residential scale.
    has_any_ext = (
        ext_paint_sqft > 0 or hardie_sqft > 0 or azek_lf > 0 or cornice_lf > 0
    )
    if has_any_ext and lift_needed == 0:
        # Single-family homes ≤3 stories use ladders, not lifts
        _sf_stories_ext = _num(project_info.get('total_stories', 0))
        if not (is_single_family and _sf_stories_ext <= 3) and _sf_stories_ext >= 2:
            lift_needed = 1

    pm = pricing_model_override if pricing_model_override else PRICING_MODEL

    # Resolve tiered rates based on actual project quantities
    wall_rate   = _get_tiered_rate(pm['gyp_walls'], wall_sqft)
    ceil_rate   = _get_tiered_rate(pm['gyp_ceilings'], ceil_sqft)
    cmu_rate    = _get_tiered_rate(pm['cmu_walls_full'], cmu_wall_sqft)
    lymewash_rate = _get_tiered_rate(pm['lymewash'], lymewash_sqft) if 'lymewash' in pm else 4.50
    plaster_rate  = _get_tiered_rate(pm['plaster'], plaster_sqft) if 'plaster' in pm else 7.50
    dryfall_rate = _get_tiered_rate(pm['dryfall_ceiling'], dryfall_sqft)
    trim_rate   = _get_tiered_rate(pm['base_trim'], trim_lf)
    door_fp_rate = _get_tiered_rate(pm['doors_full_paint'], doors_full)
    door_hm_rate = _get_tiered_rate(pm['doors_hm_panel'], doors_hm)
    door_frame_rate = _get_tiered_rate(pm['doors_frame_only'], doors_frame)
    win_rate    = _get_tiered_rate(pm['windows'], windows)
    stair_rate  = _get_tiered_rate(pm['stairs'], stair_sections)
    gyps_rate   = _get_tiered_rate(pm['gyp_between_stairs'], gyp_stairs)
    l5_rate     = _get_tiered_rate(pm['level_5_finish'], level_5_sqft)
    conc_rate   = _get_tiered_rate(pm['concrete_sealer'], concrete_sqft)
    col_rate    = _get_tiered_rate(pm['painted_columns'], columns_ea)
    # Wallcovering rate & label:
    #   - bathroom heuristic  → prep rate ($0.50/SF)
    #   - scope = remove/strip → removal rate (NOT $9 install)
    #   - otherwise            → full install ($9/SF)
    _wc_source = project_info.get('_wallcovering_source', '')
    _wc_scope = str(project_info.get('_scope_notes', '')).lower()
    _wc_is_removal = (
        any(v in _wc_scope for v in ('remov', 'strip', 'tear off', 'tear-off', 'demo'))
        and any(k in _wc_scope for k in ('wallpaper', 'wall paper', 'wallcovering', 'wall covering'))
    )
    if _wc_source == 'bathroom_heuristic' and 'wallcovering_prep' in pm:
        wc_rate = _get_tiered_rate(pm['wallcovering_prep'], wallcovering_sqft)
        wc_markup_key = 'wallcovering_prep'
        wc_label = "Wallcovering Prep"
    elif _wc_is_removal and 'wallpaper_removal' in pm:
        wc_rate = _get_tiered_rate(pm['wallpaper_removal'], wallcovering_sqft)
        wc_markup_key = 'wallpaper_removal'
        wc_label = "Wallpaper Removal (Labor)"
    else:
        wc_rate = _get_tiered_rate(pm['wallcovering_install'], wallcovering_sqft) if 'wallcovering_install' in pm else 9.00
        wc_markup_key = 'wallcovering_install'
        wc_label = "Wallcovering Install (Labor)"
    sw_rate     = _get_tiered_rate(pm['stained_wood'], stained_wood_sqft) if 'stained_wood' in pm else 6.00
    soffit_rate = _get_tiered_rate(pm['interior_soffit'], soffit_sqft) if 'interior_soffit' in pm else 0.85
    corn_rate   = _get_tiered_rate(pm['exterior_cornice'], cornice_lf)
    wt_rate     = _get_tiered_rate(pm['exterior_window_trim'], window_trim_lf)
    ext_paint_rate = _get_tiered_rate(pm['exterior_painting'], ext_paint_sqft) if 'exterior_painting' in pm else 1.80
    hardie_rate = _get_tiered_rate(pm['exterior_hardie_siding'], hardie_sqft) if 'exterior_hardie_siding' in pm else 4.85
    azek_rate   = _get_tiered_rate(pm['exterior_azek_trim'], azek_lf) if 'exterior_azek_trim' in pm else 9.00
    corner_rate = _get_tiered_rate(pm['exterior_corner_board'], corner_lf) if 'exterior_corner_board' in pm else 9.00
    lintel_rate = _get_tiered_rate(pm['exterior_steel_lintel'], steel_lintel_lf_ext) if 'exterior_steel_lintel' in pm else 32.00
    # Lift rate scales with building height — a 12-story job needs a different
    # lift class and longer rental than a 3-story. Pass stories (not the binary
    # lift_needed flag) into the tiered-rate lookup. Rate is zeroed when no lift.
    _lift_stories_qty = _num(project_info.get('total_stories', 1)) if lift_needed else 0
    lift_rate   = _get_tiered_rate(pm['exterior_lift_rental'], _lift_stories_qty)
    int_lift_rate = _get_tiered_rate(pm['interior_lift_rental'], int_lift_needed)

    # Single-family rate overrides: force small-project rates regardless of quantity
    # (A single-family home with 7,000+ sqft walls is still a single-family job)
    if is_single_family:
        if not _rate_locked('gyp_walls'):
            wall_rate = 1.25   # Rider single-family rate
        if not _rate_locked('gyp_ceilings'):
            ceil_rate = 1.25   # Rider single-family rate
        if not _rate_locked('base_trim'):
            trim_rate = 3.25   # Rider single-family rate
        if not _rate_locked('doors_full_paint'):
            door_fp_rate = 225.00  # Rider single-family rate
        if not _rate_locked('windows'):
            win_rate = 120.00  # Rider single-family: pre-primed trim only

    # Commercial rate overrides: split by building size
    if is_commercial:
        # Base trim: let extraction decide per-room; don't blanket zero
        # (Some commercial buildings have rubber/wood base, others don't)

        if is_large_commercial:
            # Large retail/warehouse rates (calibrated from Camping World, Kingston NY)
            if not _rate_locked('gyp_walls'):
                wall_rate = 0.85   # Large open spaces, lower labor density
            if not _rate_locked('gyp_ceilings'):
                ceil_rate = 0.85
            if not _rate_locked('doors_full_paint'):
                door_fp_rate = 155.00  # Commercial HM door+frame rate (Rider Mazda)
            if not _rate_locked('doors_hm_panel'):
                door_hm_rate = 110.00  # HM panel only stays at config rate

            # Wall area cap: large open commercial spaces have non-paintable perimeter
            # (glass storefronts, metal panels, overhead doors).
            # Camping World: 11,251 SF walls on 14,155 SF footprint = 0.80 ratio.
            # Using 1.25× to allow wall over-count to compensate for missing items
            # (exposed ductwork, metal walls, interior lift not yet extracted).
            total_stories = _num(project_info.get('total_stories', 1))
            if _footprint > 0 and total_stories <= 2:
                max_wall_sqft = round(_footprint * total_stories * 1.25)
                if wall_sqft > max_wall_sqft:
                    wall_sqft = max_wall_sqft
        else:
            # Small commercial / renovation rates (from config.py SMALL_COMMERCIAL_RATES)
            if not _rate_locked('gyp_walls'):
                wall_rate = SMALL_COMMERCIAL_RATES["wall_rate"]
            if not _rate_locked('gyp_ceilings'):
                ceil_rate = SMALL_COMMERCIAL_RATES["ceil_rate"]
            if not _rate_locked('doors_full_paint'):
                door_fp_rate = SMALL_COMMERCIAL_RATES["door_fp_rate"]
            if not _rate_locked('doors_hm_panel'):
                door_hm_rate = SMALL_COMMERCIAL_RATES["door_hm_rate"]

    # Non-apartment residential rate overrides (senior living, care facilities, expansions)
    # These buildings lack the spray-application efficiency of repetitive apartment units.
    # Higher labor density → rates between apartment ($0.80/SF) and small commercial ($1.40/SF).
    # Windows are typically factory-finished (vinyl/aluminum) → trim paint only at $120/EA.
    # Calibrated from Edgehill IL Expansion vs Rider Estimate #3241: Rider effective rate ~$1.27/SF.
    if is_non_apartment_residential:
        if not _rate_locked('gyp_walls'):
            wall_rate = 1.05   # Higher labor density than apartments, spray efficiency is lower
        if not _rate_locked('gyp_ceilings'):
            ceil_rate = 1.05
        if not _rate_locked('windows'):
            win_rate = 120.00  # Factory-finished windows: trim paint only (not full interior paint)

    # --- PCA Section 5D: Multi-story productivity adjustment ---
    # Productivity diminishes 1-2% per floor above 4th due to material handling,
    # elevator waits, and tool retrieval. Applied to area-based rates only.
    _total_stories_pca = _num(project_info.get('total_stories', 1))
    _pca_start = PCA_CONSTANTS["height_productivity_start_floor"]
    if _total_stories_pca > _pca_start:
        _floors_above = _total_stories_pca - _pca_start
        _height_factor = min(
            _floors_above * PCA_CONSTANTS["height_productivity_loss_per_floor"],
            PCA_CONSTANTS["height_productivity_max_loss"]
        )
        wall_rate *= (1 + _height_factor)
        ceil_rate *= (1 + _height_factor)
        cmu_rate *= (1 + _height_factor)
        dryfall_rate *= (1 + _height_factor)
        print(f"   PCA height adj: {_total_stories_pca} stories, +{_height_factor:.1%} to area rates")

    # --- Multi-family wall/ceiling area cap ---
    # LLM extraction is non-deterministic across tile batches. Batch template dedup
    # can fail, producing duplicate floors (21 multiplied units vs 12 actual).
    # Use multiple independent caps and take the tightest:
    # 1. Footprint-based: footprint × stories × 3.0 (perimeter + partitions)
    # 2. Unit-based: units × 3,100 SF/unit (calibrated: Fishkill 43K/12 = 3,584,
    #    with margin for commercial floor: + footprint × 0.5 per extra story)
    # Ceilings: units × 1,100 SF/unit (calibrated: Fishkill 13,451/12 = 1,121)
    if is_apartment:
        _stories_cap = max(1, _num(project_info.get('total_stories', 1)))
        _bi_units = _num(project_info.get('_building_inventory_units', 0))
        _cap_units = max(_total_units, _bi_units) if _bi_units > 0 else _total_units
        _wall_caps = []
        _ceil_caps = []
        if _footprint > 0:
            _wall_caps.append(round(_footprint * _stories_cap * 3.0))
            _ceil_caps.append(round(_footprint * _stories_cap * 1.0))
        if _cap_units >= 4:
            # 3,000 SF walls per unit + 50% of footprint for commercial/basement floors
            # Calibrated: Fishkill manual 43,003 walls / 12 units = 3,584/unit;
            # using 3,000 as cap allows for unit count over-estimation by building inventory.
            _extra_floors = max(0, _stories_cap - 2)  # floors beyond 2 residential
            _unit_wall_cap = round(_cap_units * 3000 + _extra_floors * _footprint * 0.5) if _footprint > 0 else round(_cap_units * 3300)
            _wall_caps.append(_unit_wall_cap)
            _ceil_caps.append(round(_cap_units * 1100))
        if _wall_caps:
            _max_wall_sqft = min(_wall_caps)
            if wall_sqft > _max_wall_sqft:
                wall_sqft = _max_wall_sqft
        if _ceil_caps:
            _max_ceil_sqft = min(_ceil_caps)
            if ceil_sqft > _max_ceil_sqft:
                ceil_sqft = _max_ceil_sqft

    # --- Residential corner board suppression ---
    # Residential manuals don't price corner boards separately — included in siding scope.
    if _is_res_ext and corner_lf > 0:
        corner_lf = 0

    # Lift rental consolidation: when both interior and exterior lift are needed,
    # charge only the exterior lift ($4,000) which covers both — don't double-charge.
    if lift_needed and int_lift_needed:
        int_lift_needed = 0  # Exterior lift covers interior work too

    # --- Exterior stain items (wood shingles, trim bands, railings) ---
    # These are distinct from paint — Rider prices stain separately at different rates.
    # Detected from exterior notes mentioning "stain", "wood shingle", "wood railing".
    stain_siding_sqft = _num(exterior.get('stain_siding_sqft', 0))
    stain_trim_lf = _num(exterior.get('stain_trim_lf', 0))
    stain_railing_lf = _num(exterior.get('stain_railing_lf', 0))
    # Also detect from exterior notes if stain items were mentioned but not quantified
    if ((not HARD_NUMBERS_ONLY) and stain_siding_sqft == 0 and stain_trim_lf == 0 and
            any(kw in _ext_notes for kw in ('stain', 'wood shingle', 'cedar shingle',
                                             'wood siding', 'cedar siding'))):
        # Try to estimate stain siding from building envelope
        _stain_footprint = _num(project_info.get('footprint_sqft', 0))
        _stain_stories = _num(project_info.get('total_stories', 0))
        if _stain_footprint > 0 and _stain_stories >= 2:
            _stain_long = math.sqrt(_stain_footprint * 1.5)
            _stain_short = _stain_footprint / _stain_long if _stain_long > 0 else 0
            _stain_perimeter = 2 * (_stain_long + _stain_short)
            stain_siding_sqft = round(_stain_perimeter * 9.0 * _stain_stories * 0.5)  # 50% of facade
            stain_trim_lf = round(_stain_perimeter * _stain_stories)

    stain_sid_rate = _get_tiered_rate(pm['exterior_stain_siding'], stain_siding_sqft) if 'exterior_stain_siding' in pm else 1.85
    stain_trim_rate = _get_tiered_rate(pm['exterior_stain_trim'], stain_trim_lf) if 'exterior_stain_trim' in pm else 2.50
    stain_rail_rate = _get_tiered_rate(pm['exterior_stain_railing'], stain_railing_lf) if 'exterior_stain_railing' in pm else 32.00
    painted_rail_rate = _get_tiered_rate(pm['painted_railing'], total_painted_railing_lf) if 'painted_railing' in pm else 18.00

    # --- Footprint-based interior pricing fallback ---
    # When room-by-room extraction is severely incomplete, use footprint × all-inclusive
    # rate as the interior price. This catches cases where the LLM only extracted a
    # fraction of rooms (common with large DD-scale PDFs).
    footprint_sqft = _num(project_info.get('footprint_sqft', 0))

    # --- Footprint reconciliation against summed room floor area ---
    # The LLM-extracted footprint sometimes lags the merged room data —
    # it's set once during early extraction and not updated when rooms
    # accumulate across pages/files. If the sum of in-scope room
    # floor_area_sqft dwarfs the reported footprint, prefer the summed
    # value (per-floor average × stories) so downstream consumers — the
    # Will payload, validation thresholds, and footprint-pricing
    # fallback — see a realistic building size.
    if analysis:
        _summed_floor_area = sum(
            _num((r.get("dimensions") or {}).get("floor_area_sqft", 0))
            for f in analysis.get("floors", []) or []
            for r in f.get("rooms", []) or []
            if r.get("in_scope", True)
        )
        if footprint_sqft > 0 and _summed_floor_area > footprint_sqft * 2:
            _recon_stories = max(_num(project_info.get('total_stories', 0)),
                                 len(analysis.get("floors", []) or []), 1)
            _old_fp = footprint_sqft
            footprint_sqft = round(_summed_floor_area / _recon_stories)
            project_info['footprint_sqft'] = footprint_sqft
            print(f"   📐 Footprint reconciled: {_old_fp:,.0f} → "
                  f"{footprint_sqft:,.0f} SF (summed in-scope room area "
                  f"{_summed_floor_area:,.0f} SF / {_recon_stories} stories)")

    # --- Footprint cross-check for institutional buildings ---
    # The LLM often underestimates footprint for large institutional projects.
    # Cross-check against extracted wall area: footprint should be ≥ wall_sqft / (stories × 3.3).
    # Wall-to-floor ratio of ~3.3× is standard for senior living per Rider.
    if _is_institutional and footprint_sqft > 0:
        _xcheck_stories = max(_num(project_info.get('total_stories', 0)), 1)
        _wall_implied_gba = wall_sqft / 3.3 * _xcheck_stories if wall_sqft > 0 else 0
        if _wall_implied_gba > footprint_sqft * 1.3:
            _old_fp = footprint_sqft
            footprint_sqft = round(_wall_implied_gba)
            print(f"   📐 Footprint cross-check: boosted {_old_fp:,.0f} → {footprint_sqft:,.0f} SF "
                  f"(wall area {wall_sqft:,.0f} SF implies larger building)")

    # --- Extraction quality detection ---
    # Check for signals that room extraction is severely incomplete:
    # 1. no_floor_plans_found flag set
    # 2. All rooms from same source sheet (especially demo sheets AD*)
    # 3. Very few rooms for institutional building type
    _extraction_quality = "normal"
    if analysis:
        _no_plans = _model_flagged_no_plans(analysis)
        _all_floors = analysis.get("floors", [])
        _all_rooms_list = [r for f in _all_floors for r in f.get("rooms", [])]
        _source_sheets = set(r.get("source_sheet", "") for r in _all_rooms_list)
        _all_from_demo = all(s.startswith("AD") for s in _source_sheets if s)
        _room_count = len(_all_rooms_list)

        if _no_plans:
            _extraction_quality = "poor"
            print(f"   ⚠️  Extraction quality: POOR — no-floor-plans flag set by model")
        elif _all_from_demo and _room_count > 0:
            _extraction_quality = "poor"
            print(f"   ⚠️  Extraction quality: POOR — all {_room_count} rooms from demolition sheets {_source_sheets}")
        elif len(_source_sheets) == 1 and _room_count > 5:
            _extraction_quality = "suspect"
            print(f"   ⚠️  Extraction quality: SUSPECT — all {_room_count} rooms from single sheet {_source_sheets}")

    # --- Estimate footprint when not provided ---
    if footprint_sqft == 0 and analysis:
        _floors = analysis.get("floors", [])
        _total_stories = _num(project_info.get('total_stories', 0))
        if _total_stories < 1:
            _total_stories = max(len(_floors), 1)

        # Sum all ceiling areas across all floors
        _total_ceil = sum(
            _num(r.get("dimensions", {}).get("ceiling_area_sqft", 0))
            for f in _floors for r in f.get("rooms", [])
        )

        if _total_ceil > 0:
            if _extraction_quality == "poor":
                # Extraction is unreliable — use aggressive estimation.
                # For institutional buildings with poor extraction, rooms represent
                # a tiny fraction of actual floor area. Use building_type heuristics.
                if _is_institutional:
                    # Institutional: extracted rooms likely <10% of actual area.
                    # footprint_sqft here = TOTAL gross building area for pricing
                    # (not per-floor). Rider prices: GBA × $3.80/SF.
                    # Typical senior living addition: 7,000-12,000 SF per floor.
                    # Use 8,500 SF/floor as middle estimate (validated against Rider's
                    # Edgehill bid: $105K / $3.80 = 27,632 SF / 3 floors = 9,211 SF/floor).
                    footprint_sqft = round(max(_total_ceil * 5, 8500 * _total_stories))
                    print(f"   📐 Institutional GBA estimate (poor extraction): "
                          f"{footprint_sqft:,.0f} SF total "
                          f"(~{footprint_sqft/_total_stories:,.0f} SF/floor × {_total_stories} stories, "
                          f"extraction captured only {_total_ceil:,.0f} SF ceiling area)")
                else:
                    # Non-institutional but poor extraction: 3x total room area as estimate
                    footprint_sqft = round(_total_ceil * 3)
                    print(f"   📐 Estimated GBA (poor extraction): {footprint_sqft:,.0f} SF "
                          f"(3× total room area)")
            else:
                # Normal extraction: rooms typically cover 50-65% of floor plate
                _avg_floor_area = _total_ceil / _total_stories
                _coverage = 0.50 if any(kw in building_type for kw in
                                         ("senior", "assisted", "living", "facility"))  \
                            else 0.65
                _est_footprint = round(_avg_floor_area / _coverage)
                if _est_footprint > _avg_floor_area * 1.2:
                    footprint_sqft = _est_footprint
                    print(f"   📐 Estimated footprint: {footprint_sqft:,.0f} SF "
                          f"(from {_total_ceil:,.0f} SF ceiling / {_total_stories} stories / {_coverage:.0%} coverage)")

    _use_footprint_pricing = False
    _footprint_interior_total = 0

    # HARD_NUMBERS_ONLY: never substitute a footprint × rate estimate for the
    # measured per-room line items. Footprint pricing discards extracted scope
    # in favor of a building-size heuristic — exactly what the policy forbids.
    if footprint_sqft > 0 and not is_commercial and not HARD_NUMBERS_ONLY:
        if is_single_family:
            _fp_rate = 1.25
        else:
            _fp_rate = _get_tiered_rate(pm['footprint_interior'], footprint_sqft) if 'footprint_interior' in pm else 3.80
        _footprint_interior_total = footprint_sqft * _fp_rate

        _expected_wall = footprint_sqft * 1.2
        _trigger_threshold = 0.40

        # For poor-quality extraction, always trigger footprint pricing
        if _extraction_quality == "poor":
            _trigger_threshold = 1.0  # Always trigger
            _use_footprint_pricing = True
        elif wall_sqft < _expected_wall * _trigger_threshold:
            _use_footprint_pricing = True

        # For institutional buildings: ALWAYS use the higher of room-by-room vs footprint.
        # Institutional PDFs (senior living, hospitals, etc.) are massive and the LLM
        # routinely captures only a fraction of units/rooms. Room-by-room may look
        # "complete enough" (>40% wall area) but still miss most units.
        # Use footprint as a FLOOR — never let room-by-room undercount.
        if _is_institutional and not _use_footprint_pricing:
            # Calculate what room-by-room interior total would be
            # (rough estimate: walls + ceilings + trim + doors + misc)
            _room_interior_est = (
                wall_sqft * wall_rate +
                ceil_sqft * ceil_rate +
                lymewash_sqft * lymewash_rate +
                plaster_sqft * plaster_rate +
                trim_lf * trim_rate +
                doors_full * door_fp_rate +
                doors_hm * door_hm_rate
            )
            if _footprint_interior_total > _room_interior_est * 1.15:
                # Footprint pricing is >15% higher — extraction likely missed rooms
                _use_footprint_pricing = True
                print(f"   📐 Institutional floor check: footprint ${_footprint_interior_total:,.0f} > "
                      f"room-by-room ${_room_interior_est:,.0f} — using footprint as floor")

        if _use_footprint_pricing:
            print(f"   📐 Footprint pricing: {footprint_sqft:,.0f} SF × ${_fp_rate:.2f} = ${_footprint_interior_total:,.0f} "
                  f"(room extraction: {wall_sqft:,.0f} wall SF, quality: {_extraction_quality})")

    line_items = [
        _line(f"Gyp. Walls - {wall_sqft:,.0f} sqft @ ${wall_rate:.2f}", wall_sqft,
              wall_rate, _get_markup('gyp_walls')),
        _line(f"Gyp. Ceilings - {ceil_sqft:,.0f} sqft @ ${ceil_rate:.2f}", ceil_sqft,
              ceil_rate, _get_markup('gyp_ceilings')),
        _line(f"CMU Walls (Full System) - {cmu_wall_sqft:,.0f} sqft @ ${cmu_rate:.2f}", cmu_wall_sqft,
              cmu_rate, _get_markup('cmu_walls_full')),
        _line(f"Lyme Wash Walls - {lymewash_sqft:,.0f} sqft @ ${lymewash_rate:.2f}", lymewash_sqft,
              lymewash_rate, _get_markup('lymewash') if 'lymewash' in pm else 0.06),
        _line(f"Plaster Walls - {plaster_sqft:,.0f} sqft @ ${plaster_rate:.2f}", plaster_sqft,
              plaster_rate, _get_markup('plaster') if 'plaster' in pm else 0.06),
        _line(f"Dryfall Ceiling - {dryfall_sqft:,.0f} sqft @ ${dryfall_rate:.2f}", dryfall_sqft,
              dryfall_rate, _get_markup('dryfall_ceiling')),
        _line(f"Base Trim - {trim_lf:,.0f} LF @ ${trim_rate:.2f}", trim_lf,
              trim_rate, _get_markup('base_trim')),
        _line(f"Doors (Full Paint) - {doors_full:.0f} EA @ ${door_fp_rate:.2f}", doors_full,
              door_fp_rate, _get_markup('doors_full_paint')),
        _line(f"Doors (HM Panel) - {doors_hm:.0f} EA @ ${door_hm_rate:.2f}", doors_hm,
              door_hm_rate, _get_markup('doors_hm_panel')),
        _line(f"Doors (Frame Only) - {doors_frame:.0f} EA @ ${door_frame_rate:.2f}", doors_frame,
              door_frame_rate, _get_markup('doors_frame_only')),
        _line(f"Windows (Interior Paint) - {windows:.0f} EA @ ${win_rate:.2f}", windows,
              win_rate, _get_markup('windows')),
        _line(f"Stairs - {stair_sections:.0f} sections @ ${stair_rate:.2f}", stair_sections,
              stair_rate, _get_markup('stairs')),
        _line(f"Gyp. Between Stairs - {gyp_stairs:,.0f} sqft @ ${gyps_rate:.2f}", gyp_stairs,
              gyps_rate, _get_markup('gyp_between_stairs')),
        _line(f"Level 5 Finish - {level_5_sqft:,.0f} sqft @ ${l5_rate:.2f}", level_5_sqft,
              l5_rate, _get_markup('level_5_finish')),
        _line(f"Concrete Sealer - {concrete_sqft:,.0f} sqft @ ${conc_rate:.2f}", concrete_sqft,
              conc_rate, _get_markup('concrete_sealer')),
        _line(f"Painted Columns - {columns_ea:.0f} EA @ ${col_rate:.2f}", columns_ea,
              col_rate, _get_markup('painted_columns')),
        _line(f"{wc_label} - {wallcovering_sqft:,.0f} sqft @ ${wc_rate:.2f}", wallcovering_sqft,
              wc_rate, _get_markup(wc_markup_key) if wc_markup_key in pm else 0.04),
        _line(f"Stained Wood Panels - {stained_wood_sqft:,.0f} sqft @ ${sw_rate:.2f}", stained_wood_sqft,
              sw_rate, _get_markup('stained_wood') if 'stained_wood' in pm else 0.04),
        _line(f"Interior Soffits - {soffit_sqft:,.0f} sqft @ ${soffit_rate:.2f}", soffit_sqft,
              soffit_rate, _get_markup('interior_soffit') if 'interior_soffit' in pm else 0.06),
        _line(f"Exterior Cornice - {cornice_lf:,.0f} LF @ ${corn_rate:.2f}", cornice_lf,
              corn_rate, _get_markup('exterior_cornice')),
        _line(f"Exterior Window Trim - {window_trim_lf:,.0f} LF @ ${wt_rate:.2f}", window_trim_lf,
              wt_rate, _get_markup('exterior_window_trim')),
        _line(f"Exterior Painting - {ext_paint_sqft:,.0f} sqft @ ${ext_paint_rate:.2f}", ext_paint_sqft,
              ext_paint_rate, _get_markup('exterior_painting') if 'exterior_painting' in pm else 0.04),
        _line(f"Ext. Hardie Siding - {hardie_sqft:,.0f} sqft @ ${hardie_rate:.2f}", hardie_sqft,
              hardie_rate, _get_markup('exterior_hardie_siding') if 'exterior_hardie_siding' in pm else 0.05),
        _line(f"Ext. Azek Trim - {azek_lf:,.0f} LF @ ${azek_rate:.2f}", azek_lf,
              azek_rate, _get_markup('exterior_azek_trim') if 'exterior_azek_trim' in pm else 0.05),
        _line(f"Ext. Corner Boards - {corner_lf:,.0f} LF @ ${corner_rate:.2f}", corner_lf,
              corner_rate, _get_markup('exterior_corner_board') if 'exterior_corner_board' in pm else 0.05),
        _line(f"Ext. Steel Lintels - {steel_lintel_lf_ext:,.0f} LF @ ${lintel_rate:.2f}", steel_lintel_lf_ext,
              lintel_rate, _get_markup('exterior_steel_lintel') if 'exterior_steel_lintel' in pm else 0.05),
        _line(f"Exterior Lift Rental - {lift_needed} EA @ ${lift_rate:.2f}", lift_needed,
              lift_rate, _get_markup('exterior_lift_rental')),
        _line(f"Interior Lift Rental - {int_lift_needed} EA @ ${int_lift_rate:.2f}", int_lift_needed,
              int_lift_rate, _get_markup('interior_lift_rental')),
        _line(f"Ext. Stain Siding - {stain_siding_sqft:,.0f} sqft @ ${stain_sid_rate:.2f}", stain_siding_sqft,
              stain_sid_rate, 0.05),
        _line(f"Ext. Stain Trim Bands - {stain_trim_lf:,.0f} LF @ ${stain_trim_rate:.2f}", stain_trim_lf,
              stain_trim_rate, 0.05),
        _line(f"Ext. Stain Railing - {stain_railing_lf:,.0f} LF @ ${stain_rail_rate:.2f}", stain_railing_lf,
              stain_rail_rate, 0.05),
        _line(f"Painted Railings - {total_painted_railing_lf:,.0f} LF @ ${painted_rail_rate:.2f}",
              total_painted_railing_lf, painted_rail_rate,
              _get_markup('painted_railing') if 'painted_railing' in pm else 0.06),
    ]

    # If footprint pricing is active, replace individual interior line items
    # with a single footprint-based line. Keep exterior items as-is.
    if _use_footprint_pricing:
        _interior_keys = {"Gyp. Walls", "Gyp. Ceilings", "CMU Walls", "Dryfall Ceiling",
                          "Base Trim", "Doors", "Windows", "Stairs", "Gyp. Between",
                          "Level 5", "Concrete Sealer", "Painted Columns",
                          "Wallcovering", "Stained Wood", "Interior Soffits", "Interior Lift",
                          "Lyme Wash", "Plaster Walls"}
        # Remove individual interior items
        line_items = [li for li in line_items
                      if not any(li["item"].startswith(k) for k in _interior_keys)]
        # Add footprint-based interior line (use _fp_rate already calculated above
        # which accounts for single-family vs institutional rate differences)
        line_items.insert(0, _line(
            f"Interior (Footprint) - {footprint_sqft:,.0f} sqft @ ${_fp_rate:.2f}",
            footprint_sqft, _fp_rate, 0.00  # Rider uses 0% markup on footprint pricing
        ))

    subtotal = sum(li["total"] for li in line_items)

    exclusions = _build_standard_exclusions(
        analysis=analysis,
        aggregated_totals=aggregated_totals,
        exterior=exterior,
        building_type=bt,
    )

    return {
        "line_items": line_items,
        "subtotal": round(subtotal, 2),
        "exclusions": exclusions,
    }


def _build_standard_exclusions(analysis=None, aggregated_totals=None,
                                exterior=None, building_type=""):
    """Standard Rider Painting exclusions surfaced on every estimate.

    These are scope-protection defaults that the pipeline already enforces
    silently in extraction (e.g., ACT not painted, factory-finished items not
    re-finished). Surfacing them in the output so the GC sees the assumptions
    rather than discovering them at the bid table.

    Returns a list of dicts: {"item": str, "reason": str, "category": str}
    """
    aggregated_totals = aggregated_totals or {}
    exterior = exterior or {}
    bt = str(building_type).lower()

    items = [
        {
            "category": "Ceilings",
            "item": "ACT (acoustical ceiling tile) and suspended/drop ceilings",
            "reason": "Not painted unless specifically called for in the finish schedule.",
        },
        {
            "category": "Doors / Frames / Trim",
            "item": "Factory-finished doors, frames, windows, millwork, and casework",
            "reason": "Excluded unless drawings explicitly require field finishing.",
        },
        {
            "category": "Coordination",
            "item": "Cut-in / patching of work installed by other trades",
            "reason": "By others. Rider Painting paints to a clean, prepared substrate.",
        },
        {
            "category": "Coordination",
            "item": "Repair of trade damage and rework caused by other trades",
            "reason": "By others. Touch-up of Rider's own work is included.",
        },
        {
            "category": "Building Envelope",
            "item": "Window-return sealant functioning as enclosure / air barrier",
            "reason": "By others where indicated as part of the air-barrier system.",
        },
        {
            "category": "Substrates",
            "item": "Painting of mechanical, electrical, plumbing, and fire-protection equipment",
            "reason": "Not included unless specifically scheduled for paint.",
        },
        {
            "category": "Substrates",
            "item": "Galvanized metal, stainless steel, anodized aluminum, and pre-finished metal panels",
            "reason": "Excluded unless drawings or specs explicitly require field paint.",
        },
        {
            "category": "Site",
            "item": "Striping, traffic markings, signage, and pavement coatings",
            "reason": "By others unless specifically included.",
        },
        {
            "category": "Hazardous Materials",
            "item": "Lead paint, asbestos, mold abatement, and any hazmat removal",
            "reason": "By others. Rider does not perform abatement.",
        },
        {
            "category": "Conditions",
            "item": "Heat, temporary power, water, and access provided by GC",
            "reason": "By GC / others. Required for paint application per manufacturer specs.",
        },
    ]

    # Conditional exclusions based on what was extracted
    if _num(aggregated_totals.get("total_cmu_wall_sqft", 0)) == 0:
        items.append({
            "category": "Substrates",
            "item": "Block filler / sealing of unpainted CMU",
            "reason": "No painted CMU surfaces identified in this scope.",
        })

    if _num(exterior.get("exterior_paint_sqft", 0)) == 0 and \
       _num(exterior.get("hardie_siding_sqft", 0)) == 0 and \
       _num(exterior.get("cornice_lf", 0)) == 0:
        items.append({
            "category": "Exterior",
            "item": "All exterior painting, staining, and clear sealer scope",
            "reason": "No exterior painting scope identified — interior-only bid.",
        })

    if "commercial" in bt:
        items.append({
            "category": "Coordination",
            "item": "Painting of access panels, fire dampers, and rated assemblies "
                    "after rough-in inspections by others",
            "reason": "Final paint coordinated with GC after MEP sign-off; "
                      "additional mobilizations may be billed.",
        })

    return items


def _validate_cost_estimate(analysis, cost_estimate):
    """Flag line items with concerning patterns for quality review."""
    warnings = []
    agg = analysis.get('aggregated_totals', {})
    pi = analysis.get('project_info', {})
    subtotal = cost_estimate.get('subtotal', 0)
    building_type = str(pi.get('building_type', '')).lower()

    # 1. Zero-quantity checks for expected items
    total_units = _num(pi.get('total_units', 0))
    if total_units > 5:
        doors_total = (_num(agg.get('total_doors_full_paint', 0))
                       + _num(agg.get('total_doors_hm_panel', 0))
                       + _num(agg.get('total_doors_frame_only', 0)))
        if doors_total == 0:
            warnings.append({
                "severity": "high",
                "item": "Doors",
                "message": f"0 doors extracted for {total_units:.0f}-unit building. "
                           f"Expected ~{total_units * 5:.0f}+"
            })

    # 2. CMU/dryfall scope gap detection for commercial buildings
    if any(kw in building_type for kw in ('commercial', 'warehouse', 'industrial')):
        cmu = _num(agg.get('total_cmu_wall_sqft', 0))
        dryfall = _num(agg.get('total_dryfall_ceiling_sqft', 0))
        if cmu == 0 and dryfall == 0:
            warnings.append({
                "severity": "medium",
                "item": "CMU/Dryfall",
                # Under hard-numbers policy a 0 here is the POLICY-CORRECT outcome
                # when no spec confirms painted CMU / exposed-deck coating — it is
                # surfaced as an RFI, not fabricated. Don't also dock confidence for
                # behaving correctly (see scoring loop's policy_zero handling).
                "policy_zero": bool(HARD_NUMBERS_ONLY),
                "message": "Commercial building with no CMU walls or dryfall ceiling detected. "
                           "Verify specs for painted CMU or exposed ceiling coating."
            })

    # 3. Line-item concentration check (any single item > 40% of total)
    # Skip on small jobs: when subtotal < $15k or ≤4 line items, one item
    # dominating is structural to the scope (e.g. walls naturally are ~55% of
    # a small-TI estimate that only has walls/ceilings/trim/doors), not a
    # signal that the takeoff is wrong.
    _line_items = cost_estimate.get('line_items', [])
    if subtotal >= 15000 and len(_line_items) > 4:
        for item in _line_items:
            item_total = item.get('total', 0)
            if subtotal > 0 and item_total > 0 and item_total / subtotal > 0.40:
                warnings.append({
                    "severity": "medium",
                    "item": item.get('item', ''),
                    "message": f"Single line item is {item_total/subtotal:.0%} of total estimate. "
                               f"Review for accuracy."
                })

    # 4. Zero walls check
    wall_sqft = _num(agg.get('total_paintable_wall_sqft', 0))
    cmu_sqft = _num(agg.get('total_cmu_wall_sqft', 0))
    if wall_sqft == 0 and cmu_sqft == 0:
        warnings.append({
            "severity": "high",
            "item": "Walls",
            "message": "No paintable wall area detected. Verify extraction."
        })

    # 4b. Wallcovering gap detection — check if notes mention WC-x but 0 sqft extracted
    wc_sqft = _num(agg.get('total_wallcovering_sqft', 0))
    if wc_sqft == 0:
        # Scan analysis notes for wallcovering references
        all_notes = " ".join(str(n) for n in analysis.get("notes", []))
        if any(kw in all_notes.lower() for kw in ("wc-", "wallcovering", "wall covering", "vinyl wall")):
            warnings.append({
                "severity": "high",
                "item": "Wallcovering",
                # Policy-driven zero: the hard-numbers policy deliberately did NOT
                # estimate wallcovering without a finish schedule / WC label, and
                # an RFI is already generated to obtain it. This is correct
                # behavior, not a degraded extraction — keep it visible as a
                # warning but don't deduct from confidence (it fires on nearly
                # every commercial job and was pinning scores at 50-60).
                "policy_zero": True,
                "message": "Scope/notes reference wallcovering or wallpaper but 0 sqft was extracted "
                           "(no finish schedule or explicit WC label confirms which walls). Per hard-numbers "
                           "policy the quantity was NOT estimated. Provide the room finish schedule (or confirm "
                           "the wallpapered walls and whether the scope is removal, new install, or both) so the "
                           "wallcovering line can be priced."
            })

    # 4c. Cornice validation for commercial buildings (EIFS/parapet misidentification)
    ext = analysis.get('exterior', {})
    cornice_lf_val = _num(ext.get('cornice_lf', 0))
    if cornice_lf_val > 0 and any(kw in building_type for kw in ('commercial', 'auto', 'industrial', 'warehouse')):
        warnings.append({
            "severity": "medium",
            "item": "Exterior Cornice",
            "message": f"{cornice_lf_val:.0f} LF exterior cornice extracted for commercial building. "
                       f"Verify this is actual painted cornice and not EIFS/parapet/coping details."
        })

    # 4d. Door count validation — check if schedule notes mention non-HM types but all counted
    schedule_data = analysis.get('schedule_data', {})
    door_sched = schedule_data.get('door_schedule', {})
    door_notes = str(door_sched.get('notes', '')).lower()
    door_total = (_num(agg.get('total_doors_full_paint', 0))
                  + _num(agg.get('total_doors_hm_panel', 0))
                  + _num(agg.get('total_doors_frame_only', 0)))
    if door_total > 15 and any(kw in door_notes for kw in ('storefront', 'ad1', 'aluminum', 'overhead', 'ohd', 'wood', 'wd1')):
        warnings.append({
            "severity": "medium",
            "item": "Door Count",
            "message": f"{door_total:.0f} painted doors counted, but schedule notes reference "
                       f"non-painted types (storefront/OH/wood). Verify only HM doors are counted. "
                       f"Typical commercial HM count is 10-15 for a building this size."
        })

    # 5. PCA cross-checks using industry multipliers
    # Door SF cross-check: total door paintable SF should be ~3-12% of wall area
    # on average-sized rooms, but small-TI suites (avg room < 250 SF) pack many
    # doors into small rooms — for those, the expected band is 10-25%. Pick the
    # threshold by avg room size to avoid false-positive over-count flags.
    if door_total > 0 and wall_sqft > 0:
        _pca_door_sf = PCA_CONSTANTS.get("door_flush_sf", 42)
        _pca_frame_sf = PCA_CONSTANTS.get("door_frame_sf", 34)
        _total_door_sf = (
            _num(agg.get('total_doors_full_paint', 0)) * (_pca_door_sf + _pca_frame_sf) +
            _num(agg.get('total_doors_hm_panel', 0)) * _pca_door_sf +
            _num(agg.get('total_doors_frame_only', 0)) * _pca_frame_sf
        )
        _door_wall_ratio = _total_door_sf / wall_sqft if wall_sqft > 0 else 0
        _total_rooms = sum(len(f.get("rooms", [])) for f in analysis.get("floors", []))
        _footprint = _num(pi.get("footprint_sqft", 0))
        _avg_room_sf = (_footprint / _total_rooms) if _total_rooms > 0 else 0
        _small_room_mode = 0 < _avg_room_sf < 250
        _door_ratio_threshold = 0.28 if _small_room_mode else 0.15
        _door_ratio_expected = "10-25%" if _small_room_mode else "3-12%"
        if _door_wall_ratio > _door_ratio_threshold:
            warnings.append({
                "severity": "medium",
                "item": "Door Count (PCA)",
                "message": f"Door surface area ({_total_door_sf:,.0f} SF at PCA multipliers) is "
                           f"{_door_wall_ratio:.0%} of wall area. Typical range is {_door_ratio_expected}. "
                           f"Possible over-count of doors."
            })

    # Stair section cross-check: each section ≈ 240 SF (12 risers × 20 SF/riser)
    stair_sections = _num(agg.get('total_stair_sections', 0))
    if stair_sections > 0:
        _pca_stair_sf = (PCA_CONSTANTS.get("stair_risers_per_section", 12)
                         * PCA_CONSTANTS.get("stair_sf_per_riser", 20))
        _total_stair_sf = stair_sections * _pca_stair_sf
        _stair_cost = stair_sections * 1500  # Current flat rate
        if subtotal > 0 and _stair_cost / subtotal > 0.15:
            warnings.append({
                "severity": "medium",
                "item": "Stairs (PCA)",
                "message": f"{stair_sections:.0f} stair sections = {_total_stair_sf:,.0f} SF "
                           f"(PCA: {_pca_stair_sf} SF/section). Stair cost is "
                           f"{_stair_cost/subtotal:.0%} of total — verify section count."
            })

    # 6. Data quality score (0-100)
    # Decouple POLICY-driven zeros from extraction QUALITY. A quantity that is 0
    # because the hard-numbers policy correctly refused to fabricate it (and
    # raised an RFI instead) should not also tank the confidence score — that
    # double-counts the same gap (once as an RFI, once as a -20/-10) and was
    # pinning commercial jobs at 50-60 even when the extraction was sound.
    # Such warnings are tagged policy_zero and still shown to the user, but are
    # excluded from the deduction. Genuine failures (zero walls/doors with no
    # policy explanation) keep their full penalty. Set
    # NIGHTSHIFT_CONFIDENCE_DECOUPLE=0 to restore the old behavior.
    decouple = os.environ.get("NIGHTSHIFT_CONFIDENCE_DECOUPLE", "1") == "1"
    quality_score = 100
    policy_excluded = 0
    for w in warnings:
        if decouple and w.get("policy_zero"):
            policy_excluded += 1
            continue
        if w["severity"] == "high":
            quality_score -= 20
        elif w["severity"] == "medium":
            quality_score -= 10
    quality_score = max(0, quality_score)

    return {
        "warnings": warnings,
        "data_quality_score": quality_score,
        "warning_count": len(warnings),
        "policy_excluded_warnings": policy_excluded,
    }


def _compute_labor_hours(analysis):
    """Compute PCA-based labor hour estimates for the project.

    Returns a dict suitable for JSON serialization and PDF rendering.
    """
    totals = analysis.get('aggregated_totals', {})
    _pca_labor = PCA_CONSTANTS.get("labor_rates", {})
    if not _pca_labor:
        return {}

    _wall_sf = _num(totals.get('total_paintable_wall_sqft', 0))
    _ceil_sf = _num(totals.get('total_paintable_ceiling_sqft', 0))
    _cmu_sf = _num(totals.get('total_cmu_wall_sqft', 0))
    _dryfall_sf = _num(totals.get('total_dryfall_ceiling_sqft', 0))
    _trim_lf = _num(totals.get('total_base_trim_lf', 0))
    _doors_f = _num(totals.get('total_doors_full_paint', 0))
    _doors_h = _num(totals.get('total_doors_hm_panel', 0))
    _doors_fr = _num(totals.get('total_doors_frame_only', 0))
    _wins = _num(totals.get('total_windows_painted_interior', 0))
    _stairs = _num(totals.get('total_stair_sections', 0))
    _wc_sf = _num(totals.get('total_wallcovering_sqft', 0))

    _pca_door_sf = PCA_CONSTANTS.get("door_flush_sf", 42)
    _pca_frame_sf = PCA_CONSTANTS.get("door_frame_sf", 34)
    _pca_win_sf = PCA_CONSTANTS.get("window_sf_default", 32)
    _pca_stair_sf = (PCA_CONSTANTS.get("stair_risers_per_section", 12)
                     * PCA_CONSTANTS.get("stair_sf_per_riser", 20))

    categories = []

    def _add(name, surface_sf, rate_key, coats=2):
        rate = _pca_labor.get(rate_key, 0)
        if surface_sf > 0 and rate > 0:
            hrs = (surface_sf / rate) * coats
            categories.append({
                "category": name,
                "surface_sf": round(surface_sf),
                "rate_sf_hr": rate,
                "coats": coats,
                "hours": round(hrs, 1),
            })

    _add("Walls (GYP)", _wall_sf, "gyp_walls_spray_1st")
    _add("Ceilings (GYP)", _ceil_sf, "gyp_ceilings_spray")
    _add("CMU Walls", _cmu_sf, "cmu_spray")
    _add("Dryfall Ceiling", _dryfall_sf, "dryfall_spray", coats=1)
    _add("Base Trim", _trim_lf * 1.0, "base_trim_brush", coats=1)  # 1 SF/LF

    # Doors: convert count to SF via PCA multipliers
    if _doors_f > 0:
        _df_sf = _doors_f * (_pca_door_sf + _pca_frame_sf)
        _add("Doors (Full Paint)", _df_sf, "doors_steel_spray_1st")
    if _doors_h > 0:
        _dh_sf = _doors_h * _pca_door_sf
        _add("Doors (HM Panel)", _dh_sf, "doors_steel_spray_1st")
    if _doors_fr > 0:
        _dfr_sf = _doors_fr * _pca_frame_sf
        _add("Doors (Frame Only)", _dfr_sf, "doors_steel_spray_1st")

    # Windows: convert count to SF
    if _wins > 0:
        _win_total_sf = _wins * _pca_win_sf
        _add("Windows", _win_total_sf, "windows_brush_1st")

    # Stairs
    if _stairs > 0:
        _stair_total_sf = _stairs * _pca_stair_sf
        _add("Stairs", _stair_total_sf, "stairs_brush_1st")

    # Wallcovering
    if _wc_sf > 0:
        _add("Wallcovering", _wc_sf, "wallcovering_54in", coats=1)

    prod_hours = sum(c["hours"] for c in categories)
    setup_hours = round(prod_hours * 0.15, 1)
    total_hours = round(prod_hours + setup_hours, 1)

    return {
        "categories": categories,
        "production_hours": round(prod_hours, 1),
        "setup_cleanup_hours": setup_hours,
        "total_hours": total_hours,
        "crew_days": round(total_hours / 8, 1),
    }


def print_estimate(analysis, costs):
    """Print the painting estimate"""

    print("\n" + "="*80)
    print("🎨 PAINTING ESTIMATE FROM CONSTRUCTION DRAWINGS")
    print("="*80)

    project_info = analysis.get('project_info', {})
    template_rooms = project_info.get('template_rooms')
    rooms_found = project_info.get('total_rooms_found', 0)
    print(f"\n📊 PROJECT SUMMARY:")
    print(f"  • Floors Analyzed: {project_info.get('total_floors_analyzed', 0)}")
    if template_rooms and template_rooms != rooms_found:
        print(f"  • Rooms Found: {rooms_found} effective ({template_rooms} templates)")
    else:
        print(f"  • Rooms Found: {rooms_found}")
    print(f"  • Scale: {project_info.get('scale_notation', 'Not specified')}")

    # Unit multiplication summary
    unit_mult = analysis.get('unit_multiplication', {})
    if unit_mult.get('applied'):
        print(f"\n🔄 UNIT MULTIPLICATION:")
        for detail in unit_mult.get('details', []):
            ut = detail.get('unit_type') or detail.get('floor', '')
            print(f"  • {detail.get('room_name', detail.get('room_id', '?'))}"
                  f" ({ut}) × {detail['unit_multiplier']} units")

    # Show room breakdown by floor (with dimensions, materials, and elements)
    print(f"\n🏢 ROOM BREAKDOWN BY FLOOR:")
    for floor in analysis.get('floors', []):
        print(f"\n  {floor['floor_name']}:")
        for room in floor.get('rooms', []):
            dims = room.get('dimensions', {})
            mats = room.get('materials', {})
            elems = room.get('elements', {})
            multiplier = _extract_multiplier_from_notes(room)
            mult_label = f" (x{multiplier} units)" if multiplier > 1 else ""
            _rname = room.get('room_name', room.get('room_id', 'Unknown'))
            _sheet = room.get('source_sheet', '')
            _sheet_label = f" [{_sheet}]" if _sheet else ""

            # Line 1: Room name, dimensions, materials
            _wall_mat = str(mats.get('walls', '-'))[:6]
            _ceil_mat = str(mats.get('ceiling', '-'))[:6]
            _ceil_ptd = "painted" if mats.get('ceiling_painted', False) else "not ptd"
            print(f"    {_rname}{mult_label}{_sheet_label}")
            print(f"      Dims: {dims.get('length_feet', 0)}' x {dims.get('width_feet', 0)}'"
                  f" x {dims.get('ceiling_height_feet', 0)}'"
                  f"  |  Walls: {_wall_mat}  |  Ceil: {_ceil_mat} ({_ceil_ptd})")

            # Line 2: Surfaces
            _w_sf = _num(dims.get('wall_area_sqft', 0))
            _c_sf = _num(dims.get('ceiling_area_sqft', 0))
            _t_lf = _num(elems.get('base_trim_lf', 0))
            print(f"      Surfaces: {_w_sf:,.0f} wall SF  |  {_c_sf:,.0f} ceil SF  |  {_t_lf:,.0f} trim LF")

            # Line 3: Elements (only if any non-zero)
            _dr_fp = _num(elems.get('doors_full_paint', 0))
            _dr_hm = _num(elems.get('doors_hm_panel', 0))
            _dr_fr = _num(elems.get('doors_frame_only', 0))
            _win = _num(elems.get('windows_painted_interior', 0))
            _st = _num(elems.get('stair_sections', 0))
            _wc = _num(elems.get('wallcovering_sqft', 0))
            _sw = _num(elems.get('stained_wood_sqft', 0))
            _sof = _num(elems.get('soffit_sqft', 0))
            elem_parts = []
            if _dr_fp > 0: elem_parts.append(f"{int(_dr_fp)} dr-FP")
            if _dr_hm > 0: elem_parts.append(f"{int(_dr_hm)} dr-HM")
            if _dr_fr > 0: elem_parts.append(f"{int(_dr_fr)} dr-Fr")
            if _win > 0: elem_parts.append(f"{int(_win)} win")
            if _st > 0: elem_parts.append(f"{int(_st)} stairs")
            if _wc > 0: elem_parts.append(f"{_wc:,.0f} WC-sf")
            if _sw > 0: elem_parts.append(f"{_sw:,.0f} stain-sf")
            if _sof > 0: elem_parts.append(f"{_sof:,.0f} soffit-sf")
            if elem_parts:
                print(f"      Elements: {' | '.join(elem_parts)}")

    # Aggregated totals
    totals = analysis.get('aggregated_totals', {})
    print(f"\n📐 AGGREGATED MEASUREMENTS:")
    print(f"  • Walls:                    {_num(totals.get('total_paintable_wall_sqft', 0)):>10,.0f} sqft")
    print(f"  • Ceilings (painted only):  {_num(totals.get('total_paintable_ceiling_sqft', 0)):>10,.0f} sqft")
    print(f"  • Base Trim:                {_num(totals.get('total_base_trim_lf', 0)):>10,.0f} LF")
    print(f"  • Doors (full paint):       {_num(totals.get('total_doors_full_paint', 0)):>10.0f}")
    print(f"  • Doors (HM panel):         {_num(totals.get('total_doors_hm_panel', 0)):>10.0f}")
    print(f"  • Windows (painted int.):   {_num(totals.get('total_windows_painted_interior', 0)):>10.0f}")
    print(f"  • Windows (all):            {_num(totals.get('total_windows_all', 0)):>10.0f}")
    print(f"  • Stair Sections:           {_num(totals.get('total_stair_sections', 0)):>10.0f}")

    exterior = analysis.get('exterior', {})
    ext_has_data = (exterior and (
        _num(exterior.get('cornice_lf', 0)) > 0 or
        _num(exterior.get('window_trim_lf', 0)) > 0
    ))
    if ext_has_data:
        print(f"\n🏗️  EXTERIOR:")
        if _num(exterior.get('cornice_lf', 0)) > 0:
            print(f"  • Cornice/Brackets:  {_num(exterior.get('cornice_lf', 0)):>10,.0f} LF")
        if _num(exterior.get('window_trim_lf', 0)) > 0:
            print(f"  • Window Trim:       {_num(exterior.get('window_trim_lf', 0)):>10,.0f} LF")
        print(f"  • Lift Required:     {'Yes' if exterior.get('lift_required') else 'No':>10}")

    # Cost breakdown
    print(f"\n💰 COST BREAKDOWN (Rider Painting Pricing):")
    print(f"{'ITEM':<44} {'QTY':>8} {'COST':>12} {'MARKUP':>10} {'TOTAL':>12}")
    print("-" * 86)

    for item in costs['line_items']:
        if item['qty'] > 0:
            print(f"{item['item']:<44} {item['qty']:>8.0f} ${item['cost']:>11,.2f}"
                  f" ${item['markup']:>9,.2f} ${item['total']:>11,.2f}")

    print("=" * 86)
    print(f"{'TOTAL PROJECT COST:':<67} ${costs['subtotal']:>16,.2f}")
    print("=" * 86)

    # PCA-based estimated labor hours (informational, not used for pricing)
    _pca_labor = PCA_CONSTANTS.get("labor_rates", {})
    if _pca_labor:
        _wall_sf = _num(totals.get('total_paintable_wall_sqft', 0))
        _ceil_sf = _num(totals.get('total_paintable_ceiling_sqft', 0))
        _cmu_sf = _num(totals.get('total_cmu_wall_sqft', 0))
        _dryfall_sf = _num(totals.get('total_dryfall_ceiling_sqft', 0))
        _trim_lf = _num(totals.get('total_base_trim_lf', 0))
        _doors_f = _num(totals.get('total_doors_full_paint', 0))
        _doors_h = _num(totals.get('total_doors_hm_panel', 0))
        _doors_fr = _num(totals.get('total_doors_frame_only', 0))
        _wins = _num(totals.get('total_windows_painted_interior', 0))
        _stairs = _num(totals.get('total_stair_sections', 0))
        _wc_sf = _num(totals.get('total_wallcovering_sqft', 0))

        _pca_door_sf = PCA_CONSTANTS.get("door_flush_sf", 42)
        _pca_frame_sf = PCA_CONSTANTS.get("door_frame_sf", 34)
        _pca_win_sf = PCA_CONSTANTS.get("window_sf_default", 32)
        _pca_stair_sf = (PCA_CONSTANTS.get("stair_risers_per_section", 12)
                         * PCA_CONSTANTS.get("stair_sf_per_riser", 20))

        # Calculate hours per category (assumes spray for walls/ceilings, 2 coats)
        _est = {}
        _spray_wall_rate = _pca_labor.get("gyp_walls_spray_1st", 650)
        _spray_wall_add = _pca_labor.get("gyp_walls_spray_add", 750)
        _est["Walls (GYP)"] = (_wall_sf / _spray_wall_rate) + (_wall_sf / _spray_wall_add) if _wall_sf > 0 else 0
        _est["Ceilings (GYP)"] = (_ceil_sf / _pca_labor.get("gyp_ceilings_spray", 650)) * 2 if _ceil_sf > 0 else 0
        _est["CMU Walls"] = (_cmu_sf / _pca_labor.get("cmu_spray", 488)) * 2 if _cmu_sf > 0 else 0
        _est["Dryfall Ceiling"] = _dryfall_sf / _pca_labor.get("dryfall_spray", 400) if _dryfall_sf > 0 else 0
        _est["Base Trim"] = (_trim_lf * 1.0) / _pca_labor.get("base_trim_brush", 100) if _trim_lf > 0 else 0
        _est["Doors (full)"] = ((_doors_f * _pca_door_sf) / _pca_labor.get("doors_steel_spray_1st", 200)) * 2 if _doors_f > 0 else 0
        _est["Doors (HM panel)"] = ((_doors_h * _pca_door_sf * 0.5) / _pca_labor.get("doors_steel_spray_1st", 200)) * 2 if _doors_h > 0 else 0
        _est["Doors (frame)"] = ((_doors_fr * _pca_frame_sf) / _pca_labor.get("doors_steel_spray_1st", 200)) * 2 if _doors_fr > 0 else 0
        _est["Windows"] = ((_wins * _pca_win_sf) / _pca_labor.get("windows_brush_1st", 85)) * 2 if _wins > 0 else 0
        _est["Stairs"] = ((_stairs * _pca_stair_sf) / _pca_labor.get("stairs_brush_1st", 100)) * 2 if _stairs > 0 else 0
        _est["Wallcovering"] = _wc_sf / _pca_labor.get("wallcovering_54in", 45) if _wc_sf > 0 else 0

        _total_prod_hrs = sum(_est.values())
        # Add 15% for setup, cleanup, mobilization per PCA guidelines
        _total_adjusted_hrs = _total_prod_hrs * 1.15

        if _total_prod_hrs > 0:
            print(f"\n  ESTIMATED LABOR HOURS (PCA Production Rates):")
            for _cat, _hrs in _est.items():
                if _hrs > 0:
                    print(f"  {'  ' + _cat + ':':<30} {_hrs:>8.1f} hrs")
            print(f"  {'-'*38}")
            print(f"  {'  Production Hours:':<30} {_total_prod_hrs:>8.1f} hrs")
            print(f"  {'  + Setup/Cleanup (15%):':<30} {_total_prod_hrs * 0.15:>8.1f} hrs")
            print(f"  {'  TOTAL ESTIMATED HOURS:':<30} {_total_adjusted_hrs:>8.1f} hrs")
            _crew_days = _total_adjusted_hrs / 8  # 8-hour days
            print(f"  {'  (1-person days):':<30} {_crew_days:>8.1f} days")

    # Scope summary
    scope_summary = analysis.get('scope_summary', {})
    if scope_summary:
        in_scope = scope_summary.get('rooms_in_scope', 0)
        excluded = scope_summary.get('rooms_excluded', 0)
        print(f"\n📋 SCOPE FILTER APPLIED:")
        print(f"  • Rooms in scope:   {in_scope}")
        print(f"  • Rooms excluded:   {excluded}")
        for excl in scope_summary.get('excluded_rooms', [])[:5]:
            print(f"    ✗ {excl.get('room_name', excl.get('room_id', '?'))}"
                  f" ({excl.get('floor', '?')}) — {excl.get('reason', '')}")
        remaining = len(scope_summary.get('excluded_rooms', [])) - 5
        if remaining > 0:
            print(f"    ... and {remaining} more")

    # Notes
    notes = analysis.get('notes', [])
    if notes:
        print(f"\n📝 NOTES:")
        for note in notes:
            print(f"  • {note}")

def interactive_adjustments(analysis, costs, pricing_model_used=None):
    """Post-run interactive CLI for adjusting pricing, measurements, counts, and scope.

    Displays a menu loop allowing the user to modify values and immediately see
    recalculated costs.  Returns the final (analysis, costs, pricing_model_used,
    adjustments_log) tuple.
    """
    import copy
    if pricing_model_used is None:
        pricing_model_used = copy.deepcopy(PRICING_MODEL)
    else:
        pricing_model_used = copy.deepcopy(pricing_model_used)

    adjustments_log = []
    totals = analysis.get('aggregated_totals', {})
    exterior = analysis.get('exterior', {})

    def _recalc():
        nonlocal costs
        costs = calculate_costs(
            totals,
            exterior=exterior,
            building_type=analysis.get('project_info', {}).get('building_type', ''),
            project_info=analysis.get('project_info', {}),
            analysis=analysis,
            pricing_model_override=pricing_model_used,
        )
        print_estimate(analysis, costs)

    def _prompt(msg):
        try:
            return input(msg).strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    # ── Measurement key labels ──
    _meas_keys = [
        ("total_paintable_wall_sqft",       "Gyp. Walls (sqft)"),
        ("total_paintable_ceiling_sqft",    "Gyp. Ceilings (sqft)"),
        ("total_cmu_wall_sqft",             "CMU Walls (sqft)"),
        ("total_dryfall_ceiling_sqft",      "Dryfall Ceiling (sqft)"),
        ("total_base_trim_lf",              "Base Trim (LF)"),
        ("total_concrete_floor_sqft",       "Concrete Floor (sqft)"),
        ("total_wallcovering_sqft",         "Wallcovering (sqft)"),
        ("total_stained_wood_sqft",         "Stained Wood (sqft)"),
        ("total_soffit_sqft",               "Interior Soffits (sqft)"),
        ("total_gyp_between_stairs_sqft",   "Gyp. Between Stairs (sqft)"),
        ("total_level_5_finish_sqft",       "Level 5 Finish (sqft)"),
    ]

    _count_keys = [
        ("total_doors_full_paint",          "Doors (Full Paint)"),
        ("total_doors_hm_panel",            "Doors (HM Panel)"),
        ("total_doors_frame_only",          "Doors (Frame Only)"),
        ("total_windows_painted_interior",  "Windows (Painted Int.)"),
        ("total_windows_all",               "Windows (All)"),
        ("total_stair_sections",            "Stair Sections"),
        ("total_painted_columns_ea",        "Painted Columns"),
    ]

    # ── Rate shorthand → PRICING_MODEL key ──
    _rate_items = [
        ("gyp_walls",          "Gyp. Walls"),
        ("gyp_ceilings",       "Gyp. Ceilings"),
        ("base_trim",          "Base Trim"),
        ("doors_full_paint",   "Doors (Full Paint)"),
        ("doors_hm_panel",     "Doors (HM Panel)"),
        ("doors_frame_only",   "Doors (Frame Only)"),
        ("windows",            "Windows"),
        ("stairs",             "Stairs"),
        ("cmu_walls_full",     "CMU Walls"),
        ("dryfall_ceiling",    "Dryfall Ceiling"),
        ("concrete_sealer",    "Concrete Sealer"),
        ("painted_columns",    "Painted Columns"),
        ("wallcovering_install", "Wallcovering Install"),
        ("stained_wood",       "Stained Wood"),
        ("exterior_cornice",   "Ext. Cornice"),
        ("exterior_window_trim","Ext. Window Trim"),
        ("exterior_painting",  "Ext. Painting"),
        ("exterior_lift_rental","Ext. Lift Rental"),
    ]

    while True:
        print("\n┌─────────────────────────────────────────┐")
        print("│  ADJUSTMENT MENU                        │")
        print("│                                         │")
        print("│  1. Adjust Pricing (rates & markups)    │")
        print("│  2. Adjust Measurements (sqft, LF)      │")
        print("│  3. Adjust Counts (doors, windows, etc) │")
        print("│  4. Other (exclude rooms, custom items) │")
        print("│  5. Regenerate PDF with current values  │")
        print("│  6. Save & Exit                         │")
        print("│  0. Exit without saving adjustments     │")
        print("└─────────────────────────────────────────┘")

        choice = _prompt("\nSelect option: ")

        # ── 1. PRICING ──
        if choice == "1":
            while True:
                print("\n── PRICING: Current Rates & Markups ──")
                valid_items = []
                for i, (pm_key, label) in enumerate(_rate_items, 1):
                    if pm_key in pricing_model_used:
                        cfg = pricing_model_used[pm_key]
                        rate = cfg["tiers"][-1]["rate"] if cfg["tiers"] else 0
                        markup = cfg["markup"]
                        unit = cfg.get("unit", "")
                        print(f"  {i:>2}. {label:<28} ${rate:>10,.2f}/{unit}  "
                              f"(markup: {markup*100:.1f}%)")
                        valid_items.append((i, pm_key, label))

                print(f"\n  Type '<number> <new_rate>' to change a rate")
                print(f"  Type '<number> markup <pct>' to change markup (e.g. '1 markup 8')")
                print(f"  Type 'back' to return to main menu")
                inp = _prompt("\n  > ")
                if inp.lower() in ("back", "b", ""):
                    break

                parts = inp.split()
                try:
                    idx = int(parts[0])
                    match = next((pm_key for i, pm_key, _ in valid_items if i == idx), None)
                    if not match:
                        print("  Invalid item number.")
                        continue

                    if len(parts) >= 3 and parts[1].lower() == "markup":
                        new_markup = float(parts[2]) / 100.0
                        old_markup = pricing_model_used[match]["markup"]
                        pricing_model_used[match]["markup"] = new_markup
                        adjustments_log.append(
                            f"Markup {match}: {old_markup*100:.1f}% → {new_markup*100:.1f}%")
                        print(f"  ✓ Markup updated")
                    elif len(parts) >= 2:
                        new_rate = float(parts[1])
                        old_rate = pricing_model_used[match]["tiers"][-1]["rate"]
                        for tier in pricing_model_used[match]["tiers"]:
                            tier["rate"] = new_rate
                        adjustments_log.append(
                            f"Rate {match}: ${old_rate:,.2f} → ${new_rate:,.2f}")
                        print(f"  ✓ Rate updated")
                    else:
                        print("  Usage: <number> <new_rate>  or  <number> markup <pct>")
                        continue

                    _recalc()
                except (ValueError, IndexError):
                    print("  Invalid input. Use: <number> <new_rate>")

        # ── 2. MEASUREMENTS ──
        elif choice == "2":
            while True:
                print("\n── MEASUREMENTS: Current Values ──")
                for i, (key, label) in enumerate(_meas_keys, 1):
                    val = _num(totals.get(key, 0))
                    print(f"  {i:>2}. {label:<32} {val:>12,.0f}")

                print(f"\n  Type '<number> <new_value>' to change")
                print(f"  Type 'back' to return to main menu")
                inp = _prompt("\n  > ")
                if inp.lower() in ("back", "b", ""):
                    break

                parts = inp.split()
                try:
                    idx = int(parts[0]) - 1
                    new_val = float(parts[1].replace(",", ""))
                    if 0 <= idx < len(_meas_keys):
                        key, label = _meas_keys[idx]
                        old_val = _num(totals.get(key, 0))
                        totals[key] = new_val
                        adjustments_log.append(
                            f"Measurement {label}: {old_val:,.0f} → {new_val:,.0f}")
                        print(f"  ✓ {label} updated: {old_val:,.0f} → {new_val:,.0f}")
                        _recalc()
                    else:
                        print("  Invalid item number.")
                except (ValueError, IndexError):
                    print("  Invalid input. Use: <number> <new_value>")

        # ── 3. COUNTS ──
        elif choice == "3":
            while True:
                print("\n── COUNTS: Current Values ──")
                for i, (key, label) in enumerate(_count_keys, 1):
                    val = _num(totals.get(key, 0))
                    print(f"  {i:>2}. {label:<32} {val:>8,.0f}")

                print(f"\n  Type '<number> <new_value>' to change")
                print(f"  Type 'back' to return to main menu")
                inp = _prompt("\n  > ")
                if inp.lower() in ("back", "b", ""):
                    break

                parts = inp.split()
                try:
                    idx = int(parts[0]) - 1
                    new_val = float(parts[1].replace(",", ""))
                    if 0 <= idx < len(_count_keys):
                        key, label = _count_keys[idx]
                        old_val = _num(totals.get(key, 0))
                        totals[key] = new_val
                        adjustments_log.append(
                            f"Count {label}: {old_val:,.0f} → {new_val:,.0f}")
                        print(f"  ✓ {label} updated: {old_val:,.0f} → {new_val:,.0f}")
                        _recalc()
                    else:
                        print("  Invalid item number.")
                except (ValueError, IndexError):
                    print("  Invalid input. Use: <number> <new_value>")

        # ── 4. OTHER ──
        elif choice == "4":
            while True:
                print("\n── OTHER ADJUSTMENTS ──")
                print("  1. Exclude a room by ID")
                print("  2. Include a room by ID (force in-scope)")
                print("  3. Change building type")
                print("  4. Add custom line item")
                print("  5. List rooms")
                print("  Type 'back' to return to main menu")
                inp = _prompt("\n  > ")
                if inp.lower() in ("back", "b", ""):
                    break

                if inp == "1":
                    rid = _prompt("  Room ID to exclude: ")
                    if not rid:
                        continue
                    found = False
                    for floor in analysis.get("floors", []):
                        for room in floor.get("rooms", []):
                            if room.get("room_id", "") == rid:
                                room["in_scope"] = False
                                room["scope_exclusion_reason"] = "Excluded via interactive adjustment"
                                found = True
                    if found:
                        # Recalculate totals from rooms
                        _recalculate_totals(analysis)
                        totals = analysis.get('aggregated_totals', {})
                        adjustments_log.append(f"Room excluded: {rid}")
                        print(f"  ✓ Room {rid} excluded")
                        _recalc()
                    else:
                        print(f"  Room ID '{rid}' not found.")

                elif inp == "2":
                    rid = _prompt("  Room ID to include: ")
                    if not rid:
                        continue
                    found = False
                    for floor in analysis.get("floors", []):
                        for room in floor.get("rooms", []):
                            if room.get("room_id", "") == rid:
                                room["in_scope"] = True
                                room["scope_exclusion_reason"] = ""
                                found = True
                    if found:
                        _recalculate_totals(analysis)
                        totals = analysis.get('aggregated_totals', {})
                        adjustments_log.append(f"Room included: {rid}")
                        print(f"  ✓ Room {rid} included")
                        _recalc()
                    else:
                        print(f"  Room ID '{rid}' not found.")

                elif inp == "3":
                    current_bt = analysis.get('project_info', {}).get('building_type', 'unknown')
                    print(f"  Current building type: {current_bt}")
                    new_bt = _prompt("  New building type: ")
                    if new_bt:
                        analysis.setdefault('project_info', {})['building_type'] = new_bt
                        adjustments_log.append(
                            f"Building type: {current_bt} → {new_bt}")
                        print(f"  ✓ Building type updated")
                        _recalc()

                elif inp == "4":
                    desc = _prompt("  Line item description: ")
                    qty_s = _prompt("  Quantity: ")
                    rate_s = _prompt("  Unit rate ($): ")
                    markup_s = _prompt("  Markup % (default 6): ") or "6"
                    try:
                        qty = float(qty_s.replace(",", ""))
                        rate = float(rate_s.replace(",", ""))
                        markup_pct = float(markup_s) / 100.0
                        cost = qty * rate
                        markup = cost * markup_pct
                        item = {
                            "item": desc,
                            "qty": qty,
                            "cost": round(cost, 2),
                            "markup": round(markup, 2),
                            "total": round(cost + markup, 2),
                        }
                        costs["line_items"].append(item)
                        costs["subtotal"] = round(
                            sum(li["total"] for li in costs["line_items"]), 2)
                        adjustments_log.append(
                            f"Custom line item added: {desc} — ${item['total']:,.2f}")
                        print(f"  ✓ Added: {desc} — ${item['total']:,.2f}")
                        print_estimate(analysis, costs)
                    except ValueError:
                        print("  Invalid numbers.")

                elif inp == "5":
                    for floor in analysis.get("floors", []):
                        print(f"\n  Floor: {floor.get('floor_name', '?')}")
                        for room in floor.get("rooms", []):
                            scope = "IN" if room.get("in_scope", True) else "OUT"
                            rid = room.get("room_id", "?")
                            rname = room.get("room_name", "?")
                            mult = room.get("unit_multiplier", 1)
                            mult_s = f" (×{mult})" if mult > 1 else ""
                            print(f"    [{scope}] {rid} — {rname}{mult_s}")

        # ── 5. REGENERATE PDF ──
        elif choice == "5":
            print("\n  Regenerating PDF...")
            # Save updated JSON and regenerate PDF
            return analysis, costs, pricing_model_used, adjustments_log, True  # regenerate=True

        # ── 6. SAVE & EXIT ──
        elif choice == "6":
            return analysis, costs, pricing_model_used, adjustments_log, True  # regenerate=True

        # ── 0. EXIT WITHOUT SAVING ──
        elif choice == "0":
            return analysis, costs, pricing_model_used, adjustments_log, False  # don't regenerate

        else:
            print("  Invalid option. Enter 1-6 or 0.")

    return analysis, costs, pricing_model_used, adjustments_log, False


def _attach_bbox_anchors(analysis, pdf_path):
    """Run Tier-1 bbox anchoring on an analysis dict. Called by every path
    that produces a (pdf_path, analysis) result so heavy/light/main workers
    all stamp bbox info regardless of which extraction code path was used.

    Failure is non-fatal — rooms get bbox=None entries and a summary records
    the error, but the takeoff result is unchanged.
    """
    if not analysis or not pdf_path:
        return
    try:
        from bbox_spike import attach_label_bboxes
        attach_label_bboxes(analysis, pdf_path)
        _bs = analysis.get("bbox_spike_summary") or {}
        if _bs.get("total_rooms"):
            print(f"   📍 Bbox anchoring: {_bs.get('anchored', 0)}/"
                  f"{_bs.get('total_rooms', 0)} rooms "
                  f"({_bs.get('coverage_pct', 0)}%)", flush=True)
    except Exception as _bbox_err:
        print(f"   ⚠️  Bbox anchoring failed (non-fatal): "
              f"{type(_bbox_err).__name__}: {str(_bbox_err)[:160]}",
              flush=True)


def analyze_and_parse(client, pdf_path, scope_notes="", schedule_hints=None,
                      building_inventory=None, project_overview=None):
    """Analyze a single PDF and return parsed JSON. Returns (path, analysis_dict) or None on failure."""
    filename = os.path.basename(pdf_path)
    try:
        result_text = analyze_construction_pdf(client, pdf_path, scope_notes=scope_notes,
                                                schedule_hints=schedule_hints,
                                                building_inventory=building_inventory,
                                                project_overview=project_overview)
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            analysis = json.loads(json_match.group())
            _attach_bbox_anchors(analysis, pdf_path)
            return (pdf_path, analysis)
        else:
            print(f"\n⚠️  Could not parse response for {filename}")
            print(f"   Raw (first 500 chars): {result_text[:500]}")
            return None
    except Exception as e:
        import traceback
        print(f"\n❌ Error analyzing {filename}: {e}")
        print(f"   Traceback: {traceback.format_exc()}")
        return None


# ---------------------------------------------------------------------------
# Merge feature — Phase 2: incremental re-run of a prior analysis with new
# files merged in. Used when a customer sends a revised plan, an RFI
# response, or an amendment instead of re-submitting fresh.
# ---------------------------------------------------------------------------

# Non-floor scope tags. Anything not in this set is treated as a floor name
# and matched case-insensitively against floor.floor_name.
_SCOPE_TAG_DOOR_SCHEDULE = "doorschedule"
_SCOPE_TAG_WINDOW_SCHEDULE = "windowschedule"
_SCOPE_TAG_FINISH_SCHEDULE = "finishschedule"
_SCOPE_TAG_EXTERIOR = "exterior"
_SCOPE_TAG_NONE = "none"  # explicit "additive only" tag — no replacements

_NON_FLOOR_SCOPE_TAGS = {
    _SCOPE_TAG_DOOR_SCHEDULE,
    _SCOPE_TAG_WINDOW_SCHEDULE,
    _SCOPE_TAG_FINISH_SCHEDULE,
    _SCOPE_TAG_EXTERIOR,
    _SCOPE_TAG_NONE,
}


def _norm_scope_tags(scope_tags):
    """Lowercase + strip-spaces normalize so 'Door Schedule' / 'doorschedule'
    / 'DoorSchedule' all collapse to the canonical key."""
    out = set()
    for t in scope_tags or []:
        if not isinstance(t, str):
            continue
        norm = t.strip().lower().replace(" ", "").replace("_", "")
        if norm:
            out.add(norm)
    return out


def _floor_tags_in_scope(scope_tags_normalized):
    """Floor-name tags only (everything not in the special non-floor set)."""
    return scope_tags_normalized - _NON_FLOOR_SCOPE_TAGS


def _floor_name_matches_tag(floor_name, scope_tag_normalized):
    """Match a floor's display name against a normalized scope tag.

    Comparison is case-insensitive, ignores spaces/underscores. Both sides
    are normalized so 'Basement' / 'basement' / 'BASEMENT' all match the
    'basement' tag, and '2nd Floor' / 'Second Floor' / 'second_floor' all
    collapse correctly.
    """
    if not floor_name:
        return False
    fnorm = str(floor_name).strip().lower().replace(" ", "").replace("_", "")
    return fnorm == scope_tag_normalized


def merge_versioned_analyses(prior_analysis, delta_analysis, scope_tags=None):
    """Merge `delta_analysis` (from re-extracting only the new PDFs) into
    `prior_analysis` (from the parent submission's stored JSON).

    Returns a NEW analysis dict — neither input is mutated.

    Merge semantics by section:

      floors[]: per-floor name. If the floor matches a scope tag in
        `scope_tags`, REPLACE that floor wholesale with delta's. Otherwise
        UNION rooms by (floor_name, room_name); delta wins on collisions.
        Floors present only in delta are appended.

      exterior: MAX-merge per numeric field. If `Exterior` is in scope_tags,
        replace wholesale with delta's. Booleans (lift_required) OR'd.
        notes appended.

      schedule_data:
        - door_schedule: replace if 'DoorSchedule' tag OR if delta's
          door_marks_counted list is longer than prior's.
        - window_schedule: replace if 'WindowSchedule' tag OR if delta has
          more window_types entries.
        - stair_info: replace if delta has more stair_sections.

      has_door_schedule / has_window_schedule: OR.

      material_legend: union by code.

      project_info: keep prior; do NOT update aggregated counts here
        (downstream _recalculate_totals + post-extraction passes do that).

      project_overview: keep prior, append source_pdfs from delta.

      Recomputed downstream (orchestrator calls these after merge):
        - aggregated_totals, _perimeter_cross_check, provenance_audit
        - manual_review_required, manual_review_reason
        - notes that came from validation/audit passes

      `notes` here: keep only entries that look like LLM-emitted observations
      (those without bracketed-prefix pipeline markers). Pipeline notes are
      regenerated.
    """
    import copy as _copy

    if not isinstance(prior_analysis, dict):
        raise TypeError("prior_analysis must be a dict")
    if not isinstance(delta_analysis, dict):
        raise TypeError("delta_analysis must be a dict")

    norm_tags = _norm_scope_tags(scope_tags)
    floor_tags = _floor_tags_in_scope(norm_tags)
    additive_only = (_SCOPE_TAG_NONE in norm_tags) or (not norm_tags)

    merged = _copy.deepcopy(prior_analysis)

    # Clear idempotency flags persisted from the prior run's stored JSON.
    # The merged analysis contains NEW rooms/floors that have never been
    # through the dedup / canonicalization / safety-net passes; a stale
    # True flag makes every one of them silently no-op on the re-run
    # (v2 quotes double-counting entire floors was the observed failure).
    for _stale_flag in (
        "_template_floors_deduped",
        "_cross_sheet_rooms_deduped",
        "_residential_corridor_ceiling_fixed",
        "_source_sheets_canonicalized",
        "_residential_ceiling_floor_applied",
    ):
        merged.pop(_stale_flag, None)
    if isinstance(merged.get("project_info"), dict):
        merged["project_info"].pop("_unit_multipliers_validated", None)

    # ── Floors ────────────────────────────────────────────────────────────
    prior_floors = merged.get("floors", []) or []
    delta_floors = delta_analysis.get("floors", []) or []

    # Index prior floors by normalized name
    def _norm_floor_name(fname):
        return str(fname or "").strip().lower().replace(" ", "").replace("_", "")

    prior_by_name = {_norm_floor_name(f.get("floor_name", "")): f for f in prior_floors}
    delta_by_name = {_norm_floor_name(f.get("floor_name", "")): f for f in delta_floors}

    merged_floors = []
    seen_names = set()

    for prior_floor in prior_floors:
        fname = prior_floor.get("floor_name", "")
        fnorm = _norm_floor_name(fname)
        seen_names.add(fnorm)

        # Does any scope tag match this floor name?
        floor_in_scope = (
            not additive_only
            and any(_floor_name_matches_tag(fname, t) for t in floor_tags)
        )

        delta_floor = delta_by_name.get(fnorm)

        if floor_in_scope and delta_floor:
            # REPLACE: scope tag instructs us to drop prior's rooms for this
            # floor and use delta's wholesale. Floors not present in delta
            # but tagged for replacement keep prior (no data to replace with).
            merged_floors.append(_copy.deepcopy(delta_floor))
        elif delta_floor:
            # UNION: combine prior's rooms with delta's rooms, keyed by
            # room_name (case-insensitive). Delta wins on collisions.
            merged_floors.append(_union_floor_rooms(prior_floor, delta_floor))
        else:
            # Floor exists in prior but not in delta — keep as-is.
            merged_floors.append(_copy.deepcopy(prior_floor))

    # Floors only in delta — append.
    for delta_floor in delta_floors:
        fnorm = _norm_floor_name(delta_floor.get("floor_name", ""))
        if fnorm and fnorm not in seen_names:
            merged_floors.append(_copy.deepcopy(delta_floor))
            seen_names.add(fnorm)

    merged["floors"] = merged_floors

    # ── Exterior ──────────────────────────────────────────────────────────
    prior_ext = merged.get("exterior", {}) or {}
    delta_ext = delta_analysis.get("exterior", {}) or {}
    if delta_ext:
        replace_exterior = (
            not additive_only and _SCOPE_TAG_EXTERIOR in norm_tags
        )
        if replace_exterior:
            merged["exterior"] = _copy.deepcopy(delta_ext)
        else:
            merged["exterior"] = _max_merge_exterior(prior_ext, delta_ext)

    # ── Schedule data ─────────────────────────────────────────────────────
    prior_sched = merged.get("schedule_data", {}) or {}
    delta_sched = delta_analysis.get("schedule_data", {}) or {}
    merged_sched = dict(prior_sched)

    # Door schedule: replace if tag OR delta is richer
    p_door = prior_sched.get("door_schedule") or {}
    d_door = delta_sched.get("door_schedule") or {}
    door_tag_replace = (not additive_only) and (_SCOPE_TAG_DOOR_SCHEDULE in norm_tags)
    p_door_marks = len(p_door.get("door_marks_counted") or [])
    d_door_marks = len(d_door.get("door_marks_counted") or [])
    if d_door and (door_tag_replace or d_door_marks > p_door_marks):
        merged_sched["door_schedule"] = _copy.deepcopy(d_door)
        merged["has_door_schedule"] = True

    # Window schedule: replace if tag OR delta is richer
    p_win = prior_sched.get("window_schedule") or {}
    d_win = delta_sched.get("window_schedule") or {}
    win_tag_replace = (not additive_only) and (_SCOPE_TAG_WINDOW_SCHEDULE in norm_tags)
    p_win_types = len(p_win.get("window_types") or [])
    d_win_types = len(d_win.get("window_types") or [])
    if d_win and (win_tag_replace or d_win_types > p_win_types):
        merged_sched["window_schedule"] = _copy.deepcopy(d_win)
        merged["has_window_schedule"] = True

    # Stair info: replace if delta has more stair_sections
    p_stair = prior_sched.get("stair_info") or {}
    d_stair = delta_sched.get("stair_info") or {}
    if d_stair:
        p_secs = float((p_stair.get("total_stair_sections") or 0))
        d_secs = float((d_stair.get("total_stair_sections") or 0))
        if d_secs > p_secs:
            merged_sched["stair_info"] = _copy.deepcopy(d_stair)

    if merged_sched:
        merged["schedule_data"] = merged_sched

    # has_*_schedule: OR with delta
    if delta_analysis.get("has_door_schedule"):
        merged["has_door_schedule"] = True
    if delta_analysis.get("has_window_schedule"):
        merged["has_window_schedule"] = True

    # ── Material legend: union by code ────────────────────────────────────
    p_legend = merged.get("material_legend", []) or []
    d_legend = delta_analysis.get("material_legend", []) or []
    seen_codes = {str(m.get("code", "")).strip().upper() for m in p_legend if isinstance(m, dict)}
    for m in d_legend:
        if isinstance(m, dict):
            code = str(m.get("code", "")).strip().upper()
            if code and code not in seen_codes:
                p_legend.append(_copy.deepcopy(m))
                seen_codes.add(code)
    merged["material_legend"] = p_legend

    # ── Project overview: keep prior, extend source_pdfs ──────────────────
    p_overview = merged.get("project_overview") or {}
    d_overview = delta_analysis.get("project_overview") or {}
    if d_overview:
        p_pdfs = list(p_overview.get("source_pdfs") or [])
        d_pdfs = list(d_overview.get("source_pdfs") or [])
        for pdf in d_pdfs:
            if pdf and pdf not in p_pdfs:
                p_pdfs.append(pdf)
        if p_pdfs:
            p_overview["source_pdfs"] = p_pdfs
        # Carry forward any source_pages dict updates (don't override prior keys).
        d_pages = (d_overview.get("source_pages") or {})
        if isinstance(d_pages, dict):
            p_pages = p_overview.get("source_pages") or {}
            if isinstance(p_pages, dict):
                for pdf_key, page_list in d_pages.items():
                    if pdf_key not in p_pages:
                        p_pages[pdf_key] = list(page_list or [])
                p_overview["source_pages"] = p_pages
        merged["project_overview"] = p_overview

    # ── Notes: keep LLM-content notes; drop pipeline-emitted bracketed ones
    # so post-extraction passes can re-emit fresh ones.
    raw_notes = merged.get("notes", []) or []
    kept_notes = []
    for n in raw_notes:
        s = str(n).strip()
        if s.startswith("[") and "]" in s:
            # Pipeline-marker note (e.g. "[Validation]", "[Schedule Override]",
            # "[Perimeter Cross-Check]"). Drop — will be regenerated.
            continue
        if s:
            kept_notes.append(s)
    # Also keep delta's LLM notes that aren't bracketed pipeline markers.
    for n in delta_analysis.get("notes", []) or []:
        s = str(n).strip()
        if s and not (s.startswith("[") and "]" in s) and s not in kept_notes:
            kept_notes.append(s)
    merged["notes"] = kept_notes

    # ── Drop fields recomputed downstream ─────────────────────────────────
    for field in (
        "aggregated_totals", "_perimeter_cross_check", "provenance_audit",
        "manual_review_required", "manual_review_reason",
    ):
        merged.pop(field, None)

    return merged


def _union_floor_rooms(prior_floor, delta_floor):
    """Union the rooms of two floors (same floor_name) by room_name.
    Delta wins on collisions. Returns a new floor dict."""
    import copy as _copy

    out = _copy.deepcopy(prior_floor)
    prior_rooms = out.get("rooms", []) or []
    delta_rooms = (delta_floor.get("rooms") or [])

    def _room_key(r):
        rn = str((r or {}).get("room_name", "")).strip().lower()
        rid = str((r or {}).get("room_id", "")).strip()
        # Prefer room_id if available; fall back to name.
        return rid or rn

    prior_index = {}
    for i, r in enumerate(prior_rooms):
        k = _room_key(r)
        if k:
            prior_index[k] = i

    merged_rooms = list(prior_rooms)
    for d_room in delta_rooms:
        k = _room_key(d_room)
        if k and k in prior_index:
            # Collision: delta wins (revised dimensions/elements override).
            merged_rooms[prior_index[k]] = _copy.deepcopy(d_room)
        else:
            merged_rooms.append(_copy.deepcopy(d_room))

    out["rooms"] = merged_rooms
    return out


def _max_merge_exterior(prior_ext, delta_ext):
    """Merge two exterior dicts by taking the MAX of numeric fields, OR of
    booleans, and appending notes. Strings on prior are kept unless empty."""
    import copy as _copy

    out = _copy.deepcopy(prior_ext)

    numeric_fields = (
        "cornice_lf", "window_trim_lf", "soffit_sqft", "railing_lf",
        "exterior_paint_sqft", "hardie_siding_sqft", "azek_trim_lf",
        "corner_board_lf", "steel_lintel_lf",
        "stain_siding_sqft", "stain_trim_lf", "stain_railing_lf",
    )
    for field in numeric_fields:
        try:
            p_val = float(out.get(field, 0) or 0)
            d_val = float(delta_ext.get(field, 0) or 0)
        except (TypeError, ValueError):
            continue
        if d_val > p_val:
            out[field] = d_val if d_val != int(d_val) else int(d_val)

    # Booleans — OR
    for bfield in ("lift_required", "interior_lift_required"):
        if delta_ext.get(bfield):
            out[bfield] = True

    # Strings — keep prior unless empty
    for sfield in ("exterior_siding_type",):
        if not out.get(sfield) and delta_ext.get(sfield):
            out[sfield] = delta_ext[sfield]

    # Notes — append
    p_notes = str(out.get("notes", "") or "")
    d_notes = str(delta_ext.get("notes", "") or "")
    if d_notes and d_notes not in p_notes:
        out["notes"] = (p_notes + " | " if p_notes else "") + d_notes

    return out


def run_analysis_merge(prior_json, new_pdf_paths, scope_tags=None,
                        contact_name="", contact_email="", scope_notes="",
                        sheet_hint=None,
                        rate_overrides=None, version=None, parent_id=None,
                        pre_skipped_files=None):
    """Re-run analysis using a prior result JSON as the baseline, merging
    in extraction from `new_pdf_paths` only.

    Pricing uses `prior_json['pricing_model']` snapshot — quotes stay rate-
    consistent across versions even if PRICING_MODEL changes upstream.

    Phase 2 implementation: orchestrator wired around merge_versioned_analyses().
    Per-PDF extraction reuses run_analysis() on the new files (some compute
    is wasted on the delta's own cost/Will pass which we discard, but it
    keeps extraction logic in one place — Phase 2.1 can optimize).
    """
    if not isinstance(prior_json, dict):
        raise TypeError("prior_json must be a dict (the parent's stored result)")

    prior_analysis = prior_json.get("analysis") or {}
    if not prior_analysis:
        raise ValueError("prior_json has no 'analysis' field — not a valid parent result")

    import copy as _copy

    if not new_pdf_paths:
        # Notes-only re-run: NO architectural re-extraction (slow + burns API
        # budget). We keep the prior takeoff as the baseline and let the cheap
        # downstream passes (re-pricing, RFIs, Will review) re-run, with the
        # reviewer's notes steering Will. New-file re-runs still extract the
        # delta below.
        print("\n" + "=" * 80)
        print(f"🔄 NIGHTSHIFT AI — RE-RUN (notes only, no re-extraction)")
        print("=" * 80)
        print(f"   Parent doc:    {prior_json.get('document', '?')}")
        print(f"   Notes:         {scope_notes or '(none)'}")
        print(f"   Sheet hint:    {sheet_hint or '(none)'}")
        print(f"   Pricing model: parent snapshot ({len(prior_json.get('pricing_model', {}))} items)")
        print("=" * 80)

        delta_result = {"analysis": {}}
        delta_analysis = {}
        merged_analysis = _copy.deepcopy(prior_analysis)
    else:
        print("\n" + "=" * 80)
        print(f"🔄 NIGHTSHIFT AI — INCREMENTAL RE-RUN (merge)")
        print("=" * 80)
        print(f"   Parent doc:    {prior_json.get('document', '?')}")
        print(f"   New files:     {[os.path.basename(p) for p in new_pdf_paths]}")
        print(f"   Scope tags:    {scope_tags or '(none — additive only)'}")
        print(f"   Pricing model: parent snapshot ({len(prior_json.get('pricing_model', {}))} items)")
        print("=" * 80)

        # 1. Run extraction on the new PDFs only. We reuse run_analysis here for
        #    per-PDF extraction + post-extraction passes; the result's analysis
        #    dict is what we merge with the prior. cost_estimate and will_synthesis
        #    on the delta are discarded — we'll recompute on the merged whole.
        # Re-run targeting: narrow the new PDFs to specific sheets if requested.
        if sheet_hint:
            new_pdf_paths, _ = _filter_pdfs_to_sheets(new_pdf_paths, sheet_hint)

        delta_result = run_analysis(
            new_pdf_paths,
            contact_name=contact_name,
            contact_email=contact_email,
            scope_notes=scope_notes,
            rate_overrides=rate_overrides,
            pre_skipped_files=pre_skipped_files,
        )
        delta_analysis = delta_result.get("analysis") or {}

        # 2. Pure merge.
        merged_analysis = merge_versioned_analyses(prior_analysis, delta_analysis,
                                         scope_tags=scope_tags)

    # 3. Re-run post-extraction passes on the merged whole. These mutate
    #    in place and re-derive aggregated_totals + validation notes.
    #
    #    SKIP for a notes-only re-run: the prior analysis is already fully
    #    processed (supplements, boosts and schedule overrides are baked into
    #    its aggregated_totals). Re-running these passes on already-supplemented
    #    data re-applies the supplements — observed as a ~18% phantom inflation
    #    on a re-run that changed nothing. With no new rooms to integrate there
    #    is nothing for them to do, so we keep the prior totals verbatim and let
    #    pricing + Will run on top.
    if new_pdf_paths:
        print("\n🧮 Re-running post-extraction passes on merged data...")
        try:
            merged_analysis = _recalculate_totals(merged_analysis)
            merged_analysis = _apply_schedule_overrides(merged_analysis)
            merged_analysis = _supplement_missing_secondary_spaces(merged_analysis)
            merged_analysis = _validate_wall_area_by_perimeter(merged_analysis)
            merged_analysis = _validate_and_boost_walls(merged_analysis)
            merged_analysis = _apply_commercial_window_exclusion(merged_analysis)
            merged_analysis = _check_wall_ceiling_ratio(merged_analysis)
        except Exception as exc:
            print(f"⚠️  Post-extraction pass failed: {exc}")
            raise
    else:
        print("\n🧮 Notes-only re-run — keeping prior takeoff totals "
              "(skipping re-extraction passes to avoid double-counting).")

    merged_analysis = _normalize_analysis(merged_analysis)

    # 4. Cost.
    pricing_snapshot = prior_json.get("pricing_model") or PRICING_MODEL
    if new_pdf_paths:
        # Recompute cost using parent's pricing snapshot — NOT the live
        # PRICING_MODEL. This is the rate-stability rule: a v2 quote uses the
        # same rates v1 was priced at, so deltas are pure-quantity changes.
        print(f"💰 Re-pricing with parent snapshot...")
        costs = calculate_costs(
            merged_analysis.get("aggregated_totals", {}),
            exterior=merged_analysis.get("exterior", {}),
            building_type=merged_analysis.get("project_info", {}).get("building_type", ""),
            project_info=merged_analysis.get("project_info", {}),
            analysis=merged_analysis,
            pricing_model_override=pricing_snapshot,
        )
    else:
        # Notes-only re-run: the prior cost estimate IS the baseline. Pricing
        # from raw aggregated_totals would discard the prior run's Will/manual
        # adjustments and hand back the pre-adjustment base (observed ~+18%).
        # Instead we carry the prior estimate forward verbatim and let Will
        # adjust it from there — so a note that changes nothing leaves the
        # subtotal exactly where it was.
        print(f"💰 Carrying prior cost estimate forward (notes-only re-run)...")
        costs = _copy.deepcopy(prior_json.get("cost_estimate") or {})

    print_estimate(merged_analysis, costs)

    # 5. Re-run validation and RFIs on merged data. RFIs regenerate from
    #    current state, so any RFI Matt resolved by uploading a new
    #    schedule simply doesn't re-fire.
    # Carry a finish schedule the prior run already detected, then re-scan the
    # newly uploaded files so an added finish schedule clears the RFI.
    _prior_an_fs = prior_json.get("analysis") or {}
    if _prior_an_fs.get("has_finish_schedule") or _prior_an_fs.get("room_finish_schedule"):
        merged_analysis["has_finish_schedule"] = True
    _set_finish_schedule_flag(merged_analysis, new_pdf_paths)
    _set_door_schedule_flag(merged_analysis, new_pdf_paths)
    _set_window_schedule_flag(merged_analysis, new_pdf_paths)

    # Upload sheet inventory: prior run's sheets plus the newly uploaded files.
    _prior_sheets = set(_prior_an_fs.get("_upload_sheet_numbers") or [])
    merged_analysis["_upload_sheet_numbers"] = sorted(
        _prior_sheets | _collect_upload_sheet_numbers(new_pdf_paths))

    validation = _validate_cost_estimate(merged_analysis, costs)
    rfi_items = generate_rfi_items(merged_analysis) or []

    pre_pricing_rfis = merged_analysis.pop("_pre_pricing_rfis", []) or []
    if pre_pricing_rfis:
        next_num = max((r.get("number", 0) for r in rfi_items), default=0) + 1
        for r in pre_pricing_rfis:
            r["number"] = next_num
            next_num += 1
        rfi_items = rfi_items + pre_pricing_rfis

    # 6. Will synthesis. Will's senior-estimator pass proposes ±25% line-item
    # adjustments whose upward edits were padding re-run quotes with unwanted
    # "inflation," so it is DISABLED on re-runs by default — re-runs carry the
    # prior/merged estimate forward verbatim. Fresh first-time submissions
    # (run_analysis) still run Will. Set NIGHTSHIFT_WILL_ON_RERUN=1 to restore.
    will_result = {"will_synthesis": None, "adjustments_log": [], "rejected_log": [],
                   "new_rfis": [], "error": None}
    if os.environ.get("NIGHTSHIFT_WILL_ON_RERUN", "0").strip() != "1":
        print("\n🧑‍💼 Will synthesis SKIPPED on re-run "
              "(NIGHTSHIFT_WILL_ON_RERUN!=1) — carrying the prior estimate "
              "forward with no senior-estimator adjustments.")
    else:
        # Surface the reviewer's notes to Will (reads project_info._scope_notes).
        # Fold the sheet hint in too — without new files there's nothing to
        # filter, so it only makes sense as guidance. Override only when given.
        _reviewer_notes = (scope_notes or "").strip()
        if not new_pdf_paths and sheet_hint:
            _focus = f"Focus on sheet(s): {sheet_hint}."
            _reviewer_notes = (f"{_reviewer_notes}\n\n{_focus}".strip()
                               if _reviewer_notes else _focus)
        if _reviewer_notes:
            _pi = merged_analysis.setdefault("project_info", {})
            if isinstance(_pi, dict):
                _pi["_scope_notes"] = _reviewer_notes

        print("\n🧑‍💼 Re-running Will Synthesis on merged data...")
        try:
            from anthropic import Anthropic
            will_client = Anthropic(api_key=CLAUDE_API_KEY)
        except Exception as _exc:
            will_client = None
            print(f"⚠️  Could not init Anthropic client for Will: {_exc}")

        if will_client is not None:
            try:
                will_result = run_will_synthesis(
                    analysis=merged_analysis,
                    cost_estimate=costs,
                    rfi_items=rfi_items,
                    validation=validation,
                    client=will_client,
                )
            except Exception as exc:
                print(f"⚠️  Will Synthesis failed: {exc}")
                will_result = {"will_synthesis": None, "adjustments_log": [],
                               "rejected_log": [], "new_rfis": [], "error": str(exc)}

        if will_result.get("will_synthesis"):
            will_rfis = will_result.get("new_rfis", []) or []
            next_num = (max((r.get("number", 0) for r in rfi_items), default=0) + 1
                        if rfi_items else 1)
            for rfi in will_rfis:
                rfi["number"] = next_num
                next_num += 1
            rfi_items = rfi_items + will_rfis
            validation = _validate_cost_estimate(merged_analysis, costs)

    # 7. Append to merge_log so audit history is queryable from the JSON.
    prior_log = list(prior_json.get("merge_log") or [])
    subtotal_before = (prior_json.get("cost_estimate") or {}).get("subtotal", 0) or 0
    subtotal_after = (costs or {}).get("subtotal", 0) or 0
    prior_log.append({
        "version": version,
        "parent_id": parent_id,
        "files_added": [os.path.basename(p) for p in new_pdf_paths],
        "scope_tags": list(scope_tags or []),
        "scope_notes": scope_notes or "",
        "merged_at": datetime.now().isoformat(),
        "subtotal_before": float(subtotal_before),
        "subtotal_after": float(subtotal_after),
        "subtotal_delta": round(float(subtotal_after) - float(subtotal_before), 2),
    })

    # 8. Source-file tracking: combine prior + delta document references.
    prior_doc = prior_json.get("document", "")
    new_doc_refs = ", ".join(os.path.basename(p) for p in new_pdf_paths)
    document_ref = (prior_doc + " | " + new_doc_refs) if prior_doc else new_doc_refs

    prior_sources = list(prior_json.get("source_files") or []) or (
        [prior_doc] if prior_doc and "|" not in prior_doc else []
    )
    new_sources = [os.path.basename(p) for p in new_pdf_paths]
    combined_sources = list(prior_sources)
    for s in new_sources:
        if s not in combined_sources:
            combined_sources.append(s)

    # 9. Save JSON + PDF.
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_json = os.path.join(output_dir, f"construction_analysis_{timestamp}.json")

    result_data = {
        "contact": {"name": contact_name, "email": contact_email},
        "document": document_ref,
        "source_files": combined_sources,
        "files_analyzed": delta_result.get("analysis", {}).get("files_analyzed"),
        "generated": datetime.now().isoformat(),
        "scope_notes": scope_notes if scope_notes else prior_json.get("scope_notes"),
        "building_inventory": _merge_building_inventory(
            prior_json.get("building_inventory"),
            delta_result.get("analysis", {}).get("_building_inventory")
                or delta_analysis.get("_building_inventory")
                or None,
        ),
        "manual_review_required": bool(merged_analysis.get("manual_review_required")),
        "manual_review_reason": merged_analysis.get("manual_review_reason"),
        "analysis": merged_analysis,
        "cost_estimate": costs,
        "labor_hours_estimate": _compute_labor_hours(merged_analysis),
        "validation": validation,
        "pricing_model": pricing_snapshot,
        "rfi_items": rfi_items if rfi_items else None,
        "will_synthesis": will_result.get("will_synthesis"),
        "will_adjustments_log": will_result.get("adjustments_log"),
        "will_rejected_log": will_result.get("rejected_log"),
        "merge_log": prior_log,
        "is_merge_result": True,
        "parent_submission_id": parent_id,
        "version": version,
    }

    with open(output_json, "w") as f:
        json.dump(result_data, f, indent=2)
    print(f"\n📁 Merged JSON saved to: {output_json}")

    # PDF report
    output_pdf = output_json.replace(".json", ".pdf")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from json_to_pdf import json_to_pdf as generate_pdf_report
        generate_pdf_report(output_json, output_pdf)
        print(f"📄 Merged PDF report saved to: {output_pdf}")
    except Exception as e:
        print(f"⚠️  Could not generate merged PDF report: {e}")
        output_pdf = None

    print(f"\n✅ MERGE COMPLETE")
    print(f"   Subtotal: ${subtotal_before:,.2f} → ${subtotal_after:,.2f} "
          f"(Δ ${subtotal_after - subtotal_before:+,.2f})")

    return {
        "analysis": merged_analysis,
        "cost_estimate": costs,
        "output_json_path": output_json,
        "output_pdf_path": output_pdf,
        "contact": {"name": contact_name, "email": contact_email},
        "document": document_ref,
        "rfi_items": rfi_items,
        "will_synthesis": will_result.get("will_synthesis"),
        "merge_log": prior_log,
        "is_merge_result": True,
        "parent_submission_id": parent_id,
        "version": version,
    }


def _merge_building_inventory(prior_inv, delta_inv):
    """Union two building_inventory dicts. New buildings/units appended;
    name conflicts → keep highest count."""
    import copy as _copy
    if not prior_inv:
        return _copy.deepcopy(delta_inv) if delta_inv else None
    if not delta_inv:
        return _copy.deepcopy(prior_inv)

    out = _copy.deepcopy(prior_inv)
    p_buildings = list(out.get("buildings") or [])
    d_buildings = list(delta_inv.get("buildings") or [])

    by_key = {}
    for i, b in enumerate(p_buildings):
        if isinstance(b, dict):
            key = (str(b.get("building_type_code") or "").strip().upper(),
                   str(b.get("building_name") or "").strip().lower())
            by_key[key] = i

    for b in d_buildings:
        if not isinstance(b, dict):
            continue
        key = (str(b.get("building_type_code") or "").strip().upper(),
               str(b.get("building_name") or "").strip().lower())
        if key in by_key:
            # Keep max count when same building appears in both.
            existing = p_buildings[by_key[key]]
            try:
                e_ct = int(existing.get("count", 0) or 0)
                d_ct = int(b.get("count", 0) or 0)
                if d_ct > e_ct:
                    existing["count"] = d_ct
            except (TypeError, ValueError):
                pass
        else:
            p_buildings.append(_copy.deepcopy(b))
            by_key[key] = len(p_buildings) - 1

    out["buildings"] = p_buildings
    out["total_buildings"] = sum(int(b.get("count", 0) or 0)
                                  for b in p_buildings if isinstance(b, dict))
    out["total_units"] = sum(
        int(_num(b.get("count", 0))) * int(_num(b.get("units_per_building", 1)))
        for b in p_buildings if isinstance(b, dict)
    )

    # Append source pages
    p_pages = list(out.get("source_pages") or [])
    for pg in (delta_inv.get("source_pages") or []):
        if pg not in p_pages:
            p_pages.append(pg)
    out["source_pages"] = p_pages

    return out




def run_analysis(pdf_paths, contact_name="", contact_email="", scope_notes="",
                  corrections_path=None, use_cache=False, multi_pass=False,
                  image_fallback=True, schedule_estimation=True,
                  rate_overrides=None, interactive=False,
                  pre_skipped_files=None):
    """
    Programmatic entry point for the analysis pipeline.
    Called by email_processor.py (or any external caller).

    Args:
        pdf_paths: list of absolute paths to PDF files
        contact_name: name of the contact (e.g. email sender)
        contact_email: email address of the contact
        scope_notes: free-form text describing painting scope (e.g.
                     "Residential floors 2-4 only, skip basement")
        corrections_path: optional path to a corrections.json file with
                          room-level overrides and global corrections
        use_cache: if True (default), use cached results when available

    Returns:
        dict with keys:
            analysis       - the merged/recalculated analysis dict
            cost_estimate  - line items + subtotal from calculate_costs()
            output_json_path - path to saved JSON
            output_pdf_path  - path to generated PDF report
    Raises:
        ValueError: if no PDFs provided or none could be analysed
        anthropic.RateLimitError: if API rate-limited
    """
    if not pdf_paths:
        raise ValueError("No PDF paths provided")

    # --- Pre-flight checks ---
    # Verify PDF files exist and are readable
    for p in pdf_paths:
        if not os.path.isfile(p):
            print(f"❌ PDF file not found: {p}")
        else:
            size_mb = os.path.getsize(p) / (1024 * 1024)
            print(f"   ✓ {os.path.basename(p)} ({size_mb:.1f} MB)")

    # Verify API key is set
    if not CLAUDE_API_KEY:
        raise ValueError(
            "CLAUDE_API_KEY not set. Set the CLAUDE_API_KEY or ANTHROPIC_API_KEY "
            "environment variable in your Render service settings."
        )

    multi_mode = len(pdf_paths) > 1
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    # --- Cache lookup: build a combined hash for all PDFs ---
    # For single-PDF jobs, cache by that PDF's hash.
    # For multi-PDF jobs, cache by hash of all PDF hashes combined.
    pdf_hashes = {}
    cache_dirs = {}
    combined_cache_dir = None
    if use_cache:
        for p in pdf_paths:
            cd, ph = _cache_dir_for(p)
            pdf_hashes[p] = ph
            cache_dirs[p] = cd

        if multi_mode:
            # Combined hash for multi-file jobs
            combined_h = hashlib.sha256()
            for p in sorted(pdf_paths):
                combined_h.update(pdf_hashes[p].encode())
            combined_cache_dir = CACHE_DIR / f"multi_{combined_h.hexdigest()}"
        else:
            combined_cache_dir = cache_dirs[pdf_paths[0]]

        # Check for fully cached final result
        if _cache_valid(combined_cache_dir):
            cached_analysis = _load_cache(combined_cache_dir, "final_result.json")
            if cached_analysis:
                print("🎨 NIGHTSHIFT AI - CONSTRUCTION DOCUMENT ANALYZER")
                print("=" * 80)
                print(f"📄 Processing: {os.path.basename(pdf_paths[0])}")
                print("=" * 80)
                print(f"\n✅ Using cached analysis (instant, deterministic)")
                print(f"   Cache: {combined_cache_dir}")

                # Jump straight to cost calculation with cached analysis
                analysis = cached_analysis
                analysis = _normalize_analysis(analysis)

                # Apply corrections to cached analysis (if any)
                corrections = _load_corrections(corrections_path)
                if corrections:
                    analysis = _apply_corrections(analysis, corrections)
                    analysis = _recalculate_totals(analysis)

                # Apply pre-run rate overrides (cached path)
                pricing_model_used = None
                if rate_overrides:
                    pricing_model_used = _apply_rate_overrides(rate_overrides)
                    print(f"\n📊 Rate overrides applied: {', '.join(rate_overrides.keys())}")

                # Re-run cost calculation (uses current pricing from config.py)
                print("\n💰 Calculating costs...")
                costs = calculate_costs(
                    analysis.get('aggregated_totals', {}),
                    exterior=analysis.get('exterior', {}),
                    building_type=analysis.get('project_info', {}).get('building_type', ''),
                    project_info=analysis.get('project_info', {}),
                    analysis=analysis,
                    pricing_model_override=pricing_model_used,
                )
                print_estimate(analysis, costs)

                # Interactive adjustment mode (cached path)
                adjustments_log = []
                if interactive:
                    print("\n🔧 Entering interactive adjustment mode...")
                    analysis, costs, pricing_model_used, adjustments_log, _ = \
                        interactive_adjustments(analysis, costs, pricing_model_used)
                    if adjustments_log:
                        analysis.setdefault("notes", []).append(
                            f"[Adjustments] {len(adjustments_log)} manual adjustment(s) applied"
                        )

                validation = _validate_cost_estimate(analysis, costs)
                if validation["warnings"]:
                    print(f"\n⚠️  VALIDATION: {validation['warning_count']} warning(s) "
                          f"(quality score: {validation['data_quality_score']}/100)")
                    for w in validation["warnings"]:
                        sev = w["severity"].upper()
                        print(f"   {sev}: {w['message']}")

                rfi_items = generate_rfi_items(analysis)
                if rfi_items:
                    print(f"\n📋 RFI: {len(rfi_items)} items requiring clarification")
                    for rfi in rfi_items:
                        q_preview = rfi['question'][:80] + ('...' if len(rfi['question']) > 80 else '')
                        print(f"   {rfi['number']}. [{rfi['category']}] {q_preview}")

                # Save output files
                output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
                os.makedirs(output_dir, exist_ok=True)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                output_json = os.path.join(output_dir, f"construction_analysis_{timestamp}.json")
                document_ref = ", ".join(os.path.basename(p) for p in pdf_paths)
                chunk_tracking = analysis.pop("_chunk_tracking", None)
                result_data = {
                    "contact": {"name": contact_name, "email": contact_email},
                    "document": document_ref,
                    "source_files": [os.path.basename(p) for p in pdf_paths] if multi_mode else None,
                    "files_analyzed": None,
                    "files_skipped": None,
                    "chunk_tracking": chunk_tracking,
                    "generated": datetime.now().isoformat(),
                    "scope_notes": scope_notes if scope_notes else None,
                    "cached": True,
                    "analysis": analysis,
                    "cost_estimate": costs,
                    "validation": validation,
                    "pricing_model": pricing_model_used if pricing_model_used else PRICING_MODEL,
                    "adjustments_applied": adjustments_log if adjustments_log else None,
                    "rfi_items": rfi_items if rfi_items else None,
                }
                with open(output_json, 'w') as f:
                    json.dump(result_data, f, indent=2)
                print(f"\n📁 JSON saved to: {output_json}")

                output_pdf = output_json.replace('.json', '.pdf')
                try:
                    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                    from json_to_pdf import json_to_pdf as generate_pdf_report
                    generate_pdf_report(output_json, output_pdf)
                    print(f"📄 PDF report saved to: {output_pdf}")
                except Exception as e:
                    print(f"⚠️  Could not generate PDF report: {e}")
                    output_pdf = None

                print(f"\n✅ ESTIMATE COMPLETE! (from cache)")
                return {
                    "analysis": analysis,
                    "cost_estimate": costs,
                    "output_json_path": output_json,
                    "output_pdf_path": output_pdf,
                    "contact": {"name": contact_name, "email": contact_email},
                    "document": document_ref,
                    "rfi_items": rfi_items,
                }

    # --- Setup progress tracking ---
    global _PROGRESS_FILE
    progress_path = os.environ.get("NIGHTSHIFT_PROGRESS_FILE")
    if progress_path:
        _PROGRESS_FILE = progress_path
    TOTAL_STEPS = 8  # schedule scan, inventory, extraction, merge, recalc, costs, json, pdf

    print("🎨 NIGHTSHIFT AI - CONSTRUCTION DOCUMENT ANALYZER")
    print("=" * 80)
    if multi_mode:
        print(f"📂 Processing {len(pdf_paths)} PDFs...")
        for i, p in enumerate(pdf_paths, 1):
            size_mb = os.path.getsize(p) / (1024 * 1024)
            print(f"   {i}. {os.path.basename(p)} ({size_mb:.1f} MB)")
    else:
        print(f"📄 Processing: {os.path.basename(pdf_paths[0])}")
    print("=" * 80)

    if scope_notes:
        print(f"📋 Scope Notes: {scope_notes}")

    _update_progress(0, TOTAL_STEPS, "Initializing", f"Loading {len(pdf_paths)} PDF(s)...")

    # --- Pre-scan for schedule pages (fast, no API cost) ---
    image_schedule_data = None
    try:
        enable_img_sched = True
        try:
            from config import ENABLE_IMAGE_SCHEDULE_EXTRACTION
            enable_img_sched = ENABLE_IMAGE_SCHEDULE_EXTRACTION
        except ImportError:
            pass

        _update_progress(1, TOTAL_STEPS, "Scanning Schedules", "Detecting door & window schedules...")
        if enable_img_sched:
            for pdf_path_scan in pdf_paths:
                fname = os.path.basename(pdf_path_scan)

                # Check schedule cache first
                sched_cache_dir = cache_dirs.get(pdf_path_scan) if use_cache else None
                if sched_cache_dir and _cache_valid(sched_cache_dir):
                    cached_sched = _load_cache(sched_cache_dir, "schedule_extraction.json")
                    if cached_sched:
                        print(f"\n✅ Using cached schedule extraction for {fname}")
                        image_schedule_data = cached_sched
                        break

                print(f"\n🔎 Pre-scanning {fname} for schedule pages...")
                sched_info = _identify_schedule_pages(pdf_path_scan)

                door_pages = sched_info.get("door_schedule_pages", [])
                win_pages = sched_info.get("window_schedule_pages", [])
                all_sched_pages = sorted(set(door_pages + win_pages))

                if all_sched_pages:
                    print(f"   Found schedule pages: door={door_pages}, window={win_pages}")
                    image_schedule_data = analyze_schedule_images(
                        client, pdf_path_scan, all_sched_pages
                    )
                    if image_schedule_data:
                        # Cache the schedule extraction
                        if sched_cache_dir:
                            _init_cache(sched_cache_dir, pdf_path_scan,
                                        pdf_hashes.get(pdf_path_scan, ""))
                            _save_cache(sched_cache_dir, "schedule_extraction.json",
                                        image_schedule_data)
                        break  # found schedules, no need to scan more files
                    else:
                        print(f"   ⚠️  Image schedule extraction returned no data for {fname}")
                else:
                    print(f"   No schedule pages found in {fname} via text scan")
    except Exception as e:
        print(f"   ⚠️  Schedule pre-scan failed: {e}")
        image_schedule_data = None

    # --- Pre-extract the Room Finish Schedule ---
    # Drives wallcovering + per-room wall/ceiling/base finishes. Gated by a
    # zero-cost text scan so the LLM call only runs when a finish schedule is
    # actually present; the result is injected into the extraction prompt.
    room_finish_schedule = None
    try:
        for pdf_path_scan in pdf_paths:
            if _detect_finish_schedule(pdf_path_scan):
                rfs_pre = _extract_room_finish_schedule(client, pdf_path_scan)
                if rfs_pre and rfs_pre.get("room_finish_schedule"):
                    room_finish_schedule = rfs_pre["room_finish_schedule"]
                    print(f"   📋 Room finish schedule pre-extracted: "
                          f"{len(room_finish_schedule)} rooms — injecting into extraction")
                    break
    except Exception as e:
        print(f"   ⚠️  Finish schedule pre-extraction failed: {e}")
        room_finish_schedule = None

    if room_finish_schedule:
        if image_schedule_data is None:
            image_schedule_data = {}
        image_schedule_data["room_finish_schedule"] = room_finish_schedule

    # --- Pre-scan for building inventory from index pages ---
    _update_progress(2, TOTAL_STEPS, "Building Inventory", "Scanning index pages for building data...")
    building_inventory = None
    # Persisted across the building-inventory loop so the partial-extraction
    # detector below can compare detected-vs-extracted architectural sheets
    # and notice silent inventory-call failures.
    _index_info_per_pdf = []
    try:
        enable_inv_scan = True
        try:
            from config import ENABLE_BUILDING_INVENTORY_SCAN
            enable_inv_scan = ENABLE_BUILDING_INVENTORY_SCAN
        except ImportError:
            pass

        if enable_inv_scan:
            # Scan ALL PDFs for index pages — collect inventories from each
            # and merge them (different volumes contain different building types)
            all_inventories = []
            for pdf_path_scan in pdf_paths:
                index_info = _detect_index_pages(pdf_path_scan)
                if index_info:
                    _index_info_per_pdf.append({
                        "pdf": os.path.basename(pdf_path_scan),
                        "has_building_list": bool(index_info.get("has_building_list")),
                        "index_text": index_info.get("index_text", ""),
                    })
                if index_info and index_info.get("has_building_list"):
                    print(f"\n📑 Index pages detected in {os.path.basename(pdf_path_scan)}: "
                          f"pages {[p + 1 for p in index_info['index_pages']]}")
                    print(f"   Building keywords: {', '.join(index_info['building_keywords_found'])}")
                    inv = _extract_building_inventory(
                        client, pdf_path_scan, index_info["index_pages"],
                        index_text=index_info.get("index_text", "")
                    )
                    if inv and inv.get("buildings"):
                        inv_total = inv.get("total_buildings", 0)
                        print(f"   🏗️  Building inventory: {inv_total} "
                              f"buildings detected from index")
                        all_inventories.append(inv)
                    else:
                        print(f"   ⚠️  Could not extract building inventory from "
                              f"{os.path.basename(pdf_path_scan)}")

            # Merge inventories from all PDFs (dedup by type code, keep highest count)
            if len(all_inventories) > 1:
                building_inventory = _merge_building_inventories(all_inventories)
            elif len(all_inventories) == 1:
                building_inventory = all_inventories[0]
                print(f"\n🏗️  Building inventory: "
                      f"{building_inventory.get('total_buildings', 0)} buildings "
                      f"(from {building_inventory.get('source_pdf', '?')})")
    except Exception as e:
        print(f"   ⚠️  Building inventory pre-scan failed: {e}")
        building_inventory = None

    # --- Phase 1: Project Overview from G-series / coversheets ---
    # Reads General Notes pages FIRST (mirrors Rider's manual workflow) to
    # establish project scope and scale before any measurement work.
    project_overview = None
    try:
        enable_overview = True
        try:
            from config import ENABLE_PROJECT_OVERVIEW_SCAN
            enable_overview = ENABLE_PROJECT_OVERVIEW_SCAN
        except ImportError:
            pass
        if enable_overview:
            project_overview = _extract_project_overview(client, pdf_paths)
    except Exception as e:
        print(f"   ⚠️  Phase 1 project overview scan failed: {e}")
        project_overview = None

    # --- Analyse each PDF ---
    all_results = []
    files_analyzed = []
    # Seed with files the caller already had to exclude (e.g. password-
    # locked PDFs in a mixed upload) so they hit the same manual-review +
    # RFI surfacing as files that fail during analysis. Previously a
    # partially locked upload was silently priced without the locked file.
    files_skipped = list(pre_skipped_files or [])
    file_room_counts = {}  # Track per-file room counts for extraction validation
    image_fallback_files = []  # Track which files used image fallback
    schedule_estimated_files = []  # Track which files used schedule-based estimation
    FILE_RETRY_COOLDOWN = 120  # seconds to wait before retrying a failed file

    # Parse building counts from filenames (e.g., "BLDG 1-3" → 3 buildings)
    file_building_counts = {}
    for pdf_path in pdf_paths:
        count, ids = _parse_building_count_from_filename(os.path.basename(pdf_path))
        file_building_counts[pdf_path] = count
        if count > 1:
            ids_str = ", ".join(str(x) for x in ids)
            print(f"   🏗️  Detected {count} buildings from filename: "
                  f"{os.path.basename(pdf_path)} (buildings {ids_str})")

    for i, pdf_path in enumerate(pdf_paths, 1):
        filename = os.path.basename(pdf_path)
        is_fp = _is_floor_plan_file(filename)
        _update_progress(3, TOTAL_STEPS, "Extracting Rooms",
                         f"Analyzing PDF {i}/{len(pdf_paths)}: {filename}",
                         pct=round(25 + (i / len(pdf_paths)) * 35))
        if multi_mode:
            print(f"\n{'─'*80}")
            fp_tag = " [Floor Plan]" if is_fp else ""
            print(f"📄 FILE {i}/{len(pdf_paths)}: {filename}{fp_tag}")
            print(f"{'─'*80}")

        # Try up to 3 attempts per file (original + 2 retries after cooldown)
        # Track the best result across attempts (most rooms = most complete extraction)
        result = None
        best_result = None
        best_rooms = 0
        for file_attempt in range(3):
            if file_attempt > 0:
                print(f"\n   🔄 Retrying {filename} (attempt {file_attempt+1}/3) after {FILE_RETRY_COOLDOWN}s cooldown...")
                time.sleep(FILE_RETRY_COOLDOWN)
            result = analyze_and_parse(client, pdf_path, scope_notes=scope_notes,
                                          schedule_hints=image_schedule_data,
                                          building_inventory=building_inventory,
                                          project_overview=project_overview)
            if result:
                _, analysis_check = result
                rooms_found_raw = analysis_check.get('project_info', {}).get('total_rooms_found', 0)
                try:
                    rooms_found = int(rooms_found_raw) if rooms_found_raw is not None else 0
                except (ValueError, TypeError):
                    rooms_found = 0
                has_incomplete = analysis_check.get('incomplete_analysis_reason')

                # Track the best result (most rooms extracted)
                if rooms_found > best_rooms:
                    best_result = result
                    best_rooms = rooms_found

                # --- Retry decision ---
                should_retry = False

                # Case 1 (existing): 0 rooms + incomplete flag
                if rooms_found == 0 and has_incomplete:
                    should_retry = True
                    print(f"   ⚠️  0 rooms extracted (incomplete analysis) — will retry")

                # Case 2 (NEW): 0 rooms from a floor plan file, even without
                # incomplete flag. The LLM sometimes returns valid JSON with 0 rooms
                # and notes like "resolution insufficient" but no incomplete reason.
                elif rooms_found == 0 and is_fp and not has_incomplete:
                    should_retry = True
                    print(f"   ⚠️  0 rooms from floor plan file — will retry "
                          f"(non-deterministic extraction)")

                if should_retry and file_attempt < 2:
                    result = None
                    continue
                break  # success (or final attempt)

        # Use the best result across all attempts
        if best_result and best_rooms > 0 and best_result is not result:
            result = best_result
            print(f"   📊 Using best extraction: {best_rooms} rooms (from earlier attempt)")
        elif best_result and result is None:
            result = best_result  # all attempts returned results but last was None

        # ── Enhanced extraction for large-format floor plans (text-layer + tiling) ──
        # Note: fires regardless of is_fp — combined-volume PDFs (e.g. "Vol. II.pdf")
        # may not match floor-plan filename patterns, but large-format pages + 0 rooms
        # is a strong enough signal to try enhanced extraction.
        #
        # ALSO triggers when rooms were found but ALL have 0 wall area — this means
        # Claude could identify room names but couldn't read dimension text (the core
        # DD-scale resolution problem).
        used_enhanced = False
        attempted_enhanced = False  # True once the enhanced-extraction block ran
        rooms_have_zero_dims = False
        if best_result and best_rooms > 0:
            _, _check_analysis = best_result
            _all_rooms = []
            for _fl in _check_analysis.get("floors", []):
                _all_rooms.extend(_fl.get("rooms", []))
            if _all_rooms:
                _nonzero_walls = sum(1 for r in _all_rooms
                                     if (r.get("wall_area_sqft") or 0) > 0)
                if _nonzero_walls == 0:
                    rooms_have_zero_dims = True
                    print(f"   ⚠️  All {len(_all_rooms)} rooms have 0 wall area — "
                          f"dimension text likely unreadable")

        # Detect "templates instead of physical floors" — common DD-scale
        # failure where the model returns "Typical Units (Floors 2-3)" rather
        # than per-floor data. Treat that as a partial extraction so the
        # large-format rescue path (text-layer + tiling) gets a shot.
        likely_incomplete = bool(
            best_result and _extraction_likely_incomplete(best_result[1])
        )

        if best_rooms == 0 or rooms_have_zero_dims or likely_incomplete:
            try:
                from config import ENABLE_ENHANCED_EXTRACTION, LARGE_FORMAT_THRESHOLD_PT
            except ImportError:
                ENABLE_ENHANCED_EXTRACTION = True
                LARGE_FORMAT_THRESHOLD_PT = 2000

            if ENABLE_ENHANCED_EXTRACTION:
                # Check if any page in this PDF is large-format, and identify
                # painting-relevant pages to avoid tiling structural/MEP sheets
                has_large_pages = False
                painting_page_indices = None
                try:
                    import fitz as _fitz_check
                    _doc_check = _fitz_check.open(pdf_path)

                    # Get painting-relevant pages via classification
                    _classifications = _classify_pdf_pages(pdf_path)
                    if _classifications:
                        _included = [c for c in _classifications if c['include']]
                        if _included:
                            painting_page_indices = [c['page_index'] for c in _included]
                        pages_to_check = painting_page_indices or range(len(_doc_check))
                    else:
                        pages_to_check = range(len(_doc_check))

                    for _pg_i in pages_to_check:
                        if _pg_i < len(_doc_check):
                            _pg = _doc_check[_pg_i]
                            if max(_pg.rect.width, _pg.rect.height) >= LARGE_FORMAT_THRESHOLD_PT:
                                has_large_pages = True
                                break
                    _doc_check.close()
                except Exception:
                    pass

                if has_large_pages:
                    attempted_enhanced = True
                    n_pages = len(painting_page_indices) if painting_page_indices else "all"
                    if best_rooms == 0:
                        _why = "Native PDF returned 0 rooms"
                    elif rooms_have_zero_dims:
                        _why = "Native rooms had unreadable dimensions"
                    else:
                        _why = "Native returned templates instead of physical floors"
                    print(f"\n   🔬 {_why} — "
                          f"large-format pages detected, trying enhanced extraction "
                          f"({n_pages} painting-relevant pages)...")
                    time.sleep(15)  # brief cooldown
                    enhanced_result = _analyze_with_enhanced_extraction(
                        client, pdf_path,
                        scope_notes=scope_notes,
                        schedule_hints=image_schedule_data,
                        building_inventory=building_inventory,
                        page_indices=painting_page_indices,
                        project_overview=project_overview,
                    )
                    if enhanced_result:
                        _, enh_analysis = enhanced_result
                        enh_rooms = enh_analysis.get('project_info', {}).get(
                            'total_rooms_found', 0)
                        # When native returned templates (likely_incomplete) but enhanced
                        # returned fewer rooms, keep the original — fewer real rooms can
                        # still be a worse estimate than multiplied templates.
                        if enh_rooms > 0 and (best_rooms == 0
                                              or rooms_have_zero_dims
                                              or enh_rooms >= best_rooms):
                            print(f"   🔬 Enhanced extraction recovered {enh_rooms} rooms!")
                            result = enhanced_result
                            best_result = enhanced_result
                            best_rooms = enh_rooms
                            used_enhanced = True
                        elif enh_rooms > 0:
                            print(f"   🔬 Enhanced extraction returned {enh_rooms} rooms "
                                  f"— keeping native ({best_rooms}) which had more")
                        else:
                            print(f"   🔬 Enhanced extraction also returned 0 rooms")
                    else:
                        print(f"   🔬 Enhanced extraction failed")

        # ── Image fallback for floor plan files that returned 0 rooms ──
        # Also fires for combined-volume PDFs (filename doesn't match floor-plan
        # patterns) when enhanced extraction was attempted but didn't recover any
        # rooms — the file has large-format architectural pages, so image
        # rendering is worth a final attempt.
        used_image_fb = False
        if image_fallback and best_rooms == 0 and (is_fp or attempted_enhanced):
            try:
                from config import ENABLE_IMAGE_FALLBACK
                do_fallback = ENABLE_IMAGE_FALLBACK
            except ImportError:
                do_fallback = True

            if do_fallback:
                print(f"\n   🖼️  Native PDF returned 0 rooms after 3 attempts "
                      f"— trying image fallback...")
                time.sleep(30)  # cooldown before image attempt
                fb_result = _analyze_floor_plan_as_images(
                    client, pdf_path,
                    scope_notes=scope_notes,
                    schedule_hints=image_schedule_data,
                    building_inventory=building_inventory,
                    project_overview=project_overview,
                )
                if fb_result:
                    _, fb_analysis = fb_result
                    fb_rooms = fb_analysis.get('project_info', {}).get(
                        'total_rooms_found', 0)
                    if fb_rooms > 0:
                        print(f"   🖼️  Image fallback recovered {fb_rooms} rooms!")
                        result = fb_result
                        best_result = fb_result
                        best_rooms = fb_rooms
                        used_image_fb = True
                        image_fallback_files.append(filename)
                    else:
                        print(f"   🖼️  Image fallback also returned 0 rooms")
                else:
                    print(f"   🖼️  Image fallback failed")

        # Multi-pass extraction with per-room median merge.
        #
        # The previous f004a50 implementation ran 2 passes and kept "whichever
        # found more rooms" — that biased toward over-extraction (e.g. the
        # 364 Main 510-room inflated run was the bias's worst-case). Reverted.
        #
        # This version runs N total passes (configurable via env, default 3)
        # and merges them by taking the MEDIAN of every per-room dimension
        # and element count. Median is robust to one outlier in either
        # direction, so the merged answer converges toward the central
        # tendency across passes — directly addresses Claude's vision-encoder
        # variance on complex architectural PDFs.
        #
        # Default N=3. Set NIGHTSHIFT_MULTI_PASS_N=5 for tighter convergence
        # at higher API cost; set to 1 to effectively disable.
        #
        # Gate: fires when pass 1 returned at least
        # NIGHTSHIFT_MULTI_PASS_MIN_ROOMS (default 20) rooms — that's the
        # signal "this PDF has substantive floor-plan content and the
        # variance fix should apply." Previous gate used the filename-only
        # _is_floor_plan_file() heuristic, which never matched real-world
        # filenames customers actually upload ("Combined_Files.pdf",
        # "Bid_Set_*.pdf", "Ridgeview_Arch_Drawings_*.pdf"), so multi-pass
        # shipped but never fired in production — confirmed by zero
        # [Multi-Pass Median] notes across three Ridgeview runs on the
        # same PDF that produced 561 / 459 / 338 rooms and
        # $360K / $342K / $249K. Don't gate on filenames you can't trust.
        try:
            mp_min_rooms = int(os.environ.get(
                "NIGHTSHIFT_MULTI_PASS_MIN_ROOMS", "20"))
        except (ValueError, TypeError):
            mp_min_rooms = 20
        # Gate on DOCUMENT signals, not pass-1 output. The old gate
        # (best_rooms >= 20) conditioned the variance fix on a sample of
        # the very random variable it exists to stabilize: a pass-1
        # under-extraction of 15 rooms skipped consensus entirely and
        # shipped as the final answer — the exact 53-vs-15 failure mode.
        # The page classifier is deterministic and zero-API-cost: if the
        # document objectively contains paint-relevant pages, the variance
        # fix applies regardless of how many rooms pass 1 happened to see.
        # The room-count gate survives only as a fallback when
        # classification is unavailable (no PyMuPDF / unreadable PDF).
        try:
            mp_min_plan_pages = int(os.environ.get(
                "NIGHTSHIFT_MULTI_PASS_MIN_PLAN_PAGES", "1"))
        except (ValueError, TypeError):
            mp_min_plan_pages = 1
        doc_included_pages = None
        if multi_pass and not use_cache and result:
            try:
                _mp_cls = _classify_pdf_pages(pdf_path)
                if _mp_cls:
                    doc_included_pages = sum(
                        1 for c in _mp_cls if c.get("include"))
            except Exception:
                doc_included_pages = None

        if doc_included_pages is not None:
            should_multi_pass = (multi_pass and not use_cache and result
                                  and best_rooms > 0
                                  and doc_included_pages >= mp_min_plan_pages)
        else:
            should_multi_pass = (multi_pass and not use_cache
                                  and result and best_rooms >= mp_min_rooms)

        if (multi_pass and not use_cache and result
                and not should_multi_pass):
            # Surface the skip reason so worker logs are honest about
            # whether the variance fix actually fired on this job.
            if doc_included_pages is not None:
                print(f"   ⏭  Multi-pass median skipped: document has "
                      f"{doc_included_pages} paint-relevant page(s) < "
                      f"threshold {mp_min_plan_pages} "
                      f"(pass 1 rooms: {best_rooms})")
            else:
                print(f"   ⏭  Multi-pass median skipped: classification "
                      f"unavailable and pass 1 returned {best_rooms} rooms "
                      f"< fallback threshold {mp_min_rooms}")

        if should_multi_pass:
            try:
                n_passes = int(os.environ.get("NIGHTSHIFT_MULTI_PASS_N", "3"))
            except (ValueError, TypeError):
                n_passes = 3
            n_passes = max(1, min(7, n_passes))

            if n_passes >= 2:
                extra_passes = n_passes - 1  # we already have pass 1 in `result`
                pass_results = [result]
                if used_image_fb:
                    mode_label = "image mode"
                elif used_enhanced:
                    mode_label = "enhanced (tiled) mode"
                else:
                    mode_label = "vector mode"
                print(f"   🔄 Multi-pass median ({mode_label}): "
                      f"running passes 2..{n_passes} of {n_passes}")
                for i in range(extra_passes):
                    time.sleep(30)  # cooldown between passes
                    try:
                        if used_image_fb:
                            extra = _analyze_floor_plan_as_images(
                                client, pdf_path, scope_notes=scope_notes,
                                schedule_hints=image_schedule_data,
                                building_inventory=building_inventory,
                                project_overview=project_overview)
                        elif used_enhanced:
                            # Pass 1 only reached its room count via enhanced
                            # (tiled large-format) extraction — native vector
                            # returned ~0. Running the extra passes in plain
                            # vector mode produces a sparse, incompatible result
                            # set, so the per-room merge can't reconcile them and
                            # the median fallback ships the sparse pass. Observed
                            # 2026-06-08 on both Wingstop jobs: Eastern pass 1
                            # enhanced=52 rooms, vector passes 2-3 = 11/12, merge
                            # kept 0/75 → shipped 12; Aliante 31 vs 12/26 →
                            # shipped 26. Keep every pass on pass 1's extraction
                            # path so they're comparable and actually mergeable.
                            extra = _analyze_with_enhanced_extraction(
                                client, pdf_path,
                                scope_notes=scope_notes,
                                schedule_hints=image_schedule_data,
                                building_inventory=building_inventory,
                                page_indices=painting_page_indices,
                                project_overview=project_overview)
                        else:
                            extra = analyze_and_parse(
                                client, pdf_path, scope_notes=scope_notes,
                                schedule_hints=image_schedule_data,
                                building_inventory=building_inventory,
                                project_overview=project_overview)
                    except Exception as _mp_exc:
                        print(f"   ⚠️  Multi-pass {i+2} failed: {_mp_exc}")
                        extra = None
                    if extra:
                        _, extra_an = extra
                        extra_rooms = extra_an.get('project_info', {}).get(
                            'total_rooms_found', 0)
                        print(f"      pass {i+2}: {extra_rooms} rooms")
                        pass_results.append(extra)
                    else:
                        print(f"      pass {i+2}: failed")

                if len(pass_results) >= 2:
                    pass_analyses = [pr[1] for pr in pass_results]
                    per_pass_rooms = [
                        a.get('project_info', {}).get('total_rooms_found', 0)
                        for a in pass_analyses
                    ]
                    merged_analysis = _merge_passes_with_median(pass_analyses)
                    merged_rooms = (merged_analysis.get('project_info', {})
                                    .get('total_rooms_found', 0))
                    # Keep the path from pass 1; analysis is the median merge.
                    result = (pass_results[0][0], merged_analysis)
                    best_rooms = merged_rooms
                    print(f"   📊 Multi-pass median merge: "
                          f"per-pass rooms {per_pass_rooms} → "
                          f"merged {merged_rooms}")
                else:
                    print(f"   📊 Multi-pass: only pass 1 succeeded, "
                          f"using single-pass result")

        # Track room count for this file (for extraction validation)
        if result:
            _, ar = result
            file_room_counts[filename] = ar.get('project_info', {}).get('total_rooms_found', 0)
        else:
            file_room_counts[filename] = 0

        if result:
            all_results.append(result)
            files_analyzed.append(filename)
            path, analysis_result = result
            if _model_flagged_no_plans(analysis_result):
                print(f"   ⚠️  No detailed floor plans in this file")
                print(f"   Pages: {analysis_result.get('pages_reviewed', 'N/A')}")
                # Step 1: Existing door/window/stair schedule extraction
                schedule_data = analyze_schedule_pdf(client, pdf_path)

                # Step 2: Room Finish Schedule extraction for wall/ceiling estimation
                try:
                    from config import ENABLE_SCHEDULE_ESTIMATION
                except ImportError:
                    ENABLE_SCHEDULE_ESTIMATION = True

                if schedule_estimation and ENABLE_SCHEDULE_ESTIMATION:
                    print(f"   📊 Attempting schedule-based estimation...")
                    time.sleep(15)  # Cooldown before second API call
                    room_schedule = _extract_room_finish_schedule(client, pdf_path)
                    if room_schedule:
                        # Preserve structural_finish_scope even if no rooms were
                        # extracted — downstream dryfall safety net relies on it.
                        struct_scope = room_schedule.get("structural_finish_scope") or []
                        if struct_scope:
                            analysis_result["structural_finish_scope"] = struct_scope
                    if room_schedule and room_schedule.get("room_finish_schedule"):
                        synthetic_floors = _estimate_from_room_finish_schedule(
                            room_schedule, schedule_data
                        )
                        if synthetic_floors:
                            analysis_result["floors"] = synthetic_floors
                            for _flag in _INCOMPLETE_PLAN_FLAGS:
                                analysis_result[_flag] = False
                            analysis_result["schedule_estimated"] = True
                            analysis_result["building_info"] = room_schedule.get("building_info", {})
                            analysis_result["room_finish_schedule"] = room_schedule.get("room_finish_schedule", [])
                            schedule_estimated_files.append(filename)
                            total_synth = sum(len(f.get("rooms", [])) for f in synthetic_floors)
                            print(f"   ✅ Schedule estimation: {total_synth} room templates generated")

                # Merge schedule data (doors/windows/stairs)
                if schedule_data:
                    for key in ("door_schedule", "window_schedule", "stair_info", "wall_types"):
                        if schedule_data.get(key):
                            analysis_result[key] = schedule_data[key]
                    all_results[-1] = (path, analysis_result)

                # Dedicated exterior pass — schedule-only PDFs almost never
                # have exterior_paint_sqft populated, so always try.
                _maybe_run_exterior_pass(client, pdf_path, analysis_result)
                all_results[-1] = (path, analysis_result)
            else:
                totals = analysis_result.get('aggregated_totals', {})
                rooms = analysis_result.get('project_info', {}).get('total_rooms_found', 0)
                print(f"   ✅ {rooms} rooms found, {totals.get('total_paintable_wall_sqft', 0):,.0f} sqft walls")

                # --- Schedule re-analysis: if floor plans found but schedules missing ---
                missing_door_sched = not analysis_result.get('has_door_schedule')
                missing_win_sched = not analysis_result.get('has_window_schedule')
                if missing_door_sched or missing_win_sched:
                    missing = []
                    if missing_door_sched:
                        missing.append("door")
                    if missing_win_sched:
                        missing.append("window")
                    print(f"   📋 {'/'.join(missing)} schedule(s) not detected — running targeted schedule re-analysis...")
                    schedule_data = analyze_schedule_pdf(client, pdf_path)
                    if schedule_data:
                        for key in ("door_schedule", "window_schedule", "stair_info", "wall_types"):
                            if schedule_data.get(key):
                                analysis_result.setdefault("schedule_data", {})[key] = schedule_data[key]
                        if schedule_data.get("door_schedule"):
                            analysis_result["has_door_schedule"] = True
                            print(f"      ✅ Door schedule recovered from re-analysis")
                        if schedule_data.get("window_schedule"):
                            analysis_result["has_window_schedule"] = True
                            print(f"      ✅ Window schedule recovered from re-analysis")
                        all_results[-1] = (path, analysis_result)
                    else:
                        print(f"      ⚠️  Schedule re-analysis returned no data")

                # Dedicated schedule-recovery pass — only fires for commercial
                # jobs with implausibly low dryfall vs footprint. Catches the
                # B&N-style failure where the LLM read floor plans but missed
                # the finish schedule's "paint exposed deck/structure/MEP"
                # callout. Cheap no-op otherwise.
                _maybe_run_schedule_recovery_pass(client, pdf_path, analysis_result)

                # Dedicated exterior pass — only fires for commercial jobs
                # with 0 sqft exterior. Cheap no-op otherwise.
                _maybe_run_exterior_pass(client, pdf_path, analysis_result)
                all_results[-1] = (path, analysis_result)
        else:
            files_skipped.append(filename)
            print(f"\n   ❌ FAILED: {filename} could not be analyzed after 2 attempts")
            print(f"   ⚠️  This file's data will be MISSING from the estimate")

    if files_skipped:
        print(f"\n{'!'*80}")
        print(f"⚠️  WARNING: {len(files_skipped)}/{len(pdf_paths)} files could not be analyzed:")
        for f in files_skipped:
            print(f"   ✗ {f}")
        print(f"   The estimate will be INCOMPLETE. Re-run to retry failed files.")
        print(f"{'!'*80}")

    if image_fallback_files:
        print(f"\n   🖼️  Image fallback used for {len(image_fallback_files)} file(s):")
        for f in image_fallback_files:
            print(f"      ✓ {f}")

    if schedule_estimated_files:
        print(f"\n   📊 Schedule estimation used for {len(schedule_estimated_files)} file(s):")
        for f in schedule_estimated_files:
            print(f"      ✓ {f}")

    if not all_results:
        _failed_list = ", ".join(os.path.basename(p) for p in pdf_paths)
        _skip_list = ", ".join(files_skipped) if files_skipped else "none"
        raise ValueError(
            f"No PDFs could be analysed successfully. "
            f"Files attempted: {_failed_list}. "
            f"Files skipped: {_skip_list}. "
            f"Check the logs above for per-file error messages (❌ lines). "
            f"Common causes: missing API key, PDF files not found, "
            f"API errors (rate limit, auth), or missing dependencies (PyMuPDF)."
        )

    # --- Merge or use single result ---
    if multi_mode:
        print(f"\n{'='*80}")
        print(f"🔗 MERGING {len(all_results)} analyses into combined estimate...")
        print(f"{'='*80}")
        _update_progress(4, TOTAL_STEPS, "Merging Results", f"Combining data from {len(all_results)} files...")
        analysis = merge_analyses(all_results, file_building_counts=file_building_counts)
    else:
        _, analysis = all_results[0]
        if _model_flagged_no_plans(analysis):
            print(f"\n⚠️  NO FLOOR PLANS FOUND")
            print(f"Pages reviewed: {analysis.get('pages_reviewed', 'Unknown')}")
        analysis = _normalize_scope_fields(analysis)
        analysis = _recalculate_totals(analysis)
        # Schedule overrides applied AFTER all recalculations (see below)

    # Normalize scope fields after merge (ensures every room has in_scope)
    _update_progress(5, TOTAL_STEPS, "Validating & Recalculating", "Applying guardrails and schedule overrides...")
    analysis = _normalize_scope_fields(analysis)

    # --- Skipped files: block silent partial estimates ---
    # files_skipped used to be written into the result JSON and read by
    # NOTHING — a 3-file upload missing one whole file shipped a normal-
    # looking proposal. A missing file is missing scope: force manual
    # review and surface an RFI naming the files.
    if files_skipped:
        analysis["manual_review_required"] = True
        _skip_reason = (
            f"{len(files_skipped)} of {len(pdf_paths)} uploaded file(s) "
            f"could not be analyzed: {', '.join(files_skipped)}. "
            f"Their scope is MISSING from this estimate."
        )
        if analysis.get("manual_review_reason"):
            analysis["manual_review_reason"] = (
                str(analysis["manual_review_reason"]) + " | " + _skip_reason)
        else:
            analysis["manual_review_reason"] = _skip_reason
        analysis.setdefault("notes", []).append(
            f"[Files Skipped] {_skip_reason} RFI REQUIRED: re-supply or "
            f"re-run the failed file(s) before relying on this estimate — "
            f"no quantities from them are priced."
        )

    # --- Finish schedule + upload sheet inventory ---
    # Carry the pre-extracted Room Finish Schedule onto the result so the
    # has_finish_schedule flag and downstream RFIs see it.
    if room_finish_schedule and not analysis.get("room_finish_schedule"):
        analysis["room_finish_schedule"] = room_finish_schedule
    _set_finish_schedule_flag(analysis, pdf_paths)
    _set_door_schedule_flag(analysis, pdf_paths)
    _set_window_schedule_flag(analysis, pdf_paths)
    # Canonicalize source_sheet on every room BEFORE the upload-sheet
    # inventory is built and BEFORE downstream dedup runs. The LLM
    # sometimes emits ANSI-style sheet IDs ('A-102') for pages actually
    # marked in a different convention ('A2'); the dedup pass treats
    # those as different sheets and ends up with phantom floor templates.
    analysis = _canonicalize_source_sheets(analysis, pdf_paths)
    analysis["_upload_sheet_numbers"] = sorted(_collect_upload_sheet_numbers(pdf_paths))

    # --- Whitebox / Prime Only exclusion (before validation) ---
    analysis = _apply_whitebox_exclusion(analysis)

    # --- Accessory structure exclusion for single-family ---
    # For single-family homes, exclude floors that represent separate structures
    # (sheds, garages, accessory buildings) that aren't part of the main house
    # painting scope. Also exclude "Lower Level" / utility-only basement floors
    # when they contain only utility/mechanical rooms.
    _sf_bt = str(analysis.get("project_info", {}).get("building_type", "")).lower()
    _sf_units_raw = analysis.get("project_info", {}).get("total_units", 0)
    _sf_units = _num(_sf_units_raw) if isinstance(_sf_units_raw, (int, float)) else 0
    _sf_neg_kw = ("multi", "mixed", "commercial", "apartment",
                   "senior", "assisted", "living", "facility", "institutional",
                   "hospital", "medical", "nursing", "dormitor", "hotel")
    _is_sf = (
        any(kw in _sf_bt for kw in ("single", "detached"))
        or (_sf_units <= 2 and isinstance(_sf_units_raw, (int, float))
            and not any(kw in _sf_bt for kw in _sf_neg_kw))
        or _detect_single_family_from_rooms(analysis)
    )
    if _is_sf:
        _excluded_floor_names = []
        _ACCESSORY_KW = ("shed", "accessory", "outbuilding", "detached garage",
                         "barn", "carport", "pool house", "guest house")
        _UTILITY_ONLY_KW = ("utility", "mechanical", "boiler", "storage")
        for floor in list(analysis.get("floors", [])):
            fname = floor.get("floor_name", "").lower()
            # Exclude accessory structures
            if any(kw in fname for kw in _ACCESSORY_KW):
                _excluded_floor_names.append(floor.get("floor_name", ""))
                analysis["floors"].remove(floor)
                continue
            # Exclude lower level / basement floors that only have utility rooms
            if "lower level" in fname or "basement" in fname or "foundation" in fname:
                rooms = floor.get("rooms", [])
                all_utility = all(
                    any(kw in r.get("room_name", "").lower() for kw in _UTILITY_ONLY_KW)
                    for r in rooms
                ) if rooms else False
                if all_utility:
                    _excluded_floor_names.append(floor.get("floor_name", ""))
                    analysis["floors"].remove(floor)
                    continue
        if _excluded_floor_names:
            print(f"   🏠 Single-family: excluded accessory floors: {_excluded_floor_names}")
            analysis.setdefault("notes", []).append(
                f"[Single-Family Scope] Excluded non-main-house floors: "
                f"{', '.join(_excluded_floor_names)} — not in typical residential painting scope"
            )
            # Recalculate totals after floor removal
            analysis = _recalculate_totals(analysis)

    # Normalize Claude's JSON to the expected schema before any consumer
    # reads it (prevents off-type TypeErrors; flags degraded extractions).
    analysis = _normalize_analysis(analysis)

    # Run extraction validation checks
    analysis = _validate_extraction(analysis, file_room_counts=file_room_counts,
                                    project_overview=project_overview)

    # --- Phase 4: Provenance audit (hallucination detection, count reconciliation) ---
    # Conservative — flags only, does not auto-remove. Runs AFTER dedup + recalc
    # so it sees the final merged room set, but BEFORE building-inventory scaling
    # (that step multiplies rooms — auditing pre-multiplication is the right unit).
    try:
        enable_audit = True
        try:
            from config import ENABLE_PROVENANCE_AUDIT
            enable_audit = ENABLE_PROVENANCE_AUDIT
        except ImportError:
            pass
        if enable_audit:
            analysis = _audit_room_provenance(
                analysis, project_overview=project_overview)
        if project_overview:
            analysis["project_overview"] = project_overview
    except Exception as e:
        print(f"   ⚠️  Phase 4 provenance audit failed: {e}")

    # Run building inventory validation (auto-scale multipliers from index pages)
    if building_inventory:
        analysis = _validate_building_inventory(
            analysis, building_inventory, file_building_counts=file_building_counts)
        analysis = _recalculate_totals(analysis)

    # Exterior scope safety net — zero out exterior if scope says interior only
    if scope_notes and any(kw in scope_notes.lower()
                           for kw in ("interior only", "no exterior", "skip exterior")):
        analysis["exterior"] = {}
        analysis.setdefault("notes", []).append(
            "[Scope] Exterior excluded per scope notes")

    # --- Apply corrections (if any) ---
    corrections = _load_corrections(corrections_path)
    if corrections:
        analysis = _apply_corrections(analysis, corrections)
        analysis = _recalculate_totals(analysis)  # Re-recalculate after corrections

    # --- Apply schedule overrides LAST (after all recalculations) ---
    # This must be the FINAL modification to aggregated_totals before cost calc.
    # Schedule data is authoritative — it lists every door/window in the project.
    # _recalculate_totals() would wipe these overrides (it rebuilds from room data),
    # so schedule overrides must come after all recalculate calls.
    if image_schedule_data:
        existing_sd = analysis.get("schedule_data", {})
        for key in ("door_schedule", "window_schedule", "stair_info"):
            if image_schedule_data.get(key):
                existing_sd[key] = image_schedule_data[key]
        analysis["schedule_data"] = existing_sd
        if image_schedule_data.get("door_schedule"):
            analysis["has_door_schedule"] = True
        if image_schedule_data.get("window_schedule"):
            analysis["has_window_schedule"] = True
        print(f"\n   📋 Image-based schedule data injected into analysis")
    # Apply schedule overrides (from either PDF-based or image-based schedule)
    if analysis.get("schedule_data"):
        analysis = _apply_schedule_overrides(analysis)

    # --- Secondary space supplement (closets, halls, entries) ---
    # Uses rooms-per-unit density to detect missing secondary spaces and
    # supplements wall/ceiling/trim with estimated area. Must run BEFORE
    # perimeter cross-check and wall boost so those operate on supplemented totals.
    analysis = _supplement_missing_secondary_spaces(analysis)

    # --- Unit-multiplier sanity validator ---
    # Catches the Ridgeview-2026-05-28 case: model emits multipliers that
    # sum correctly to total_units but distribute wildly wrong across unit
    # types (e.g. 18 1BR + 10 2BR for a building that's actually 28/2).
    # Cannot deterministically fix without OCR on the T1 unit-mix table
    # (which is vector art on most architectural sets), but adds an audit
    # note + RFI flag so the estimator catches the issue manually instead
    # of bidding blind.
    analysis = _validate_unit_multipliers(analysis)

    # --- Residential ceiling floor (GSF-based) ---
    # Per-room ceiling extraction systematically under-counts dense
    # vector-rendered architectural sets. When extracted ceiling falls
    # materially below footprint × stories × efficiency, bump to the
    # GSF-based floor. KonstructIQ comparison on Ridgeview: extracted
    # 32,601 SF vs truth 42,923 SF (= GSF). Default efficiency assumes
    # commons are painted (typical for supportive housing / multifamily);
    # auto-downshifts when the finish schedule shows ACT in commons.
    analysis = _apply_residential_ceiling_floor(analysis)

    # --- Perimeter-based wall cross-check (must run BEFORE wall boost) ---
    # Computes perimeter-derived wall totals and stores in _perimeter_cross_check
    # for _validate_and_boost_walls() to use as preferred boost source.
    analysis = _validate_wall_area_by_perimeter(analysis)

    # --- Wall area validation + boost (for residential multi-family) ---
    # Uses perimeter-based boost (preferred) or footprint-based (fallback).
    # Boost cap elevated when secondary space supplement confirmed under-extraction.
    # Must come AFTER _recalculate_totals, secondary supplement, and perimeter cross-check.
    analysis = _validate_and_boost_walls(analysis)

    # --- Commercial window exclusion (after all overrides/boosts) ---
    # For commercial (non-residential) buildings, zero all painted windows.
    # Must come AFTER schedule overrides so schedule data doesn't override back.
    analysis = _apply_commercial_window_exclusion(analysis)

    # --- Wall:Ceiling ratio guard rail (after all boosts/corrections) ---
    # Informational check — does NOT modify totals, only adds warnings/RFIs.
    analysis = _check_wall_ceiling_ratio(analysis)

    # --- Stair estimation fallback ---
    # If stair_sections is still 0 but the building has multiple stories, estimate.
    # Stairs are often mentioned in notes but not captured in room elements.
    agg = analysis.get("aggregated_totals", {})
    pi = analysis.get("project_info", {})
    current_stairs = _num(agg.get("total_stair_sections", 0))
    total_stories = _num(pi.get("total_stories", 0))

    # Single-family stair exclusion: Rider does NOT include stair painting
    # in single-family residential scope. Zero out any extracted stairs.
    building_type_str = str(pi.get("building_type", "")).lower()
    total_units_raw = pi.get("total_units", 0)
    total_units = _num(total_units_raw)
    total_units_is_numeric = isinstance(total_units_raw, (int, float))
    # IMPORTANT: institutional keywords must be excluded here — senior living,
    # facilities, hospitals etc. with 0 units should NOT be treated as single-family.
    _sf_negative_kw = ("multi", "mixed", "commercial", "apartment",
                        "senior", "assisted", "living", "facility", "institutional",
                        "hospital", "medical", "nursing", "dormitor", "hotel")
    is_single_family = (
        any(kw in building_type_str for kw in ("single", "detached"))
        or (total_units_is_numeric and total_units <= 2
            and not any(kw in building_type_str for kw in _sf_negative_kw))
    )
    # Room-based override: detect single-family from actual room inventory
    if not is_single_family and _detect_single_family_from_rooms(analysis):
        is_single_family = True
        print(f"   🏠 Room-based detection: treating as single-family for stair scope")
    if is_single_family:
        if current_stairs > 0:
            print(f"   🏠 Single-family: excluding {current_stairs} stair sections (not in Rider scope)")
            analysis.setdefault("notes", []).append(
                f"[Single-Family Scope] Excluded {current_stairs} stair sections — "
                f"stair painting typically not in scope for single-family residential"
            )
            agg["total_stair_sections"] = 0
            agg["total_gyp_between_stairs_sqft"] = 0
            analysis["aggregated_totals"] = agg
        # Skip the rest of the stair estimation for single-family
        current_stairs = 0
        total_stories = 0  # Prevents stair fallback from triggering below

    # Check for basement/cellar floor in extracted data
    # total_stories typically counts above-grade floors only (1st, 2nd, 3rd)
    # Basement adds an extra level transition for stair calculations.
    # Require >=2 rooms on the basement floor — Fishkill labels a single
    # foundation slab as "Foundation/Basement" but has no usable basement
    # space, so it should not contribute a stair transition.
    floors_list = analysis.get("floors", [])
    has_basement = any(
        ("base" in (f.get("floor_name", "") or "").lower() or
         "cellar" in (f.get("floor_name", "") or "").lower())
        and len(f.get("rooms", []) or []) >= 2
        for f in floors_list
    )
    effective_levels = int(total_stories) + (1 if has_basement else 0)

    # Calculate expected minimum stair count based on building size
    # Typical: 2 stairwells × (effective_levels - 1) transitions × 2 flights
    expected_min_stairs = 2 * max(1, effective_levels - 1) * 2

    if total_stories >= 2 and (current_stairs == 0 or current_stairs < expected_min_stairs * 0.7):
        # Stairs are missing or seem too low for the building
        est_stairs = 0

        _stairwell_note_seen = False
        if current_stairs == 0:
            # Try to parse stair count from stair-specific notes only
            for note in list(analysis.get("notes", [])):
                note_lower = note.lower()
                # Skip notes that aren't about stairs
                if 'stair' not in note_lower:
                    continue
                # Skip our own fallback notes (avoid circular reference)
                if '[stair fallback]' in note_lower:
                    continue
                # "X total sections", "X stair sections", "X stair flights"
                m = re.search(r'(\d+)\s*total\s*(?:stair|section|flight)', note_lower)
                if m:
                    est_stairs = max(est_stairs, int(m.group(1)))
                m1b = re.search(r'(\d+)\s*stair\s*(?:section|flight)', note_lower)
                if m1b:
                    est_stairs = max(est_stairs, int(m1b.group(1)))
                # "N stairwells" — multiply by effective_levels × 2 flights
                # per transition. HARD_NUMBERS_ONLY: the stairwell COUNT may
                # be stated, but flights-per-transition is an assumption —
                # a free-text "2 stairwells" turned into 12 priced sections
                # ($18k on a 4-level building). Explicit section/flight
                # counts (m, m1b, m3) are stated numbers and stay accepted.
                if not HARD_NUMBERS_ONLY:
                    m2 = re.search(r'(\d+)\s*stairwell', note_lower)
                    if m2:
                        stairwells = int(m2.group(1))
                        transitions = max(1, effective_levels - 1)
                        est_stairs = max(est_stairs, stairwells * transitions * 2)
                elif re.search(r'(\d+)\s*stairwell', note_lower):
                    _stairwell_note_seen = True
                # "= X total" at end of stair note
                m3 = re.search(r'=\s*(\d+)\s*total', note_lower)
                if m3:
                    est_stairs = max(est_stairs, int(m3.group(1)))

            # Cap at reasonable maximum: 4 stairwells × effective_levels × 2 flights
            max_reasonable = 4 * effective_levels * 2
            if est_stairs > max_reasonable:
                est_stairs = 0

            if HARD_NUMBERS_ONLY and est_stairs == 0 and _stairwell_note_seen:
                analysis.setdefault("notes", []).append(
                    "[Stair Check] Notes mention a stairwell count but no "
                    "explicit section/flight count was extracted. RFI "
                    "REQUIRED: confirm the number of stair sections/flights "
                    "to paint (stairwell-count extrapolation is NOT priced "
                    "under hard-numbers policy)."
                )

        # If notes didn't give us a number, use building heuristic.
        # Gated by HARD_NUMBERS_ONLY: a geometry-based stair estimate — and the
        # undercount "boost" that depends on it — is a heuristic, not a measured
        # count. With the policy on, trust the extracted count (or an explicit
        # stair count parsed from the notes above) and surface gaps as RFIs.
        if est_stairs == 0 and not HARD_NUMBERS_ONLY:
            est_stairwells = 2  # standard for residential
            transitions = max(1, effective_levels - 1)
            est_stairs = est_stairwells * transitions * 2

        # Only apply if the estimate is higher than current count
        if est_stairs > current_stairs:
            agg["total_stair_sections"] = est_stairs
            analysis["aggregated_totals"] = agg
            basement_note = f" (includes basement)" if has_basement else ""
            if current_stairs > 0:
                analysis.setdefault("notes", []).append(
                    f"[Stair Boost] Room extraction found {current_stairs} sections, "
                    f"but building has {effective_levels} levels{basement_note} — "
                    f"boosted to {est_stairs} sections (2 stairwells x {max(1, effective_levels-1)} transitions x 2 flights)"
                )
                print(f"   🪜 Stair boost: {current_stairs} -> {est_stairs} sections "
                      f"for {effective_levels}-level building{basement_note}")
            else:
                analysis.setdefault("notes", []).append(
                    f"[Stair Fallback] Estimated {est_stairs} stair sections "
                    f"({effective_levels} levels{basement_note}, no stair data in room extraction)"
                )
                print(f"   🪜 Stair fallback: estimated {est_stairs} sections "
                      f"for {effective_levels}-level building{basement_note}")

    # --- Stair section cap for multi-story buildings ---
    # When LLM-extracted stairs significantly exceed the building heuristic,
    # the LLM likely counted landings as separate sections. Cap to heuristic.
    # Use effective_levels (includes basement) to stay symmetric with the boost
    # formula above — otherwise a 3-story + basement building gets boosted to
    # 12 then capped back to 8, losing the basement stair flight. Rider's
    # 364 Main takeoff has 11 sections for this exact shape.
    final_stair_count = _num(agg.get("total_stair_sections", 0))
    cap_expected = 2 * max(1, effective_levels - 1) * 2
    if total_stories >= 2 and cap_expected > 0:
        stair_cap = round(cap_expected * 1.25)
        if final_stair_count > stair_cap:
            agg["total_stair_sections"] = cap_expected
            analysis["aggregated_totals"] = agg
            basement_note = " (incl. basement)" if has_basement else ""
            analysis.setdefault("notes", []).append(
                f"[Stair Cap] Capped stairs from {final_stair_count} to "
                f"{cap_expected} sections (heuristic: 2 stairwells x "
                f"{max(1, effective_levels - 1)} transitions x 2 flights = "
                f"{cap_expected}{basement_note}). Extraction likely counted "
                f"landings as separate sections."
            )
            print(f"   🪜 Stair cap: {final_stair_count} -> {cap_expected} sections "
                  f"(capped to {effective_levels}-level heuristic{basement_note})")

    # --- Gyp between stairs auto-estimate ---
    # If stairs exist but gyp_between_stairs is missing or too low, estimate from stair count.
    # Rider data: 1,424 sqft / 11 sections ≈ 130 sqft per section
    final_stairs = _num(agg.get("total_stair_sections", 0))
    current_gyp_stairs = _num(agg.get("total_gyp_between_stairs_sqft", 0))
    expected_gyp = round(final_stairs * 130) if final_stairs > 0 else 0
    if final_stairs > 0 and current_gyp_stairs < expected_gyp * 0.5:
        # Missing or significantly under-estimated — use heuristic
        agg["total_gyp_between_stairs_sqft"] = expected_gyp
        analysis["aggregated_totals"] = agg
        if current_gyp_stairs > 0:
            analysis.setdefault("notes", []).append(
                f"[Gyp Stairs Boost] Gyp between stairs boosted from {current_gyp_stairs:,} to "
                f"{expected_gyp:,} sqft ({final_stairs} sections x 130 sqft/section)"
            )
            print(f"   🧱 Gyp between stairs: {current_gyp_stairs:,} -> {expected_gyp:,} sqft "
                  f"({final_stairs} sections x 130 sqft)")
        else:
            analysis.setdefault("notes", []).append(
                f"[Gyp Stairs Estimate] Estimated {expected_gyp:,} sqft gyp between stairs "
                f"({final_stairs} sections x 130 sqft/section)"
            )
            print(f"   🧱 Gyp between stairs: estimated {expected_gyp:,} sqft "
                  f"({final_stairs} sections x 130 sqft)")

    # --- Painted railing auto-boost ---
    # Stair railings should propagate with stair_sections, but unit-multipliers
    # are per-room (not per-floor) so railings on upper floors are silently dropped
    # when extraction only catches Level 1 stair rooms. Mirror the stair-section
    # boost: ~15 LF painted railing per stair section (calibrated from stair
    # rooms in recent runs — typical 12-15 LF/section per flight, both sides).
    current_railing = _num(agg.get("total_painted_railing_lf", 0))
    expected_railing = round(final_stairs * 15) if final_stairs > 0 else 0
    if final_stairs > 0 and current_railing < expected_railing * 0.5:
        agg["total_painted_railing_lf"] = expected_railing
        analysis["aggregated_totals"] = agg
        if current_railing > 0:
            analysis.setdefault("notes", []).append(
                f"[Railing Boost] Painted railing boosted from {current_railing:,} to "
                f"{expected_railing:,} LF ({final_stairs} sections x 15 LF/section)"
            )
            print(f"   🪜 Railing boost: {current_railing:,} -> {expected_railing:,} LF "
                  f"({final_stairs} sections x 15 LF/section)")
        else:
            analysis.setdefault("notes", []).append(
                f"[Railing Estimate] Estimated {expected_railing:,} LF painted railing "
                f"({final_stairs} sections x 15 LF/section)"
            )
            print(f"   🪜 Railing fallback: estimated {expected_railing:,} LF "
                  f"({final_stairs} sections x 15 LF/section)")

    # --- Save analysis to cache (before cost calc, which uses current pricing) ---
    if use_cache and combined_cache_dir:
        if not combined_cache_dir.exists():
            if multi_mode:
                combined_cache_dir.mkdir(parents=True, exist_ok=True)
                _save_cache(combined_cache_dir, "metadata.json", {
                    "pdf_paths": [str(p) for p in pdf_paths],
                    "code_hash": _code_hash(),
                    "created_at": datetime.utcnow().isoformat(),
                })
            else:
                _init_cache(combined_cache_dir, pdf_paths[0],
                            pdf_hashes.get(pdf_paths[0], ""))
        _save_cache(combined_cache_dir, "final_result.json", analysis)
        print(f"\n   💾 Analysis cached for instant re-runs")

    # --- Inject building_inventory units into project_info for cost caps ---
    # building_inventory is extracted from index pages (more reliable than per-batch LLM)
    if building_inventory and building_inventory.get('total_units'):
        _pi = analysis.setdefault('project_info', {})
        _pi['_building_inventory_units'] = building_inventory['total_units']

    # --- Apply pre-run rate overrides ---
    pricing_model_used = None
    if rate_overrides:
        pricing_model_used = _apply_rate_overrides(rate_overrides)
        overridden = [k for k in rate_overrides if k != "markup"]
        if overridden:
            print(f"\n📊 Rate overrides applied: {', '.join(overridden)}")
        if "markup" in rate_overrides:
            print(f"   Global markup override: {float(rate_overrides['markup'])*100:.1f}%")

    # --- Partial-extraction failure detector ---
    # The unified extraction and the building-inventory pre-scan both swallow
    # Anthropic InternalServerError / timeout exceptions per chunk and produce
    # whatever data they got. On a transient API outage this yields a
    # "looks-fine" analysis with most of the PDF missing — the May 5 Albany
    # B&N run extracted 4 rooms from 2 sheets when the same PDF on May 1
    # extracted 26 rooms from 18 sheets. We don't want to ship a bid based on
    # 1/9th of the PDF, so detect this case and flag for re-run rather than
    # let the downstream sanity check absorb it as a generic "scope missing"
    # signal that pricing tries to recover from.
    def _flag_partial_extraction(reason_msg):
        analysis["manual_review_required"] = True
        existing = analysis.get("manual_review_reason") or ""
        new_reason = f"[EXTRACTION INCOMPLETE — RE-RUN REQUIRED] {reason_msg}"
        analysis["manual_review_reason"] = (
            f"{new_reason} | {existing}" if existing else new_reason
        )
        analysis.setdefault("notes", []).append(f"[Partial Extraction] {reason_msg}")
        analysis.setdefault("_pre_pricing_rfis", []).append({
            "category": "Extraction Incomplete",
            "question": (
                f"This extraction appears severely incomplete and the bid below "
                f"should NOT be sent. {reason_msg} Most likely cause: the "
                f"Anthropic API returned a transient error during one or more "
                f"PDF chunks and the pipeline produced a degraded result. "
                f"Re-run the analysis on the same PDF; if a re-run reproduces "
                f"the same numbers, escalate."
            ),
            "action_required": (
                "Re-run the analysis. Do not send this proposal — extraction "
                "missed a significant portion of the PDF."
            ),
            "severity": "high",
            "source": "partial_extraction_detector",
        })
        print(f"\n🚨 PARTIAL EXTRACTION FAILURE DETECTED")
        print(f"   {reason_msg}")
        print(f"   ⚠️  Job flagged for manual review and re-run")

    # Trigger 1: index page detected a building list but inventory call returned null
    _inventory_should_have_worked = any(
        info.get("has_building_list") for info in _index_info_per_pdf
    )
    if _inventory_should_have_worked and building_inventory is None:
        _flag_partial_extraction(
            "Index pages detected a building list (drawing index, sheet schedule, "
            "or building schedule) but the building-inventory call returned null "
            "— the API call likely failed silently during pre-scan."
        )

    # Trigger 2: architectural-sheet coverage from extracted rooms vs. drawing index.
    #
    # IMPORTANT: The denominator must only include sheets that ACTUALLY produce
    # rooms (floor plans, foundation plans, roof plans, apartment plans, RCPs).
    # The drawing index also lists elevations, sections, schedules, and
    # details — these are reference material and don't contain room
    # measurements. If they're in the denominator the metric is unhittable
    # (a typical 17-sheet set has only 4-7 rooms-expected sheets).
    # Sheet-ID recognition uses the canonical _SHEET_NUMBER_RE + _normalize_sheet_token
    # so dotted IDs ("A1.02", "A2.01a") and dashed/spaced variants all parse. The old
    # `\d{2,3}` regex silently missed every dotted-convention sheet and instead matched
    # ASTM spec callouts (A653, A615, A706…) and reference-symbol examples (1/A101) as
    # if they were sheets — a phantom denominator that produced a false "low coverage /
    # DO NOT SEND" alarm (observed on the Tesla Cybercab set: real sheets A1.02/A2.01/
    # A3.01 compared against phantom {A653,A570,A611,…} → bogus 33% coverage).
    #
    # To stay robust we (a) title-anchor each ID — a real sheet has a plan/elevation/
    # section/schedule/detail keyword next to it in the index; an ASTM spec does not;
    # (b) intersect with sheets physically present in the upload (title-block scan);
    # (c) treat any sheet that actually yielded rooms as both covered AND room-bearing,
    # so numerator and denominator share an attribution basis and the alarm fires only
    # when real plan sheets present in the set yielded nothing (true truncation).
    _ARCH_PREFIXES = ("A", "AD")
    _ROOMS_EXPECTED_KW = (
        "floor plan", "foundation plan", "roof plan",
        "apartment plan", "ceiling plan", "rcp",
        "enlarged floor", "enlarged plan",
    )
    _REFERENCE_ONLY_KW = (
        "elevation", "section", "schedule", "detail", "cover", "index",
        "general note", "site plan", "demolition", "abbreviation",
        "wall section", "stair section", "canopy", "key plan",
    )
    _TITLE_KW = _ROOMS_EXPECTED_KW + _REFERENCE_ONLY_KW

    def _arch_sheet_id(raw):
        """Normalize a string to an A/AD architectural sheet token
        ('A1.02' -> 'A102'), or None if it isn't one. Drops bare fragments
        like 'A2' (len < 3) that fall out of partial matches."""
        m = _SHEET_NUMBER_RE.match(str(raw).strip())
        if not m or m.group(1).upper() not in _ARCH_PREFIXES:
            return None
        norm = _normalize_sheet_token(m.group(1) + m.group(2))
        return norm if len(norm) >= 3 else None

    # Sheets physically present in the uploaded set (authoritative title-block scan).
    _present_arch = {s for s in (analysis.get("_upload_sheet_numbers") or [])
                     if s and s[0] == "A"}

    # Sheets the extraction actually attributed rooms to.
    _extracted_arch_sheets = set()
    for _floor in analysis.get("floors", []) or []:
        for _room in _floor.get("rooms", []) or []:
            _sid = _arch_sheet_id(_room.get("source_sheet", ""))
            if _sid:
                _extracted_arch_sheets.add(_sid)

    # Parse the drawing index: each real sheet ID sits next to its title. Titles run
    # together in linearized index text, so classify room-bearing only when a room
    # keyword precedes any reference keyword in the adjacent context window.
    _all_arch_sheets = set()
    _rooms_expected_sheets = set()
    for _info in _index_info_per_pdf:
        _txt = _info.get("index_text") or ""
        for _m in _SHEET_NUMBER_RE.finditer(_txt):
            if _m.group(1).upper() not in _ARCH_PREFIXES:
                continue
            _norm = _normalize_sheet_token(_m.group(1) + _m.group(2))
            if len(_norm) < 3:
                continue
            _ctx = _txt[_m.end():_m.end() + 40].lower()
            if not any(kw in _ctx for kw in _TITLE_KW):
                continue  # ASTM spec / random number / equipment tag — not a sheet
            _all_arch_sheets.add(_norm)
            _first_room = min([_ctx.find(kw) for kw in _ROOMS_EXPECTED_KW if kw in _ctx] or [10 ** 9])
            _first_ref = min([_ctx.find(kw) for kw in _REFERENCE_ONLY_KW if kw in _ctx] or [10 ** 9])
            if _first_room < _first_ref:
                _rooms_expected_sheets.add(_norm)

    # Room-bearing sheets that genuinely exist in the upload, unioned with the sheets
    # that produced rooms. Coverage < 60% now means "real plan sheets present in the
    # set yielded nothing" — not "rooms were attributed to the occupant-load/FFE plan
    # instead of the floor plan."
    _rooms_present = (_rooms_expected_sheets & _present_arch) if _present_arch else _rooms_expected_sheets
    _denominator = _rooms_present | _extracted_arch_sheets
    _extracted_in_denom = _extracted_arch_sheets & _denominator

    if (len(_denominator) >= 4
            and _extracted_in_denom
            and len(_extracted_in_denom) / len(_denominator) < 0.60):
        _coverage_pct = (len(_extracted_in_denom) / len(_denominator)) * 100
        _missed_sheets = sorted(_denominator - _extracted_in_denom)
        _flag_partial_extraction(
            f"Extracted rooms came from only {len(_extracted_in_denom)} of "
            f"{len(_denominator)} architectural plan sheets present in the set "
            f"({_coverage_pct:.0f}% coverage; ≥60% expected). "
            f"(room-bearing sheets physically present in the upload; ASTM specs, "
            f"reference-symbol callouts, and detail/schedule sheets excluded). "
            f"Missed sheets: {_missed_sheets[:8]}"
            f"{'...' if len(_missed_sheets) > 8 else ''}. "
            f"Extracted: {sorted(_extracted_in_denom)[:8]}."
        )

    # Trigger 3: chunk_tracking shows ≥50% of PDF chunks failed
    _ct = analysis.get("_chunk_tracking") or {}
    _total_chunks = int(_ct.get("total_chunks") or 0)
    _failed = _ct.get("chunks_failed") or []
    _failed_count = len(_failed) if isinstance(_failed, list) else int(_failed or 0)
    if _total_chunks > 0 and _failed_count / _total_chunks >= 0.5:
        _flag_partial_extraction(
            f"{_failed_count} of {_total_chunks} PDF chunks failed during "
            f"extraction ({_failed_count/_total_chunks:.0%}). Most of the PDF "
            f"was not analyzed."
        )

    # --- Sales-floor-ACT heuristic check ---
    # Standalone retail buildings >5,000 sqft very rarely have a suspended
    # ACT ceiling on the main sales floor — they're almost always painted
    # to the deck (open structure). When the LLM tags the largest retail
    # room as ACT/not-painted, that's a strong signal the unified extraction
    # missed the finish-schedule callout. Surface as an RFI rather than
    # silently dropping ~footprint × $1/sqft of dryfall scope.
    _floor_is_retail_commercial = False
    _retail_pi = analysis.get("project_info", {}) or {}
    _retail_bt = str(_retail_pi.get("building_type", "")).lower()
    if any(kw in _retail_bt for kw in ("retail", "commercial", "dealership")):
        _floor_is_retail_commercial = True

    if _floor_is_retail_commercial:
        _largest_room = None
        _largest_fa = 0
        for _floor in analysis.get("floors", []) or []:
            for _room in _floor.get("rooms", []) or []:
                if not _room.get("in_scope", True):
                    continue
                _fa = _num((_room.get("dimensions") or {}).get("floor_area_sqft", 0))
                if _fa > _largest_fa:
                    _largest_fa = _fa
                    _largest_room = _room

        if _largest_room and _largest_fa >= 5000:
            _mats = _largest_room.get("materials", {}) or {}
            _ceil = str(_mats.get("ceiling", "")).lower()
            _ceil_painted = bool(_mats.get("ceiling_painted", False))
            _is_act = ("act" in _ceil or "acoustic" in _ceil
                       or "suspended" in _ceil or "drop" in _ceil)
            if _is_act and not _ceil_painted:
                _room_label = (_largest_room.get("room_name")
                               or _largest_room.get("room_id") or "largest room")
                # Auto-correct: flip ceiling to DRYFALL and add the SF.
                # This is the high-confidence default for >5,000 SF retail;
                # the RFI below asks the GC to confirm the assumption.
                _largest_room.setdefault("materials", {})
                _largest_room["materials"]["ceiling"] = "DRYFALL"
                _largest_room["materials"]["ceiling_painted"] = True
                _largest_room.setdefault("dimensions", {})
                _largest_room["dimensions"]["ceiling_area_sqft"] = _largest_fa
                _existing_note = _largest_room.get("notes") or ""
                _largest_room["notes"] = (
                    _existing_note + " [Auto-corrected: ACT → DRYFALL per "
                    "sales-floor heuristic; awaiting RFI confirmation]"
                ).strip()

                rfi_msg = (
                    f"The largest in-scope room ({_room_label}, "
                    f"{_largest_fa:,.0f} sqft) on this commercial/retail job "
                    f"was originally tagged with an ACT (suspended/acoustic) "
                    f"ceiling and NOT painted. Standalone retail boxes "
                    f">5,000 sqft almost always have OPEN-TO-DECK ceilings "
                    f"with paint-to-deck scope. We have ASSUMED open-to-deck "
                    f"and added ~{_largest_fa:,.0f} sqft of dryfall scope. "
                    f"Confirm whether the sales floor actually has ACT (in "
                    f"which case dryfall should be removed) or open-to-deck "
                    f"(scope as priced)."
                )
                analysis.setdefault("notes", []).append(
                    f"[Sales-Floor-ACT Check] {rfi_msg}")
                _existing_rfis = analysis.setdefault("_pre_pricing_rfis", [])
                _existing_rfis.append({
                    "category": "Scope Conflict",
                    "question": rfi_msg,
                    "action_required": (
                        "Confirm sales-floor ceiling type: ACT (remove "
                        "auto-added dryfall) vs. open-to-deck (scope as "
                        "priced)."
                    ),
                    "severity": "high",
                    "source": "sales_floor_act_heuristic",
                })
                print(f"\n⚠️  SALES-FLOOR-ACT HEURISTIC FIRED")
                print(f"   {_room_label}: {_largest_fa:,.0f} sqft, "
                      f"ceiling auto-flipped ACT → DRYFALL")
                print(f"   Added ~{_largest_fa:,.0f} sqft of paint-to-deck "
                      f"scope. RFI surfaced for GC confirmation.")
                # Recompute aggregated totals to pick up the new dryfall SF
                # before the cost calculation runs below.
                try:
                    _recalculate_totals(analysis)
                except Exception as _recalc_err:
                    print(f"   ⚠️  Recalc after ACT auto-flip failed: "
                          f"{_recalc_err}")

    # --- Pre-finalize sanity check ---
    # Backstop against missed-scope incidents (see Rider B&N regression):
    # for any commercial job where total extracted paintable surface is
    # implausibly small relative to building footprint, mark the analysis
    # for manual review BEFORE pricing runs. Pricing still proceeds (so
    # Rider sees something), but a high-severity flag bubbles into
    # validation, RFI, and the saved JSON for downstream UI to surface.
    #
    # Runs AFTER the sales-floor-ACT auto-flip so the totals reflect any
    # dryfall scope the auto-flip just recovered — otherwise the reason
    # text reports a stale low number (e.g. 16,930 SF / 0.9× footprint)
    # when the post-flip total is actually 28,584 SF / 1.6× and the flag
    # may not even need to fire.
    _sanity_pi = analysis.get("project_info", {}) or {}
    _sanity_bt = str(_sanity_pi.get("building_type", "")).lower()
    _sanity_is_commercial = any(kw in _sanity_bt for kw in (
        "commercial", "auto", "industrial", "warehouse",
        "retail", "dealership"))
    if _sanity_is_commercial:
        _sanity_agg = analysis.get("aggregated_totals", {}) or {}
        _sanity_ext = analysis.get("exterior", {}) or {}
        _wall = _num(_sanity_agg.get("total_paintable_wall_sqft", 0))
        # Canonical key is total_paintable_ceiling_sqft (set in _recalculate_totals
        # at line ~8339). The previous *_gyp_* variant was a typo that silently
        # zeroed the ceiling contribution, making the threshold tighter than
        # intended on borderline jobs.
        _ceil_gyp = _num(_sanity_agg.get("total_paintable_ceiling_sqft", 0))
        _ceil_dry = _num(_sanity_agg.get("total_dryfall_ceiling_sqft", 0))
        _ext_paint = _num(_sanity_ext.get("exterior_paint_sqft", 0))
        _ext_hardie = _num(_sanity_ext.get("hardie_siding_sqft", 0))
        _total_paintable = _wall + _ceil_gyp + _ceil_dry + _ext_paint + _ext_hardie
        _footprint = _num(_sanity_pi.get("footprint_sqft", 0))

        # FIRST CHECK — footprint missing entirely. This is the most
        # fundamental extraction failure: if we couldn't even determine
        # the building's gross footprint, every downstream sanity check
        # (which relies on footprint as the denominator) short-circuits
        # to "fine, no comparison possible" and the result silently
        # ships.
        #
        # Observed 2026-05-30 14:46 UTC on the Urban Air re-run after
        # the discipline-map fix: extraction now saw 6 FS-series sheets
        # (was 1) and produced 16 rooms (was 12), but `footprint_sqft`
        # came back None because no plan view yielded enough
        # geometry to derive it. The ratio check below short-circuited
        # because `_footprint > 1000` was False (None is falsy), the
        # manual_review_required flag stayed False, and a $17,302
        # estimate shipped to DN Contracting for a 173 MB Adventure
        # Park bid set. The extractor itself even wrote in the notes:
        # "NEXT STEPS: (1) Obtain and review A-series architectural
        # floor plans for room dimensions." That's a clear "extraction
        # is incomplete, do NOT ship" signal we need to honor.
        if not _footprint or _footprint <= 0:
            # Distinguish "footprint legitimately not derivable from the plans
            # but we still extracted a substantial, dimensioned takeoff" from a
            # genuinely thin/incomplete extraction. The latter MUST block
            # (Urban Air: 16 rooms, $17k, notes self-reported "obtain A-series
            # plans"); the former is better served by an RFI so a sound job
            # isn't needlessly held. NIGHTSHIFT_FOOTPRINT_RFI=1 enables the soft
            # path; default off preserves the current always-block behavior.
            _soft_footprint = (
                os.environ.get("NIGHTSHIFT_FOOTPRINT_RFI", "0") == "1")
            _rooms_with_dims = sum(
                1 for _f in (analysis.get("floors", []) or [])
                for _r in (_f.get("rooms", []) or [])
                if _r.get("in_scope", True)
                and _num((_r.get("dimensions") or {}).get(
                    "wall_area_sqft", 0)) > 0)
            try:
                _fp_min_rooms = int(os.environ.get(
                    "NIGHTSHIFT_FOOTPRINT_RFI_MIN_ROOMS", "20"))
            except (ValueError, TypeError):
                _fp_min_rooms = 20
            _notes_blob = " ".join(
                str(n) for n in (analysis.get("notes", []) or [])).lower()
            # The extractor's own "this is incomplete" admissions — if present,
            # always block regardless of room count (honors the Urban Air signal).
            _self_incomplete = any(kw in _notes_blob for kw in (
                "incomplete", "obtain and review", "not included",
                "next steps", "resubmit", "couldn't be parsed",
                "could not be parsed"))
            if (_soft_footprint and not _self_incomplete
                    and _rooms_with_dims >= _fp_min_rooms
                    and _total_paintable >= 10000):
                rfi_text = (
                    f"Building footprint could not be derived from the plans, "
                    f"but a substantial dimensioned takeoff was extracted "
                    f"({_rooms_with_dims} rooms with measured walls; "
                    f"{_total_paintable:,.0f} sqft paintable). Confirm the gross "
                    f"building footprint (or provide the A-series plan with a "
                    f"dimensioned outline) so the takeoff can be cross-checked "
                    f"against it.")
                analysis.setdefault("_pre_pricing_rfis", []).append({
                    "category": "Missing Information",
                    "question": rfi_text,
                    "action_required": (
                        "Provide/confirm gross building footprint to validate "
                        "the extracted takeoff."),
                    "severity": "medium",
                    "source": "footprint_unconfirmed_soft",
                })
                analysis.setdefault("notes", []).append(
                    f"[Footprint Unconfirmed] {rfi_text}")
                print(f"\n⚠️  No footprint, but {_rooms_with_dims} dimensioned "
                      f"rooms / {_total_paintable:,.0f} sqft extracted — "
                      f"surfaced as RFI instead of blocking.")
            else:
                flag_msg = (
                    f"[MANUAL REVIEW REQUIRED] Building footprint could not "
                    f"be determined from the extracted rooms (extracted "
                    f"paintable surface: {_total_paintable:,.0f} sqft). For "
                    f"a commercial building this means the architectural "
                    f"floor plans either weren't included in the extraction "
                    f"or their dimensions couldn't be parsed. Every "
                    f"downstream area/cost estimate has no anchor to verify "
                    f"against. Do NOT send this proposal without a reviewer "
                    f"confirming what's missing — and consider whether the "
                    f"customer needs to resubmit a different drawing set."
                )
                analysis["manual_review_required"] = True
                analysis["manual_review_reason"] = flag_msg
                analysis.setdefault("notes", []).append(flag_msg)
                print(f"\n🚨 PRE-FINALIZE SANITY CHECK FAILED — NO FOOTPRINT")
                print(f"   Extracted paintable: {_total_paintable:,.0f} sqft")
                print(f"   Footprint:           (missing — set to None/0)")
                print(f"   ⚠️  Flagged for manual review.")

        # Threshold: paintable_surface < footprint × 3 is structurally
        # implausible for a commercial building. A typical retail box:
        #   walls ≈ footprint × 0.4 (perimeter × 14ft / footprint)
        #   ceiling ≈ footprint × 0.9
        #   exterior ≈ footprint × 0.4
        # So footprint × 3 is conservative — most jobs land at 4-6×.
        elif _footprint > 1000 and _total_paintable < _footprint * 3:
            ratio = (_total_paintable / _footprint) if _footprint else 0
            flag_msg = (
                f"[MANUAL REVIEW REQUIRED] Total extracted paintable surface "
                f"({_total_paintable:,.0f} sqft) is implausibly low relative "
                f"to building footprint ({_footprint:,.0f} sqft) for a "
                f"commercial job — ratio is {ratio:.1f}× footprint, expected "
                f"3-6×. This usually means the finish schedule, exposed "
                f"structure / paint-to-deck scope, or exterior was missed "
                f"in extraction. Do NOT send this proposal without a senior "
                f"reviewer confirming the takeoff."
            )
            analysis["manual_review_required"] = True
            analysis["manual_review_reason"] = flag_msg
            analysis.setdefault("notes", []).append(flag_msg)
            print(f"\n🚨 PRE-FINALIZE SANITY CHECK FAILED")
            print(f"   Paintable: {_total_paintable:,.0f} sqft "
                  f"(walls {_wall:,.0f}, gyp ceil {_ceil_gyp:,.0f}, "
                  f"dryfall {_ceil_dry:,.0f}, ext {_ext_paint + _ext_hardie:,.0f})")
            print(f"   Footprint: {_footprint:,.0f} sqft "
                  f"(ratio {ratio:.1f}×, expected 3-6×)")
            print(f"   ⚠️  Flagged for manual review — proposal will print "
                  f"but should NOT be sent without reviewer sign-off.")

    # --- Calculate costs ---
    _update_progress(6, TOTAL_STEPS, "Calculating Costs", "Applying pricing model...")
    print("\n💰 Calculating costs...")
    costs = calculate_costs(
        analysis.get('aggregated_totals', {}),
        exterior=analysis.get('exterior', {}),
        building_type=analysis.get('project_info', {}).get('building_type', ''),
        project_info=analysis.get('project_info', {}),
        analysis=analysis,
        pricing_model_override=pricing_model_used,
    )

    print_estimate(analysis, costs)

    # --- Interactive adjustment mode ---
    adjustments_log = []
    if interactive:
        print("\n🔧 Entering interactive adjustment mode...")
        print("   You can modify pricing, measurements, counts, and scope.")
        analysis, costs, pricing_model_used, adjustments_log, regenerate = \
            interactive_adjustments(analysis, costs, pricing_model_used)
        if adjustments_log:
            analysis.setdefault("notes", []).append(
                f"[Adjustments] {len(adjustments_log)} manual adjustment(s) applied"
            )

    # --- Validate cost estimate ---
    validation = _validate_cost_estimate(analysis, costs)
    if validation["warnings"]:
        print(f"\n⚠️  VALIDATION: {validation['warning_count']} warning(s) "
              f"(quality score: {validation['data_quality_score']}/100)")
        for w in validation["warnings"]:
            sev = w["severity"].upper()
            print(f"   {sev}: {w['message']}")

    # --- Generate RFI items ---
    rfi_items = generate_rfi_items(analysis)

    # Merge in pre-pricing RFIs (e.g. sales-floor-ACT heuristic) emitted
    # before cost calculation so they reach Will Synthesis and the proposal.
    pre_pricing_rfis = analysis.pop("_pre_pricing_rfis", []) or []
    if pre_pricing_rfis:
        next_num = (max((r.get("number", 0) for r in (rfi_items or [])), default=0) + 1)
        for r in pre_pricing_rfis:
            r["number"] = next_num
            next_num += 1
        rfi_items = (rfi_items or []) + pre_pricing_rfis

    if rfi_items:
        print(f"\n📋 RFI: {len(rfi_items)} items requiring clarification")
        for rfi in rfi_items:
            q_preview = rfi['question'][:80] + ('...' if len(rfi['question']) > 80 else '')
            print(f"   {rfi['number']}. [{rfi['category']}] {q_preview}")

    # --- Will Synthesis: senior estimator review with bounded edit authority ---
    will_result = run_will_synthesis(
        analysis=analysis,
        cost_estimate=costs,
        rfi_items=rfi_items,
        validation=validation,
        client=client,
    )
    if will_result.get("will_synthesis"):
        will_rfis = will_result.get("new_rfis", [])
        next_num = (max((r.get("number", 0) for r in rfi_items), default=0) + 1
                    if rfi_items else 1)
        for rfi in will_rfis:
            rfi["number"] = next_num
            next_num += 1
        rfi_items = (rfi_items or []) + will_rfis

        for adj in will_result.get("adjustments_log", []):
            adjustments_log.append(
                f"Will adjusted {adj['category']}: "
                f"{adj['from_value']:,.0f} → {adj['to_value']:,.0f} "
                f"(${adj['delta_dollars']:+,.0f}) — {adj['reason']}"
            )

        validation = _validate_cost_estimate(analysis, costs)
    elif will_result.get("error"):
        analysis.setdefault("notes", []).append(
            f"[Will Synthesis] Skipped: {will_result['error']}"
        )

    # --- Save JSON ---
    _update_progress(7, TOTAL_STEPS, "Generating Report", "Saving JSON and creating PDF...")
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_json = os.path.join(output_dir, f"construction_analysis_{timestamp}.json")

    document_ref = ", ".join(os.path.basename(p) for p in pdf_paths)

    # Extract chunk tracking from analysis if present (don't leak internals)
    chunk_tracking = analysis.pop("_chunk_tracking", None)

    result_data = {
        "contact": {"name": contact_name, "email": contact_email},
        "document": document_ref,
        "source_files": [os.path.basename(p) for p in pdf_paths] if multi_mode else None,
        "files_analyzed": files_analyzed if multi_mode else None,
        "files_skipped": files_skipped if (multi_mode and files_skipped) else None,
        "image_fallback_files": image_fallback_files if image_fallback_files else None,
        "schedule_estimated_files": schedule_estimated_files if schedule_estimated_files else None,
        "chunk_tracking": chunk_tracking,
        "generated": datetime.now().isoformat(),
        "scope_notes": scope_notes if scope_notes else None,
        "building_inventory": building_inventory if building_inventory else None,
        # Top-level mirror of pre-finalize sanity check so the UI / email
        # layer can gate proposal sending without descending into `analysis`.
        "manual_review_required": bool(analysis.get("manual_review_required")),
        "manual_review_reason": analysis.get("manual_review_reason"),
        "analysis": analysis,
        "cost_estimate": costs,
        "labor_hours_estimate": _compute_labor_hours(analysis),
        "validation": validation,
        "pricing_model": pricing_model_used if pricing_model_used else PRICING_MODEL,
        "adjustments_applied": adjustments_log if adjustments_log else None,
        "rfi_items": rfi_items if rfi_items else None,
        "will_synthesis": will_result.get("will_synthesis"),
        "will_adjustments_log": will_result.get("adjustments_log"),
        "will_rejected_log": will_result.get("rejected_log"),
    }

    with open(output_json, 'w') as f:
        json.dump(result_data, f, indent=2)

    print(f"\n📁 JSON saved to: {output_json}")

    # --- Generate PDF report ---
    output_pdf = output_json.replace('.json', '.pdf')
    try:
        # Import json_to_pdf from the same directory
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from json_to_pdf import json_to_pdf as generate_pdf_report
        generate_pdf_report(output_json, output_pdf)
        print(f"📄 PDF report saved to: {output_pdf}")
    except Exception as e:
        print(f"⚠️  Could not generate PDF report: {e}")
        output_pdf = None

    _update_progress(8, TOTAL_STEPS, "Complete", "Estimate ready!", pct=100)
    print(f"\n✅ ESTIMATE COMPLETE!")

    return {
        "analysis": analysis,
        "cost_estimate": costs,
        "output_json_path": output_json,
        "output_pdf_path": output_pdf,
        "contact": {"name": contact_name, "email": contact_email},
        "document": document_ref,
        "rfi_items": rfi_items,
        "adjustments_applied": adjustments_log if adjustments_log else None,
        "will_synthesis": will_result.get("will_synthesis"),
    }


def main():
    """Main CLI entry point — parses args and delegates to run_analysis()."""

    _run_start = time.time()

    if len(sys.argv) < 4:
        print("Nightshift AI - Construction Document Analyzer")
        print("\nUsage (single file):")
        print('  python3 Takeoff_DIRECT.py --rfp_file "file.pdf" --contact_name "Name" --contact_email "email"')
        print("\nUsage (folder of split PDFs):")
        print('  python3 Takeoff_DIRECT.py --rfp_dir "/path/to/folder/" --contact_name "Name" --contact_email "email"')
        print("\nOptional flags:")
        print('  --scope "Residential floors 2-4 only, skip basement and commercial"')
        print('  --corrections "/path/to/corrections.json"')
        print('  --cache           Enable caching (disabled by default until accuracy target met)')
        print('  --clear-cache    Delete cache for this PDF, then run fresh')
        print('  --multi-pass     Run floor plan files twice, keep best extraction')
        print('  --image-fallback  Render floor plans as images when native PDF returns 0 rooms (default: ON)')
        print('  --no-image-fallback  Disable image fallback')
        print('  --schedule-estimation  Estimate from Room Finish Schedules when floor plans missing (default: ON)')
        print('  --no-schedule-estimation  Disable schedule-based estimation')
        print("\nAdjustment flags (pre-run rate overrides):")
        print('  --interactive         Enable post-run interactive adjustment menu')
        print('  --wall-rate 1.50      Override gyp wall rate ($/sqft)')
        print('  --ceiling-rate 1.50   Override ceiling rate ($/sqft)')
        print('  --door-rate 200       Override door (full paint) rate ($/ea)')
        print('  --window-rate 425     Override window rate ($/ea)')
        print('  --trim-rate 3.25      Override base trim rate ($/LF)')
        print('  --stair-rate 1500     Override stair rate ($/ea)')
        print('  --markup 0.08         Override global markup (decimal, e.g. 0.08 = 8%)')
        sys.exit(1)

    # Parse boolean flags (no value) separately from key-value pairs
    bool_flags = set()
    args = {}
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ('--cache', '--no-cache', '--clear-cache', '--multi-pass',
                    '--image-fallback', '--no-image-fallback',
                    '--schedule-estimation', '--no-schedule-estimation',
                    '--interactive'):
            bool_flags.add(arg[2:])  # strip '--'
            i += 1
        elif arg.startswith('--') and i + 1 < len(sys.argv):
            args[arg[2:]] = sys.argv[i + 1]
            i += 2
        else:
            i += 1

    contact_name = args.get('contact_name')
    contact_email = args.get('contact_email')
    rfp_dir = args.get('rfp_dir', '').strip("'\" ") or None
    rfp_file = args.get('rfp_file', '').strip("'\" ") or None
    scope_notes = args.get('scope', '')
    corrections_path = args.get('corrections')
    # Caching disabled by default until accuracy reaches 10% confidence target.
    # Use --cache to opt in, or re-enable by changing default in run_analysis().
    use_cache = 'cache' in bool_flags and 'no-cache' not in bool_flags
    multi_pass = 'multi-pass' in bool_flags
    image_fallback = 'no-image-fallback' not in bool_flags  # default ON
    schedule_estimation = 'no-schedule-estimation' not in bool_flags  # default ON
    interactive = 'interactive' in bool_flags

    # Build rate_overrides dict from CLI flags
    rate_overrides = {}

    # Load from JSON file/string first (--rate-overrides-json)
    _ro_json = args.get('rate-overrides-json', '')
    if _ro_json:
        try:
            if os.path.exists(_ro_json):
                with open(_ro_json) as f:
                    rate_overrides = json.load(f)
            else:
                rate_overrides = json.loads(_ro_json)
        except (json.JSONDecodeError, IOError) as e:
            print(f"⚠️  Could not parse --rate-overrides-json: {e}")

    # Individual CLI flags override JSON values
    _rate_flag_map = {
        'wall-rate': 'wall_rate',
        'ceiling-rate': 'ceiling_rate',
        'door-rate': 'door_rate',
        'window-rate': 'window_rate',
        'trim-rate': 'trim_rate',
        'stair-rate': 'stair_rate',
        'markup': 'markup',
    }
    for flag, key in _rate_flag_map.items():
        if flag in args:
            try:
                rate_overrides[key] = float(args[flag])
            except ValueError:
                print(f"⚠️  Invalid value for --{flag}: {args[flag]} (must be a number)")
    if not rate_overrides:
        rate_overrides = None

    # Build list of PDFs to process
    if rfp_dir:
        pdf_files = sorted(glob.glob(os.path.join(rfp_dir, '*.pdf')))
        if not pdf_files:
            pdf_files = sorted(glob.glob(os.path.join(rfp_dir, '*.PDF')))
        if not pdf_files:
            print(f"❌ No PDF files found in: {rfp_dir}")
            sys.exit(1)
    elif rfp_file:
        pdf_files = [rfp_file]
    else:
        print("❌ Provide --rfp_file or --rfp_dir")
        sys.exit(1)

    # Clear cache if requested
    if 'clear-cache' in bool_flags:
        import shutil
        for p in pdf_files:
            cd, _ = _cache_dir_for(p)
            if cd.exists():
                shutil.rmtree(cd)
                print(f"🗑️  Cache cleared for {os.path.basename(p)}")
        use_cache = True  # run fresh but re-populate cache

    try:
        run_analysis(pdf_files, contact_name, contact_email,
                     scope_notes=scope_notes, corrections_path=corrections_path,
                     use_cache=use_cache, multi_pass=multi_pass,
                     image_fallback=image_fallback,
                     schedule_estimation=schedule_estimation,
                     rate_overrides=rate_overrides,
                     interactive=interactive)

        _elapsed = time.time() - _run_start
        _mins, _secs = divmod(int(_elapsed), 60)
        print(f"\n⏱️  Total runtime: {_mins}m {_secs}s ({_elapsed:.1f}s)")
        try:
            _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
            os.makedirs(_log_dir, exist_ok=True)
            _pdf_names = ",".join(os.path.basename(p) for p in pdf_files)
            with open(os.path.join(_log_dir, 'runtime.log'), 'a') as _rt:
                _rt.write(f"{datetime.now().isoformat()}\tduration_s={_elapsed:.1f}\tpdfs={_pdf_names}\n")
        except OSError:
            pass

    except anthropic.RateLimitError:
        print("\n❌ API rate limit exceeded after multiple retries")
        print("   Your plan allows 30,000 input tokens/minute.")
        print("   Try again in a few minutes, or contact Anthropic to increase your limit.")
        sys.exit(1)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
