#!/usr/bin/env python3
"""The elevation size guard must keep the elevations, not the front matter.

2026-09-02, 168 Holley St (Profeta Painting). _extract_exterior_scope builds
a filtered PDF of elevation-candidate pages; when it exceeds 5 MB the guard
truncated to `elevation_indices[:4]`, commented "typically the 4 cardinal
elevations". They are not the cardinals — they are the four lowest-numbered
pages that matched. On this set that kept the cover sheet (whose only cue is
a drawing index LISTING "EXTERIOR ELEVATIONS" as a sheet name, and which ate
3.0 MB of the 5 MB budget), the A-000 and A-001 specification sheets, and
A-200 — zero elevation cues between the last three — while dropping A-301
Exterior Elevations, the single page carrying two cues. Exterior scope
priced $0 and the exterior notes quoted A-000 spec text.

Locks in: (1) real elevation sheets outscore front matter, (2) a drawing
index never wins, (3) selection fits the budget, (4) zero-evidence pages
never fill leftover headroom, (5) the flag OFF restores `[:4]` exactly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CLAUDE_API_KEY", "x")

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  X {msg}")
    else:
        print("  ok " + msg.split(":")[0])


def load(flag):
    os.environ["NIGHTSHIFT_ELEV_PAGE_RANKING"] = flag
    sys.modules.pop("Takeoff_DIRECT", None)
    import Takeoff_DIRECT as T
    return T


T = load("1")

# --- 1) Scoring separates a real elevation sheet from front matter.
a301 = "exterior elevation  building elevation  north elevation  hardie siding"
cover = "drawing index  a-301 exterior elevations  a-207 finish plans  sheet list"
specs = "division 9 painting  gypsum board  latex paint  msds submittals"
check(T._score_elevation_page(a301) > 0, "a real elevation sheet scored <= 0")
check(T._score_elevation_page(cover) < 0,
      f"a drawing index was not demoted: {T._score_elevation_page(cover)}")
check(T._score_elevation_page(specs) == 0, "a spec sheet scored non-zero")
check(T._score_elevation_page(a301) > T._score_elevation_page(cover),
      "elevation sheet did not outscore the cover sheet")

# An index marker must beat the cue count — a cover sheet listing several
# elevation sheet names would otherwise outscore the elevations themselves.
busy_index = ("drawing index  north elevation  south elevation  "
              "east elevation  west elevation  exterior elevations")
check(T._score_elevation_page(busy_index) < T._score_elevation_page(a301),
      f"a cue-heavy drawing index outscored a real elevation: "
      f"{T._score_elevation_page(busy_index)} vs {T._score_elevation_page(a301)}")

# --- 2) Flag OFF leaves the scorer inert and restores [:4].
check(load("0")._score_elevation_page(a301) == 0,
      "scorer active with the flag OFF")
_REAL = "nsai_profeta_a207_2026-09-01/plans_clean.pdf"
if os.path.exists(_REAL):
    check(load("0")._rank_elevation_pages(_REAL, [0, 1, 2, 3, 4, 5],
                                          5 * 1024 * 1024) == [0, 1, 2, 3],
          "flag OFF did not fall back to [:4] on a readable PDF")
check(not load("0")._elevation_ranking_enabled(),
      "kill switch did not disable ranking")

# --- 3) End-to-end on the real set, when present.
T = load("1")
PDF = "nsai_profeta_a207_2026-09-01/plans_clean.pdf"
if os.path.exists(PDF):
    import fitz
    BUDGET = 5 * 1024 * 1024
    cands = T._identify_elevation_pages(PDF)
    check(15 in cands, "A-301 (page 16) is not even an elevation candidate")
    check(15 not in cands[:4], "fixture drift: legacy path already kept A-301")
    ranked = T._rank_elevation_pages(PDF, cands, BUDGET)
    check(15 in ranked,
          f"ranked selection dropped A-301: {[i + 1 for i in ranked]}")
    check(0 not in ranked, "cover sheet survived ranking")
    size = len(T._create_filtered_pdf(PDF, ranked))
    check(size <= BUDGET,
          f"ranked selection busts the budget: {size / 1024 / 1024:.2f} MB")
    doc = fitz.open(PDF)
    zero = [i + 1 for i in ranked
            if T._score_elevation_page(doc[i].get_text().lower()) <= 0]
    doc.close()
    check(not zero, f"zero-evidence page(s) filled leftover headroom: {zero}")
else:
    print("  ~ skipped end-to-end: source PDF not present")

os.environ.pop("NIGHTSHIFT_ELEV_PAGE_RANKING", None)
sys.modules.pop("Takeoff_DIRECT", None)
import Takeoff_DIRECT as T
check(T._elevation_ranking_enabled(), "ranking is not ON by default")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
