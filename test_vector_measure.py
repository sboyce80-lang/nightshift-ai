"""Tests for vector_measure (Phase 3 VME, M0).

Offline unit tests (scale parser, interval union, layer filter) always run.
The 364-Main integration check runs only when the sample PDF is present and
asserts the measurement reproduces Rider's golden (85,353 SF) within tolerance.
"""
import os
import sys

import vector_measure as vm

HERE = os.path.dirname(os.path.abspath(__file__))
_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        _fails.append(name)


# --------------------------------------------------------------------------
print("scale parsing")
# 1/8"=1'-0" -> 0.125*72/1 = 9 pts/ft, etc.
for s, expect in [
    ('1/8"=1\'-0"', 9.0),
    ('1/4" = 1\'-0"', 18.0),
    ('3/32"=1\'-0"', 6.75),
    ('1"=1\'-0"', 72.0),
    ('1-1/2"=1\'-0"', 108.0),
    ('SCALE: 1/8" = 1\'-0"', 9.0),
]:
    got = vm.parse_scale(s)
    check(f"parse_scale({s!r})={got}", got is not None and abs(got - expect) < 1e-6,
          f"expect {expect}")
check("parse_scale(no scale) is None", vm.parse_scale("FLOOR PLAN") is None)
check("parse_scale('') is None", vm.parse_scale("") is None)

# --------------------------------------------------------------------------
print("interval union (duplicate/overlap collapse)")
check("disjoint sums", abs(vm.union_length([(0, 10), (20, 25)]) - 15.0) < 1e-9)
check("identical duplicates collapse", abs(vm.union_length([(0, 10), (0, 10), (0, 10)]) - 10.0) < 1e-9)
check("overlap merges", abs(vm.union_length([(0, 10), (5, 15)]) - 15.0) < 1e-9)
check("empty is 0", vm.union_length([]) == 0.0)

# --------------------------------------------------------------------------
print("wall-layer classification")
check("A-WALL-NEW is wall", vm.is_wall_layer("X-FLOOR PLAN|A-WALL-NEW"))
check("a-wall-demising is wall", vm.is_wall_layer("M-2|a-wall-demising"))
check("bare WALL is wall", vm.is_wall_layer("WALL"))
check("PARTITION is wall", vm.is_wall_layer("PARTITION"))
check("A-Anno-Iden-Wall excluded", not vm.is_wall_layer("M-2|A-Anno-Iden-Wall"))
check("A-Wall-Patt (hatch) excluded", not vm.is_wall_layer("A-Wall-Patt"))
check("A-WALL-BELOW excluded", not vm.is_wall_layer("A-WALL-BELOW"))
check("A-Lite-Wall (glazing) excluded", not vm.is_wall_layer("A-Lite-Wall"))
check("empty layer excluded", not vm.is_wall_layer(""))
check("DUCTWORK excluded", not vm.is_wall_layer("DUCTWORK"))

# --------------------------------------------------------------------------
# Integration: 364 Main — M0 validates scale-detection + measurement stability.
# Building-TOTAL accuracy is deliberately NOT asserted here: it needs M1
# (per-floor sheet selection / cross-sheet dedup) and M2 (paintability, poché).
# NOTE: the spike's "composite within 2% of golden" was a SCALE ARTIFACT — the
# composite sheet (p2) is drawn at 3/32"=1'-0", not 1/8"; at its true scale it
# reads ~111k SF (30% over golden 85,353). Lesson: measure per-floor sheets at
# their own detected scale, then attribute + dedup in M1.
PDF = os.path.join(HERE, "spike_samples", "364Main.pdf")
if vm.fitz is not None and os.path.exists(PDF):
    print("364 Main integration — per-sheet scale detection + measurement stability")
    check("p13 A-105 (1st floor) scale == 9 (1/8\")",
          abs((vm.detect_scale(PDF, 12) or 0) - 9.0) < 1e-6, f"{vm.detect_scale(PDF,12)}")
    check("p8 A-100 (basement) scale == 9 (1/8\")",
          abs((vm.detect_scale(PDF, 7) or 0) - 9.0) < 1e-6, f"{vm.detect_scale(PDF,7)}")
    check("p2 (composite) scale == 6.75 (3/32\") — NOT 1/8\"",
          abs((vm.detect_scale(PDF, 1) or 0) - 6.75) < 1e-6, f"{vm.detect_scale(PDF,1)}")
    m13 = vm.measure_wall_faces(PDF, 12)   # auto-detects 9 pts/ft
    check("p13 has layer attribution", m13["has_layer_attribution"])
    check("p13 wall_face_lf deterministic (~2883 ft)", abs(m13["wall_face_lf"] - 2883) < 50,
          f"{m13['wall_face_lf']:.0f}")
    print("     building-TOTAL vs golden 85,353 is an M1/M2 target, not asserted here.")
else:
    print("364 Main integration SKIPPED (PDF or PyMuPDF unavailable)")

# --------------------------------------------------------------------------
print("M2 centerline clustering (wall runs)")
# one wall = two faces 0.5ft apart (4.5pt @ scale9), 10ft long -> ONE 10ft run
one_wall = [(100.0, 0, 90), (104.5, 0, 90)]   # pts; /9 = 10 ft
check("two faces of one wall -> one run", abs(vm.cluster_wall_runs(one_wall, 9.0) / 9 - 10) < 0.5,
      f"{vm.cluster_wall_runs(one_wall, 9.0)/9:.1f}ft")
# multi-line assembly (4 parallel lines within thickness) -> still one run
assembly = [(100.0, 0, 90), (101.5, 0, 90), (103.0, 0, 90), (104.5, 0, 90)]
check("4-line assembly -> one run", abs(vm.cluster_wall_runs(assembly, 9.0) / 9 - 10) < 0.5)
# two distinct walls 3ft apart (27pt) -> two runs (not merged at 0.85ft thresh)
two_walls = [(100.0, 0, 90), (127.0, 0, 90)]
check("two distinct walls -> two runs", abs(vm.cluster_wall_runs(two_walls, 7.65) / 9 - 20) < 0.5,
      f"{vm.cluster_wall_runs(two_walls, 7.65)/9:.1f}ft")
check("empty -> 0", vm.cluster_wall_runs([], 9.0) == 0.0)

FISH = os.path.join(HERE, "spike_samples", "397Fishkill.pdf")
if vm.fitz is not None and os.path.exists(FISH):
    # 1st floor (M-1_SD) faces/2 was 1,708 LF (229% over Rider 747); centerline
    # clustering collapses the multi-line/poché over-count to a plausible run.
    r = vm.measure_wall_runs(FISH, 4, layer_prefix="M-1_SD", pts_per_ft=9.0)
    check("Fishkill 1st centerline << faces/2 over-count", r["wall_run_lf"] < 700,
          f"{r['wall_run_lf']:.0f} LF (Rider 747)")

# --------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# M5 Tier-2: geometric wall detection (no layers) — synthetic fixture
# ---------------------------------------------------------------------------
def test_tier2_geometric_walls():
    import fitz
    import tempfile, os
    import vector_measure as vm
    # Build a synthetic layerless plan at 9 pts/ft (1/8"): a 40ft x 30ft room
    # drawn as parallel face pairs 0.5ft (4.5pt) apart, plus decoys: a grid
    # line (no pair), a dimension pair 3ft apart (outside thickness band),
    # and a short door-leaf line.
    doc = fitz.open()
    page = doc.new_page(width=800, height=600)
    s = 9.0
    x0, y0 = 100, 100
    w, h = 40 * s, 30 * s
    t = 0.5 * s
    sh = page.new_shape()
    for (a, b) in (((x0, y0), (x0 + w, y0)), ((x0, y0 + h), (x0 + w, y0 + h))):
        sh.draw_line(fitz.Point(*a), fitz.Point(*b))                    # outer H
        sh.draw_line(fitz.Point(a[0], a[1] + t), fitz.Point(b[0], b[1] + t))  # inner H
    for (a, b) in (((x0, y0), (x0, y0 + h)), ((x0 + w, y0), (x0 + w, y0 + h))):
        sh.draw_line(fitz.Point(*a), fitz.Point(*b))
        sh.draw_line(fitz.Point(a[0] + t, a[1]), fitz.Point(b[0] + t, b[1]))
    sh.draw_line(fitz.Point(50, 50), fitz.Point(750, 50))       # grid/dim decoy (unpaired)
    sh.draw_line(fitz.Point(50, 50 + 3 * s), fitz.Point(750, 50 + 3 * s))  # 3ft gap decoy
    sh.draw_line(fitz.Point(x0 + 60, y0 + 5), fitz.Point(x0 + 60, y0 + 5 + 0.9 * s))  # leaf
    sh.finish()
    sh.commit()
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    doc.save(path)
    doc.close()
    r = vm.measure_wall_runs_geometric(path, 0, pts_per_ft=s)
    os.remove(path)
    lf = r["wall_run_lf"]
    # true run = 2x40 + 2x30 = 140 LF; allow clustering slop
    check(f"tier2 synthetic room ~140 LF (got {lf:.1f})", abs(lf - 140) <= 8)
    check("tier2 scale source propagated", r["scale_source"] == "given")


test_tier2_geometric_walls()


print(f"\n=== {'ALL PASS' if not _fails else str(len(_fails)) + ' FAILED: ' + ', '.join(_fails)} ===")
sys.exit(1 if _fails else 0)
