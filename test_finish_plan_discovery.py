#!/usr/bin/env python3
"""A finish PLAN must be found, and an EXTERIOR finish schedule must not win.

2026-09-01, Profeta Painting / 168 Holley St. The targeted room-finish read
is aimed at the pages _find_finish_schedule_pages returns. On this set it
returned exactly one page — p16, sheet A-301 EXTERIOR ELEVATIONS, which
carries an "EXTERIOR FINISH SCHEDULE" for siding/trim/fascia AND was one of
four sheets that failed extraction. The actual room finish source, p14 /
A-207 "FINISH PLANS & SPECIFICATIONS", carried all six finish column tokens
(wall/base/floor/room finish, room name, room number) plus the PT-/WC-/WD-
legend and was never read.

Downstream that produced: base trim 388 LF against 2,859 LF of in-scope room
perimeter (WD-2 wood base is on the sheet), wallcovering 0 SF against six
WC-1/WC-2 wall tags, and 40 of 59 rooms stamped "GYP (assumed)" against a
"ALL WALLS TO BE PAINTED PT-1, U.N.O." note.

Locks in: (1) selection never narrows versus the old behaviour, (2) strong room-keyed token
evidence stands alone with no title phrase, (3) an exterior finish schedule
never wins the title pass, (4) the flag OFF restores pre-fix behaviour
exactly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def load(flag):
    os.environ["NIGHTSHIFT_FINISH_PLAN_DISCOVERY"] = flag
    sys.modules.pop("Takeoff_DIRECT", None)
    import Takeoff_DIRECT as T
    return T


# --- 1) Token-only: the A-207 text shape matches with no title phrase.
T = load("1")
a207 = ("finish plans & specifications  room name  room number  "
        "wall finish  base finish  floor finish  room finish  pt-1  wc-1  wd-2")
check(T._finish_token_only_match(a207),
      "A-207 token shape did not match on token evidence alone")
check(not any(p in a207 for p in T._FINISH_TITLE_PHRASES),
      "test fixture accidentally contains a legacy title phrase")

# Two tokens is not enough, and tokens without a room key are not enough.
check(not T._finish_token_only_match("wall finish base finish"),
      "two tokens wrongly matched")
check(not T._finish_token_only_match(
          "wall finish base finish floor finish ceiling finish"),
      "surface tokens without a room key wrongly matched")
check(T._finish_token_only_match("wall finish base finish room name"),
      "three tokens incl. a room key failed to match")

# --- 2) Finish-PLAN titles are recognised when the flag is on.
# --- 3) An exterior finish schedule must not win the title pass.
T = load("1")
check("exterior finish schedule" in T._finish_title_excludes(),
      "exterior finish schedule not excluded when flag ON")
check("exterior finish schedule" not in T._SCHEDULE_REFERENCE_EXCLUDES,
      "exterior exclusion wrongly added to the shared reference excludes")
check("exterior finish schedule" not in load("0")._finish_title_excludes(),
      "exterior exclusion leaked through when flag OFF")

# The generic detector must still honour the excludes it is handed.
T = load("1")


class _FakePage:
    def __init__(self, spans):
        self._spans = spans

    def get_text(self, kind=None):
        if kind == "dict":
            return {"blocks": [{"lines": [{"spans": [
                {"text": s, "size": 14.0} for s in self._spans]}]}]}
        return " ".join(self._spans)


check(not T._has_schedule_sheet_title(
          _FakePage(["EXTERIOR FINISH SCHEDULE"]),
          T._FINISH_TITLE_PHRASES, T._finish_title_excludes()),
      "exterior finish schedule still matched the title pass")
check(T._has_schedule_sheet_title(
          _FakePage(["ROOM FINISH SCHEDULE"]),
          T._FINISH_TITLE_PHRASES, T._finish_title_excludes()),
      "a real room finish schedule stopped matching")
# A finish PLAN is found by its per-room columns, not by its title block —
# a title phrase for it also matched the cover sheet's drawing index.
check(not T._has_schedule_sheet_title(
          _FakePage(["FINISH PLANS & SPECIFICATIONS"]),
          T._FINISH_TITLE_PHRASES, T._finish_title_excludes()),
      "finish-plan titles should not drive selection (index contamination)")

# --- 4) Flag OFF is inert: token-only never fires.
check(not load("0")._finish_token_only_match(a207),
      "token-only pass fired with the flag OFF")

# --- 5) End-to-end on the real Profeta set, when it is available.
PDF = ("/Users/stevenboyce/Downloads/2026-08-07_168_HOLLEY_ST_BASC_OFFICES"
       "_Architectural_Stamped_normalized.annotated.pdf")
if os.path.exists(PDF):
    off = [p + 1 for p in load("0")._find_finish_schedule_pages(PDF)]
    on = [p + 1 for p in load("1")._find_finish_schedule_pages(PDF)]
    check(off == [16], f"pre-fix behaviour changed (expected [16]): {off}")
    check(on == [14], f"fixed run should target A-207 on p14, got: {on}")
else:
    print("  ~ skipped end-to-end: source PDF not present")

# --- 5b) The finish-PLAN phrase must be ADDITIVE ONLY.
#     Dutchess Livestock draws every drawing title at 9.5pt, so if a plan
#     title were allowed to feed the font-size promotion, one 18.9pt
#     "FINISH PLAN" heading would push past TITLE_FONT_PT and discard the
#     five small-font finish sheets that were previously returned.
DUTCHESS = "golden/plans/Dutchess_Livestock_Bidding_Documents.pdf"
if os.path.exists(DUTCHESS):
    off = load("0")._find_finish_schedule_pages(DUTCHESS)
    on = load("1")._find_finish_schedule_pages(DUTCHESS)
    check(set(off).issubset(set(on)),
          f"discovery narrowed a golden set: off={[p+1 for p in off]} "
          f"on={[p+1 for p in on]}")
else:
    print("  ~ skipped Dutchess non-narrowing check: corpus not present")

# --- 6) A title-size page must NOT suppress pages carrying the finish rows.
#     Hudson Hotel: the finish-PLAN title sits on one sheet while five other
#     sheets hold the per-room finish columns. Recording token hits as 0.0pt
#     "title" hits made the 12pt+ rule discard all five.
try:
    import fitz

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       ".finish_union_probe.pdf")
    doc = fitz.open()
    pg = doc.new_page()                      # p1: big title, no rows
    pg.insert_text((72, 200), "FINISH SCHEDULE", fontsize=24)
    pg = doc.new_page()                      # p2: the actual finish rows
    pg.insert_text((72, 200),
                   "ROOM NAME  ROOM NUMBER  WALL FINISH  BASE FINISH  "
                   "FLOOR FINISH", fontsize=8)
    doc.save(tmp)
    doc.close()
    try:
        got = [i + 1 for i in load("1")._find_finish_schedule_pages(tmp)]
        check(got == [1, 2],
              f"title page suppressed the page holding the finish rows: {got}")
    finally:
        os.remove(tmp)
except ImportError:
    print("  ~ skipped union probe: PyMuPDF unavailable")

os.environ.pop("NIGHTSHIFT_FINISH_PLAN_DISCOVERY", None)
sys.modules.pop("Takeoff_DIRECT", None)
import Takeoff_DIRECT as T
check(T._finish_plan_discovery_enabled(), "discovery is not ON by default")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
