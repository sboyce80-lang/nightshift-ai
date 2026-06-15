# Phase 2.2 update — per-sheet extraction regresses image-only PDFs to $0

**Date:** 2026-06-14
**Trigger:** Matt's feedback on **Estimate 4628 (119 Franklin)**, run on live prod
**Submission:** `44089ae4-3b1f-4cb2-849c-ada1915bc6cb`
**Input:** `submissions/44089ae4.../uploads/119_Franklin_Bid_Docs.pdf` (R2), 34 pages, **image-only/scanned** (32 of 34 pages have no text layer)
**Repro:** `nightshift-repo/run_franklin_4628.py` (per-sheet ON), output in `franklin_local_run/`

---

## TL;DR

Matt's 4628 review (undercounted walls/ceilings, "finish schedule not found", missing room
dimensions, invented exterior cornice) all trace to **one root cause: the bid set is an
image-only/scanned PDF.** The live prod run (old code) couldn't read the raster sheets and
concluded *"only 2 of 34 sheets were provided (T-1, SS-1)"*, then still **priced** assumed
exterior scope.

I reran 4628 locally with **Phase 2.2 per-sheet extraction ON**. It did **not** fix the
undercount — **it made it worse**: the per-sheet plan-sheet classifier kept **only the Title
Sheet**, extracted **0 rooms**, and produced a **$0 estimate**.

| Run | Walls SF | Ceilings SF | Rooms | Finish sched | Cornice | Exterior SF | Subtotal |
|---|---|---|---|---|---|---|---|
| Live prod (old code) | 2,286 | 688 | 58 | not found | 204 LF / $4,406 | 3,188 | $29,764 |
| **Local, per-sheet ON** | **0** | **0** | **0** | found ✓ | dropped | 0 | **$0** ❌ |
| Local, legacy multimodal + prov. gate ON | 13,854 | 4,556 | 29 | found ✓ | **0 (stripped)** | **0 (stripped)** | $32,651 ⚠️ |

⚠️ The legacy-multimodal control lands in **manual review — "footprint could not be
determined"** (18,410 SF extracted, no footprint anchor). It addresses the *direction* of
Matt's feedback (no longer "light", finish schedule found, invented cornice/exterior stripped)
but is not a clean number — and we have **no verified 119 Franklin takeoff** to know whether
13,854 SF is right or now *over*-extracted. Treat as directional, not validated.

This is a **release-blocker for turning per-sheet on by default**: any scanned bid set would
collapse to $0. `NIGHTSHIFT_PER_SHEET_EXTRACTION` defaults to `0` today, so prod is unaffected
— but it must be fixed before Phase 2.2 ships on.

---

## Root cause (two compounding defects)

In `_extract_rooms_per_sheet` (Takeoff_DIRECT.py), plan sheets are identified by a text-layer
signal **or** a title-block keyword detector, with a large-format fallback only when *no* plan
sheets are found.

### Defect 1 — Title/index sheet is a false positive

`_title_text_is_plan_sheet` (Takeoff_DIRECT.py:3061) returns `True` on any page whose text
contains `"floor plan"`, `"reflected ceiling plan"`, etc. The **Title Sheet's "List of
Drawings"** enumerates exactly those phrases as index entries, so **T-1 is misclassified as a
plan sheet.** (T-1 is the one page in this set with a real text layer, so it's the only page
any text detector can score.)

### Defect 2 — All-or-nothing rasterized-plan fallback

The scanned-plan fallback is gated on `if not plan_pages:` (Takeoff_DIRECT.py:3886). Because
Defect 1 put T-1 into `plan_pages`, the list was **non-empty**, so the fallback that would have
rasterized the **31 image-only floor plans never fired.** Net result: per-sheet measured only
the title sheet → 0 rooms → $0.

```
   📑 PER-SHEET EXTRACTION: 1 plan sheet(s) of 32 painting-relevant, 34 total
      📄 Sheet T1 (p1): extracting...
   📑 Per-sheet merge: 0 rooms across 1 sheet(s)
   ✅ 0 rooms found, 0 sqft walls
```

A single false positive suppresses the entire scanned-PDF path. The legacy multimodal path
(what live used) reads all 32 painting-relevant pages via chunked vision and *does* extract
geometry — so per-sheet is strictly worse here.

### Same defect leaks into the enhanced-tiled path

The `_title_text_is_plan_sheet` helper is also used by the **enhanced (tiled) extraction**
plan-sheet recovery (Takeoff_DIRECT.py:4375–4392). A control rerun on that path (multi-pass)
logs the identical failure:

```
🔬 Plan-sheet recovery: text-layer dims identified 0 page(s); title-block text identifies
   1 plan sheet(s) — adding 1 rasterized plan sheet(s) [1] ...
🔬 ENHANCED EXTRACTION: Tiling 1 floor plan page(s) (of 32 painting-relevant, 34 total)
```

So Defect 1 (the title/index false positive) is **not** per-sheet-specific — fixing the helper
hardens both the per-sheet and enhanced-tiled paths against image-only sets.

---

## Fix

Three parts. (1) and (2) are the core fix; (3) is a safety net so per-sheet can never emit a
$0 estimate when the legacy path would succeed.

### (1) Negative-gate the title/index sheet

New helper, used like the existing section/detail gate inside the plan-detection loop
(before the dims/keyword checks, ~Takeoff_DIRECT.py:3863):

```python
def _title_text_is_index_sheet(raw_text):
    """True for a Title/Cover/Index sheet whose 'List of Drawings' enumerates
    plan-sheet names (e.g. 'FLOOR PLAN', 'REFLECTED CEILING PLAN') as index
    entries — text that falsely trips _title_text_is_plan_sheet. A drawing
    index is not itself a plan sheet (it has no measurable geometry)."""
    txt = str(raw_text or "").lower()
    if not txt:
        return False
    INDEX_MARKERS = ("list of drawings", "drawing index", "sheet index",
                     "drawing list", "index of drawings", "title sheet",
                     "cover sheet")
    return any(k in txt for k in INDEX_MARKERS)
```

```python
        # NEGATIVE gate FIRST ...
        if _title_text_is_section_or_detail(raw):
            section_skipped.append(pg)
            continue
        if _title_text_is_index_sheet(raw):      # <-- NEW: drop title/index sheets
            section_skipped.append(pg)
            continue
```

### (2) Make the rasterized-plan fallback additive, not all-or-nothing

Replace the `if not plan_pages:` fallback (Takeoff_DIRECT.py:3886) with one that unions in
**image-only large-format pages whenever they exist** — so a stray text-detected sheet can't
suppress the scanned-plan path:

```python
    # Large-format included pages with NO usable text layer are scanned plan
    # sheets the text detectors can't score. Union them in whenever they exist,
    # not only when plan_pages is empty — a single false positive (e.g. a Title
    # Sheet whose drawing index lists 'FLOOR PLAN') must not suppress the
    # rasterized-plan path for an entire image-only set.
    # (119 Franklin / estimate 4628: 1 title sheet detected, 31 scanned plans
    #  dropped -> $0.)
    raster_plans = []
    seen = set(plan_pages) | set(section_skipped)
    for c in included:
        pg = c["page_index"]
        if pg in seen:
            continue
        if (text_layers.get(pg) or {}).get("raw_text", "").strip():
            continue  # has text the detectors above already judged
        try:
            is_large, _, _ = _is_large_format_page(pdf_path, pg, 2000)
        except Exception:
            is_large = False
        if is_large:
            raster_plans.append(pg)
    if raster_plans:
        print(f"   🖼  Per-sheet: adding {len(raster_plans)} image-only "
              f"large-format page(s) as scanned plan sheets")
        plan_pages.extend(raster_plans)
```

### (3) Safety net — never emit a $0 per-sheet estimate; fall back to legacy

After the per-sheet merge, if 0 rooms / 0 wall SF were extracted, **return `None`** so the
orchestrator uses the legacy multimodal path (the same `return None` contract already used at
Takeoff_DIRECT.py:3900 when no plan sheets are found). A per-sheet run that produces nothing
should defer to the path that produces something, not ship a $0 number.

---

## Validation

1. Rerun `run_franklin_4628.py` (per-sheet ON) — expect the 31 scanned plans to be rasterized
   and extracted; walls/ceilings/rooms in the ~live ballpark (≥ 2,286 / 688 / 58), **not** $0.
2. Add a tier-1/regression guard: **a per-sheet run on an image-only set must not return 0
   rooms / $0.** (119 Franklin is a ready fixture — input + live baseline saved under
   `franklin_local_run/`.)
3. Re-run the golden regression (`run_golden_regression.py`, both modes) to confirm no
   text-layer set regresses from the new title/index negative gate.

---

## Notes on the rest of Matt's 4628 feedback (separate from Phase 2.2)

These are extraction-quality / pricing-provenance items, addressed on other paths — tracked
here only so 2.2 work isn't expected to cover them:

- **Invented exterior cornice (204 LF) / unverified exterior 3,188 SF** → Phase 2.3
  **provenance gate** (`NIGHTSHIFT_PROVENANCE_GATE`, default 0). Confirmed working on the
  legacy-multimodal control rerun: **cornice 204 → 0 LF and exterior 3,188 → 0 SF** (classified
  `assumed`, no A-1 elevation sheet / no measured backing, dropped to unpriced-exposure RFI).
  This directly resolves Matt's "invented cornice / verify exterior" items.
- **"Finish schedule not found" (A-9)** → `has_finish_schedule` flips **False → True** on both
  local configs once the raster sheets are read.
- **Walls/ceilings undercount (live 2,286 / 688)** → on local code the legacy-multimodal path
  jumps to **13,854 / 4,556** — no longer "light", but it overshoots into a no-footprint manual
  review. Whether that's *correct* or now *over*-extracted is unknown without a verified 119
  Franklin takeoff. **Action: get Rider's manual takeoff for 119 Franklin** to anchor this as a
  golden/calibration case before trusting either direction.
