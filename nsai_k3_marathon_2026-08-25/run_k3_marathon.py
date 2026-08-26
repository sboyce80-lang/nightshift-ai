#!/usr/bin/env python3
"""K=3 marathon: all 10 goldens at the marathon posture PLUS
NIGHTSHIFT_JOB_DRAW_MEDIAN=3 — the variance-program board.

Target (Steven, 2026-08-25): 7-9 of 9 priced jobs in ±10% band, and
repeatably. Child subprocess per job (worktree Takeoff_DIRECT), plans
from the canonical repo, worktree-local checkpoints. K3_BOARD.md
rewritten after every job."""
import os, sys, json, subprocess
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WT = os.path.dirname(HERE)                       # the worktree root
MAIN = os.path.join(os.path.dirname(WT), "nightshift-repo")
PY = os.path.join(MAIN, ".venv", "bin", "python")
TIMEOUT_S = 12 * 3600                            # 3 draws of a 3.5h job

# Ascending expected runtime (K=1 minutes x3): tsc 35, harlem 16,
# fishkill 45, caris 50, hudson 47, 364 48, dutchess 48, honey 115,
# ulum 214, homewood 183.
SEQUENCE = ["jw_harlem_valley", "tsc_fusion_highland", "fishkill_397",
            "jw_caris_hyde_park", "jw_hudson_hotel", "364_main",
            "dutchess_livestock", "honey_farms_malta",
            "jw_homewood_suites", "jw_under_canvas_ulum"]


def log(m):
    line = f"[{datetime.now(timezone.utc).strftime('%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    with open(os.path.join(HERE, "orchestrator.log"), "a") as f:
        f.write(line + "\n")


def cell(job):
    p = os.path.join(HERE, "results", f"{job}.json")
    return json.load(open(p)) if os.path.exists(p) else None


def write_report():
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                         cwd=WT, capture_output=True, text=True
                         ).stdout.strip()
    lines = [f"# K=3 draw-median board — 2026-08-25 · {sha} · "
             f"marathon posture + JOB_DRAW_MEDIAN=3", "",
             "| job | class | KS subtotal | target bid | Δ | ±10% | mr | "
             "sel draw | spread | mean qty err |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    n_band = n_scored = 0
    for job in SEQUENCE:
        c = cell(job)
        if not c:
            lines.append(f"| {job} | | … | | | | | | | |")
            continue
        if not c.get("ok"):
            lines.append(f"| {job} | {c.get('cls','')} | FAILED "
                         f"{str(c.get('error'))[:40]} | | | | | | | |")
            continue
        bid = c.get("bid")
        d = c.get("delta_pct")
        band = c.get("in_band_10")
        if band is not None:
            n_scored += 1
            n_band += 1 if band else 0
        me = c.get("mean_abs_err_pct")
        rep = c.get("draw_report") or {}
        lines.append(
            f"| {job} | {c['cls']} | ${c['subtotal']:,.0f} | "
            f"{('$%s' % format(bid, ',.0f')) if bid else 'qty only'} | "
            f"{('%+.1f%%' % d) if d is not None else '—'} | "
            f"{'✅' if band else ('—' if band is None else '✗')} | "
            f"{'Y' if c.get('manual_review') else 'N'} | "
            f"{rep.get('selected_draw', '—')}/{rep.get('k', '—')} | "
            f"{('%.0f%%' % rep['subtotal_spread_pct']) if rep.get('subtotal_spread_pct') is not None else '—'} | "
            f"{('%.1f%%' % me) if me is not None else '—'} |")
    lines += ["", f"**In band: {n_band}/{n_scored} scored — "
              f"target 7-9/9**", ""]
    with open(os.path.join(HERE, "K3_BOARD.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    for job in SEQUENCE:
        if cell(job):
            log(f"SKIP {job} — result exists")
            continue
        log(f"START {job}")
        try:
            subprocess.run(
                [PY, os.path.join(HERE, "run_k3_child.py"), job],
                cwd=WT, timeout=TIMEOUT_S,
                stdout=open(os.path.join(HERE, "results",
                                         f"{job}.run.log"), "w"),
                stderr=subprocess.STDOUT)
            c = cell(job)
            if c and c.get("ok"):
                rep = c.get("draw_report") or {}
                log(f"DONE {job} subtotal=${c['subtotal']:,.0f} "
                    f"delta={c.get('delta_pct')} band={c.get('in_band_10')} "
                    f"sel={rep.get('selected_draw')}/{rep.get('k')} "
                    f"spread={rep.get('subtotal_spread_pct')} "
                    f"mr={c.get('manual_review')} "
                    f"({c['elapsed_s']/60:.0f}m)")
            else:
                log(f"FAILED {job} err={(c or {}).get('error', 'no meta')}")
        except subprocess.TimeoutExpired:
            log(f"TIMEOUT {job} after {TIMEOUT_S/3600:.0f}h")
        write_report()
    log("K3 MARATHON COMPLETE")
    write_report()


if __name__ == "__main__":
    main()
