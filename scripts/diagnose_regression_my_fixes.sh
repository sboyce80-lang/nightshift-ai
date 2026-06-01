#!/usr/bin/env bash
# PATH A — Compare regression results WITH vs. WITHOUT this session's
# Ridgeview fixes. Cheap: no API calls. Uses JSONs already on disk.
#
# What this answers: "Are my 6 Ridgeview fixes (Takeoff_DIRECT.py
# uncommitted edits) helping or hurting the Rider regression cases?"
#
# What this does NOT answer: "Which historical commit caused the
# regression in the first place?" — see diagnose_regression_bisect.sh
# for that.
#
# Usage:
#   bash scripts/diagnose_regression_my_fixes.sh /path/to/output1.json /path/to/output2.json ...
#
# Or rely on the default glob (last 10 construction_analysis_*.json in
# ~/Downloads):
#   bash scripts/diagnose_regression_my_fixes.sh

set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# --- Resolve test inputs ---
if [ "$#" -gt 0 ]; then
  TEST_JSONS=("$@")
else
  # Default: 10 most recent construction_analysis_*.json in Downloads
  mapfile -t TEST_JSONS < <(ls -t "$HOME"/Downloads/construction_analysis_2026*.json 2>/dev/null | head -10)
fi
if [ "${#TEST_JSONS[@]}" -eq 0 ]; then
  echo "ERROR: no JSON files specified or found in ~/Downloads"
  exit 2
fi

echo "Will test ${#TEST_JSONS[@]} JSON(s) against regression_test.py reference cases."
echo

# --- Sanity: must have uncommitted Takeoff_DIRECT.py edits to compare ---
if git diff --quiet Takeoff_DIRECT.py 2>/dev/null; then
  echo "NOTE: Takeoff_DIRECT.py has no uncommitted changes."
  echo "This script compares regression WITH vs WITHOUT your session's"
  echo "edits. Since there are none, this is just a single regression run."
  echo
fi

# --- Phase 1: regression WITH this session's fixes (current working tree) ---
echo "=== Phase 1: regression WITH this session's Takeoff_DIRECT.py edits ==="
WITH_LOG="$(mktemp -t ks_reg_with.XXXXXX)"
python3 regression_test.py --check "${TEST_JSONS[@]}" 2>&1 | tee "$WITH_LOG" || true
echo

# --- Stash this session's edits ---
# Stash ONLY Takeoff_DIRECT.py — leave regression_test.py alone (it's
# the Job bid testing session's work and is the test runner itself).
# scripts/pull_ridgeview_run.py and verify_ridgeview_dedup.py are
# untracked and inert (nothing calls them), so leaving them is safe.
echo "=== Stashing Takeoff_DIRECT.py edits ==="
STASH_REF=""
if ! git diff --quiet Takeoff_DIRECT.py 2>/dev/null; then
  git stash push -m "ridgeview-fixes-WIP-diagnose" -- Takeoff_DIRECT.py
  STASH_REF="$(git stash list | head -1 | cut -d: -f1)"
  echo "Stashed as $STASH_REF"
else
  echo "No edits to stash."
fi
echo

# --- Phase 2: regression WITHOUT this session's fixes (clean HEAD) ---
echo "=== Phase 2: regression on CLEAN HEAD (without Ridgeview fixes) ==="
WITHOUT_LOG="$(mktemp -t ks_reg_without.XXXXXX)"
python3 regression_test.py --check "${TEST_JSONS[@]}" 2>&1 | tee "$WITHOUT_LOG" || true
echo

# --- Restore edits ---
if [ -n "$STASH_REF" ]; then
  echo "=== Restoring stashed edits ==="
  git stash pop "$STASH_REF"
  echo
fi

# --- Phase 3: diff the two runs ---
echo "=== Delta (lines that differ between WITH and WITHOUT my fixes) ==="
diff "$WITHOUT_LOG" "$WITH_LOG" || true
echo
echo "Full logs:"
echo "  WITHOUT fixes: $WITHOUT_LOG"
echo "  WITH fixes:    $WITH_LOG"
echo
echo "Interpretation:"
echo "  * Cases that PASS in WITH but FAIL in WITHOUT → my fixes helped."
echo "  * Cases that FAIL in WITH but PASS in WITHOUT → my fixes regressed."
echo "  * Cases that FAIL in both → the regression is in an older commit;"
echo "    run diagnose_regression_bisect.sh next."
