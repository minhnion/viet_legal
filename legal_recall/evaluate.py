"""Deterministic ranking, group coverage and exact character evidence metrics."""

from collections import defaultdict
from pathlib import Path
import json
import math


def interval_union(spans):
    merged = []
    for span in sorted(spans, key=lambda s: (s["start"], s["end"])):
        start, end = span["start"], span["end"]
        if type(start) is not int or type(end) is not int or start < 0 or start >= end:
            raise ValueError("Invalid evidence span")
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def overlap_size(left, right):
    return sum(max(0, min(b, d) - max(a, c)) for a, b in left for c, d in right)


def score(gold, prediction, k):
    grades = {r["unit_id"]: r["grade"] for r in gold["qrels"]}
    core = {uid for uid, grade in grades.items() if grade == 2}
    ranking = list(dict.fromkeys(prediction.get("ranking", [])))[:k]
    hits = len(core.intersection(ranking))
    recall = hits / len(core) if core else 0
    precision = hits / k
    f2 = 5 * precision * recall / (4 * precision + recall) if precision or recall else 0
    dcg = sum((2 ** grades.get(uid, 0) - 1) / math.log2(i + 2) for i, uid in enumerate(ranking))
    ideal = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(sorted(grades.values(), reverse=True)[:k]))
    groups = gold["requirement_groups"]
    covered = sum(bool(set(g["unit_ids"]).intersection(ranking)) for g in groups)
    expected = defaultdict(list)
    actual = defaultdict(list)
    for r in gold["qrels"]:
        if r["grade"] > 0:
            expected[r["unit_id"]].extend(r["evidence"])
    for item in prediction.get("evidence", []):
        actual[item["unit_id"]].extend(item["spans"])
    expected = {uid: interval_union(s) for uid, s in expected.items()}
    actual = {uid: interval_union(s) for uid, s in actual.items()}
    correct = sum(overlap_size(s, actual.get(uid, [])) for uid, s in expected.items() if uid in ranking)
    expected_size = sum(b - a for s in expected.values() for a, b in s)
    actual_size = sum(b - a for s in actual.values() for a, b in s)
    ep = correct / actual_size if actual_size else 0
    er = correct / expected_size if expected_size else 0
    return {"recall": recall, "precision": precision, "f2": f2, "ndcg": dcg / ideal if ideal else 0,
            "requirement_coverage": covered / len(groups) if groups else 0,
            "complete_coverage": float(bool(groups) and covered == len(groups)),
            "evidence_precision": ep, "evidence_recall": er,
            "evidence_f1": 2 * ep * er / (ep + er) if ep + er else 0,
            "unjudged_returned": sum(uid not in grades for uid in ranking)}


def evaluate(export_dir, prediction_file, k=20):
    if k <= 0:
        raise ValueError("k must be positive")
    gold = [json.loads(line) for line in (Path(export_dir) / "gold_qrels.jsonl").read_text().splitlines() if line.strip()]
    predictions = {}
    for line in Path(prediction_file).read_text().splitlines():
        if line.strip():
            item = json.loads(line)
            if item["query_id"] in predictions:
                raise ValueError("Duplicate prediction query ID")
            predictions[item["query_id"]] = item
    unknown = predictions.keys() - {g["query_id"] for g in gold}
    if unknown:
        raise ValueError("Predictions contain query IDs outside the exported gold set")
    per_query = []
    for g in gold:
        if g["answerability"] == "answerable":
            per_query.append({"query_id": g["query_id"], **score(g, predictions.get(g["query_id"], {}), k)})
    metrics = {key: sum(r[key] for r in per_query) / len(per_query) for key in per_query[0] if key != "query_id"} if per_query else {}
    return {"k": k, "answerable_queries": len(per_query), "metrics": metrics, "per_query": per_query,
            "limitations": "Ranking scores are against judged qrels; unjudged results require review. Clarification queries excluded from ranking metrics."}
