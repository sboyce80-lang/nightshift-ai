#!/usr/bin/env python3
"""
Knight Shift — Formal Estimate PDF Generator
============================================
Third deliverable that the worker produces alongside the full job PDF and JSON.
The Estimate is a parsed-down, contractor-branded document suitable for sharing
with stakeholders for approval — modeled on the Rider Painting estimate format
(see reference samples in the project root).

Public entry point:
    generate_estimate_pdf(submission, organization, result, out_dir) -> str

Returns the absolute path to the written PDF.

Rendering is HTML/CSS → PDF via WeasyPrint. The HTML lives inline in this
module (kept as a single self-contained file so the worker doesn't need a
Jinja loader for one template).
"""

import base64
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

from jinja2 import Template


logger = logging.getLogger("nightshift.estimate")


# Boilerplate "Important Notes & Exclusions" — printed on the last page of every
# estimate. Generic enough to suit any painting/construction contractor; orgs
# that want custom language can append per-org overrides later (out of scope
# for v1).
DEFAULT_BOILERPLATE = [
    "A late fee of 1.5% will be applied to any unpaid balance remaining 30 days after the invoice date.",
    "Pricing is based on the use of standard contractor-grade products, pending approved submittals.",
    "All labor and materials necessary to complete the scope of work described above are included.",
    "Any alterations or deviations from the above scope that incur additional costs will only be executed upon written approval of a revised estimate or signed change order.",
    "Ceilings will be finished in flat paint only, unless the surface has been prepared to a Level 5 finish.",
    "Should an existing fireproof coating be present on the exposed ceiling or structural elements, the volume of paint required for full coverage may vary. Pricing may be subject to adjustment if a substantial quantity of material is necessary to adequately conceal the existing coating.",
]


_HTML_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Estimate #{{ estimate_number }} — {{ org.name }}</title>
<style>
    @page {
        size: letter;
        margin: 0.5in;
        @bottom-center {
            content: "Page " counter(page) " of " counter(pages);
            font-family: Helvetica, Arial, sans-serif;
            font-size: 9pt;
            color: #888;
        }
    }
    body {
        font-family: Helvetica, Arial, sans-serif;
        font-size: 11pt;
        line-height: 1.4;
        color: #111;
        margin: 0;
    }
    h1.title {
        text-align: center;
        font-weight: 400;
        font-size: 20pt;
        color: #888;
        letter-spacing: 6px;
        margin: 0 0 18px 0;
    }
    .header {
        display: flex;
        justify-content: space-between;
        gap: 24px;
        margin-bottom: 28px;
    }
    .header .left { width: 48%; }
    .header .right { width: 48%; text-align: right; }
    .logo {
        max-height: 80px;
        max-width: 220px;
        margin-bottom: 10px;
    }
    .org-name { font-weight: bold; font-size: 12.5pt; margin-top: 14px; }
    .org-line { margin: 2px 0; font-size: 10pt; }
    .prepared-for {
        font-weight: bold;
        font-size: 11pt;
        margin-bottom: 4px;
    }
    .estimate-meta {
        margin-top: 18px;
        font-size: 10.5pt;
    }
    .estimate-meta-row {
        display: flex;
        justify-content: flex-end;
        gap: 24px;
    }
    .estimate-meta-row .label { color: #444; }
    .estimate-meta-row .value { min-width: 120px; text-align: right; }
    table.items {
        width: 100%;
        border-collapse: collapse;
        margin: 8px 0 20px 0;
    }
    table.items thead th {
        border-top: 1px solid #000;
        border-bottom: 1px solid #000;
        text-align: left;
        font-weight: bold;
        font-size: 11pt;
        padding: 8px 0;
    }
    table.items thead th.num { text-align: right; }
    table.items td {
        padding: 6px 0 4px 0;
        vertical-align: top;
    }
    table.items td.num { text-align: right; white-space: nowrap; }
    .item-row { border-top: 1px solid #eee; }
    .item-row td { padding-top: 12px; }
    .item-row.first { border-top: none; }
    .item-title { font-size: 11pt; }
    .item-scope {
        font-size: 10pt;
        color: #222;
        margin-top: 4px;
        white-space: pre-wrap;
    }
    .totals {
        margin-top: 18px;
        margin-left: 55%;
        border-top: 1px solid #000;
    }
    .totals-row {
        display: flex;
        justify-content: space-between;
        padding: 6px 0;
    }
    .totals-row.subtotal { border-bottom: 1px solid #ccc; }
    .totals-row.grand { font-weight: bold; font-size: 12pt; }
    .review-banner {
        border: 2px solid #000;
        background: #f2f2f2;
        padding: 10px 14px;
        margin: 0 0 16px 0;
    }
    .review-banner .rb-title {
        font-weight: bold;
        font-size: 12pt;
        letter-spacing: 0.5px;
    }
    .review-banner .rb-body {
        font-size: 10pt;
        margin-top: 5px;
    }
    .review-banner ul {
        margin: 6px 0 0 0;
        padding-left: 18px;
        font-size: 10pt;
    }
    .open-items {
        margin-top: 22px;
    }
    .open-items h2 {
        font-size: 12pt;
        margin: 0 0 10px 0;
    }
    .open-items ul {
        margin: 0 0 14px 0;
        padding-left: 18px;
        font-size: 10.5pt;
    }
    .open-items li { margin-bottom: 7px; }
    .open-items .oi-sub {
        font-size: 11pt;
        font-weight: bold;
        margin: 0 0 6px 0;
    }
    .notes-page {
        page-break-before: always;
    }
    .notes-page h2 {
        font-size: 12pt;
        margin: 0 0 12px 0;
    }
    .notes-page ul {
        margin: 0;
        padding-left: 18px;
        font-size: 10.5pt;
    }
    .notes-page li {
        margin-bottom: 8px;
    }
</style>
</head>
<body>
    <h1 class="title">ESTIMATE</h1>

    {% if review.needs_review %}
    <div class="review-banner">
        <div class="rb-title">DRAFT — INTERNAL REVIEW REQUIRED, NOT FOR SUBMISSION</div>
        <div class="rb-body">This estimate was generated from an incomplete or
        low-confidence takeoff and has <strong>not</strong> been cleared for
        release to an owner or general contractor. Resolve the open items on the
        last page before issuing a bid.</div>
        {% if review.reasons %}
        <ul>
        {% for reason in review.reasons %}
            <li>{{ reason }}</li>
        {% endfor %}
        </ul>
        {% endif %}
    </div>
    {% endif %}

    <div class="header">
        <div class="left">
            {% if logo_src %}
            <img class="logo" src="{{ logo_src }}" alt="{{ org.name }}">
            {% endif %}
            <div class="org-name">{{ org.name }}</div>
            {% if org.street_address %}<div class="org-line">{{ org.street_address }}</div>{% endif %}
            {% if org_city_line %}<div class="org-line">{{ org_city_line }}</div>{% endif %}
            {% if org.phone %}<div class="org-line">Phone: {{ org.phone }}</div>{% endif %}
            {% if org.contact_email %}<div class="org-line">Email: {{ org.contact_email }}</div>{% endif %}
            {% if org.website %}<div class="org-line">Web: {{ org.website }}</div>{% endif %}
        </div>
        <div class="right">
            <div class="prepared-for">Prepared For</div>
            {% if client_name %}<div class="org-line">{{ client_name }}</div>{% endif %}
            {% if client_address %}<div class="org-line">{{ client_address }}</div>{% endif %}
            {% if client_phone %}<div class="org-line">{{ client_phone }}</div>{% endif %}

            <div class="estimate-meta">
                <div class="estimate-meta-row">
                    <span class="label">Estimate #</span>
                    <span class="value">{{ estimate_number }}</span>
                </div>
                <div class="estimate-meta-row">
                    <span class="label">Date</span>
                    <span class="value">{{ estimate_date }}</span>
                </div>
                {% if org.tax_id %}
                <div class="estimate-meta-row">
                    <span class="label">Business / Tax #</span>
                    <span class="value">{{ org.tax_id }}</span>
                </div>
                {% endif %}
            </div>
        </div>
    </div>

    <table class="items">
        <thead>
            <tr>
                <th>Description</th>
                <th class="num">Total</th>
            </tr>
        </thead>
        <tbody>
            {% for item in line_items %}
            <tr class="item-row{% if loop.first %} first{% endif %}">
                <td>
                    <div class="item-title">{{ item.title }}</div>
                    {% if item.scope %}<div class="item-scope">{{ item.scope }}</div>{% endif %}
                </td>
                <td class="num">${{ "{:,.2f}".format(item.total) }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <div class="totals">
        <div class="totals-row subtotal">
            <span>Subtotal</span>
            <span>${{ "{:,.2f}".format(subtotal) }}</span>
        </div>
        <div class="totals-row grand">
            <span>Total</span>
            <span>${{ "{:,.2f}".format(subtotal) }}</span>
        </div>
    </div>

    {% if open_items.excluded or open_items.unresolved %}
    <div class="open-items">
        <h2>Scope Not Included &amp; Open Items</h2>
        {% if open_items.excluded %}
        <div class="oi-sub">Excluded from this price</div>
        <ul>
        {% for item in open_items.excluded %}
            <li>{{ item }}</li>
        {% endfor %}
        </ul>
        {% endif %}
        {% if open_items.unresolved %}
        <div class="oi-sub">Information required before this price is firm</div>
        <ul>
        {% for item in open_items.unresolved %}
            <li>{{ item }}</li>
        {% endfor %}
        </ul>
        {% endif %}
    </div>
    {% endif %}

    <div class="notes-page">
        <h2>Important Notes &amp; Exclusions</h2>
        <ul>
        {% for note in boilerplate %}
            <li>{{ note }}</li>
        {% endfor %}
        </ul>
    </div>
</body>
</html>
""")


def _resolve_logo_src(organization) -> Optional[str]:
    """Return the value the HTML template should set as the logo <img src>.

    Preference order:
        1. R2-hosted upload (logo_r2_key): fetch bytes and inline as a
           data: URI. Keeps the PDF self-contained — no expiring presigned
           URLs, no external dependencies at render time.
        2. External URL (logo_url): pass straight through (Clerk CDN or
           a user-pasted public URL). WeasyPrint fetches it during render.
        3. None: skip the <img> tag.

    Failures during R2 fetch fall back to logo_url so an outage on the
    storage backend never breaks PDF generation entirely.
    """
    r2_key = getattr(organization, "logo_r2_key", None)
    if r2_key:
        try:
            # Local import — keeps generate_estimate_pdf importable in
            # environments without R2 credentials (e.g. unit tests).
            import storage  # type: ignore
            data = storage.get_bytes(r2_key)
            mime, _ = mimetypes.guess_type(r2_key)
            if not mime:
                # Fall back to PNG — works for any standard browser/PDF
                # renderer even if the extension was lost.
                mime = "image/png"
            return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        except Exception as exc:
            logger.warning("Could not inline R2 logo %s, falling back to logo_url: %s",
                           r2_key, exc)
    return getattr(organization, "logo_url", None) or None


def _slugify(text: str) -> str:
    """lowercase, alnum-only, collapsed; used for the output filename prefix."""
    s = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    return s or "estimate"


def _estimate_number_for(submission_id: str) -> str:
    """Deterministic 4-digit estimate number derived from the submission UUID.

    Stable across regenerations so the same job always carries the same number,
    and roughly evenly distributed so collisions inside one org are unlikely
    without needing a cross-row sequence. Range 3000–9999 mirrors the Rider
    reference samples (3590, 3592, 3593) visually.
    """
    hex_chunk = re.sub(r"[^0-9a-f]", "", submission_id.lower())[:8] or "0"
    return str(3000 + (int(hex_chunk, 16) % 7000))


def _city_line(city: Optional[str], state: Optional[str], postal: Optional[str]) -> str:
    parts = []
    if city:
        parts.append(city.strip())
    if state:
        parts.append(state.strip())
    line = ", ".join(parts)
    if postal:
        line = f"{line} {postal.strip()}".strip()
    return line


def _build_line_items(result: dict) -> List[dict]:
    """Collapse the analysis cost_estimate into the few rows the Estimate prints.

    The full job PDF/JSON enumerate every priced row (often 15–25 line items
    once exterior, doors, stairs, specialty finishes are split out). The
    Estimate is a stakeholder-facing document — too much detail dilutes the
    pricing decision. We group by category and keep titles human-readable.

    Each output row: {title, scope, total}.
        title  — short human label ("Interior painting — walls & ceilings")
        scope  — multiline bullet text under the title
        total  — dollar amount (cost+markup) summed across grouped rows
    """
    costs = result.get("cost_estimate", {}) or {}
    items = costs.get("line_items", []) or []

    # Coarse buckets keyed by substring match against the cost line label.
    # Order matters — first match wins, so the most specific buckets are
    # listed first. (Wallcovering would otherwise fall into Interior because
    # the label contains "wall"; Exterior Window Trim would otherwise fall
    # into Trim, etc.)
    buckets = [
        # NOTE: bare "railing" must NOT live in the Exterior bucket — the
        # "Painted Railings" line is interior stair handrail (Harlem Valley
        # 2026-08-12: an interior handrail printed under "Exterior surfaces
        # power-washed…" on an interior-only bid). Exterior railing lines
        # are labeled "Ext. Stain Railing" and still match "ext.".
        # Power washing prints as its own row — the allowance line (JW-class
        # exterior scope, 2026-08) is often a five-figure amount and matched
        # no bucket, so it fell into the bare "Additional scope" row: a $38k
        # unexplained charge on Hudson Hotel (the Biddle specialty-line
        # failure class). Listed before Exterior so wash-only scope isn't
        # described as "finished with two coats".
        ("Power washing",
         ["power wash", "pressure wash"],
         "Exterior surfaces power-washed per the plan requirements. Priced as an allowance; remove if carried by others."),
        ("Exterior",
         ["exterior", "ext.", "hardie", "azek", "cornice", "siding", "lintel"],
         "Exterior surfaces power-washed, scraped, spot-primed, caulked, and finished with two coats."),
        ("Specialty coatings",
         ["cmu", "dryfall", "concrete", "lyme wash", "lymewash", "plaster",
          "column", "wallcovering", "stained wood", "level 5", "lift rental"],
         "Specialty surface preparation and coating per manufacturer requirements."),
        ("Stairs",
         ["stair", "railing", "handrail"],
         "Risers, railings, and adjacent stair walls prepared and finished as part of the painted-stair scope."),
        # Scope text is rebuilt below from the lines that actually matched.
        # 2026-09-01 (Profeta / 168 Holley St): every door on the job was
        # PRE-FIN so 0 doors were priced, yet this row printed "Includes
        # baseboards, casings, doors, and frames as scheduled" over a
        # base-trim-only $1,324.05 — a contractual offer to paint doors for
        # free.
        ("Trim, doors, and windows",
         ["trim", "door", "window", "hm panel", "frame", "cabinet"],
         "Caulked, filled, sanded, and finished with two coats."),
        ("Interior painting — walls & ceilings",
         ["wall", "ceiling", "soffit"],
         "Surfaces prepared with standard renovation prep (patching, sanding, caulking) and finished with primer plus two coats."),
    ]

    grouped = {title: {"title": title, "scope": scope, "total": 0.0}
               for title, _kw, scope in buckets}  # type: dict
    misc = {"title": "Additional scope", "scope": "", "total": 0.0}
    misc_labels = []

    specialty_labels = []
    trim_labels = []
    for li in items:
        qty = float(li.get("qty") or 0)
        total = float(li.get("total") or 0)
        if qty <= 0 or total <= 0:
            continue
        label = str(li.get("item") or "").lower()
        matched = False
        for title, kws, _scope in buckets:
            if any(kw in label for kw in kws):
                grouped[title]["total"] += total
                if title == "Specialty coatings":
                    specialty_labels.append(label)
                elif title == "Trim, doors, and windows":
                    trim_labels.append(label)
                matched = True
                break
        if not matched:
            misc["total"] += total
            misc_labels.append(str(li.get("item") or "").strip())

    # Name only the trim components that were actually priced. Listing
    # "doors, and frames" on a row that priced zero doors reads as an
    # all-inclusive trim offer.
    if trim_labels:
        present = []
        if any("base" in l or "crown" in l or "trim" in l for l in trim_labels):
            present.append("baseboards and casings")
        if any("door" in l or "hm panel" in l or "frame" in l for l in trim_labels):
            present.append("doors and frames")
        if any("window" in l for l in trim_labels):
            present.append("window trim")
        if any("cabinet" in l for l in trim_labels):
            present.append("cabinetry")
        if present:
            listed = present[0] if len(present) == 1 else (
                ", ".join(present[:-1]) + " and " + present[-1])
            grouped["Trim, doors, and windows"]["scope"] = (
                "Caulked, filled, sanded, and finished with two coats. "
                f"Includes {listed} as scheduled.")
    # When the Specialty bucket holds ONLY stained-wood lines, say so — a
    # generic "Specialty coatings" row the customer can't tie to any plan
    # scope reads as an invented charge (Rider feedback on Biddle,
    # 2026-07-21: the $311 stained-wood line surfaced as an unexplained
    # 'Specialty coatings' item). Gated with the stained-wood hard-numbers
    # gate so the rollout is a single flag.
    if os.environ.get("NIGHTSHIFT_STAINED_WOOD_GATE", "0").strip() in (
            "1", "true", "True"):
        if specialty_labels and all(
                "stained wood" in l for l in specialty_labels):
            grouped["Specialty coatings"]["title"] = \
                "Stained & clear-coat woodwork"
            grouped["Specialty coatings"]["scope"] = (
                "Wood surfaces stained/clear-coated per the finish "
                "schedule.")

    # Print order: Interior first (most familiar), then trim/stairs, then
    # specialty + exterior, then any uncategorized leftovers. Buckets with
    # zero total are dropped from the estimate.
    display_order = [
        "Interior painting — walls & ceilings",
        "Trim, doors, and windows",
        "Stairs",
        "Specialty coatings",
        "Exterior",
        "Power washing",
    ]
    out = [grouped[title] for title in display_order if grouped[title]["total"] > 0]
    if misc["total"] > 0:
        # Never print a bare unexplained amount (the Biddle failure class) —
        # list the underlying cost lines as the scope text.
        misc["scope"] = "\n".join(misc_labels)
        out.append(misc)
    return out


def _review_gate_enabled() -> bool:
    """Kill switch for the review gate. Default ON — an estimate that the
    pipeline itself refused to clear must never render as a clean bid."""
    return os.environ.get("NIGHTSHIFT_ESTIMATE_REVIEW_GATE", "1").strip() not in (
        "0", "false", "False")


def _review_state(result: dict) -> dict:
    """Decide whether this estimate may be presented as a finished bid.

    2026-09-01 (Profeta / 168 Holley St, the first PLG self-serve job): the
    pipeline set ready_to_send=false, route_to_human_review=true,
    manual_review_required=true and calibrated confidence 24 (+/-54%) — four
    plan pages had failed extraction and the whole exterior scope priced at
    $0. The branded estimate still rendered as a clean, caveat-free
    $34,139.02 bid. The estimator had no signal in the document itself that
    it was not ready to send.

    Reads the routing decisions the pipeline already made; it never makes a
    new judgement of its own.
    """
    reasons: List[str] = []

    analysis = result.get("analysis", {}) or {}
    will = result.get("will_synthesis", {}) or {}
    flags = will.get("pipeline_flags", {}) or {}

    # Will's gate (and confidence.reconcile_will_confidence, which can only
    # tighten it). ready_to_send is only meaningful when Will actually ran —
    # an absent will_synthesis must not be read as "not ready".
    if flags:
        if flags.get("ready_to_send") is False:
            reasons.append("The takeoff was not cleared for release "
                           "(ready_to_send = false).")
        if flags.get("route_to_human_review"):
            reasons.append("Routed to human review before the price is issued.")
        for override in (flags.get("ready_to_send_overrides") or []):
            reasons.append(str(override))

    # Coverage / scope-missing gate.
    if result.get("manual_review_required") or analysis.get("manual_review_required"):
        why = (result.get("manual_review_reason")
               or analysis.get("manual_review_reason") or "").strip()
        reasons.append(f"Manual review flag: {why}" if why else "Manual review flag set.")

    # Calibrated band — printed whenever it is wide enough to change a bid.
    cal = analysis.get("calibrated_confidence", {}) or {}
    err = cal.get("predicted_error_pct")
    level = cal.get("confidence_level")
    if isinstance(err, (int, float)) and err >= 15:
        reasons.append(f"Predicted accuracy is only within +/-{err:.0f}% at 90% "
                       f"confidence (calibrated confidence {level}).")

    # De-duplicate while preserving order.
    seen = set()
    deduped = [r for r in reasons if not (r in seen or seen.add(r))]
    return {"needs_review": bool(deduped), "reasons": deduped}


# Exclusions carrying these sources are job-specific findings (Will's review,
# the hard-numbers gates). "standard" rows are the generic trade boilerplate
# already covered by DEFAULT_BOILERPLATE and are not reprinted here.
_BOILERPLATE_EXCLUSION_SOURCES = {"standard", ""}


def _open_items(result: dict) -> dict:
    """Job-specific exclusions and unresolved questions for the estimate.

    The full job PDF carries all of these; the branded estimate carried none
    of them. A bid that names no exclusions is read as all-inclusive, so the
    job-specific rows travel with the price.

    Returns {"excluded": [str], "unresolved": [str]}.
    """
    excluded: List[str] = []
    unresolved: List[str] = []

    costs = result.get("cost_estimate", {}) or {}
    for exc in (costs.get("exclusions") or []):
        if not isinstance(exc, dict):
            continue
        source = str(exc.get("source") or "").strip().lower()
        if source in _BOILERPLATE_EXCLUSION_SOURCES:
            continue
        item = str(exc.get("item") or "").strip()
        reason = str(exc.get("reason") or "").strip()
        if not item:
            continue
        excluded.append(f"{item} — {reason}" if reason else item)

    # High-severity validation warnings are priced-at-zero scope: the quantity
    # exists on the drawings but the hard-numbers policy refused to invent it.
    for warn in ((result.get("validation", {}) or {}).get("warnings") or []):
        if not isinstance(warn, dict):
            continue
        if str(warn.get("severity") or "").lower() != "high" and not warn.get("policy_zero"):
            continue
        msg = str(warn.get("message") or "").strip()
        if msg:
            unresolved.append(msg)

    will = result.get("will_synthesis", {}) or {}
    for missing in ((will.get("pipeline_flags", {}) or {}).get("missing_information") or []):
        text = str(missing or "").strip()
        if text:
            unresolved.append(text)

    def _dedupe(rows):
        seen = set()
        return [r for r in rows if not (r in seen or seen.add(r))]

    return {"excluded": _dedupe(excluded), "unresolved": _dedupe(unresolved)}


def _client_block(submission, result: dict) -> Tuple[str, str, str]:
    """Best-effort 'Prepared For' fields from the submission + analysis JSON."""
    analysis = result.get("analysis", {}) or {}
    project = analysis.get("project_info", {}) or {}

    name = (getattr(submission, "business_name", None) or "").strip()
    phone = (getattr(submission, "phone", None) or "").strip()
    # The analysis may have captured a project address; if not, the scope notes
    # are often the cleanest single line for the "Prepared For" block.
    address = (project.get("project_address")
               or project.get("address")
               or (getattr(submission, "scope_notes", None) or "")).strip()
    # Trim multi-line scope_notes to the first non-empty line so the header
    # stays compact.
    address = next((ln.strip() for ln in address.splitlines() if ln.strip()), "")
    return name, address, phone


def _result_subtotal(result: dict) -> float:
    return float((result.get("cost_estimate", {}) or {}).get("subtotal", 0) or 0)


def estimate_filename(org_name: str, estimate_number: str) -> str:
    """Public so the worker and the UI can agree on the filename suffix."""
    return f"{_slugify(org_name)}_estimate_{estimate_number}.pdf"


def is_estimate_filename(filename: str) -> bool:
    """Filename convention used to distinguish the estimate from the full job PDF/JSON."""
    return bool(filename) and filename.lower().endswith(".pdf") and "_estimate_" in filename.lower()


def generate_estimate_pdf(submission, organization, result: dict, out_dir: str,
                           boilerplate: Optional[Iterable[str]] = None) -> str:
    """Render the formal Estimate PDF for one completed submission.

    Args:
        submission:   the Submission ORM row (or any object exposing
                      .id, .business_name, .phone, .scope_notes).
        organization: the Organization ORM row that owns this submission.
        result:       run_analysis() return dict (analysis + cost_estimate).
        out_dir:      a writable directory; the PDF is written under it.
        boilerplate:  optional iterable of strings overriding DEFAULT_BOILERPLATE.

    Returns:
        Absolute path to the written PDF.
    """
    # Local import — WeasyPrint pulls in cairo/pango shared libs at import
    # time and we don't want to pay that cost on every worker module load
    # if no estimate is being generated this cycle.
    from weasyprint import HTML

    estimate_number = _estimate_number_for(submission.id)
    today = datetime.now(timezone.utc).astimezone().strftime("%m/%d/%Y")

    client_name, client_address, client_phone = _client_block(submission, result)

    # Routing state the pipeline already decided, surfaced in the document
    # itself rather than only in the full job PDF the estimator may not open.
    review = _review_state(result) if _review_gate_enabled() else {
        "needs_review": False, "reasons": []}
    open_items = _open_items(result) if _review_gate_enabled() else {
        "excluded": [], "unresolved": []}
    if review["needs_review"]:
        logger.warning(
            "Estimate %s rendered as DRAFT (review required): %s",
            submission.id, "; ".join(review["reasons"]))

    html_str = _HTML_TEMPLATE.render(
        org=organization,
        logo_src=_resolve_logo_src(organization),
        org_city_line=_city_line(organization.city, organization.state, organization.postal_code),
        client_name=client_name,
        client_address=client_address,
        client_phone=client_phone,
        estimate_number=estimate_number,
        estimate_date=today,
        line_items=_build_line_items(result),
        subtotal=_result_subtotal(result),
        review=review,
        open_items=open_items,
        boilerplate=list(boilerplate) if boilerplate is not None else DEFAULT_BOILERPLATE,
    )

    filename = estimate_filename(organization.name, estimate_number)
    out_path = os.path.join(out_dir, filename)

    HTML(string=html_str).write_pdf(out_path)
    logger.info("Wrote estimate PDF %s (submission=%s, org=%s, total=$%.2f)",
                out_path, submission.id, organization.name, _result_subtotal(result))
    return out_path
