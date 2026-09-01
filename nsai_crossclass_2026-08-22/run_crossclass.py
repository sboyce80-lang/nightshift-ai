#!/usr/bin/env python3
"""Cross-class golden regression orchestrator (2026-08-22).

Question: does the validated 28-flag JW-class set regress Rider-class
accuracy? Runs each Rider golden job under (A) prod-equivalent baseline flags
and (B) baseline + JW flag set, at the current SHA, interleaved per job so
each A/B pair lands as early as possible. Each cell runs in a subprocess with
a wall-clock timeout. Writes results/ + REPORT.md as it goes.
"""
import os, sys, json, subprocess, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, ".venv", "bin", "python")
TIMEOUT_S = 4 * 3600

# smallest first so the harness proves itself before the long runs.
# tsc_fusion_highland = CEILING_ASSUME_PAINTED counter-class (the flag's
# documented risk case); honey_farms_malta = 103-page set exercising the
# DD_MIN_PAGES>=60 single-read consensus rule.
JOB_ORDER = ("dutchess_livestock", "fishkill_397", "364_main",
             "tsc_fusion_highland", "honey_farms_malta")
SEQUENCE = [(job, cfg) for job in JOB_ORDER for cfg in ("baseline", "jwflags")]


def log(m):
    line = f"[{datetime.now(timezone.utc).strftime('%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    with open(os.path.join(HERE, "orchestrator.log"), "a") as f:
        f.write(line + "\n")


def cell(job, cfg):
    p = os.path.join(HERE, "results", f"{job}_{cfg}.json")
    return json.load(open(p)) if os.path.exists(p) else None


def write_report():
    lines = ["# Cross-class golden regression — 2026-08-22",
             "",
             f"SHA: {subprocess.run(['git','rev-parse','--short','HEAD'],cwd=ROOT,capture_output=True,text=True).stdout.strip()}"
             "  |  A = prod-equivalent baseline, B = baseline + 28-flag JW set",
             "",
             "| job | A mean err | B mean err | Δ(B−A) | A subtotal | B subtotal | A mr | B mr | verdict |",
             "|---|---|---|---|---|---|---|---|---|"]
    for job in JOB_ORDER:
        a, b = cell(job, "baseline"), cell(job, "jwflags")
        def me(c): return (f"{c['mean_abs_err_pct']:.1f}%"
                           if c and c.get("ok") and c.get("mean_abs_err_pct") is not None
                           else ("FAIL" if c else "…"))
        def st(c): return f"${c['subtotal']:,.0f}" if c and c.get("ok") else "…"
        def mr(c): return str(c.get("manual_review")) if c and c.get("ok") else "…"
        verdict = ""
        if a and b and a.get("ok") and b.get("ok") and \
           a.get("mean_abs_err_pct") is not None and b.get("mean_abs_err_pct") is not None:
            d = b["mean_abs_err_pct"] - a["mean_abs_err_pct"]
            verdict = ("REGRESSION" if d > 5 else
                       "improved" if d < -5 else "neutral")
            dtxt = f"{d:+.1f}"
        else:
            dtxt = ""
        lines.append(f"| {job} | {me(a)} | {me(b)} | {dtxt} | {st(a)} | {st(b)} | {mr(a)} | {mr(b)} | {verdict} |")
    lines += ["", "## Per-metric detail", ""]
    for job in JOB_ORDER:
        a, b = cell(job, "baseline"), cell(job, "jwflags")
        if not (a and a.get("ok")) and not (b and b.get("ok")):
            continue
        lines.append(f"### {job}")
        lines.append("| metric | target | A actual | A err | B actual | B err |")
        lines.append("|---|---|---|---|---|---|")
        arows = {r["metric"]: r for r in (a or {}).get("rows", [])}
        brows = {r["metric"]: r for r in (b or {}).get("rows", [])}
        for k in sorted(set(arows) | set(brows)):
            ar, br = arows.get(k), brows.get(k)
            tgt = (ar or br)["target"]
            def cellfmt(r):
                return (f"{r['actual']:,.0f} | {r['err_pct']:.0f}%" if r else "… | …")
            lines.append(f"| {k} | {tgt:,.0f} | {cellfmt(ar)} | {cellfmt(br)} |")
        lines.append("")
    with open(os.path.join(HERE, "REPORT.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    # fresh checkpoint namespace: back up the live sheet_checkpoints once
    ckpt = os.path.join(ROOT, ".cache", "sheet_checkpoints")
    bak = ckpt + ".pre_crossclass_2026-08-22"
    if os.path.isdir(ckpt) and not os.path.isdir(bak):
        os.rename(ckpt, bak)
        log(f"checkpoints: moved live dir aside -> {os.path.basename(bak)}")
    for job, cfg in SEQUENCE:
        if cell(job, cfg):
            log(f"SKIP {job}/{cfg} — result exists")
            continue
        log(f"START {job} [{cfg}]")
        t0 = time.time()
        try:
            r = subprocess.run(
                [PY, os.path.join(HERE, "run_child.py"), job, cfg],
                cwd=ROOT, timeout=TIMEOUT_S,
                stdout=open(os.path.join(HERE, "results", f"{job}_{cfg}.run.log"), "w"),
                stderr=subprocess.STDOUT)
            c = cell(job, cfg)
            if c and c.get("ok"):
                log(f"DONE {job} [{cfg}] mean_err="
                    f"{c['mean_abs_err_pct']:.1f}% subtotal=${c['subtotal']:,.0f} "
                    f"mr={c['manual_review']} ({c['elapsed_s']/60:.0f}m)")
            else:
                log(f"FAILED {job} [{cfg}] rc={r.returncode} "
                    f"err={(c or {}).get('error','no result.json')}")
        except subprocess.TimeoutExpired:
            log(f"TIMEOUT {job} [{cfg}] after {TIMEOUT_S/3600:.0f}h")
        write_report()
    log("ALL CELLS DONE")
    write_report()


if __name__ == "__main__":
    main()
