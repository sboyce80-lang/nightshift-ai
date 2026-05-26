#!/usr/bin/env python3
"""
Knight Shift — Job Queue Workers
================================
Functions executed by RQ workers. This module must NOT import Flask, so an
`rq worker` process can load it without dragging in the web framework.

Source of truth for status is the `submissions` table in Postgres.
R2 holds the actual files (uploads + results).

Public entry point:
    process_submission(submission_id, pdf_keys, contact_info, scope_notes)
"""

import os
import sys
import logging
import smtplib
import tempfile
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

# Ensure local imports work whether invoked as `rq worker` or directly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    EMAIL_ADDRESS, EMAIL_APP_PASSWORD,
    EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT,
    COMPANY_NAME, COMPANY_EMAIL, COMPANY_PHONE,
    ADMIN_EMAILS,
)
import storage
from db import session_scope
from models import Submission, File, Organization
from Takeoff_DIRECT import run_analysis, run_analysis_merge
from generate_estimate_pdf import generate_estimate_pdf

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("nightshift.jobs")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.FileHandler(os.path.join(LOG_DIR, "worker.log"))
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def update_status(submission_id, status, error=None, subtotal=None):
    """Patch the submission row's status (and optionally error/subtotal).

    Uses a raw UPDATE (not the ORM session.get + dirty-tracking path) so
    behavior is predictable even on the work-horse's first-ever DB call,
    and logs the affected rowcount so a silent no-op is visible in logs.
    """
    values = {
        "status": status,
        "updated_at": datetime.now(timezone.utc),
    }
    if error is not None:
        values["error"] = error[:2000]
    if subtotal is not None:
        values["subtotal"] = subtotal

    try:
        with session_scope() as session:
            stmt = (
                Submission.__table__.update()
                .where(Submission.id == submission_id)
                .values(**values)
            )
            result = session.execute(stmt)
            if result.rowcount == 0:
                logger.warning("update_status: no row affected for %s (status=%s)",
                               submission_id, status)
            else:
                logger.info("update_status: %s -> %s (rows=%d)",
                            submission_id, status, result.rowcount)
    except Exception as exc:
        logger.warning("Could not update status for %s -> %s: %s",
                       submission_id, status, exc, exc_info=True)


# Submissions older than this still in queued/processing on worker startup
# are treated as abandoned. 4h leaves a wide margin past the longest
# realistic DD-scale takeoff (~1.5h) so live jobs are never false-positived.
ABANDONED_AGE_SECONDS = int(os.environ.get("ABANDONED_AGE_SECONDS", "14400"))


def reconcile_abandoned_submissions(redis_conn, queue_names):
    """Sweep DB rows whose RQ jobs are gone or failed but DB still says active.

    Called from worker startup. When a worker is killed by OOM, deploy,
    Render eviction, or RQ's own job_timeout, the SIGKILL bypasses Python
    and `update_status('failed')` never runs. The DB row stays at
    queued/processing forever. RQ marks the job AbandonedJobError in its
    failed registry, but we don't pick that up unless we look.

    For each old still-active DB row, check what RQ thinks:
        - job missing entirely      -> ghost, mark failed
        - job in failed registry    -> mark failed, copy exc info
        - job still queued/started  -> leave alone (legitimate long run)
        - job finished              -> anomaly, mark failed with note

    Idempotent and safe to run repeatedly. Returns a count of rows changed.
    """
    from rq.job import Job
    from rq.exceptions import NoSuchJobError

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ABANDONED_AGE_SECONDS)
    rows_changed = 0

    try:
        with session_scope() as session:
            stuck = (session.query(Submission)
                     .filter(Submission.status.in_(("queued", "processing")),
                             Submission.updated_at < cutoff)
                     .all())
            stuck_snapshot = [
                (s.id, s.status, s.updated_at) for s in stuck
            ]
    except Exception as exc:
        logger.warning("Reconcile: failed to query stuck submissions: %s",
                       exc, exc_info=True)
        return 0

    if not stuck_snapshot:
        logger.info("Reconcile: no stuck submissions older than %ds", ABANDONED_AGE_SECONDS)
        return 0

    logger.info("Reconcile: examining %d candidate submission(s) older than %ds",
                len(stuck_snapshot), ABANDONED_AGE_SECONDS)

    for sid, db_status, last_seen in stuck_snapshot:
        # 1. Look up the RQ job by its id (which equals submission_id).
        try:
            job = Job.fetch(sid, connection=redis_conn)
            rq_status = job.get_status()
            exc_summary = (job.exc_info or "").splitlines()[-1] if job.exc_info else ""
        except NoSuchJobError:
            job = None
            rq_status = "missing"
            exc_summary = ""
        except Exception as exc:
            logger.warning("Reconcile: Job.fetch failed for %s: %s", sid, exc)
            continue

        # 2. Active RQ states mean the job is legitimately still in flight.
        if rq_status in ("queued", "started", "scheduled", "deferred"):
            logger.info("Reconcile: %s still %s in RQ — leaving alone",
                        sid, rq_status)
            continue

        # 3. Anything else is a stuck row. Build a clear error message and
        #    flip the DB row to 'failed' (use update_status so the change
        #    goes through the same observable code path).
        if rq_status == "missing":
            err_msg = (
                "Worker crashed before completion (RQ job no longer exists). "
                f"Last seen at {last_seen.isoformat()}. Please re-submit."
            )
        elif rq_status == "failed":
            tail = exc_summary or "AbandonedJobError or unhandled exception"
            err_msg = (
                f"Worker crashed before completion: {tail}. "
                f"Last seen at {last_seen.isoformat()}. Please re-submit."
            )
        elif rq_status == "finished":
            err_msg = (
                "Anomaly: RQ marked the job finished but the DB never recorded "
                "completion. Files (if any) may be in storage — contact support."
            )
        else:
            err_msg = (
                f"Worker reconciliation found job in unexpected state: {rq_status}. "
                f"Last seen at {last_seen.isoformat()}. Please re-submit."
            )

        update_status(sid, "failed", error=err_msg)
        logger.warning("Reconcile: %s flipped to 'failed' (was '%s', RQ='%s')",
                       sid, db_status, rq_status)
        rows_changed += 1

    logger.info("Reconcile: complete — %d row(s) reconciled", rows_changed)
    return rows_changed


def _build_and_upload_annotated_drawings(submission_id, result, local_pdfs, workdir):
    """Render an annotated copy of each source PDF with room bboxes drawn on
    each referenced page, and upload as additional result file(s).

    One annotated PDF per source PDF, named `<original_basename>.annotated.pdf`.
    Skipped silently if no rooms have bbox info (e.g. bbox attachment failed
    upstream, or the result is from a code path that doesn't run it).

    Best-effort — failures are logged but do NOT fail the submission.
    """
    try:
        from bbox_spike import render_annotated_pdf, annotated_drawings_filename

        analysis = (result or {}).get("analysis") or {}
        rooms_iter = (r for f in analysis.get("floors", []) or []
                      for r in f.get("rooms", []) or [])

        # Group rooms by the source_pdf path recorded at attach_label_bboxes time.
        # Fall back to basename lookup against local_pdfs if the absolute path
        # doesn't survive (e.g. workdir reused across runs).
        by_source: dict[str, int] = {}
        for r in rooms_iter:
            b = r.get("bbox") or {}
            src = b.get("source_pdf")
            if src:
                by_source[src] = by_source.get(src, 0) + 1

        if not by_source:
            logger.info("Annotated drawings: no bbox info present, skipping for %s",
                        submission_id)
            return []

        # Build a basename → local-path map for fallback resolution
        local_by_basename = {os.path.basename(p): p for p in (local_pdfs or [])}

        uploaded = []
        for src_path, room_count in by_source.items():
            resolved = src_path if os.path.exists(src_path) \
                else local_by_basename.get(os.path.basename(src_path))
            if not resolved or not os.path.exists(resolved):
                logger.warning("Annotated drawings: source PDF not found for %s "
                               "(tried %s, basename %s); skipping",
                               submission_id, src_path, os.path.basename(src_path))
                continue

            out_filename = annotated_drawings_filename(os.path.basename(resolved))
            out_path = os.path.join(workdir, out_filename)

            summary = render_annotated_pdf(resolved, result, out_path)
            logger.info("Annotated drawings for %s/%s: %d/%d pages referenced, "
                        "%d rooms drawn, %d misses, %.1f MB",
                        submission_id, out_filename,
                        summary["referenced_pages"], summary["pages"],
                        summary["rooms_drawn"], summary["misses_marked"],
                        summary["output_size_bytes"] / 1024 / 1024)

            r2_key = storage.result_key(submission_id, out_filename)
            storage.upload_file(out_path, r2_key, content_type="application/pdf")
            _record_result_file(submission_id, out_filename, r2_key,
                                os.path.getsize(out_path), "application/pdf")
            uploaded.append(out_path)

        return uploaded
    except Exception as exc:
        logger.error("Annotated drawings generation failed for %s: %s",
                     submission_id, exc, exc_info=True)
        return []


def _build_and_upload_estimate(submission_id, result, workdir):
    """Render the formal Estimate PDF and upload it as a third result file.

    Best-effort: any failure (missing org row, WeasyPrint not installed,
    bad logo URL, etc.) is logged but does NOT fail the submission. The
    full job PDF + JSON are the source of truth; the Estimate is a
    convenience deliverable.

    Returns the local PDF path on success, or None.
    """
    try:
        with session_scope() as session:
            sub = session.get(Submission, submission_id)
            if sub is None:
                logger.warning("Estimate skipped: submission %s not found", submission_id)
                return None
            org = session.get(Organization, sub.org_id)
            if org is None:
                logger.warning("Estimate skipped: org %s not found for submission %s",
                               sub.org_id, submission_id)
                return None
            # Detach so we can use the rows after the session closes.
            session.expunge(sub)
            session.expunge(org)

        pdf_path = generate_estimate_pdf(sub, org, result, workdir)
        filename = os.path.basename(pdf_path)
        r2_key = storage.result_key(submission_id, filename)
        size_bytes = os.path.getsize(pdf_path)
        storage.upload_file(pdf_path, r2_key, content_type="application/pdf")
        _record_result_file(submission_id, filename, r2_key, size_bytes, "application/pdf")
        return pdf_path
    except Exception as exc:
        logger.error("Estimate PDF generation failed for %s: %s",
                     submission_id, exc, exc_info=True)
        return None


def _record_result_file(submission_id, filename, r2_key, size_bytes, content_type):
    """Idempotently record a result file in the `files` table."""
    try:
        with session_scope() as session:
            existing = session.query(File).filter(
                File.submission_id == submission_id,
                File.kind == "result",
                File.filename == filename,
            ).one_or_none()
            if existing:
                existing.r2_key = r2_key
                existing.size_bytes = size_bytes
                existing.content_type = content_type
            else:
                session.add(File(
                    submission_id=submission_id,
                    kind="result",
                    filename=filename,
                    r2_key=r2_key,
                    size_bytes=size_bytes,
                    content_type=content_type,
                ))
    except Exception as exc:
        logger.warning("Could not record result file %s for %s: %s",
                       filename, submission_id, exc)


# ---------------------------------------------------------------------------
# Main worker entry point — RQ calls this
# ---------------------------------------------------------------------------

def process_submission(submission_id, pdf_keys, contact_info, scope_notes,
                        rate_overrides=None):
    """Run the full takeoff pipeline for a submission.

    Args:
        submission_id: UUID, also used as the RQ job id and the R2 prefix.
        pdf_keys: list of R2 object keys (e.g. submissions/<id>/uploads/X.pdf).
        contact_info: dict with name, email, phone, business_name.
        scope_notes: free-form scope text.
        rate_overrides: optional dict of pricing overrides applied to
                        PRICING_MODEL via Takeoff_DIRECT._apply_rate_overrides.

    Workflow:
        1. Mark submission `processing` in the DB.
        2. Pull every input from R2 to a tempdir.
        3. Hand local paths to run_analysis().
        4. Upload output JSON + PDF back to R2 under .../results/.
        5. Record `files` rows for the results.
        6. Email the contact.
        7. Mark `completed` (with subtotal) in the DB.

    Failures: mark `failed` + email; re-raise so RQ records job as failed.
    """
    logger.info("Processing submission %s (%d PDFs)", submission_id, len(pdf_keys))
    update_status(submission_id, "processing")

    with tempfile.TemporaryDirectory(prefix=f"ns-job-{submission_id}-") as workdir:
        local_pdfs = []
        try:
            for key in pdf_keys:
                filename = key.rsplit("/", 1)[-1]
                local_path = os.path.join(workdir, filename)
                storage.download_file(key, local_path)
                local_pdfs.append(local_path)

            # Pre-normalize any pages that would be too dense for the heavy
            # worker to process without triggering Render's CPU-load
            # preemption, or that would blow Claude's 5 MB per-image cap on
            # image-fallback. Pages whose single-serialized PDF size exceeds
            # 5 MB get rasterized to JPEG-embedded PDF pages at 150 DPI.
            # Lean pages pass through untouched so small files incur no
            # quality penalty. See pdf_preprocess.py docstring for
            # background. Toggleable via NIGHTSHIFT_DISABLE_PDF_NORMALIZE=1.
            try:
                from pdf_preprocess import normalize_oversized_pages
                normalized_pdfs = []
                for p in local_pdfs:
                    res = normalize_oversized_pages(p)
                    if res["did_normalize"]:
                        logger.info(
                            "Normalized %s: %d/%d pages rasterized at %d DPI "
                            "(%.1f MB → %.1f MB)",
                            os.path.basename(p),
                            res["pages_normalized"], res["total_pages"],
                            res["dpi"], res["src_size_mb"], res["dst_size_mb"]
                        )
                        normalized_pdfs.append(res["dst_path"])
                    else:
                        normalized_pdfs.append(p)
                local_pdfs = normalized_pdfs
            except Exception as norm_exc:
                # Don't let preprocessing failures kill the job — fall
                # through to analysis on the original PDFs and let the
                # downstream cascade handle whatever it can.
                logger.warning(
                    "PDF normalization failed for %s — analyzing unmodified PDFs (%s)",
                    submission_id, norm_exc, exc_info=True
                )

            # Multi-pass extraction (always-on as of 2026-05-26): re-extract
            # floor-plan PDFs and keep whichever pass found more rooms. Adds
            # ~30s + 1× API cost per FP file, eliminates the LLM-variance
            # downside we saw on the 397Fishkill twin-run regression
            # ($100K → $88K on identical input, same code). Toggle off via
            # NIGHTSHIFT_MULTI_PASS=0 if rate-limit cost becomes an issue.
            _multi_pass = os.environ.get("NIGHTSHIFT_MULTI_PASS", "1").strip() != "0"
            result = run_analysis(
                local_pdfs,
                contact_name=contact_info["name"],
                contact_email=contact_info["email"],
                scope_notes=scope_notes,
                rate_overrides=rate_overrides,
                multi_pass=_multi_pass,
            )

            for key_name, content_type in (
                ("output_json_path", "application/json"),
                ("output_pdf_path", "application/pdf"),
            ):
                src = result.get(key_name)
                if src and os.path.exists(src):
                    filename = os.path.basename(src)
                    r2_key = storage.result_key(submission_id, filename)
                    size_bytes = os.path.getsize(src)
                    storage.upload_file(src, r2_key, content_type=content_type)
                    _record_result_file(submission_id, filename, r2_key,
                                        size_bytes, content_type)

            # Third deliverable: formal Estimate PDF. Failure here doesn't
            # block completion — the full PDF/JSON are the source of truth.
            _build_and_upload_estimate(submission_id, result, workdir)

            # Fourth deliverable: Annotated Drawings PDF — each source page
            # rendered with room bboxes drawn on top, so the contractor (and
            # we) can visually confirm what was measured and spot missed
            # sheets at a glance. Best-effort.
            annotated_pdf_paths = _build_and_upload_annotated_drawings(
                submission_id, result, local_pdfs, workdir,
            )

            send_result_email(contact_info, result,
                              extra_attachment_paths=annotated_pdf_paths)

            subtotal = result.get("cost_estimate", {}).get("subtotal", 0) or 0
            update_status(submission_id, "completed", subtotal=subtotal)
            logger.info("Submission %s completed — $%s estimate",
                        submission_id, f"{subtotal:,.2f}")

            return {"submission_id": submission_id, "subtotal": subtotal}

        except Exception as exc:
            # If the user cancelled this job mid-flight, the DB row is already
            # "cancelled" — don't overwrite with "failed" or send an error email.
            with session_scope() as session:
                sub = session.get(Submission, submission_id)
                if sub is not None and sub.status == "cancelled":
                    logger.info("Submission %s cancelled mid-run; suppressing failure path",
                                submission_id)
                    raise

            logger.error("Submission %s failed: %s", submission_id, exc, exc_info=True)
            update_status(submission_id, "failed", error=str(exc))
            try:
                send_error_email(contact_info, str(exc))
            except Exception as email_exc:
                logger.error("Failed to send error email: %s", email_exc)
            raise


# ---------------------------------------------------------------------------
# Merge worker — incremental re-run on a parent submission's stored JSON
# ---------------------------------------------------------------------------

def _find_parent_result_json_key(parent_id):
    """Return the R2 key of parent_id's most recent result JSON, or None.

    A submission can have at most one result JSON (uq_files_submission_kind_filename
    enforces uniqueness on filename), but if a worker rerun ever produced a
    second one we pick the latest by created_at.
    """
    with session_scope() as session:
        row = (
            session.query(File)
            .filter(
                File.submission_id == parent_id,
                File.kind == "result",
                File.filename.like("%.json"),
            )
            .order_by(File.created_at.desc())
            .first()
        )
        return row.r2_key if row else None


def merge_submission(submission_id, parent_id, new_pdf_keys, contact_info,
                      scope_notes=None, scope_tags=None, rate_overrides=None,
                      sheet_hint=None):
    """Incremental re-run for a v2+ child submission.

    Loads the parent's stored result JSON from R2, runs extraction on ONLY
    the new PDFs, calls run_analysis_merge() to merge + recompute, uploads
    the new result files under THIS submission's prefix, emails the contact,
    and marks the child `completed` with the new subtotal.

    Args:
        submission_id: UUID of the new (v2+) submission row to update.
        parent_id: UUID of the parent (v1 or earlier) whose JSON is baseline.
        new_pdf_keys: R2 keys of files uploaded for THIS version only.
        contact_info: same dict shape as process_submission.
        scope_notes: optional string the user typed describing the change.
        scope_tags: optional list like ["Basement","DoorSchedule"] driving
                    replace-vs-union semantics in merge_analyses().
        rate_overrides: passed through to run_analysis_merge for symmetry,
                        but pricing primarily uses the parent's snapshot.

    Failures: mark `failed` + email; re-raise so RQ records the failure.
    """
    logger.info("Merging submission %s onto parent %s (%d new PDFs, tags=%s)",
                submission_id, parent_id, len(new_pdf_keys), scope_tags)
    update_status(submission_id, "processing")

    parent_json_key = _find_parent_result_json_key(parent_id)
    if not parent_json_key:
        msg = (f"Parent submission {parent_id} has no stored result JSON — "
               f"cannot merge. Re-submit fresh instead.")
        logger.error(msg)
        update_status(submission_id, "failed", error=msg)
        try:
            send_error_email(contact_info, msg)
        except Exception:
            pass
        raise RuntimeError(msg)

    with tempfile.TemporaryDirectory(prefix=f"ns-merge-{submission_id}-") as workdir:
        local_pdfs = []
        try:
            # Pull parent JSON
            import json
            parent_json_local = os.path.join(workdir, "_parent_result.json")
            storage.download_file(parent_json_key, parent_json_local)
            with open(parent_json_local, "r") as fh:
                prior_json = json.load(fh)

            # Pull new PDFs
            for key in new_pdf_keys:
                filename = key.rsplit("/", 1)[-1]
                local_path = os.path.join(workdir, filename)
                storage.download_file(key, local_path)
                local_pdfs.append(local_path)

            result = run_analysis_merge(
                prior_json,
                local_pdfs,
                scope_tags=scope_tags or [],
                contact_name=contact_info.get("name", ""),
                contact_email=contact_info.get("email", ""),
                scope_notes=scope_notes or "",
                sheet_hint=sheet_hint,
                rate_overrides=rate_overrides,
            )

            for key_name, content_type in (
                ("output_json_path", "application/json"),
                ("output_pdf_path", "application/pdf"),
            ):
                src = result.get(key_name)
                if src and os.path.exists(src):
                    filename = os.path.basename(src)
                    r2_key = storage.result_key(submission_id, filename)
                    size_bytes = os.path.getsize(src)
                    storage.upload_file(src, r2_key, content_type=content_type)
                    _record_result_file(submission_id, filename, r2_key,
                                        size_bytes, content_type)

            _build_and_upload_estimate(submission_id, result, workdir)

            send_result_email(contact_info, result)

            subtotal = result.get("cost_estimate", {}).get("subtotal", 0) or 0
            update_status(submission_id, "completed", subtotal=subtotal)
            logger.info("Merge submission %s completed — $%s estimate",
                        submission_id, f"{subtotal:,.2f}")

            return {"submission_id": submission_id,
                    "parent_id": parent_id,
                    "subtotal": subtotal}

        except Exception as exc:
            with session_scope() as session:
                sub = session.get(Submission, submission_id)
                if sub is not None and sub.status == "cancelled":
                    logger.info("Merge %s cancelled mid-run; suppressing failure path",
                                submission_id)
                    raise

            logger.error("Merge submission %s failed: %s", submission_id, exc, exc_info=True)
            update_status(submission_id, "failed", error=str(exc))
            try:
                send_error_email(contact_info, str(exc))
            except Exception as email_exc:
                logger.error("Failed to send error email: %s", email_exc)
            raise


# ---------------------------------------------------------------------------
# Email Notifications
# ---------------------------------------------------------------------------

def send_email_with_attachments(to_email, subject, body, attachment_paths,
                                 from_name=None, cc=None):
    """Send a plaintext email with PDF/JSON attachments over the same SMTP
    relay used by send_result_email.

    Args:
        to_email:         single recipient email address (string).
        subject:          plain text subject line.
        body:             plain text body. UTF-8 safe.
        attachment_paths: iterable of local file paths to attach.
                          Extension drives the MIME subtype (pdf/json/octet).
        from_name:        display name; defaults to COMPANY_NAME.
        cc:               optional list of CC addresses.

    Returns True on send, False if SMTP isn't configured. Raises on send failure.
    """
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        logger.warning("SMTP not configured — cannot send email to %s", to_email)
        return False

    msg = MIMEMultipart()
    msg["From"] = f"{from_name or COMPANY_NAME} <{EMAIL_ADDRESS}>"
    msg["To"] = to_email
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", _charset="utf-8"))

    for path in attachment_paths or []:
        if not path or not os.path.exists(path):
            continue
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        subtype = {"pdf": "pdf", "json": "json"}.get(ext, "octet-stream")
        with open(path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype=subtype)
            att.add_header(
                "Content-Disposition", "attachment",
                filename=os.path.basename(path),
            )
            msg.attach(att)

    recipients = [to_email] + (list(cc) if cc else [])
    with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        server.sendmail(EMAIL_ADDRESS, recipients, msg.as_string())
    logger.info("Sent email '%s' to %s", subject, to_email)
    return True


def send_result_email(contact_info, result, extra_attachment_paths=None):
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        logger.warning("SMTP not configured — skipping email notification")
        return

    costs = result.get("cost_estimate", {})
    analysis = result.get("analysis", {})
    totals = analysis.get("aggregated_totals", {})
    project = analysis.get("project_info", {})

    items_text = ""
    for item in costs.get("line_items", []):
        if item.get("qty", 0) > 0:
            items_text += f"  - {item['item']}: ${item['total']:,.2f}\n"

    body = f"""Hi {contact_info['name']},

Thank you for submitting your construction documents through Knight Shift. Your painting estimate is ready.

PROJECT SUMMARY
  Floors analyzed: {project.get('total_floors_analyzed', 'N/A')}
  Rooms found:     {project.get('total_rooms_found', 'N/A')}

MEASUREMENTS EXTRACTED
  Paintable walls:    {totals.get('total_paintable_wall_sqft', 0):,.0f} sq ft
  Paintable ceilings: {totals.get('total_paintable_ceiling_sqft', 0):,.0f} sq ft
  Base trim:          {totals.get('total_base_trim_lf', 0):,.0f} linear feet
  Doors (full paint): {totals.get('total_doors_full_paint', 0):,.0f}
  Doors (HM panel):   {totals.get('total_doors_hm_panel', 0):,.0f}
  Windows (painted):  {totals.get('total_windows_painted_interior', 0):,.0f}
  Stair sections:     {totals.get('total_stair_sections', 0):,.0f}

COST ESTIMATE
{items_text}
  TOTAL: ${costs.get('subtotal', 0):,.2f}

IMPORTANT: This is a preliminary estimate generated automatically from your
drawings. A formal proposal will follow after review.

The detailed analysis is attached as a PDF report.

Best regards,
{COMPANY_NAME}
{COMPANY_PHONE}
{COMPANY_EMAIL}
"""

    msg = MIMEMultipart()
    msg["From"] = f"{COMPANY_NAME} <{EMAIL_ADDRESS}>"
    msg["To"] = f"{contact_info['name']} <{contact_info['email']}>"
    cc_addrs = sorted(a for a in ADMIN_EMAILS
                      if a != (contact_info.get("email") or "").lower())
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = "Knight Shift - Your Painting Estimate is Ready"
    msg.attach(MIMEText(body, "plain"))

    pdf_path = result.get("output_pdf_path")
    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="pdf")
            att.add_header(
                "Content-Disposition", "attachment",
                filename=os.path.basename(pdf_path),
            )
            msg.attach(att)

    json_path = result.get("output_json_path")
    if json_path and os.path.exists(json_path):
        with open(json_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="json")
            att.add_header(
                "Content-Disposition", "attachment",
                filename=os.path.basename(json_path),
            )
            msg.attach(att)

    for extra_path in extra_attachment_paths or []:
        if not extra_path or not os.path.exists(extra_path):
            continue
        with open(extra_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="pdf")
            att.add_header(
                "Content-Disposition", "attachment",
                filename=os.path.basename(extra_path),
            )
            msg.attach(att)

    try:
        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        logger.info("Result email sent to %s", contact_info["email"])
    except Exception as exc:
        logger.error("Failed to send result email: %s", exc)


def send_error_email(contact_info, error_msg):
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        logger.warning("SMTP not configured — skipping error email")
        return

    body = f"""Hi {contact_info['name']},

Thank you for submitting your construction documents through Knight Shift.

Unfortunately, our system encountered an issue processing your documents:
  {error_msg}

This may happen when drawings are in an unsupported format or contain
elements our system can't yet interpret.

Please reply to this email or call {COMPANY_PHONE} for assistance.

Best regards,
{COMPANY_NAME}
"""

    msg = MIMEMultipart()
    msg["From"] = f"{COMPANY_NAME} <{EMAIL_ADDRESS}>"
    msg["To"] = f"{contact_info['name']} <{contact_info['email']}>"
    if (contact_info.get("email") or "").lower() != "admin@knightshiftai.com":
        msg["Cc"] = "admin@knightshiftai.com"
    msg["Subject"] = "Knight Shift - Issue Processing Your Documents"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        logger.info("Error email sent to %s", contact_info["email"])
    except Exception as exc:
        logger.error("Failed to send error email: %s", exc)
