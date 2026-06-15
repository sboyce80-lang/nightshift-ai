# KnightShiftAI — Master Tracker (consolidated)

**Date:** 2026-06-14 · **Branch:** `phase0-review-fixes` · **Reconciles:** June-10 Code Review (55+ findings, Phase 0/1/2) + Phase 2.x status + Phase 3 VME
**Verification:** file:line spot-checks of the actual code, 2026-06-14 (read-only).

---

## ⚠️ HEADLINE: the work is done, but it isn't shipped

The single most important finding is exactly the risk the June-10 review warned about — *fixes coded but never shipped*. It is now **larger**, not smaller:

- **`phase0-review-fixes` is 47 commits ahead of `main`/`origin` (1 behind). NONE of the 47 review-fix commits are on main.** Production still runs the pre-review code.
- **No CI exists anywhere** (`.github/` absent). Nothing runs the test suite or the "golden-set accuracy harness." Tests are offline-only.
- **Three accuracy guarantees are inert by default even once merged:** per-sheet extraction (`NIGHTSHIFT_PER_SHEET_EXTRACTION=0`), provenance gate (`NIGHTSHIFT_PROVENANCE_GATE=0`), calibrated confidence (dormant: 4 of 8 golden rows).
- **Half-shipped v2 watchdog (care-point, blast radius bounded — corrected):** prod has **no working watchdog** at all (v1 was never deployed); the branch's **v2** (heartbeat + RQ cross-check) is coded but its heartbeat column (`alembic 0022`) + `_start_heartbeat` worker live **only on the branch**. The v2 code is safe-by-design (reaps only on RQ-inactive AND stale-heartbeat; degrades gracefully on NULL heartbeat). Wrong deploy order is **bounded**, not catastrophic: cron-before-migration → cron errors/no-ops; cron-after-migration-but-before-heartbeat-worker → healthy jobs running **>2h** (the `--legacy-stale-min` default) get failed. Safe order + dry-run first: **`docs/DEPLOY_WATCHDOG_RUNBOOK_2026-06-14.md`**. Independent of the golden regression — can ship via cherry-pick ahead of the extraction merge.

**Implication:** the next phase is not more fixes. It is a **controlled merge → migrate → deploy → enable-flags → build-CI** sequence, in dependency order.

---

## Act-this-week criticals (from the review's Executive Summary)

| # | Item | Status | Evidence |
|---|---|---|---|
| B1 | Stranded Ridgeview floor-parse fix (1.65× ceiling inflation) | ✅ **Fixed on branch, committed — NOT on main** | `_normalize_floor_key` Takeoff_DIRECT.py:5146, ordinal regex :5211; commit `b71c3f1`. main has only a partial form. |
| B2 | Unvalidated `confidence-room-recovery` branch | ⚠️ **Merged to HEAD, never live-validated** | top commit `63242b7` = "live validation blocked on API credits"; offline tests only; **no CI**. |
| B3 | Stuck-job watchdog deploy-as-written would reap healthy jobs | ⚠️ **v2 coded + wired on branch; prod still v1, heartbeat backing absent on main** | `scripts/sweep_stuck_jobs.py` v2, cron in branch `render.yaml:109`; `_start_heartbeat` jobs.py:119 + `alembic 0022` branch-only. |

---

## Findings by failure mode (§1–§6)

Status legend: ✅ Fixed · 🟡 Partial/gated · 🔴 Open · 💤 Dead-code landmine. "Gated" = behind an env flag or `HARD_NUMBERS_ONLY`.

### §1 Dropped files / pages / chunks
| Finding | Status | Evidence / note |
|---|---|---|
| 1a transient blip discards page, chunk marked OK | ✅ | `_retry_chunk_without_bad_pages` :925; dropped pages marked `failed` in ledger :6962; blocks auto-send. |
| 1b failed chunk shifts later page attribution | ✅ | `_merge_chunk_responses` indexes by true chunk # :5429. |
| 1c fallback hard-caps at 12 plan sheets | ✅ | cap removed :4471; optional `NIGHTSHIFT_MAX_TILE_PAGES` cut is recorded as `failed`, not silent. |
| 1d "PT-101 PAINT PLAN" → Plumbing → dropped; multi-file tracking | 🟡 | multi-file tracking **fixed** (:18207). **Still open:** "PT" matches `P`(Plumbing,exclude) :1762; Division-9 keyword rescue :1429 does NOT include "paint plan" → a bare PT paint sheet can still be excluded. |
| 1e failed file skipped silently; locked files dropped | ✅ | `files_skipped` now forces manual review + RFI :18770; `PdfPasswordLockedError` :pdf_preprocess.py:85. |

### §2 Run-to-run variance
| Finding | Status | Evidence / note |
|---|---|---|
| 2a consensus only fires if pass-1 ≥20 rooms (53-vs-15) | ✅ | gate now keys on deterministic page classification :18531, not pass-1 room count. |
| 2b truncation undetected (`stop_reason` unread) | ✅ | `TruncatedResponseError` on `max_tokens` :281, 7 call sites; page-by-page retry. |
| 2c majority-vote deletes rooms (A-102 vs A1.02) | ✅ (active path) / 💤 | `_canonical_room_key` excludes sheet from identity :3161. **Landmine:** legacy `_merge_passes_with_median` :14962 still uses raw `.upper()` sheet key — reintroduces the bug if the non-per-sheet fallback runs. Delete or route through canonical key. |
| 2d ~200 calls/job, zero prompt caching | 🟡 | caching **fixed** (`_PROMPT_CACHE_CONTROL` :257, applied 5 paths); call-volume reduction is structural (per-sheet replaces 3×), not an explicit cap. |

### §3 Fabricated / inflated scope — *all neutralized only by `HARD_NUMBERS_ONLY=True` (config.py:456); none removed*
| Finding | Status | Evidence / note |
|---|---|---|
| 3a invented "typical unit" rooms (+45% wall) | 🟡 gated | suppressed→RFI under policy :9668; full path runs only if flag False. |
| 3b wall validator ×1.60 from 1-job ratio | 🟡 gated | Mode-2 footprint boost suppressed→RFI :9870; Mode-1 still scales walls from *measured* perimeter only, no longer trim. |
| 3c dryfall = 75% footprint, ungated copy | 🟡 gated | now gated :7755; captures+RFI under policy. |
| 3d prompt defaults disguised as measurements | 🔴 **OPEN** | prompt still emits "CMU silent→paintable" :5815, "exposed→DRYFALL" :5832, "150-200 doors" :6151; the gate that would catch them (`PROVENANCE_GATE`) is **off by default**. |
| 3e "2 stairwells"→$18k; "28 units"→×28 | 🟡 | stairwell **fixed/gated** :19151; **unit-multiplier note-parse still ungated** :11156 (`"28 units total"`→×28; schema field preferred but note-text is live fallback). |

### §4 Dedup & naming
| Finding | Status | Evidence / note |
|---|---|---|
| 4a Ridgeview ordinal floor-parse never shipped | ✅ | `_normalize_floor_key` :5202, wired into dedup :12501. (= B1; on branch only.) |
| 4b stale "already deduped" flags survive re-runs | ✅ | re-run path pops dedup flags :17198. |
| 4c four sheet conventions; A-101A→nothing, A2.01a→"A2." | 🟡 | canonical `_normalize_sheet_token` added & used widely :2003, **but detection regex still fails revision suffixes** — `A-101A`→no match, `A2.01a`→`A2`. |
| 4d RCP/ceiling dedup skips on blank building_type; asymmetric vote | 🟡 | asymmetric vote **fixed** (symmetric + ceiling backfill :12560); **blank building_type still bails entirely** :12453. |
| 4e room dedup both directions; >3000 SF guard dead | ✅ | `building` in key :3182; >3000 guard now reads correct key & fires :10841. |

### §5 Confidence, pricing & traceability
| Finding | Status | Evidence / note |
|---|---|---|
| 5a confidence is self-reported; quality score counts warnings | 🔴 **OPEN/Partial** | new evidence-derived `confidence.py` built & on by default, **but display-only & dormant** (4/8 cal rows → `calibrated=False`). **Customer email still shows Will's self-reported `level_pct`** :email_processor.py:427; `ready_to_send` still keys off it; `data_quality_score` still a warning-counter :16405. |
| 5b customer rate overrides overwritten by building-type rates | ✅ | `_rate_locked()` guard :15455 across all building types. |
| 5c-i fractional qty at tier boundary → cheapest tier | ✅ | half-open ranges :15278. |
| 5c-ii wallcovering $9.00 = SF/SY transposition | ✅ resolved non-bug | kept $9.00/SF per Rider Mazda (intentional). |
| 5d boosts/caps/edits leave no record (last 30% of chain) | ✅ record / 🟡 enforce | `_quantity_adjustments` ledger records every mutation unconditionally :12649; the strict gate that *removes* assumed increments is `PROVENANCE_GATE`-off-by-default. |

### §6 Job pipeline reliability
| Finding | Status | Evidence / note |
|---|---|---|
| 6a OOM → "processing" forever; recovery weak | ✅ code / ⚠️ deploy | `reconcile_abandoned_submissions` :310, `_start_heartbeat` :119, v2 watchdog cron; **no RQ auto-`Retry()`**; **deploy state diverged (see headline).** |
| 6b browser-upload never counts pages → OOM worker | 🟡 | enqueue still routes browser-direct blind (`total_pages=0`) :web_app.py:879; **mitigated** by worker-side `_reroute_to_heavy_if_misrouted` :jobs.py:193 before API spend. |
| 6c email sends before completion; double-send; review jobs vanish | ✅ | status persisted before email; atomic `claim_email_send` :260; `needs_review` surfaced in UI :web_app.py:1409. |

---

## The 4 Guarantees / Phase 0-1-2 (review's resolution plan)

| Guarantee / layer | Coded? | Default | On main? | Note |
|---|---|---|---|---|
| **G1** Coverage Ledger + blocking gate | ✅ `CoverageLedger` :410, `_apply_coverage_gate` :536 | on | ❌ | `unaccounted==0` assert is a "target," not yet enforced. |
| G1 L1 per-sheet + structured outputs | ✅ | structured **on**; per-sheet **off** | ❌ | centerpiece extraction is off by default. |
| G1 L2 retry taxonomy | ✅ :925 | on | ❌ | |
| G1 L3 fallback ladder ("5 routes") | 🟡 | n/a | ❌ | routes exist (render/tile/jpeg/multimodal) but **no unified "5 independent routes per page"** abstraction — ad-hoc. |
| G1 L4 preflight at upload | 🟡 | n/a | ❌ | decrypt + reroute present; full count/render-before-spend partial. |
| G1 L5 inclusion-biased filtering | ✅ | on | ❌ | (but see 1d PT gap.) |
| G1 L6 failure≠finished / resume | ✅ | on | ❌ | depends on heartbeat (alembic 0022, branch-only). |
| **G2** anchored per-sheet + verification vs 3× consensus | ✅ | **off** (`PER_SHEET=0`) | ❌ | inert in prod until flag on. |
| **G3** provenance-tagged + single pricing gate | ✅ | **off** (`PROVENANCE_GATE=0`) | ❌ | observability ledger on; strict removal gate off. |
| **G4** calibrated confidence + feedback + CI harness | 🟡 | on but **dormant** | ❌ | `MIN_CALIBRATION=8`, have **N=4**; **no CI** anywhere. |

---

## Phase 2.x ↔ review mapping + Phase 3 VME placement

- Review **Phase 2 (±10% architecture)** = our **2.x**: 2.2 per-sheet (+ image-only $0 fix, this session, **uncommitted +65 lines**), 2.3 provenance/Trust Summary, 2.4 calibrated confidence, 2.1 golden consolidation (the N≥8 unlock).
- **P2-G** (base trim + small-commercial floor dedup): ✅ locked in this session (all 4 checks pass).
- **Phase 3 VME** (vector measurement engine): de-risked this session (deterministic geometry reproduced golden within 2% on 364). **Partially supersedes G2** (replaces vision-anchored extraction with line measurement) for vector sets — which is 100% of sampled bid sets. G1/G3/G4 remain necessary and feed off VME's `measured` provenance.

---

## Recommended priority order (ship-first, then accuracy)

1. **De-risk the deploy, in this order:** merge `alembic 0022` + heartbeat worker code → THEN `render.yaml` v2 watchdog. Never the cron before the migration. (Resolves B3 landmine.)
2. **Stand up minimal CI** running the offline test suite + golden harness on push. (Resolves "no CI"; lets B2 actually validate.)
3. **Controlled merge of the 47 commits to main**, behind the existing default-off flags, with the golden regression as the gate (currently running for baseline).
4. **2.1 golden consolidation to N≥8** → activates G4 calibration and 5a's honest confidence; also the prerequisite for Phase 3 VME accuracy bars.
5. **Close the residual code-open items:** 3d (prompt defaults / turn on provenance gate), 3e unit-multiplier note-parse, 1d PT-paint-plan exclusion, 4c revision-suffix regex, 4d blank-building_type dedup, 2c legacy median-merge landmine.
6. **Phase 3 VME** build (M0→M5) as the truest-number engine for vector sets.

**Bottom line:** the accuracy work is largely *built*; the dominant risk is *operational* — 47 unshipped commits, no CI, three flags off, and a half-shipped watchdog. Shipping discipline is now the gating constraint, not more code.
