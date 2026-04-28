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
    REDIS_URL, RQ_QUEUE_NAME, RQ_JOB_TIMEOUT, RQ_RESULT_TTL,
    CLERK_PUBLISHABLE_KEY,
)
import storage
from db import session_scope
from models import User, Submission, File
from auth import require_auth, current_user_id, clerk_frontend_api_host

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
_queue = Queue(RQ_QUEUE_NAME, connection=_redis)


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
    saved = (user.pricing_overrides or {}) if user else {}
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


@app.route("/")
def index():
    """Public landing — Clerk.js handles auth client-side and bootstraps the
    session cookie via the dev-mode handshake. The form is hidden by JS
    until Clerk confirms a signed-in user. Server-side auth is enforced on
    /submit and /api/jobs/<id>, which only fire after the cookie is in place.

    Pricing context is fetched server-side too — but unauthenticated callers
    get the global defaults (Clerk-protected pages will refuse data fetch).

    Phones are redirected to /mobile unless ?desktop=1 is passed.
    """
    if _is_mobile_ua() and request.args.get("desktop") != "1":
        return redirect(url_for("mobile"))

    user = None
    try:
        from auth import _read_session_token, verify_session, AuthError
        token = _read_session_token()
        try:
            claims = verify_session(token)
            clerk_uid = claims.get("sub")
            if clerk_uid:
                with session_scope() as session:
                    user = session.query(User).filter(User.clerk_user_id == clerk_uid).one_or_none()
        except AuthError:
            pass
    except Exception:
        pass

    rates, markup = _effective_user_overrides(user)
    return render_template(
        "index.html",
        rate_fields=RATE_FIELDS,
        effective_rates=rates,
        effective_markup=markup,
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
    #    Start from the user's saved profile, then merge any per-job
    #    overrides posted from the inline pricing table (rate__<key>).
    with session_scope() as session:
        user = session.get(User, user_id)
        rate_overrides = _flatten_overrides(user.pricing_overrides) if user else None

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

    # 6. Enqueue the job.
    pdf_keys = [k for (_, k, _) in uploaded_files]
    try:
        job = _queue.enqueue(
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

    logger.info("Submission %s enqueued — %d PDFs from %s <%s> (job %s)",
                submission_id, len(pdf_keys), name, email_addr, job.id)

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
        wanted = ("completed", "failed")
    else:
        wanted = ("queued", "processing")
        status_filter = "active"

    rows = []
    with session_scope() as session:
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
    return render_template("jobs.html", submissions=rows, status_filter=status_filter)


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


@app.route("/pricing", methods=["GET", "POST"])
@require_auth
def pricing_settings():
    """View / edit per-account pricing overrides (rates + markup)."""
    uid = current_user_id()
    with session_scope() as session:
        user = session.get(User, uid)

        if request.method == "POST":
            if request.form.get("reset"):
                user.pricing_overrides = None
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
            user.pricing_overrides = overrides or None
            flash("Pricing saved.", "success")
            return redirect(url_for("pricing_settings"))

        overrides = user.pricing_overrides or {}

    return render_template(
        "pricing.html",
        overrides={
            "markup": overrides.get("markup"),
            "rates": overrides.get("rates", {}),
        },
        rate_fields=RATE_FIELDS,
    )


# ---------------------------------------------------------------------------
# Error Handlers
# ---------------------------------------------------------------------------

@app.errorhandler(413)
def too_large(e):
    flash(f"Upload too large. Maximum total size is "
          f"{MAX_PDF_SIZE_MB * MAX_PDFS_PER_EMAIL} MB.", "error")
    return redirect(url_for("index"))


@app.errorhandler(500)
def server_error(e):
    logger.error("500 error: %s", e)
    flash("Something went wrong. Please try again.", "error")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting Knight Shift web form on port %d (queue=%s, redis=%s)",
                WEB_PORT, RQ_QUEUE_NAME, REDIS_URL)
    app.run(host="0.0.0.0", port=WEB_PORT, debug=True)
