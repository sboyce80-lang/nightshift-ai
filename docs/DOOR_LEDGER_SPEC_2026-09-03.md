# Door ledger — reconnaissance + spec (2026-09-03)

Doors are wrong on 6/6 recent validation jobs (−93%…+105%) with all four
door flags ON. The fix direction is deterministic: doors come from a parsed
ledger, never from vision counting. Tonight's recon shows it needs TWO modes
and a small golden harness before any code ships — the naive version would
overfit exactly like the flag pile it replaces.

## What the sets actually contain

| set | door schedule? | extractable? | notes |
|---|---|---|---|
| Northwell/Phelps | NO (78 doors, 31% of bid) | — | schedule never issued to bidders |
| Harlem Valley | on A902 — NOT in submitted set | — | legend: "SEE A902 FOR DOOR SCHEDULE"; doors are numbered symbols on the plan |
| ULUM | A-603 p31 | **NO — table plotted as curves** (2 text marks on the whole page) | door marks appear as TEXT on plan pages instead (p8: 001-014, p9: 100-114…) |
| Hudson | no "door schedule" text in 8-page set | — | 197 doors priced by JW |

## Mode A — schedule-table parse (when text table exists: PNC class)
- Reuse `_sheet_index`'s row-reassembly (factor into `_page_rows(page)`),
  header-row detection (>=3 of: DOOR, NO/MARK, SIZE, TYPE, MAT'L, FRAME,
  FINISH, HDW, REMARKS), column mapping by header x-centers.
- Row = leading door-mark token below the header. Ledger: count + per-door
  material (WD/HM/AL) → full-paint vs HM-panel split.
- Validate against PNC (Scott's corrected JSON) + Purdy (doors 0/6
  schedule-correct history) before enabling anywhere.

## Mode B — plan-symbol count (schedule absent/curves: Harlem, ULUM, Northwell class)
- Door marks are text labels near door symbols; they collide with room
  numbers, dimensions, keynotes. Disambiguation needs geometry (the mark
  sits in a hexagon/circle; door 101 serves room 101 so equality with a
  room label is a COLLISION signal, not confirmation).
- Prototype: `get_drawings()` symbol detection around mark labels; count
  distinct marks inside door-symbol shapes per floor-plan page (pages from
  `select_floor_plan_pages`, which now works on mute-title sets).
- Validate against Harlem (29), ULUM (26), Northwell (78 — JW's count with
  no schedule in set, so JW counted symbols too).

## Reconcile contract (both modes)
- Flag `NIGHTSHIFT_DOOR_SCHEDULE_LEDGER`. Ledger present + >=5 entries →
  ledger count is authoritative; extraction supplies only the paint
  classification when the ledger lacks a material column. Delta > 25% vs
  priced doors → RFI line, never silent.
- Provenance: every door line carries `source: ledger(A-603)` or
  `source: symbols(p8,p9)` so read-then-discard is visible in review.

## Existing hooks (integration points, do not duplicate)
- `NIGHTSHIFT_DOOR_SOURCE_RECONCILE` / door material reconcile (PR #15)
- schedule-override persist kill switch (PR #10, Purdy)
- `NIGHTSHIFT_DOOR_SWING_AUTHORITY`, `NIGHTSHIFT_DOOR_DENSITY_RECONCILE`,
  `NIGHTSHIFT_DOOR_TYPICAL_TRANSFER` — all four stay subordinate to the
  ledger when it exists; long-term they collapse into it.
