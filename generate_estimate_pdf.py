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

import logging
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

    <div class="header">
        <div class="left">
            {% if org.logo_url %}
            <img class="logo" src="{{ org.logo_url }}" alt="{{ org.name }}">
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
        ("Exterior",
         ["exterior", "ext.", "hardie", "azek", "cornice", "siding", "railing", "lintel"],
         "Exterior surfaces power-washed, scraped, spot-primed, caulked, and finished with two coats."),
        ("Specialty coatings",
         ["cmu", "dryfall", "concrete", "lyme wash", "lymewash", "plaster",
          "column", "wallcovering", "stained wood", "level 5", "lift rental"],
         "Specialty surface preparation and coating per manufacturer requirements."),
        ("Stairs",
         ["stair"],
         "Risers and adjacent stair walls prepared and finished as part of the painted-stair scope."),
        ("Trim, doors, and windows",
         ["trim", "door", "window", "hm panel", "frame"],
         "Caulked, filled, sanded, and finished with two coats. Includes baseboards, casings, doors, and frames as scheduled."),
        ("Interior painting — walls & ceilings",
         ["wall", "ceiling", "soffit"],
         "Surfaces prepared with standard renovation prep (patching, sanding, caulking) and finished with primer plus two coats."),
    ]

    grouped = {title: {"title": title, "scope": scope, "total": 0.0}
               for title, _kw, scope in buckets}  # type: dict
    misc = {"title": "Additional scope", "scope": "", "total": 0.0}

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
                matched = True
                break
        if not matched:
            misc["total"] += total

    # Print order: Interior first (most familiar), then trim/stairs, then
    # specialty + exterior, then any uncategorized leftovers. Buckets with
    # zero total are dropped from the estimate.
    display_order = [
        "Interior painting — walls & ceilings",
        "Trim, doors, and windows",
        "Stairs",
        "Specialty coatings",
        "Exterior",
    ]
    out = [grouped[title] for title in display_order if grouped[title]["total"] > 0]
    if misc["total"] > 0:
        out.append(misc)
    return out


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

    html_str = _HTML_TEMPLATE.render(
        org=organization,
        org_city_line=_city_line(organization.city, organization.state, organization.postal_code),
        client_name=client_name,
        client_address=client_address,
        client_phone=client_phone,
        estimate_number=estimate_number,
        estimate_date=today,
        line_items=_build_line_items(result),
        subtotal=_result_subtotal(result),
        boilerplate=list(boilerplate) if boilerplate is not None else DEFAULT_BOILERPLATE,
    )

    filename = estimate_filename(organization.name, estimate_number)
    out_path = os.path.join(out_dir, filename)

    HTML(string=html_str).write_pdf(out_path)
    logger.info("Wrote estimate PDF %s (submission=%s, org=%s, total=$%.2f)",
                out_path, submission.id, organization.name, _result_subtotal(result))
    return out_path
