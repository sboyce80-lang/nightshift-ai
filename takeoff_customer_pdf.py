#!/usr/bin/env python3
"""
Knight Shift — Customer-Facing Takeoff PDF
==========================================
Part of the full review package (2026-09-05 product decision): every
estimate delivery carries (1) this takeoff, (2) the branded Estimate PDF,
(3) the annotated drawings, and (4) a "Please confirm scope" section.

The internal analysis PDF (json_to_pdf.py) speaks reviewer dialect —
manual-review reasons, gate records, "Do NOT send this proposal" alarms.
None of that may reach a customer. This renderer builds the document from
an explicit whitelist of measurement fields (category totals + per-room
tables) and then, following the 2026-09-02 white-label lesson (a delivery
went out with 11 competitor-brand references that lived in the JSON, not
the layout), scans the *rendered content* for internal-voice markers and
competitor names before any PDF is written. A violation raises — the
delivery goes out without the takeoff rather than with a tainted one.

Public entry points:
    generate_customer_takeoff_pdf(submission, organization, result, out_dir)
        -> absolute path to the written PDF (WeasyPrint, org-branded the
           same way generate_estimate_pdf is).

    build_customer_takeoff_html(submission, organization, result) -> str
        The full HTML document (separated so offline tests can assert on
        content without WeasyPrint's native libraries).

    build_scope_confirm_items(result) -> [dict]
        The "Please confirm scope" rows — allowance lines, customer-facing
        RFIs, and policy-uncertain (priced-at-zero) scope. Shared with the
        delivery email body in jobs.py. Framing per Steven: we provide the
        numbers; the customer decides what goes into the final bid.
"""

import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from jinja2 import Template

from generate_estimate_pdf import (
    _city_line,
    _estimate_number_for,
    _resolve_logo_src,
    _slugify,
)

logger = logging.getLogger("nightshift.takeoff_customer")


# ---------------------------------------------------------------------------
# Internal-voice scrub
# ---------------------------------------------------------------------------

# Reviewer-dialect phrases that must never appear in a customer document.
# Matched case-insensitively against the rendered HTML. Deliberately
# specific — these are strings the pipeline itself writes into result JSON
# ("Do NOT send this proposal...", manual_review reasons, routing flags),
# not words a drawing might legitimately contain.
INTERNAL_VOICE_MARKERS = (
    "do not send",
    "not for submission",
    "manual review",
    "manual_review",
    "ready_to_send",
    "route_to_human",
    "needs_review",
    "pipeline_flags",
    "human review",
    "senior reviewer",
    "low-confidence takeoff",
)


def customer_safety_violations(text: str,
                               org_name: Optional[str] = None) -> List[str]:
    """Scan rendered customer-facing content for internal-voice markers and
    competitor names. Returns the list of markers found (empty = safe).

    Competitor names come from will_synthesis._KNOWN_CONTRACTOR_NAMES —
    the same target list the Will-output scrub uses. A name matching the
    document's own org is not a violation (Rider's own takeoff may say
    Rider).
    """
    low = (text or "").lower()
    hits = [m for m in INTERNAL_VOICE_MARKERS if m in low]

    try:
        from will_synthesis import _KNOWN_CONTRACTOR_NAMES
    except Exception:  # pragma: no cover — will_synthesis always importable
        _KNOWN_CONTRACTOR_NAMES = ()
    own = (org_name or "").strip().lower()
    for name in _KNOWN_CONTRACTOR_NAMES:
        nl = name.lower()
        if own and (nl in own or own in nl):
            continue  # the org's own name is not a leak
        if nl in low:
            hits.append(name)
    return hits


# ---------------------------------------------------------------------------
# "Please confirm scope"
# ---------------------------------------------------------------------------

SCOPE_CONFIRM_FRAMING = (
    "We provided the numbers below — you decide what goes into the final "
    "bid. Allowances are built to be struck; open questions are not priced "
    "until you confirm them."
)


def _fmt_qty(qty) -> str:
    try:
        q = float(qty or 0)
    except (TypeError, ValueError):
        return "-"
    if q <= 0:
        return "-"
    return f"{q:,.0f}" if q == int(q) else f"{q:,.2f}"


def build_scope_confirm_items(result: dict) -> List[dict]:
    """Collect the scope items the customer should confirm before bidding.

    Sources, in print order:
      (a) allowance lines — cost_estimate.line_items whose label carries
          "ALLOWANCE" (the labeling convention across Takeoff_DIRECT).
          These are strikeable by design.
      (b) customer-facing RFI items (result["rfi_items"]) — already
          written as questions to the customer; only the `question` field
          is used (action_required speaks estimator dialect).
      (c) policy-uncertain lines — validation warnings the hard-numbers
          policy priced at zero (policy_zero, or severity high). Scope
          that exists on the drawings but is NOT in the price.

    Each row: {"kind", "item", "qty", "amount", "question"} where amount
    is a float (0.0 for unpriced scope) and qty is display text.
    """
    items: List[dict] = []
    seen_questions = set()

    costs = result.get("cost_estimate", {}) or {}
    for li in (costs.get("line_items") or []):
        if not isinstance(li, dict):
            continue
        label = str(li.get("item") or "")
        if "allowance" not in label.lower():
            continue
        total = float(li.get("total") or 0)
        if total <= 0:
            continue
        items.append({
            "kind": "Allowance",
            "item": label,
            "qty": _fmt_qty(li.get("qty")),
            "amount": total,
            "question": ("Included in the price as a strikeable allowance — "
                         "keep it in your final bid, or strike it?"),
        })

    rfis = (result.get("rfi_items")
            or (result.get("analysis", {}) or {}).get("rfi_items") or [])
    for rfi in rfis:
        if not isinstance(rfi, dict):
            continue
        question = str(rfi.get("question") or "").strip()
        if not question or question in seen_questions:
            continue
        seen_questions.add(question)
        items.append({
            "kind": str(rfi.get("category") or "Open question"),
            "item": str(rfi.get("category") or "Open question"),
            "qty": "-",
            "amount": 0.0,
            "question": question,
        })

    validation = result.get("validation", {}) or {}
    for warn in (validation.get("warnings") or []):
        if not isinstance(warn, dict):
            continue
        is_policy = bool(warn.get("policy_zero"))
        is_high = str(warn.get("severity") or "").lower() == "high"
        if not (is_policy or is_high):
            continue
        msg = str(warn.get("message") or "").strip()
        if not msg or msg in seen_questions:
            continue
        seen_questions.add(msg)
        items.append({
            "kind": "Not in the price",
            "item": "Unpriced scope",
            "qty": "-",
            "amount": 0.0,
            "question": f"{msg} Should this be added to the final bid?",
        })

    return items


# ---------------------------------------------------------------------------
# HTML template (autoescaped — room names come straight off drawings)
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Takeoff #{{ takeoff_number }} — {{ org.name }}</title>
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
        font-size: 10.5pt;
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
    .header { display: flex; justify-content: space-between; gap: 24px; margin-bottom: 24px; }
    .header .left { width: 48%; }
    .header .right { width: 48%; text-align: right; }
    .logo { max-height: 80px; max-width: 220px; margin-bottom: 10px; }
    .org-name { font-weight: bold; font-size: 12.5pt; margin-top: 14px; }
    .org-line { margin: 2px 0; font-size: 10pt; }
    .prepared-for { font-weight: bold; font-size: 11pt; margin-bottom: 4px; }
    .meta { margin-top: 18px; font-size: 10pt; }
    .meta-row { display: flex; justify-content: flex-end; gap: 24px; }
    .meta-row .label { color: #444; }
    .meta-row .value { min-width: 120px; text-align: right; }
    h2.section {
        font-size: 12pt;
        border-bottom: 1.5px solid #0a2540;
        color: #0a2540;
        padding-bottom: 3px;
        margin: 20px 0 8px 0;
    }
    h3.floor { font-size: 10.5pt; margin: 12px 0 4px 0; color: #333; }
    table.grid { width: 100%; border-collapse: collapse; font-size: 8.5pt; }
    table.grid th {
        background: #0a2540; color: #fff; text-align: left;
        padding: 4px 6px; font-weight: bold;
    }
    table.grid td { padding: 3px 6px; border-bottom: 0.5px solid #ddd; }
    table.grid tr:nth-child(even) td { background: #f5f7fa; }
    table.grid .num { text-align: right; }
    table.grid th.num { text-align: right; }
    .intro { font-size: 10pt; color: #333; margin: 0 0 10px 0; }
    .confirm-framing {
        background: #fdf6e3; border: 1px solid #b58900;
        padding: 8px 12px; font-size: 10pt; margin-bottom: 10px;
    }
    .confirm-note { font-size: 9pt; color: #555; margin-top: 6px; }
    .scope-confirm { page-break-inside: avoid; }
</style>
</head>
<body>
    <h1 class="title">TAKEOFF</h1>

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
        </div>
        <div class="right">
            <div class="prepared-for">Prepared For</div>
            {% if client_name %}<div class="org-line">{{ client_name }}</div>{% endif %}
            {% if project_name %}<div class="org-line">{{ project_name }}</div>{% endif %}
            {% if project_location %}<div class="org-line">{{ project_location }}</div>{% endif %}
            <div class="meta">
                <div class="meta-row">
                    <span class="label">Takeoff #</span>
                    <span class="value">{{ takeoff_number }}</span>
                </div>
                <div class="meta-row">
                    <span class="label">Date</span>
                    <span class="value">{{ takeoff_date }}</span>
                </div>
            </div>
        </div>
    </div>

    <p class="intro">Quantities measured from your drawings. Every number
    below is traceable to a sheet in the annotated plan set delivered with
    this takeoff.</p>

    <h2 class="section">Measured Quantities by Category</h2>
    <table class="grid">
        <thead>
            <tr><th>Category</th><th class="num">Quantity</th><th>Unit</th></tr>
        </thead>
        <tbody>
        {% for row in category_rows %}
            <tr>
                <td>{{ row.label }}</td>
                <td class="num">{{ row.qty }}</td>
                <td>{{ row.unit }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>

    {% if scope_items %}
    <div class="scope-confirm">
    <h2 class="section">Please Confirm Scope</h2>
    <div class="confirm-framing">{{ framing }}</div>
    <table class="grid">
        <thead>
            <tr><th>Item</th><th class="num">Qty</th><th class="num">Amount</th><th>Please confirm</th></tr>
        </thead>
        <tbody>
        {% for it in scope_items %}
            <tr>
                <td>{{ it.item }}</td>
                <td class="num">{{ it.qty }}</td>
                <td class="num">{% if it.amount and it.amount > 0 %}${{ "{:,.2f}".format(it.amount) }}{% else %}$0.00 — not priced{% endif %}</td>
                <td>{{ it.question }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
    <div class="confirm-note">Answer any of the above from your job page
    (Rerun with Revisions) or by replying to the delivery email, and the
    takeoff will be re-issued with your confirmations applied.</div>
    </div>
    {% endif %}

    {% for floor in floors %}
    {% if loop.first %}<h2 class="section">Room-by-Room Measurements</h2>{% endif %}
    <h3 class="floor">{{ floor.name }} ({{ floor.room_count }} room{% if floor.room_count != 1 %}s{% endif %})</h3>
    <table class="grid">
        <thead>
            <tr>
                <th>Room</th><th>Dimensions</th>
                <th class="num">Walls (SF)</th><th class="num">Ceiling (SF)</th>
                <th class="num">Base Trim (LF)</th><th class="num">Doors</th>
                <th class="num">Windows</th><th class="num">Count</th>
            </tr>
        </thead>
        <tbody>
        {% for room in floor.rooms %}
            <tr>
                <td>{{ room.name }}</td>
                <td>{{ room.dims }}</td>
                <td class="num">{{ room.wall_sqft }}</td>
                <td class="num">{{ room.ceiling_sqft }}</td>
                <td class="num">{{ room.trim_lf }}</td>
                <td class="num">{{ room.doors }}</td>
                <td class="num">{{ room.windows }}</td>
                <td class="num">{{ room.mult }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endfor %}
</body>
</html>
""", autoescape=True)


# Category rows rendered from aggregated_totals. Fixed whitelist — a key
# not on this list does not print, whatever the JSON carries.
_CATEGORY_FIELDS = (
    ("total_paintable_wall_sqft", "Paintable walls", "SF"),
    ("total_paintable_ceiling_sqft", "Paintable ceilings", "SF"),
    ("total_base_trim_lf", "Base trim", "LF"),
    ("total_doors_full_paint", "Doors — full paint (panel + frame)", "EA"),
    ("total_doors_hm_panel", "Doors — hollow-metal panel", "EA"),
    ("total_doors_frame_only", "Doors — frame only", "EA"),
    ("total_windows_painted_interior", "Windows — painted interior", "EA"),
    ("total_stair_sections", "Stair sections", "EA"),
    ("total_painted_railing_lf", "Painted railing", "LF"),
    ("total_wallcovering_sqft", "Wallcovering", "SF"),
    ("total_stained_wood_sqft", "Stained / clear-coat wood", "SF"),
    ("total_painted_cabinet_sqft", "Painted cabinetry", "SF"),
    ("total_soffit_sqft", "Soffits", "SF"),
    ("total_concrete_floor_sqft", "Concrete floor coating", "SF"),
    ("total_painted_columns_ea", "Painted columns", "EA"),
    ("total_level_5_finish_sqft", "Level 5 finish", "SF"),
    ("total_dryfall_ceiling_sqft", "Dryfall ceilings", "SF"),
    ("total_cmu_wall_sqft", "CMU walls", "SF"),
)


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _category_rows(analysis: dict) -> List[dict]:
    agg = analysis.get("aggregated_totals", {}) or {}
    rows = []
    for key, label, unit in _CATEGORY_FIELDS:
        qty = _num(agg.get(key))
        if qty <= 0:
            continue
        rows.append({"label": label, "qty": f"{qty:,.0f}", "unit": unit})
    return rows


def _room_multiplier(room: dict) -> int:
    """Unit multiplier as recorded on the room (template rooms expanded by
    unit mix). Mirrors json_to_pdf's note parsing but reads only the
    structured field — notes are internal-voice territory."""
    try:
        m = int(room.get("unit_multiplier") or 1)
        return m if m >= 1 else 1
    except (TypeError, ValueError):
        return 1


def _floor_tables(analysis: dict) -> List[dict]:
    floors_out = []
    for floor in (analysis.get("floors") or []):
        rooms_out = []
        for room in (floor.get("rooms") or []):
            if not room.get("in_scope", True):
                continue
            dims = room.get("dimensions", {}) or {}
            elems = room.get("elements", {}) or {}
            length = _num(dims.get("length_feet"))
            width = _num(dims.get("width_feet"))
            height = _num(dims.get("ceiling_height_feet"))
            if length > 0 and width > 0 and height > 0:
                dim_str = f"{length:.0f} x {width:.0f} x {height:.0f}"
            elif length > 0 and width > 0:
                dim_str = f"{length:.0f} x {width:.0f}"
            else:
                dim_str = "-"
            doors = (_num(elems.get("doors_full_paint",
                                   elems.get("doors")))
                     + _num(elems.get("doors_hm_panel"))
                     + _num(elems.get("doors_frame_only")))
            windows = _num(elems.get("windows_painted_interior",
                                     elems.get("windows")))
            mult = _room_multiplier(room)
            rooms_out.append({
                "name": str(room.get("room_name")
                            or room.get("room_id") or "-"),
                "dims": dim_str,
                "wall_sqft": f"{_num(dims.get('wall_area_sqft')):,.0f}",
                "ceiling_sqft": f"{_num(dims.get('ceiling_area_sqft')):,.0f}",
                "trim_lf": f"{_num(elems.get('base_trim_lf')):,.0f}",
                "doors": f"{doors:,.0f}" if doors > 0 else "-",
                "windows": f"{windows:,.0f}" if windows > 0 else "-",
                "mult": f"x{mult}" if mult > 1 else "1",
            })
        if rooms_out:
            floors_out.append({
                "name": str(floor.get("floor_name") or "Floor"),
                "room_count": len(rooms_out),
                "rooms": rooms_out,
            })
    return floors_out


def build_customer_takeoff_html(submission, organization, result: dict) -> str:
    """Assemble the customer takeoff HTML from whitelisted fields only."""
    analysis = result.get("analysis", {}) or {}
    project = analysis.get("project_info", {}) or {}
    takeoff_number = _estimate_number_for(submission.id)
    today = datetime.now(timezone.utc).astimezone().strftime("%m/%d/%Y")

    return _HTML_TEMPLATE.render(
        org=organization,
        logo_src=_resolve_logo_src(organization),
        org_city_line=_city_line(
            organization.city, organization.state, organization.postal_code),
        client_name=(getattr(submission, "business_name", None) or "").strip(),
        project_name=str(project.get("project_name") or "").strip(),
        project_location=str(project.get("location") or "").strip(),
        takeoff_number=takeoff_number,
        takeoff_date=today,
        category_rows=_category_rows(analysis),
        scope_items=build_scope_confirm_items(result),
        framing=SCOPE_CONFIRM_FRAMING,
        floors=_floor_tables(analysis),
    )


def takeoff_filename(org_name: str, takeoff_number: str) -> str:
    """Filename convention, parallel to estimate_filename."""
    return f"{_slugify(org_name)}_takeoff_{takeoff_number}.pdf"


def is_takeoff_filename(filename: str) -> bool:
    return bool(filename) and filename.lower().endswith(".pdf") \
        and "_takeoff_" in filename.lower()


def generate_customer_takeoff_pdf(submission, organization, result: dict,
                                  out_dir: str) -> str:
    """Render the customer takeoff PDF for one completed submission.

    Raises ValueError if the rendered content carries internal-voice
    markers or a competitor name — the caller treats that as "no takeoff
    attaches", never as "attach it anyway".
    """
    html_str = build_customer_takeoff_html(submission, organization, result)

    violations = customer_safety_violations(
        html_str, org_name=getattr(organization, "name", "") or "")
    if violations:
        raise ValueError(
            "customer takeoff failed the internal-voice/competitor scrub: "
            + ", ".join(repr(v) for v in violations))

    # Local import — WeasyPrint drags in cairo/pango at import time.
    from weasyprint import HTML

    filename = takeoff_filename(
        organization.name, _estimate_number_for(submission.id))
    out_path = os.path.join(out_dir, filename)
    HTML(string=html_str).write_pdf(out_path)
    logger.info("Wrote customer takeoff PDF %s (submission=%s, org=%s)",
                out_path, submission.id, organization.name)
    return out_path
