import json
import sys

from cognitive_engine import analyze_cdsco_failure_batch
from temporal_tasks import calculate_base_score

FIELDS = ["is_paper_failure", "violates_rule_96", "violates_sub_rule_7", "violates_schedule_h2"]


def main(path: str = "eval_dataset.json", event_type: str = "NSQ_DRUG") -> None:
    data = json.load(open(path))
    items = [
        {"drug_name": r["drug_name"], "manufacturer": r["manufacturer"],
         "batch_no": r["batch_no"], "reason": r["reason"], "event_date": r.get("event_date")}
        for r in data
    ]

    predictions = analyze_cdsco_failure_batch(items)
    if not predictions or all(p == {} for p in predictions.values()):
        print("No LLM predictions returned (is GROQ_API_KEY set?).")
        sys.exit(1)

    stats = {f: {"tp": 0, "fp": 0, "tn": 0, "fn": 0} for f in FIELDS}
    exact_matches = 0
    score_delta_sum = 0

    print(f"\n{'#':>2} {'drug':<34} " + "".join(f"{f[:11]:>13}" for f in FIELDS) + f"  {'GT':>3} {'Pred':>4}")
    for i, (rec, item) in enumerate(zip(data, items)):
        pred = predictions.get(str(i), {})
        gt = rec["ground_truth"]
        pred_flags = {f: bool(pred.get(f, False)) for f in FIELDS}

        row_flags = []
        for f in FIELDS:
            p, g = pred_flags[f], gt[f]
            if p and g: stats[f]["tp"] += 1
            elif p and not g: stats[f]["fp"] += 1
            elif not p and not g: stats[f]["tn"] += 1
            else: stats[f]["fn"] += 1
            row_flags.append("1" if p else ".")

        gt_score = calculate_base_score(event_type, gt, rec.get("event_date"))
        pred_score = calculate_base_score(event_type, pred_flags, rec.get("event_date"))
        score_delta_sum += pred_score - gt_score
        exact = pred_flags == gt
        exact_matches += exact

        print(f"{i:>2} {rec['drug_name'][:34]:<34} " + "".join(f"{x:>13}" for x in row_flags) +
              f"  {gt_score:>3} {pred_score:>4}")

    print("\n--- Per-field metrics ---")
    for f in FIELDS:
        s = stats[f]
        total = s["tp"] + s["tn"] + s["fp"] + s["fn"]
        acc = (s["tp"] + s["tn"]) / total if total else 0
        prec = s["tp"] / (s["tp"] + s["fp"]) if (s["tp"] + s["fp"]) else float("nan")
        rec = s["tp"] / (s["tp"] + s["fn"]) if (s["tp"] + s["fn"]) else float("nan")
        print(f"  {f:<22} acc={acc:.2f}  prec={prec:.2f}  recall={rec:.2f}  "
              f"FP={s['fp']} FN={s['fn']}  (n={total})")

    n = len(data)
    print(f"\nFull-row exact match: {exact_matches}/{n}")
    print(f"Mean score inflation (predicted - ground-truth): +{score_delta_sum / n:.1f} pts")


if __name__ == "__main__":
    main()
