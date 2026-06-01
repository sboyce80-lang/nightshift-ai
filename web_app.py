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

from flask import Flask, request, render_template, redirect, url_for, flash, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

from redis import Redis
from rq import Queue
from rq.job import Job
from rq.command import send_stop_job_command
from rq.exceptions import NoSuchJobError, InvalidJobOperation

# ---------------------------------------------------------------------------
# Ensure local imports work
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    MAX_PDF_SIZE_MB, MAX_PDFS_PER_EMAIL, UPLOAD_PART_SIZE_BYTES,
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
    notify_user_of_denial,
    notify_user_of_org_invite,
    notifications_configured,
)
from generate_estimate_pdf import is_estimate_filename
from bbox_spike import is_annotated_drawings_filename

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

# Advanced/specialty items hidden by default — admins toggle them on per org.
# Stored shape on org.pricing_overrides:
#   {"advanced_enabled": ["lymewash_rate", ...]}
# Tuple: (key, label, unit, default, category)
ADVANCED_RATE_FIELDS = [
    ("lymewash_rate", "Lyme wash", "sqft", "4.50", "Faux"),
    ("plaster_rate",  "Plaster",   "sqft", "7.50", "Faux"),
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
# "Send Estimate" message defaults — used by /messages (Message Settings tab)
# and the send-estimate modal on the Completed tab. Stored per-org in
# organizations.message_settings JSON. NULL → use these built-in defaults.
# Templates accept {business_name}, {subtotal}, {filename} placeholders.
# ---------------------------------------------------------------------------
DEFAULT_SEND_ESTIMATE_SUBJECT = "Estimate for {business_name}"
DEFAULT_SEND_ESTIMATE_BODY = (
    "Hello,\n\n"
    "Please find attached the formal estimate for {business_name}.\n\n"
    "Estimate total: {subtotal}\n\n"
    "Let me know if you have any questions or if you'd like to proceed.\n\n"
    "Thank you."
)

# Outer cap on saved templates. Long enough for a proper cover note, short
# enough to keep the form predictable in the DB and on email relays.
MESSAGE_SUBJECT_MAX = 255
MESSAGE_BODY_MAX = 8000
MESSAGE_RECIPIENT_MAX = 320  # SMTP path-length limit
MESSAGE_CC_BCC_MAX = 10      # combined, per field


def _parse_address_list(raw):
    """Split a comma/newline-separated address string into a cleaned list.

    Returns (addresses, errors). Empty input → ([], []).
    """
    addrs = []
    errors = []
    if not raw:
        return addrs, errors
    seen = set()
    for piece in re.split(r"[,;\n]+", raw):
        piece = piece.strip()
        if not piece:
            continue
        low = piece.lower()
        if low in seen:
            continue
        if len(piece) > MESSAGE_RECIPIENT_MAX or not _EMAIL_RE.match(piece):
            errors.append(f"Not a valid email address: {piece}")
            continue
        seen.add(low)
        addrs.append(piece)
    return addrs, errors


def _resolve_message_settings(org):
    """Return the org's effective message settings, merged with defaults."""
    ms = (org.message_settings if org is not None else None) or {}
    return {
        "subject_template": ms.get("subject_template") or DEFAULT_SEND_ESTIMATE_SUBJECT,
        "body_template": ms.get("body_template") or DEFAULT_SEND_ESTIMATE_BODY,
        "cc": list(ms.get("cc") or []),
        "bcc": list(ms.get("bcc") or []),
    }


# Single-source-of-truth regex for address validation. Defined up here so
# /messages can use it before the send-estimate route's module position.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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
    to size-based heuristic — never blocks a submission on a broken PDF.

    Tries PyMuPDF first (much more tolerant of unusual xref/structure that
    real-world DD-set PDFs contain) and falls back to PyPDF2. Rider
    Painting's scanned multi-PDF DD sets routinely caused PyPDF2 alone to
    return 0, which dropped them into the small-job timeout bucket.
    """
    try:
        import fitz  # PyMuPDF
        with fitz.open(path) as doc:
            n = doc.page_count
            if n > 0:
                return n
    except Exception as exc:
        logger.warning("PyMuPDF page count failed for %s: %s", path, exc)

    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(path, strict=False)
        return len(reader.pages)
    except Exception as exc:
        logger.warning("PyPDF2 page count failed for %s: %s", path, exc)
        return 0


def _pick_queue(total_pages, max_size_bytes):
    max_mb = max_size_bytes / (1024 * 1024)
    if total_pages >= HEAVY_QUEUE_PAGE_THRESHOLD or max_mb >= HEAVY_QUEUE_FILE_MB:
        return _queue_heavy, RQ_QUEUE_HEAVY
    return _queue_fast, RQ_QUEUE_FAST


def _pick_timeout(total_pages, max_size_bytes):
    """Per-submission RQ job_timeout, scaled to payload.

    A flat 2h timeout was too tight for 4-PDF DD-scale sets (one SUMMIT
    submission was killed at the 91-min mark with no completion in sight)
    and far too generous for small jobs (which then squat the queue when
    truly hung). Tier the limit so big payloads get room and small ones
    fail fast.

    Returns seconds. Falls back to RQ_JOB_TIMEOUT (the env-configurable
    safety net) if anything goes sideways.
    """
    try:
        max_mb = max_size_bytes / (1024 * 1024)
        # DD-scale (300+ MB or 50+ pages): 4h
        if max_mb >= 300 or total_pages >= 50:
            return 4 * 3600
        # Large (100-300 MB): 2h
        if max_mb >= 100 or total_pages >= 25:
            return 2 * 3600
        # Medium (30-100 MB or 10+ pages): 1h
        if max_mb >= 30 or total_pages >= 10:
            return 3600
        # Small everything else: 60 min
        return 3600
    except Exception:
        return RQ_JOB_TIMEOUT


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
        "lymewash_rate": "lymewash",
        "plaster_rate":  "plaster",
    }
    org = user.current_organization if user else None
    saved = (org.pricing_overrides or {}) if org else {}
    saved_rates = saved.get("rates") or {}
    saved_markup = saved.get("markup")
    advanced_enabled = set(saved.get("advanced_enabled") or [])

    rates = {}
    for shorthand, _label, _unit, _default in RATE_FIELDS:
        if shorthand in saved_rates:
            rates[shorthand] = float(saved_rates[shorthand])
        else:
            pm_key = _rate_map.get(shorthand)
            tiers = (PRICING_MODEL.get(pm_key) or {}).get("tiers") or []
            rates[shorthand] = float(tiers[0]["rate"]) if tiers else 0.0

    advanced_rates = {}
    for shorthand, _label, _unit, _default, _cat in ADVANCED_RATE_FIELDS:
        if shorthand in saved_rates:
            advanced_rates[shorthand] = float(saved_rates[shorthand])
        else:
            pm_key = _rate_map.get(shorthand)
            tiers = (PRICING_MODEL.get(pm_key) or {}).get("tiers") or []
            advanced_rates[shorthand] = float(tiers[0]["rate"]) if tiers else 0.0

    markup = float(saved_markup) if saved_markup is not None else 0.06
    return rates, markup, advanced_rates, advanced_enabled


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
                # Valid Clerk session but no local row yet — happens on the
                # first landing-page hit after sign-in, since `/` is public
                # and so never runs @require_auth's _sync_user. Provision
                # the row + default org now, then re-query.
                from auth import _sync_user
                _sync_user(clerk_uid)
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
                "denied_at": (org.denied_at if org else None),
            }
            # Pre-fetch effective rates while the session is still open.
            rates, markup, adv_rates, adv_enabled = (
                _effective_user_overrides(user) if org
                else _effective_user_overrides(None)
            )
            snapshot["rates"] = rates
            snapshot["markup"] = markup
            snapshot["advanced_rates"] = adv_rates
            snapshot["advanced_enabled"] = adv_enabled
            saved = (org.pricing_overrides or {}) if org else {}
            snapshot["has_org_overrides"] = bool(
                saved.get("rates")
                or saved.get("markup") is not None
                or saved.get("advanced_enabled")
            )
            return user.id, snapshot
    except Exception as exc:
        logger.debug("Optional auth detection failed: %s", exc)
        return None, None


@app.route("/favicon.ico")
def favicon():
    """Browser tab icon. Browsers auto-request /favicon.ico from the root, so
    this single route covers every page without touching the 14 templates."""
    return send_from_directory(
        app.static_folder, "logo_helm_sm.png", mimetype="image/png",
    )


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

    Phones get the streamlined /mobile submission form once signed in.
    Anonymous phone visitors still see the full marketing landing.
    ?desktop=1 opts a phone into the desktop layout regardless of auth.
    """
    _uid, snap = _try_signed_in_user_snapshot()
    if snap is None:
        # Cold-cookie path: render landing. Its JS detects an existing Clerk
        # session and reloads (now with the cookie set) — that lands here
        # again with `snap` populated. Anon phones get the same marketing
        # page (now mobile-responsive); /mobile is only useful post-login.
        return render_template("landing.html")

    if _is_mobile_ua() and request.args.get("desktop") != "1":
        return redirect(url_for("mobile"))

    if snap["org_id"] is None:
        return redirect(url_for("onboarding"))

    if not snap["is_beta_approved"]:
        if snap["denied_at"] is not None:
            return render_template(
                "waitlist.html",
                org_name=snap["org_name"],
                requested_at=snap["approval_requested_at"],
                denied=True,
            )
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
        advanced_rate_fields=ADVANCED_RATE_FIELDS,
        effective_rates=snap["rates"],
        effective_markup=snap["markup"],
        advanced_rates=snap["advanced_rates"],
        advanced_enabled=snap["advanced_enabled"],
        has_org_overrides=snap["has_org_overrides"],
        org_name=snap["org_name"],
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


def _check_submission_gates(user_id):
    """Run beta-approval + daily-cap gates for the current user. Returns
    (org_id, error_message). Caller redirects on error_message."""
    with session_scope() as session:
        user = session.get(User, user_id)
        org_id = user.current_organization_id if user else None

    if org_id is None:
        return None, "Your account is not yet fully set up. Please contact support."

    with session_scope() as session:
        org = session.get(Organization, org_id)
        if org is None or not org.is_beta_approved:
            return org_id, (
                "Your organization is on the Nightshift AI beta waitlist. "
                "We'll email you as soon as your access is approved."
            )

        cap = org.daily_submission_cap or BETA_DAILY_SUBMISSION_CAP_DEFAULT
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent_count = (
            session.query(Submission)
            .filter(Submission.org_id == org_id, Submission.submitted_at >= cutoff)
            .count()
        )
        if recent_count >= cap:
            return org_id, (
                f"Your organization has reached its daily submission limit "
                f"({cap} per 24 hours). Please try again later or contact support "
                f"to request a higher limit."
            )

    return org_id, None


@app.route("/api/uploads/init", methods=["POST"])
@require_auth
def api_uploads_init():
    """Issue presigned R2 multipart-upload URLs for browser-direct upload.

    Body: {"files": [{"filename": str, "size": int}, ...]}
    Returns:
      {"submission_id": str, "part_size": int,
       "uploads": [{"filename", "key", "upload_id",
                    "parts": [{"part_number": int, "url": str}, ...]}, ...]}

    The submission_id is allocated here and used as the R2 prefix so the
    final /submit call can locate the keys. No DB row is created — keys
    that are abandoned mid-upload are cleaned up by the bucket lifecycle
    rule (24h abort on incomplete multipart).
    """
    user_id = current_user_id()
    org_id, err = _check_submission_gates(user_id)
    if err:
        # 403 for gate failures, 429 for rate limit. The browser surfaces the
        # `error` field directly to the user.
        status = 429 if "limit" in err.lower() else 403
        return jsonify({"error": err}), status

    payload = request.get_json(silent=True) or {}
    files_in = payload.get("files") or []
    if not isinstance(files_in, list) or not files_in:
        return jsonify({"error": "Missing files."}), 400
    if len(files_in) > MAX_PDFS_PER_EMAIL:
        return jsonify({"error": f"Maximum {MAX_PDFS_PER_EMAIL} files allowed."}), 400

    submission_id = str(uuid.uuid4())
    seen_filenames = set()
    uploads_resp = []

    try:
        for idx, entry in enumerate(files_in):
            raw_name = (entry.get("filename") or "").strip()
            try:
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid file size."}), 400

            if not raw_name:
                return jsonify({"error": "Missing filename."}), 400
            if os.path.splitext(raw_name)[1].lower() not in ALLOWED_EXTENSIONS:
                return jsonify({"error": f"Only PDF files are accepted. Rejected: {raw_name}"}), 400
            if size <= 0:
                return jsonify({"error": f"{raw_name}: invalid size."}), 400
            if size > MAX_PDF_SIZE_MB * 1024 * 1024:
                return jsonify({"error": f"{raw_name} exceeds the {MAX_PDF_SIZE_MB} MB size limit."}), 400

            filename = secure_filename(raw_name) or f"upload_{idx + 1}.pdf"
            # Prevent collisions if the user picks two files with the same name.
            base, ext = os.path.splitext(filename)
            n = 1
            while filename in seen_filenames:
                n += 1
                filename = f"{base}_{n}{ext}"
            seen_filenames.add(filename)

            key = storage.upload_key(submission_id, filename)
            upload_id = storage.create_multipart_upload(key, content_type="application/pdf")

            num_parts = max(1, (size + UPLOAD_PART_SIZE_BYTES - 1) // UPLOAD_PART_SIZE_BYTES)
            parts = [
                {"part_number": pn, "url": storage.presign_upload_part(key, upload_id, pn)}
                for pn in range(1, num_parts + 1)
            ]

            uploads_resp.append({
                "filename": filename,
                "original_filename": raw_name,
                "key": key,
                "upload_id": upload_id,
                "size": size,
                "parts": parts,
            })
    except storage.StorageNotConfigured as exc:
        logger.error("R2 not configured: %s", exc)
        return jsonify({"error": "Storage is not configured. Please contact support."}), 500
    except Exception as exc:
        logger.error("uploads/init failed for submission %s: %s", submission_id, exc, exc_info=True)
        # Best-effort: abort any multiparts we already created in this request.
        for u in uploads_resp:
            storage.abort_multipart_upload(u["key"], u["upload_id"])
        return jsonify({"error": "Could not start upload. Please try again."}), 500

    logger.info("uploads/init: user %s org %s submission %s — %d files",
                user_id, org_id, submission_id, len(uploads_resp))
    return jsonify({
        "submission_id": submission_id,
        "part_size": UPLOAD_PART_SIZE_BYTES,
        "uploads": uploads_resp,
    })


def _validate_submission_key(key: str) -> bool:
    """Keys we issue look like submissions/<uuid>/uploads/<filename>. Refuse
    anything else so a forged /complete or /abort can't touch other prefixes."""
    return key.startswith("submissions/") and "/uploads/" in key and ".." not in key


@app.route("/api/uploads/complete", methods=["POST"])
@require_auth
def api_uploads_complete():
    """Finalize a multipart upload after the browser has PUT every part."""
    user_id = current_user_id()
    payload = request.get_json(silent=True) or {}
    key = (payload.get("key") or "").strip()
    upload_id = (payload.get("upload_id") or "").strip()
    parts_in = payload.get("parts") or []

    if not key or not upload_id or not parts_in:
        return jsonify({"error": "Missing key, upload_id, or parts."}), 400
    if not _validate_submission_key(key):
        return jsonify({"error": "Invalid key."}), 400

    try:
        parts = sorted(
            [{"PartNumber": int(p["part_number"]), "ETag": str(p["etag"])} for p in parts_in],
            key=lambda p: p["PartNumber"],
        )
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Malformed parts list."}), 400

    try:
        storage.complete_multipart_upload(key, upload_id, parts)
        head = storage.head_object(key)
        size = int(head.get("ContentLength") or 0)
    except storage.StorageNotConfigured as exc:
        logger.error("R2 not configured: %s", exc)
        return jsonify({"error": "Storage is not configured."}), 500
    except Exception as exc:
        logger.error("uploads/complete failed (user %s, key %s): %s", user_id, key, exc, exc_info=True)
        return jsonify({"error": "Could not finalize upload."}), 500

    if size > MAX_PDF_SIZE_MB * 1024 * 1024:
        # Browser declared a smaller size at init time but uploaded more bytes.
        # Delete the object and reject — don't let it through to /submit.
        try:
            storage.get_client().delete_object(Bucket=storage.R2_BUCKET, Key=key)
        except Exception:
            pass
        return jsonify({
            "error": f"Uploaded file exceeds the {MAX_PDF_SIZE_MB} MB size limit."
        }), 400

    return jsonify({"ok": True, "key": key, "size": size})


@app.route("/api/uploads/abort", methods=["POST"])
@require_auth
def api_uploads_abort():
    """Best-effort cleanup if the browser cancels mid-upload."""
    payload = request.get_json(silent=True) or {}
    key = (payload.get("key") or "").strip()
    upload_id = (payload.get("upload_id") or "").strip()
    if not key or not upload_id or not _validate_submission_key(key):
        return jsonify({"error": "Invalid key or upload_id."}), 400
    storage.abort_multipart_upload(key, upload_id)
    return jsonify({"ok": True})


@app.route("/api/uploads/presign-part", methods=["POST"])
@require_auth
def api_uploads_presign_part():
    """Re-mint a presigned URL for a single part so the browser can recover
    from URL expiry mid-upload without restarting the whole file."""
    payload = request.get_json(silent=True) or {}
    key = (payload.get("key") or "").strip()
    upload_id = (payload.get("upload_id") or "").strip()
    try:
        part_number = int(payload.get("part_number") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid part_number."}), 400

    if not key or not upload_id or part_number < 1:
        return jsonify({"error": "Missing key, upload_id, or part_number."}), 400
    if not _validate_submission_key(key):
        return jsonify({"error": "Invalid key."}), 400

    try:
        url = storage.presign_upload_part(key, upload_id, part_number)
    except storage.StorageNotConfigured as exc:
        logger.error("R2 not configured: %s", exc)
        return jsonify({"error": "Storage is not configured."}), 500
    except Exception as exc:
        logger.error("uploads/presign-part failed (key %s, part %d): %s",
                     key, part_number, exc, exc_info=True)
        return jsonify({"error": "Could not refresh URL."}), 500

    return jsonify({"url": url})


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

    # 2. Get uploaded files. Two paths:
    #    (a) New: browser uploaded directly to R2 via /api/uploads/* and posts
    #        a JSON manifest in the `uploaded_manifest` form field. Files are
    #        already in R2; we just need to verify and record them.
    #    (b) Legacy fallback: traditional multipart form with file blobs in
    #        `attachments`. Used by JS-disabled clients and as a safety net.
    uploaded_files = []   # list of (filename, r2_key, size_bytes)
    total_pages = 0
    max_size_bytes = 0
    manifest_raw = (request.form.get("uploaded_manifest") or "").strip()

    if manifest_raw:
        # ---- (a) Browser-direct upload path ----
        try:
            manifest = json.loads(manifest_raw)
            submission_id = str(manifest["submission_id"])
            entries = list(manifest["files"])
        except (ValueError, KeyError, TypeError):
            flash("Upload manifest was malformed. Please try again.", "error")
            return redirect(url_for("index"))

        # uuid sanity check — keys we issued always live under
        # submissions/<uuid>/uploads/, so refuse anything that doesn't fit.
        try:
            uuid.UUID(submission_id)
        except (ValueError, TypeError):
            flash("Invalid submission id.", "error")
            return redirect(url_for("index"))

        if not entries:
            flash("Please upload at least one PDF file.", "error")
            return redirect(url_for("index"))
        if len(entries) > MAX_PDFS_PER_EMAIL:
            flash(f"Maximum {MAX_PDFS_PER_EMAIL} files allowed.", "error")
            return redirect(url_for("index"))

        try:
            for entry in entries:
                filename = str(entry.get("filename") or "").strip()
                key = str(entry.get("key") or "").strip()
                if not filename or not key:
                    flash("Upload manifest is missing fields.", "error")
                    return redirect(url_for("index"))
                # Defensive: every key must live under THIS submission's prefix.
                expected_prefix = storage.submission_prefix(submission_id) + "uploads/"
                if not key.startswith(expected_prefix):
                    logger.warning("submit: rejecting key %s outside prefix %s (user %s)",
                                   key, expected_prefix, user_id)
                    flash("Upload manifest references an unexpected location.", "error")
                    return redirect(url_for("index"))

                head = storage.head_object(key)
                size_bytes = int(head.get("ContentLength") or 0)
                if size_bytes <= 0:
                    flash(f"{filename}: upload appears empty. Please try again.", "error")
                    return redirect(url_for("index"))
                if size_bytes > MAX_PDF_SIZE_MB * 1024 * 1024:
                    flash(f"{filename} exceeds the {MAX_PDF_SIZE_MB} MB size limit.", "error")
                    return redirect(url_for("index"))

                if size_bytes > max_size_bytes:
                    max_size_bytes = size_bytes
                uploaded_files.append((filename, key, size_bytes))
        except storage.StorageNotConfigured as exc:
            logger.error("R2 not configured: %s", exc)
            flash("Storage is not configured. Please contact support.", "error")
            return redirect(url_for("index"))
        except Exception as exc:
            logger.error("submit: manifest verification failed for %s: %s",
                         submission_id, exc, exc_info=True)
            flash("Could not verify your uploaded files. Please try again.", "error")
            return redirect(url_for("index"))
        # Page count is deferred to the worker — the file is in R2, not on disk.
        # Queue routing falls back to size alone, which is fine: a 660-page PDF
        # is always large enough to trip HEAVY_QUEUE_FILE_MB.
    else:
        # ---- (b) Legacy form-file path ----
        files = request.files.getlist("attachments")
        valid_files = [f for f in files if f.filename and f.filename.strip()]

        if not valid_files:
            flash("Please upload at least one PDF file.", "error")
            return redirect(url_for("index"))

        if len(valid_files) > MAX_PDFS_PER_EMAIL:
            flash(f"Maximum {MAX_PDFS_PER_EMAIL} files allowed.", "error")
            return redirect(url_for("index"))

        submission_id = str(uuid.uuid4())

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
    for key, _label, _unit, _default, _cat in ADVANCED_RATE_FIELDS:
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

    # 6. Enqueue the job — route to fast or heavy queue and pick a per-job
    #    timeout sized to the payload (big DD-scale sets need 4h, small
    #    single-PDF jobs only need 30 min).
    pdf_keys = [k for (_, k, _) in uploaded_files]
    queue, queue_name = _pick_queue(total_pages, max_size_bytes)
    job_timeout = _pick_timeout(total_pages, max_size_bytes)
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
            job_timeout=job_timeout,
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

    logger.info("Submission %s enqueued on %s — %d PDFs, %d pages, %.1f MB max, timeout=%ds from %s <%s> (job %s)",
                submission_id, queue_name, len(pdf_keys), total_pages,
                max_size_bytes / (1024 * 1024), job_timeout, name, email_addr, job.id)

    # Post/Redirect/Get: land the user on a GET-able URL so that refresh,
    # back-button, or a shared link doesn't reload as `GET /submit` (405).
    return redirect(url_for("thank_you", submission_id=submission_id))


# Hard cap on how many revisions a single project tree can spawn. Sanity
# guard — if Rider is on v10 of a project, something process-wise is wrong
# and a fresh submission is the right move.
MAX_SUBMISSION_VERSIONS = 10


@app.route("/submit/<parent_id>/resubmit", methods=["POST"])
@require_auth
def resubmit(parent_id):
    """Add files to an existing submission and re-run incrementally.

    Creates a v2+ child submission pointing at `parent_id`, uploads the new
    PDFs to the child's R2 prefix, and enqueues `jobs.merge_submission`. The
    parent's stored result JSON is the baseline; only the new files are
    re-extracted, then merged with replace-vs-union semantics driven by the
    posted `merge_scope_tags`.
    """
    user_id = current_user_id()
    with session_scope() as session:
        user = session.get(User, user_id)
        name = user.name or ""
        email_addr = user.email
        org_id = user.current_organization_id

    if org_id is None:
        logger.error("resubmit blocked: user %s has no current_organization_id",
                     user_id)
        flash("Your account is not yet fully set up. Please contact support.",
              "error")
        return redirect(url_for("index"))

    # Beta gate + daily cap apply to merge re-runs the same as fresh submissions.
    with session_scope() as session:
        org = session.get(Organization, org_id)
        if org is None or not org.is_beta_approved:
            logger.info("resubmit blocked: org %s not beta-approved (user %s)",
                        org_id, user_id)
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
            logger.info("resubmit blocked: org %s hit daily cap %d (user %s)",
                        org_id, cap, user_id)
            flash(
                f"Your organization has reached its daily submission limit "
                f"({cap} per 24 hours). Please try again later.",
                "error",
            )
            return redirect(url_for("index"))

    # Parent lookup + access checks (org-scoped so a teammate can revise).
    with session_scope() as session:
        parent = session.get(Submission, parent_id)
        if parent is None:
            return ("Not found", 404)
        if parent.org_id != org_id:
            logger.warning("resubmit blocked: user %s tried to revise %s (org mismatch)",
                           user_id, parent_id)
            return ("Not found", 404)
        if parent.status != "completed":
            flash(
                f"This submission is in status '{parent.status}' — only completed "
                f"submissions can be re-run incrementally. Submit fresh instead.",
                "error",
            )
            return redirect(url_for("job_detail", submission_id=parent_id)
                            if "job_detail" in app.view_functions
                            else url_for("index"))
        if (parent.version or 1) >= MAX_SUBMISSION_VERSIONS:
            flash(
                f"This project has reached the {MAX_SUBMISSION_VERSIONS}-revision "
                f"limit. Please start a fresh submission.",
                "error",
            )
            return redirect(url_for("index"))

        parent_user_id = parent.user_id
        parent_version = parent.version or 1
        parent_root_id = parent.parent_submission_id or parent.id
        parent_business_name = parent.business_name
        parent_phone = parent.phone

    # Verify a parent result JSON exists in R2 — without it, merge_submission
    # has no baseline to load.
    has_parent_json = False
    with session_scope() as session:
        has_parent_json = (
            session.query(File)
            .filter(
                File.submission_id == parent_id,
                File.kind == "result",
                File.filename.like("%.json"),
            )
            .first()
            is not None
        )
    if not has_parent_json:
        flash(
            "We couldn't find the original analysis JSON for this submission. "
            "Please re-submit fresh.",
            "error",
        )
        return redirect(url_for("index"))

    # Phase 1B accepts only the legacy multipart form path. The browser-direct
    # R2 manifest flow used by /submit is not yet wired for resubmit.
    files = request.files.getlist("attachments")
    valid_files = [f for f in files if f.filename and f.filename.strip()]

    # Typed re-run fields — needed by both the merge path (new files) and the
    # no-file path (re-run the original plans with these notes as guidance).
    merge_notes_text = (request.form.get("merge_notes") or "").strip() or None
    merge_sheet_hint = (request.form.get("merge_sheet_hint") or "").strip() or None
    raw_tags = request.form.getlist("merge_scope_tags")
    if not raw_tags:
        # Allow comma-separated single field as well as repeated form fields.
        single = (request.form.get("merge_scope_tags") or "").strip()
        raw_tags = [t.strip() for t in single.split(",") if t.strip()] if single else []
    merge_scope_tags = [t for t in raw_tags if t]

    # A re-run needs SOMETHING to act on: new files, or typed notes / a sheet
    # hint / scope tags that steer a refine of the existing estimate. With no
    # new files we skip re-extraction entirely (cheap) and re-run the
    # downstream passes against the stored JSON — see jobs.merge_submission /
    # run_analysis_merge. Block only a fully-empty submit so we can't silently
    # re-bill a completed job.
    if not valid_files and not (merge_notes_text or merge_sheet_hint
                                or merge_scope_tags):
        flash("Add at least one PDF, or describe what to change, to re-run.",
              "error")
        return redirect(url_for("index"))

    if len(valid_files) > MAX_PDFS_PER_EMAIL:
        flash(f"Maximum {MAX_PDFS_PER_EMAIL} files allowed per re-run.", "error")
        return redirect(url_for("index"))

    submission_id = str(uuid.uuid4())
    uploaded_files = []   # (filename, key, size_bytes)
    total_pages = 0
    max_size_bytes = 0

    with tempfile.TemporaryDirectory(prefix=f"ns-resubmit-{submission_id}-") as staging:
        try:
            for idx, f in enumerate(valid_files):
                filename = secure_filename(f.filename) or f"upload_{idx + 1}.pdf"
                ext = os.path.splitext(filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    flash(f"Only PDF files are accepted. Rejected: {f.filename}",
                          "error")
                    return redirect(url_for("index"))

                local_path = os.path.join(staging, filename)
                f.save(local_path)

                size_bytes = os.path.getsize(local_path)
                if size_bytes > MAX_PDF_SIZE_MB * 1024 * 1024:
                    flash(f"{filename} exceeds the {MAX_PDF_SIZE_MB} MB size limit.",
                          "error")
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
            logger.error("Upload error for resubmit %s: %s", submission_id,
                         exc, exc_info=True)
            flash("An error occurred while uploading your files. Please try again.",
                  "error")
            try:
                storage.delete_prefix(storage.submission_prefix(submission_id))
            except Exception:
                pass
            return redirect(url_for("index"))

    # (Typed re-run fields — merge_notes_text, merge_sheet_hint,
    # merge_scope_tags — were parsed above, before the no-file branch.)

    # Persist child submission + uploaded files.
    try:
        with session_scope() as session:
            sub = Submission(
                id=submission_id,
                user_id=user_id,
                org_id=org_id,
                parent_submission_id=parent_id,
                version=parent_version + 1,
                merge_notes=merge_notes_text,
                merge_scope_tags=merge_scope_tags or None,
                phone=parent_phone,
                business_name=parent_business_name,
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
        logger.error("DB write failed for resubmit %s: %s", submission_id,
                     exc, exc_info=True)
        flash("An error occurred while saving your re-run. Please try again.",
              "error")
        try:
            storage.delete_prefix(storage.submission_prefix(submission_id))
        except Exception:
            pass
        return redirect(url_for("index"))

    # Pricing: child uses parent's snapshot (carried in prior_json). Per-job
    # rate overrides on the resubmit form aren't supported in Phase 1B —
    # keep the quote rate-stable across versions by default.

    pdf_keys = [k for (_, k, _) in uploaded_files]
    queue, queue_name = _pick_queue(total_pages, max_size_bytes)
    job_timeout = _pick_timeout(total_pages, max_size_bytes)
    try:
        job = queue.enqueue(
            "jobs.merge_submission",
            kwargs={
                "submission_id": submission_id,
                "parent_id": parent_id,
                "new_pdf_keys": pdf_keys,
                "contact_info": {
                    "name": name,
                    "email": email_addr,
                    "phone": parent_phone or "",
                    "business_name": parent_business_name or "",
                },
                "scope_notes": merge_notes_text,
                "scope_tags": merge_scope_tags,
                "sheet_hint": merge_sheet_hint,
                "rate_overrides": None,
            },
            job_id=submission_id,
            job_timeout=job_timeout,
            result_ttl=RQ_RESULT_TTL,
            failure_ttl=RQ_RESULT_TTL,
        )
    except Exception as exc:
        logger.error("Failed to enqueue merge %s: %s", submission_id, exc)
        flash("Our queue is unavailable right now. Please try again in a few minutes.",
              "error")
        try:
            with session_scope() as session:
                sub = session.get(Submission, submission_id)
                if sub:
                    sub.status = "failed"
                    sub.error = f"enqueue failed: {exc}"
        except Exception:
            pass
        return redirect(url_for("index"))

    logger.info(
        "Resubmit %s enqueued on %s — parent=%s (root=%s) v%d→v%d, "
        "%d new PDFs, tags=%s (job %s)",
        submission_id, queue_name, parent_id, parent_root_id,
        parent_version, parent_version + 1, len(pdf_keys),
        merge_scope_tags, job.id,
    )

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
            estimate = None
            annotated_drawings = []
            if s.status == "completed":
                for f in s.files:
                    if f.kind == "result":
                        # End users don't know what to do with the raw result
                        # JSON — hide it from the UI. The file still lives in
                        # R2 and is mailed to admins separately for archive.
                        if f.filename.lower().endswith(".json"):
                            continue
                        try:
                            entry = {
                                "filename": f.filename,
                                "url": storage.presigned_download_url(f.r2_key),
                                "is_estimate": is_estimate_filename(f.filename),
                                "is_annotated_drawings": is_annotated_drawings_filename(f.filename),
                            }
                        except Exception as exc:
                            logger.warning("Could not sign URL for %s: %s", f.r2_key, exc)
                            continue
                        if entry["is_estimate"]:
                            estimate = entry
                        elif entry["is_annotated_drawings"]:
                            annotated_drawings.append(entry)
                        else:
                            results.append(entry)
            rows.append({
                "id": s.id,
                "business_name": s.business_name,
                "submitted_at": s.submitted_at,
                "deadline": s.deadline,
                "status": s.status,
                "subtotal": float(s.subtotal) if s.subtotal is not None else None,
                "upload_count": sum(1 for f in s.files if f.kind == "upload"),
                "results": results,
                "estimate": estimate,
                "annotated_drawings": annotated_drawings,
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

        # Strip raw result JSON from the user-facing payload — it's an
        # engineering artifact, not something the contractor would open.
        # The file is still in R2 and is sent to admins separately.
        result_files = [
            f for f in sub.files
            if f.kind == "result"
            and not f.filename.lower().endswith(".json")
        ]
        results = [{
            "filename": f.filename,
            "size": f.size_bytes,
            "url": storage.presigned_download_url(f.r2_key),
            "is_estimate": is_estimate_filename(f.filename),
            "is_annotated_drawings": is_annotated_drawings_filename(f.filename),
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


@app.route("/api/jobs/<submission_id>/resubmit-context", methods=["GET"])
@require_auth
def job_resubmit_context_api(submission_id):
    """Context the resubmit modal needs to render: parent's known floor
    names (for the scope-tag picker), schedule presence, version + cap,
    and any prior merge_log so the UI can show revision history.

    Authorization: org-scoped (any teammate in the parent's org can revise),
    matching the resubmit route itself.
    """
    user_id = current_user_id()
    with session_scope() as session:
        user = session.get(User, user_id)
        org_id = user.current_organization_id if user else None
        sub = session.get(Submission, submission_id)
        if sub is None or sub.org_id != org_id:
            return jsonify({"error": "not found"}), 404
        if sub.status != "completed":
            return jsonify({"error": "not completed", "status": sub.status}), 409

        version = sub.version or 1
        json_files = [f for f in sub.files
                      if f.kind == "result" and f.filename.lower().endswith(".json")]
        if not json_files:
            return jsonify({"error": "result JSON not found"}), 404
        json_file = sorted(json_files, key=lambda f: f.id, reverse=True)[0]
        r2_key = json_file.r2_key

    try:
        raw = storage.get_bytes(r2_key)
        data = json.loads(raw)
    except Exception as exc:
        logger.error("Failed to load result JSON %s: %s", r2_key, exc)
        return jsonify({"error": "failed to load result"}), 500

    analysis = data.get("analysis") or {}
    floors = analysis.get("floors") or []
    floor_names = [f.get("floor_name") for f in floors if f.get("floor_name")]

    return jsonify({
        "version": version,
        "max_versions": MAX_SUBMISSION_VERSIONS,
        "can_resubmit": version < MAX_SUBMISSION_VERSIONS,
        "floor_names": floor_names,
        "has_door_schedule": bool(analysis.get("has_door_schedule")),
        "has_window_schedule": bool(analysis.get("has_window_schedule")),
        "subtotal": (data.get("cost_estimate") or {}).get("subtotal"),
        "manual_review_required": bool(data.get("manual_review_required")),
        "merge_log": data.get("merge_log") or [],
    })


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


CHAT_MAX_MESSAGE_CHARS = 4000
CHAT_MAX_HISTORY = 40  # 20 turns of user/assistant


@app.route("/api/jobs/<submission_id>/chat", methods=["POST"])
@require_auth
def job_chat_api(submission_id):
    """Answer a follow-up question about a completed takeoff job.

    Body: {"messages": [{"role": "user"|"assistant", "content": "..."}, ...]}
    Returns: {"role": "assistant", "content": "..."}

    Stateless — the client sends the full conversation history each turn.
    """
    body = request.get_json(silent=True) or {}
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages required"}), 400
    if len(messages) > CHAT_MAX_HISTORY:
        return jsonify({"error": "too many messages"}), 400
    for m in messages:
        if (not isinstance(m, dict)
                or m.get("role") not in ("user", "assistant")
                or not isinstance(m.get("content"), str)
                or not m["content"].strip()):
            return jsonify({"error": "invalid message"}), 400
        if len(m["content"]) > CHAT_MAX_MESSAGE_CHARS:
            return jsonify({"error": "message too long"}), 400
    if messages[0].get("role") != "user":
        return jsonify({"error": "first message must be from user"}), 400

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
        json_file = sorted(json_files, key=lambda f: f.id, reverse=True)[0]
        r2_key = json_file.r2_key

    try:
        raw = storage.get_bytes(r2_key)
        data = json.loads(raw)
    except Exception as exc:
        logger.error("Failed to load result JSON %s: %s", r2_key, exc)
        return jsonify({"error": "failed to load result"}), 500

    try:
        from chat import chat_about_job
        result = chat_about_job(data, messages)
    except Exception as exc:
        logger.error("Chat failed for submission %s: %s", submission_id, exc, exc_info=True)
        return jsonify({"error": "chat failed"}), 500

    return jsonify({
        "role": "assistant",
        "content": result.get("reply", ""),
        "proposed_corrections": result.get("proposed_corrections") or [],
    })


@app.route("/api/help/chat", methods=["POST"])
@require_auth
def general_help_chat_api():
    """Answer a general product-help question about Knight Shift.

    Body: {"messages": [{"role": "user"|"assistant", "content": "..."}, ...]}
    Returns: {"role": "assistant", "content": "..."}
    """
    body = request.get_json(silent=True) or {}
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages required"}), 400
    if len(messages) > CHAT_MAX_HISTORY:
        return jsonify({"error": "too many messages"}), 400
    for m in messages:
        if (not isinstance(m, dict)
                or m.get("role") not in ("user", "assistant")
                or not isinstance(m.get("content"), str)
                or not m["content"].strip()):
            return jsonify({"error": "invalid message"}), 400
        if len(m["content"]) > CHAT_MAX_MESSAGE_CHARS:
            return jsonify({"error": "message too long"}), 400
    if messages[0].get("role") != "user":
        return jsonify({"error": "first message must be from user"}), 400

    try:
        from chat import chat_general_help
        reply = chat_general_help(messages)
    except Exception as exc:
        logger.error("General help chat failed: %s", exc, exc_info=True)
        return jsonify({"error": "chat failed"}), 500

    return jsonify({"role": "assistant", "content": reply})


@app.route("/api/jobs/completed/list", methods=["GET"])
@require_auth
def completed_jobs_list_api():
    """Lightweight list of the current user's completed jobs for the chat picker.

    Returns: [{"id", "label", "submitted_at", "subtotal"}, ...] — newest first.
    """
    uid = current_user_id()
    items = []
    with session_scope() as session:
        subs = (session.query(Submission)
                .filter(Submission.user_id == uid)
                .filter(Submission.status == "completed")
                .order_by(Submission.submitted_at.desc())
                .limit(50).all())
        for s in subs:
            label = s.business_name or s.id[:8]
            items.append({
                "id": s.id,
                "label": label,
                "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
                "subtotal": float(s.subtotal) if s.subtotal is not None else None,
            })
    return jsonify(items)


@app.route("/api/jobs/<submission_id>/regenerate", methods=["POST"])
@require_auth
def job_regenerate_api(submission_id):
    """Re-render a completed estimate with adjusted line-item rates / markups.

    Body: {"adjustments": [{"label": "<cleanLabel>", "rate": <float>,
                            "markup_pct": <float>}, ...]}

    Loads the saved result JSON, applies the new rates/markups to the
    matching line items, recomputes subtotal, re-renders the PDF, and
    re-uploads both back to R2. The DB row's subtotal is also updated.
    No LLM calls — this is a pure pricing recompute.
    """
    import re
    import tempfile
    from json_to_pdf import json_to_pdf

    uid = current_user_id()
    payload = request.get_json(silent=True) or {}
    adjustments = payload.get("adjustments") or []
    by_label = {}
    for adj in adjustments:
        label = (adj.get("label") or "").strip()
        if not label:
            continue
        try:
            rate = float(adj.get("rate"))
            markup_pct = float(adj.get("markup_pct"))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid rate/markup_pct"}), 400
        if rate < 0 or markup_pct < 0 or markup_pct > 100:
            return jsonify({"error": "rate/markup_pct out of range"}), 400
        by_label[label] = (rate, markup_pct)

    if not by_label:
        return jsonify({"error": "no adjustments provided"}), 400

    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != uid:
            return jsonify({"error": "not found"}), 404
        if sub.status != "completed":
            return jsonify({"error": "not completed"}), 409

        json_files = [f for f in sub.files
                      if f.kind == "result"
                      and f.filename.lower().endswith(".json")]
        pdf_files = [f for f in sub.files
                     if f.kind == "result"
                     and f.filename.lower().endswith(".pdf")]
        if not json_files:
            return jsonify({"error": "result JSON not found"}), 404

        json_file = sorted(json_files, key=lambda f: f.id, reverse=True)[0]
        json_key = json_file.r2_key
        json_filename = json_file.filename
        pdf_key = pdf_files[0].r2_key if pdf_files else None
        pdf_filename = (pdf_files[0].filename if pdf_files
                        else json_filename.replace(".json", ".pdf"))

    try:
        raw = storage.get_bytes(json_key)
        data = json.loads(raw)
    except Exception as exc:
        logger.error("Regenerate: failed to load %s: %s", json_key, exc)
        return jsonify({"error": "failed to load result"}), 500

    cost_estimate = data.get("cost_estimate") or {}
    line_items = cost_estimate.get("line_items") or []
    rate_re = re.compile(r"@ \$[\d,]+\.\d+")

    matched = 0
    for li in line_items:
        qty = float(li.get("qty") or 0)
        if qty == 0:
            continue
        label = (li.get("item") or "").split(" - ")[0]
        if label not in by_label:
            continue
        rate, markup_pct = by_label[label]
        cost = qty * rate
        markup = cost * markup_pct / 100.0
        total = cost + markup
        li["cost"] = round(cost, 2)
        li["markup"] = round(markup, 2)
        li["total"] = round(total, 2)
        li["item"] = rate_re.sub(f"@ ${rate:.2f}", li.get("item") or "", count=1)
        matched += 1

    if matched == 0:
        return jsonify({"error": "no matching line items"}), 400

    new_subtotal = round(sum(float(li.get("total") or 0) for li in line_items), 2)
    cost_estimate["subtotal"] = new_subtotal
    data["cost_estimate"] = cost_estimate

    # Re-render PDF + re-upload both files.
    with tempfile.TemporaryDirectory(prefix=f"ns-regen-{submission_id}-") as workdir:
        json_path = os.path.join(workdir, json_filename)
        pdf_path = os.path.join(workdir, pdf_filename)
        try:
            with open(json_path, "w") as f:
                json.dump(data, f, indent=2)
            json_to_pdf(json_path, pdf_path)
            storage.upload_file(json_path, json_key,
                                content_type="application/json")
            if pdf_key:
                storage.upload_file(pdf_path, pdf_key,
                                    content_type="application/pdf")
            else:
                new_pdf_key = storage.result_key(submission_id, pdf_filename)
                storage.upload_file(pdf_path, new_pdf_key,
                                    content_type="application/pdf")
                with session_scope() as session:
                    session.add(File(
                        submission_id=submission_id,
                        kind="result",
                        filename=pdf_filename,
                        r2_key=new_pdf_key,
                        size_bytes=os.path.getsize(pdf_path),
                        content_type="application/pdf",
                    ))
        except Exception as exc:
            logger.error("Regenerate: render/upload failed for %s: %s",
                         submission_id, exc, exc_info=True)
            return jsonify({"error": "failed to regenerate"}), 500

    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is not None:
            sub.subtotal = new_subtotal

    logger.info("Submission %s regenerated by user %d — new subtotal $%.2f",
                submission_id, uid, new_subtotal)
    return jsonify({"ok": True, "subtotal": new_subtotal,
                    "items_updated": matched})


@app.route("/api/jobs/<submission_id>/adjust-quantities", methods=["POST"])
@require_auth
def job_adjust_quantities_api(submission_id):
    """Apply user quantity corrections to a completed estimate and re-price it.

    Body: {"adjustments": [{"label": "<leading line-item label>",
                            "qty": <float>}, ...]}

    Loads the saved result JSON, sets the new quantity on each matching
    cost-estimate line item (keeping its unit rate and markup %), recomputes
    line totals + subtotal, re-renders the PDF, and re-uploads both to R2.
    The DB subtotal and a manual_corrections audit log are updated too.
    No LLM calls — this is a pure pricing recompute.
    """
    import re
    import tempfile
    from datetime import datetime, timezone
    from json_to_pdf import json_to_pdf

    uid = current_user_id()
    payload = request.get_json(silent=True) or {}
    adjustments = payload.get("adjustments") or []
    by_label = {}
    for adj in adjustments:
        label = (adj.get("label") or "").strip()
        if not label:
            continue
        try:
            qty = float(adj.get("qty"))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid qty"}), 400
        if qty < 0 or qty > 10_000_000:
            return jsonify({"error": "qty out of range"}), 400
        by_label[label] = qty

    if not by_label:
        return jsonify({"error": "no adjustments provided"}), 400

    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != uid:
            return jsonify({"error": "not found"}), 404
        if sub.status != "completed":
            return jsonify({"error": "not completed"}), 409

        json_files = [f for f in sub.files
                      if f.kind == "result"
                      and f.filename.lower().endswith(".json")]
        pdf_files = [f for f in sub.files
                     if f.kind == "result"
                     and f.filename.lower().endswith(".pdf")]
        if not json_files:
            return jsonify({"error": "result JSON not found"}), 404

        json_file = sorted(json_files, key=lambda f: f.id, reverse=True)[0]
        json_key = json_file.r2_key
        json_filename = json_file.filename
        pdf_key = pdf_files[0].r2_key if pdf_files else None
        pdf_filename = (pdf_files[0].filename if pdf_files
                        else json_filename.replace(".json", ".pdf"))

    try:
        raw = storage.get_bytes(json_key)
        data = json.loads(raw)
    except Exception as exc:
        logger.error("Adjust-qty: failed to load %s: %s", json_key, exc)
        return jsonify({"error": "failed to load result"}), 500

    cost_estimate = data.get("cost_estimate") or {}
    line_items = cost_estimate.get("line_items") or []
    # Replace the qty shown right after the first " - " in the item label.
    qty_label_re = re.compile(r"( - )([\d,]+(?:\.\d+)?)")

    applied = []
    for li in line_items:
        label = (li.get("item") or "").split(" - ")[0]
        if label not in by_label:
            continue
        old_qty = float(li.get("qty") or 0)
        if old_qty <= 0:
            continue
        new_qty = by_label[label]
        old_cost = float(li.get("cost") or 0)
        old_markup = float(li.get("markup") or 0)
        rate = old_cost / old_qty
        markup_pct = (old_markup / old_cost) if old_cost else 0.0
        cost = new_qty * rate
        markup = cost * markup_pct
        li["qty"] = new_qty
        li["cost"] = round(cost, 2)
        li["markup"] = round(markup, 2)
        li["total"] = round(cost + markup, 2)
        li["item"] = qty_label_re.sub(
            lambda m: m.group(1) + format(new_qty, ",.0f"),
            li.get("item") or "", count=1)
        applied.append({"label": label, "from_qty": old_qty, "to_qty": new_qty})

    if not applied:
        return jsonify({"error": "no matching line items"}), 400

    new_subtotal = round(sum(float(li.get("total") or 0) for li in line_items), 2)
    cost_estimate["subtotal"] = new_subtotal
    data["cost_estimate"] = cost_estimate

    # Audit trail — the result JSON previously had no user-correction field.
    corrections_log = data.get("manual_corrections") or []
    corrections_log.append({
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "kind": "quantity",
        "changes": applied,
    })
    data["manual_corrections"] = corrections_log

    # Re-render PDF + re-upload both files.
    with tempfile.TemporaryDirectory(prefix=f"ns-adjqty-{submission_id}-") as workdir:
        json_path = os.path.join(workdir, json_filename)
        pdf_path = os.path.join(workdir, pdf_filename)
        try:
            with open(json_path, "w") as f:
                json.dump(data, f, indent=2)
            json_to_pdf(json_path, pdf_path)
            storage.upload_file(json_path, json_key,
                                content_type="application/json")
            if pdf_key:
                storage.upload_file(pdf_path, pdf_key,
                                    content_type="application/pdf")
            else:
                new_pdf_key = storage.result_key(submission_id, pdf_filename)
                storage.upload_file(pdf_path, new_pdf_key,
                                    content_type="application/pdf")
                with session_scope() as session:
                    session.add(File(
                        submission_id=submission_id,
                        kind="result",
                        filename=pdf_filename,
                        r2_key=new_pdf_key,
                        size_bytes=os.path.getsize(pdf_path),
                        content_type="application/pdf",
                    ))
        except Exception as exc:
            logger.error("Adjust-qty: render/upload failed for %s: %s",
                         submission_id, exc, exc_info=True)
            return jsonify({"error": "failed to regenerate"}), 500

    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is not None:
            sub.subtotal = new_subtotal

    logger.info("Submission %s quantities adjusted by user %d — new subtotal $%.2f",
                submission_id, uid, new_subtotal)
    return jsonify({"ok": True, "subtotal": new_subtotal,
                    "items_updated": len(applied)})


@app.route("/api/jobs/<submission_id>/prioritize", methods=["POST"])
@require_auth
def job_prioritize_api(submission_id):
    """Bump the caller's queued job to the front of its RQ queue.

    Implemented by removing the job from its current queue position and
    re-enqueuing with at_front=True. The DB row and job_id are unchanged.
    Owner-only — non-owners get 404 to avoid leaking IDs.
    """
    uid = current_user_id()
    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != uid:
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
    """Cancel a queued or in-flight job. Owner-only.

    Queued jobs: removed from RQ and marked cancelled.
    Processing jobs: stop signal sent to the worker; DB row marked cancelled.
    """
    uid = current_user_id()
    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != uid:
            return jsonify({"error": "not found"}), 404
        if sub.status not in ("queued", "processing"):
            return jsonify({"error": f"cannot cancel a {sub.status} job"}), 409
        was_processing = sub.status == "processing"
        sub.status = "cancelled"

    try:
        job = Job.fetch(submission_id, connection=_redis)
        if was_processing:
            try:
                send_stop_job_command(_redis, submission_id)
            except InvalidJobOperation:
                # Job finished between status check and stop signal — DB row
                # already marked cancelled; let the worker's completion no-op.
                pass
        else:
            job.cancel()
            job.delete()
    except NoSuchJobError:
        # DB row already updated; treat as success.
        pass
    except Exception as exc:
        logger.warning("Cancel: RQ cleanup failed for %s: %s", submission_id, exc)

    logger.info("Submission %s cancelled by user %d (was %s)",
                submission_id, uid, "processing" if was_processing else "queued")
    return jsonify({"ok": True})


# Email-address regex for the send-estimate endpoint is _EMAIL_RE, defined
# alongside the message-settings helpers at the top of this module.


@app.route("/api/jobs/<submission_id>/send-estimate", methods=["POST"])
@require_auth
def job_send_estimate_api(submission_id):
    """Email the formal Estimate PDF to a stakeholder. Owner-only.

    Body JSON: {to: "...", subject: "...", body: "..."}.
    The estimate PDF is the one tagged by generate_estimate_pdf.is_estimate_filename
    on the submission's result files; we download it from R2 and attach.
    """
    uid = current_user_id()
    data = request.get_json(silent=True) or {}
    to_email = (data.get("to") or "").strip()
    subject = (data.get("subject") or "").strip()
    body = data.get("body") or ""

    if not _EMAIL_RE.match(to_email):
        return jsonify({"error": "Enter a valid recipient email address."}), 400
    if not subject:
        return jsonify({"error": "Subject is required."}), 400
    if len(subject) > MESSAGE_SUBJECT_MAX:
        return jsonify({
            "error": f"Subject is too long (max {MESSAGE_SUBJECT_MAX} characters)."
        }), 400
    if not body.strip():
        return jsonify({"error": "Message body is required."}), 400

    # CC/BCC: accept either a list or a free-form string from the modal,
    # validate, and fall back to the org defaults when the client doesn't
    # send anything (the typical case — the modal pre-fills from defaults).
    def _coerce_list(v):
        if v is None:
            return None
        if isinstance(v, list):
            return ", ".join(str(x) for x in v if x)
        return str(v)

    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != uid:
            return jsonify({"error": "not found"}), 404
        if sub.status != "completed":
            return jsonify({"error": "Job must be completed before sending the estimate."}), 409
        estimate_file = next(
            (f for f in sub.files
             if f.kind == "result" and is_estimate_filename(f.filename)),
            None,
        )
        if estimate_file is None:
            return jsonify({"error": "No estimate PDF found for this submission."}), 404
        r2_key = estimate_file.r2_key
        filename = estimate_file.filename

        user = session.get(User, uid)
        org = user.current_organization if user else None
        from_name = (org.name if org else None) or "Knight Shift"
        defaults = _resolve_message_settings(org)

    raw_cc = _coerce_list(data.get("cc"))
    raw_bcc = _coerce_list(data.get("bcc"))
    if raw_cc is None:
        cc_list = list(defaults["cc"])
    else:
        cc_list, cc_errors = _parse_address_list(raw_cc)
        if cc_errors:
            return jsonify({"error": cc_errors[0]}), 400
    if raw_bcc is None:
        bcc_list = list(defaults["bcc"])
    else:
        bcc_list, bcc_errors = _parse_address_list(raw_bcc)
        if bcc_errors:
            return jsonify({"error": bcc_errors[0]}), 400

    # Drop the primary recipient from CC/BCC to avoid sending it twice.
    to_low = to_email.lower()
    cc_list = [a for a in cc_list if a.lower() != to_low]
    bcc_list = [a for a in bcc_list if a.lower() != to_low and a.lower() not in {c.lower() for c in cc_list}]

    if len(cc_list) > MESSAGE_CC_BCC_MAX or len(bcc_list) > MESSAGE_CC_BCC_MAX:
        return jsonify({
            "error": f"Too many CC/BCC recipients (max {MESSAGE_CC_BCC_MAX} each).",
        }), 400

    # Download the PDF to a temp file, then send.
    from jobs import send_email_with_attachments
    with tempfile.TemporaryDirectory(prefix=f"ks-send-{submission_id}-") as workdir:
        local_path = os.path.join(workdir, filename)
        try:
            storage.download_file(r2_key, local_path)
        except Exception as exc:
            logger.error("send-estimate %s: failed to fetch %s: %s",
                         submission_id, r2_key, exc)
            return jsonify({"error": "Could not retrieve the estimate file."}), 500

        try:
            sent = send_email_with_attachments(
                to_email=to_email,
                subject=subject,
                body=body,
                attachment_paths=[local_path],
                from_name=from_name,
                cc=cc_list or None,
                bcc=bcc_list or None,
            )
        except Exception as exc:
            logger.error("send-estimate %s: SMTP error: %s", submission_id, exc, exc_info=True)
            return jsonify({"error": "Failed to send the email. Please try again."}), 502

    if not sent:
        return jsonify({"error": "Email delivery is not configured on the server."}), 503

    logger.info("Estimate for %s sent by user %d to %s (cc=%d, bcc=%d)",
                submission_id, uid, to_email, len(cc_list), len(bcc_list))
    return jsonify({"ok": True, "to": to_email})


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
                flash("Pricing reset to system defaults.", "success")
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

            # Advanced (toggleable) items — only persist a rate if the item
            # is enabled, so a disabled item resets to the system default.
            advanced_enabled = []
            for key, _label, _unit, _default, _cat in ADVANCED_RATE_FIELDS:
                if not request.form.get(f"enable__{key}"):
                    continue
                advanced_enabled.append(key)
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
            if advanced_enabled:
                overrides["advanced_enabled"] = advanced_enabled
            org.pricing_overrides = overrides or None
            flash("Pricing saved.", "success")
            return redirect(url_for("pricing_settings"))

        overrides = org.pricing_overrides or {}

    return render_template(
        "pricing.html",
        overrides={
            "markup": overrides.get("markup"),
            "rates": overrides.get("rates", {}),
            "advanced_enabled": set(overrides.get("advanced_enabled") or []),
        },
        rate_fields=RATE_FIELDS,
        advanced_rate_fields=ADVANCED_RATE_FIELDS,
    )


@app.route("/messages", methods=["GET", "POST"])
@require_auth
def message_settings():
    """View / edit per-org defaults for the Send-Estimate email.

    Pairs with the modal on the Completed jobs tab — that modal pre-fills
    the subject + body from these templates, and the API merges in the
    saved CC/BCC lists if the request doesn't override them.

    Org-scoped, same shape as /pricing — any member of the org sees the
    same saved settings.
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
                org.message_settings = None
                flash("Message settings reset to system defaults.", "success")
                return redirect(url_for("message_settings"))

            subject_tmpl = (request.form.get("subject_template") or "").strip()
            body_tmpl = (request.form.get("body_template") or "").strip()
            cc_raw = request.form.get("cc") or ""
            bcc_raw = request.form.get("bcc") or ""

            errors = []
            if subject_tmpl and len(subject_tmpl) > MESSAGE_SUBJECT_MAX:
                errors.append(
                    f"Subject template is too long "
                    f"(max {MESSAGE_SUBJECT_MAX} characters)."
                )
            if body_tmpl and len(body_tmpl) > MESSAGE_BODY_MAX:
                errors.append(
                    f"Body template is too long "
                    f"(max {MESSAGE_BODY_MAX} characters)."
                )

            cc_list, cc_errors = _parse_address_list(cc_raw)
            bcc_list, bcc_errors = _parse_address_list(bcc_raw)
            errors.extend(cc_errors)
            errors.extend(bcc_errors)
            if len(cc_list) > MESSAGE_CC_BCC_MAX:
                errors.append(
                    f"Too many CC addresses (max {MESSAGE_CC_BCC_MAX})."
                )
            if len(bcc_list) > MESSAGE_CC_BCC_MAX:
                errors.append(
                    f"Too many BCC addresses (max {MESSAGE_CC_BCC_MAX})."
                )

            if errors:
                for e in errors:
                    flash(e, "error")
                return redirect(url_for("message_settings"))

            saved = {}
            # Persist only fields that differ from the system default, so a
            # user clearing a field reverts to the built-in template.
            if subject_tmpl and subject_tmpl != DEFAULT_SEND_ESTIMATE_SUBJECT:
                saved["subject_template"] = subject_tmpl
            if body_tmpl and body_tmpl != DEFAULT_SEND_ESTIMATE_BODY:
                saved["body_template"] = body_tmpl
            if cc_list:
                saved["cc"] = cc_list
            if bcc_list:
                saved["bcc"] = bcc_list
            org.message_settings = saved or None
            flash("Message settings saved.", "success")
            return redirect(url_for("message_settings"))

        defaults = _resolve_message_settings(org)

    return render_template(
        "messages.html",
        settings=defaults,
        cc_text="\n".join(defaults["cc"]),
        bcc_text="\n".join(defaults["bcc"]),
        default_subject=DEFAULT_SEND_ESTIMATE_SUBJECT,
        default_body=DEFAULT_SEND_ESTIMATE_BODY,
        subject_max=MESSAGE_SUBJECT_MAX,
        body_max=MESSAGE_BODY_MAX,
        cc_bcc_max=MESSAGE_CC_BCC_MAX,
    )


@app.route("/api/messages/defaults", methods=["GET"])
@require_auth
def message_defaults_api():
    """Return the caller's org message defaults — used by the Completed
    tab modal to pre-fill subject/body and seed the CC/BCC inputs."""
    uid = current_user_id()
    with session_scope() as session:
        user = session.get(User, uid)
        org = user.current_organization if user else None
        defaults = _resolve_message_settings(org)
    return jsonify({
        "subject_template": defaults["subject_template"],
        "body_template": defaults["body_template"],
        "cc": defaults["cc"],
        "bcc": defaults["bcc"],
    })


# ---------------------------------------------------------------------------
# Usage / ROI
# ---------------------------------------------------------------------------

# Industry-average seeds for the "savings" calc:
#   $36/hr  — BLS "Cost Estimators" median ≈ $75K/yr ÷ 2080 hrs.
#   8 hrs   — typical commercial-painting takeoff per the PCA references
#             baked into config.py.
USAGE_DEFAULT_HOURLY_WAGE = 36.0
USAGE_DEFAULT_HOURS_PER_ESTIMATE = 8.0


@app.route("/usage", methods=["GET", "POST"])
@require_auth
def usage_dashboard():
    """Org-scoped usage and ROI metrics.

    GET  — aggregate completed submissions for the current org and render
           cards plus a small form for the savings inputs.
    POST — persist hourly_wage + hours_per_estimate on the org.
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
                org.usage_settings = None
                flash("Savings inputs reset to industry averages.", "success")
                return redirect(url_for("usage_dashboard"))

            errors = []
            settings = {}
            for key, lo, hi in (
                ("hourly_wage", 0, 1000),
                ("hours_per_estimate", 0, 500),
            ):
                raw = (request.form.get(key) or "").strip()
                if not raw:
                    continue
                try:
                    v = float(raw)
                    if v < lo or v > hi:
                        raise ValueError("out of range")
                    settings[key] = v
                except ValueError:
                    errors.append(f"{key}: must be between {lo} and {hi}")

            if errors:
                for e in errors:
                    flash(e, "error")
                return redirect(url_for("usage_dashboard"))

            org.usage_settings = settings or None
            flash("Savings inputs saved.", "success")
            return redirect(url_for("usage_dashboard"))

        # Aggregate completed submissions for this org. Failed/cancelled
        # jobs don't count toward "jobs run" because the value isn't real.
        completed = (session.query(Submission)
                     .filter(Submission.org_id == org.id,
                             Submission.status == "completed")
                     .all())

        total_jobs = len(completed)
        total_value = sum(float(s.subtotal or 0) for s in completed)
        avg_value = (total_value / total_jobs) if total_jobs else 0.0

        runtimes_sec = []
        for s in completed:
            if s.submitted_at and s.updated_at:
                delta = (s.updated_at - s.submitted_at).total_seconds()
                if delta > 0:
                    runtimes_sec.append(delta)
        avg_runtime_sec = (sum(runtimes_sec) / len(runtimes_sec)) if runtimes_sec else 0.0

        us = org.usage_settings or {}
        hourly_wage = float(us.get("hourly_wage", USAGE_DEFAULT_HOURLY_WAGE))
        hours_per_estimate = float(us.get(
            "hours_per_estimate", USAGE_DEFAULT_HOURS_PER_ESTIMATE,
        ))
        savings = total_jobs * hourly_wage * hours_per_estimate

        # Whether the user has saved inputs (controls "industry default" hint).
        wage_saved = "hourly_wage" in us
        hours_saved = "hours_per_estimate" in us

    return render_template(
        "usage.html",
        metrics={
            "total_jobs": total_jobs,
            "total_value": total_value,
            "avg_value": avg_value,
            "avg_runtime_sec": avg_runtime_sec,
            "savings": savings,
        },
        savings_inputs={
            "hourly_wage": hourly_wage,
            "hours_per_estimate": hours_per_estimate,
            "wage_saved": wage_saved,
            "hours_saved": hours_saved,
            "default_hourly_wage": USAGE_DEFAULT_HOURLY_WAGE,
            "default_hours_per_estimate": USAGE_DEFAULT_HOURS_PER_ESTIMATE,
        },
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

            # Branding fields surfaced on the Estimate PDF. Empty string →
            # NULL so the template can fall back to defaults cleanly.
            # Keys: form field → (model attr, max length)
            branding_fields = {
                "logo_url":       ("logo_url",       1024),
                "street_address": ("street_address",  255),
                "city":           ("city",            128),
                "state":          ("state",            64),
                "postal_code":    ("postal_code",      32),
                "phone":          ("phone",            64),
                "contact_email":  ("contact_email",   320),
                "website":        ("website",         255),
                "tax_id":         ("tax_id",           64),
            }
            for form_key, (attr, maxlen) in branding_fields.items():
                raw = (request.form.get(form_key) or "").strip()
                if len(raw) > maxlen:
                    flash(f"{form_key.replace('_',' ').title()} is too long (max {maxlen}).",
                          "error")
                    return redirect(url_for("organization"))
                setattr(org, attr, raw or None)

            flash("Organization updated.", "success")
            return redirect(url_for("organization"))

        member_count = (session.query(OrganizationMembership)
                               .filter(OrganizationMembership.organization_id == org.id)
                               .count())
        owner_count = (session.query(OrganizationMembership)
                              .filter(OrganizationMembership.organization_id == org.id,
                                      OrganizationMembership.role == "owner")
                              .count())

        # If the owner has uploaded a logo, surface a fresh presigned URL
        # for the in-page preview. Presigned URLs expire (R2_SIGNED_URL_EXPIRY),
        # but this one only lives for the duration of the page view — the
        # estimate PDF generator fetches bytes directly via logo_r2_key.
        uploaded_logo_preview = None
        if org.logo_r2_key:
            try:
                uploaded_logo_preview = storage.presigned_download_url(org.logo_r2_key)
            except Exception as exc:
                logger.warning("Could not presign org %d logo preview: %s", org.id, exc)

        return render_template(
            "organization.html",
            org=org,
            my_role=my_role,
            is_owner=is_owner,
            member_count=member_count,
            owner_count=owner_count,
            daily_cap_effective=org.daily_submission_cap or BETA_DAILY_SUBMISSION_CAP_DEFAULT,
            uploaded_logo_preview=uploaded_logo_preview,
        )


# Image MIME types the logo upload accepts. SVG is excluded — embedded
# <script> in an SVG would render in browser previews of the image URL.
# PDF generation goes through WeasyPrint which is safe, but the same bytes
# are also surfaced as a presigned URL for the Org Settings preview.
_LOGO_ALLOWED_MIMES = {
    "image/png":  "png",
    "image/jpeg": "jpg",
    "image/gif":  "gif",
    "image/webp": "webp",
}
_LOGO_MAX_BYTES = 5 * 1024 * 1024  # 5 MB — generous for a logo, stops abuse


@app.route("/api/org/logo", methods=["POST"])
@require_auth
def org_logo_upload():
    """Upload an org logo image. Owner-only.

    Accepts multipart/form-data with a single 'file' field. Streams the
    bytes to R2 under orgs/<org_id>/logo.<ext>, updates the org row, and
    returns a presigned URL the browser can use to display the uploaded
    image in the settings page preview.
    """
    uid = current_user_id()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "No file provided."}), 400

    # Browser-supplied content type — first cheap filter. We still cap by
    # byte count below in case a malicious client claims image/png on a
    # giant binary.
    mime = (upload.mimetype or "").lower()
    if mime not in _LOGO_ALLOWED_MIMES:
        return jsonify({"error": "Logo must be a PNG, JPEG, GIF, or WebP image."}), 415

    blob = upload.read(_LOGO_MAX_BYTES + 1)
    if len(blob) == 0:
        return jsonify({"error": "Uploaded file is empty."}), 400
    if len(blob) > _LOGO_MAX_BYTES:
        return jsonify({"error": "Logo file is too large (max 5 MB)."}), 413

    ext = _LOGO_ALLOWED_MIMES[mime]

    with session_scope() as session:
        user = session.get(User, uid)
        org = user.current_organization if user else None
        if org is None:
            return jsonify({"error": "no organization"}), 400
        if not _is_owner(session, uid, org.id):
            return jsonify({"error": "Only org owners can change the logo."}), 403

        key = storage.org_logo_key(org.id, ext)
        try:
            storage.put_bytes(blob, key, content_type=mime)
        except Exception as exc:
            logger.error("Logo upload failed for org %d: %s", org.id, exc, exc_info=True)
            return jsonify({"error": "Could not save the logo to storage."}), 502

        # Setting logo_r2_key tells the PDF generator to prefer the upload
        # over logo_url. We deliberately don't null out logo_url here so
        # the user can still see the Clerk fallback if they later "remove"
        # the upload (a future feature).
        org.logo_r2_key = key
        org_id = org.id

    try:
        preview_url = storage.presigned_download_url(key)
    except Exception as exc:
        logger.warning("Logo upload OK but presign failed for org %d: %s", org_id, exc)
        preview_url = None

    logger.info("Org %d logo uploaded (%d bytes, %s)", org_id, len(blob), mime)
    return jsonify({"ok": True, "preview_url": preview_url})


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

    notify_payload = None
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

        inviter_name = user.name or ""
        inviter_email = user.email or ""
        org_name = org.name

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

        notify_payload = {
            "email": raw_email,
            "role": role,
            "org_name": org_name,
            "inviter_name": inviter_name,
            "inviter_email": inviter_email,
        }

    # Send notification AFTER commit so a Resend hiccup doesn't roll back the
    # membership. Mirrors the pattern in /onboarding.
    email_sent = False
    if notify_payload:
        app_url = url_for("index", _external=True)
        try:
            email_sent = notify_user_of_org_invite(
                notify_payload["email"],
                notify_payload["org_name"],
                notify_payload["role"],
                notify_payload["inviter_name"],
                notify_payload["inviter_email"],
                app_url,
            )
        except Exception as exc:
            logger.error("Invite notification to %s failed: %s",
                         notify_payload["email"], exc)

    if email_sent:
        flash(f"Invited {raw_email} — notification email sent.", "success")
    elif notifications_configured():
        flash(
            f"Invited {raw_email}. Email notification failed to send — "
            "they can still join by signing in with that address.",
            "success",
        )
    else:
        flash(
            f"Invited {raw_email}. They'll join automatically when they sign in. "
            "(Email notifications are not configured.)",
            "success",
        )
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
            if org is not None and org.denied_at is not None:
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

        if org.denied_at is not None:
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

        # Pending = applied (approval_requested_at NOT NULL), not approved,
        # and not denied.
        pending_q = (session.query(Organization)
                            .filter(Organization.is_beta_approved.is_(False))
                            .filter(Organization.approval_requested_at.isnot(None))
                            .filter(Organization.denied_at.is_(None))
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


@app.route("/admin/orgs/<int:org_id>/deny", methods=["POST"])
@require_auth
def admin_orgs_deny(org_id):
    """Mark an org's access request as denied and email the owners."""
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
            flash(f"{org.name} is already approved — cannot deny.", "error")
            return redirect(url_for("admin_orgs"))

        if org.denied_at is not None:
            flash(f"{org.name} was already denied.", "success")
            return redirect(url_for("admin_orgs"))

        org.denied_at = datetime.now(timezone.utc)

        org_name = org.name
        for m in org.memberships:
            if m.role == "owner" and m.user and m.user.email:
                notify.append((m.user.email, m.user.name or ""))

    for email, name in notify:
        try:
            notify_user_of_denial(email, name, org_name)
        except Exception as exc:
            logger.error("Denial notification to %s failed: %s", email, exc)

    flash(f"Denied {org_name}.", "success")
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
