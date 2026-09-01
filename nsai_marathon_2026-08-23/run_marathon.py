#!/usr/bin/env python3
"""Marathon orchestrator: all 10 goldens (5 Rider + 5 JW) at 6efa4d2
under the class-gated candidate flag posture. Fresh checkpoints, fresh
extraction — the honest current-state scoreboard. Ascending expected
runtime; Homewood last. MARATHON.md rewritten after every job."""
import os, sys, json, subprocess, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, ".venv", "bin", "python")
TIMEOUT_S = 4 * 3600

SEQUENCE = ["dutchess_livestock", "tsc_fusion_highland", "fishkill_397",
            "jw_caris_hyde_park", "jw_harlem_valley", "364_main",
            "jw_hudson_hotel", "jw_under_canvas_ulum",
            "honey_farms_malta", "jw_homewood_suites"]


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
                         cwd=ROOT, capture_output=True, text=True
                         ).stdout.strip()
    lines = [f"# Marathon scoreboard — 2026-08-23 · {sha} · "
             f"class-gated candidate posture", "",
             "| job | class | KS subtotal | target bid | Δ | ±10% | mr | mean qty err |",
             "|---|---|---|---|---|---|---|---|"]
    n_band = n_scored = 0
    for job in SEQUENCE:
        c = cell(job)
        if not c:
            lines.append(f"| {job} | | … | | | | | |")
            continue
        if not c.get("ok"):
            lines.append(f"| {job} | {c.get('cls','')} | FAILED "
                         f"{str(c.get('error'))[:40]} | | | | | |")
            continue
        bid = c.get("bid")
        d = c.get("delta_pct")
        band = c.get("in_band_10")
        if band is not None:
            n_scored += 1
            n_band += 1 if band else 0
        me = c.get("mean_abs_err_pct")
        lines.append(
            f"| {job} | {c['cls']} | ${c['subtotal']:,.0f} | "
            f"{('$%s' % format(bid, ',.0f')) if bid else 'qty only'} | "
            f"{('%+.1f%%' % d) if d is not None else '—'} | "
            f"{'✅' if band else ('—' if band is None else '✗')} | "
            f"{'Y' if c.get('manual_review') else 'N'} | "
            f"{('%.1f%%' % me) if me is not None else '—'} |")
    lines += ["", f"**In band: {n_band}/{n_scored} scored**", ""]
    with open(os.path.join(HERE, "MARATHON.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    ckpt = os.path.join(ROOT, ".cache", "sheet_checkpoints")
    bak = ckpt + ".pre_marathon_2026-08-23"
    if os.path.isdir(ckpt) and not os.path.isdir(bak):
        os.rename(ckpt, bak)
        log(f"checkpoints: moved aside -> {os.path.basename(bak)}")
    for job in SEQUENCE:
        if cell(job):
            log(f"SKIP {job} — result exists")
            continue
        log(f"START {job}")
        try:
            subprocess.run(
                [PY, os.path.join(HERE, "run_marathon_child.py"), job],
                cwd=ROOT, timeout=TIMEOUT_S,
                stdout=open(os.path.join(HERE, "results",
                                         f"{job}.run.log"), "w"),
                stderr=subprocess.STDOUT)
            c = cell(job)
            if c and c.get("ok"):
                log(f"DONE {job} subtotal=${c['subtotal']:,.0f} "
                    f"delta={c.get('delta_pct')} band={c.get('in_band_10')} "
                    f"mr={c.get('manual_review')} "
                    f"({c['elapsed_s']/60:.0f}m)")
            else:
                log(f"FAILED {job} err={(c or {}).get('error', 'no meta')}")
        except subprocess.TimeoutExpired:
            log(f"TIMEOUT {job} after {TIMEOUT_S/3600:.0f}h")
        write_report()
    log("MARATHON COMPLETE")
    write_report()


if __name__ == "__main__":
    main()
