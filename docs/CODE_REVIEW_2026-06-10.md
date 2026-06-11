# KnightShiftAI — Full-Pipeline Code Review

**Date:** 2026-06-10
**Scope:** End-to-end audit of the takeoff pipeline on branch `confidence-room-recovery` (HEAD 63242b7): ingest/chunking, extraction, multi-pass consensus, dedup/naming, hard-numbers policy, pricing, confidence, traceability, output generation, and job orchestration.
**Method:** Six parallel deep-dive audits (Software Engineer + QA lens), each tied to a reported customer failure mode. All findings carry file:line evidence; nothing below is speculative.

---

## Executive summary

The pipeline's bones are good — temperature-0 everywhere, room-level provenance (`source_sheet`/`source_page` + bbox anchoring at ~76% coverage on real jobs), an annotated-PDF trust artifact, a real hard-numbers flag, and pure recomputed aggregation. But the five customer-reported failure modes are all real, all reproducible in the code, and they share three root causes:

1. **No invariants, only heuristics.** Nothing asserts "every page is accounted for," "every quantity has a measured source," or "every status transition is legal." Every safety mechanism is a scattered, individually-bypassable check.
2. **The consensus machinery amplifies noise instead of damping it.** The multi-pass system is gated on its own random output, silently drops truncated responses, and its majority-vote merge structurally deletes real rooms.
3. **Identity is string-luck.** Rooms, sheets, and floors are matched by raw LLM-emitted strings normalized four different incompatible ways.

**Most urgent single finding:** the Ridgeview ordinal-floor-parse fix (the 1.65× ceiling-inflation fix) **never reached this branch or main** — it is stranded on `backup/local-wip-2026-05-29`. The shipped `_parse_floor_range` fails 9 of 18 cases pinned by `verify_ridgeview_dedup.py`. That regression is live today.

**Second most urgent:** the recovery branch you're standing on is **live-unvalidated** — `validate_recovery_summary.json` contains only two API-credit errors; the validation run never executed.

---

## Part 1 — Why files/pages/rooms get dropped (failure mode #1)

There is no end-to-end coverage ledger. Pages exit the pipeline through ~10 independent drop points, most recorded only as `print()` statements.

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| 1.1 | CRIT | `page_offsets` are positionally misaligned when any chunk fails or fails to parse — every room after the failure gets the wrong `source_page`, which corrupts the very checks (Check 8, sheet coverage) meant to detect lost pages | `Takeoff_DIRECT.py:3317-3339, 4709-4710` |
| 1.2 | CRIT | `_retry_chunk_without_bad_pages` permanently drops pages on **transient** errors (network blip during the per-page probe ⇒ page gone forever); dropped pages recorded nowhere machine-readable; chunk still marked "succeeded"; surviving pages renumbered so offsets drift | `Takeoff_DIRECT.py:359-491` (esp. 380-382, 415-419) |
| 1.3 | CRIT | `merge_analyses` builds a fresh dict that never copies `_chunk_tracking` — on every multi-file job, chunk-failure detection, Check 4/8, and the ≥50%-failed trigger all silently see nothing | `Takeoff_DIRECT.py:10972-10997, 15151-15156` |
| 1.4 | CRIT | Enhanced (tiled) extraction hard-caps at 12 plan pages (`NIGHTSHIFT_MAX_TILE_PAGES`); pages 13+ are dropped, not batched — despite a batching loop existing right below | `Takeoff_DIRECT.py:2486-2493` vs 2552-2554 |
| 1.5 | CRIT | Discipline classification excludes via `startswith` on single letters: `PT-101 Paint Plan`→Plumbing, `SF`→Structural, `EQ`→Electrical, all excluded; `FP` hard-mapped to Fire Protection though the code's own docstring uses `FP-101-LEVEL-1-PLAN.pdf` as a floor-plan example | `Takeoff_DIRECT.py:847-878, 1127-1163` |
| 1.6 | HIGH | A failed chunk only blocks the job at ≥50% chunk failure; with 2-page chunks on large-format sets, one failed chunk = 5% — but those 2 pages can be *the* floor plans (the Aliante/Wingstop incident class). The `fix/sheet-coverage-threshold` branch further weakens the coverage trigger (60%→30%) | `Takeoff_DIRECT.py:4704-4706, 8661-8669, 15712-15722` |
| 1.7 | HIGH | `files_skipped` (file failed after 3 retries, job continues) is written into the JSON and **read by nothing** — no manual-review flag, no RFI, no email mention. A 3-file upload missing 1 file ships a normal-looking proposal | `Takeoff_DIRECT.py:13565-13569, 15115-15126, 16069` |
| 1.8 | HIGH | Password-locked PDFs in a mixed upload are silently dropped (only errors if *all* are locked) | `jobs.py:369-390, 632-644` |
| 1.9 | HIGH | `_split_pdf_from_plan` skips corrupt pages (`except: pass`) and empty chunks with no record; chunk-id ledgers then disagree about page ranges | `Takeoff_DIRECT.py:346-356` |
| 1.10 | HIGH | Sheet-coverage trigger has structural blind spots: needs ≥4 denominator sheets (most retail jobs have 1–3), and is bypassed entirely when zero rooms have `source_sheet` — the worst case is exempt | `Takeoff_DIRECT.py:15609-15710` |
| 1.11 | HIGH | `analyze_schedule_pdf` reads only chunk 1 of large PDFs — schedules on later pages never read; door/window counts silently partial | `Takeoff_DIRECT.py:4781, 4870-4896` |
| 1.12 | HIGH | Results cached *before* partial-extraction detectors run; cache-hit path re-serves incomplete results unflagged (CLI/cache-enabled runs); `_code_hash` ignores `pdf_preprocess.py` | `Takeoff_DIRECT.py:15527-15541, 14368-14479, 189-197` |
| 1.13 | MED | Cross-chunk floor merge drops loser rooms with no `room_id` (`if not rid: continue`) and collapses distinct rooms sharing an id; chunk-context prompt tells the model to SKIP already-seen room_ids — suppressing legitimate repeats across buildings | `Takeoff_DIRECT.py:3388-3407, 4619-4628` |
| 1.14 | MED | Check 7 (zero-room plan files) keys on `_is_floor_plan_file(filename)` which the code itself admits never matches real customer filenames | `Takeoff_DIRECT.py:8740-8758` vs 14908-14916 |
| 1.15 | MED | Exception cleanup `os.unlink(cp)` on tuples — always throws, swallowed; chunk temp files leak on error paths (disk pressure on the OOM-prone fast worker) | `Takeoff_DIRECT.py:4764-4769` |

### Fix: the Coverage Ledger (structural, ~zero API cost)

Every page of every uploaded PDF must end the run in exactly one accounted state, asserted at the end:

1. At intake in `run_analysis`, build `{file, sha256, total_pages, pages: {idx: state}}` with states `MEASURED | EXCLUDED(reason) | DEGRADED(reason) | FAILED(reason)`, initialized `UNACCOUNTED`.
2. Convert all ~10 drop points above from `print()` to ledger writes.
3. Fix the two ledger-corrupters first (1.1, 1.2's renumbering) — carry `(chunk_idx, page_list, text)` triples through the merge.
4. End-of-run assertions: `UNACCOUNTED == 0` (raise — a violation means a new leak); any plan-classified page in `FAILED`/wrongly-`EXCLUDED` ⇒ `manual_review_required` + pre-pricing RFI naming the exact sheets. This one gate subsumes 1.6–1.8, 1.4, and the Trigger-2 blind spots, and survives multi-file merges (fixes 1.3 structurally).
5. Persist the ledger into result JSON and render one line in the proposal: "120 pages: 96 measured, 21 excluded (MEP/structural), 3 FAILED — see RFI."

### Prevention design: six layers that make FAILED rare, and never silent

The ledger is detection + a blocking gate (a job with any FAILED plan page cannot auto-send — it lands in `needs_review` naming the sheet, it does not ship short). But the goal is to make FAILED essentially never happen. Most of today's "failures" are recoverable events the current code treats as permanent:

**Layer 1 — Eliminate the two biggest failure causes by construction.**
- *Truncation:* a 5–8 page chunk can emit more JSON than 64K output tokens; the response cuts off mid-array and the whole chunk is discarded. **Per-sheet extraction makes truncation structurally impossible** — one sheet's rooms never approach the limit.
- *Parse failures:* the `re.search(r'\{.*\}')` + `json.loads` lottery at 12 sites. **Structured outputs (tool-use JSON schema)** make responses schema-valid by construction; the API retries malformed output before the pipeline ever sees it.
These two convert the majority of historical chunk failures into non-events.

**Layer 2 — Correct retry taxonomy: transient ≠ permanent.** The worst single bug (`Takeoff_DIRECT.py:415-419`) marks a page "bad" on *any* exception — a 2-second network blip permanently removes a page from the takeoff. Transient errors (429/500/overloaded/connection reset) must retry with backoff within the job timeout; only a true `BadRequestError` on page content means "this representation can't be sent." API transients are near-100% recoverable on retry; correct classification alone eliminates almost all remaining drops.

**Layer 3 — Fallback ladder: every page has multiple independent representations.** All five rungs already exist in the codebase; they are simply not connected (a failed single-page chunk returns `None` and the ladder is never climbed):
1. Native PDF chunk (primary)
2. Page rendered as high-DPI image → `_analyze_page_multimodal` (:1957, today only used for 413 errors)
3. Re-render at different DPI/quality (exists in image fallback)
4. Tiled extraction for large-format sheets → `_tile_page` (:1677)
5. Rasterize-and-rebuild via PyMuPDF for pages PyPDF2 can't copy (`pdf_preprocess.py` already does this for oversized pages — a "corrupt" page in one library usually renders fine in the other)
A page reaches FAILED only after all five methods are exhausted — in practice, genuinely unrenderable garbage.

**Layer 4 — Preflight at upload.** Before any API spend: open every file, count pages, render every page to a thumbnail, detect encryption. Seconds, free. A password-locked file (today silently dropped, `jobs.py:369`) becomes a submit-time prompt for the password; a corrupt page is reported while the customer is still at their desk. This moves the only truly unrecoverable failure class to the one moment a human can fix it.

**Layer 5 — Misclassification: bias toward inclusion.** The economics are asymmetric — analyzing an extra MEP sheet costs one cheap call; dropping a paint plan costs a blown bid. Exclude only on *exact* discipline-prefix match with a non-plan title; everything ambiguous gets measured. Remove the 12-page tile cap by batching (the loop already exists at :2552).

**Layer 6 — Job semantics: failure means "not finished," never "finished minus pages."** Per-sheet extraction makes each sheet an independent, checkpointable unit: a sheet that exhausts its ladder retries alone (on the heavy worker if needed); a worker OOM resumes from checkpoints instead of restarting; only after bounded retries does the job land in `needs_review` with the sheet named — blocked by the ledger gate, never shipped short.

Stacked: Layers 1–2 remove ~90% of historical failures by construction; Layer 3 recovers nearly all genuine page problems; Layer 4 catches unrecoverable ones before money is spent; Layer 5 stops "lost by filter"; Layer 6 guarantees whatever survives results in a *blocked* job. The ledger is then the audit proving the layers worked — and the catch-all for code paths added next year. Layers 2, 4, 5 and connecting the ladder rungs are small surgical changes landable in Phase 0/1; Layer 1/6 (per-sheet) is the Phase 2 anchor that also fixes variance and cost.

---

## Part 2 — Why runs swing 53 rooms → 15 rooms (failure mode #2)

Temperature is 0 at all 15 LLM call sites; no `top_p`, no seed. The residual noise source is the vision encoder itself (the code documents 510/264/83 rooms across three identical runs at `Takeoff_DIRECT.py:11371-11374`). The pipeline then **amplifies** that noise:

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| 2.1 | CRIT | Multi-pass consensus only fires when **pass 1** found ≥20 rooms (`NIGHTSHIFT_MULTI_PASS_MIN_ROOMS`). A pass-1 undercount of 15 rooms skips consensus entirely and ships as final — the variance fix is gated on a sample of the variable it's supposed to stabilize. This alone reproduces 53-vs-15 | `Takeoff_DIRECT.py:14917-14923` |
| 2.2 | CRIT | Zero truncation detection: `stop_reason` is never read anywhere in 16,281 lines. A response hitting 64K max_tokens mid-JSON is marked "succeeded" (success recorded before parse), then dies silently in `_merge_chunk_responses` (`except JSONDecodeError: pass`). An entire chunk's floors vanish with no log, and `chunks_failed` under-reports — corrupting the pass-ranking fallback too | `Takeoff_DIRECT.py:4633-4637, 3317-3327` |
| 2.3 | HIGH | `min_pass_presence = max(2, ceil(N/2))`: a real room seen in 1 of 3 passes is deleted; when one pass fails (allowed), N=2 ⇒ **intersection** of two noisy samples. The code's own comment admits it "structurally discards real rooms." Union mode exists (`NIGHTSHIFT_MERGE_UNION=1`) but defaults off | `Takeoff_DIRECT.py:11463-11475, 14984-14994` |
| 2.4 | HIGH | Merge key `(floor_name_norm, source_sheet.upper(), room_name_norm)` is brittle: `A1.02` vs `A-102` vs `A101` are three keys (the canonical `_normalize_sheet_token` exists but isn't used here); the docstring's claimed floor-name collision is false. Documented result: Ridgeview 2026-05-29 — 54 candidate rooms, **0 matched across passes**, all dropped, then a luck-of-the-draw fallback ships one whole pass (tie-break prefers *fewer* rooms) | `Takeoff_DIRECT.py:11485-11494, 11544-11550, 11576-11681` |
| 2.5 | HIGH | Within-pass false consensus: two distinct rooms named "Storage" in ONE pass count as `len(instances)=2`, satisfying the 2-pass presence rule with zero cross-pass confirmation — and then collapse into one room whose dims are a median of two different physical rooms. Every plan with repeated generic names (Closet, Stair, Corridor) is undercounted by the merge itself | `Takeoff_DIRECT.py:11485-11532` |
| 2.6 | MED | `_median_num` filters zeros before the median: `median([0,0,800]) → 800` — a field two passes agreed was 0 takes the lone outlier | `Takeoff_DIRECT.py:11399-11410` |
| 2.7 | MED | Chunk/batch floor merges are winner-take-floor by wall area with `room_id`-keyed loser re-merge — double-counts mismatched floor keys, drops id-less rooms, order-sensitive template resolution | `Takeoff_DIRECT.py:3344-3479, 3175-3260` |
| 2.8 | MED | Pass-mode pinning (passes 2..N reuse pass 1's extraction mode — the Wingstop fix) exists on this branch but may not be deployed; pre-branch prod produces exactly the reported signature | `Takeoff_DIRECT.py:14953-14977` |

### Cost structure

~65–75 API calls per run for a representative 100k SF set (20 chunks × 3 passes + pre-scans + retries), plus 20–30 min of pure `time.sleep`. The customer-side "run it 3×" workaround ⇒ ~200 calls/job. **No prompt caching anywhere** — the multi-thousand-token static prompt and identical PDF chunks are re-sent verbatim every pass.

### Fix: determinism design

1. **Read `stop_reason` on every stream; on `max_tokens`, split & re-request; on parse failure, mark the chunk failed and retry once.** (~30 lines; kills the biggest silent variance source.)
2. **Structured outputs / tool-use JSON schema** for extraction — eliminates the `re.search(r'\{.*\}')` + `json.loads` lottery at all 12 parse sites.
3. **Anchor room identity in the deterministic text layer.** `_extract_page_text_layer` already extracts room labels at zero API cost; match passes on `(canonical_sheet_id, text-layer room label/bbox)`, not free-text names.
4. **Per-sheet extraction** (1 call per plan sheet): small outputs (no truncation), independently retryable, cacheable; merge becomes a deterministic union keyed by sheet.
5. **Replace 3× consensus with 1 extraction + 1 verification pass** ("here are the extracted rooms with anchors; list labeled spaces on this sheet that are missing, and extracted rooms with no visible anchor"). Additive-with-evidence instead of majority-vote-deletion; ~⅓ the cost.
6. Interim one-liners on current machinery: gate multi-pass on document signals (plan sheets detected), not `best_rooms >= 20`; compute presence over *distinct passes*; key duplicate names per occurrence; normalize the sheet component of the merge key with `_normalize_sheet_token`; consider `NIGHTSHIFT_MERGE_UNION=1`.
7. **Add `cache_control`** to the document + static prompt prefix (≈90% input-cost cut on passes 2..N); kill the customer-side 3× rerun once repeatability is demonstrated.

---

## Part 3 — Where scope gets fabricated (failure mode #3)

`HARD_NUMBERS_ONLY=True` (config.py:456) gates ~12 fabrication paths — but at least **9 ungated paths remain**, and the prompts themselves still instruct the model to assume scope. Enforcement is ~15 scattered `if not HARD_NUMBERS_ONLY` guards with **no central gate before pricing**.

| ID | Sev | Finding | $ impact | Evidence |
|----|-----|---------|----------|----------|
| 3.1 | CRIT | `_supplement_missing_secondary_spaces` fabricates whole rooms (closets/halls from `SECONDARY_SPACE_TEMPLATES`, "derived from Edgehill validated data") when room density < expected; up to +45% of extracted walls. Ungated, called unconditionally | $10–20k/job | `Takeoff_DIRECT.py:7239-7395, 147-162, 15300` |
| 3.2 | CRIT | `_validate_and_boost_walls` multiplies walls **and trim** up to 1.60× from a footprint ratio calibrated on one job; the cap is raised by the *fabricated* F1 density signal — one heuristic licensing another. Ungated | $9–19k | `Takeoff_DIRECT.py:7398-7566` |
| 3.3 | CRIT | Dryfall Recovery Pass: `footprint × 0.75` dryfall from a finish callout — an exact ungated **copy** of a heuristic that IS gated 4,300 lines later. Direct proof the scattered-gate architecture leaks | $15–17k | `Takeoff_DIRECT.py:5434-5518` vs gated 9862-9880 |
| 3.4 | CRIT | `_estimate_from_room_finish_schedule`: entire takeoff from "typical construction" dimensions (`living: 18×14×9.5`...) × units × buildings when no plans extract. Ungated (own flag `ENABLE_SCHEDULE_ESTIMATION=True`) | Whole estimate | `Takeoff_DIRECT.py:6262-6713` |
| 3.5 | HIGH | Residential door supplement: up to +35% doors **above the authoritative door schedule**, using room counts the same function's docstring says are double-counted | $5–8k | `Takeoff_DIRECT.py:6878-6916` |
| 3.6 | HIGH | `_recalculate_totals` pre-pass fabricates perimeter, walls, and `base_trim_lf = est_perimeter` for floor-area-only rooms; on residential the trim is priced | varies | `Takeoff_DIRECT.py:9408-9428` |
| 3.7 | HIGH | A note saying "2 stairwells" becomes `2 × (levels−1) × 2` sections @ $1,500 — $18k on a 4-level building from a sentence; bypasses the gated geometry default | up to $18k | `Takeoff_DIRECT.py:15387-15413` |
| 3.8 | MED | Prompt defaults (invisible — arrive as "measurements"): CMU specs-silent → paintable; commercial exposed ceilings → DRYFALL by default; residential ceilings → painted; blank door material → full paint; "20-unit building typically has 150–200 doors — if fewer, re-check"; "if wall LF under 1,500 you are UNDER-MEASURING. Go back"; stair/cornice "estimate" hints | large, invisible | `Takeoff_DIRECT.py:3660, 3677, 3679, 3868, 3880-3885, 3996, 4045-4062; 2787-2870` |
| 3.9 | MED | Corrections autoload: `_load_corrections` defaults to `corrections.json` next to the module — any file there silently applies to **every job on the worker** (fmliving sets ×8 multipliers) | per-file | `Takeoff_DIRECT.py:8397-8514` |
| 3.10 | MED | `_extract_multiplier_from_notes`: "28 units total" in a free-text note ⇒ quantity ×28 (cap ×500), no schema field, no cross-check | extreme tail | `Takeoff_DIRECT.py:8517-8545` |
| 3.11 | MED | `_detect_answered_topics` marks a topic "answered" whenever its total is non-zero — including boosted/fabricated totals — suppressing the RFIs that should have fired | trust | `Takeoff_DIRECT.py:10520, 10576` |

Asymmetry note: nearly every "safety net" pushes quantities **up**; only caps and a few exclusions push down. Mitigation that works and must be kept: the gated fallbacks list (9655, 9769, 9888, 9952, 9993, 10030, 12079, 12335, 12474, 15425), the base-trim gate (9536/9215), whitebox/window exclusions.

### Fix: provenance-tagged quantities + one gate

1. Replace scalar quantities with records `{qty, source, basis, ref}`, `source ∈ {measured, schedule, derived, assumed, manual_override}`; `derived` with any `assumed` parent is `assumed`.
2. LLM contract: every quantity gets a sibling `src` field ("callout|scaled|schedule|assumed"); delete every prompt default listed in 3.8 and replace with "set 0 + note 'unconfirmed — RFI'".
3. **Single choke point** `build_priced_takeoff(analysis)` before `calculate_costs`: under `HARD_NUMBERS_ONLY` it drops every `assumed` record to 0 and registers an RFI carrying the would-have-been qty as "unpriced exposure." Heuristics keep running — but they can only emit `assumed` records, so they become RFI generators automatically. A bare float written into `aggregated_totals` raises.
4. Estimate output prints source mix per line ("Gyp Walls — 38,400 SF measured + 0 assumed") and an "Unpriced exposure" section ("Base trim: 0 LF priced — 848 LF unconfirmed, see RFI #3"). This preserves the sales value of the heuristics without pricing them.
5. Tactical now: gate 3.1/3.2/3.3/3.5/3.6/3.7; require explicit corrections path; fix 3.11 to require `measured|schedule` backing.

---

## Part 4 — Dedup & naming (failure mode #4)

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| 4.1 | CRIT | **Ridgeview ordinal floor-parse fix is not on this branch (or main)** — shipped `_parse_floor_range` regexes fail 9/18 cases pinned by `verify_ridgeview_dedup.py` ("2nd Floor"→∅, "Third Floor"→∅). Fixed parser exists only on `backup/local-wip-2026-05-29`; stranded during the 2026-05-28 rebaseline. The documented 1.65× ceiling inflation is reproducible today | `Takeoff_DIRECT.py:9039-9046` vs `verify_ridgeview_dedup.py:48-70` |
| 4.2 | CRIT | `_template_floors_deduped` / `_cross_sheet_rooms_deduped` idempotency flags persist into stored JSON; the incremental re-run path deep-copies the prior analysis ⇒ dedup **no-ops on all newly merged rooms** in v2 quotes | `Takeoff_DIRECT.py:9112, 9312, 13966, 14012` |
| 4.3 | HIGH | Four incompatible sheet-ID conventions coexist (canonical `_normalize_sheet_token`; dot-kept `f"{prefix}{number}"`; raw strip; raw `.upper()`). Regex gaps: `A-101A` revision suffixes don't match at all; `A2.01a` truncates to "A2"; `A-1021` invisible. Cross-pass `A-102` vs `A1.02` ⇒ different merge keys ⇒ room fails pass-presence ⇒ deleted — a plausible mechanism behind the June room collapse | `Takeoff_DIRECT.py:1287-1290, 1030-1140, 7808-8022, 11445-11490, 881-884` |
| 4.4 | HIGH | `_deduplicate_rooms` errs both directions: unit-key has no building/floor dimension (unit 301's bedroom in BLDG-1 and BLDG-2 merge to one); same-name+similar-area rooms collapse (two "Corridor"s); rooms with no `room_id` are silently discarded on name collision; winner-picking never reconciles `unit_multiplier` (can keep the ×1 instance and drop the ×3) — and the function runs only on multi-file jobs | `Takeoff_DIRECT.py:7789-8100 (7907-7968, 8093-8098)` |
| 4.5 | HIGH | Floor normalization split: chunk merge uses `_normalize_floor_key`, multi-file combine uses **raw string equality** ("1st Floor"/"First Floor"/"Level 1" = three floors). Inside the normalizer, `"two.?bed"` is a regex used as a literal substring (never matches), and worded-ordinal scanning maps "Twenty-Second" → 2 | `Takeoff_DIRECT.py:11061-11066, 3054-3081` |
| 4.6 | HIGH | Ceiling/RCP dedup (the Five Below fix) is gated to commercial-keyword + ≤1 unit + zero multipliers — unknown building_type gets **no RCP dedup**; the ceiling vote is asymmetric (only ever flips painted→False) and the keeper (max wall area = floor-plan instance) may carry `ceiling_area_sqft=0` while the zeroed RCP instance had the real area ⇒ **the ceiling vanishes entirely** on exactly the jobs the fix targets | `Takeoff_DIRECT.py:9285-9389 (9314-9334, 9359-9365)` |
| 4.7 | MED-HIGH | Template floors: Jaccard `> 0.5` strict boundary fails to merge `{2}` vs `{2,3}` (exactly 0.5) ⇒ double-count; conversely losing floors are dropped **wholesale** ("2nd Floor" common areas vs "2nd Floor - Typical Units" ⇒ one whole floor's scope deleted); `&` not in the range-regex class | `Takeoff_DIRECT.py:9099-9203 (9134, 9155)` |
| 4.8 | MED | `_audit_room_provenance` outlier check reads `room["wall_area_sqft"]` (top level) but the value lives in `room["dimensions"]` — always 0, so the >3,000 SF hallucination check **never fires**. Dead code | `Takeoff_DIRECT.py:8202-8229` |
| 4.9 | OK | `_recalculate_totals` aggregation itself is pure/recomputed — zeroed items can't resurrect; building multiplier not double-applied. (Caveats: the 3.6 pre-pass fabrication; overrides-then-recalc ordering is by convention only) | `Takeoff_DIRECT.py:9392-10136` |

### Fix: canonical identity

- **One sheet normalizer** (prefix 1–3 letters, optional separator, 1–4 digits with optional dot, optional revision letter → `A102`, `AD102`, `A101A`), stamped once as `source_sheet_canonical` at ingest; delete the other three conventions.
- **One room identity**: `(building_id, floor_key [ordinal-aware, ONE normalizer], room_number_or_unit, geom_hash [quantized area/perimeter/height or bbox centroid])` — sheet ID is provenance, **not** identity (that single change fixes the cross-pass drop and makes RCP/floor-plan instances collide by construction, replacing the gated heuristic 4.6).
- **Merge fields on collision** (max-detail per field, OR ceiling data from the instance that has it, reconcile multipliers with audit note) instead of drop-the-loser; log every merge.
- Immediately: cherry-pick the ordinal parser from `backup/local-wip-2026-05-29` and wire `verify_ridgeview_dedup.py` into CI; clear `_*_deduped` flags in `merge_versioned_analyses`.

---

## Part 5 — Confidence, pricing & traceability (failure mode #5)

### Confidence (CRITICAL, trust)

Three unrelated things are called "confidence," none calibrated:
- `data_quality_score` (`Takeoff_DIRECT.py:12717-12906`): `100 − 20×high − 10×medium` warning counts over ~8 anomaly patterns. Measures warning incidence, not error — a takeoff missing 40% of rooms that trips no pattern scores **100**. Internal only; rendered nowhere customer-facing.
- **Will's `level_pct`** (`will_synthesis.py:203-235`): an integer the LLM is *asked for* in the prompt — model-self-reported vibes. This is the number customers see (`email_processor.py:425-436`) and what gates `ready_to_send` (≥85).
- `SCHEDULE_ESTIMATION_CONFIDENCE = 0.85` (config.py:460): not a confidence — a 15% quantity derate, silently biasing schedule takeoffs low.

Nothing is calibrated against the ≥9 Rider-verified projects already in the repo (`generate_scorecard.py:34-44` is hardcoded prose). **The branch's own validation run never executed** (`validate_recovery_summary.json` = two API-credit errors).

**Fix — calibrated confidence.** The problem decomposes into two halves, and both are addressed by this plan:

*Half 1 — the pipeline has no evidence to compute confidence from.* You cannot compute honest confidence from a pipeline that loses information silently. Each structural fix converts an invisible failure into a measured confidence input:

| Today (invisible) | After (measurable confidence input) |
|---|---|
| Chunk fails → rooms silently vanish | Coverage ledger: **% of plan pages measured** |
| No idea if extraction found all rooms | Verification pass: per-sheet **measured recall** ("verifier found 2 labeled spaces on A-102 not in extraction") |
| Room is a free-text string | Anchor coverage: **% of rooms tied to a text-layer label** at a real sheet location (~76% on Wingstop today, computed but unused) |
| Boosts inflate quantities indistinguishably from measurements | Provenance gate: **% of priced dollars with source=measured/schedule** |
| Pass disagreement hidden by the merge | Pass/verifier agreement: CV on wall SF, room counts |
| Caps and Will edits mutate quantities invisibly | Adjustment ledger: **\|adjustments\| / subtotal** |

These six are deterministic, cheap, and each correlates mechanically with takeoff error.

*Half 2 — turn evidence into a calibrated percentage.* Consolidate the existing labels (≥9 Rider-verified projects, the Five Below/Damjan manual takeoff, tier-1 regression cases) into a `golden/` set with true per-job and per-metric errors. Calibration at this scale is binning, not ML (~50-line script): jobs with coverage=100%, anchor coverage >85%, verifier misses=0, assumed-$=0 empirically erred 3–8%; jobs with a failed sheet or anchor coverage <60% erred 15–40%. Report the 90th-percentile error of the matching bin: *"predicted error ≤ 9% at 90% confidence."* Refit whenever a verified takeoff lands.

Two properties make this trustworthy where the current number cannot be:
1. **Falsifiable and self-correcting.** Every customer/Rider correction auto-appends to `golden/` and the curve refits. One dashboard chart — "of jobs where we claimed ±10%, what fraction were?" — *is* the product claim. The answer to "why should I trust 92%?" becomes "on the last 30 verified jobs, our 90%+ scores were within 10% on 28," not "the model felt good."
2. **Hard gates are orthogonal.** Any failed plan page, missing footprint, zero walls, or manual-review flag *caps* displayed confidence (e.g., at 60%) regardless of the evidence score — the score can never be high-and-wrong for a reason already known.

Dependency note: the accuracy fixes (Parts 1–4) make high confidence *achievable*; calibration makes the reported number *honest*. Without the first, calibration honestly reports "predicted error 35%." Without the second, it's vibes. Both are required, which is why they share the roadmap.

### Pricing

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| 5.1 | HIGH | Building-type hardcoded rates **silently clobber org `rate_overrides`** (single-family/commercial/senior — i.e., most jobs); `_get_markup` likewise ignores per-item markup overrides | `Takeoff_DIRECT.py:12162-12265, 11954-11958` |
| 5.2 | MED | Tier-boundary gap: fractional quantities (3,499.5 SF) match no tier (`max 3499`/`min 3500` integer bounds) and fall into the **cheapest** tier via the fallback. Verified by execution | `Takeoff_DIRECT.py:11789-11805`, config.py:71-95 |
| 5.3 | MED | Rates resolved *before* sanity caps shrink quantities — capped 50,000→3,200 SF still prices at the ≥3,500 volume rate | `Takeoff_DIRECT.py:12165-12217` vs 12246-12316 |
| 5.4 | MED | Global `markup` override unconditionally overwrites per-item markup overrides, contradicting its own comment | `Takeoff_DIRECT.py:11780-11784` |
| 5.5 | MED | Will adjustments scale line items without re-tiering or writing back to `aggregated_totals` — PDF measurement tables and priced lines diverge (PDF reconciles walls/trim only, >4% gaps only) | `will_synthesis.py:524-595`, `json_to_pdf.py:922-970` |
| 5.6 | VERIFY | Wallcovering install $9.00/**SF** ≈ $81/SY vs industry $7–14/SY — likely SF/SY transposition, ~6–10× overprice. Verify vs the Mazda source takeoff | config.py:232-235 |
| 5.7 | OK | No PDF re-summation drift — both PDFs render `cost_estimate.subtotal` directly; line totals sum to subtotal by construction | `json_to_pdf.py:1069-1072`, `generate_estimate_pdf.py:355-457` |

Pricing/materials matrices live entirely in config.py (`PRICING_MODEL`, `SMALL_COMMERCIAL_RATES`, `PCA_CONSTANTS`); no DB-backed pricing; no separate materials matrix (baked into all-inclusive rates).

### Traceability

What works: schema-required `source_sheet`/`source_page`; deterministic bbox anchoring (`bbox_spike.py`) with 76% coverage on a real Wingstop job; annotated PDF with color-coded boxes and **EXTRACTION FAILURE** page banners wired in `jobs.py:200-260`; per-metric traceability tables in the estimate PDF.

What breaks: provenance dies at the aggregation→pricing boundary — boosts, caps, derates, footprint reconciliation, and Will edits mutate quantities with at best a prose note (~the last 30% of the chain). Plus the dead outlier audit (4.8) and first-match-wins anchor ambiguity (`bbox_spike.py:229-269`).

**Fix:** an append-only `quantity_adjustments` ledger — every mutator appends `{rule, item, from, to, basis, measurement_ids}`; line items carry `extracted_qty`/`priced_qty`/adjustment ids; the PDF renders the ledger mechanically for all items; add a one-page **Trust Summary** (anchor coverage, failed-sheet count, adjustment impact, calibrated confidence interval, open RFIs). Acceptance metric: % of priced dollars reachable measurement_id→room→anchored bbox (target ≥95% ledgered, ≥85% anchored).

---

## Part 6 — Job orchestration & reliability

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| 6.1 | CRIT | The undeployed watchdog would kill **live** jobs: it sweeps on `updated_at` staleness (30 min), but nothing updates the row while a job runs, and legitimate jobs run 30–90+ min. It also never touches Redis, so the swept job later completes and flips `failed → completed` (unguarded UPDATE) — customer gets "resubmit" then an estimate. **Do not deploy as-is** | `render.yaml:108-115`, `scripts/sweep_stuck_jobs.py:148-188`, `jobs.py:62-94` |
| 6.2 | CRIT | OOM-killed work-horse leaves the row at `processing` forever: reconciliation runs only **once at worker startup** and only for rows >4h old, and RQ's started-registry masks fresh deaths. No `Retry` configured — at-most-once on the most failure-prone path. This is the 2026-04-30 incident class | `jobs.py:100, 147-195, 536-552`, `worker.py:181-191` |
| 6.3 | HIGH | Browser-direct R2 manifest path never counts pages — routing falls back to **largest single file** size only; a 600-page vector set under 30 MB lands on the OOM-prone fast worker with a 1h timeout. The "660-page PDF always trips the MB threshold" comment is false for vector PDFs | `web_app.py:245-249, 877-879` |
| 6.4 | HIGH | Timeout inconsistency: `/resubmit` sizes the timeout on the **new files only** while re-running the whole project; `scripts/reenqueue.py` (the incident runbook path) flat 7200s kills re-runs of 4h jobs; `_pick_timeout` ignores the 3× multi-pass multiplier | `web_app.py:1324, 252-278`, `scripts/reenqueue.py:142` |
| 6.5 | HIGH | `needs_review` jobs vanish from the customer UI (matches neither "completed" nor "active" filter) and the detail page polls a frozen bar forever; manual-review email is best-effort | `web_app.py:1386-1391`, `templates/job_detail.html:261-262` |
| 6.6 | MED-HIGH | Estimate email sent **before** `status=completed`; warm-shutdown requeue then re-runs the job ⇒ second email, possibly with **different numbers** (multi-pass nondeterminism) — worst-case trust outcome. No `emailed_at` guard | `jobs.py:520-530`, `worker.py:93-163` |
| 6.7 | MED | `/prioritize` TOCTOU can double-run a submission (remove-then-enqueue races dequeue); `update_status` is a blind last-write-wins UPDATE with no transition guards | `web_app.py:2036-2048` |
| 6.8 | MED | render.yaml vs prod drift confirmed (cron absent; heavy plan pro_plus vs incident doc "4 GB Pro"; Redis/Streamlit/email_processor entirely out-of-band) — any Blueprint sync is a loaded gun until reconciled | `render.yaml`, incident doc |
| 6.9 | MED | Two shadow pipelines bypass the state machine: `email_processor.py` runs `run_analysis` inline in its IMAP loop (crash ⇒ duplicate run + duplicate reply); `streamlit_app.py` keeps a third file-based queue invisible to the DB | `email_processor.py:261-332`, `streamlit_app.py:80-167` |
| 6.10 | LOW-MED | `_update_progress` is dead code in prod (only Streamlit sets `_PROGRESS_FILE`); the web progress bar is a constant 55. The same checkpoints are exactly where the 6.1 heartbeat belongs | `Takeoff_DIRECT.py:111-127`, `templates/job_detail.html:262` |

### Fix: four additions, ~2–3 days, no new infrastructure

1. **Heartbeat** (½ day): `heartbeat_at` + `progress` columns; 60s ticker thread in `process_submission`; wire `_update_progress` to it. Real progress UI for free.
2. **RQ-aware watchdog** (½ day): reuse `reconcile_abandoned_submissions` logic as the cron body, every 10 min; reap on stale heartbeat + RQ cross-check, never wall-clock alone; cancel the RQ job before emailing.
3. **Idempotent completion + bounded retry** (1 day): upload results → rowcount-guarded `processing→completed` → email behind `emailed_at IS NULL` claim; then `Retry(max=1)` for infra failures, retry forced to heavy queue.
4. **Deterministic routing + timeout persistence** (½ day): page count at submit (pdf.js in manifest, verified server-side); route on `sum(bytes)`+pages; persist queue/timeout on the row; every re-enqueue path reads from it; in-worker guard re-enqueues heavy-class payloads off the fast worker.

---

## Prioritized roadmap

### Phase 0 — this week (small, surgical, high-leverage)
1. Cherry-pick the ordinal `_parse_floor_range` from `backup/local-wip-2026-05-29`; wire `verify_ridgeview_dedup.py` into CI (4.1).
2. Re-run `validate_recovery_run.py` — the branch is live-unvalidated (Part 5).
3. `stop_reason` check + treat JSON parse failure as chunk failure (2.2).
4. Fix `page_offsets` positional indexing (1.1).
5. Fix the dead provenance audit key (`dimensions.wall_area_sqft`) (4.8).
6. Gate the dryfall recovery copy, stair note-parsing, door supplement, secondary-space supplement, wall boost behind `HARD_NUMBERS_ONLY` (3.1–3.7 tactical).
7. Fix `_median_num` zero-filtering (2.6); clear `_*_deduped` flags on re-run merge (4.2).
8. Rate-override precedence over building-type hardcodes (5.1); half-open tier ranges (5.2).
9. Email-after-status + `emailed_at` guard (6.6). Surface `files_skipped` + locked files as manual review + RFI (1.7, 1.8).
10. Multi-pass gate on document signals, not `best_rooms >= 20` (2.1).

### Phase 1 — next 2–3 weeks (structural guarantees)
- **Coverage Ledger** with end-of-run assertions + customer-facing coverage line (Part 1 design).
- **Canonical sheet normalizer** stamped at ingest, used everywhere (4.3); ordinal-aware single floor normalizer (4.5).
- **Structured outputs** for all extraction calls (2 fix #2); prompt caching (`cache_control`).
- Heartbeat + RQ-aware watchdog + idempotent retry + deterministic routing (Part 6 design).
- Strip the fabrication prompts (3.8) — every default becomes "0 + RFI".
- Ceiling-vote asymmetry + keeper area backfill in RCP dedup (4.6).

### Phase 2 — next 1–2 months (the 90% architecture)
- **Provenance-tagged quantity records + single `build_priced_takeoff` gate** (Part 3 design) — makes hard-numbers structurally unbypassable.
- **Per-sheet extraction + verification pass** replacing 3× consensus (Part 2 design) — repeatable, cheaper, anchored in the deterministic text layer.
- **Adjustment ledger + Trust Summary page** (Part 5 design).
- **Calibrated confidence** from the consolidated `golden/` set, with the closed feedback loop on customer corrections.
- Golden-set accuracy harness in CI (promote tier-2 reference cases to tier-1; retire the hardcoded scorecard).

### Accuracy math — why this gets you to ±10%
The reported failures compound multiplicatively today: a dropped plan sheet (−10–30%), consensus deletion (−20–70% rooms on bad runs), fabrication boosts (+15–60% on walls/trim), duplicate template floors (+65% ceilings). No amount of tuning individual heuristics stabilizes a product of four unstable factors. The three structural changes — coverage ledger (nothing missing), anchored per-sheet extraction (nothing random), provenance gate (nothing invented) — each replace a class of heuristics with an invariant. The calibrated confidence interval is then honest by construction, and "±10% at 90% confidence" becomes a measurable, dashboard-able claim instead of a marketing number.
