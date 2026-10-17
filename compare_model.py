"""Run a candidate Ollama model on the fixed, manually labeled audit sample."""
from __future__ import annotations

import argparse
import base64
import csv
import json
import time
from pathlib import Path

import requests

from census import DATA, PROMPT, ROOT, normalize_label


def predict(model: str, image_path: Path, no_think: bool = False) -> str:
    image64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    for attempt in range(3):
        response = requests.post("http://localhost:11434/api/chat", json={
            "model": model, "stream": False, "format": "json", "think": not no_think,
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": PROMPT, "images": [image64]}],
        }, timeout=180)
        response.raise_for_status()
        body = response.json()
        message = body.get("message", {})
        raw = message.get("content") or (message.get("thinking", "") if no_think else "")
        if body.get("done") and raw.strip():
            return normalize_label(json.loads(raw))
        time.sleep(1 + attempt)
    raise RuntimeError("empty/incomplete model response")


def scores(rows: list[dict], parity: int) -> str:
    sample = [r for r in rows if int(r["audit_index"]) % 2 == parity and r["prediction"]]
    tp = sum(r["human_label"] == "anime" and r["prediction"] == "anime" for r in sample)
    fp = sum(r["human_label"] != "anime" and r["prediction"] == "anime" for r in sample)
    fn = sum(r["human_label"] == "anime" and r["prediction"] != "anime" for r in sample)
    return f"n={len(sample)} anime TP={tp} FP={fp} FN={fn} precision={tp/(tp+fp) if tp+fp else 0:.3f} recall={tp/(tp+fn) if tp+fn else 0:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3-vl:4b")
    parser.add_argument("--no-think", action="store_true")
    parser.add_argument("--validation-only", action="store_true")
    args = parser.parse_args()
    sample = DATA / "audit" / "audit_sample.csv"
    suffix = "_nothink" if args.no_think else ""
    output = DATA / "audit" / f"{args.model.replace(':','_')}{suffix}_trial.csv"
    with sample.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    prior = {}
    if output.exists():
        with output.open(encoding="utf-8-sig", newline="") as file:
            prior = {r["audit_index"]: r for r in csv.DictReader(file)}
    results = []
    for index, row in enumerate(rows, 1):
        if args.validation_only and index % 2 == 0:
            continue
        old = prior.get(row["audit_index"])
        if old and old["prediction"]:
            results.append(old)
            continue
        try:
            prediction = predict(args.model, ROOT / row["avatar_path"], args.no_think)
            error = ""
        except Exception as exc:
            prediction, error = "", str(exc)
        results.append({"audit_index": row["audit_index"], "user_id": row["user_id"], "human_label": row["human_label"], "prediction": prediction, "error": error})
        with output.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=["audit_index", "user_id", "human_label", "prediction", "error"])
            writer.writeheader()
            writer.writerows(results)
        if index % 25 == 0:
            print(f"audited {index}/{len(rows)}", flush=True)
    print("validation (odd):", scores(results, 1))
    print("holdout (even):", scores(results, 0))


if __name__ == "__main__":
    main()
