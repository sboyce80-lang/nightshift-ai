# VME ceilings — gate shipped, coverage is the blocker (2026-09-04)

`_apply_vme_ceilings` (NIGHTSHIFT_VME_CEILINGS, default OFF): measured
room polygons from the room-geometry shadow own painted-ceiling AREA in
both directions; the schedule/ACT evidence chain keeps owning
painted-vs-not. Discipline mirrors the walls gate: coverage >= 60% of
painted area, sanity band (0.4, 2.5), dead-roster abstain, counterfactual
on every abstain.

## Offline replay vs round-5 rosters (replay_vme_ceilings.py, free)

Applied on 1/11: harlem (coverage 0.80, 19 rooms, 7,473 -> 9,644 SF —
directionally right, harlem ceilings ran -46% historically). Abstained
everywhere else on the coverage floor — and the abstain is CORRECT:

- fishkill: 73 rooms anchored, only 6 MEASURED (rest leak/merge in the
  flood fill) -> 4% of painted area covered. The join is fine
  (F1-OFFICE1 matches); the measurement is the gap.
- 364_main 38%, dutchess 37%, caris 32%, homewood 4%; hudson/ulum/tsc
  shadows carry zero measured rooms.

## Next work (before the flag can flip anywhere but harlem-class)

1. room_geometry.measure_room_areas coverage: leak plugging + merge
   splitting are the loss modes (fishkill 6/73). Consider raising
   _PLUG_PX, door-gap bridging, and per-room re-probe at higher px_per_ft
   when a room leaks.
2. Anchor enrichment: walls got 5 -> 71 anchors/page from page-text
   room anchors (vme_attribution.page_text_room_anchors); the shadow
   still anchors only from extraction bboxes. Feed the text anchors in.
3. Whole-floor basis-1 analog (total_enclosed_area x painted-ceiling
   fraction) as a fallback — only with the walls gate's coverage
   preconditions; do NOT ship it as an escape hatch for the per-room
   coverage floor.

## v2 measurement round (same night): NIGHTSHIFT_ROOM_GEOMETRY_V2

Two loss modes attacked (adaptive closure ladder + render-mask fallback;
nearest-anchor splitting of merged components, distinct coordinates only):

| job | measured v1→v2 | coverage v1→v2 | note |
|---|---|---|---|
| homewood | 12→28 | 4%→41% | splits work; remainder = template siblings (19 anchors on ONE point — not separable, needs template-unit basis) |
| hudson | 0→4 | 0→30% | partial enclosure rescue |
| harlem | 19→26 | 0.80→0.927 | applies with larger correction (9,644→12,588; no ceiling target to verify) |
| fishkill | 6→7 | 4%→11% | enclosure still fails (walls invisible to both segments AND render) |
| 364/dutchess/ulum | unchanged | — | |

REMAINING BLOCKERS, precisely: (1) template-instance rosters put all
sibling anchors on one point — the fix is a TEMPLATE-UNIT BASIS (measure
the typical unit's rooms once on the enlarged unit plan × unit_multiplier,
which the roster already tracks), not more partitioning; (2) fishkill/ulum
-class enclosure failure — wall linework invisible to axis segments and
too faint for the render threshold; try stroke-width-aware rasterization
of get_drawings() paths (all orientations) before giving up. Nothing new
clears the 60% coverage bar yet; the gate stays honest.
