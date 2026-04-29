#!/usr/bin/env python3
"""
Knight Shift — Web Form for RFP Submission
=============================================
Flask application that provides a branded web form for submitting
construction document PDFs for automated painting estimates.

Flow:
    1. Browser POSTs form + PDFs to /submit.
    2. Flask validates input, stages files in a temp dir.
    3. Each PDF is streamed to Cloudflare R2 under
       submissions/<id>/uploads/<filename>.
    4. A row is created in the `submissions` table (and `files`).
    5. An RQ job is enqueued; Redis hands it to a worker.
    6. Worker downloads inputs from R2, runs the takeoff, uploads
       results back to R2, updates the DB, and emails the contact.

Source of truth: Postgres. R2 holds files only.

Usage:
    Development:  python web_app.py        (terminal 1)
                  python worker.py         (terminal 2)
                  redis-server             (terminal 3 if not already running)
    Production:   gunicorn --bind 0.0.0.0:8080 --workers 2 --timeout 120 wsgi:app
                  rq worker nightshift     # one or more worker processes
"""

import os
import re
import sys
import json
import uuid
import logging
import tempfile

from flask import Flask, request, render_template, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

from redis import Redis
from rq import Queue
from rq.job import Job
from rq.exceptions import NoSuchJobError

# ---------------------------------------------------------------------------
# Ensure local imports work
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    MAX_PDF_SIZE_MB, MAX_PDFS_PER_EMAIL,
    WEB_PORT, FLASK_SECRET_KEY,
    REDIS_URL, RQ_QUEUE_FAST, RQ_QUEUE_HEAVY,
    HEAVY_QUEUE_PAGE_THRESHOLD, HEAVY_QUEUE_FILE_MB,
    RQ_JOB_TIMEOUT, RQ_RESULT_TTL,
    BETA_DAILY_SUBMISSION_CAP_DEFAULT,
    CLERK_PUBLISHABLE_KEY,
)
import storage
from datetime import datetime, timezone, timedelta

from db import session_scope
from models import User, Submission, File, Organization, OrganizationMembership
from auth import require_auth, current_user_id, clerk_frontend_api_host, is_admin
from orgs import FREE_EMAIL_DOMAINS, _domain_of
from notifications import (
    notify_admin_of_new_signup,
    notify_user_of_approval,
    notifications_configured,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "web_app.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("nightshift.web")

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY or os.urandom(24).hex()

# Trust Render's proxy headers so request.url reflects the public hostname
# (knightshiftai.com) instead of the internal localhost:$PORT. Without this,
# server-side redirects (e.g. Clerk sign-in return URL) point users at localhost.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.config["MAX_CONTENT_LENGTH"] = MAX_PDF_SIZE_MB * MAX_PDFS_PER_EMAIL * 1024 * 1024

ALLOWED_EXTENSIONS = {".pdf"}

# ---------------------------------------------------------------------------
# Pricing — fields surfaced in the /pricing settings UI. Keys match the
# shorthand accepted by Takeoff_DIRECT._apply_rate_overrides().
# ---------------------------------------------------------------------------
RATE_FIELDS = [
    ("wall_rate",     "Interior walls (gyp)",     "sqft", "1.25"),
    ("ceiling_rate",  "Ceilings (gyp)",           "sqft", "1.25"),
    ("door_rate",     "Doors (full paint)",       "ea",   "225.00"),
    ("window_rate",   "Windows",                  "ea",   "120.00"),
    ("trim_rate",     "Base trim",                "lf",   "3.25"),
    ("stair_rate",    "Stairs",                   "ea",   "1500.00"),
    ("cmu_rate",      "CMU walls (full)",         "sqft", "1.10"),
    ("dryfall_rate",  "Dryfall ceilings",         "sqft", "0.90"),
    ("concrete_rate", "Concrete sealer",          "sqft", "2.20"),
    ("column_rate",   "Painted columns",          "ea",   "200.00"),
]


def _flatten_overrides(po):
    """Convert {"rates": {...}, "markup": x} -> flat dict for run_analysis."""
    if not po:
        return None
    flat = dict(po.get("rates") or {})
    if po.get("markup") is not None:
        flat["markup"] = po["markup"]
    return flat or None

# ---------------------------------------------------------------------------
# Job Queue
# ---------------------------------------------------------------------------
_redis = Redis.from_url(REDIS_URL)
# Two queues, two dedicated workers (see render.yaml). The fast queue keeps
# small jobs from getting stuck behind a 30-min DD-scale takeoff.
_queue_fast = Queue(RQ_QUEUE_FAST, connection=_redis)
_queue_heavy = Queue(RQ_QUEUE_HEAVY, connection=_redis)


def _count_pdf_pages(path):
    """Best-effort page count. Returns 0 on failure so routing falls back
    to size-based heuristic — never blocks a submission on a broken PDF."""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(path, strict=False)
        return len(reader.pages)
    except Exception as exc:
        logger.warning("page count failed for %s: %s", path, exc)
        return 0


def _pick_queue(total_pages, max_size_bytes):
    max_mb = max_size_bytes / (1024 * 1024)
    if total_pages >= HEAVY_QUEUE_PAGE_THRESHOLD or max_mb >= HEAVY_QUEUE_FILE_MB:
        return _queue_heavy, RQ_QUEUE_HEAVY
    return _queue_fast, RQ_QUEUE_FAST


# ---------------------------------------------------------------------------
# Template context — Clerk frontend keys available in every render
# ---------------------------------------------------------------------------

@app.context_processor
def _inject_clerk_context():
    try:
        host = clerk_frontend_api_host() if CLERK_PUBLISHABLE_KEY else ""
    except Exception:
        host = ""
    return {
        "clerk_publishable_key": CLERK_PUBLISHABLE_KEY,
        "clerk_frontend_api_host": host,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _effective_user_overrides(user):
    """Build {rate_key: float, markup: float} reflecting the user's saved
    overrides over the global PRICING_MODEL defaults. Used to pre-fill the
    inline pricing table on the New Estimate page."""
    from config import PRICING_MODEL  # local import — avoid module-level cost

    # Map RATE_FIELDS shorthand -> PRICING_MODEL key (mirrors Takeoff_DIRECT).
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
    }
    org = user.current_organization if user else None
    saved = (org.pricing_overrides or {}) if org else {}
    saved_rates = saved.get("rates") or {}
    saved_markup = saved.get("markup")

    rates = {}
    for shorthand, _label, _unit, _default in RATE_FIELDS:
        if shorthand in saved_rates:
            rates[shorthand] = float(saved_rates[shorthand])
        else:
            pm_key = _rate_map.get(shorthand)
            tiers = (PRICING_MODEL.get(pm_key) or {}).get("tiers") or []
            rates[shorthand] = float(tiers[0]["rate"]) if tiers else 0.0

    markup = float(saved_markup) if saved_markup is not None else 0.06
    return rates, markup


_MOBILE_UA_RE = re.compile(
    r"(iPhone|iPod|Android.*Mobile|BlackBerry|IEMobile|Opera Mini|Windows Phone)",
    re.IGNORECASE,
)


def _is_mobile_ua():
    """True if the User-Agent looks like a phone. iPad and Android tablets
    are intentionally excluded — they get the desktop layout."""
    ua = (request.user_agent.string or "")
    return bool(_MOBILE_UA_RE.search(ua))


def _try_signed_in_user_snapshot():
    """Return (user_id, org_state_dict) if the request has a verifiable Clerk
    session and a synced local User row, else (None, None). Used by `/` to
    decide whether to render the landing page, the waitlist, the onboarding
    redirect, or the estimate form — all without requiring @require_auth on
    the public landing.

    Returns a snapshot dict (not the live ORM objects) so the caller can use
    it after the session_scope has closed.
    """
    try:
        from auth import _read_session_token, verify_session, AuthError
        token = _read_session_token()
        if not token:
            return None, None
        try:
            claims = verify_session(token)
        except AuthError:
            return None, None
        clerk_uid = claims.get("sub")
        if not clerk_uid:
            return None, None
        with session_scope() as session:
            user = (session.query(User)
                          .filter(User.clerk_user_id == clerk_uid)
                          .one_or_none())
            if user is None:
                return None, None
            org = user.current_organization
            snapshot = {
                "user_id": user.id,
                "org_id": org.id if org else None,
                "org_name": org.name if org else None,
                "is_beta_approved": bool(org and org.is_beta_approved),
                "approval_requested_at": (org.approval_requested_at if org else None),
            }
            # Pre-fetch effective rates while the session is still open.
            rates, markup = _effective_user_overrides(user) if org else (
                _effective_user_overrides(None)
            )
            snapshot["rates"] = rates
            snapshot["markup"] = markup
            return user.id, snapshot
    except Exception as exc:
        logger.debug("Optional auth detection failed: %s", exc)
        return None, None


@app.route("/")
def index():
    """Public entry point.

    Routing (server-side, with a JS fallback for the cold-cookie case):
      - Unauthenticated → landing.html (Login + Request Access CTAs).
      - Authenticated, but org missing → /onboarding (defensive).
      - Authenticated, org not beta-approved + no approval request yet
        → /onboarding (push them to fill out the company form).
      - Authenticated, org not beta-approved + request submitted
        → waitlist.html.
      - Authenticated, approved → existing estimate form (index.html).

    Phones are redirected to /mobile unless ?desktop=1 is passed.
    """
    if _is_mobile_ua() and request.args.get("desktop") != "1":
        return redirect(url_for("mobile"))

    _uid, snap = _try_signed_in_user_snapshot()
    if snap is None:
        # Cold-cookie path: render landing. Its JS detects an existing Clerk
        # session and reloads (now with the cookie set) — that lands here
        # again with `snap` populated.
        return render_template("landing.html")

    if snap["org_id"] is None:
        return redirect(url_for("onboarding"))

    if not snap["is_beta_approved"]:
        if snap["approval_requested_at"] is None:
            return redirect(url_for("onboarding"))
        return render_template(
            "waitlist.html",
            org_name=snap["org_name"],
            requested_at=snap["approval_requested_at"],
        )

    return render_template(
        "index.html",
        rate_fields=RATE_FIELDS,
        effective_rates=snap["rates"],
        effective_markup=snap["markup"],
    )


@app.route("/sign-in", methods=["GET"])
def sign_in():
    """GET-able sign-in landing. Opens the Clerk modal client-side and then
    navigates to ?next=<path> on success. Used as the redirect target from
    @require_auth so that:
      - relative-URL redirects don't leak the internal localhost host, and
      - a POST that hit @require_auth bounces back via GET (no 405).
    """
    next_path = request.args.get("next") or "/"
    # Same-origin only — block protocol/host injection via the next param.
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    return render_template("sign_in.html", next_path=next_path)


@app.route("/mobile")
def mobile():
    """Streamlined phone-first submission form. Same /submit endpoint, but
    no inline pricing override UI — the backend falls back to the user's
    saved pricing defaults when no rate__ fields are POSTed."""
    return render_template("mobile.html")


@app.route("/submit", methods=["POST"])
@require_auth
def submit():
    """Authenticated user uploads PDFs; we persist + enqueue."""

    # 1. Identity from session — no form fields for name/email any more.
    user_id = current_user_id()
    with session_scope() as session:
        user = session.get(User, user_id)
        name = user.name or ""
        email_addr = user.email
        org_id = user.current_organization_id

    if org_id is None:
        # Post-migration every Clerk-synced user is provisioned an org in
        # auth._sync_user. Reaching here means provisioning failed silently;
        # refuse the submission rather than write a row that will fail the
        # NOT-NULL constraint on submissions.org_id.
        logger.error("submit blocked: user %s has no current_organization_id", user_id)
        flash("Your account is not yet fully set up. Please contact support.", "error")
        return redirect(url_for("index"))

    # 1b. Beta gate. Reject before touching R2 so we don't waste an upload.
    with session_scope() as session:
        org = session.get(Organization, org_id)
        if org is None or not org.is_beta_approved:
            logger.info("submit blocked: org %s not beta-approved (user %s)", org_id, user_id)
            flash(
                "Your organization is on the Nightshift AI beta waitlist. "
                "We'll email you as soon as your access is approved.",
                "error",
            )
            return redirect(url_for("index"))

        cap = org.daily_submission_cap or BETA_DAILY_SUBMISSION_CAP_DEFAULT
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent_count = (
            session.query(Submission)
            .filter(Submission.org_id == org_id, Submission.submitted_at >= cutoff)
            .count()
        )
        if recent_count >= cap:
            logger.info("submit blocked: org %s hit daily cap %d (user %s)",
                        org_id, cap, user_id)
            flash(
                f"Your organization has reached its daily submission limit "
                f"({cap} per 24 hours). Please try again later or contact support "
                f"to request a higher limit.",
                "error",
            )
            return redirect(url_for("index"))

    # 2. Get uploaded files
    files = request.files.getlist("attachments")
    valid_files = [f for f in files if f.filename and f.filename.strip()]

    if not valid_files:
        flash("Please upload at least one PDF file.", "error")
        return redirect(url_for("index"))

    if len(valid_files) > MAX_PDFS_PER_EMAIL:
        flash(f"Maximum {MAX_PDFS_PER_EMAIL} files allowed.", "error")
        return redirect(url_for("index"))

    submission_id = str(uuid.uuid4())

    # 3. Stage files locally, validate, push to R2.
    uploaded_files = []   # list of (filename, r2_key, size_bytes)
    total_pages = 0
    max_size_bytes = 0

    with tempfile.TemporaryDirectory(prefix=f"ns-{submission_id}-") as staging:
        try:
            for idx, f in enumerate(valid_files):
                filename = secure_filename(f.filename) or f"upload_{idx + 1}.pdf"
                ext = os.path.splitext(filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    flash(f"Only PDF files are accepted. Rejected: {f.filename}", "error")
                    return redirect(url_for("index"))

                local_path = os.path.join(staging, filename)
                f.save(local_path)

                size_bytes = os.path.getsize(local_path)
                if size_bytes > MAX_PDF_SIZE_MB * 1024 * 1024:
                    flash(f"{filename} exceeds the {MAX_PDF_SIZE_MB} MB size limit.", "error")
                    return redirect(url_for("index"))

                total_pages += _count_pdf_pages(local_path)
                if size_bytes > max_size_bytes:
                    max_size_bytes = size_bytes

                key = storage.upload_key(submission_id, filename)
                storage.upload_file(local_path, key, content_type="application/pdf")
                uploaded_files.append((filename, key, size_bytes))

        except storage.StorageNotConfigured as exc:
            logger.error("R2 not configured: %s", exc)
            flash("Storage is not configured. Please contact support.", "error")
            return redirect(url_for("index"))
        except Exception as exc:
            logger.error("Upload error for submission %s: %s", submission_id, exc, exc_info=True)
            flash("An error occurred while uploading your files. Please try again.", "error")
            try:
                storage.delete_prefix(storage.submission_prefix(submission_id))
            except Exception:
                pass
            return redirect(url_for("index"))

    # 4. Persist submission + files to the DB.
    phone = request.form.get("phone", "").strip()
    business_name = request.form.get("business_name", "").strip()
    scope_notes = request.form.get("scope_notes", "").strip()
    deadline = request.form.get("deadline", "").strip()

    try:
        with session_scope() as session:
            sub = Submission(
                id=submission_id,
                user_id=user_id,
                org_id=org_id,
                phone=phone or None,
                business_name=business_name or None,
                scope_notes=scope_notes or None,
                deadline=deadline or None,
                status="queued",
            )
            session.add(sub)
            for filename, r2_key, size_bytes in uploaded_files:
                session.add(File(
                    submission_id=submission_id,
                    kind="upload",
                    filename=filename,
                    r2_key=r2_key,
                    size_bytes=size_bytes,
                    content_type="application/pdf",
                ))
    except Exception as exc:
        logger.error("DB write failed for %s: %s", submission_id, exc, exc_info=True)
        flash("An error occurred while saving your submission. Please try again.", "error")
        try:
            storage.delete_prefix(storage.submission_prefix(submission_id))
        except Exception:
            pass
        return redirect(url_for("index"))

    # 5. Build pricing overrides for this submission.
    #    Start from the org's saved pricing profile, then merge any per-job
    #    overrides posted from the inline pricing table (rate__<key>).
    with session_scope() as session:
        user = session.get(User, user_id)
        org_overrides = (
            user.current_organization.pricing_overrides
            if user and user.current_organization else None
        )
        rate_overrides = _flatten_overrides(org_overrides)

    per_job = {}
    for key, _label, _unit, _default in RATE_FIELDS:
        raw = (request.form.get(f"rate__{key}") or "").strip()
        if not raw:
            continue
        try:
            v = float(raw)
            if 0 <= v <= 100000:
                per_job[key] = v
        except ValueError:
            pass

    raw_markup = (request.form.get("rate__markup") or "").strip()
    if raw_markup:
        try:
            mv = float(raw_markup)
            if 0 <= mv <= 1:
                per_job["markup"] = mv
        except ValueError:
            pass

    if per_job:
        rate_overrides = dict(rate_overrides or {})
        rate_overrides.update(per_job)

    # 6. Enqueue the job — route to fast or heavy queue based on size/pages.
    pdf_keys = [k for (_, k, _) in uploaded_files]
    queue, queue_name = _pick_queue(total_pages, max_size_bytes)
    try:
        job = queue.enqueue(
            "jobs.process_submission",
            kwargs={
                "submission_id": submission_id,
                "pdf_keys": pdf_keys,
                "contact_info": {
                    "name": name,
                    "email": email_addr,
                    "phone": phone,
                    "business_name": business_name,
                },
                "scope_notes": scope_notes,
                "rate_overrides": rate_overrides,
            },
            job_id=submission_id,
            job_timeout=RQ_JOB_TIMEOUT,
            result_ttl=RQ_RESULT_TTL,
            failure_ttl=RQ_RESULT_TTL,
        )
    except Exception as exc:
        logger.error("Failed to enqueue submission %s: %s", submission_id, exc)
        flash("Our queue is unavailable right now. Please try again in a few minutes.", "error")
        # Mark the DB row failed; leave files in R2 for forensics.
        try:
            with session_scope() as session:
                sub = session.get(Submission, submission_id)
                if sub:
                    sub.status = "failed"
                    sub.error = f"enqueue failed: {exc}"
        except Exception:
            pass
        return redirect(url_for("index"))

    logger.info("Submission %s enqueued on %s — %d PDFs, %d pages, %.1f MB max from %s <%s> (job %s)",
                submission_id, queue_name, len(pdf_keys), total_pages,
                max_size_bytes / (1024 * 1024), name, email_addr, job.id)

    # Post/Redirect/Get: land the user on a GET-able URL so that refresh,
    # back-button, or a shared link doesn't reload as `GET /submit` (405).
    return redirect(url_for("thank_you", submission_id=submission_id))


@app.route("/thank-you/<submission_id>", methods=["GET"])
@require_auth
def thank_you(submission_id):
    """GET-safe confirmation page for a submission the caller owns."""
    uid = current_user_id()
    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != uid:
            return ("Not found", 404)
        user = session.get(User, uid)
        name = (user.name if user else "") or ""
        email_addr = user.email if user else ""
        num_files = sum(1 for f in sub.files if f.kind == "upload")

    return render_template(
        "thank_you.html",
        name=name,
        email=email_addr,
        num_files=num_files,
        submission_id=submission_id,
    )


@app.route("/jobs", methods=["GET"])
@require_auth
def jobs_list():
    """Render the user's submission history (HTML).

    Filtered by query string `status`:
        active     -> queued + processing (default)
        completed  -> completed + failed
    """
    uid = current_user_id()
    status_filter = (request.args.get("status") or "active").lower()
    if status_filter == "completed":
        wanted = ("completed", "failed", "cancelled")
    else:
        wanted = ("queued", "processing")
        status_filter = "active"

    rows = []
    with session_scope() as session:
        user = session.get(User, uid)
        admin = is_admin(user)
        subs = (session.query(Submission)
                .filter(Submission.user_id == uid)
                .filter(Submission.status.in_(wanted))
                .order_by(Submission.submitted_at.desc())
                .limit(100).all())
        for s in subs:
            results = []
            if s.status == "completed":
                for f in s.files:
                    if f.kind == "result":
                        try:
                            results.append({
                                "filename": f.filename,
                                "url": storage.presigned_download_url(f.r2_key),
                            })
                        except Exception as exc:
                            logger.warning("Could not sign URL for %s: %s", f.r2_key, exc)
            rows.append({
                "id": s.id,
                "business_name": s.business_name,
                "submitted_at": s.submitted_at,
                "status": s.status,
                "subtotal": float(s.subtotal) if s.subtotal is not None else None,
                "upload_count": sum(1 for f in s.files if f.kind == "upload"),
                "results": results,
            })
    return render_template("jobs.html", submissions=rows,
                           status_filter=status_filter, is_admin=admin)


@app.route("/jobs/<submission_id>", methods=["GET"])
@require_auth
def job_detail(submission_id):
    """HTML status page for one submission. Polls /api/jobs/<id> for live updates."""
    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != current_user_id():
            return ("Not found", 404)
    return render_template("job_detail.html", submission_id=submission_id)


@app.route("/api/jobs/<submission_id>", methods=["GET"])
@require_auth
def job_status_api(submission_id):
    """Return submission status + signed download URLs for any results.

    Authorization: only the submission's owner may view it. Other users get
    a 404 (not 403) so we don't leak existence of foreign IDs.
    """
    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != current_user_id():
            return jsonify({"error": "not found"}), 404

        result_files = [f for f in sub.files if f.kind == "result"]
        results = [{
            "filename": f.filename,
            "size": f.size_bytes,
            "url": storage.presigned_download_url(f.r2_key),
        } for f in result_files]

        payload = {
            "submission_id": sub.id,
            "status": sub.status,
            "error": sub.error,
            "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
            "updated_at": sub.updated_at.isoformat() if sub.updated_at else None,
            "subtotal": float(sub.subtotal) if sub.subtotal is not None else None,
            "results": results,
        }

    # RQ status is best-effort; the DB row is authoritative.
    rq_status = None
    rq_error = None
    try:
        job = Job.fetch(submission_id, connection=_redis)
        rq_status = job.get_status(refresh=True)
        if job.is_failed and job.exc_info:
            rq_error = job.exc_info.splitlines()[-1]
    except NoSuchJobError:
        rq_status = "expired"

    payload["rq_status"] = rq_status
    if not payload["error"] and rq_error:
        payload["error"] = rq_error
    return jsonify(payload)


@app.route("/api/jobs/<submission_id>/result", methods=["GET"])
@require_auth
def job_result_api(submission_id):
    """Return the parsed cost_estimate (line_items + subtotal) from the
    submission's result JSON in R2. Used by the Completed tab to render
    an editable line-item breakdown without exposing the full result blob.
    """
    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != current_user_id():
            return jsonify({"error": "not found"}), 404
        if sub.status != "completed":
            return jsonify({"error": "not completed"}), 409

        json_files = [f for f in sub.files
                      if f.kind == "result" and f.filename.lower().endswith(".json")]
        if not json_files:
            return jsonify({"error": "result JSON not found"}), 404
        # Most recent result JSON wins if there are multiples.
        json_file = sorted(json_files, key=lambda f: f.id, reverse=True)[0]
        r2_key = json_file.r2_key

    try:
        raw = storage.get_bytes(r2_key)
        data = json.loads(raw)
    except Exception as exc:
        logger.error("Failed to load result JSON %s: %s", r2_key, exc)
        return jsonify({"error": "failed to load result"}), 500

    ce = data.get("cost_estimate") or {}
    return jsonify({
        "line_items": ce.get("line_items") or [],
        "subtotal": ce.get("subtotal"),
        "exclusions": ce.get("exclusions") or [],
    })


@app.route("/api/jobs/<submission_id>/prioritize", methods=["POST"])
@require_auth
def job_prioritize_api(submission_id):
    """Admin-only: bump a queued job to the front of its RQ queue.

    Implemented by removing the job from its current queue position and
    re-enqueuing with at_front=True. The DB row and job_id are unchanged.
    """
    uid = current_user_id()
    with session_scope() as session:
        user = session.get(User, uid)
        if not is_admin(user):
            return jsonify({"error": "forbidden"}), 403
        sub = session.get(Submission, submission_id)
        if sub is None:
            return jsonify({"error": "not found"}), 404
        if sub.status != "queued":
            return jsonify({"error": f"cannot prioritize a {sub.status} job"}), 409

    try:
        job = Job.fetch(submission_id, connection=_redis)
    except NoSuchJobError:
        return jsonify({"error": "job not found in queue"}), 404

    origin = job.origin  # the queue name this job was enqueued on
    queue = Queue(origin, connection=_redis)
    try:
        queue.remove(job)
        queue.enqueue_job(job, at_front=True)
    except Exception as exc:
        logger.error("Failed to prioritize %s: %s", submission_id, exc)
        return jsonify({"error": "failed to prioritize"}), 500

    logger.info("Submission %s prioritized on queue %s by user %d",
                submission_id, origin, uid)
    return jsonify({"ok": True, "queue": origin})


@app.route("/api/jobs/<submission_id>/cancel", methods=["POST"])
@require_auth
def job_cancel_api(submission_id):
    """Cancel a queued job. Owner-only. Refuses jobs that are already processing."""
    uid = current_user_id()
    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != uid:
            return jsonify({"error": "not found"}), 404
        if sub.status != "queued":
            return jsonify({"error": f"cannot cancel a {sub.status} job"}), 409
        sub.status = "cancelled"

    try:
        job = Job.fetch(submission_id, connection=_redis)
        job.cancel()
        job.delete()
    except NoSuchJobError:
        # DB row already updated; treat as success.
        pass
    except Exception as exc:
        logger.warning("Cancel: RQ cleanup failed for %s: %s", submission_id, exc)

    logger.info("Submission %s cancelled by user %d", submission_id, uid)
    return jsonify({"ok": True})


@app.route("/pricing", methods=["GET", "POST"])
@require_auth
def pricing_settings():
    """View / edit per-org pricing overrides (rates + markup).

    Pricing is org-scoped: any member of the same org sees the same saved
    rates. Owners edit; member-vs-owner role enforcement ships with the
    invite/role UI.
    """
    uid = current_user_id()
    with session_scope() as session:
        user = session.get(User, uid)
        org = user.current_organization if user else None
        if org is None:
            flash("Your account is not yet fully set up. Please contact support.",
                  "error")
            return redirect(url_for("index"))

        if request.method == "POST":
            if request.form.get("reset"):
                org.pricing_overrides = None
                flash("Pricing reset to Rider defaults.", "success")
                return redirect(url_for("pricing_settings"))

            rates, errors = {}, []
            for key, _label, _unit, _default in RATE_FIELDS:
                raw = (request.form.get(key) or "").strip()
                if not raw:
                    continue
                try:
                    v = float(raw)
                    if v < 0 or v > 100000:
                        raise ValueError("out of range")
                    rates[key] = v
                except ValueError:
                    errors.append(f"{key}: must be a number between 0 and 100000")

            markup = None
            raw_m = (request.form.get("markup") or "").strip()
            if raw_m:
                try:
                    markup = float(raw_m)
                    if markup < 0 or markup > 1:
                        raise ValueError()
                except ValueError:
                    errors.append("markup: must be between 0.0 and 1.0")

            if errors:
                for e in errors:
                    flash(e, "error")
                return redirect(url_for("pricing_settings"))

            overrides = {}
            if rates:
                overrides["rates"] = rates
            if markup is not None:
                overrides["markup"] = markup
            org.pricing_overrides = overrides or None
            flash("Pricing saved.", "success")
            return redirect(url_for("pricing_settings"))

        overrides = org.pricing_overrides or {}

    return render_template(
        "pricing.html",
        overrides={
            "markup": overrides.get("markup"),
            "rates": overrides.get("rates", {}),
        },
        rate_fields=RATE_FIELDS,
    )


# ---------------------------------------------------------------------------
# Account / Members
# ---------------------------------------------------------------------------

def _membership_for(session, user_id, org_id):
    """Return the (single) membership row linking user_id to org_id, or None."""
    return (session.query(OrganizationMembership)
                   .filter(OrganizationMembership.user_id == user_id,
                           OrganizationMembership.organization_id == org_id)
                   .one_or_none())


def _is_owner(session, user_id, org_id):
    m = _membership_for(session, user_id, org_id)
    return bool(m and m.role == "owner")


@app.route("/api/account/me", methods=["GET"])
@require_auth
def account_me_api():
    """Lightweight payload for the top-right dropdown header."""
    uid = current_user_id()
    with session_scope() as session:
        user = session.get(User, uid)
        org = user.current_organization if user else None
        m = _membership_for(session, uid, org.id) if (user and org) else None
        return jsonify({
            "email": user.email if user else None,
            "name": user.name if user else None,
            "org_name": org.name if org else None,
            "org_id": org.id if org else None,
            "role": m.role if m else None,
            "is_personal_org": bool(org and org.is_personal),
        })


@app.route("/account/organization", methods=["GET", "POST"])
@require_auth
def organization():
    """View / edit the user's current org. Owner can rename; everyone else
    sees the page read-only. Domain / personal / verified / beta-approval
    are not editable here — they're either auto-derived (domain, personal)
    or admin-managed (verified, beta_approved, daily_cap).
    """
    uid = current_user_id()
    with session_scope() as session:
        user = session.get(User, uid)
        org = user.current_organization if user else None
        if org is None:
            flash("Your account is not yet fully set up. Please contact support.", "error")
            return redirect(url_for("index"))

        my_role = (_membership_for(session, uid, org.id) or
                   OrganizationMembership(role="member")).role
        is_owner = (my_role == "owner")

        if request.method == "POST":
            if not is_owner:
                flash("Only org owners can edit the organization.", "error")
                return redirect(url_for("organization"))
            new_name = (request.form.get("name") or "").strip()
            if not new_name:
                flash("Organization name is required.", "error")
                return redirect(url_for("organization"))
            if len(new_name) > 255:
                flash("Organization name is too long (max 255 characters).", "error")
                return redirect(url_for("organization"))
            org.name = new_name
            flash("Organization updated.", "success")
            return redirect(url_for("organization"))

        member_count = (session.query(OrganizationMembership)
                               .filter(OrganizationMembership.organization_id == org.id)
                               .count())
        owner_count = (session.query(OrganizationMembership)
                              .filter(OrganizationMembership.organization_id == org.id,
                                      OrganizationMembership.role == "owner")
                              .count())
        return render_template(
            "organization.html",
            org=org,
            my_role=my_role,
            is_owner=is_owner,
            member_count=member_count,
            owner_count=owner_count,
            daily_cap_effective=org.daily_submission_cap or BETA_DAILY_SUBMISSION_CAP_DEFAULT,
        )


@app.route("/account/members", methods=["GET"])
@require_auth
def members():
    """Render the members management page for the user's current org."""
    uid = current_user_id()
    with session_scope() as session:
        user = session.get(User, uid)
        org = user.current_organization if user else None
        if org is None:
            flash("Your account is not yet fully set up. Please contact support.", "error")
            return redirect(url_for("index"))

        my_role = (_membership_for(session, uid, org.id) or
                   OrganizationMembership(role="member")).role

        rows = (session.query(OrganizationMembership, User)
                       .join(User, User.id == OrganizationMembership.user_id)
                       .filter(OrganizationMembership.organization_id == org.id)
                       .order_by(OrganizationMembership.created_at.asc())
                       .all())
        members_list = [{
            "membership_id": m.id,
            "user_id": u.id,
            "email": u.email,
            "name": u.name or "",
            "role": m.role,
            "is_self": (u.id == uid),
            "joined_at": m.created_at,
            "active": bool(u.clerk_user_id),
        } for (m, u) in rows]

        return render_template(
            "members.html",
            org=org,
            members=members_list,
            my_role=my_role,
            can_invite=(my_role == "owner") and not org.is_personal,
        )


@app.route("/account/members/invite", methods=["POST"])
@require_auth
def members_invite():
    """Owner pre-creates a User + Membership for an email on the org's domain.

    No outbound email yet — invitee just signs in with that email and the
    existing _sync_user path links their Clerk account to the pre-created row.
    Personal orgs cannot invite (they're single-user by design).
    """
    uid = current_user_id()
    raw_email = (request.form.get("email") or "").strip().lower()
    role = (request.form.get("role") or "member").strip().lower()
    if role not in ("owner", "member"):
        role = "member"

    if not raw_email or "@" not in raw_email:
        flash("Enter a valid email address.", "error")
        return redirect(url_for("members"))

    with session_scope() as session:
        user = session.get(User, uid)
        org = user.current_organization if user else None
        if org is None or not _is_owner(session, uid, org.id):
            flash("Only org owners can invite members.", "error")
            return redirect(url_for("members"))
        if org.is_personal:
            flash("Personal accounts cannot have additional members.", "error")
            return redirect(url_for("members"))

        # Domain restriction: corporate orgs only accept emails on their own
        # domain. Mirrors the auto-provisioning rule in orgs.provision_org_for_user.
        invitee_domain = _domain_of(raw_email)
        if org.email_domain and invitee_domain != org.email_domain:
            flash(
                f"Invitees must use a @{org.email_domain} email address.",
                "error",
            )
            return redirect(url_for("members"))
        if invitee_domain in FREE_EMAIL_DOMAINS:
            flash("Free-email addresses cannot be added to a corporate org.", "error")
            return redirect(url_for("members"))

        existing = session.query(User).filter(User.email == raw_email).one_or_none()
        if existing is None:
            existing = User(email=raw_email, name=None)
            session.add(existing)
            session.flush()  # need id for the membership row

        # Idempotent: if they're already in the org, just bump their role.
        existing_m = _membership_for(session, existing.id, org.id)
        if existing_m is not None:
            if existing_m.role != role:
                existing_m.role = role
                flash(f"Updated {raw_email} to {role}.", "success")
            else:
                flash(f"{raw_email} is already a member.", "success")
            return redirect(url_for("members"))

        session.add(OrganizationMembership(
            organization_id=org.id,
            user_id=existing.id,
            role=role,
        ))
        # If this is the invitee's first org, seed their current_organization
        # so they land in it on first sign-in.
        if existing.current_organization_id is None:
            existing.current_organization_id = org.id

    flash(f"Invited {raw_email}. They'll join automatically when they sign in.",
          "success")
    return redirect(url_for("members"))


@app.route("/account/members/<int:membership_id>/remove", methods=["POST"])
@require_auth
def members_remove(membership_id):
    """Owner-only removal. Refuses to remove the last remaining owner."""
    uid = current_user_id()
    with session_scope() as session:
        user = session.get(User, uid)
        org = user.current_organization if user else None
        if org is None or not _is_owner(session, uid, org.id):
            flash("Only org owners can remove members.", "error")
            return redirect(url_for("members"))

        target = session.get(OrganizationMembership, membership_id)
        if target is None or target.organization_id != org.id:
            flash("Member not found.", "error")
            return redirect(url_for("members"))

        if target.role == "owner":
            owner_count = (session.query(OrganizationMembership)
                                  .filter(OrganizationMembership.organization_id == org.id,
                                          OrganizationMembership.role == "owner")
                                  .count())
            if owner_count <= 1:
                flash("Can't remove the last owner — promote someone else first.",
                      "error")
                return redirect(url_for("members"))

        # If the removed user's current_organization points here, clear it so
        # their next sign-in re-provisions them into a personal org.
        target_user = session.get(User, target.user_id)
        if target_user and target_user.current_organization_id == org.id:
            target_user.current_organization_id = None

        target_email = target_user.email if target_user else "user"
        session.delete(target)

    flash(f"Removed {target_email} from the org.", "success")
    return redirect(url_for("members"))


@app.route("/account/members/<int:membership_id>/role", methods=["POST"])
@require_auth
def members_role(membership_id):
    """Owner-only role change. Refuses to demote the last owner."""
    uid = current_user_id()
    new_role = (request.form.get("role") or "").strip().lower()
    if new_role not in ("owner", "member"):
        flash("Invalid role.", "error")
        return redirect(url_for("members"))

    with session_scope() as session:
        user = session.get(User, uid)
        org = user.current_organization if user else None
        if org is None or not _is_owner(session, uid, org.id):
            flash("Only org owners can change roles.", "error")
            return redirect(url_for("members"))

        target = session.get(OrganizationMembership, membership_id)
        if target is None or target.organization_id != org.id:
            flash("Member not found.", "error")
            return redirect(url_for("members"))

        if target.role == "owner" and new_role == "member":
            owner_count = (session.query(OrganizationMembership)
                                  .filter(OrganizationMembership.organization_id == org.id,
                                          OrganizationMembership.role == "owner")
                                  .count())
            if owner_count <= 1:
                flash("Can't demote the last owner — promote someone else first.",
                      "error")
                return redirect(url_for("members"))

        target.role = new_role

    flash("Role updated.", "success")
    return redirect(url_for("members"))


# ---------------------------------------------------------------------------
# Onboarding (sign-up access request) + Admin Approval
# ---------------------------------------------------------------------------

@app.route("/onboarding", methods=["GET", "POST"])
@require_auth
def onboarding():
    """Capture explicit Name + Company on first sign-in and notify admins.

    Skipped when the user's org is already beta-approved (returning users
    who land here via a stale link bounce home).
    """
    uid = current_user_id()

    if request.method == "GET":
        with session_scope() as session:
            user = session.get(User, uid)
            org = user.current_organization if user else None
            if org is not None and org.is_beta_approved:
                return redirect(url_for("index"))
            return render_template(
                "onboarding.html",
                user_name=user.name or "" if user else "",
                user_email=user.email if user else "",
                company_name=(org.name if org else ""),
            )

    # POST
    submitted_name = (request.form.get("name") or "").strip()
    submitted_company = (request.form.get("company_name") or "").strip()

    if not submitted_name:
        flash("Please enter your name.", "error")
        return redirect(url_for("onboarding"))
    if not submitted_company:
        flash("Please enter your company name.", "error")
        return redirect(url_for("onboarding"))
    if len(submitted_company) > 200 or len(submitted_name) > 200:
        flash("Name and company must each be under 200 characters.", "error")
        return redirect(url_for("onboarding"))

    notify_payload = None
    with session_scope() as session:
        user = session.get(User, uid)
        if user is None:
            flash("Account not found. Please sign in again.", "error")
            return redirect(url_for("index"))

        org = user.current_organization
        if org is None:
            flash("Your organization is not set up. Please contact support.",
                  "error")
            return redirect(url_for("index"))

        if org.is_beta_approved:
            return redirect(url_for("index"))

        user.name = submitted_name
        org.name = submitted_company

        first_request = org.approval_requested_at is None
        org.approval_requested_at = datetime.now(timezone.utc)

        # Capture a snapshot for the email send (which we do AFTER commit so a
        # send failure doesn't roll back the application).
        if first_request:
            notify_payload = {
                "user_email": user.email,
                "user_name": user.name,
                "org_name": org.name,
                "org_email_domain": org.email_domain,
            }

    if notify_payload:
        admin_url = url_for("admin_orgs", _external=True)

        # Lightweight stand-in objects with the attribute shape the helper
        # expects. Avoids passing detached ORM rows out of the session_scope.
        class _U: pass
        class _O: pass
        u, o = _U(), _O()
        u.name = notify_payload["user_name"]
        u.email = notify_payload["user_email"]
        o.name = notify_payload["org_name"]
        o.email_domain = notify_payload["org_email_domain"]
        try:
            notify_admin_of_new_signup(u, o, admin_url)
        except Exception as exc:
            logger.error("Admin signup notification failed: %s", exc)

    flash("Thanks — your access request has been received.", "success")
    return redirect(url_for("index"))


@app.route("/admin/orgs", methods=["GET"])
@require_auth
def admin_orgs():
    """List orgs awaiting beta approval. Admin-only."""
    uid = current_user_id()
    with session_scope() as session:
        user = session.get(User, uid)
        if not is_admin(user):
            return ("Forbidden", 403)

        # Pending = applied (approval_requested_at NOT NULL) but not yet approved.
        pending_q = (session.query(Organization)
                            .filter(Organization.is_beta_approved.is_(False))
                            .filter(Organization.approval_requested_at.isnot(None))
                            .order_by(Organization.approval_requested_at.desc()))

        approved_q = (session.query(Organization)
                             .filter(Organization.is_beta_approved.is_(True))
                             .order_by(Organization.created_at.desc())
                             .limit(50))

        def _row(org):
            owner_emails = []
            for m in org.memberships:
                if m.role == "owner" and m.user and m.user.email:
                    owner_emails.append(m.user.email)
            return {
                "id": org.id,
                "name": org.name,
                "email_domain": org.email_domain,
                "is_personal": org.is_personal,
                "owner_emails": owner_emails,
                "requested_at": org.approval_requested_at,
                "created_at": org.created_at,
            }

        pending = [_row(o) for o in pending_q.all()]
        approved = [_row(o) for o in approved_q.all()]

    return render_template(
        "admin_orgs.html",
        pending=pending,
        approved=approved,
        notifications_ok=notifications_configured(),
    )


@app.route("/admin/orgs/<int:org_id>/approve", methods=["POST"])
@require_auth
def admin_orgs_approve(org_id):
    """Flip is_beta_approved=True and email the org's owners."""
    uid = current_user_id()
    notify = []
    with session_scope() as session:
        user = session.get(User, uid)
        if not is_admin(user):
            return ("Forbidden", 403)

        org = session.get(Organization, org_id)
        if org is None:
            flash("Organization not found.", "error")
            return redirect(url_for("admin_orgs"))

        if org.is_beta_approved:
            flash(f"{org.name} is already approved.", "success")
            return redirect(url_for("admin_orgs"))

        org.is_beta_approved = True
        # If approval is granted before the user ever submitted /onboarding
        # (rare path — admin pre-approves a known org), backfill the request
        # timestamp so the org doesn't reappear on the pending list.
        if org.approval_requested_at is None:
            org.approval_requested_at = datetime.now(timezone.utc)

        org_name = org.name
        for m in org.memberships:
            if m.role == "owner" and m.user and m.user.email:
                notify.append((m.user.email, m.user.name or ""))

    app_url = url_for("index", _external=True)
    for email, name in notify:
        try:
            notify_user_of_approval(email, name, org_name, app_url)
        except Exception as exc:
            logger.error("Approval notification to %s failed: %s", email, exc)

    flash(f"Approved {org_name}.", "success")
    return redirect(url_for("admin_orgs"))


# ---------------------------------------------------------------------------
# Error Handlers
# ---------------------------------------------------------------------------

@app.errorhandler(413)
def too_large(e):
    flash(f"Upload too large. Maximum total size is "
          f"{MAX_PDF_SIZE_MB * MAX_PDFS_PER_EMAIL} MB.", "error")
    return redirect(url_for("index"))


_ERROR_500_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>Something went wrong &mdash; Knight Shift</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body class="dark-theme">
<main class="page-shell" style="display:flex;align-items:center;justify-content:center;min-height:80vh;">
<div style="text-align:center;color:#cbd5e1;max-width:480px;padding:2rem;">
<h1 style="color:#fff;margin-bottom:0.5rem;">Something went wrong</h1>
<p>The server hit an unexpected error. Try again, or contact support if it keeps happening.</p>
<p style="margin-top:1.5rem;"><a href="/" style="color:#60a5fa;">Return home</a></p>
</div></main></body></html>"""


@app.errorhandler(500)
def server_error(e):
    logger.error("500 error: %s", e, exc_info=True)
    return _ERROR_500_HTML, 500


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting Knight Shift web form on port %d (queues=%s/%s, redis=%s)",
                WEB_PORT, RQ_QUEUE_FAST, RQ_QUEUE_HEAVY, REDIS_URL)
    app.run(host="0.0.0.0", port=WEB_PORT, debug=True)
