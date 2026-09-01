# Phelps Hospital / Northwell (JW RP 26-010-AUG) — Variance Recap
Run 2026-08-31 14:03–22:10 (8h07m) · K=3 draw median · 22-sheet annotation-stripped set

## Headline
| | |
|---|---|
| JW final base bid | **$39,004.63** |
| KnightShift subtotal | **$107,267.69** |
| Variance | **+$68,263.06 (+175.0%)** |
| Manual review flagged | **Yes** |
| Draw spread (K=3) | 90,353 / 114,259 / 107,268 → **23.0%** |

## Dollar attribution
| Driver | JW | KS | $ impact |
|---|---:|---:|---:|
| Walls over-extracted | 20,308 SF | 44,574 SF | **+$22,428** |
| Ceilings: ACT flipped to painted | 2,365 SF | 24,005 SF | **+$18,840** |
| Doors (no schedule in set) | 78 EA | 185 EA | **+$18,019** |
| Phantom scope from A104 | none | stairs/concrete/railings | **+$15,034** |
| Windows missed | 25 EA | 0 | −$2,500 |
| WC-1 missed | 80 SF | 0 | −$630 |
| General requirements missed | $2,500 | 0 | −$2,500 |
| Base trim (both zero) | 0 | 0 | $0 ✅ |

## Root causes

### 1. Scope boundary failure — the whole floor was priced, not the suite (largest driver)
Sheet **A104 is the full 4th-floor plan**; the job is a renovation of **Suites 400 + 417,
8,724 GSF**. A104 alone yielded **114 of 181 rooms**: 21,422 SF of floor, 56,286 SF of wall,
20,758 SF of painted ceiling and **141 of the 185 doors** — including Elevator Lobby, Stair,
Existing Stair, Stair 2, Men's/Women's Lockers, Receiving, Case Cart Storage and Electrical Room.
None are in JW's scope.

**161 of 181 rooms carry no `400-xx` / `417-xx` room number.** The finish schedule on sheet A102
is the hard-number scope boundary (~50 numbered rooms) — it was read but never used to bound scope.
`NIGHTSHIFT_SCHEDULE_SCOPE_AUTHORITATIVE` (the Honey fix) does exactly this but is per-job and was OFF.
Every phantom line traces to A104: stairs (6 sections, $9,450), gyp between stairs ($428),
concrete sealer (1,250 SF, $2,888), painted railings (120 LF, $2,268).

### 2. Ceiling assume-painted overrides observed ACT
Per-sheet extraction got this **right** — A608, A610, A611, A613, A615, A616 all set
`ceiling_painted=false`, recorded "ACT assumed per healthcare default", and raised RFIs.
Aggregation then flipped **118 rooms** to
`"GYP (assumed — enclosed-room default; room-function heuristic said ACT)"`, producing
**24,005 SF of painted ceiling vs JW's 2,365**. The plans' finish schedule explicitly lists
ACT1/ACT2/ACT3 per room. This is an assumption overriding a hard number — a
`no_heuristic_scope` violation. Suspect `NIGHTSHIFT_CEILING_ASSUME_PAINTED_ACT` (added for Harlem).

### 3. Footprint error inverts the plausibility guard
Project overview read **"Total GSF: 8,724" correctly**, but `gross_sqft` came out **null** and
`footprint_sqft` **45,000** (5.2× high; `total_stories` 5 for a single-floor fitout). The guard fired:

> "Total extracted paintable surface (73,693 sqft) is implausibly **low** relative to building
> footprint (45,000 sqft) — ratio is 1.6×, expected 3-6×. This usually means the finish schedule …
> was missed."

Against the true 8,724 GSF the ratio is **8.4× — wildly over**. The guard told the reviewer to hunt
for *more* scope on a job already 175% over. **The right warning inverted into a harmful one.**

### 4. Doors inferred without a schedule
No door schedule exists in the set. 185 doors ≈ one per over-extracted room. JW hand-counted 78
($12,090 = 31% of his bid). Fixing scope (#1) should largely fix this.

### 5. Window schedule extraction returned zero
Pre-scan flagged p3 as a window-schedule page, but extraction returned
`Windows: 0 total, 0 painted interior` on **all three draws**. JW priced 25 window stool/apron/trim.

## What worked
- **Annotation strip**: 359 markups → 0; 3,351 chars of Legend answer-key text and 542 appearance-stream
  paths removed; zero residual quantity tokens; all 22 pages and sheet text intact.
- **Base trim correctly zero** on both sides — RB1C/IB2/TB1C rubber base correctly not painted.
- **WP-1 correctly excluded** as Inpro wall protection, not paint — matching JW's own exclusion.
- **Tile wainscot** flagged for deduction with RFI.
- **Manual review = True**; draw-median consensus ran cleanly and auto-retried a cold draw.

## Recommended next run
1. `NIGHTSHIFT_SCHEDULE_SCOPE_AUTHORITATIVE=1` — bound scope to the ~50 scheduled rooms.
2. Investigate the ceiling ACT override; likely disable `NIGHTSHIFT_CEILING_ASSUME_PAINTED_ACT` here.
3. Fix `gross_sqft` drop → footprint fallback, and make the plausibility guard prefer read GSF.
4. Re-run and re-score; expect the door count to fall out of the scope fix.
