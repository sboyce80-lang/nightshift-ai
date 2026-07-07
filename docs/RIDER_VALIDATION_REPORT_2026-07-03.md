# Rider 10-Job Validation Report — 2026-07-03

**What was done:** 10 historical Rider Painting jobs (bid set PDF + Brian Rider's verified takeoff spreadsheet, pulled from the connected Drive) were run through the full pipeline **locally on current main (`96d87b6`) with prod's exact flag state**, then compared against Rider's numbers on *measurements first* (walls, ceilings, doors, trim) and dollars second. Two live prod re-runs (Purdy Ave, Burger King) rounded out the evidence. All artifacts live in `scratchpad/rider_batch/` (plans, takeoffs, results, `targets.json`, `compare.py`, `vme_score.py`).

---

## Scoreboard — pipeline subtotal vs Rider's bid

| Job | Type | Pipeline | Rider | Δ | System's own risk call |
|---|---|---:|---:|---:|---|
| **Honey Farms** | c-store | $29,034 | $28,564 | **+1.6%** ✅ | conf 0 (±255%), clarifications_only |
| **364 Main** | mixed-use resi | $177,236 | $162,456 | **+9.1%** ✅ | conf 49, clarifications_only |
| Mazda Middletown | dealership | $78,515 | $82,604 (total bid) | **−4.9%** / +170% vs interior-only | conf 0, **do_not_bid** ✓ |
| TSC Highland | retail repaint | $57,467 | $45,050 (total) | +27.6% | conf 0 (±168%), clarifications ✓ |
| Mercedes Danbury | dealership | $128,107 | $93,118 | +37.6% | conf 0, **do_not_bid** ✓ |
| CenHud Fishkill | industrial addn | $31,092 | $52,593 | −40.9% | Will 42% — **missed** |
| Thorne Memorial | historic resto | $174,535 | $332,733 | −47.5% | conf 0 (±255%), cautious ✓ |
| Atria Briarcliff | senior living | $579,564 | $296,806 | +95% | conf 49 — **missed** |
| Livestock Hill | small comm | $53,775 | $22,758 | +136% | conf 49 — **missed** |
| Colonie Seniors | senior MF | $1,154,736 | $487,621 | +137% | conf 49 — **missed** |

**Within ±10%: 2 of 10** (3 of 10 counting Mazda on the total-bid basis). Median absolute error ≈ 44%.

### Prod re-runs (this week's fixes, live)
- **Purdy Ave**: $142,974 → **$107,279**. Door schedule now read correctly (0 full-paint + 6 HM — apartment doors are factory-finished Masonite; the old run priced 136 phantom doors) and the override **persisted** (the ledger fix working). Phantom $8.3k exterior lift removed. Calibrated confidence honestly reports 0 (±255%) on unverified walls. Open: 21 stair sections (~$33k) still implausible.
- **Burger King**: $25,758 → **$14,029 exterior-only** — correctly refuses to price interior scope that only existed as estimates off a cover-sheet table (proposed floor plans were never in the PDF). Will's recap: "not a complete bid, don't send." **This is the hard-numbers policy working.**

---

## What went right

1. **The guardrail/honesty layer is real.** Burger King fail-safed; Purdy's doors corrected and stayed corrected; Will's recommendations (`do_not_bid`, `cautious`) fired on the worst four jobs.
2. **Risk routing was correct on 6 of 10 jobs.** Everywhere the per-sheet evidence inputs were live, calibrated confidence discriminated (conf 0, predicted error ±168–255% on Mazda/Mercedes/TSC/Thorne/Honey). The 4 misses (CenHud, Livestock, Atria, Colonie) all sat in the flat-49 legacy bin — fixable by harvesting this batch into calibration rows.
3. **Two jobs inside ±10%** — including 364 Main, the job that under-bid by $75k three days ago. This week's cap-guard + schedule-persistence fixes are visibly load-bearing.
4. **The VME geometric engine validated on real jobs**: CenHud walls **−2.5%** measured with zero layer/text/human input (vision was −50%); Thorne +271% → +24%; TSC +469% → −12% (with read heights). The "LLM reads, geometry measures" architecture is provably right.
5. **A permanent accuracy harness now exists** — 10 jobs, verified targets, one-command rescoring. Every future fix gets a number instead of an anecdote.

## What went wrong

1. **Vision-as-measurer remains the root cause.** Multifamily/unit-multiplied jobs over-extract ~2× (Colonie +113% walls, Atria +163%); the two ±10% wins were partly offsetting errors (Honey's walls were +177% internally).
2. **Substrate/system classification is weak**: TSC's GYP/CMU inverted; Mercedes' 32,848 SF exposed ceiling billed as GYP ceilings (+741% ceilings, −97% dryfall); Thorne's plaster restoration priced at commercial gyp rates.
3. **Door counting** still noisy in both directions (Mercedes 49 vs 29 full; Honey 1 vs 8; CenHud billed 39 full-paint vs Rider's 35 frames-only).
4. **Config landmine confirmed:** wallcovering carried at **$9.00/SF install** while Rider's Mazda takeoff bills **$0.50/SF** — an 18× line-item error waiting for the next WC job. Needs Rider's confirmation, then a one-line fix.
5. **Takeoff-basis ambiguity blocks two scores**: Mercedes' 5,100 LF (faces vs runs?) and Honey's color-additive billing need Brian's 5-minute answers.

## Overall confidence score

- **As an autonomous bid machine on complete sets: 4/10** (2–3 of 10 inside ±10%; median |error| ~44%).
- **As an estimator's assistant with the honesty layer routing what humans review: 7/10** — it now *knows* when it's wrong on jobs with per-sheet evidence, and says so before a bid goes out.
- **Guardrails/fail-safe behavior: 9/10.**
- **Trajectory:** the VME engine already measures walls at −2.5% on its best job with no human input. The gap between 4/10 and 9/10 is measurement, and measurement is now a geometry problem with a working prototype — not a model-capability hope.

## Next steps

1. **Release 1 (VME accuracy, in flight)** — reliability-gated per-room scope filtering (the batch showed blind scope trust degrades good measurements), 364 basement rule, Colonie page patterns. Gate: ≥8/10 jobs within ±10% walls on this harness.
2. **Release 2 (VME primary)** — geometric walls/ceilings/trim as `measured` provenance replacing vision estimates; retire the boost/cap heuristics on that path.
3. **Release 3 (classification fixes)** — GYP/CMU legend-first, frames-only doors, exposed-ceiling vs GYP-ceiling, wallcovering rate (pending Rider).
4. **Release 4 (confidence)** — harvest these 10 jobs + re-runs into calibration (N 9→20) to kill the flat-49 bin.
5. **Ask Brian (blocking two scores + one config):** Mercedes LF basis; Honey billing basis; wallcovering $0.50 vs $9.00.
