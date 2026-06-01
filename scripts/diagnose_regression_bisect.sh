#!/usr/bin/env bash
# PATH B — Find the historical commit that broke a specific regression
# case. EXPENSIVE: each bisect step re-extracts a PDF through
# Takeoff_DIRECT.py at that commit's state, which costs API tokens (~$2-5
# per extraction depending on PDF size).
#
# This script does NOT auto-run the bisect — it generates the bisect
# script for you, with the right "good" and "bad" SHAs and a runner that
# does extraction + regression_test in one pass. You inspect, then run.
#
# Usage:
#   bash scripts/diagnose_regression_bisect.sh <pdf_path> <case_keyword>
#
# Example:
#   bash scripts/diagnose_regression_bisect.sh ~/Downloads/cenHud_Fishkill.pdf fishkill
#
# Inputs:
#   <pdf_path>      Original PDF to re-extract at each bisect step.
#   <case_keyword>  Keyword from regression_test.py's match_keywords
#                   (e.g. "fishkill", "dutchess", "honey farms", "364",
#                   "tsc", "4651", "grenadier") that identifies which
#                   reference case this PDF tests.

set -eu

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <pdf_path> <case_keyword>"
  exit 2
fi

PDF_PATH="$1"
CASE_KW="$2"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

if [ ! -f "$PDF_PATH" ]; then
  echo "ERROR: PDF not found: $PDF_PATH"
  exit 2
fi

# --- Candidate "good" SHAs from the extraction-touching commit list ---
# Choose the OLDEST commit you remember the case passing on. If
# uncertain, start before the recent burst of B1-B8 / I1-I5 changes.
echo "Extraction-touching commits in last 14 days (newest first):"
echo
git log --oneline --since="14 days ago" --format='  %h  %s' \
  -- Takeoff_DIRECT.py config.py
echo
echo "Pick a GOOD SHA (oldest commit you believe the case PASSED on)."
echo "Common starting point: a commit BEFORE ffc9701 'Fix 8 beta-flagged"
echo "extraction bugs (B1-B8)' which bundled many changes."
echo
read -r -p "GOOD SHA (commit known to pass) > " GOOD_SHA
read -r -p "BAD SHA  (commit known to fail, usually HEAD: 4190670) > " BAD_SHA
GOOD_SHA="${GOOD_SHA:-}"
BAD_SHA="${BAD_SHA:-HEAD}"

if [ -z "$GOOD_SHA" ]; then
  echo "ERROR: GOOD SHA is required"
  exit 2
fi

# --- Generate the bisect run script ---
RUN_SCRIPT="$(mktemp -t ks_bisect_run.XXXXXX.sh)"
chmod +x "$RUN_SCRIPT"
cat > "$RUN_SCRIPT" <<EOF
#!/usr/bin/env bash
# Bisect step runner. Re-extracts $PDF_PATH at the current commit, then
# checks regression. Exit 0 = pass (good commit), 1 = fail (bad commit),
# 125 = skip (cannot test this commit).
set -eu
cd "$REPO"

# Run extraction. Use the same args you'd use in production.
OUT_DIR=\$(mktemp -d -t ks_bisect_out.XXXXXX)
python3 Takeoff_DIRECT.py \\
  --rfp_file "$PDF_PATH" \\
  --contact_name "bisect" \\
  --contact_email "bisect@knightshiftai.com" \\
  --output_dir "\$OUT_DIR" 2>&1 | tail -5
OUT_JSON=\$(ls -t "\$OUT_DIR"/construction_analysis_*.json 2>/dev/null | head -1)
if [ -z "\$OUT_JSON" ]; then
  echo "Bisect step: extraction failed at \$(git rev-parse --short HEAD), skipping"
  exit 125
fi

# Check regression. Filter to just the case we care about by keyword.
python3 regression_test.py --check "\$OUT_JSON" 2>&1 | tee /tmp/bisect_step.log
# Bail with non-zero if our case appears in the failure list.
if grep -qi "$CASE_KW.*FAIL\\|FAIL.*$CASE_KW" /tmp/bisect_step.log; then
  echo "Bisect step: case '$CASE_KW' FAILED at \$(git rev-parse --short HEAD)"
  exit 1
fi
echo "Bisect step: case '$CASE_KW' PASSED at \$(git rev-parse --short HEAD)"
exit 0
EOF

# --- Print the bisect commands for the user to inspect, then run ---
cat <<EOF

================================================================================
Bisect plan ready. Review the runner, then execute the commands below.

Runner script: $RUN_SCRIPT
  (it does: extract \$PDF_PATH → check regression for '$CASE_KW' → exit 0 or 1)

Important: each step will:
  * Re-extract $PDF_PATH at the bisect commit (API call, ~\$2-5)
  * Take 3-10 minutes per commit depending on PDF size
  * git bisect typically needs log2(N) steps to localize a bad commit

For ~12 candidate commits, expect ~4 steps, ~20-40 min, ~\$10-20.

Step 1 — Stash all your work-in-progress (bisect needs a clean tree):
    git stash push -m "pre-bisect-WIP" \\
      Takeoff_DIRECT.py regression_test.py

Step 2 — Start the bisect:
    git bisect start
    git bisect bad $BAD_SHA
    git bisect good $GOOD_SHA

Step 3 — Run the bisect (or do it manually with git bisect bad/good):
    git bisect run "$RUN_SCRIPT"

Step 4 — When bisect prints "<sha> is the first bad commit", inspect:
    git show <sha>

Step 5 — Reset and restore:
    git bisect reset
    git stash pop

If anything goes wrong mid-bisect:
    git bisect reset   # always safe, returns to HEAD
    git stash pop      # restore your work-in-progress

================================================================================
EOF
