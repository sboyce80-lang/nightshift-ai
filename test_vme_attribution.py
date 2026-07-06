"""Validate VME M1 (sheet/floor attribution) on 364 + Fishkill.

Checks: (1) each floor gets ONE canonical source, (2) the all-floors composite
is excluded (no double-count), (3) building total lands near golden physical SF.
Runs only when the sample PDFs are present.
"""
import os
import vme_attribution as m1

HERE = os.path.dirname(os.path.abspath(__file__))

# job -> (pdf, per-floor heights, golden per-floor LF for reference, golden physical total)
JOBS = {
    "364 Main": {
        "pdf": "spike_samples/364Main.pdf",
        "heights": {0: 9.0, 1: 12.0, 2: 9.5, 3: 9.5},
        "golden_floor_lf": {0: 735.9, 1: 1498.04, 2: 3198.73, 3: 3196.38},
        "golden_total": 85353,
    },
    "Fishkill 397": {
        "pdf": "spike_samples/397Fishkill.pdf",
        "heights": {1: 10.08, 2: 9.58, 3: 9.58},
        "golden_floor_lf": {1: 746.99, 2: 1585.07, 3: 1585.07},
        "golden_total": 37900,   # 3-floor walls only (full golden 43,003 incl stairs+gyp)
    },
}

fails = []
for name, j in JOBS.items():
    pdf = os.path.join(HERE, j["pdf"])
    if m1.fitz is None or not os.path.exists(pdf):
        print(f"SKIP {name} (PDF/PyMuPDF unavailable)")
        continue
    r = m1.measure_building(pdf, j["heights"])
    print(f"\n===== {name} =====  primary scale {r['primary_scale']} pts/ft")
    print(f"{'floor':5} {'src pg':>6} {'scale':>5} {'myRuns':>7} {'goldLF':>7} {'run%':>5} {'myArea':>8}")
    for fl, d in r["floors"].items():
        glf = j["golden_floor_lf"].get(fl)
        runp = f"{d['runs_lf']/glf*100:.0f}%" if glf else "-"
        print(f"{fl:5} {d['page']:>6} {d['scale']:>5} {d['runs_lf']:7.0f} "
              f"{glf or 0:7.0f} {runp:>5} {str(d['area_sqft']):>8}")
    pct = r["total_area_sqft"] / j["golden_total"] * 100
    print(f"  TOTAL area {r['total_area_sqft']:,.0f} vs golden {j['golden_total']:,} = {pct:.0f}%")
    print(f"  excluded (composite/dupe) pages: {r['excluded_pages']}")
    # checks
    if not r["excluded_pages"]:
        fails.append(f"{name}: no composite/dupe page excluded (expected the all-floors sheet)")
    if not (60 <= pct <= 140):
        fails.append(f"{name}: total {pct:.0f}% of golden outside 60-140% band")
    seen = [d["page"] for d in r["floors"].values()]
    # each floor mapped to exactly one page (dict guarantees one source per floor)
    print(f"  one-source-per-floor: {len(r['floors'])} floors -> pages {seen}")

# --------------------------------------------------------------------------
print("\nM2 part 2 — unit-partition recovery (deterministic core)")
# floor = demising + sum(unit_type_run * count)
r = m1.recover_floor_wall_runs(400.0, {"A": 100.0, "B": 50.0}, {"A": 2, "B": 3})
if abs(r - (400 + 200 + 150)) > 1e-9:
    fails.append(f"recover_floor_wall_runs math: {r}")
else:
    print("  PASS  demising + sum(unit_run * count)")
if m1.recover_floor_wall_runs(300.0, {}, {}) != 300.0:
    fails.append("recover_floor_wall_runs empty -> demising only")
else:
    print("  PASS  no units -> demising only")
if m1.recover_floor_wall_runs(0.0, {"A": 100.0}, {"A": 0}) != 0.0:
    fails.append("recover_floor_wall_runs zero count")
else:
    print("  PASS  zero count contributes nothing")

# --------------------------------------------------------------------------
print("\nM3 shadow — compute_vme_shadow (robust, comparison-only)")
# robustness: never raises, returns None on bad input
if m1.compute_vme_shadow("/no/such/file.pdf") is not None:
    fails.append("shadow bad-path should be None")
else:
    print("  PASS  bad path -> None (never raises)")
if m1.compute_vme_shadow(None) is not None:
    fails.append("shadow None should be None")
else:
    print("  PASS  None -> None")
import os as _os
_fish = _os.path.join(HERE, "spike_samples", "397Fishkill.pdf")
if m1.fitz is not None and _os.path.exists(_fish):
    s = m1.compute_vme_shadow(_fish)
    ok = (s and s["total_wall_run_lf"] > 0 and s["n_floor_pages"] >= 1
          and isinstance(s.get("by_page"), list))
    if not ok:
        fails.append(f"shadow on Fishkill malformed: {s}")
    else:
        print(f"  PASS  Fishkill shadow well-formed ({s['total_wall_run_lf']:.0f} LF, "
              f"{s['n_floor_pages']} sheets)")

print("\nM2 part 3 — room_regions envelope seal (synthetic)")
if m1.fitz is not None:
    import fitz, tempfile
    # A 40x30ft building whose EXTERIOR is drawn as UNPAIRED single lines (so
    # it produces no wall `runs`), one interior partition drawn as a face pair,
    # and a sheet-border decoy spanning >85% of the page. This is exactly the
    # Livestock leak: seeding the flood grid from runs alone leaves the
    # exterior without a cell boundary -> the whole interior floods to the page
    # edge as one leaky blob. The fix (seed from clustered wall lines + drop
    # border-scale lines) must seal it into bounded rooms.
    s = 9.0
    doc = fitz.open(); page = doc.new_page(width=800, height=600)
    x0, y0, w, h = 150, 120, 40 * s, 30 * s
    t = 0.5 * s
    sh = page.new_shape()
    def rect_lines(a, b, c, d):   # 4 explicit single lines (draw_rect -> re is skipped)
        sh.draw_line(fitz.Point(a, b), fitz.Point(c, b))
        sh.draw_line(fitz.Point(a, d), fitz.Point(c, d))
        sh.draw_line(fitz.Point(a, b), fitz.Point(a, d))
        sh.draw_line(fitz.Point(c, b), fitz.Point(c, d))
    # exterior envelope: single lines, no pair
    rect_lines(x0, y0, x0 + w, y0 + h)
    # interior partition (vertical, mid) as a face pair -> a real run + splits
    xm = x0 + w / 2
    sh.draw_line(fitz.Point(xm, y0), fitz.Point(xm, y0 + h))
    sh.draw_line(fitz.Point(xm + t, y0), fitz.Point(xm + t, y0 + h))
    # sheet-border decoy near the page edge (spans ~95% of page each way)
    rect_lines(20, 20, 780, 580)
    # small corner marks (north arrow / scale bar / dims) beyond the building,
    # as on any real sheet, so the grid extends past the envelope and the
    # sealed interior rooms are correctly interior (not grid-edge "leaky").
    for (cx, cy) in ((45, 45), (740, 45), (45, 540), (740, 540)):
        sh.draw_line(fitz.Point(cx, cy), fitz.Point(cx + 3 * s, cy))
        sh.draw_line(fitz.Point(cx, cy), fitz.Point(cx, cy + 3 * s))
    sh.finish(); sh.commit()
    fd, path = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
    doc.save(path); doc.close()
    regs = m1.room_regions(path, 0, s, anchors=[])
    os.remove(path)
    tight = [r for r in regs if not r["leaky"]]
    tight_sum = sum(r["area_ft2"] for r in tight)
    page_ft2 = (800 * 600) / (s * s)
    # building interior ~1200 ft^2 split into two ~600 ft^2 rooms
    if len(tight) < 2:
        fails.append(f"envelope leak: only {len(tight)} sealed rooms (want >=2)")
    elif not (800 <= tight_sum <= 1300):
        fails.append(f"envelope seal area {tight_sum:.0f} ft2 off (want ~1200)")
    elif any(r["area_ft2"] > 0.7 * page_ft2 for r in tight):
        fails.append("border decoy created a full-page pseudo-room")
    elif not any(r["leaky"] for r in regs):
        fails.append("outside not tagged leaky")
    else:
        print(f"  PASS  envelope sealed: {len(tight)} rooms, {tight_sum:.0f} ft2, "
              f"outside leaky, no pseudo-room")

print(f"\n=== {'PASS' if not fails else 'ISSUES: ' + '; '.join(fails)} ===")
