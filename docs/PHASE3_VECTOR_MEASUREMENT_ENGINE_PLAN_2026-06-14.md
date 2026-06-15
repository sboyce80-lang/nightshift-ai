# Phase 3 — Vector Measurement Engine (VME) build plan + remaining roadmap

**Date:** 2026-06-14
**Branch:** phase0-review-fixes
**Status:** de-risked, ready to build

---

## Why this exists (the pivot)

Audit of 9 original bid sets / 563 pages (incl. Rider's NYULH Westchester, WMC Kingston, Wingstop, 119 Franklin, Dutchess, 364, Fishkill): **100% vector, 0 scanned.** The wall lines are exact CAD geometry sitting in the file. The current pipeline conflates "no text layer" with "raster image," rasterizes vector pages, and hands them to a vision model that flattens walls to bounding-box rectangles — the root cause of both the $0 image-only collapse and the residual wall under-counts.

**De-risk spike result (364 Main, golden walls = 85,353 SF):** a purely deterministic 3-step geometry algorithm (no vision, no ratios) measures wall faces directly from the CAD lines. The approach is sound and the per-sheet primitive + scale auto-detection are validated (see `vector_measure.py` / `test_vector_measure.py`).

> **⚠️ CORRECTION (2026-06-15, M0 build):** the spike's "within 2%" was a **scale artifact, retracted.** It hardcoded 1/8"=9 pts/ft for every sheet; the "composite" sheet is actually **3/32"=1'-0"**, so at its true scale it reads **111,542 SF — 30% OVER golden**, not 2% under. Per-floor sheets at their correct detected scale: basement 16,786 SF, 1st floor 25,946 SF (plausible), but the upper-floor sheet reads ~2× high (multi-floor content + cross-layer duplication). **Net: accuracy is UNPROVEN.** The approach is still the right direction, but **M1 (sheet/floor selection + dedup) and M2 (paintability/poché) are more load-bearing than the spike implied**, and real validation needs the golden set (2.1).

The VME is the path to the "truest number": **trace and measure the hard lines on the plan.**

---

## The proven core algorithm (from the spike)

For a given sheet:
1. **Layer filter** — keep paths whose CAD layer (from PyMuPDF `get_drawings()[].layer`, populated on BDC-tagged pages) matches `A-Wall` / `a-wall-demising` / `partition`; exclude `anno`/`iden`/`patt`/`hatch`/`blow`.
2. **Drop diagonal segments** — wall poché/hatch fill is ~45° diagonal; true wall faces are orthogonal. Dropping diagonals removed the dominant overcount (e.g. 6,161 of 12,682 ft on the 1st-floor sheet).
3. **Interval-union collinear axis-aligned segments** — bucket horizontals by y, verticals by x; union the covered intervals. Collapses coincident duplicate face lines (~halves the remainder).

Output: deduped wall-face line length (feet, via per-sheet scale). Wall paint area (both faces) ≈ face-line length × height.

---

## Phased build

### Phase M0 — Harden the spike into a module
- Extract the spike into `vector_measure.py`: `measure_wall_faces(pdf_path, page_index) -> {wall_face_lf, by_layer, diagnostics}`.
- **Per-sheet scale auto-detection** — read the drawing scale from the title block (`1/8"=1'-0"`, `1/4"=1'`, etc.); fallback: calibrate from a known grid spacing or a dimensioned string. (Spike assumed 1/8"=1'; must be detected.)
- Validate: reproduce spike numbers (p2 composite 83,656; p13 1st floor 25,946).
- **Exit:** module returns per-sheet wall-face LF with detected scale, matching the spike.

### Phase M1 — Sheet selection & floor attribution (the #1 build problem)
- Classify each page: floor plan vs RCP vs finish vs MEP vs section/detail vs **composite** (multi-floor), using sheet ID (`A-1xx`), layer prefix (`M-0/1/2/3` = Basement/1/2/3), and title text.
- **Pick ONE canonical wall source per physical floor** — the composite sheet AND the per-floor sheets both carry the walls; using both double-counts. Reuse the canonical-identity logic from the existing dedup work.
- **Exit:** one wall-run set per floor, no cross-sheet double counting; sum over floors ≈ golden on 364.

### Phase M2 — Surface computation (LF → paintable SF)
- **Height** per room/floor from the finish schedule / sections / floor-to-floor; flagged default fallback (spike used flat 9 ft).
- **Paintability filter** — exclude exterior faces of perimeter walls, shafts/chases, unpainted CMU, glazing openings; subtract door/window openings (schedules already extracted).
- **Angled walls** — generalize beyond orthogonal: parallel-pair centerline extraction for non-axis-aligned walls (orthogonal-only is fine for 364, not for angled buildings).
- **Exit:** paintable wall SF per floor/room, provenance = `measured (vector)`.

### Phase M3 — Integration & routing
- New path behind `NIGHTSHIFT_VECTOR_MEASURE` (default **off**), inserted at page routing: *vector page → VME; else existing path.*
- **Tiering:** Tier 1 layer-tagged (filter by layer) → Tier 2 flattened vector, no layers (WMC/NYULH/Dutchess) → geometric wall classification (parallel-pair detection by thickness/weight) → Tier 3 true raster → existing vision path (the Phase 2.2 fallback).
- Feed VME output into the pipeline as `measured` provenance so it flows through the provenance gate (2.3), calibrated confidence (2.4), and pricing unchanged.
- **Extend to other surfaces the same geometry yields for free:** ceilings (RCP layers), base trim (floor-line perimeter = wall runs), soffits.
- **Exit:** VME-on produces a full estimate on a vector set end-to-end.

### Phase M4 — Validation & calibration
- Run across **all** Rider golden sets, not just 364; establish real per-surface accuracy distributions (replaces the "2% fortunate" with measured bars).
- Two-track regression: **VME-off parity** (proves no regression) + **VME-on accuracy** (vs golden).
- Tighten height/scale/paintability where errors concentrate.
- **Exit:** documented accuracy per surface across the golden set.

### Phase M5 — Tier-2 classifier + rollout
- Build the Tier-2 geometric wall classifier for flattened-vector sets (no layers): detect walls as parallel line pairs at wall-thickness spacing / heavier line weight.
- Edge cases: curved walls, partial sheets, missing scale, mixed vector/raster.
- Graceful fallback: low VME confidence / sparse geometry → defer to vision path (never worse than today).
- Flip default-on once the accuracy bar is met across golden.

---

## Remaining roadmap (all phased work, with status)

| Track | What | Status | Depends on |
|---|---|---|---|
| **2.2 image-only** | per-sheet $0-collapse fix | ✅ done + validated (119 Franklin) | — |
| **P2-G** | base trim + small-commercial floor dedup | ✅ locked in (all 4 checks pass) | — |
| **2.2 follow-ups** | discipline-map soft-fail (PG-named sheets); per-sheet default-on decision | open | superseded for vector sets by Phase 3 |
| **Baseline lock** | golden regression both modes (no-regression for index gate) | 🔄 running | — |
| **2.1 golden consolidation** | Rider's verified takeoffs → `golden/calibration_data.json` (N≥8) | open — **linchpin** | Rider Drive source |
| **2.3 provenance gate / Trust Summary** | strip unsupported scope; show measured vs assumed | implemented, default-off | validation via 2.1 |
| **2.4 calibrated confidence** | confidence scoring from residuals | implemented, needs N≥8 to activate | **2.1** |
| **Phase 3 VME** | vector measurement engine (M0–M5) | de-risked, ready | 2.1 for accuracy bars |

### Dependency / sequencing logic
- **2.1 (golden consolidation) is the linchpin.** It unblocks validation of Phase 3 (M4 accuracy bars), activates 2.4 calibration (N≥8), and is the only way to *prove* any number converges on Rider's truth. Do it early / in parallel with M0–M2.
- **Phase 3 VME is the accuracy engine** for vector sets — which is all of them. It supersedes the vision-extraction accuracy heuristics (A/B wall boosts, per-sheet perimeter tuning) for vector inputs.
- **2.3 / 2.4 are the trust layer** on top — they consume VME's `measured` provenance and get cleaner inputs from it.
- **Strategic call on per-sheet default-on:** now that image-only is fixed, the instinct is to turn per-sheet on. But VME is the better primary path for vector sets. Recommend: **do not promote per-sheet to default; aim VME as the primary vector path**, keep per-sheet/vision as the Tier-3 raster fallback.

### Recommended order
1. **2.1 golden consolidation** (parallelizable, unblocks everything) + **M0–M1** (module + sheet dedup).
2. **M2–M3** (surfaces + integration) → first end-to-end VME estimate.
3. **M4** validation across golden → accuracy bars → activate **2.4** calibration.
4. **2.3** rollout (with VME provenance) + **M5** Tier-2 classifier + default-on.

---

## Risks & honest caveats
- The spike's 2% is partly fortunate (height/scale/paintability assumptions netted out) — **not yet an established accuracy bar**; M4 sets the real bars.
- **Sheet dedup (M1)** is the main build risk — composite vs per-floor double-count.
- **Tier-2 (flattened vector, no layers)** is genuinely harder; ~1/3 of the sampled sets (WMC, NYULH, Dutchess) have no OCG layers and need the geometric classifier.
- Height & paintability still need schedule/section data, not pure line geometry.
- Everything still requires Rider's golden takeoffs to *prove* accuracy — measurement replaces ratio-calibration, but validation against ground truth doesn't go away.
