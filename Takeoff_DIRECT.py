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
from config import CLAUDE_API_KEY, PRICING_MODEL
import anthropic
import base64
from datetime import datetime
import os


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

# Footprint-based estimation constants (calibrated to Rider Painting / Chestnut)
# Paintable residential unit area as fraction of gross floor area.
# Excludes corridors, mechanical, structure, retail — which are NOT painted.
RESIDENTIAL_EFFICIENCY = 0.63
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


def _retry_chunk_without_bad_pages(chunk_path, call_api_fn, chunk_label=""):
    """
    When a multi-page chunk fails with 'Could not process PDF', test each
    page individually, discard bad pages, reassemble the good ones, and retry.

    Args:
        chunk_path:   Path to the chunk PDF that failed.
        call_api_fn:  Callable(base64_str, label="") -> response_text.
        chunk_label:  Human-readable label for logging.

    Returns:
        str or None — API response text from the cleaned chunk,
                      or None if no good pages remain.
    """
    try:
        reader = PyPDF2.PdfReader(chunk_path)
    except Exception:
        print(f"   ⚠️  Could not read chunk for page-level retry")
        return None

    total_pages = len(reader.pages)
    if total_pages <= 1:
        print(f"   ⚠️  Single-page chunk failed — skipping this page")
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
            bad_page_nums.append(i + 1)
            print(f"      Page {i+1}: ❌ skipped (unreadable)")

        except Exception as e:
            # Non-PDF error (rate limit, network, overloaded) — skip page
            # rather than crashing entire file analysis
            bad_page_nums.append(i + 1)
            print(f"      Page {i+1}: ⚠️  skipped ({type(e).__name__}: {str(e)[:80]})")

        finally:
            if single_path:
                try:
                    os.unlink(single_path)
                except Exception:
                    pass

    if bad_page_nums:
        print(f"   🗑️  Removed bad pages: {bad_page_nums}")

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

    except anthropic.BadRequestError as e:
        print(f"   ⚠️  Cleaned chunk still fails — {e}")
        # Last resort: send each page individually and combine results
        print(f"   🔄 Falling back to single-page processing for {chunk_label}")
        page_results = []
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
            except Exception as page_err:
                print(f"      Page {page_idx+1}: ❌ {page_err}")
            finally:
                if single_tmp:
                    try:
                        os.unlink(single_tmp)
                    except Exception:
                        pass
        if page_results:
            print(f"   ✅ Recovered {len(page_results)}/{len(good_pages)} pages individually")
            return "\n".join(page_results)
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

    # --- Quick path: try the (filtered) file ---
    with open(effective_path, 'rb') as f:
        raw = f.read()
    pdf_data = base64.standard_b64encode(raw).decode("utf-8")

    if len(pdf_data) <= MAX_B64_BYTES:
        print(f"✅ PDF loaded ({len(pdf_data)/1024/1024:.1f} MB encoded)")
        _cleanup_filtered_tmp(_filtered_tmp_path)
        return pdf_data

    # --- Large file: split into 5-page chunks ---
    try:
        reader = PyPDF2.PdfReader(effective_path)
        total_pages = len(reader.pages)
    except Exception:
        # Can't read with PyPDF2 — send raw
        print(f"✅ PDF loaded ({len(pdf_data)/1024/1024:.1f} MB encoded, could not split)")
        _cleanup_filtered_tmp(_filtered_tmp_path)
        return pdf_data

    raw_mb = len(pdf_data) / 1024 / 1024
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
        return pdf_data

    # First chunk is returned for the primary API call
    first_path, first_offset = chunk_info[0]
    with open(first_path, 'rb') as f:
        first_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    os.unlink(first_path)

    # Remaining chunks are stored for the caller to process
    _pending_chunks = chunk_info[1:]  # list of (path, start_page_1based)
    _chunk_page_offsets = [first_offset] + [off for _, off in chunk_info[1:]]

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

        # Also check for high density of sheet number patterns (A-101, A-102, etc.)
        # which indicates a drawing index even without explicit "drawing index" title
        sheet_refs = re.findall(r'[A-Z]{1,2}\s*[-.]?\s*\d{2,3}', text)
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
]

# Sheet prefix → discipline mapping (order matters: check longer prefixes first)
_DISCIPLINE_MAP = [
    # Included disciplines (painting-relevant)
    ('AD', 'Architectural Demo', True),
    ('AI', 'Architectural Interiors', True),
    ('ID', 'Interior Design', True),
    ('A',  'Architectural', True),
    ('G',  'General', True),
    ('T',  'Title', True),
    # Excluded disciplines
    ('FP', 'Fire Protection', False),
    ('FA', 'Fire Alarm', False),
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

    doc = fitz.open(pdf_path)
    classifications = []
    g_t_count = 0  # Track how many General/Title pages we've included

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_rect = page.rect
        page_w = page_rect.width
        page_h = page_rect.height

        # --- Strategy: extract text from title block region first ---
        # Title blocks are typically in the bottom-right ~40% width × 20% height
        title_block_rect = fitz.Rect(
            page_w * 0.60, page_h * 0.80,
            page_w, page_h
        )
        title_text = page.get_text(clip=title_block_rect).strip()

        # Also get bottom 30% for fallback
        bottom_rect = fitz.Rect(0, page_h * 0.70, page_w, page_h)
        bottom_text = page.get_text(clip=bottom_rect).strip()

        # Full page text (for Division 9 keyword scan)
        full_text = page.get_text().strip()
        full_text_lower = full_text.lower()

        # --- Find sheet number ---
        sheet_number = None
        discipline_prefix = None

        # Search in priority order: title block → bottom 30% → full page
        for search_text in [title_text, bottom_text, full_text]:
            if sheet_number:
                break
            for m in _SHEET_NUMBER_RE.finditer(search_text):
                prefix = m.group(1).upper()
                number = m.group(2)
                # Validate: prefix must be a known discipline letter
                known = any(prefix == dp or prefix.startswith(dp)
                            for dp, _, _ in _DISCIPLINE_MAP)
                if known:
                    sheet_number = f"{prefix}{number}"
                    discipline_prefix = prefix
                    break

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
    return classifications


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


def _render_pages_to_images(pdf_path, page_numbers, dpi=250):
    """
    Render specific PDF pages to PNG images at the given DPI using PyMuPDF.
    Returns list of (page_num_0based, base64_png_string) tuples.
    At 250 DPI a letter page is ~2080×2690 px (~300-500 KB PNG).
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
        png_bytes = pix.tobytes("png")
        b64 = base64.standard_b64encode(png_bytes).decode("utf-8")
        images.append((page_num, b64))
        print(f"      📸 Rendered page {page_num + 1} → "
              f"{pix.width}×{pix.height} px ({len(png_bytes)/1024:.0f} KB)")

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


def _enhance_image_for_extraction(png_bytes):
    """
    Apply contrast enhancement and sharpening to a rendered PDF page image.
    Architectural drawings benefit from increased contrast (thin lines on white)
    and slight sharpening to make text/dimensions more legible.

    Args:
        png_bytes: raw PNG bytes
    Returns:
        enhanced PNG bytes
    """
    try:
        from PIL import Image, ImageEnhance
        import io

        Image.MAX_IMAGE_PIXELS = None  # architectural sheets are large
        img = Image.open(io.BytesIO(png_bytes))

        # Increase contrast by 1.3x — makes thin architectural lines pop
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.3)

        # Sharpen slightly — helps dimension text legibility
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(1.5)

        # Save back to PNG bytes
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except (ImportError, Exception):
        # PIL not available or image processing failed — return original
        return png_bytes


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


def _analyze_floor_plan_as_images(client, pdf_path, scope_notes="",
                                   schedule_hints=None, building_inventory=None):
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

    # Render all pages as images
    page_numbers = list(range(total_pages))
    print(f"\n   🖼️  IMAGE FALLBACK: Rendering {total_pages} page(s) "
          f"at {IMAGE_FALLBACK_DPI} DPI...")
    images = _render_pages_to_images(pdf_path, page_numbers,
                                      dpi=IMAGE_FALLBACK_DPI)

    if not images:
        print(f"   ❌ Image fallback: no pages rendered")
        return None

    # Safety check: auto-reduce DPI if any page exceeds Claude's 8000px limit
    MAX_DIMENSION = 7999
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
                reduced_dpi = int(IMAGE_FALLBACK_DPI * scale)
                print(f"      ⚠️  Page {page_num + 1} is {w}×{h}px — "
                      f"re-rendering at {reduced_dpi} DPI")
                images = _render_pages_to_images(pdf_path, page_numbers,
                                                  dpi=reduced_dpi)
                break  # re-rendered all pages at lower DPI
        except ImportError:
            pass  # can't check dimensions without PIL, proceed anyway

    # Optional image enhancement
    if IMAGE_FALLBACK_ENHANCE:
        enhanced_images = []
        for page_num, b64_data in images:
            raw_bytes = base64.standard_b64decode(b64_data)
            enhanced_bytes = _enhance_image_for_extraction(raw_bytes)
            enhanced_b64 = base64.standard_b64encode(
                enhanced_bytes).decode("utf-8")
            enhanced_images.append((page_num, enhanced_b64))
        images = enhanced_images

    # Build the same extraction prompt used by the PDF path
    effective_prompt = _build_extraction_prompt(
        scope_notes=scope_notes, schedule_hints=schedule_hints,
        building_inventory=building_inventory)

    # Batch images to avoid 413 Payload Too Large errors.
    # Max ~6 full-page images per API call (each ~1-3 MB at 190 DPI).
    MAX_IMAGES_PER_CALL = 6
    image_batches = []
    for i in range(0, len(images), MAX_IMAGES_PER_CALL):
        image_batches.append(images[i:i + MAX_IMAGES_PER_CALL])

    all_batch_results = []

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
                    "media_type": "image/png",
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
                    model="claude-sonnet-4-20250514",
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
    return (pdf_path, final)


def _analyze_with_enhanced_extraction(client, pdf_path, scope_notes="",
                                       schedule_hints=None, building_inventory=None,
                                       page_indices=None):
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
    MAX_PAGES_PER_CALL = 12
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
            if min(w_in, h_in) > 36:
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
    MAX_TILES_PER_CALL = 8  # ~2 pages of 4 tiles each; keeps payload under API limit
    tile_batches = []
    for i in range(0, len(all_tiles), MAX_TILES_PER_CALL):
        tile_batches.append(all_tiles[i:i + MAX_TILES_PER_CALL])

    # Free the master list now — batches hold references, all_tiles just held duplicates
    del all_tiles

    all_analysis_results = []

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
            text_layer_context=text_context)

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
                    model="claude-sonnet-4-20250514",
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
        if not result_text:
            continue

        # Parse JSON response
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            try:
                analysis = json.loads(json_match.group())
                rooms = analysis.get('project_info', {}).get('total_rooms_found', 0)
                print(f"   🔬 Batch {batch_idx + 1}: extracted {rooms} rooms")
                if rooms > 0:
                    all_analysis_results.append(analysis)
            except json.JSONDecodeError:
                print(f"   ❌ Enhanced extraction batch: could not parse JSON")

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
3. Read the FRAME material, TYPE, FINISH, and NOTES columns carefully
4. Determine if the interior frame is painted:
   • PAINTED INTERIOR (count as windows_painted_interior):
     - Schedule explicitly says "painted", "paint", "black", or "color" for interior finish
     - Frame material is WOOD (WD) — wood frames require painting
     - Notes or finish column mentions "field painted" or "prime and paint"
   • NOT PAINTED INTERIOR (do not count):
     - Factory-finished aluminum, vinyl, fiberglass, or clad windows
     - Double-hung / single-hung with no paint specification (factory finish is default)
     - Storefronts (SF-prefix) — NEVER painted interior
     - Fire-rated windows (FYRE-TEC or similar) — factory finish unless noted otherwise
     - "Pre-treated", "shop finish", "shop finished", "shop painted" — window was
       finished/painted off-site at the manufacturer, no field painting needed
     - "Pre-finished", "factory painted", "factory finish" — same as above
     - Any reference to off-site finishing means the window is NOT painted in the field
   • BE CONSERVATIVE: If there is NO explicit paint specification for interior,
     the window is NOT painted interior.  Most modern windows come factory-finished.
5. Calculate total: type_qty × number_of_marks, or count each row individually

IMPORTANT: Do NOT assume residential windows are painted unless the schedule says so.
Most modern residential windows are factory-finished vinyl or aluminum-clad.
Only count windows as painted if you see explicit evidence in the schedule.

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
      {"mark": "W1", "qty": 10, "frame": "wood", "painted_interior": true},
      {"mark": "W2", "qty": 5, "frame": "aluminum", "painted_interior": false}
    ],
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
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            temperature=0,
            timeout=300.0,  # 5 min timeout
            messages=[{"role": "user", "content": content}],
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)
    except Exception as e:
        print(f"   ❌ Schedule image API call failed: {e}")
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


def _floor_room_count(floor):
    """Total effective rooms on a floor accounting for unit_multiplier."""
    total = 0
    for r in floor.get("rooms", []):
        m = r.get("unit_multiplier")
        total += int(m) if isinstance(m, (int, float)) and m > 1 else 1
    return total


def _floor_total_wall_area(floor):
    """Total wall area on a floor accounting for unit_multiplier."""
    total = 0
    for r in floor.get("rooms", []):
        m = r.get("unit_multiplier")
        mult = int(m) if isinstance(m, (int, float)) and m > 1 else 1
        total += r.get("dimensions", {}).get("wall_area_sqft", 0) * mult
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


def _merge_chunk_responses(texts, page_offsets=None):
    """
    Merge multiple JSON response texts (from chunked PDF processing) into
    a single combined JSON string.  Each chunk may contain partial floor/room
    data; this function combines all floors and rooms, sums aggregated totals,
    and returns the merged JSON as a string.

    When two chunks describe the same physical floor with different names
    (e.g. "Basement" vs "Foundation/Basement"), the merger keeps the version
    with more rooms/data (usually from the actual floor plan sheet rather than
    a demolition or code compliance sheet).

    page_offsets: optional list of 1-based page offsets for each chunk,
                  used to fix source_page values (which Claude reports relative
                  to each chunk rather than the original PDF).
    """
    parsed = []
    for t in texts:
        m = re.search(r'\{.*\}', t, re.DOTALL)
        if m:
            try:
                parsed.append(json.loads(m.group()))
            except json.JSONDecodeError:
                pass

    if not parsed:
        return texts[0]  # nothing could be parsed — return first raw text

    # Apply page offsets to source_page values (Claude reports page numbers
    # relative to each chunk; we need them relative to the original PDF)
    if page_offsets and len(page_offsets) >= len(parsed):
        for chunk_idx, chunk_data in enumerate(parsed):
            offset = page_offsets[chunk_idx] - 1  # convert to 0-based addition
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
                    cext[key] = max(cext.get(key, 0), val)
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
                              building_inventory=None, text_layer_context=None):
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
            "KNOWN SCHEDULE DATA (pre-extracted from door/window schedule images):",
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
            hint_parts.append(f"- Windows with painted interior: {w_painted}")
        s_total = si.get("total_stair_sections", 0) or 0
        if s_total:
            hint_parts.append(f"- Total stair flight sections: {s_total}")
        hint_parts.append(
            "Use these totals as REFERENCE when assigning doors/windows to rooms.")
        hint_parts.append(
            "Your per-room counts should approximately SUM to these schedule totals.")
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
- Number of stories (from building sections, drawing index, title sheets, or notes)
- Number of units/apartments (from unit schedules, floor plans, light/ventilation tables)
- Total building footprint (from site plan or floor plan dimensions, e.g. 133' x 70')
- Presence of commercial/retail spaces on ground floor
Report this in project_info as additional fields: "building_type", "total_stories",
"total_units", "footprint_sqft"
This classification is CRITICAL — a 20-unit mixed-use building requires extracting 100+
rooms across multiple floors, not just 10-20 rooms like a single-family home.

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
- Read ceiling height callouts (CLG HT: 9'-0")
- Calculate: Wall Area = Perimeter × Ceiling Height
- Calculate: Ceiling Area = Length × Width (only if ceiling_painted = true)
- Base trim LF = room perimeter (assume base trim in ALL rooms with gyp walls)
- Level 5 finish: Check the FINISH SCHEDULE, wall type legends, and room notes for "Level 5",
  "Level 5 skim coat", "L5", "smooth finish", or "skim coat" specifications on any wall or ceiling.
  Common locations: entryways, foyers, hallways, great rooms, formal dining rooms (especially in
  high-end single-family homes). If found, set "level_5_finish_sqft" to 1 (it's priced per
  occurrence, not per sqft). Set to 0 if not specified anywhere in the documents.
- Concrete floor sealer: if a room has a concrete floor that requires sealer, record the floor area
  in "concrete_floor_sqft". This includes:
  * Residential: garages, parking, basements with concrete floors
  * Commercial: service bays, service areas, parts rooms, parts receiving, mechanical rooms,
    warehouse areas, receiving docks, maintenance areas — ANY back-of-house area with concrete floors
  * Check the FINISH SCHEDULE "Floor Finish" column for concrete, sealed concrete, or epoxy coating
  * When the room is a service/parts/mechanical area, DEFAULT to concrete floor unless finish
    schedule explicitly shows a different floor type (carpet, tile, VCT, etc.)
  Set concrete_floor_sqft = floor_area_sqft for qualifying rooms. Set to 0 if not a concrete floor.

Columns: Count painted structural columns visible on floor plans.
  - ONLY count columns marked with paint references (PT-?, "painted columns",
    "paint all exposed columns", column finish schedule showing paint)
  - Do NOT count columns inside walls or columns with no paint callout
  - Record as "painted_columns_ea" per room (set to 0 if none)

Wallcovering: CRITICAL — Check the ROOM FINISH SCHEDULE "Wall Finish" column for EVERY room.
  - Look at the Room Finish Schedule table (usually sheet A-601 or in the finish schedule area).
    Each row lists a room number and its wall finish type. If the "Wall Finish" column says
    WC-1, WC-2, WC-3, WC-5, WC-6, "wallcovering", "vinyl wallcovering", or any WC-x code,
    those walls get wallcovering INSTEAD of paint.
  - For rooms with wallcovering walls:
    * Calculate: wallcovering_sqft = wallcovering wall LF × wall height
    * If ALL walls in a room have wallcovering: wallcovering_sqft = perimeter × ceiling height,
      and wall_area_sqft should be REDUCED by the same amount (those walls are NOT painted)
    * If SOME walls have wallcovering and some have paint: split accordingly
  - SUBTRACT wallcovering walls from wall_area_sqft — DO NOT double-count as both paint and WC
  - Record wallcovering area as "wallcovering_sqft" per room (set to 0 if no wallcovering)
  - This is a MAJOR cost item ($9/sqft) — missing wallcovering can cause 30%+ estimate error
  - In commercial buildings, wallcovering is common in showrooms, lobbies, corridors, and
    customer-facing areas

Stained Wood / Clear-Coat Panels: Check finish schedules and interior elevations for:
  - Stained wood panels, wood veneer panels, clear-coated wood
  - Finish codes like WD-1, WD-2, ST-1, "stain", "clear coat", "natural finish", "oak panel"
  - These are NOT painted — they require stain/clear-coat application ($6/sqft)
  - Calculate: stained_wood_sqft = panel area (height × width per panel × count)
  - SUBTRACT stained wood walls from wall_area_sqft — do NOT double-count as painted
  - Record as "stained_wood_sqft" per room (set to 0 if none)
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

STEP 5: WINDOWS — CRITICAL DISTINCTION
- Count ALL windows visible as "windows_total"
- Count ONLY windows requiring INTERIOR painting as "windows_painted_interior"
- Factory-finished aluminum/vinyl storefront frames do NOT get painted → do NOT count
- Wood-framed windows or windows with "painted", "black", or colored interior finishes DO count
- If the PDF includes a WINDOW SCHEDULE, read it to determine frame materials and finishes
  (look for "painted interior", "wood frame", "black finish", "WD" material codes)
- If no window schedule exists AND frame material is completely unclear → set windows_painted_interior = 0
  and add a note
- Residential unit windows are more likely to be painted interior than commercial storefront windows
CRITICAL WINDOW SCHEDULE RULE: If a WINDOW SCHEDULE exists, the total window count across
the ENTIRE building comes from that schedule — do NOT estimate or guess window counts per room.
Count the total painted-interior windows from the schedule, then distribute them across rooms.
Example: If the window schedule lists 26 windows total with wood frames requiring paint,
and you have 20 apartments across 2 floors, that's roughly 1-2 windows per apartment —
NOT 2-3 per room. Use the schedule total as your ceiling.

STEP 6: STAIRS — COUNT ACROSS ENTIRE BUILDING
- Count TOTAL stair flight sections in the entire building (1 section = one run between landings)
- A stair running from Floor 1 to Floor 3 has MULTIPLE sections (typically 2 per floor transition)
- A 4-story building with 2 stairwells typically has 2 stairs × ~3 floor transitions × ~2 flights = ~8-12 sections
- Only count stairs with painted components (wood treads, risers, railings, stringers)
- Estimate gyp wall area between/around ALL stair runs as "gyp_between_stairs_sqft"
- Include landings in the gyp wall area calculation
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
- Railings on balconies, decks, roof areas → estimate LF
- SIDING / CLADDING MATERIALS — identify from elevation notes, details, and material callouts:
  * Hardie / fiber cement siding → measure total SQFT from elevation drawings ("hardie_siding_sqft")
  * Azek / PVC trim boards → measure total LF of trim runs ("azek_trim_lf")
  * Corner boards (Azek or wood) → count building corners × height, total LF ("corner_board_lf")
  * Steel exposed lintels above windows/doors → count and measure total LF ("steel_lintel_lf")
  * Set "exterior_siding_type" to primary cladding name (e.g. "hardie", "vinyl", "wood", "stucco")
  * When material-specific siding is identified, do NOT also include that area in exterior_paint_sqft
    (exterior_paint_sqft is ONLY for generic painted surfaces not covered by a specific material item)
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
    "footprint_sqft": 10000
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
            "ceiling_painted": true
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
            "soffit_sqft": 0
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
    "total_dryfall_ceiling_sqft": 0,
    "total_base_trim_lf": 0,
    "total_doors_full_paint": 0,
    "total_doors_hm_panel": 0,
    "total_doors_frame_only": 0,
    "total_windows_painted_interior": 0,
    "total_windows_all": 0,
    "total_stair_sections": 0,
    "total_gyp_between_stairs_sqft": 0,
    "total_level_5_finish_sqft": 0,
    "total_concrete_floor_sqft": 0,
    "total_painted_columns_ea": 0,
    "total_wallcovering_sqft": 0,
    "total_stained_wood_sqft": 0,
    "total_soffit_sqft": 0
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
- Windows: check window schedule for painted specs; default to 0 only if NO schedule exists
- Door schedules override floor plan counts
- Include ALL hallways, corridors, lobbies, and common areas
- Base trim in EVERY room with gyp walls, even if not explicitly called out
- Break EVERY apartment into individual rooms — never list just "Living/Dining/Kitchen"
- Extract ALL closets as separate rooms: linen closets, coat closets, pantry closets,
  utility closets, walk-in closets, storage closets. These are commonly missed but
  contribute meaningful ceiling and wall area. Each closet needs its own dimensions,
  ceiling_painted=true, and base_trim_lf.
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
    if building_inventory and building_inventory.get("buildings"):
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

    return prompt


def analyze_construction_pdf(client, pdf_path, scope_notes="", schedule_hints=None,
                             building_inventory=None):
    """
    Send PDF directly to Claude for analysis.
    Claude can read PDFs natively without conversion.
    """

    print(f"\n📄 Reading PDF file...")
    pdf_data = _load_pdf_for_api(pdf_path, _client_for_validation=client)

    prompt = _build_extraction_prompt(scope_notes=scope_notes, schedule_hints=schedule_hints,
                                      building_inventory=building_inventory)
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
                    model="claude-sonnet-4-20250514",
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
        try:
            result_text = _call_api(pdf_data)
        except anthropic.BadRequestError as e:
            if "Could not process" in str(e):
                print(f"   ⚠️  First chunk failed — attempting page-level retry")
                # Reconstruct a temp file from base64 so retry helper can read pages
                raw_bytes = base64.standard_b64decode(pdf_data)
                first_tmp = None
                try:
                    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                        tmp.write(raw_bytes)
                        first_tmp = tmp.name
                    result_text = _retry_chunk_without_bad_pages(
                        first_tmp, _call_api, chunk_label="chunk 1"
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
                raise  # Not a "Could not process PDF" error

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

            for idx, chunk_info in enumerate(_pending_chunks, 2):
                chunk_path, _chunk_start = chunk_info if isinstance(chunk_info, tuple) else (chunk_info, 1)

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
                    if "Could not process" in str(e) or "500" in str(e):
                        print(f"   ⚠️  Chunk {idx}/{total_chunks} failed ({str(e)[:120]}) — attempting page-level retry")
                        retry_result = _retry_chunk_without_bad_pages(
                            chunk_path, _call_api, chunk_label=f"chunk {idx}/{total_chunks}"
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
                except anthropic.InternalServerError as chunk_err:
                    # Overloaded or 500 after all retries exhausted — skip chunk, don't crash
                    print(f"   ⚠️  Chunk {idx}/{total_chunks} skipped (API overloaded after retries): {str(chunk_err)[:100]}")
                    chunks_failed.append(idx)
                except Exception as chunk_err:
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
                result_text = _merge_chunk_responses(all_texts, page_offsets=_chunk_page_offsets)
            elif len(all_texts) == 1:
                result_text = all_texts[0]
            else:
                raise RuntimeError("All PDF chunks failed — no data could be extracted")

            # Inject chunk tracking metadata into the merged result
            try:
                merged_data = json.loads(re.search(r'\{.*\}', result_text, re.DOTALL).group())
                merged_data["_chunk_tracking"] = {
                    "total_chunks": total_chunks,
                    "chunks_succeeded": chunks_succeeded,
                    "chunks_failed": chunks_failed,
                }
                result_text = json.dumps(merged_data)
            except (json.JSONDecodeError, AttributeError):
                pass  # if we can't parse, don't break the flow

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
     * Wood doors (WD1, WD2) — typically pre-finished per manufacturer
     * Glass doors (GL1, GL-1) — not painted
     * Doors marked "NOT USED" or "NIC" — skip entirely
   - Count ALL qualifying doors in the schedule across ALL floors
   - If the schedule has columns for "Material" or "Type", use those to classify
   - For commercial buildings, most painted doors are HM (Hollow Metal) type

2. WINDOW SCHEDULE — BE CONSERVATIVE on painted interior counts:
   - Count total windows in the schedule
   - Count ONLY windows requiring INTERIOR PAINTING — row by row:
     * DO NOT assume all windows are painted just because the spec says "painted finish"
     * Only count windows where the INTERIOR FRAME is WOOD and explicitly marked for paint
     * Storefront windows = NOT painted interior (aluminum frame, factory finish)
     * Aluminum-framed windows = NOT painted interior (even if exterior is painted)
     * Vinyl windows = NOT painted interior
     * Commercial ground-floor windows are almost NEVER painted interior
     * Only RESIDENTIAL windows with wood frames typically get interior paint
     * If the schedule doesn't clearly distinguish painted vs non-painted interiors,
       set windows_painted_interior to 0 and note "unable to determine from schedule"
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
            model="claude-sonnet-4-20250514",
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
   - wall_finish: What's specified for walls (e.g., "Paint", "PT-1", "Wallcovering", "Tile", "CMU Paint")
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
   - floors_per_building: Number of floors per building (from sections, elevations, or notes)
   - has_garage: Whether the building has a parking garage (true/false)
   - garage_floor_area_sqft: If garage area is noted anywhere, include it (0 if unknown)
   - has_pool: Whether this is a pool/amenities building
   - ceiling_height_ft: Default ceiling height if noted in specs (0 if unknown)

3. COMMON AREA ROOMS: Also extract common area rooms (lobbies, corridors, stairwells, mechanical rooms,
   trash rooms, storage rooms) that appear in the finish schedule. These exist once per floor, not per unit.
   Mark their unit_type as "common_area".

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

If no Room Finish Schedule is found, return {"room_finish_schedule": [], "building_info": {}, "notes": ["No Room Finish Schedule found"]}.
Be precise — extract every room listed in the schedule."""

    try:
        result_parts = []
        with client.messages.stream(
            model="claude-sonnet-4-20250514",
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
            model="claude-sonnet-4-20250514",
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
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)

        if json_match:
            inventory = json.loads(json_match.group())
            buildings = inventory.get("buildings", [])

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

            return inventory
        else:
            print(f"   ⚠️  Could not parse building inventory response")
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
                    model="claude-sonnet-4-20250514",
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
        b.get("count", 1) * b.get("units_per_building", 1) for b in buildings
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
        """Determine wall material type from finish spec."""
        if not wall_finish:
            return "GYP"
        wf = wall_finish.lower()
        if any(kw in wf for kw in ("cmu", "block", "masonry", "concrete")):
            return "CMU"
        return "GYP"

    def _is_painted_ceiling(ceiling_finish):
        """Determine if ceiling gets paint."""
        if not ceiling_finish:
            return True
        cf = ceiling_finish.lower()
        # ACT (acoustic ceiling tile) is NOT painted
        if any(kw in cf for kw in ("act", "acoustic", "exposed", "none", "n/a")):
            return False
        return True

    def _get_ceiling_material(ceiling_finish):
        """Determine ceiling material type."""
        if not ceiling_finish:
            return "GYP"
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
        return True  # Default to having base trim

    def _is_concrete_floor(floor_finish):
        """Determine if floor needs concrete sealer."""
        if not floor_finish:
            return False
        ff = floor_finish.lower()
        return any(kw in ff for kw in ("concrete", "sealed concrete", "concrete sealer"))

    # Determine building multiplier
    n_buildings = bi.get("total_identical_buildings", 1) if ENABLE_BUILDING_MULTIPLIER else 1
    unit_types = bi.get("unit_types", [])
    floors_per_building = max(bi.get("floors_per_building", 1), 1)
    ceiling_height_override = bi.get("ceiling_height_ft", 0)

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
            ceiling_finish = room.get("ceiling_finish", "GWB - Paint")
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
                },
                "notes": f"Schedule-estimated room ({total_multiplier}x: {units_per_building} units/bldg × {n_buildings} buildings). "
                         f"Wall finish: {wall_finish}. Ceiling: {ceiling_finish}. Base: {base_finish}.",
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
        ceiling_finish = room.get("ceiling_finish", "GWB - Paint")
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
            },
            "notes": f"Schedule-estimated common area ({common_multiplier}x: {floors_per_building} floors × {n_buildings} buildings). "
                     f"Wall: {wall_finish}. Ceiling: {ceiling_finish}. Base: {base_finish}.",
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
                "concrete_floor_sqft": round(garage_sqft, 2),
                "painted_columns_ea": 0,
                "wallcovering_sqft": 0,
                "stained_wood_sqft": 0,
                "soffit_sqft": 0,
            },
            "notes": f"Garage concrete sealer ({n_buildings}x buildings × {garage_sqft:,.0f} sqft/building = {garage_sqft * n_buildings:,.0f} sqft total)",
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

    # --- Residential window casing heuristic ---
    # In residential buildings, even factory-finished windows have interior wood casings
    # that require painting. If schedule says 0 painted but building is residential,
    # estimate painted casings based on building type.
    # NOTE: Only apply when the schedule lists a positive window count — if the schedule
    # reports 0 total windows (aluminum/factory-finished), there are no casings to paint
    # either, so skip this heuristic entirely.
    building_type = str(combined.get("project_info", {}).get("building_type", "")).lower()
    is_multi_family = any(kw in building_type
                         for kw in ("mixed", "multi", "apartment"))
    is_single_family = any(kw in building_type for kw in ("single", "detached"))
    is_residential = is_multi_family or is_single_family or "residential" in building_type
    total_units = _num(combined.get("project_info", {}).get("total_units", 0))

    # Determine total windows from best available source (schedule > room extraction)
    total_win_available = sched_win_total if sched_win_total > 0 else _num(agg.get("total_windows", 0))

    if is_single_family and total_win_available > 0 and sched_win_painted == 0:
        # Single-family: ALL windows get interior trim painting
        # Rider Ruel data: 23 windows painted out of ~20 total
        est_painted = int(total_win_available)
        agg["total_windows_painted_interior"] = est_painted
        overrides_applied.append(
            f"Windows: single-family heuristic — all {est_painted} windows estimated to have "
            f"painted interior wood trim. Schedule showed 0 painted but single-family homes "
            f"typically have painted wood casings/trim on all windows."
        )
    elif is_multi_family and sched_win_total > 0 and sched_win_painted == 0 and total_units > 0:
        # Multi-family: estimate ~1.3 painted casings per unit
        # Rider data: 26 painted windows / 20 units = 1.3 per unit
        est_painted = round(total_units * 1.3)
        est_painted = min(est_painted, int(sched_win_total))
        agg["total_windows_painted_interior"] = est_painted
        overrides_applied.append(
            f"Windows: multi-family casing heuristic — {est_painted} windows estimated to have "
            f"painted interior casings ({total_units:.0f} units x 1.3/unit). "
            f"Schedule showed 0 painted but residential units typically have wood casings."
        )

    # --- Door count sanity check for multi-unit residential ---
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
    if ext_paint == 0 and is_commercial:
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
            "precast", "precast panel"))
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
                if not exterior.get("lift_required"):
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

    if overrides_applied:
        combined.setdefault("notes", [])
        for note in overrides_applied:
            combined["notes"].append(f"[Schedule Override] {note}")
        print(f"\n📋 Schedule overrides applied:")
        for note in overrides_applied:
            print(f"   • {note}")

    combined["aggregated_totals"] = agg
    return combined


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

            # Same safety cap as footprint-based boost
            MAX_BOOST_FACTOR = 1.30
            if boost_factor > MAX_BOOST_FACTOR:
                analysis.setdefault("notes", []).append(
                    f"[Perimeter Wall Boost Cap] Computed boost {boost_factor:.2f}x exceeds "
                    f"max {MAX_BOOST_FACTOR}x. Capping. Perimeter-derived: {perimeter_wall:,}, "
                    f"aggregated: {current_wall:,}."
                )
                boost_factor = MAX_BOOST_FACTOR

            if boost_factor > 1.05:
                boosted_wall = round(current_wall * boost_factor)
                current_ceil = _num(agg.get("total_paintable_ceiling_sqft", 0))
                boosted_ceil = round(current_ceil * boost_factor) if current_ceil > 0 else current_ceil
                current_trim = _num(agg.get("total_base_trim_lf", 0))
                boosted_trim = round(current_trim * boost_factor) if current_trim > 0 else current_trim

                agg["total_paintable_wall_sqft"] = boosted_wall
                agg["total_paintable_ceiling_sqft"] = boosted_ceil
                agg["total_base_trim_lf"] = boosted_trim
                analysis["aggregated_totals"] = agg

                analysis.setdefault("notes", []).append(
                    f"[Perimeter Wall Boost] Aggregated walls ({current_wall:,} sqft) "
                    f"< perimeter-derived ({perimeter_wall:,} sqft). "
                    f"Boosted to {boosted_wall:,} sqft ({boost_factor:.2f}x). "
                    f"Ceilings {current_ceil:,}->{boosted_ceil:,}, "
                    f"Trim {current_trim:,}->{boosted_trim:,} LF."
                )
                print(f"   📐 Perimeter wall boost: {current_wall:,} -> {boosted_wall:,} sqft "
                      f"({boost_factor:.2f}x, from perimeter data)")

            return analysis  # Used perimeter-based; skip footprint-based

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
        # Boost to expected ratio
        boost_target = expected_wall
        boost_factor = boost_target / current_wall if current_wall > 0 else 1.0

        # SAFETY CAP: Limit boost factor to 1.30x maximum.
        # Footprint extraction by the LLM is unreliable (±36% variance observed).
        # Without a cap, a bad footprint can produce boost factors of 1.5-1.6x,
        # causing $30-40K swings on large multi-family projects.
        # 1.30x covers the legitimate extraction gap (typically 10-20% under) while
        # preventing runaway inflation from footprint errors.
        MAX_BOOST_FACTOR = 1.30
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
            # Also boost ceilings proportionally (they track with rooms)
            current_ceil = _num(agg.get("total_paintable_ceiling_sqft", 0))
            boosted_ceil = round(current_ceil * boost_factor) if current_ceil > 0 else current_ceil
            # And trim (perimeter-based, tracks with wall extraction completeness)
            current_trim = _num(agg.get("total_base_trim_lf", 0))
            boosted_trim = round(current_trim * boost_factor) if current_trim > 0 else current_trim

            agg["total_paintable_wall_sqft"] = boosted_wall
            agg["total_paintable_ceiling_sqft"] = boosted_ceil
            agg["total_base_trim_lf"] = boosted_trim
            analysis["aggregated_totals"] = agg

            analysis.setdefault("notes", []).append(
                f"[Wall Boost] Extracted wall area ({current_wall:,} sqft) was {actual_ratio:.2f}x "
                f"floor area — expected ~{expected_wall_ratio}x for residential multi-family. "
                f"Boosted to {boosted_wall:,} sqft (factor {boost_factor:.2f}x). "
                f"Ceilings {current_ceil:,}->{boosted_ceil:,}, Trim {current_trim:,}->{boosted_trim:,} LF."
            )
            print(f"   📐 Wall boost: {current_wall:,} -> {boosted_wall:,} sqft "
                  f"({boost_factor:.2f}x factor, was {actual_ratio:.2f}x floor area)")

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
            multiplier = max(1, int(_num(room.get("unit_multiplier", 1))))

            expected_wall = perimeter * ceiling_h

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
    Three-tier deduplication:
    1. Exact room_id match → keep higher detail_score
    2. Normalized unit+type match with dimension similarity (±10%) → keep higher detail
    3. Exact name + similar area (within 50 sqft) → keep higher detail
    3b. Same normalized type + same floor + similar wall area (±20%) for non-unit rooms
        (catches cross-chunk duplicates in single-family homes where the same room
         is extracted with slightly different names/dimensions)

    Returns: (deduplicated_rooms, dedup_log)
    """
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


def _validate_extraction(analysis, file_room_counts=None):
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
            r.get("dimensions", {}).get("wall_area_sqft", 0) * max(1, r.get("unit_multiplier", 1))
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


def _recalculate_totals(analysis):
    """
    Recalculate aggregated_totals from individual room data.
    Applies ceiling_painted filter, door type split, window painted filter,
    and unit_multiplier for repeated/typical unit types.
    Works for both single-file and merged multi-file analyses.
    """
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

    total_wall = 0
    total_ceiling = 0
    total_cmu_wall = 0
    total_dryfall_ceiling = 0
    total_trim = 0
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
            wall_mat = str(mats.get("walls", "")).lower()
            if "cmu" in wall_mat:
                total_cmu_wall += _num(dims.get("wall_area_sqft", 0)) * multiplier
            elif any(kw in wall_mat for kw in ("gyp", "gwb", "gypsum", "paintable")):
                total_wall += _num(dims.get("wall_area_sqft", 0)) * multiplier

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
            total_trim += _num(elems.get("base_trim_lf", 0)) * multiplier

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
            total_level_5 += _num(elems.get("level_5_finish_sqft", 0)) * multiplier

            # Concrete floor sealer (garages, basements, mechanical rooms)
            # For mixed-use/residential buildings, only count specific room types —
            # not entire floor footprints or generic spaces
            _conc_sqft = _num(elems.get("concrete_floor_sqft", 0))
            if _conc_sqft > 0:
                _bt_conc = str(analysis.get("project_info", {}).get("building_type", "")).lower()
                _is_res_conc = any(kw in _bt_conc for kw in (
                    "residential", "mixed", "multi", "apartment"))
                if _is_res_conc:
                    _conc_room_ok = any(kw in rname_lower for kw in (
                        "garage", "parking", "mechanical", "boiler", "storage",
                        "utility", "janitor", "maintenance", "trash"))
                    if not _conc_room_ok:
                        _conc_sqft = 0  # Skip concrete for non-qualifying rooms
                total_concrete_floor += _conc_sqft * multiplier

            # Painted columns (commercial)
            total_painted_columns += _num(elems.get("painted_columns_ea", 0)) * multiplier

            # Wallcovering (labor-only install)
            total_wallcovering += _num(elems.get("wallcovering_sqft", 0)) * multiplier

            # Stained wood / clear-coat panels
            total_stained_wood += _num(elems.get("stained_wood_sqft", 0)) * multiplier

            # Interior soffits (GYP drywall drops)
            total_soffit += _num(elems.get("soffit_sqft", 0)) * multiplier

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

    if len(all_rooms_list) == 0 and total_units_val >= 4:
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
    if total_dryfall_ceiling == 0:
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
            all_notes_lower = all_notes.lower()
            dryfall_in_notes = any(kw in all_notes_lower for kw in (
                "dryfall", "dry fall", "spray-applied", "spray applied",
                "paint exposed", "painted deck", "paint deck", "dry-fall"))

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

    # --- Wallcovering estimation fallback ---
    # When finish schedule mentions WC-x codes but LLM extracted 0 wallcovering_sqft,
    # estimate wallcovering from customer-facing rooms (showroom, lobby, boutique, lounge).
    # Wallcovering is typically applied to accent walls (30-50% of wall area) in
    # customer-facing spaces.
    has_wc_refs = False
    if total_wallcovering == 0:
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
    if total_wallcovering == 0 and not has_wc_refs:
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
    if total_stained_wood == 0:
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
    if total_concrete_floor > 0:
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
    # For commercial buildings, zero painted windows (storefront/aluminum frames).
    # For residential/mixed-use buildings, KEEP painted window counts because
    # residential windows commonly have painted wood frames or interior finishes.
    has_win_sched = analysis.get("has_window_schedule")
    notes_text = " ".join(str(n) for n in analysis.get("notes", []))
    no_schedule_in_notes = (
        "no window schedule" in notes_text.lower()
        or "no door or window schedule" in notes_text.lower()
        or "window schedule not" in notes_text.lower()
    )
    if has_win_sched is False or (has_win_sched is None and no_schedule_in_notes):
        building_type = str(analysis.get("project_info", {}).get("building_type", "")).lower()
        # "commercial/mixed-use" contains "mixed" but is commercial — commercial takes precedence
        # unless the type also explicitly mentions "residential"/"apartment"/"condo"
        _has_commercial = "commercial" in building_type
        _has_residential_kw = any(kw in building_type
                                  for kw in ("residential", "apartment", "condo"))
        is_residential = (not _has_commercial or _has_residential_kw) and any(
            kw in building_type for kw in ("residential", "mixed", "multi", "apartment", "condo"))

        if total_windows_painted > 0:
            if is_residential:
                # Residential/mixed-use: keep painted windows, flag for RFI confirmation
                print(f"   ⚠️  No window schedule found — keeping {total_windows_painted} "
                      f"painted windows for residential building (will generate RFI)")
                analysis.setdefault("notes", []).append(
                    f"[Window Guard Rail] No window schedule found but {total_windows_painted} "
                    f"windows kept as painted for residential building — RFI recommended to confirm"
                )
            else:
                # Commercial: zero painted windows (storefront/aluminum frames not painted)
                print(f"   ⚠️  No window schedule found — zeroing {total_windows_painted} "
                      f"assumed painted windows for commercial building (will generate RFI)")
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
    }

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
            if supplement > 100:
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
    For commercial (non-residential) buildings, zero out all painted windows.
    Commercial windows (storefront, aluminum-frame) are not field-painted.
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

    current_windows = _num(agg.get("total_windows_painted_interior", 0))
    if current_windows > 0:
        print(f"   ⚠️  Commercial building — zeroing {current_windows:.0f} painted windows "
              f"(commercial windows assumed not painted)")
        analysis.setdefault("notes", []).append(
            f"[Commercial Window Exclusion] Zeroed {current_windows:.0f} painted windows — "
            f"commercial building windows assumed storefront/aluminum (not painted). "
            f"RFI: If windows require painting, provide window schedule with finish specs."
        )
        agg["total_windows_painted_interior"] = 0
        analysis["aggregated_totals"] = agg
        # Also zero at room level for consistency
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


def generate_rfi_items(analysis):
    """
    Scan the analysis dict for missing/incomplete data and return
    a list of RFI (Request For Information) item dicts.

    Each item:
        {"number": int, "category": str, "question": str, "action_required": str}

    Categories:
        "Missing Drawings", "Incomplete Dimensions", "Missing Schedules",
        "Material Specifications", "Clarification Needed"

    Returns [] if no issues found.
    """
    items = []

    # --- 1. No floor plans found ---
    if analysis.get("no_floor_plans_found") or analysis.get("no_detailed_floor_plans_found"):
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
                "per unit. Can you provide the door schedule sheets (typically A-501/A-502)?"
            ),
            "action_required": "Provide door schedule sheet(s) showing door types, materials, and frame specifications."
        })

    # --- 3. No window schedule ---
    if analysis.get("has_window_schedule") is False:
        items.append({
            "category": "Missing Schedules",
            "question": (
                "No window schedule was found in the provided documents. We need to "
                "determine which windows have painted interior frames versus factory-finished "
                "aluminum. Can you provide the window schedule?"
            ),
            "action_required": "Provide window schedule showing frame materials and finish specifications."
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
                "Our estimate excludes interior window painting for this commercial building. "
                "Commercial windows are typically storefront or aluminum-frame and do not require "
                "field painting. If any windows DO require painting (e.g., wood-frame or "
                "painted interior trim), please provide the window schedule with finish "
                "specifications so we can add them to the estimate."
            ),
            "action_required": (
                "Confirm windows are excluded, or provide window schedule showing "
                "which windows require interior painting."
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


def _get_tiered_rate(item_config, quantity):
    """Return the unit rate for the tier matching the given quantity.

    item_config is a dict from PRICING_MODEL, e.g.:
        {"unit": "sqft", "markup": 0.08,
         "tiers": [{"min_qty": 0, "max_qty": 3499, "rate": 1.10},
                    {"min_qty": 3500, "max_qty": None, "rate": 0.80}]}
    """
    tiers = item_config.get("tiers", [])
    for tier in tiers:
        max_qty = tier.get("max_qty")
        if tier["min_qty"] <= quantity and (max_qty is None or quantity <= max_qty):
            return tier["rate"]
    # Fallback: use last tier if quantity exceeds all ranges
    if tiers:
        return tiers[-1]["rate"]
    return 0


def calculate_costs(aggregated_totals, exterior=None, building_type="", project_info=None):
    """Calculate costs using Rider Painting pricing model from config.py"""

    if exterior is None:
        exterior = {}
    if project_info is None:
        project_info = {}

    # Building-type-aware markup and rate overrides
    bt = str(building_type).lower()
    is_single_family = any(kw in bt for kw in ("single", "detached"))
    is_commercial = "commercial" in bt

    # Sub-classify commercial by footprint: large (retail/warehouse) vs small (office/renovation)
    _footprint = _num(project_info.get('footprint_sqft', 0))
    is_large_commercial = is_commercial and _footprint > 10000

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

    # Markup: single-family 8%, large commercial 5%, small commercial 8%, multi-family 6% (default)
    if is_single_family:
        markup_override = 0.08
    elif is_commercial:
        markup_override = 0.05 if is_large_commercial else 0.08
    else:
        markup_override = None  # None = use per-item default (6%)

    def _get_markup(item_key):
        """Return markup for an item — override for single-family, else use config default."""
        if markup_override is not None:
            return markup_override
        return PRICING_MODEL[item_key]['markup']

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

    # Specialty finishes
    level_5 = _num(aggregated_totals.get('total_level_5_finish_sqft', 0))
    # Level 5 is priced per occurrence (each area), not per sqft
    level_5_count = 1 if level_5 > 0 else 0

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

    # --- Hardie/Azek/Lintel: only price if extraction explicitly found siding TYPE ---
    # The LLM may detect Hardie on elevations, but new construction Hardie comes
    # factory-primed and doesn't need field painting. Only price siding materials
    # when the exterior notes explicitly say "paint" or "field finish" for siding.
    # Check exterior_siding_type and notes for painting indicators.
    _ext_siding_type = str(exterior.get('exterior_siding_type', '')).lower()
    _ext_notes = str(exterior.get('notes', '')).lower()
    _siding_needs_paint = any(kw in _ext_notes for kw in (
        'paint siding', 'field paint', 'field finish', 'prime and paint',
        'finish coat', 'two coat', '2 coat', 'topcoat'))
    # Also check if scope_notes from user mention exterior siding painting
    _scope_ext = str(project_info.get('_scope_notes', '')).lower()
    _siding_needs_paint = _siding_needs_paint or any(kw in _scope_ext for kw in (
        'paint siding', 'hardie paint', 'exterior siding', 'siding painting'))
    # Negative check: if exterior notes explicitly say siding is factory-finished or
    # non-paintable material (cork, vinyl, metal, aluminum), suppress siding even if
    # the LLM extracted Hardie/Azek quantities.
    _siding_factory_finished = any(kw in _ext_notes for kw in (
        'cork siding', 'vinyl siding', 'metal siding', 'metal roofing',
        'aluminum siding', 'composite siding', 'factory finish',
        'pre-finish', 'prefinish', 'not require painting',
        'do not require paint', 'does not require paint',
        'no painting required', 'no paint required'))
    if _siding_factory_finished:
        _siding_needs_paint = False

    if not _siding_needs_paint:
        # New construction: siding/trim comes factory-finished. Zero out material-specific
        # exterior items. Keep generic ext_paint_sqft and cornice (those are always painted).
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
    # FALLBACK: For single-family homes with 2+ stories and extracted exterior notes
    # (elevation sheets were parsed) but 0 cornice, estimate from building perimeter.
    # This handles non-deterministic chunk processing where elevation data is sometimes
    # missed between runs.
    if is_single_family and cornice_lf == 0:
        _sf_footprint = _num(project_info.get('footprint_sqft', 0))
        _sf_stories = _num(project_info.get('total_stories', 0))
        # Only apply fallback when we have a footprint and multi-story building
        if _sf_footprint > 0 and _sf_stories >= 2:
            # Estimate perimeter from footprint (assume ~1.5:1 aspect ratio)
            _sf_long = math.sqrt(_sf_footprint * 1.5)
            _sf_short = _sf_footprint / _sf_long if _sf_long > 0 else 0
            _sf_perimeter = 2 * (_sf_long + _sf_short)
            cornice_lf = round(_sf_perimeter)
        elif wall_sqft > 0 and _sf_stories >= 2:
            # No footprint available — estimate perimeter from total wall area
            # Typical residential: wall_area = perimeter × ceiling_height × stories
            # Average ceiling height ~9ft, 2 stories
            _est_ceiling = 9.0
            _est_perimeter = wall_sqft / (_est_ceiling * max(_sf_stories, 2))
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
    # If any exterior scope exists, require exterior lift (unless single-family ≤3 stories)
    has_any_ext = ext_paint_sqft > 0 or hardie_sqft > 0 or azek_lf > 0
    if has_any_ext and lift_needed == 0:
        # Single-family homes ≤3 stories use ladders, not lifts
        _sf_stories_ext = _num(project_info.get('total_stories', 0))
        if not (is_single_family and _sf_stories_ext <= 3):
            lift_needed = 1

    pm = PRICING_MODEL

    # Resolve tiered rates based on actual project quantities
    wall_rate   = _get_tiered_rate(pm['gyp_walls'], wall_sqft)
    ceil_rate   = _get_tiered_rate(pm['gyp_ceilings'], ceil_sqft)
    cmu_rate    = _get_tiered_rate(pm['cmu_walls_full'], cmu_wall_sqft)
    dryfall_rate = _get_tiered_rate(pm['dryfall_ceiling'], dryfall_sqft)
    trim_rate   = _get_tiered_rate(pm['base_trim'], trim_lf)
    door_fp_rate = _get_tiered_rate(pm['doors_full_paint'], doors_full)
    door_hm_rate = _get_tiered_rate(pm['doors_hm_panel'], doors_hm)
    door_frame_rate = _get_tiered_rate(pm['doors_frame_only'], doors_frame)
    win_rate    = _get_tiered_rate(pm['windows'], windows)
    stair_rate  = _get_tiered_rate(pm['stairs'], stair_sections)
    gyps_rate   = _get_tiered_rate(pm['gyp_between_stairs'], gyp_stairs)
    l5_rate     = _get_tiered_rate(pm['level_5_finish'], level_5_count)
    conc_rate   = _get_tiered_rate(pm['concrete_sealer'], concrete_sqft)
    col_rate    = _get_tiered_rate(pm['painted_columns'], columns_ea)
    # Wallcovering rate: use prep rate ($0.50/SF) for bathroom heuristic, full install ($9/SF) otherwise
    _wc_source = project_info.get('_wallcovering_source', '')
    if _wc_source == 'bathroom_heuristic' and 'wallcovering_prep' in pm:
        wc_rate = _get_tiered_rate(pm['wallcovering_prep'], wallcovering_sqft)
    else:
        wc_rate = _get_tiered_rate(pm['wallcovering_install'], wallcovering_sqft) if 'wallcovering_install' in pm else 9.00
    sw_rate     = _get_tiered_rate(pm['stained_wood'], stained_wood_sqft) if 'stained_wood' in pm else 6.00
    soffit_rate = _get_tiered_rate(pm['interior_soffit'], soffit_sqft) if 'interior_soffit' in pm else 0.85
    corn_rate   = _get_tiered_rate(pm['exterior_cornice'], cornice_lf)
    wt_rate     = _get_tiered_rate(pm['exterior_window_trim'], window_trim_lf)
    ext_paint_rate = _get_tiered_rate(pm['exterior_painting'], ext_paint_sqft) if 'exterior_painting' in pm else 1.80
    hardie_rate = _get_tiered_rate(pm['exterior_hardie_siding'], hardie_sqft) if 'exterior_hardie_siding' in pm else 4.85
    azek_rate   = _get_tiered_rate(pm['exterior_azek_trim'], azek_lf) if 'exterior_azek_trim' in pm else 9.00
    corner_rate = _get_tiered_rate(pm['exterior_corner_board'], corner_lf) if 'exterior_corner_board' in pm else 9.00
    lintel_rate = _get_tiered_rate(pm['exterior_steel_lintel'], steel_lintel_lf_ext) if 'exterior_steel_lintel' in pm else 32.00
    lift_rate   = _get_tiered_rate(pm['exterior_lift_rental'], lift_needed)
    int_lift_rate = _get_tiered_rate(pm['interior_lift_rental'], int_lift_needed)

    # Single-family rate overrides: force small-project rates regardless of quantity
    # (A single-family home with 7,000+ sqft walls is still a single-family job)
    if is_single_family:
        wall_rate = 1.25   # Rider single-family rate
        ceil_rate = 1.25   # Rider single-family rate
        trim_rate = 3.25   # Rider single-family rate
        door_fp_rate = 225.00  # Rider single-family rate
        win_rate = 120.00  # Rider single-family: pre-primed trim only

    # Commercial rate overrides: split by building size
    if is_commercial:
        # Base trim: let extraction decide per-room; don't blanket zero
        # (Some commercial buildings have rubber/wood base, others don't)

        if is_large_commercial:
            # Large retail/warehouse rates (calibrated from Camping World, Kingston NY)
            wall_rate = 0.85   # Large open spaces, lower labor density
            ceil_rate = 0.85
            door_fp_rate = 155.00  # Commercial HM door+frame rate (Rider Mazda)
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
            # Small commercial / renovation rates (calibrated from BFCU Glenmont)
            wall_rate = 1.40   # Standard interior painting rate
            ceil_rate = 1.40
            door_fp_rate = 155.00  # Commercial door rate
            door_hm_rate = 110.00  # HM frame-only rate (Rider BFCU)

    # Non-apartment residential rate overrides (senior living, care facilities, expansions)
    # These buildings lack the spray-application efficiency of repetitive apartment units.
    # Higher labor density → rates between apartment ($0.80/SF) and small commercial ($1.40/SF).
    # Windows are typically factory-finished (vinyl/aluminum) → trim paint only at $120/EA.
    # Calibrated from Edgehill IL Expansion vs Rider Estimate #3241: Rider effective rate ~$1.27/SF.
    if is_non_apartment_residential:
        wall_rate = 1.05   # Higher labor density than apartments, spray efficiency is lower
        ceil_rate = 1.05
        win_rate = 120.00  # Factory-finished windows: trim paint only (not full interior paint)

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

    line_items = [
        _line(f"Gyp. Walls - {wall_sqft:,.0f} sqft @ ${wall_rate:.2f}", wall_sqft,
              wall_rate, _get_markup('gyp_walls')),
        _line(f"Gyp. Ceilings - {ceil_sqft:,.0f} sqft @ ${ceil_rate:.2f}", ceil_sqft,
              ceil_rate, _get_markup('gyp_ceilings')),
        _line(f"CMU Walls (Full System) - {cmu_wall_sqft:,.0f} sqft @ ${cmu_rate:.2f}", cmu_wall_sqft,
              cmu_rate, _get_markup('cmu_walls_full')),
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
        _line(f"Level 5 Finish - {level_5_count} EA @ ${l5_rate:.2f}", level_5_count,
              l5_rate, _get_markup('level_5_finish')),
        _line(f"Concrete Sealer - {concrete_sqft:,.0f} sqft @ ${conc_rate:.2f}", concrete_sqft,
              conc_rate, _get_markup('concrete_sealer')),
        _line(f"Painted Columns - {columns_ea:.0f} EA @ ${col_rate:.2f}", columns_ea,
              col_rate, _get_markup('painted_columns')),
        _line(f"Wallcovering Install (Labor) - {wallcovering_sqft:,.0f} sqft @ ${wc_rate:.2f}", wallcovering_sqft,
              wc_rate, _get_markup('wallcovering_install') if 'wallcovering_install' in pm else 0.04),
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
    ]

    subtotal = sum(li["total"] for li in line_items)

    return {
        "line_items": line_items,
        "subtotal": round(subtotal, 2)
    }

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
                "message": "Commercial building with no CMU walls or dryfall ceiling detected. "
                           "Verify specs for painted CMU or exposed ceiling coating."
            })

    # 3. Line-item concentration check (any single item > 40% of total)
    for item in cost_estimate.get('line_items', []):
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
                "message": "Finish schedule references wallcovering (WC-x) but 0 sqft extracted. "
                           "Wallcovering is typically $9/sqft — verify rooms with WC finish types."
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

    # 5. Data quality score (0-100)
    quality_score = 100
    for w in warnings:
        if w["severity"] == "high":
            quality_score -= 20
        elif w["severity"] == "medium":
            quality_score -= 10
    quality_score = max(0, quality_score)

    return {
        "warnings": warnings,
        "data_quality_score": quality_score,
        "warning_count": len(warnings),
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

    # Show room breakdown by floor
    print(f"\n🏢 ROOM BREAKDOWN BY FLOOR:")
    for floor in analysis.get('floors', []):
        print(f"\n  {floor['floor_name']}:")
        for room in floor.get('rooms', []):
            dims = room.get('dimensions', {})
            multiplier = _extract_multiplier_from_notes(room)
            mult_label = f" (×{multiplier} units)" if multiplier > 1 else ""
            print(f"    • {room.get('room_name', room.get('room_id', 'Unknown'))}{mult_label}")
            print(f"      {dims.get('length_feet', 0)}' x {dims.get('width_feet', 0)}'"
                  f" x {dims.get('ceiling_height_feet', 0)}'"
                  f" = {_num(dims.get('floor_area_sqft', 0)):,.0f} sqft")

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

def analyze_and_parse(client, pdf_path, scope_notes="", schedule_hints=None,
                      building_inventory=None):
    """Analyze a single PDF and return parsed JSON. Returns (path, analysis_dict) or None on failure."""
    filename = os.path.basename(pdf_path)
    try:
        result_text = analyze_construction_pdf(client, pdf_path, scope_notes=scope_notes,
                                                schedule_hints=schedule_hints,
                                                building_inventory=building_inventory)
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            analysis = json.loads(json_match.group())
            return (pdf_path, analysis)
        else:
            print(f"\n⚠️  Could not parse response for {filename}")
            print(f"   Raw (first 500 chars): {result_text[:500]}")
            return None
    except Exception as e:
        print(f"\n❌ Error analyzing {filename}: {e}")
        return None


def run_analysis(pdf_paths, contact_name="", contact_email="", scope_notes="",
                  corrections_path=None, use_cache=False, multi_pass=False,
                  image_fallback=True, schedule_estimation=True):
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

                # Apply corrections to cached analysis (if any)
                corrections = _load_corrections(corrections_path)
                if corrections:
                    analysis = _apply_corrections(analysis, corrections)
                    analysis = _recalculate_totals(analysis)

                # Re-run cost calculation (uses current pricing from config.py)
                print("\n💰 Calculating costs...")
                costs = calculate_costs(
                    analysis.get('aggregated_totals', {}),
                    exterior=analysis.get('exterior', {}),
                    building_type=analysis.get('project_info', {}).get('building_type', ''),
                    project_info=analysis.get('project_info', {})
                )
                print_estimate(analysis, costs)

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
                    "pricing_model": PRICING_MODEL,
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

    # --- Pre-scan for building inventory from index pages ---
    _update_progress(2, TOTAL_STEPS, "Building Inventory", "Scanning index pages for building data...")
    building_inventory = None
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

    # --- Analyse each PDF ---
    all_results = []
    files_analyzed = []
    files_skipped = []
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
                                          building_inventory=building_inventory)
            if result:
                _, analysis_check = result
                rooms_found = analysis_check.get('project_info', {}).get('total_rooms_found', 0)
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

        if best_rooms == 0 or rooms_have_zero_dims:
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
                    n_pages = len(painting_page_indices) if painting_page_indices else "all"
                    print(f"\n   🔬 Native PDF returned 0 rooms — "
                          f"large-format pages detected, trying enhanced extraction "
                          f"({n_pages} painting-relevant pages)...")
                    time.sleep(15)  # brief cooldown
                    enhanced_result = _analyze_with_enhanced_extraction(
                        client, pdf_path,
                        scope_notes=scope_notes,
                        schedule_hints=image_schedule_data,
                        building_inventory=building_inventory,
                        page_indices=painting_page_indices
                    )
                    if enhanced_result:
                        _, enh_analysis = enhanced_result
                        enh_rooms = enh_analysis.get('project_info', {}).get(
                            'total_rooms_found', 0)
                        if enh_rooms > 0:
                            print(f"   🔬 Enhanced extraction recovered {enh_rooms} rooms!")
                            result = enhanced_result
                            best_result = enhanced_result
                            best_rooms = enh_rooms
                            used_enhanced = True
                        else:
                            print(f"   🔬 Enhanced extraction also returned 0 rooms")
                    else:
                        print(f"   🔬 Enhanced extraction failed")

        # ── Image fallback for floor plan files that returned 0 rooms ──
        used_image_fb = False
        if image_fallback and is_fp and best_rooms == 0:
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
                    building_inventory=building_inventory
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

        # Optional multi-pass: re-extract floor plan files for best result
        # Only when --multi-pass is enabled, cache is off, and first pass got rooms
        if multi_pass and not use_cache and is_fp and result and best_rooms > 0:
            if used_image_fb:
                # Second pass also uses image mode for consistency
                print(f"   🔄 Multi-pass (image mode): running second extraction...")
                time.sleep(30)
                pass2_result = _analyze_floor_plan_as_images(
                    client, pdf_path, scope_notes=scope_notes,
                    schedule_hints=image_schedule_data,
                    building_inventory=building_inventory)
            else:
                print(f"   🔄 Multi-pass: running second extraction for comparison...")
                time.sleep(30)  # brief cooldown to avoid rate limits
                pass2_result = analyze_and_parse(client, pdf_path, scope_notes=scope_notes,
                                                 schedule_hints=image_schedule_data,
                                                 building_inventory=building_inventory)
            if pass2_result:
                _, pass2_analysis = pass2_result
                pass2_rooms = pass2_analysis.get('project_info', {}).get(
                    'total_rooms_found', 0)
                if pass2_rooms > best_rooms:
                    print(f"   📊 Multi-pass improved: {best_rooms} -> {pass2_rooms} rooms")
                    result = pass2_result
                    best_rooms = pass2_rooms
                else:
                    print(f"   📊 Multi-pass: kept original ({best_rooms} vs {pass2_rooms} rooms)")

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
            if analysis_result.get('no_floor_plans_found') or analysis_result.get('no_detailed_floor_plans_found'):
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
                    if room_schedule and room_schedule.get("room_finish_schedule"):
                        synthetic_floors = _estimate_from_room_finish_schedule(
                            room_schedule, schedule_data
                        )
                        if synthetic_floors:
                            analysis_result["floors"] = synthetic_floors
                            analysis_result["no_floor_plans_found"] = False
                            analysis_result["schedule_estimated"] = True
                            analysis_result["building_info"] = room_schedule.get("building_info", {})
                            schedule_estimated_files.append(filename)
                            total_synth = sum(len(f.get("rooms", [])) for f in synthetic_floors)
                            print(f"   ✅ Schedule estimation: {total_synth} room templates generated")

                # Merge schedule data (doors/windows/stairs)
                if schedule_data:
                    for key in ("door_schedule", "window_schedule", "stair_info", "wall_types"):
                        if schedule_data.get(key):
                            analysis_result[key] = schedule_data[key]
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
        raise ValueError("No PDFs could be analysed successfully")

    # --- Merge or use single result ---
    if multi_mode:
        print(f"\n{'='*80}")
        print(f"🔗 MERGING {len(all_results)} analyses into combined estimate...")
        print(f"{'='*80}")
        _update_progress(4, TOTAL_STEPS, "Merging Results", f"Combining data from {len(all_results)} files...")
        analysis = merge_analyses(all_results, file_building_counts=file_building_counts)
    else:
        _, analysis = all_results[0]
        if analysis.get('no_floor_plans_found') or analysis.get('no_detailed_floor_plans_found'):
            print(f"\n⚠️  NO FLOOR PLANS FOUND")
            print(f"Pages reviewed: {analysis.get('pages_reviewed', 'Unknown')}")
        analysis = _normalize_scope_fields(analysis)
        analysis = _recalculate_totals(analysis)
        # Schedule overrides applied AFTER all recalculations (see below)

    # Normalize scope fields after merge (ensures every room has in_scope)
    _update_progress(5, TOTAL_STEPS, "Validating & Recalculating", "Applying guardrails and schedule overrides...")
    analysis = _normalize_scope_fields(analysis)

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
    _is_sf = (
        any(kw in _sf_bt for kw in ("single", "detached"))
        or (_sf_units <= 2 and isinstance(_sf_units_raw, (int, float)))
    ) and not any(kw in _sf_bt for kw in ("multi", "mixed", "commercial", "apartment"))
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

    # Run extraction validation checks
    analysis = _validate_extraction(analysis, file_room_counts=file_room_counts)

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

    # --- Perimeter-based wall cross-check (must run BEFORE wall boost) ---
    # Computes perimeter-derived wall totals and stores in _perimeter_cross_check
    # for _validate_and_boost_walls() to use as preferred boost source.
    analysis = _validate_wall_area_by_perimeter(analysis)

    # --- Wall area validation + boost (for residential multi-family) ---
    # Uses perimeter-based boost (preferred) or footprint-based (fallback).
    # Must come AFTER _recalculate_totals and perimeter cross-check.
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
    is_single_family = (
        any(kw in building_type_str for kw in ("single", "detached"))
        or (total_units_is_numeric and total_units <= 2)
    )
    if is_single_family and not any(kw in building_type_str for kw in ("multi", "mixed", "commercial", "apartment", "senior", "living")):
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
    # Basement adds an extra level transition for stair calculations
    floors_list = analysis.get("floors", [])
    has_basement = any("base" in f.get("floor_name", "").lower() or
                       "cellar" in f.get("floor_name", "").lower()
                       for f in floors_list)
    effective_levels = int(total_stories) + (1 if has_basement else 0)

    # Calculate expected minimum stair count based on building size
    # Typical: 2 stairwells × (effective_levels - 1) transitions × 2 flights
    expected_min_stairs = 2 * max(1, effective_levels - 1) * 2

    if total_stories >= 2 and (current_stairs == 0 or current_stairs < expected_min_stairs * 0.7):
        # Stairs are missing or seem too low for the building
        est_stairs = 0

        if current_stairs == 0:
            # Try to parse stair count from stair-specific notes only
            for note in analysis.get("notes", []):
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
                # "N stairwells" — multiply by effective_levels × 2 flights per transition
                m2 = re.search(r'(\d+)\s*stairwell', note_lower)
                if m2:
                    stairwells = int(m2.group(1))
                    transitions = max(1, effective_levels - 1)
                    est_stairs = max(est_stairs, stairwells * transitions * 2)
                # "= X total" at end of stair note
                m3 = re.search(r'=\s*(\d+)\s*total', note_lower)
                if m3:
                    est_stairs = max(est_stairs, int(m3.group(1)))

            # Cap at reasonable maximum: 4 stairwells × effective_levels × 2 flights
            max_reasonable = 4 * effective_levels * 2
            if est_stairs > max_reasonable:
                est_stairs = 0

        # If notes didn't give us a number, use building heuristic
        if est_stairs == 0:
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
    # Use above-grade stories only for cap (basement stairs are fewer/simpler).
    final_stair_count = _num(agg.get("total_stair_sections", 0))
    above_grade_expected = 2 * max(1, int(total_stories) - 1) * 2  # no basement
    if total_stories >= 2 and above_grade_expected > 0:
        stair_cap = round(above_grade_expected * 1.25)
        if final_stair_count > stair_cap:
            agg["total_stair_sections"] = above_grade_expected
            analysis["aggregated_totals"] = agg
            analysis.setdefault("notes", []).append(
                f"[Stair Cap] Capped stairs from {final_stair_count} to "
                f"{above_grade_expected} sections (heuristic: 2 stairwells x "
                f"{max(1, int(total_stories) - 1)} transitions x 2 flights = "
                f"{above_grade_expected}). Extraction likely counted landings "
                f"as separate sections."
            )
            print(f"   🪜 Stair cap: {final_stair_count} -> {above_grade_expected} sections "
                  f"(capped to above-grade heuristic for {int(total_stories)}-story building)")

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

    # --- Calculate costs ---
    _update_progress(6, TOTAL_STEPS, "Calculating Costs", "Applying pricing model...")
    print("\n💰 Calculating costs...")
    costs = calculate_costs(
        analysis.get('aggregated_totals', {}),
        exterior=analysis.get('exterior', {}),
        building_type=analysis.get('project_info', {}).get('building_type', ''),
        project_info=analysis.get('project_info', {})
    )

    print_estimate(analysis, costs)

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
    if rfi_items:
        print(f"\n📋 RFI: {len(rfi_items)} items requiring clarification")
        for rfi in rfi_items:
            q_preview = rfi['question'][:80] + ('...' if len(rfi['question']) > 80 else '')
            print(f"   {rfi['number']}. [{rfi['category']}] {q_preview}")

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
        "analysis": analysis,
        "cost_estimate": costs,
        "validation": validation,
        "pricing_model": PRICING_MODEL,
        "rfi_items": rfi_items if rfi_items else None,
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
    }


def main():
    """Main CLI entry point — parses args and delegates to run_analysis()."""

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
        sys.exit(1)

    # Parse boolean flags (no value) separately from key-value pairs
    bool_flags = set()
    args = {}
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ('--cache', '--no-cache', '--clear-cache', '--multi-pass',
                    '--image-fallback', '--no-image-fallback',
                    '--schedule-estimation', '--no-schedule-estimation'):
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
                     schedule_estimation=schedule_estimation)

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
