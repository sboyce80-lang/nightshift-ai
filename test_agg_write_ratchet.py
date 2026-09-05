#!/usr/bin/env python3
"""Ratchet: direct writes to aggregated_totals are frozen.

Phase 0 rule — gates do not write aggregated_totals directly; every
quantity change goes through _ledger_stage so the reconciliation
invariant (_reconcile_quantity_ledger) can see it. This test freezes the
count of explicit direct-write sites in Takeoff_DIRECT.py at its current
value. If your change adds one, wrap the mutation in a
_ledger_stage(...) snapshot pair instead (see the ledger block around
"Quantity-adjustment ledger"); if the write is genuinely ledgered some
other way, lower/adjust FROZEN_DIRECT_WRITES in the same PR and say why
in the PR description.

(Aliased writes — agg = analysis["aggregated_totals"]; agg[k] = ... —
are not statically countable; the runtime reconciler catches those. This
ratchet stops the explicit form from growing back.)
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

FROZEN_DIRECT_WRITES = 1  # as of 2026-09-05 (the ceiling recompute write)

pattern = re.compile(
    r'\["aggregated_totals"\]\[[^\]]+\]\s*(?:=(?!=)|\+=|-=)')

with open(os.path.join(HERE, "Takeoff_DIRECT.py")) as f:
    src = f.read()

sites = []
for i, line in enumerate(src.splitlines(), 1):
    if pattern.search(line):
        sites.append((i, line.strip()[:100]))

print("aggregated_totals direct-write ratchet")
for ln, txt in sites:
    print(f"  line {ln}: {txt}")
print(f"  found {len(sites)}, frozen at {FROZEN_DIRECT_WRITES}")

if len(sites) > FROZEN_DIRECT_WRITES:
    print("  ❌ new direct write(s) to aggregated_totals. Route the "
          "mutation through _ledger_stage instead.")
    sys.exit(1)
print("  ✓ no new direct writes")
