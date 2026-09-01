# Cross-class regression — findings log (2026-08-22/23)

## RETEST VERDICT (Fishkill, JW flags + exterior fixes, worktree code)
Subtotal $171,011 → **$108,547 (−0.2% vs baseline $108,741)** — the +57%
phantom-exterior regression is fully closed. FIX A fired exactly as
designed: gate record `zeroed_negative: {hardie_siding_sqft: "...factory-
finished fiber cement product not field-painted..."}` with AZEK/corner/
lintel keys kept per-item; RFI + note shipped; mr=True. Walls hit 43,258
vs 43,003 target (**1%**). Residuals are the KNOWN variance class, not
the fixes: stairs 16 vs 8, doors 113 vs 159 (swing run-to-run), and this
run's elevation pass extracted 0 LF cornice/AZEK at pricing time (B run
had 485/320) so the legit ~$14k trim priced $0 — extraction determinism,
queued under the door/stair variance work. Mean err 52.2% vs B 36.6% is
entirely stairs/doors/WC noise; the exterior mechanism is fixed.

## Dutchess Livestock: NEUTRAL
A 47.8% → B 50.1% mean err (Δ+2.3, under threshold), subtotal $60.1k→$61.1k
(Rider golden $22.8k — both configs ~2.6x over on price; pre-existing, not
flag-caused). Walls/doors/base identical; ceilings +43%→+59% drift; windows
0 vs 25 both configs (schedule says "22 windows, 0 painted interior" — scope
convention vs Rider, not a flag failure).

## Fishkill 397: REGRESSION (subtotal +57%) — phantom exterior scope
Quantity metrics flat (37.3%→36.6%) but subtotal $108,741 → $171,011.
Entire delta = new exterior scope priced under the JW set: Hardie siding
$48,007, cornice $6,656, Azek $4,583, lift $4,160, lintels $2,890.
Rider's golden takeoff has no exterior lines.

Failure chain (each link is a fix candidate):
1. **NIGHTSHIFT_ELEV_PASS_ALL_TYPES** fired the dedicated elevation pass on
   this mixed-use job (baseline never ran it). CORRECTION (verified against
   the PDF): pages 12-13 (1-based) ARE real A-200/A-201 elevation sheets
   with directional titles — the run's own "Missing Drawings" RFI claiming
   the A-200 series absent is itself WRONG (RFI-accuracy issue, noted).
   So the pass had legitimate drawings; the quantity (14,800 SF Hardie) is
   an LLM area estimate off real elevations — measurement-quality concern,
   not fabrication-from-nothing.
   → FIX C (still built, reframed): NIGHTSHIFT_ELEV_REQUIRE_SHEETS —
   abstain when NO true elevation sheet exists among cue-matched pages
   (the BofA "phantom exterior on interior-only set" class). Guard
   verified to keep Fishkill's and Caris's real elevation pages.
2. **G2 exterior-evidence gate is job-level, not per-item.** Record:
   `{"evidence": true, "kept": [all 5 keys]}`. Legit paint evidence exists
   (painted AZEK, black lintels) — and that kept the Hardie line too, even
   though the extraction's own notes say HardiePanel is "factory-finished
   fiber cement, not field-painted."
   → FIX A: per-item evidence; negative-evidence tokens ("factory-finished",
   "not field-painted", "pre-finished") zero that item (analog of the WC
   positive-evidence zeroing).
3. **Will's correction was auto-rejected by a direction-blind guard.** Will
   suggested Ext. Hardie Siding 9,427 → 0 citing the factory-finish note;
   rejection reason `hard_numbers_only_policy`. The policy exists to stop
   scope inflation; it also blocks documented scope REMOVAL.
   → FIX B: allow Will reductions-to-zero backed by cited document evidence
   (or at minimum -100% suggestions), keep the guard for increases.
4. Fail-safes that DID work: manual_review=True fired; Scope Conflict +
   Missing Drawings RFIs generated. Under the mandatory-review rollout
   constraint this would not have shipped silently — but the number on the
   proposal is still wrong by +$66k.

AZEK/cornice/lintels (~$14k) are genuinely painted per notes — whether they
belong in a Rider-class interior bid is a scope-convention question for
Steven; the $48k Hardie line is objectively wrong by the run's own evidence.

## 364 Main: quantities IMPROVED, subtotal inflated by allowance lines
A 34.9% → B 29.7% mean err (walls 104.1k→99.3k vs 85.4k target, base trim
6,110→6,712 vs 8,629, stairs 14→12 vs 11 — the JW gates genuinely help
quantities here). But subtotal $166.5k (+2.5% vs Rider $162,456) →
$195.7k (+20%). Decomposition: **Concrete Sealer allowance +$15,334**
(S6 sealed-concrete, tuned for JW utility rooms, fires broadly on this
multifamily) and **Window Sash allowance +$10,971** (window-sash ops,
tuned for Hudson's wood windows; note windows metric still reads 0 vs
Rider's 26 — the allowance prices sash scope the extraction can't even
see as windows). Without those two lines B = +4.3% vs Rider.

PATTERN across Fishkill + 364: the JW-class ALLOWANCE features close real
gaps on JW-style bids but read as over-bid vs Rider-class golden takeoffs
(Rider doesn't carry them). This is a POLICY split, not a bug: candidate
resolution = class-gate the allowance flags (sealed concrete, window sash,
power wash, Level-5) or accept +15-20% vs Rider convention with the
strikeable-allowance framing. Steven's call for the summary.

## TSC Fusion Highland: REGRESSION — CEILING_ASSUME_PAINTED counter-class confirmed
A 68.9% → B 80.3% mean err; subtotal $26.8k → $40.1k (+50%). The 8/11
prediction ("Mercedes/TSC = counter-class, OFF pending golden sweep")
confirmed empirically: **Dryfall Ceiling $0 → $16,897** — assume-painted
priced the exposed-structure ceilings on an industrial job. Also under B:
gyp walls 711→0 (worse; CMU also down 21.3k→15.8k vs 26.6k target),
Interior Lift Rental +$2,625 appeared. mr=True (fail-safe held). Fix
direction: occupancy/class gate on CEILING_ASSUME_PAINTED (industrial /
exposed-structure exclusion), not a revert — the flag earned +9pts on
Harlem's blind bid.

## Honey Farms Malta: the JW set's WIN — evidence gate killed live phantom exterior
A 133.8% → B 137.0% mean err (neutral; walls identically over-extracted
15,686 vs 4,580 in BOTH configs — pre-existing small-commercial class
issue). Subtotal $47.8k (+67% vs Rider $28,564) → $17.8k (−38%). Driver:
**baseline/prod carries $24,444 phantom Ext. Hardie Siding on this job
TODAY; the G2 exterior-evidence gate zeroed it** (the BofA failure class,
caught live). Also zeroed under B: stained wood ($3.8k), base trim,
ext window trim/corner boards (policy zeroings); Gyp Ceilings 1,573→288;
Concrete Sealer +$2.8k appeared. B overshoots downward but mr=True.
103-page set exercised the DD≥60 single-read rule (B ran N=1, 137m).

## Harness notes (addendum)
- run_child meta `manual_review` read result.get("manual_review_required")
  pre-finalization — same pre-set-key bug as run_one 8/20. Authoritative:
  the copied .result.json files, which show mr=True on ALL jwflags cells
  (mandatory review works).
- 364_main_baseline.result.json recovered from output/ (20260822_192621).

## Fixes implemented (worktree nightshift-crossfix-wt, branch crossclass-fixes)
All three built + tested 8/22 eve (17 checks in test_exterior_scope_fixes.py
PASS; test_g_gates, cross-sheet-claims, markup suites PASS). Flags default
OFF: FIX A extends NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE (per-item negative
evidence: factory-finished/not-field-painted sentence zeroes the item it
names, beats job-level positive evidence); FIX B NIGHTSHIFT_WILL_SCOPE_REMOVAL
(hard-numbers guard permits Will reductions-to-zero on exterior lines whose
reason cites factory-finish/by-others documentation); FIX C
NIGHTSHIFT_ELEV_REQUIRE_SHEETS (elevation pass abstains without a real
elevation sheet; directional-titles check precedes the drawing-index filter
because real elevation sheets cite many sheet ids). NOT committed. Retest
plan (after main batch frees the API lane): Fishkill jwflags+fixes —
expect Hardie $48k gone, AZEK/cornice/lintels (~$14k) kept, subtotal
~$123k vs baseline $108.7k.

## Harness note
run_child originally overwrote the copied full result JSON with the meta
file (same filename); fixed to `.result.json` suffix mid-batch. Dutchess +
Fishkill full results recovered from output/ by timestamp.
