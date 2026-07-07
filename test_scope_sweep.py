"""Tests for the flag-gated Scope Sweep (NIGHTSHIFT_SCOPE_SWEEP, default OFF).

The sweep is the reconciliation net for the keyword-gated pre-scans: a
low-cost LLM pass over painting-relevant pages the coverage ledger says were
never measured (excluded schedules/legends/notes, unaccounted pages), whose
findings are deterministically diffed against the priced analysis. It may
emit notes, has_*_schedule flag upgrades, and pre-pricing RFIs ONLY — never
quantities (hard-numbers policy: missing scope -> $0 + RFI).

Covers: flag gating, candidate selection from the ledger (measured/failed/
non-include pages skipped, keyword prioritization, page cap, no-text blind-
spot pages), the sweep call glue with a fake client (batching, provenance
stamping, JSON parsing), and every reconciliation branch (wallcovering /
finish-schedule flag upgrade / specialty / dryfall / exterior / alternates /
scope notes), including the no-new-discovery and already-priced suppression
paths and the quantities-never-mutated invariant. Offline, no API.
"""
import copy
import json
import os

# Default-off check must happen BEFORE anything sets the env var.
os.environ.pop("NIGHTSHIFT_SCOPE_SWEEP", None)

import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# ---------------------------------------------------------------------------
# Flag gating
# ---------------------------------------------------------------------------
check(T._scope_sweep_enabled() is False, "flag must default OFF")
os.environ["NIGHTSHIFT_SCOPE_SWEEP"] = "1"
check(T._scope_sweep_enabled() is True, "flag=1 must enable")

os.environ["NIGHTSHIFT_SCOPE_SWEEP_MAX_PAGES"] = "999"
check(T._scope_sweep_max_pages() == 40, "page cap must clamp to 40")
os.environ["NIGHTSHIFT_SCOPE_SWEEP_MAX_PAGES"] = "bogus"
check(T._scope_sweep_max_pages() == 12, "bad page cap must fall back to 12")
os.environ.pop("NIGHTSHIFT_SCOPE_SWEEP_MAX_PAGES", None)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------
FAKE_PDF = "/nonexistent/fake_set.pdf"

_page_states = {0: "measured", 1: "excluded", 2: "excluded", 3: "excluded",
                4: "failed", 5: "unaccounted"}
_page_text = {
    0: "FIRST FLOOR PLAN 12'-0\"",
    1: "ROOM FINISH SCHEDULE wall finish WC-1 vinyl wallcovering",
    2: "STRUCTURAL FRAMING",           # include=False below — must be skipped
    3: "",                              # raster page: no text layer
    4: "DOOR SCHEDULE fire rating",     # failed page — must be skipped
    5: "misc detail with no scope keywords at all",
}
_page_include = {0: True, 1: True, 2: False, 3: True, 4: True, 5: True}


def _fake_classify(pdf_path):
    return [{"page_index": i, "include": _page_include[i],
             "discipline": "Architectural", "sheet_number": f"A-{100 + i}"}
            for i in range(6)]


def _fake_text_layer(pdf_path, page_index):
    return {"raw_text": _page_text.get(page_index, "")}


def _make_ledger():
    led = T.CoverageLedger()
    led.files[os.path.abspath(FAKE_PDF)] = {
        "name": os.path.basename(FAKE_PDF),
        "total_pages": 6,
        "pages": {i: {"state": _page_states[i], "reason": ""}
                  for i in range(6)},
    }
    return led


_orig_classify = T._classify_pdf_pages
_orig_text_layer = T._extract_page_text_layer
try:
    T._classify_pdf_pages = _fake_classify
    T._extract_page_text_layer = _fake_text_layer

    cands = T._scope_sweep_candidate_pages([FAKE_PDF], ledger=_make_ledger())
    got_pages = [c["page_idx0"] for c in cands]
    check(0 not in got_pages, "measured page must not be a candidate")
    check(2 not in got_pages, "include=False page must not be a candidate")
    check(4 not in got_pages, "failed page must not be a candidate "
                              "(coverage gate already blocks)")
    check(5 not in got_pages, "text page with zero keyword score must be "
                              "dropped")
    check(got_pages == [1, 3],
          f"expected candidates [1, 3] by score, got {got_pages}")
    check(cands[0]["score"] > cands[1]["score"],
          "finish-schedule text must outrank blind-spot raster page")
    check(cands[1]["score"] == 1, "no-text page must score 1 (blind spot)")
    check(cands[0]["sheet"] == "A-101", "sheet id must come from "
                                        "classification")

    # Page cap
    os.environ["NIGHTSHIFT_SCOPE_SWEEP_MAX_PAGES"] = "1"
    capped = T._scope_sweep_candidate_pages([FAKE_PDF],
                                            ledger=_make_ledger())
    check([c["page_idx0"] for c in capped] == [1],
          "cap=1 must keep only the top-scored page")
    os.environ.pop("NIGHTSHIFT_SCOPE_SWEEP_MAX_PAGES", None)

    # No ledger -> no candidates (library use stays inert)
    check(T._scope_sweep_candidate_pages([FAKE_PDF], ledger=None) == []
          or T._COVERAGE_LEDGER is not None,
          "no active ledger must yield no candidates")
finally:
    T._classify_pdf_pages = _orig_classify
    T._extract_page_text_layer = _orig_text_layer


# ---------------------------------------------------------------------------
# Sweep call glue with a fake client (batching, provenance, parsing)
# ---------------------------------------------------------------------------
_SWEEP_JSON = json.dumps({
    "pages": [
        {"image_index": 1, "page_kind": "finish_schedule",
         "findings": [
             {"category": "wallcovering", "item": "WC-1 vinyl wallcovering",
              "detail": "corridors and lobby walls", "codes": ["WC-1"]},
             {"category": "dryfall_exposed_structure",
              "item": "paint exposed deck",
              "detail": "dryfall at open ceiling areas", "codes": []},
         ]},
        {"image_index": 2, "page_kind": "other", "findings": []},
        {"image_index": 99, "page_kind": "other", "findings": []},  # bogus
    ]
})


class _FakeStream:
    def __init__(self, text):
        self.text_stream = [text]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeMessages:
    def __init__(self, text):
        self._text = text
        self.calls = 0

    def stream(self, **kwargs):
        self.calls += 1
        return _FakeStream(self._text)


class _FakeClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


_orig_candidates = T._scope_sweep_candidate_pages
_orig_render = T._render_page_to_jpeg_b64
try:
    T._scope_sweep_candidate_pages = lambda paths, ledger=None: [
        {"pdf_path": FAKE_PDF, "page_idx0": 1, "sheet": "A-101",
         "score": 20, "text": "ROOM FINISH SCHEDULE WC-1"},
        {"pdf_path": FAKE_PDF, "page_idx0": 3, "sheet": "A-103",
         "score": 1, "text": ""},
    ]
    # The sweep must use the dimension-clamped single-page renderer
    # (max_dim 7800) — _render_pages_to_images has no clamp and 400s the
    # API on large-format sheets (Beloit validation, 2026-07-06).
    T._render_page_to_jpeg_b64 = lambda pdf, pg, **kw: ("QUJDRA==", 100, 100)

    client = _FakeClient(_SWEEP_JSON)
    an = {"aggregated_totals": {"total_wallcovering_sqft": 0},
          "floors": [], "notes": []}
    sweep = T._run_scope_sweep(client, [FAKE_PDF], an)
    check(sweep is not None, "sweep with fake client must return results")
    check(client.messages.calls == 1, "2 pages must fit one batch (5/call)")
    check(len(an["_scope_sweep"]["pages_swept"]) == 2,
          "both rendered pages must be recorded as swept "
          f"(got {len(an['_scope_sweep']['pages_swept'])})")
    fnd = an["_scope_sweep"]["findings"]
    check(len(fnd) == 2, f"expected 2 findings with provenance, got "
                         f"{len(fnd)} (bogus image_index must be dropped)")
    check(all(f.get("page") == 2 and f.get("sheet") == "A-101"
              for f in fnd), "findings must carry page provenance")

    # Empty candidates -> clean no-op
    T._scope_sweep_candidate_pages = lambda paths, ledger=None: []
    check(T._run_scope_sweep(client, [FAKE_PDF], {}) is None,
          "no candidates must be a clean no-op")
finally:
    T._scope_sweep_candidate_pages = _orig_candidates
    T._render_page_to_jpeg_b64 = _orig_render


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def _analysis(agg=None, notes=None, sweep_findings=None, sweep_pages=None,
              **extra):
    a = {
        "project_info": {"building_type": "commercial"},
        "aggregated_totals": agg if agg is not None else {
            "total_wallcovering_sqft": 0,
            "total_dryfall_ceiling_sqft": 0,
            "total_paintable_wall_sqft": 10000,
        },
        "floors": [{"floor_name": "1", "rooms": [
            {"room_name": "Office", "notes": "",
             "materials": {"walls": "GYP", "ceiling": "ACT"},
             "dimensions": {"wall_area_sqft": 1000}},
        ]}],
        "notes": list(notes or []),
    }
    a.update(extra)
    if sweep_findings is not None or sweep_pages is not None:
        a["_scope_sweep"] = {"model": "test",
                             "findings": sweep_findings or [],
                             "pages_swept": sweep_pages or []}
    return a


def _rfi_cats(a):
    return [r["category"] for r in a.get("_pre_pricing_rfis", [])]


_WC_FIND = {"category": "wallcovering", "item": "WC-1 vinyl wallcovering",
            "detail": "corridor walls", "codes": ["WC-1"],
            "file": "set.pdf", "page": 12, "sheet": "A-601"}

# (a) New WC discovery, $0 wallcovering -> RFI + note, quantities untouched
a = _analysis(sweep_findings=[_WC_FIND])
before = copy.deepcopy((a["aggregated_totals"], a["floors"]))
T._reconcile_scope_sweep(a)
check("Wallcovering" in _rfi_cats(a),
      "new WC code on unmeasured page + $0 wallcovering must RFI")
check(any("[Scope Sweep]" in n and "WC-1" in n for n in a["notes"]),
      "WC discovery must be noted with code + provenance")
check((a["aggregated_totals"], a["floors"]) == before,
      "reconciliation must NEVER mutate quantities")
check(a["_scope_sweep"]["reconciliation"]["rfis_added"] >= 1,
      "reconciliation record must count RFIs")

# (b) WC code already visible to the extraction -> note, but NO RFI
a = _analysis(notes=["Room 204: WC-1 extent unclear"],
              sweep_findings=[_WC_FIND])
T._reconcile_scope_sweep(a)
check("Wallcovering" not in _rfi_cats(a),
      "already-known WC code must not re-ask")
check(any("WC-1" in n and "[Scope Sweep]" in n for n in a["notes"]),
      "already-known WC still gets an audit note")

# (c) Wallcovering already priced -> nothing fires
a = _analysis(agg={"total_wallcovering_sqft": 500},
              sweep_findings=[_WC_FIND])
T._reconcile_scope_sweep(a)
check("Wallcovering" not in _rfi_cats(a), "priced WC must not RFI")
check(not any("Wallcovering" in n or "WC-1" in n
              for n in a["notes"]), "priced WC must not add a note")

# (d) Finish-schedule page found by sweep -> flag upgrade + RFI
a = _analysis(sweep_pages=[{"file": "set.pdf", "page": 9, "sheet": "A-600",
                            "page_kind": "finish_schedule",
                            "n_findings": 0}])
a["has_finish_schedule"] = False
T._reconcile_scope_sweep(a)
check(a["has_finish_schedule"] is True,
      "sweep-found finish schedule must upgrade the detection flag")
check("Finish Schedule" in _rfi_cats(a),
      "unreadable finish schedule must RFI for its contents")
check("has_finish_schedule" in
      a["_scope_sweep"]["reconciliation"]["flags_upgraded"],
      "flag upgrade must be recorded")

# (d2) Flag already True -> no duplicate RFI
a = _analysis(sweep_pages=[{"file": "set.pdf", "page": 9, "sheet": "A-600",
                            "page_kind": "finish_schedule",
                            "n_findings": 0}])
a["has_finish_schedule"] = True
T._reconcile_scope_sweep(a)
check("Finish Schedule" not in _rfi_cats(a),
      "already-detected finish schedule must not RFI")

# (e) Door schedule page -> flag only, no RFI
a = _analysis(sweep_pages=[{"file": "set.pdf", "page": 4, "sheet": "A-401",
                            "page_kind": "door_schedule", "n_findings": 0}])
T._reconcile_scope_sweep(a)
check(a.get("has_door_schedule") is True, "door schedule flag must upgrade")
check("Finish Schedule" not in _rfi_cats(a) and not any(
    "Door" in c for c in _rfi_cats(a)),
    "door schedule page alone must not RFI")

# (f) Dryfall callout, nothing priced -> Ceiling Scope RFI
_DF = {"category": "dryfall_exposed_structure", "item": "paint exposed deck",
       "detail": "dryfall at sales floor", "codes": [],
       "file": "set.pdf", "page": 3, "sheet": "A-002"}
a = _analysis(sweep_findings=[_DF])
T._reconcile_scope_sweep(a)
check("Ceiling Scope" in _rfi_cats(a),
      "dryfall callout with $0 dryfall must RFI")

# (f2) structural scope already captured -> suppressed
a = _analysis(sweep_findings=[_DF])
a["structural_finish_scope"] = [{"surface": "deck"}]
T._reconcile_scope_sweep(a)
check("Ceiling Scope" not in _rfi_cats(a),
      "captured structural scope must suppress the dryfall RFI")

# (g) New specialty finish -> RFI; known one -> suppressed
_EP = {"category": "specialty_finish", "item": "Epoxy paint at kitchen",
       "detail": "epoxy on CMU", "codes": [],
       "file": "set.pdf", "page": 7, "sheet": "A-101"}
a = _analysis(sweep_findings=[_EP])
T._reconcile_scope_sweep(a)
check("Specialty Finishes" in _rfi_cats(a), "new epoxy must RFI")

a = _analysis(sweep_findings=[_EP])
a["floors"][0]["rooms"][0]["notes"] = "Kitchen walls epoxy per schedule"
T._reconcile_scope_sweep(a)
check("Specialty Finishes" not in _rfi_cats(a),
      "epoxy already in room notes must not re-ask")

# (h) Exterior scope: empty exterior -> RFI; priced exterior -> suppressed
_EXT = {"category": "exterior", "item": "Paint exterior hollow metal",
        "detail": "north elevation note", "codes": [],
        "file": "set.pdf", "page": 15, "sheet": "A-201"}
a = _analysis(sweep_findings=[_EXT])
T._reconcile_scope_sweep(a)
check("Exterior Scope" in _rfi_cats(a), "exterior note w/o exterior must RFI")

a = _analysis(sweep_findings=[_EXT])
a["exterior"] = {"total_exterior_wall_sqft": 2400}
T._reconcile_scope_sweep(a)
check("Exterior Scope" not in _rfi_cats(a),
      "priced exterior must suppress the RFI")

# (i) Alternates -> RFI; scope_note -> note only
_ALT = {"category": "alternates", "item": "Alternate 2: repaint stairwells",
        "detail": "add alternate", "codes": [],
        "file": "set.pdf", "page": 2, "sheet": "G-001"}
_SN = {"category": "scope_note", "item": "Owner supplies wallcovering",
       "detail": "installation by GC", "codes": [],
       "file": "set.pdf", "page": 2, "sheet": "G-001"}
a = _analysis(sweep_findings=[_ALT, _SN])
T._reconcile_scope_sweep(a)
check("Alternates" in _rfi_cats(a), "new alternate must RFI")
check(any("Owner supplies wallcovering" in n for n in a["notes"]),
      "scope_note must surface as a note")
check(len(_rfi_cats(a)) == 1, "scope_note must not RFI")

# (j) No sweep payload / empty payload -> clean no-ops
a = _analysis()
check(T._reconcile_scope_sweep(a) is a and not a.get("_pre_pricing_rfis"),
      "missing sweep payload must no-op")
a = _analysis(sweep_findings=[], sweep_pages=[])
T._reconcile_scope_sweep(a)
check(not a.get("_pre_pricing_rfis"), "empty sweep must add nothing")
check(T._reconcile_scope_sweep(None) is None,
      "non-dict analysis must not crash")

# (k) End-to-end shape: sweep findings from the fake-client run reconcile
a = _analysis(sweep_findings=[_WC_FIND, _DF],
              sweep_pages=[{"file": "set.pdf", "page": 12, "sheet": "A-601",
                            "page_kind": "finish_schedule",
                            "n_findings": 2}])
a["has_finish_schedule"] = False
before = copy.deepcopy((a["aggregated_totals"], a["floors"]))
T._reconcile_scope_sweep(a)
cats = _rfi_cats(a)
check({"Wallcovering", "Ceiling Scope", "Finish Schedule"} <= set(cats),
      f"combined case must queue all three RFIs, got {cats}")
check((a["aggregated_totals"], a["floors"]) == before,
      "combined case must not mutate quantities")

os.environ.pop("NIGHTSHIFT_SCOPE_SWEEP", None)

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
