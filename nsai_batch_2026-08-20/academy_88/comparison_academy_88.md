# 88 Academy Street (RP 26-013) — KnightShiftAI vs JW Estimates

**JW final base bid: $16,163.70**  |  **KSAI subtotal: $30,343.29**  |  **Δ $+14,179.59 (+87.7%)**

- Building: 3-story R-2 rehab, Poughkeepsie NY. JW gross 2,750 SF.
- Manual review: **True** — [MANUAL REVIEW REQUIRED] All 5 extraction draws failed plausibility checks; the priced draw is the least implausible of them. Confirm the takeoff against the drawings before sending.
- Draw-median: K=5, selected draw 4, subtotal spread 8.3%, all_draws_implausible=True
- Runtime 3.2h. Code: nightshift-round3-wt @ k3-round3-fixes (NOT main).

| Line | Unit | JW qty | KSAI qty | qty Δ | JW $ | KSAI $ | $ Δ |
|---|---|---|---|---|---|---|---|
| Walls (gyp/std) | SF | 6,678 | — | — | $5,609.52 | — |  |
| Walls (moisture-res.) | SF | 1,499 | — | — | $1,259.16 | — |  |
| Walls — TOTAL | SF | 8,177 | 11,697 | +43% | $6,868.68 | $9,918.89 | $+3,050.21 |
| Ceilings (gyp) | SF | 2,223 | — | — | $1,867.32 | — |  |
| Ceilings (MR) | SF | 215 | — | — | $180.60 | — |  |
| Ceilings — TOTAL | SF | 2,438 | 3,463 | +42% | $2,047.92 | $4,588.48 | $+2,540.56 |
| Base trim | LF | 616 | 857 | +39% | $2,102.10 | $2,953.74 | $+851.64 |
| Door/window casing | LF | 646 | 0 | -100% | $0.00 | $0.00 | $+0.00 |
| Doors (full paint) | EA | 19 | 5 | -74% | $2,945.00 | $1,192.50 | $-1,752.50 |
| Windows (paint) | EA | 22 | 0 | -100% | $2,200.00 | $0.00 | $-2,200.00 |
| Stairs | sect | 0 | 6 | new | $0.00 | $9,540.00 | $+9,540.00 |
| Gyp. between stairs | SF | 0 | 480 | new | $0.00 | $432.48 | $+432.48 |
| Painted railings | LF | 0 | 90 | new | $0.00 | $1,717.20 | $+1,717.20 |
| **TOTAL** |  |  |  |  | **$16,163.70** | **$30,343.29** | **$+14,179.59** |

## Variance decomposition

| Driver | $ impact | share of $14,180 overage |
|---|---|---|
| Stair complex (6 sections + gyp-between + 90 LF railing) — fabricated | $+11,689.68 | +82% |
| Walls over-read (11,697 vs 8,177 SF, +43%) | $+3,050.21 | +22% |
| Ceilings over-read (3,463 vs 2,438 SF, +42%) + $1.25 vs $0.80 rate | $+2,540.56 | +18% |
| Base trim over-read (857 vs 616 LF, +39%) | $+851.64 | +6% |
| Doors UNDER (5 vs 19) — door-schedule misread | $-1,752.50 | -12% |
| Windows UNDER (0 priced vs 22) — hard-numbers gate | $-2,200.00 | -16% |
| **Net** | **$+14,179.59** | 100% |

## Per-draw stability (K=5)

| Draw | Walls SF | Ceil SF | Base LF | Doors | Stairs | Subtotal |
|---|---|---|---|---|---|---|
| 1 | 11,338 | 3,463 | 1,488 | 5 | — | $27,933.63 |
| 2 | 10,102 | 3,301 | 1,083 | 5 | — | $29,553.43 |
| 3 | 10,761 | 3,257 | 1,361 | 5 | — | $28,394.87 |
| 4 ←priced | 11,697 | 3,463 | 857 | 5 | — | $30,343.29 |
| 5 | 12,038 | 3,739 | 918 | 5 | — | $29,589.78 |
