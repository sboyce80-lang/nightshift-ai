#!/usr/bin/env python3
"""
Will Synthesis Module — Senior Estimator Review Layer for Nightshift AI
========================================================================

Takes the completed analysis + cost_estimate from run_analysis() and runs
a final Claude API call as "Will," a Senior Estimator persona, to:

  1. Review the estimate and propose bounded adjustments to line items
  2. Generate a GC-level scope of work narrative
  3. Generate a Joist-style shorthand scope
  4. Produce a confidence percentage and bid recommendation
  5. Identify top risks and items to confirm before bid
  6. Assess prevailing wage applicability
  7. Add Will-specific RFIs that the Python pipeline missed

GUARDRAILS:
  - Will can adjust any line item by at most ±25% from the calculated value.
  - Adjustments outside the ±25% band become RFIs instead of silent overrides.
  - Every adjustment is logged in adjustments_log with from/to/reason.
  - Will cannot modify scope protection rules (ACT exclusions, factory-finished, etc.) —
    those are hard-coded in the Python pipeline.
"""

import os
import json
import re
import anthropic
from datetime import datetime

try:
    from config import CLAUDE_API_KEY
except ImportError:
    CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")

try:
    from config import HARD_NUMBERS_ONLY
except ImportError:
    HARD_NUMBERS_ONLY = True


# ---------------------------------------------------------------------------
# Guardrail Constants
# ---------------------------------------------------------------------------

# Maximum percentage Will can adjust any single line item, up or down.
# Adjustments beyond this become RFIs instead of overrides.
MAX_ADJUSTMENT_PCT = 0.25

# Line items Will is NEVER allowed to touch — these are scope protection rules
# baked into the Python pipeline and Will should not override them.
PROTECTED_CATEGORIES = {
    # Future expansion — currently empty. Add category names here if you find
    # Will trying to override hard-coded scope rules.
}

# Categories whose DOWNWARD adjustments are blocked when the upstream sanity
# check has flagged the takeoff as "implausibly low / missing scope". Reducing
# these would compound the miss instead of recovering it.
SCOPE_RECOVERY_CATEGORIES = {
    "Gyp. Walls",
    "Gyp. Ceilings",
    "CMU Walls",
    "Dryfall Ceiling",
    "Doors (Full Paint)",
    "Doors (HM Panel)",
    "Doors (Frame Only)",
    "Base Trim",
    "Concrete Sealer",
    "Exterior Painting",
    "Hardie Siding",
    "Exterior Cornice",
}

# Reason-substring markers that indicate the manual-review flag fired because
# scope was UNDERCOUNTED (not over-extracted). When any of these appear in
# `analysis.manual_review_reason`, downward adjustments to SCOPE_RECOVERY_CATEGORIES
# are hard-rejected.
LOW_SCOPE_REASON_MARKERS = (
    "implausibly low",
    "below expected",
    "missing scope",
    "missing finish schedule",
    "exposed structure",
    "paint-to-deck",
    "exterior was missed",
    "expected 3-6",
    "ratio is",
)

# Categories Will IS allowed to adjust (line item names from cost_estimate)
ADJUSTABLE_CATEGORIES = {
    "Gyp. Walls",
    "Gyp. Ceilings",
    "CMU Walls",
    "Dryfall Ceiling",
    "Base Trim",
    "Doors (Full Paint)",
    "Doors (HM Panel)",
    "Doors (Frame Only)",
    "Windows",
    "Stairs",
    "Gyp. Between Stairs",
    "Level 5 Finish",
    "Concrete Sealer",
    "Painted Columns",
    "Wallcovering",
    "Stained Wood",
    "Interior Soffits",
    "Exterior Cornice",
    "Exterior Window Trim",
    "Exterior Painting",
    "Hardie Siding",
    "Azek Trim",
    "Corner Boards",
    "Steel Lintels",
    "Lift Rental",
    "Stain Siding",
    "Stain Trim",
    "Stain Railing",
}


# ---------------------------------------------------------------------------
# Will's System Prompt
# ---------------------------------------------------------------------------

WILL_SYSTEM_PROMPT = """You are Will, Senior Estimator for Rider Painting, Inc. You are reviewing a completed takeoff and cost estimate that was produced by an automated pipeline. Your job is to do what a senior estimator does at the end of every bid: review the numbers, catch what the pipeline missed, write the scope language, and put your name on a confidence level and bid recommendation.

You are operating inside the Nightshift automated proposal pipeline. There is no human in the loop on this turn — your output goes directly to the proposal document and email reply. Write accordingly: be decisive, structured, and never ask clarifying questions back.

## Tone

Professional GC-level. Practical. Construction-focused. Direct. The way a seasoned estimator talks to a project manager. No filler, no apologies, no AI hedging language ("I'd be happy to," "as an AI," "please let me know"). The objective is to **win profitable work for Rider Painting while avoiding hidden scope.**

## Your authority — and its limits

You have **bounded edit authority** over the cost estimate's line items. You can adjust any single line item by **at most ±25%** from the calculated value when you have a defensible reason. If you believe a number is wrong by more than 25%, **do not override it** — flag it as an RFI instead. The pipeline tracks every adjustment you make.

You CANNOT:
- Modify scope protection rules (ACT exclusions, factory-finished items, etc.) — those are non-negotiable and already enforced
- Add line items that don't exist in the original estimate (you can only adjust existing ones)
- Adjust totals directly — only line items, and the totals will be recomputed

You CAN and SHOULD:
- Trim or boost line items by up to ±25% with a clear written reason
- Flag anything beyond ±25% as an RFI
- Identify line items that look suspicious based on building type, project context, or proportions
- Write the proposal-ready scope of work and Joist shorthand
- Set confidence and bid recommendation

## Required output format

Return a single JSON object and nothing else. No preamble, no markdown fences, no commentary outside the JSON. The pipeline parses this directly. Use this exact schema:

```
{
  "project_type": "residential" | "commercial" | "unknown",
  "prevailing_wage": {
    "applies": true | false | "unknown",
    "county": string | null,
    "wage_schedule_basis": string | null,
    "notes": string
  },
  "adjustments": [
    {
      "category": string,
      "from_value": number,
      "to_value": number,
      "from_total": number,
      "to_total": number,
      "reason": string,
      "confidence": number
    }
  ],
  "rejected_adjustments": [
    {
      "category": string,
      "current_value": number,
      "suggested_value": number,
      "pct_change": number,
      "reason_for_rejection": "exceeds_25_percent_band" | "protected_category",
      "converted_to_rfi": true
    }
  ],
  "additional_rfis": [
    {
      "category": "Missing Drawings" | "Incomplete Dimensions" | "Missing Schedules" | "Material Specifications" | "Clarification Needed" | "Scope Conflict" | "Pricing Concern",
      "question": string,
      "action_required": string,
      "severity": "high" | "medium" | "low"
    }
  ],
  "additional_exclusions": [
    {
      "category": string,
      "item": string,
      "reason": string
    }
  ],
  "gc_scope_of_work": string,
  "joist_shorthand_scope": string,
  "confidence": {
    "level_pct": number,
    "reasoning": string,
    "top_risks": [ string ],
    "items_to_confirm_before_bid": [ string ],
    "bid_recommendation": "aggressive" | "cautious" | "clarifications_only" | "do_not_bid"
  },
  "estimator_recap": string,
  "pipeline_flags": {
    "ready_to_send": true | false,
    "route_to_human_review": true | false,
    "missing_information": [ string ]
  }
}
```

## Field-by-field guidance

**`adjustments`**: Each adjustment must include `from_value` (current quantity, e.g. 2,150 sqft), `to_value` (your adjusted quantity), the resulting `from_total` and `to_total` dollar amounts, a clear `reason`, and your `confidence` (0.0–1.0). Only include adjustments you actually want applied. If you don't want to adjust something, leave it out.

**`rejected_adjustments`**: If you considered adjusting something by more than 25% but had to back off, log it here. The `pct_change` should be the percentage you would have applied (positive or negative). These get auto-converted to RFIs by the pipeline.

**`additional_rfis`**: RFIs the Python pipeline didn't generate. Don't duplicate the existing RFI list — add new ones based on your review. Categorize them and rate severity.

**`additional_exclusions`**: The pipeline already supplies a standard scope-protection exclusions list in the input under `exclusions` (ACT, factory-finished items, cut-in by others, trade damage, hazmat, MEP equipment, etc.). DO NOT duplicate those. Use `additional_exclusions` only to add **project-specific** exclusions you spot during review — for example: a specific room or area called "tenant fit-out, by tenant", a specialty coating named in the spec but not budgeted, an exterior element shown but flagged as future phase, equipment screens, decorative metals, etc. Each entry needs a category, the item, and a one-sentence reason. If you have nothing to add, return an empty array.

**`prevailing_wage`**: The pipeline pre-extracts prevailing-wage indicators into the input as `project_info.prevailing_wage_signal`. Copy `applies` / `county` / `wage_schedule_basis` from that signal into your output unless you have stronger evidence to override. If the signal is `unknown`, you must either (a) raise a high-severity RFI requesting confirmation, or (b) infer from context (e.g., school district owner, NYCHA, public housing) and explain in `notes`. Never silently default to `false`.

**`gc_scope_of_work`**: A 4–8 sentence narrative describing what Rider Painting is bidding to do, written in the voice of a senior estimator. This goes straight into the proposal. Sign it `— Will, Senior Estimator, Rider Painting, Inc.` at the end.

**`joist_shorthand_scope`**: A bulleted, terse scope suitable for a Joist proposal. One item per line, format like "Walls — 12,400 SF GYP, 2 coats, eggshell." Include all confirmed line items.

**`confidence.level_pct`**: An integer 0–100. Reflects your confidence the estimate is defensible at the bid table. Common ranges:
- 90+: Clean takeoff, schedules complete, no major RFIs
- 75–89: Good takeoff with minor RFIs
- 60–74: Material RFIs that affect price; route to human review
- Below 60: Don't auto-send; human review required

**`bid_recommendation`**:
- `aggressive`: Bid tight, this is a winnable job we want
- `cautious`: Bid with margin, several unknowns
- `clarifications_only`: Don't put a number out yet; respond with RFIs first
- `do_not_bid`: Walk away

**`estimator_recap`**: A 2–3 sentence executive summary for the email reply. This is what the GC reads first. Plain English, no jargon.

**`pipeline_flags`**: Same routing logic as the rest of Nightshift. `ready_to_send` true only when confidence ≥ 85, no high-severity RFIs, and no rejected adjustments.

## Which sheets were actually submitted — do not contradict this

The input includes `sheets_processed`: the authoritative list of drawing sheets the pipeline READ and anchored rooms to. **Any sheet_id in that list was in the submitted drawing set.** You must never claim — in `missing_information`, `additional_rfis`, `confidence.reasoning`, `top_risks`, `estimator_recap`, or `gc_scope_of_work` — that one of those sheets was "not submitted", "not included", "missing from the set", or "not in the drawing set". Asking the GC to "provide sheet A401" when A401 is in `sheets_processed` is a factual error that destroys the GC's trust in the whole proposal.

A sheet can be present but *thin*: it may carry room footprints yet show no building section or reflected ceiling plan, so wall heights came back as 0 and walls are undercounted. That is a real problem — but the correct framing is **"sheet A401 was read but does not carry a section/RCP, so wall heights could not be derived from it"**, and the RFI asks for the missing *detail* (a section, an RCP, dimensioned heights), NOT for the sheet. Only describe a sheet as missing if its sheet_id is genuinely absent from `sheets_processed`.

## How to review the estimate

Walk it in this order — same as how you'd review any junior's takeoff:

1. **Project type sanity check** — Does the building type match the line items? A "single-family" project with 80,000 sqft of walls is probably misclassified.

2. **Wall:ceiling ratio** — Should be roughly 3.3x for residential, 1.5–10x for commercial. The pipeline already flags this; you double-check.

3. **Window scope** — Commercial buildings rarely have painted interior windows. Residential typically has wood-frame windows that need trim paint. Watch for over-counts.

4. **Door classification** — HM doors get spray-painted (different rate than full-paint wood). Check that the schedule was read correctly.

5. **Trim and base** — Apartment buildings often spray base inline; some don't carry separate base trim. Single-family always carries base separately.

6. **Exterior** — Check that lift cost matches story count. 1-story = no lift. 2+ stories with exterior scope = lift required.

7. **Cornice/specialty exterior** — In commercial buildings, "cornice" is sometimes EIFS or coping that the LLM misclassified. Trim if it looks wrong.

8. **Specialty finishes** — Wallcovering, stained wood, dryfall. These are price-sensitive ($6–9/sqft). Confirm they came from finish schedule, not LLM guess.

9. **Prevailing wage** — The pipeline already runs a document-level prevailing-wage scan and reports the result in `project_info.prevailing_wage_signal` (fields: `applies` ∈ {yes, no, unknown}, `county`, `wage_schedule_basis`, `indicators`, `source_pages`). Treat that signal as authoritative and copy it into your `prevailing_wage` output. Only override when scope_notes or pipeline_notes contain stronger contradicting evidence — and document the override in `notes`. If `applies == "unknown"`, write a high-severity RFI in `additional_rfis` requesting confirmation; do not silently assume non-PW. If `applies == "yes"` and the cost estimate appears to use standard (non-PW) labor rates, flag a high-severity "Pricing Concern" RFI noting the labor-cost gap.

10. **Round numbers test** — Total feels right? A 2,500 sqft single-family home shouldn't price at $80,000. A 3-story 20-unit building shouldn't price at $35,000.

11. **Manual review flag** — The payload includes a `manual_review_required` boolean and `manual_review_reason` string set by an upstream sanity check. When `manual_review_required == true`, READ THE REASON CAREFULLY before doing anything.
   - If the reason mentions phrases like "implausibly low", "below expected", "missing scope", "missing finish schedule", "exposed structure", "paint-to-deck", or "exterior was missed" — this is a SCOPE-MISSING signal, NOT an over-extraction signal. The upstream extraction undershot. **DO NOT propose downward adjustments to wall, ceiling, CMU, dryfall, or door counts in this case.** Reducing those would compound the miss. Instead:
     - Leave existing line items alone (or propose UPWARD adjustments within ±25% if you have a defensible reason)
     - Surface high-severity RFIs in `additional_rfis` calling out the specific missing scope (finish schedule / Finish Legend, exposed structure / paint-to-deck, exterior, etc.)
     - Set `pipeline_flags.ready_to_send = false` and `route_to_human_review = true`
     - In your `estimator_recap`, explicitly call out that the takeoff is suspected to be undercounted and a human reviewer must verify before the proposal goes out.
   - Only treat the flag as an over-extraction signal if the `manual_review_reason` literally says "over-extracted" or "implausibly high" (it currently never does — the only sanity check that fires writes "implausibly low").

## Critical constraints

- **Output JSON only.** No markdown, no prose preamble, no code fences. The pipeline will fail to parse anything else.
- **Adjustments must stay within ±25%.** Going beyond means it becomes an RFI in `rejected_adjustments`, not an `adjustments` entry.
- **Don't invent line items.** You can only adjust categories that exist in the input estimate.
- **Sign your scope of work.** End `gc_scope_of_work` with `— Will, Senior Estimator, Rider Painting, Inc.`
- **When `manual_review_required == true` with a "low/missing" reason, downward adjustments to scope-recovery categories (Gyp. Walls, Gyp. Ceilings, CMU Walls, Dryfall Ceiling, Doors) are auto-rejected by the pipeline.** Don't waste budget proposing them.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _category_from_item_label(item_label):
    """Extract the canonical category name from a line item label.

    Line items look like: "Gyp. Walls - 12,400 sqft @ $1.25"
    We want to extract: "Gyp. Walls"
    """
    if " - " in item_label:
        return item_label.split(" - ", 1)[0].strip()
    return item_label.strip()


def _detect_pca_under_extraction(analysis):
    """Scan room notes for PCA cross-check flags indicating wall-area
    under-extraction.

    The PCA cross-check (computed in Takeoff_DIRECT.py) annotates room
    notes with strings like:
      "[PCA check: expected 2600 SF, got 1400 SF (46% deviation)]"
    when the extracted wall area falls below the perimeter-derived
    expectation. We surface this signal both in the Will payload and as
    a guardrail so Will doesn't reduce wall/trim quantities that PCA has
    already flagged as under-extracted.

    Returns dict:
      {
        "walls_under_extracted": bool,
        "details": [ {room_name, expected, got, deviation_pct}, ... ],
      }
    """
    details = []
    pca_re = re.compile(
        r"PCA check:\s*expected\s+(\d[\d,]*)\s*SF[^,]*,\s*got\s+(\d[\d,]*)\s*SF\s*\((\d+)%\s*deviation\)",
        re.IGNORECASE,
    )
    for floor in analysis.get("floors", []) or []:
        for room in floor.get("rooms", []) or []:
            if not room.get("in_scope", True):
                continue
            note = room.get("notes") or ""
            if isinstance(note, list):
                note = " ".join(str(x) for x in note)
            elif not isinstance(note, str):
                note = str(note)
            m = pca_re.search(note)
            if not m:
                continue
            expected = int(m.group(1).replace(",", ""))
            got = int(m.group(2).replace(",", ""))
            dev = int(m.group(3))
            if got < expected:  # under-extraction
                details.append({
                    "room_name": room.get("room_name", "?"),
                    "room_id": room.get("room_id", "?"),
                    "expected_wall_sqft": expected,
                    "got_wall_sqft": got,
                    "deviation_pct": dev,
                })
    return {
        "walls_under_extracted": len(details) > 0,
        "details": details,
    }


# Categories whose downward adjustments should be blocked when PCA detects
# wall-area under-extraction. Trim follows perimeter, so it's affected too.
PCA_WALL_LINKED_CATEGORIES = {
    "Gyp. Walls",
    "CMU Walls",
    "Base Trim",
}


def _build_review_payload(analysis, cost_estimate, rfi_items, validation):
    """Build the user-message payload for Will's review.

    Includes the full analysis context Will needs to make informed decisions:
    project info, aggregated totals, line items with current values, existing
    RFIs (so Will doesn't duplicate), and validation warnings.
    """
    pi = analysis.get("project_info", {})
    agg = analysis.get("aggregated_totals", {})
    ext = analysis.get("exterior", {})

    line_items_simplified = []
    for item in cost_estimate.get("line_items", []):
        if _num(item.get("qty", 0)) > 0:
            line_items_simplified.append({
                "category": _category_from_item_label(item["item"]),
                "label": item["item"],
                "qty": item["qty"],
                "cost": item["cost"],
                "markup": item["markup"],
                "total": item["total"],
            })

    # Pre-extracted prevailing-wage signal from the LLM document scan.
    # Will should treat this as the authoritative starting point and only
    # override based on additional evidence in scope_notes / pipeline_notes.
    pw_signal = pi.get("prevailing_wage") if isinstance(pi.get("prevailing_wage"), dict) else {
        "applies": "unknown", "county": None, "wage_schedule_basis": None,
        "indicators": [], "source_pages": []
    }

    pca_signal = _detect_pca_under_extraction(analysis)

    # Authoritative list of sheets the pipeline actually read and anchored rooms
    # to. Without this, Will has only ambiguous room notes to reason from and has
    # hallucinated that a sheet was "not submitted" when it was in fact processed
    # (INNIO Waukesha: Will claimed A401 was missing while the pipeline anchored
    # all 6 of its rooms). Will must NOT contradict this list.
    sheets_processed = []
    for sp in (analysis.get("_sheet_pages") or []):
        if not isinstance(sp, dict):
            continue
        sid = sp.get("sheet_id")
        if not sid:
            continue
        sheets_processed.append({
            "sheet_id": sid,
            "page": sp.get("page"),
            "rooms_anchored": sp.get("rooms", 0),
        })

    payload = {
        # Sheets the pipeline READ and extracted from. A sheet appearing here was
        # in the submitted set — never describe it as missing / not submitted /
        # not included. If such a sheet lacked usable detail (e.g. no section or
        # RCP, so wall heights were 0), say it "was read but lacks <X>", and ask
        # for the missing DETAIL, not for the sheet itself.
        "sheets_processed": sheets_processed,
        "project_info": {
            "building_type": pi.get("building_type", "unknown"),
            "total_stories": pi.get("total_stories", 0),
            "total_units": pi.get("total_units", 0),
            "footprint_sqft": pi.get("footprint_sqft", 0),
            "total_floors_analyzed": pi.get("total_floors_analyzed", 0),
            "total_rooms_found": pi.get("total_rooms_found", 0),
            "project_name": pi.get("project_name", ""),
            "location": pi.get("location", ""),
            "prevailing_wage_signal": pw_signal,
        },
        "aggregated_totals": agg,
        "exterior": ext,
        "line_items": line_items_simplified,
        "subtotal": cost_estimate.get("subtotal", 0),
        "exclusions": cost_estimate.get("exclusions", []),
        "existing_rfis": rfi_items or [],
        "validation_warnings": validation.get("warnings", []) if validation else [],
        "data_quality_score": validation.get("data_quality_score", 0) if validation else 0,
        "scope_notes": pi.get("_scope_notes", "") if isinstance(pi, dict) else "",
        # Manual-review sanity-check signal — read the reason carefully before
        # adjusting. A "low/missing" reason means the takeoff undershot;
        # downward adjustments to scope-recovery categories will be auto-rejected.
        "manual_review_required": bool(analysis.get("manual_review_required")),
        "manual_review_reason": analysis.get("manual_review_reason"),
        # PCA cross-check: flags rooms where extracted wall area is below the
        # perimeter-derived expectation. When walls_under_extracted is true,
        # do NOT propose downward adjustments to Gyp. Walls / CMU Walls /
        # Base Trim — the pipeline guardrail will reject them. Consider
        # upward adjustments instead.
        "pca_cross_check": pca_signal,
        "pipeline_notes": analysis.get("notes", [])[:30],  # cap to avoid overwhelming context
    }
    return payload


def _is_low_scope_manual_review(analysis):
    """True when analysis.manual_review_required is set AND the reason text
    indicates the takeoff is undercounted (not over-extracted)."""
    if not isinstance(analysis, dict):
        return False
    if not analysis.get("manual_review_required"):
        return False
    reason = str(analysis.get("manual_review_reason") or "").lower()
    return any(marker in reason for marker in LOW_SCOPE_REASON_MARKERS)


def _validate_adjustment(adjustment, line_items_by_category, analysis=None):
    """Validate a single proposed adjustment against the guardrails.

    Returns (is_valid, reason). If invalid, the adjustment should be moved
    to rejected_adjustments instead of applied.
    """
    cat = adjustment.get("category", "").strip()

    # Protected category check
    if cat in PROTECTED_CATEGORIES:
        return (False, "protected_category")

    # Must be in the adjustable set OR exist in current line items
    # (Will sometimes uses slightly different naming; we match against both)
    if cat not in ADJUSTABLE_CATEGORIES and cat not in line_items_by_category:
        return (False, "category_not_found")

    # Find current value
    current = line_items_by_category.get(cat)
    if not current:
        # Try fuzzy match
        for known_cat in line_items_by_category:
            if cat.lower() in known_cat.lower() or known_cat.lower() in cat.lower():
                current = line_items_by_category[known_cat]
                break
        if not current:
            return (False, "category_not_found")

    current_qty = _num(current.get("qty", 0))
    proposed_qty = _num(adjustment.get("to_value", 0))

    # Can't adjust what isn't there
    if current_qty == 0:
        return (False, "current_value_is_zero")

    # ±25% guardrail
    pct_change = abs(proposed_qty - current_qty) / current_qty
    if pct_change > MAX_ADJUSTMENT_PCT:
        return (False, "exceeds_25_percent_band")

    # Low-scope manual review: hard-block downward adjustments to
    # scope-recovery categories. The upstream sanity check fired because
    # scope was undercounted; reducing these would compound the miss.
    # See LOW_SCOPE_REASON_MARKERS for the trigger wording.
    if proposed_qty < current_qty and cat in SCOPE_RECOVERY_CATEGORIES \
            and _is_low_scope_manual_review(analysis):
        return (False, "manual_review_low_scope_blocks_downward")

    # PCA cross-check guardrail: reject downward adjustments to wall- and
    # trim-linked categories when the perimeter-derived PCA check has
    # flagged extracted wall area as under-extracted. Will sometimes
    # reasons from a stale or wrong footprint and proposes cuts that move
    # the estimate the wrong direction; this catches that.
    if proposed_qty < current_qty and cat in PCA_WALL_LINKED_CATEGORIES \
            and analysis is not None:
        pca = _detect_pca_under_extraction(analysis)
        if pca.get("walls_under_extracted"):
            return (False, "pca_under_extraction_blocks_downward")

    return (True, "ok")


def _apply_adjustments_to_estimate(cost_estimate, valid_adjustments, line_items_by_category):
    """Apply Will's accepted adjustments to the cost estimate in-place.

    Recalculates line item totals (qty × unit_rate × markup) and the subtotal.
    Returns (modified_estimate, adjustments_log).
    """
    log = []
    line_items = cost_estimate.get("line_items", [])

    # Build label → index map for in-place modification
    by_label = {item["item"]: i for i, item in enumerate(line_items)}
    by_category = {_category_from_item_label(item["item"]): i for i, item in enumerate(line_items)}

    for adj in valid_adjustments:
        cat = adj.get("category", "").strip()
        idx = by_category.get(cat)
        if idx is None:
            # Fuzzy match
            for known_cat, known_idx in by_category.items():
                if cat.lower() in known_cat.lower() or known_cat.lower() in cat.lower():
                    idx = known_idx
                    break
        if idx is None:
            continue

        item = line_items[idx]
        old_qty = _num(item.get("qty", 0))
        new_qty = _num(adj.get("to_value", 0))

        if old_qty == 0:
            continue

        # Recompute proportionally — this preserves the unit rate and markup ratio
        old_cost = _num(item.get("cost", 0))
        old_markup = _num(item.get("markup", 0))
        old_total = _num(item.get("total", 0))

        scale = new_qty / old_qty
        new_cost = round(old_cost * scale, 2)
        new_markup = round(old_markup * scale, 2)
        new_total = round(new_cost + new_markup, 2)

        item["qty"] = new_qty
        item["cost"] = new_cost
        item["markup"] = new_markup
        item["total"] = new_total

        # Update label to reflect new qty
        # Old: "Gyp. Walls - 12,400 sqft @ $1.25"
        # New: "Gyp. Walls - 11,500 sqft @ $1.25 [Will: -7%]"
        if " @ " in item["item"]:
            prefix = item["item"].split(" - ")[0]
            rate_part = item["item"].split(" @ ")[1]
            unit_match = re.search(r"(sqft|LF|EA|each)", item["item"], re.IGNORECASE)
            unit = unit_match.group(1) if unit_match else ""
            pct = ((new_qty - old_qty) / old_qty) * 100
            item["item"] = f"{prefix} - {new_qty:,.0f} {unit} @ {rate_part.split(' ')[0]} [Will: {pct:+.0f}%]"

        log.append({
            "category": cat,
            "from_value": old_qty,
            "to_value": new_qty,
            "from_total": old_total,
            "to_total": new_total,
            "delta_dollars": round(new_total - old_total, 2),
            "reason": adj.get("reason", ""),
            "confidence": adj.get("confidence", 0),
        })

    # Recalculate subtotal
    new_subtotal = round(sum(_num(item.get("total", 0)) for item in line_items), 2)
    cost_estimate["subtotal"] = new_subtotal

    return cost_estimate, log


_MISSING_SHEET_PHRASES = (
    "not submitted",
    "not included",
    "not in the drawing set",
    "not in the set",
    "missing from the set",
    "was not in",
    "wasn't in",
    "not provided in the set",
    "absent from the",
)


def _sanitize_missing_sheet_claims(will_output, analysis):
    """Strip claims that a *processed* sheet was not submitted.

    Will occasionally asserts a sheet is missing when the pipeline actually read
    it (e.g. claiming A401 was "not submitted" while all 6 of its rooms were
    anchored). Those claims, sent to a GC who submitted that exact sheet, destroy
    trust. The prompt instructs Will not to do this; this is the deterministic
    backstop in case it slips anyway.

    A sheet is treated as present iff its sheet_id appears in `_sheet_pages`. For
    each present sheet we drop:
      - `missing_information` entries that name it alongside a missing-phrase
      - `additional_rfis` entries that name it alongside a missing-phrase (these
        are the machine-actionable fields that drive the RFI list and routing)
    Removed items are recorded in will_output["_sanitized_sheet_claims"] so the
    correction is auditable. Free-text prose fields are left untouched (rewriting
    them safely is unreliable); the prompt is the primary guard there.
    """
    processed_ids = {
        str(sp.get("sheet_id")).upper()
        for sp in (analysis.get("_sheet_pages") or [])
        if isinstance(sp, dict) and sp.get("sheet_id")
    }
    if not processed_ids:
        return will_output

    def _names_present_sheet_as_missing(text):
        t = str(text or "")
        tl = t.lower()
        if not any(p in tl for p in _MISSING_SHEET_PHRASES):
            return None
        for sid in processed_ids:
            # word-ish boundary so "A40" doesn't match "A401"
            if re.search(rf"\b{re.escape(sid)}\b", t, re.IGNORECASE):
                return sid
        return None

    removed = []

    flags = will_output.get("pipeline_flags")
    if isinstance(flags, dict) and isinstance(flags.get("missing_information"), list):
        kept = []
        for entry in flags["missing_information"]:
            sid = _names_present_sheet_as_missing(entry)
            if sid:
                removed.append({"field": "missing_information", "sheet_id": sid, "text": entry})
            else:
                kept.append(entry)
        flags["missing_information"] = kept

    if isinstance(will_output.get("additional_rfis"), list):
        kept = []
        for rfi in will_output["additional_rfis"]:
            blob = f"{rfi.get('question', '')} {rfi.get('action_required', '')}" if isinstance(rfi, dict) else str(rfi)
            sid = _names_present_sheet_as_missing(blob)
            if sid:
                removed.append({"field": "additional_rfis", "sheet_id": sid,
                                "text": rfi.get("question", "") if isinstance(rfi, dict) else str(rfi)})
            else:
                kept.append(rfi)
        will_output["additional_rfis"] = kept

    if removed:
        will_output["_sanitized_sheet_claims"] = removed
        sheets = sorted({r["sheet_id"] for r in removed})
        print(f"   🧹 Sanitized {len(removed)} false 'sheet missing' claim(s) "
              f"for processed sheet(s): {', '.join(sheets)}")

    return will_output


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_will_synthesis(analysis, cost_estimate, rfi_items=None, validation=None,
                        client=None, model="claude-sonnet-4-6",
                        use_adaptive_thinking=False, effort="medium"):
    """Run the Will synthesis layer on a completed analysis + cost estimate.

    Args:
        analysis: dict from run_analysis() containing aggregated_totals, floors, etc.
        cost_estimate: dict with line_items and subtotal
        rfi_items: list of existing RFI dicts (so Will doesn't duplicate)
        validation: dict with warnings and data_quality_score
        client: optional anthropic.Anthropic instance (creates one if None)
        model: Claude model to use
        use_adaptive_thinking: enable adaptive thinking for A/B testing whether
            deeper reasoning improves Will's adjustments and RFIs. Off by default
            (existing behavior). Adds latency and tokens.
        effort: thinking depth when use_adaptive_thinking is True
            ("low" | "medium" | "high" | "max"). Default "medium".

    Returns:
        dict with keys:
            will_synthesis     - the parsed JSON output from Will
            cost_estimate      - the (possibly adjusted) cost estimate
            adjustments_log    - list of accepted adjustments with from/to/reason
            rejected_log       - list of rejected adjustments (now RFIs)
            new_rfis           - additional RFIs Will identified
            error              - string error message if synthesis failed, else None
    """
    if not CLAUDE_API_KEY:
        return {
            "will_synthesis": None,
            "cost_estimate": cost_estimate,
            "adjustments_log": [],
            "rejected_log": [],
            "new_rfis": [],
            "error": "CLAUDE_API_KEY not set — Will synthesis skipped",
        }

    if client is None:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    print("\n" + "=" * 80)
    print("👷 WILL SYNTHESIS — Senior Estimator Review")
    print("=" * 80)

    # Build the review payload
    payload = _build_review_payload(analysis, cost_estimate, rfi_items, validation)

    # Build line items lookup for guardrail validation
    line_items_by_category = {}
    for item in cost_estimate.get("line_items", []):
        if _num(item.get("qty", 0)) > 0:
            cat = _category_from_item_label(item["item"])
            line_items_by_category[cat] = item

    # Call Will
    user_message = (
        "Review this completed Rider Painting takeoff and cost estimate. "
        "Apply your senior-estimator judgment, propose any line item adjustments "
        "within the ±25% guardrail, write the GC-level scope of work and Joist "
        "shorthand, and set your confidence and bid recommendation.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    )

    print(f"   📤 Sending estimate to Will for review ({len(payload.get('line_items', []))} line items)...")

    call_kwargs = {
        "model": model,
        "timeout": 180.0,
        "system": WILL_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
    }
    if use_adaptive_thinking:
        # Thinking tokens count toward max_tokens — bump the ceiling so the JSON
        # response isn't truncated mid-thought.
        call_kwargs["max_tokens"] = 16000
        call_kwargs["thinking"] = {"type": "adaptive"}
        call_kwargs["output_config"] = {"effort": effort}
    else:
        call_kwargs["max_tokens"] = 8000
        call_kwargs["temperature"] = 0

    try:
        result_parts = []
        with client.messages.stream(**call_kwargs) as stream:
            for text in stream.text_stream:
                result_parts.append(text)
            final_message = stream.get_final_message()
        raw_response = "".join(result_parts)

        if use_adaptive_thinking:
            thinking_text = next(
                (b.thinking for b in final_message.content if b.type == "thinking"),
                "",
            )
            print(f"   🧠 Will engaged adaptive thinking (effort={effort}): "
                  f"{final_message.usage.output_tokens} output tokens, "
                  f"{len(thinking_text)} chars of thinking summary")
    except anthropic.RateLimitError as e:
        print(f"   ⚠️  Will synthesis rate-limited: {e}")
        return {
            "will_synthesis": None,
            "cost_estimate": cost_estimate,
            "adjustments_log": [],
            "rejected_log": [],
            "new_rfis": [],
            "error": f"Rate limit during Will synthesis: {e}",
        }
    except Exception as e:
        print(f"   ❌ Will synthesis API call failed: {e}")
        return {
            "will_synthesis": None,
            "cost_estimate": cost_estimate,
            "adjustments_log": [],
            "rejected_log": [],
            "new_rfis": [],
            "error": f"Will synthesis failed: {e}",
        }

    # Parse Will's JSON response
    json_match = re.search(r"\{.*\}", raw_response, re.DOTALL)
    if not json_match:
        print(f"   ❌ Will returned non-JSON response: {raw_response[:300]}")
        return {
            "will_synthesis": None,
            "cost_estimate": cost_estimate,
            "adjustments_log": [],
            "rejected_log": [],
            "new_rfis": [],
            "error": "Will returned non-JSON response",
        }

    try:
        will_output = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        print(f"   ❌ Could not parse Will's JSON: {e}")
        return {
            "will_synthesis": None,
            "cost_estimate": cost_estimate,
            "adjustments_log": [],
            "rejected_log": [],
            "new_rfis": [],
            "error": f"Will JSON parse error: {e}",
        }

    # Deterministic backstop: strip any "sheet X not submitted" claim that
    # contradicts the list of sheets the pipeline actually processed.
    will_output = _sanitize_missing_sheet_claims(will_output, analysis)

    # Apply guardrails to proposed adjustments
    proposed_adjustments = will_output.get("adjustments", [])
    valid_adjustments = []
    auto_rejected = []

    for adj in proposed_adjustments:
        if HARD_NUMBERS_ONLY:
            # Hard-numbers-only policy: Will does not silently reshape measured/
            # calculated quantities toward "typical" ranges. Every proposed
            # quantity adjustment is converted to an RFI so the concern is
            # surfaced for human confirmation instead of applied.
            is_valid, reason = False, "hard_numbers_only_policy"
        else:
            is_valid, reason = _validate_adjustment(adj, line_items_by_category, analysis)
        if is_valid:
            valid_adjustments.append(adj)
        else:
            auto_rejected.append({
                "category": adj.get("category", ""),
                "current_value": _num(adj.get("from_value", 0)),
                "suggested_value": _num(adj.get("to_value", 0)),
                "pct_change": (
                    ((_num(adj.get("to_value", 0)) - _num(adj.get("from_value", 0)))
                     / _num(adj.get("from_value", 1))) * 100
                    if _num(adj.get("from_value", 0)) > 0 else 0
                ),
                "reason_for_rejection": reason,
                "original_reason": adj.get("reason", ""),
                "converted_to_rfi": True,
            })

    # Apply valid adjustments to the cost estimate
    cost_estimate, adjustments_log = _apply_adjustments_to_estimate(
        cost_estimate, valid_adjustments, line_items_by_category
    )

    # Convert auto-rejected adjustments into RFIs
    auto_rejected_rfis = []
    for rej in auto_rejected:
        if rej["reason_for_rejection"] == "exceeds_25_percent_band":
            direction = "reduce" if rej["suggested_value"] < rej["current_value"] else "increase"
            auto_rejected_rfis.append({
                "category": "Pricing Concern",
                "question": (
                    f"Will (senior estimator) flagged the {rej['category']} line item: "
                    f"current value {rej['current_value']:,.0f} would be adjusted to "
                    f"{rej['suggested_value']:,.0f} ({rej['pct_change']:+.0f}%). This exceeds the "
                    f"±25% auto-adjust guardrail. Reason: {rej['original_reason']}"
                ),
                "action_required": f"Confirm or override the suggested {direction} for {rej['category']}.",
                "severity": "high",
                "source": "will_guardrail",
            })
        elif rej["reason_for_rejection"] == "hard_numbers_only_policy":
            direction = "reduce" if rej["suggested_value"] < rej["current_value"] else "increase"
            auto_rejected_rfis.append({
                "category": "Pricing Concern",
                "question": (
                    f"Will (senior estimator) reviewed the {rej['category']} line item "
                    f"and suggested adjusting it from {rej['current_value']:,.0f} to "
                    f"{rej['suggested_value']:,.0f} ({rej['pct_change']:+.0f}%). Under the "
                    f"hard-numbers-only policy, quantities are not auto-adjusted toward "
                    f"typical ranges — the measured value is retained and the concern is "
                    f"surfaced for review instead. Reason given: {rej['original_reason']}"
                ),
                "action_required": (
                    f"Confirm whether to manually {direction} {rej['category']}, "
                    f"or keep the measured quantity."
                ),
                "severity": "medium",
                "source": "will_hard_numbers_policy",
            })
        elif rej["reason_for_rejection"] == "manual_review_low_scope_blocks_downward":
            auto_rejected_rfis.append({
                "category": "Scope Conflict",
                "question": (
                    f"Will proposed reducing {rej['category']} from "
                    f"{rej['current_value']:,.0f} to {rej['suggested_value']:,.0f} "
                    f"({rej['pct_change']:+.0f}%), but the upstream sanity check "
                    f"already flagged this takeoff as MISSING SCOPE (paintable surface "
                    f"implausibly low vs footprint). Reducing scope-recovery line items "
                    f"would compound the miss, so the adjustment was auto-blocked. "
                    f"Will's stated reason: {rej['original_reason']}"
                ),
                "action_required": (
                    f"Senior reviewer must verify whether {rej['category']} is truly "
                    f"over-counted (in which case adjust manually) or whether the "
                    f"missing scope flagged elsewhere should be recovered first."
                ),
                "severity": "high",
                "source": "will_guardrail",
            })
        elif rej["reason_for_rejection"] == "pca_under_extraction_blocks_downward":
            auto_rejected_rfis.append({
                "category": "Scope Conflict",
                "question": (
                    f"Will proposed reducing {rej['category']} from "
                    f"{rej['current_value']:,.0f} to {rej['suggested_value']:,.0f} "
                    f"({rej['pct_change']:+.0f}%), but the PCA perimeter cross-check "
                    f"flagged extracted wall area as UNDER-extracted in one or more "
                    f"rooms (got < expected). Reducing wall- or trim-linked line "
                    f"items would move the estimate further in the wrong direction, "
                    f"so the adjustment was auto-blocked. Will's stated reason: "
                    f"{rej['original_reason']}"
                ),
                "action_required": (
                    f"Senior reviewer must reconcile: are walls actually over-counted "
                    f"(in which case adjust manually after re-checking PCA notes), or "
                    f"should the under-extracted rooms be increased to expected "
                    f"perimeter-derived values?"
                ),
                "severity": "high",
                "source": "will_guardrail",
            })

    # Will's additional RFIs
    additional_rfis = will_output.get("additional_rfis", [])
    for rfi in additional_rfis:
        rfi.setdefault("source", "will_synthesis")

    new_rfis = additional_rfis + auto_rejected_rfis

    # Merge Will's project-specific exclusions into the cost estimate's
    # standard exclusions list. Tag source so the proposal can distinguish
    # between standard scope-protection rules and Will's call-outs.
    additional_exclusions = will_output.get("additional_exclusions", []) or []
    if additional_exclusions:
        existing_exclusions = cost_estimate.get("exclusions", []) or []
        for excl in existing_exclusions:
            excl.setdefault("source", "standard")
        for excl in additional_exclusions:
            if not isinstance(excl, dict):
                continue
            excl.setdefault("source", "will_synthesis")
            existing_exclusions.append(excl)
        cost_estimate["exclusions"] = existing_exclusions

    # Print summary
    if adjustments_log:
        print(f"\n   ✅ Will applied {len(adjustments_log)} adjustment(s):")
        for adj in adjustments_log:
            delta = adj["delta_dollars"]
            sign = "+" if delta >= 0 else ""
            print(f"      • {adj['category']}: {adj['from_value']:,.0f} → "
                  f"{adj['to_value']:,.0f} ({sign}${delta:,.0f})")
            print(f"        Reason: {adj['reason'][:100]}")
    else:
        print(f"\n   ✓ Will accepted the estimate as calculated (no adjustments)")

    if auto_rejected:
        print(f"\n   ⚠️  Will proposed {len(auto_rejected)} adjustment(s) outside the ±25% "
              f"band — converted to RFIs:")
        for rej in auto_rejected:
            print(f"      • {rej['category']}: {rej['pct_change']:+.0f}% — "
                  f"{rej['reason_for_rejection']}")

    if new_rfis:
        print(f"\n   📋 Will added {len(new_rfis)} additional RFI(s)")

    confidence = will_output.get("confidence", {})
    if confidence:
        print(f"\n   🎯 Will's confidence: {confidence.get('level_pct', 0)}% — "
              f"recommendation: {confidence.get('bid_recommendation', 'unknown')}")
        recap = will_output.get("estimator_recap", "")
        if recap:
            print(f"\n   📝 Recap: {recap}")

    pipeline_flags = will_output.get("pipeline_flags", {})
    if pipeline_flags:
        ready = pipeline_flags.get("ready_to_send", False)
        review = pipeline_flags.get("route_to_human_review", True)
        print(f"\n   🚦 Pipeline routing: ready_to_send={ready}, "
              f"route_to_human_review={review}")

    return {
        "will_synthesis": will_output,
        "cost_estimate": cost_estimate,
        "adjustments_log": adjustments_log,
        "rejected_log": auto_rejected,
        "new_rfis": new_rfis,
        "error": None,
        "thinking_mode": "adaptive" if use_adaptive_thinking else "off",
        "effort": effort if use_adaptive_thinking else None,
    }


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Minimal smoke test — requires a real analysis JSON file path as arg
    import sys
    if len(sys.argv) < 2:
        print("Usage: python will_synthesis.py /path/to/construction_analysis_*.json")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        data = json.load(f)

    result = run_will_synthesis(
        analysis=data.get("analysis", {}),
        cost_estimate=data.get("cost_estimate", {}),
        rfi_items=data.get("rfi_items", []),
        validation=data.get("validation", {}),
    )

    print("\n" + "=" * 80)
    print("FULL WILL SYNTHESIS OUTPUT")
    print("=" * 80)
    print(json.dumps(result, indent=2, default=str))
