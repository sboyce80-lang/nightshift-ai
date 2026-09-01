# Door "aggregate-vs-line reconcile" — investigated 2026-08-22, reframed

The 8/22 golden-harness session recorded a "new bug class: door counts
diverge aggregate-vs-priced-line (Caris agg 104 vs line 62; ULUM 4 vs ~16;
Homewood 164 vs ~336)". **That framing is wrong.** Sweep of every stored
result era (17 result*.json across the 5 JW jobs) shows
`analysis.aggregated_totals.total_doors_full_paint` == the priced
"Doors (Full Paint)" line quantity in *every* file, and
`regression_test.extract_metrics` returns the same value. There is no
internal inconsistency to reconcile.

The remembered numbers are cross-RUN comparisons (latest G-gates result.json
vs r4final/s4-era results of the same job):

| job | baseline | s3/s4 | r4final | conf | G-gates final |
|---|---|---|---|---|---|
| Caris fp | 63 | 93 / 2 | 62 | 68 | **104** |
| ULUM fp+hm | 28+0 | 28+0 / 16+7 | 15+11 | 15+10 | **4+11** |
| Homewood fp+hm | 372+0 | 696+60 / 303+68 | 362+66 | 295+150 | **164+131** |

Real phenomena to fix (feeds the variance-reduction queue):

1. **Door-count run variance survives N=3 consensus.** Caris swings 2–104
   across runs at near-identical code. Fill-only consensus never *lowers* a
   non-zero read, so an over-count on any single draw sticks; and schedule/
   plan-count paths (door source reconcile "single_family plan=N") bypass
   per-sheet consensus entirely. Candidate: median-vote specifically for
   door counts (they're integers from counting, not measurements — the
   fill-only rationale doesn't apply), or route doors through the V2
   median machinery.
2. **fp↔HM classification flapping** (ULUM 28+0 → 15+10 → 4+11): total is
   fairly stable (~15–28) while the split moves. Rate delta $155 vs $110
   makes this a real $ swing. Candidate: classification consensus or a
   schedule-echo tie-break.

Rooms carry no door fields in any era (doors live in schedule_data +
aggregated_totals only), so a "rooms vs aggs mutation point" reconcile has
nothing to operate on.
