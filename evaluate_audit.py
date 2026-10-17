"""Compare a model's anime detection against the fixed manual audit sample."""
from __future__ import annotations

import argparse
import csv
import sqlite3

from census import DATA, DB_PATH


def evaluate(rows: list[dict], split: str) -> tuple[int, int, int, int, int]:
    parity = 1 if split == "validation" else 0
    chosen = [r for r in rows if int(r["audit_index"]) % 2 == parity and r.get("prediction")]
    tp = sum(r["human_label"] == "anime" and r["prediction"] == "anime" for r in chosen)
    fp = sum(r["human_label"] != "anime" and r["prediction"] == "anime" for r in chosen)
    fn = sum(r["human_label"] == "anime" and r["prediction"] != "anime" for r in chosen)
    return len(chosen), tp, fp, fn, sum(r["human_label"] == r["prediction"] for r in chosen)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gemma3:4b")
    parser.add_argument("--trial", action="store_true")
    args = parser.parse_args()
    sample_path = DATA / "audit" / "audit_sample.csv"
    with sample_path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    if args.trial:
        path = DATA / "audit" / f"{args.model.replace(':','_')}_trial.csv"
        with path.open(encoding="utf-8-sig", newline="") as file:
            predictions = {r["user_id"]: r["prediction"] for r in csv.DictReader(file)}
    else:
        con = sqlite3.connect(DB_PATH)
        predictions = {str(r[0]): r[1] for r in con.execute("""SELECT a.user_id,c.label FROM audit a JOIN avatars av ON av.user_id=a.user_id JOIN classifications c ON c.sha256=av.sha256 WHERE c.model=? AND c.status='ok'""", (args.model,))}
    for row in rows:
        row["prediction"] = predictions.get(row["user_id"], "")
    for split in ("validation", "holdout"):
        n,tp,fp,fn,agree = evaluate(rows,split)
        precision = tp/(tp+fp) if tp+fp else 0
        recall = tp/(tp+fn) if tp+fn else 0
        print(f"{split}: n={n}, anime TP={tp} FP={fp} FN={fn}, precision={precision:.3f}, recall={recall:.3f}, all-category agreement={agree}/{n}")


if __name__ == "__main__":
    main()
