#!/usr/bin/env python3
"""auto_takeoff.py — orchestrator for large architectural PDFs.

Splits a PDF into right-sized chunks, submits each as its own RQ job,
polls until all complete, then merges per-chunk JSONs into one consolidated
estimate. Run once, walk away, come back to a single output.

Usage:
    python3 scripts/auto_takeoff.py PDF_PATH [options]

Options:
    --name NAME              Contact name (required)
    --email EMAIL            Contact email (required)
    --business NAME          Business / project name (defaults to PDF stem)
    --user-id N              User ID for the submission rows (default: 1)
    --org-id N               Org ID for the submission rows (default: 1)
    --out PATH               Output JSON path (default: <pdf>_consolidated.json)
    --max-mb FLOAT           Max chunk size in MB (default: 50)
    --max-pages N            Max pages per chunk (default: 10)
    --dense-page-mb FLOAT    Pages > this size trigger single-page chunks
                             (default: 8)
    --skip-submit            Use existing chunk submissions, just poll+merge.
                             Pass --existing-sids "<sid1>,<sid2>,..." to
                             resume polling without re-uploading.
    --existing-sids STR      Comma-separated submission IDs to resume on.
    --dry-run                Split + report what would happen, don't submit.
    --poll-secs N            Seconds between DB polls (default: 60)

Idempotent if interrupted: prints a `--existing-sids` line on every
successful submit so you can resume by passing it back in.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---- project imports ------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

from PyPDF2 import PdfReader, PdfWriter

import storage  # noqa: E402
from db import session_scope  # noqa: E402
from models import Submission, File  # noqa: E402
from config import (  # noqa: E402
    REDIS_URL, RQ_QUEUE_FAST, RQ_QUEUE_HEAVY, RQ_JOB_TIMEOUT, RQ_RESULT_TTL,
)

from redis import Redis  # noqa: E402
from rq import Queue  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("auto_takeoff")


# ---------------------------------------------------------------------------
# Phase 1: split
# ---------------------------------------------------------------------------

def analyze_pdf(pdf_path: Path) -> dict:
    """Return per-page byte sizes (after writing each page individually)."""
    reader = PdfReader(str(pdf_path))
    page_sizes_mb = []
    log.info("Analyzing %s (%d pages)…", pdf_path.name, len(reader.pages))
    for i, page in enumerate(reader.pages):
        w = PdfWriter()
        w.add_page(page)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            w.write(tmp)
            tmp.flush()
            page_sizes_mb.append(os.path.getsize(tmp.name) / (1024 * 1024))
        if (i + 1) % 50 == 0:
            log.info("  …analyzed %d / %d pages", i + 1, len(reader.pages))
    return {
        "total_pages": len(reader.pages),
        "total_mb": sum(page_sizes_mb),
        "page_sizes_mb": page_sizes_mb,
    }


def plan_chunks(
    page_sizes_mb: list[float],
    max_mb: float,
    max_pages: int,
    dense_page_mb: float,
) -> list[tuple[int, int]]:
    """Return list of (start_page, end_page) inclusive, 0-indexed.

    Greedy: walk pages, start a new chunk when adding the next page would
    exceed max_mb OR max_pages. A page > dense_page_mb forces its own chunk.
    """
    chunks: list[tuple[int, int]] = []
    n = len(page_sizes_mb)
    i = 0
    while i < n:
        # Single-page chunk for dense pages
        if page_sizes_mb[i] > dense_page_mb:
            chunks.append((i, i))
            i += 1
            continue
        start = i
        size = 0.0
        count = 0
        while i < n and count < max_pages:
            if page_sizes_mb[i] > dense_page_mb:
                break  # next iteration will give it its own chunk
            if size + page_sizes_mb[i] > max_mb and count > 0:
                break
            size += page_sizes_mb[i]
            count += 1
            i += 1
        chunks.append((start, i - 1))
    return chunks


def write_chunks(
    pdf_path: Path,
    chunks: list[tuple[int, int]],
    out_dir: Path,
) -> list[Path]:
    """Materialize each chunk as its own PDF; return file paths."""
    reader = PdfReader(str(pdf_path))
    paths = []
    stem = pdf_path.stem
    for idx, (start, end) in enumerate(chunks, start=1):
        w = PdfWriter()
        for p in reader.pages[start:end + 1]:
            w.add_page(p)
        out = out_dir / f"{stem}_chunk{idx:02d}_pp{start+1}-{end+1}.pdf"
        with open(out, "wb") as f:
            w.write(f)
        sz_mb = out.stat().st_size / (1024 * 1024)
        log.info("  chunk %02d: pages %d-%d  (%.1f MB)", idx, start + 1, end + 1, sz_mb)
        paths.append(out)
    return paths


# ---------------------------------------------------------------------------
# Phase 2: submit
# ---------------------------------------------------------------------------

def _pick_queue(redis_conn, total_pages: int, max_size_bytes: int):
    """Mirror web_app._pick_queue: heavy if >10 pages or any file >30 MB."""
    if total_pages > 10 or max_size_bytes > 30 * 1024 * 1024:
        return Queue(RQ_QUEUE_HEAVY, connection=redis_conn), RQ_QUEUE_HEAVY
    return Queue(RQ_QUEUE_FAST, connection=redis_conn), RQ_QUEUE_FAST


def submit_chunk(
    chunk_path: Path,
    parent_label: str,
    contact: dict,
    user_id: int,
    org_id: int,
    redis_conn,
    chunk_idx: int,
    total_chunks: int,
) -> str:
    """Upload chunk to R2, create Submission row, enqueue RQ job.

    Returns the new submission_id (UUID string).
    """
    submission_id = str(uuid.uuid4())
    filename = chunk_path.name
    size_bytes = chunk_path.stat().st_size
    r2_key = storage.upload_key(submission_id, filename)
    business_name = f"{parent_label} [{chunk_idx}/{total_chunks}]"

    storage.upload_file(str(chunk_path), r2_key, content_type="application/pdf")

    with session_scope() as session:
        sub = Submission(
            id=submission_id,
            user_id=user_id,
            org_id=org_id,
            business_name=business_name,
            scope_notes=f"auto_takeoff chunk {chunk_idx}/{total_chunks} of {parent_label}",
            status="queued",
        )
        session.add(sub)
        session.add(File(
            submission_id=submission_id,
            kind="upload",
            filename=filename,
            r2_key=r2_key,
            size_bytes=size_bytes,
            content_type="application/pdf",
        ))

    # Page count for queue routing — re-read just to be sure.
    page_count = len(PdfReader(str(chunk_path)).pages)
    queue, queue_name = _pick_queue(redis_conn, page_count, size_bytes)
    queue.enqueue(
        "jobs.process_submission",
        kwargs={
            "submission_id": submission_id,
            "pdf_keys": [r2_key],
            "contact_info": {
                "name": contact["name"],
                "email": contact["email"],
                "phone": contact.get("phone", ""),
                "business_name": business_name,
            },
            "scope_notes": "",
            "rate_overrides": None,
        },
        job_id=submission_id,
        job_timeout=RQ_JOB_TIMEOUT,
        result_ttl=RQ_RESULT_TTL,
        failure_ttl=RQ_RESULT_TTL,
    )
    log.info("  ✓ submitted chunk %d → %s (queue=%s)", chunk_idx, submission_id, queue_name)
    return submission_id


# ---------------------------------------------------------------------------
# Phase 3: poll
# ---------------------------------------------------------------------------

TERMINAL_OK = {"completed"}
TERMINAL_BAD = {"failed", "cancelled"}


def poll_until_done(sids: list[str], poll_secs: int) -> dict[str, dict]:
    """Block until every sid is terminal. Return {sid: row_dict}."""
    pending = set(sids)
    final: dict[str, dict] = {}
    start = time.time()

    while pending:
        with session_scope() as session:
            rows = session.query(Submission).filter(
                Submission.id.in_(pending)
            ).all()
            for r in rows:
                if r.status in TERMINAL_OK | TERMINAL_BAD:
                    final[r.id] = {
                        "id": r.id,
                        "business_name": r.business_name,
                        "status": r.status,
                        "subtotal": float(r.subtotal) if r.subtotal else None,
                        "error": r.error,
                    }
                    pending.discard(r.id)

        elapsed = int(time.time() - start)
        done_ok = sum(1 for v in final.values() if v["status"] in TERMINAL_OK)
        done_bad = sum(1 for v in final.values() if v["status"] in TERMINAL_BAD)
        log.info(
            "  poll: %d/%d done (ok=%d, failed=%d), %d in flight, elapsed=%dm",
            len(final), len(sids), done_ok, done_bad, len(pending), elapsed // 60,
        )
        if pending:
            time.sleep(poll_secs)

    return final


# ---------------------------------------------------------------------------
# Phase 4: merge
# ---------------------------------------------------------------------------

def fetch_result_jsons(sids: list[str], scratch_dir: Path) -> dict[str, dict]:
    """Download each sid's analysis JSON from R2; return {sid: parsed_json}."""
    out: dict[str, dict] = {}
    with session_scope() as session:
        for sid in sids:
            files = session.query(File).filter(
                File.submission_id == sid,
                File.kind == "result",
                File.filename.like("construction_analysis_%.json"),
            ).all()
            if not files:
                log.warning("  no result JSON found for %s — skipping", sid)
                continue
            f = files[-1]  # latest
            local = scratch_dir / f"{sid}_{f.filename}"
            try:
                storage.download_file(f.r2_key, str(local))
                with open(local) as fh:
                    out[sid] = json.load(fh)
                log.info("  ✓ fetched %s (%d bytes)", sid, local.stat().st_size)
            except Exception as exc:
                log.error("  ✗ download failed for %s: %s", sid, exc)
    return out


def _sum_numeric(into: dict, src: dict) -> None:
    """Recursively sum numeric leaves from src into into."""
    for k, v in src.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            into[k] = into.get(k, 0) + v
        elif isinstance(v, dict):
            sub = into.setdefault(k, {})
            if isinstance(sub, dict):
                _sum_numeric(sub, v)


def merge_results(results: dict[str, dict], parent_label: str) -> tuple[dict, list[str]]:
    """Return (merged_dict, merge_report_lines)."""
    report = [f"=== Merge report for {parent_label} ==="]
    report.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    report.append(f"Source chunks: {len(results)}")
    report.append("")

    merged_subtotal = 0.0
    merged_aggregated: dict = {}
    merged_rooms: list = []
    merged_buildings: list = []
    merged_rfis: list = []
    merged_review_reasons: list = []
    seen_buildings: set = set()  # (name, footprint) dedup key
    chunk_summary = []

    for sid, j in results.items():
        sub = j.get("subtotal", 0) or 0
        try:
            sub = float(sub)
        except (TypeError, ValueError):
            sub = 0
        merged_subtotal += sub
        chunk_summary.append((sid, j.get("project_name") or sid[:8], sub))

        agg = j.get("aggregated_totals") or {}
        _sum_numeric(merged_aggregated, agg)

        # Rooms — concatenate, tag with source sid
        for room in (j.get("rooms") or []):
            if isinstance(room, dict):
                tagged = dict(room)
                tagged["_source_chunk_sid"] = sid
                merged_rooms.append(tagged)

        # Building inventory — dedupe by (name, footprint)
        for bldg in (j.get("building_inventory") or []):
            if not isinstance(bldg, dict):
                continue
            name = (bldg.get("building_name") or "").strip().lower()
            fp = bldg.get("footprint_sqft") or bldg.get("footprint") or 0
            try:
                fp_round = round(float(fp), 0)
            except (TypeError, ValueError):
                fp_round = 0
            key = (name, fp_round)
            if key in seen_buildings:
                report.append(f"  [dedupe] dropped duplicate building: {name!r} fp={fp_round}")
                continue
            seen_buildings.add(key)
            merged_buildings.append(bldg)

        # RFIs / manual-review reasons — concatenate
        for rfi in (j.get("rfis") or j.get("RFIs") or []):
            if isinstance(rfi, dict):
                tagged = dict(rfi)
                tagged["_source_chunk_sid"] = sid
                merged_rfis.append(tagged)
        if j.get("manual_review_reason"):
            merged_review_reasons.append(f"[{sid[:8]}] {j['manual_review_reason']}")

    report.append("Per-chunk subtotals:")
    for sid, name, sub in chunk_summary:
        report.append(f"  {sid[:8]}  {name:50s}  ${sub:>14,.2f}")
    report.append("")
    report.append(f"COMBINED subtotal: ${merged_subtotal:,.2f}")
    report.append(f"Combined rooms: {len(merged_rooms)}")
    report.append(f"Combined buildings (after dedupe): {len(merged_buildings)}")
    report.append(f"Combined RFIs: {len(merged_rfis)}")
    report.append("")

    merged = {
        "project_name": parent_label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_chunk_sids": list(results.keys()),
        "subtotal": round(merged_subtotal, 2),
        "aggregated_totals": merged_aggregated,
        "rooms": merged_rooms,
        "building_inventory": merged_buildings,
        "rfis": merged_rfis,
        "manual_review_reasons": merged_review_reasons,
    }
    return merged, report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pdf_path", type=Path)
    p.add_argument("--name", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--business", default=None)
    p.add_argument("--user-id", type=int, default=1)
    p.add_argument("--org-id", type=int, default=1)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--max-mb", type=float, default=50.0)
    p.add_argument("--max-pages", type=int, default=10)
    p.add_argument("--dense-page-mb", type=float, default=8.0)
    p.add_argument("--skip-submit", action="store_true")
    p.add_argument("--existing-sids", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--poll-secs", type=int, default=60)
    args = p.parse_args()

    pdf_path = args.pdf_path.resolve()
    if not pdf_path.exists():
        log.error("PDF not found: %s", pdf_path)
        sys.exit(1)

    parent_label = args.business or pdf_path.stem
    out_path = args.out or pdf_path.parent / f"{pdf_path.stem}_consolidated.json"
    report_path = out_path.with_suffix(".merge_report.txt")
    contact = {"name": args.name, "email": args.email}

    # ---- skip-submit / resume mode --------------------------------------
    if args.skip_submit:
        sids = [s.strip() for s in args.existing_sids.split(",") if s.strip()]
        if not sids:
            log.error("--skip-submit requires --existing-sids")
            sys.exit(2)
        log.info("Skip-submit mode: polling %d existing sids", len(sids))
    else:
        # ---- Phase 1: analyze + split ------------------------------------
        analysis = analyze_pdf(pdf_path)
        log.info("PDF: %d pages, %.1f MB total, page-size avg=%.1f MB",
                 analysis["total_pages"], analysis["total_mb"],
                 analysis["total_mb"] / max(1, analysis["total_pages"]))

        chunks = plan_chunks(
            analysis["page_sizes_mb"],
            max_mb=args.max_mb,
            max_pages=args.max_pages,
            dense_page_mb=args.dense_page_mb,
        )
        log.info("Plan: %d chunks", len(chunks))

        scratch = Path(tempfile.mkdtemp(prefix="auto_takeoff_"))
        log.info("Materializing chunks → %s", scratch)
        chunk_paths = write_chunks(pdf_path, chunks, scratch)

        if args.dry_run:
            log.info("DRY-RUN: would submit %d chunks. Stopping.", len(chunk_paths))
            return

        # ---- Phase 2: submit --------------------------------------------
        log.info("Submitting %d chunks…", len(chunk_paths))
        sids: list[str] = []
        redis_conn = Redis.from_url(REDIS_URL)
        for i, cp in enumerate(chunk_paths, start=1):
            sid = submit_chunk(
                cp, parent_label, contact,
                user_id=args.user_id, org_id=args.org_id,
                redis_conn=redis_conn,
                chunk_idx=i, total_chunks=len(chunk_paths),
            )
            sids.append(sid)
            # Print after every submit so you can resume on Ctrl-C.
            print(f"\n--existing-sids {','.join(sids)}\n", flush=True)

    # ---- Phase 3: poll ---------------------------------------------------
    log.info("Polling %d submissions every %ds…", len(sids), args.poll_secs)
    final = poll_until_done(sids, poll_secs=args.poll_secs)

    failed = [sid for sid, row in final.items() if row["status"] in TERMINAL_BAD]
    ok = [sid for sid, row in final.items() if row["status"] in TERMINAL_OK]
    log.info("Done: %d ok, %d failed", len(ok), len(failed))
    if failed:
        for sid in failed:
            log.warning("  ✗ %s — %s — %s",
                        sid, final[sid]["status"],
                        (final[sid]["error"] or "")[:200])

    # ---- Phase 4: merge --------------------------------------------------
    log.info("Fetching result JSONs…")
    scratch = Path(tempfile.mkdtemp(prefix="auto_takeoff_results_"))
    results = fetch_result_jsons(ok, scratch)

    log.info("Merging %d JSONs…", len(results))
    merged, report = merge_results(results, parent_label)

    out_path.write_text(json.dumps(merged, indent=2, default=str))
    report_path.write_text("\n".join(report))
    log.info("✓ Wrote consolidated JSON: %s", out_path)
    log.info("✓ Wrote merge report:      %s", report_path)
    log.info("Combined subtotal: $%s", f"{merged['subtotal']:,.2f}")

    if failed:
        log.warning("WARNING: %d chunks failed — combined estimate is incomplete.", len(failed))
        sys.exit(3)


if __name__ == "__main__":
    main()
