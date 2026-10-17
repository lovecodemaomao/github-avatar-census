"""Reproduce the two-stage rule on the fixed 150-image audit sample."""
from __future__ import annotations

import csv

from census import DATA
from evaluate_audit import evaluate


base = DATA / "audit"
with (base / "qwen3-vl_4b_trial.csv").open(encoding="utf-8-sig", newline="") as file:
    full = {r["audit_index"]: r for r in csv.DictReader(file)}
with (base / "qwen3-vl_4b_nothink_trial.csv").open(encoding="utf-8-sig", newline="") as file:
    fast = list(csv.DictReader(file))
assert len(full) == len(fast) == 150
for row in fast:
    if row["prediction"] == "uncertain":
        row["prediction"] = full[row["audit_index"]]["prediction"]
path = base / "pipeline_trial.csv"
with path.open("w", encoding="utf-8-sig", newline="") as file:
    writer = csv.DictWriter(file, fieldnames=list(fast[0].keys()))
    writer.writeheader()
    writer.writerows(fast)
for split in ("validation", "holdout"):
    n,tp,fp,fn,agree = evaluate(fast, split)
    print(f"{split}: n={n}, anime TP={tp}, FP={fp}, FN={fn}, all-category agreement={agree}/{n}")
