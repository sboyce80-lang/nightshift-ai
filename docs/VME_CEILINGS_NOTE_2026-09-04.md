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
