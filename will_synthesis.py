## Field-by-field guidance

**`adjustments`**: Each adjustment must include `from_value` (current quantity, e.g. 2,150 sqft), `to_value` (your adjusted quantity), the resulting `from_total` and `to_total` dollar amounts, a clear `reason`, and your `confidence` (0.0–1.0). Only include adjustments you actually want applied. If you don't want to adjust something, leave it out.

**`rejected_adjustments`**: If you considered adjusting something by more than 25% but had to back off, log it here. The `pct_change` should be the percentage you would have applied (positive or negative). These get auto-converted to RFIs by the pipeline.

**`additional_rfis`**: RFIs the Python pipeline didn't generate. Don't duplicate the existing RFI list — add new ones based on your review. Categorize them and rate severity.

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

9. **Prevailing wage** — Check project_info, scope notes, and any government/public/school references. If it's a NY public project, prevailing wage almost certainly applies.

10. **Round numbers test** — Total feels right? A 2,500 sqft single-family home shouldn't price at $80,000. A 3-story 20-unit building shouldn't price at $35,000.

## Critical constraints

- **Output JSON only.** No markdown, no prose preamble, no code fences. The pipeline will fail to parse anything else.
- **Adjustments must stay within ±25%.** Going beyond means it becomes an RFI in `rejected_adjustments`, not an `adjustments` entry.
- **Don't invent line items.** You can only adjust categories that exist in the input estimate.
- **Sign your scope of work.** End `gc_scope_of_work` with `— Will, Senior Estimator, Rider Painting, Inc.`
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

    payload = {
        "project_info": {
            "building_type": pi.get("building_type", "unknown"),
            "total_stories": pi.get("total_stories", 0),
            "total_units": pi.get("total_units", 0),
            "footprint_sqft": pi.get("footprint_sqft", 0),
            "total_floors_analyzed": pi.get("total_floors_analyzed", 0),
            "total_rooms_found": pi.get("total_rooms_found", 0),
            "project_name": pi.get("project_name", ""),
            "location": pi.get("location", ""),
        },
        "aggregated_totals": agg,
        "exterior": ext,
        "line_items": line_items_simplified,
        "subtotal": cost_estimate.get("subtotal", 0),
        "existing_rfis": rfi_items or [],
        "validation_warnings": validation.get("warnings", []) if validation else [],
        "data_quality_score": validation.get("data_quality_score", 0) if validation else 0,
        "scope_notes": pi.get("_scope_notes", "") if isinstance(pi, dict) else "",
        "pipeline_notes": analysis.get("notes", [])[:30],
    }
    return payload


def _validate_adjustment(adjustment, line_items_by_category):
    """Validate a single proposed adjustment against the guardrails.

    Returns (is_valid, reason). If invalid, the adjustment should be moved
    to rejected_adjustments instead of applied.
    """
    cat = adjustment.get("category", "").strip()

    if cat in PROTECTED_CATEGORIES:
        return (False, "protected_category")

    if cat not in ADJUSTABLE_CATEGORIES and cat not in line_items_by_category:
        return (False, "category_not_found")

    current = line_items_by_category.get(cat)
    if not current:
        for known_cat in line_items_by_category:
            if cat.lower() in known_cat.lower() or known_cat.lower() in cat.lower():
                current = line_items_by_category[known_cat]
                break
        if not current:
            return (False, "category_not_found")

    current_qty = _num(current.get("qty", 0))
    proposed_qty = _num(adjustment.get("to_value", 0))

    if current_qty == 0:
        return (False, "current_value_is_zero")

    pct_change = abs(proposed_qty - current_qty) / current_qty
    if pct_change > MAX_ADJUSTMENT_PCT:
        return (False, "exceeds_25_percent_band")

    return (True, "ok")


def _apply_adjustments_to_estimate(cost_estimate, valid_adjustments, line_items_by_category):
    """Apply Will's accepted adjustments to the cost estimate in-place.

    Recalculates line item totals (qty × unit_rate × markup) and the subtotal.
    Returns (modified_estimate, adjustments_log).
    """
    log = []
    line_items = cost_estimate.get("line_items", [])

    by_label = {item["item"]: i for i, item in enumerate(line_items)}
    by_category = {_category_from_item_label(item["item"]): i for i, item in enumerate(line_items)}

    for adj in valid_adjustments:
        cat = adj.get("category", "").strip()
        idx = by_category.get(cat)
        if idx is None:
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

    new_subtotal = round(sum(_num(item.get("total", 0)) for item in line_items), 2)
    cost_estimate["subtotal"] = new_subtotal

    return cost_estimate, log


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_will_synthesis(analysis, cost_estimate, rfi_items=None, validation=None,
                        client=None, model="claude-sonnet-4-20250514"):
    """Run the Will synthesis layer on a completed analysis + cost estimate.

    Args:
        analysis: dict from run_analysis() containing aggregated_totals, floors, etc.
        cost_estimate: dict with line_items and subtotal
        rfi_items: list of existing RFI dicts (so Will doesn't duplicate)
        validation: dict with warnings and data_quality_score
        client: optional anthropic.Anthropic instance (creates one if None)
        model: Claude model to use

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

    payload = _build_review_payload(analysis, cost_estimate, rfi_items, validation)

    line_items_by_category = {}
    for item in cost_estimate.get("line_items", []):
        if _num(item.get("qty", 0)) > 0:
            cat = _category_from_item_label(item["item"])
            line_items_by_category[cat] = item

    user_message = (
        "Review this completed Rider Painting takeoff and cost estimate. "
        "Apply your senior-estimator judgment, propose any line item adjustments "
        "within the ±25% guardrail, write the GC-level scope of work and Joist "
        "shorthand, and set your confidence and bid recommendation.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    )

    print(f"   📤 Sending estimate to Will for review ({len(payload.get('line_items', []))} line items)...")

    try:
        result_parts = []
        with client.messages.stream(
            model=model,
            max_tokens=8000,
            temperature=0,
            timeout=180.0,
            system=WILL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)
        raw_response = "".join(result_parts)
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

    proposed_adjustments = will_output.get("adjustments", [])
    valid_adjustments = []
    auto_rejected = []

    for adj in proposed_adjustments:
        is_valid, reason = _validate_adjustment(adj, line_items_by_category)
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

    cost_estimate, adjustments_log = _apply_adjustments_to_estimate(
        cost_estimate, valid_adjustments, line_items_by_category
    )

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

    additional_rfis = will_output.get("additional_rfis", [])
    for rfi in additional_rfis:
        rfi.setdefault("source", "will_synthesis")

    new_rfis = additional_rfis + auto_rejected_rfis

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
    }


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
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
