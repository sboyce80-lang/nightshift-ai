# Path to ±10% on every job — gap decomposition across all 10 goldens
2026-08-23. Sources: JW ground_truth.json ×5, Rider takeoff xlsx (Dutchess,
Fishkill, Honey, TSC — parsed tonight), REFERENCE_CASES, all 11 crossclass
runs. KEY CORRECTION to the overnight report: Rider's takeoffs for
Fishkill, Honey, and TSC all INCLUDE exterior scope — the exterior story
is not "phantom scope" but "wrong quantity, wrong rate, wrong evidence
resolution", and it is the #1 cross-class mechanism.

## Where every job stands (best-known config vs target bid)

| # | Job | Target | KS best | Δ | Dominant gap |
|---|-----|--------|---------|---|--------------|
| 1 | Harlem (JW) | $43.5k | $36.4k | −16.2% | ceilings/doors dims on mega-sheet |
| 2 | Hudson (JW) | $146.0k | $129.1k | −11.6% | window counting from elevations (~$40k class) |
| 3 | Caris (JW) | $87.6k | $78.5k | −10.4% | door-count run variance (62–104 vs 75) |
| 4 | ULUM (JW) | $41.6k | $29.0k | −30.2% | floors policy (polished/epoxy RFI'd, JW absorbs) + trim |
| 5 | Homewood (JW) | $1.323M | $1.121M | −15.3% | WC per-unit-typical transfer |
| 6 | Dutchess | $22.8k* | $60.1k | +164% | $34.6k unevidenced exterior (fix built); int-only = +11.6% |
| 7 | Fishkill | $129.4k | $108.5k | −16.2% | int +29% (stairs 2×, doors −29%) MINUS missing ext $45.6k |
| 8 | 364 Main | $162.5k | $166.5k | **+2.5% ✅** | in band (A config; B needs allowance gating → +4.3% ✅) |
| 9 | TSC | n/a ($) | n/a | quantities | gyp 0 vs 5,447; CMU misses 8,900 SF EXTERIOR side |
| 10 | Honey | $28.6k | $47.8k | +67% | int walls 3.4× over-scope + ext qty 1.3×/rate 2.2× |

*Dutchess target ambiguity: xlsx sheet "New Finish Schedule Jan '26" sums
higher (~$26–35k parse-dependent) than the June-parsed $22,758 — re-derive
before final scoring (LaGrange-class revision question).

## Mechanisms, ranked by cross-job $ impact

**M1 — Exterior discipline (6+ jobs, ±$30–45k per job).** Three sub-parts:
- *Evidence resolution* — FIXED TONIGHT (worktree): per-item positive
  evidence (material+paint token co-occurrence, either word order — the
  old regex missed "siding painted PT01" and zeroed $15k of real Honey
  scope), negative factory-finish veto per item, finish-schedule text now
  in the evidence blobs, elevation-sheet guard, Will scope-removal
  exception. 24 checks green. Dutchess's $27k unevidenced Hardie now
  zeroes with RFI while painted AZEK survives; Honey's painted siding
  survives.
- *Quantity* — OPEN FEATURE: elevation pass emits LLM area estimates
  (Fishkill 14.8k vs Rider 6.2k SF = 2.4×; Honey 5.0k vs 3.9k = 1.3×).
  Needs measured basis: perimeter LF × eave height from plans/sections,
  or elevation-sheet geometry (VME-style), with the estimate demoted to
  RFI when no measurable basis exists. This is the exterior analog of
  the VME-authoritative-walls flip.
- *Rates* — CONFIG (Steven): one-size hardie $4.85/SF vs Rider's
  per-material ladder (V-groove $2.20, azek trim $9/LF, lintels $32/LF
  match!). Add siding-material rate keys; default conservatively.

**M2 — Integer-count run variance (doors, stairs, windows, ext items).**
Caris doors 2–104 across runs; Fishkill stairs 16 vs 8 (exactly 2× —
suspect floor-dup on stair sections); retest's elevation pass dropped
AZEK/cornice entirely. Fill-only consensus can't lower over-counts and
count paths bypass consensus. Fix: median-vote consensus specifically for
integer count fields + stair-section dedup check. This is now the
DOMINANT error class on otherwise-close jobs (Caris, Fishkill interior,
Hudson doors).

**M3 — Schedule-authoritative paint scope (interior).** Honey walls
15,686 SF vs Rider 4,580: Rider paints only the PT-01/PT-02 scheduled
areas; KS paints every wall. Dutchess ceilings +43–59%. TSC gyp/CMU
splits. The WC_SCHEDULE_AUTHORITATIVE pattern needs a paint-scope analog:
when a finish schedule enumerates painted areas (PT-xx per room/surface),
schedule quantities beat whole-room assumption; unscheduled surfaces →
$0 + RFI per hard-numbers policy.

**M4 — Class gates on JW-tuned flags.** CEILING_ASSUME_PAINTED excluded
on industrial/exposed-structure (TSC $16.9k dryfall). Allowance flags
(sealed concrete $15.3k + window sash $11k on 364; level-5; power-wash)
gated by bid convention — JW-class on, Rider-class off — OR kept
strikeable everywhere (POLICY, Steven).

**M5 — Named single-job features (already queued).** Hudson elevation
window counting; Homewood WC per-unit transfer; ULUM floors policy
(polished/epoxy pricing decision); Harlem mega-sheet dims via
room-geometry promotion; TSC exterior-CMU measurement.

## Per-job path to ±10%

1. **Harlem**: geometric ceilings from ROOM_GEOMETRY_SHADOW promotion +
   door dims. (M5; shadow corpus already collecting since 8/11.)
2. **Hudson**: elevation window counting (M5) + door median-vote (M2).
3. **Caris**: door median-vote alone (M2) — confirmatory run was −4.7%.
4. **ULUM**: floors policy decision (M4/M5) is ~$8–10k of the 30%; trim
   extraction the rest.
5. **Homewood**: WC per-unit transfer (M5); WC chain already correct.
6. **Dutchess**: per-item evidence fix (DONE) → ~+11.6%; then doors
   (11 vs 28, M2/M3) and windows (0 vs 25) close it. Re-derive target.
7. **Fishkill**: stair dedup (M2) + door recovery (M2) fix interior;
   exterior needs M1-quantity + the Hardie RFI answered (Rider bids it
   despite the factory-finish note — exactly what the RFI asks).
8. **364 Main**: DONE at A config; keep allowances gated (M4).
9. **TSC**: ceiling class gate (M4, built direction) + gyp-zeroing
   regression under B (investigate WC-deduct interaction) + exterior CMU
   (M1-quantity).
10. **Honey**: schedule-authoritative paint scope (M3) is the big one
    (walls 3.4×) + M1 quantity/rates on exterior.

## Build order (recommended)
1. PR the exterior evidence block (built + tested, worktree) — fixes
   Dutchess/Honey/Fishkill evidence resolution in one shot.
2. M2 integer-count median consensus + stair dedup — top variance killer,
   unblocks Caris/Hudson/Fishkill interior and the mandatory-review exit.
3. M4 class gates (ceiling occupancy gate + allowance convention flag).
4. M3 schedule-authoritative paint scope (feature, validate on Honey/TSC/
   Dutchess).
5. M1 exterior quantity measurement (feature; VME-for-elevations).
6. M5 singles in parallel as capacity allows.
Policy decisions needed from Steven: allowance convention (M4), ULUM
floors, Fishkill Hardie RFI, Dutchess target revision, exterior rate
ladder values.
