"""Human review is an explicit, validated gate; model agreement never creates gold."""

from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
import json
import time
from jsonschema import Draft202012Validator

from .config import canonical, digest
from .store import write_jsonl
from .text import leakage, sentences
from .evaluate import interval_union


REVIEW_SCHEMA = {
    "type": "object", "required": ["query_id", "base_hash", "reviewer_id", "decision"],
    "properties": {
        "query_id": {"type": "string"}, "base_hash": {"type": "string"}, "reviewer_id": {"type": "string"},
        "decision": {"enum": ["unreviewed", "approve", "reject", "adjudicate"]},
        **{k: {"type": "boolean"} for k in ["facts_checked", "query_checked", "family_checked", "coverage_checked"]},
        "answerability": {"type": "string"}, "issue_text": {"type": "string"}, "query_text": {"type": "string"},
        "facts_sentence_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "qrels": {"type": "array", "items": {"type": "object", "required": ["unit_id", "grade", "evidence"],
            "properties": {"unit_id": {"type": "string"}, "grade": {"type": "integer", "enum": [0, 1, 2]},
                "evidence": {"type": "array", "items": {"type": "object", "required": ["start", "end"],
                    "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}}}}}}},
        "requirement_groups": {"type": "array", "items": {"type": "object", "required": ["group_id", "unit_ids"],
            "properties": {"group_id": {"type": "string"}, "unit_ids": {"type": "array", "items": {"type": "string"}}}}},
        "version_confirmations": {"type": "array", "items": {"type": "object", "required": ["unit_id", "verified", "official_source_url", "version_key", "version_description"],
            "properties": {"unit_id": {"type": "string"}, "verified": {"type": "boolean"}, "official_source_url": {"type": "string"}, "version_key": {"type": "string"}, "version_description": {"type": "string"}}}},
        "resolves_review_hashes": {"type": "array", "items": {"type": "string"}}, "notes": {"type": "string"}
    }
}


class ReviewError(ValueError):
    pass


def query_hash(query):
    return digest(query)


def active_queries(store):
    run = store.get_meta("active_run")
    if run and store.get_meta("active_snapshot") != store.snapshot():
        raise ReviewError("Corpus/families changed since annotation; annotate the new snapshot before review/export")
    for row in store.connection.execute("SELECT payload FROM queries WHERE run_hash=? ORDER BY id", (run,)):
        yield json.loads(row[0])


def review_template(query):
    proposals = query["annotation"]["assessments"]
    positive = [a for a in proposals if a["relevance_proposal"] in {"core_candidate", "supporting"}]
    return {"query_id": query["id"], "base_hash": query_hash(query), "reviewer_id": "", "decision": "unreviewed",
            "facts_checked": False, "query_checked": False, "family_checked": False, "coverage_checked": False,
            "answerability": query["model_input"]["answerability_proposal"],
            "facts_sentence_ids": [f["sentence_id"] for f in query["model_input"]["facts"]],
            "issue_text": query["model_input"]["issue"], "query_text": query["model_input"]["question"],
            "qrels": [{"unit_id": a["candidate_id"], "grade": 2 if a["relevance_proposal"] == "core_candidate" else 1,
                       "evidence": [{"start": e["start"], "end": e["end"]} for e in a["evidence"]]} for a in positive],
            "requirement_groups": [{"group_id": g["group_id"], "unit_ids": g["candidate_ids"]}
                                   for g in query["annotation"]["set_review"].get("proposed_requirement_groups", [])],
            "version_confirmations": [{"unit_id": a["candidate_id"], "verified": False,
                                       "official_source_url": "", "version_key": "", "version_description": "",
                                       "effective_from": None, "effective_until": None} for a in positive],
            "resolves_review_hashes": [], "notes": ""}


def export_review(config, store):
    root = config.output_dir / "review"
    queries = list(active_queries(store))
    write_jsonl(root / "template.jsonl", (review_template(q) for q in queries))
    def independent_templates():
        for q in queries:
            template = review_template(q)
            template.update(qrels=[], requirement_groups=[], version_confirmations=[])
            yield template
    write_jsonl(root / "independent_template.jsonl", independent_templates())

    def bundles():
        for q in queries:
            case = store.connection.execute("SELECT text FROM cases WHERE id=?", (q["case_id"],)).fetchone()
            units = [store.get_unit(uid) for uid in q["annotation"]["pool_ids"]]
            yield {"query": q, "base_hash": query_hash(q), "judgment_text": case[0], "sources": units,
                   "instructions": "Duplicate template for independent reviewers. Fill decisions/checks/evidence/official version sources; never import model proposals as approvals."}
    write_jsonl(root / "bundles.jsonl", bundles())
    write_jsonl(root / "independent_bundles.jsonl", ({"query_id": q["id"], "base_hash": query_hash(q),
                "model_input": q["model_input"],
                "sources": [store.get_unit(uid) for uid in sorted(q["annotation"]["pool_ids"])],
                "instructions": "Independently assess the query and sources before opening model proposals or the judgment citation list."}
                for q in queries))
    return {"queries": len(queries), "template": str(root / "template.jsonl"), "bundles": str(root / "bundles.jsonl")}


def valid_official_url(url, domains):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return (parts.scheme in {"http", "https"} and not parts.username and not parts.password
            and any(host == d.lower() or host.endswith("." + d.lower()) for d in domains))


def validate_review(review, query, store, config):
    if list(Draft202012Validator(REVIEW_SCHEMA).iter_errors(review)):
        raise ReviewError("Review does not match the expected JSON types/structure")
    if not isinstance(review, dict) or review.get("base_hash") != query_hash(query):
        raise ReviewError("Review base_hash does not match the current candidate query")
    reviewer = review.get("reviewer_id")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ReviewError("Nonempty reviewer_id is required")
    decision = review.get("decision")
    if decision not in {"approve", "reject", "adjudicate"}:
        raise ReviewError("Review must be approve, reject or adjudicate; unreviewed templates cannot be imported")
    if decision == "reject":
        return
    for key in ["facts_checked", "query_checked", "family_checked", "coverage_checked"]:
        if review.get(key) is not True:
            raise ReviewError(f"{key} must explicitly be true for gold approval")
    if review.get("answerability") not in {"answerable", "needs_clarification"}:
        raise ReviewError("Only answerable/needs_clarification supported here; corpus_missing needs a controlled withholding experiment")
    if not isinstance(review.get("query_text"), str) or not review["query_text"].strip() or not isinstance(review.get("issue_text"), str) or not review["issue_text"].strip():
        raise ReviewError("Reviewed query and issue must be nonempty")
    if leakage(review["query_text"] + " " + review["issue_text"]):
        raise ReviewError("Reviewed query contains explicit citation or court-outcome leakage")
    case = store.connection.execute("SELECT text FROM cases WHERE id=?", (query["case_id"],)).fetchone()
    allowed = {s["id"]: s for s in sentences(case[0])}
    ids = review.get("facts_sentence_ids")
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or any(i not in allowed for i in ids):
        raise ReviewError("Reviewed fact IDs must be unique existing source sentence IDs")
    if any(allowed[i]["forbidden_section"] or allowed[i]["explicit_legal_or_outcome"] for i in ids):
        raise ReviewError("Reviewed facts include a forbidden reasoning/outcome/citation sentence")
    qrels = review.get("qrels")
    if not isinstance(qrels, list):
        raise ReviewError("qrels must be a list")
    positives, core, seen = set(), set(), set()
    for qrel in qrels:
        uid, grade = qrel.get("unit_id"), qrel.get("grade")
        unit = store.get_unit(uid)
        if not unit or unit["parent_id"] is not None or uid in seen:
            raise ReviewError("Qrels must reference distinct indexed article units")
        seen.add(uid)
        if type(grade) is not int or grade not in {0, 1, 2}:
            raise ReviewError("Grade must be integer 0, 1 or 2")
        evidence = qrel.get("evidence")
        if not isinstance(evidence, list) or (grade > 0 and not evidence):
            raise ReviewError("Positive qrels require evidence spans")
        for span in evidence:
            start, end = span.get("start"), span.get("end")
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(unit["text"]):
                raise ReviewError("Evidence offsets must be nonempty Unicode character spans within the unit text")
        if grade > 0:
            if not unit["article"]:
                raise ReviewError("Positive sources require an identified original article; repair the source mapping first")
            positives.add(uid)
        if grade == 2:
            core.add(uid)
    confirmations = review.get("version_confirmations")
    if not isinstance(confirmations, list):
        raise ReviewError("version_confirmations must be a list")
    confirmed = set()
    for item in confirmations:
        uid = item.get("unit_id")
        if uid not in positives or uid in confirmed or item.get("verified") is not True:
            raise ReviewError("Every version confirmation must uniquely verify a positive source")
        if not valid_official_url(item.get("official_source_url", ""), config.official_domains):
            raise ReviewError("Version verification requires a source URL on a configured official domain")
        if not isinstance(item.get("version_description"), str) or not item["version_description"].strip():
            raise ReviewError("A reviewed version description is required")
        if not item.get("version_key", "").strip():
            raise ReviewError("A shared, reviewed version_key is required")
        for field in ["effective_from", "effective_until"]:
            if item.get(field) is not None:
                try:
                    date.fromisoformat(item[field])
                except (TypeError, ValueError):
                    raise ReviewError("Version dates must be ISO dates or null") from None
        if item.get("effective_from") and item.get("effective_until") and item["effective_from"] > item["effective_until"]:
            raise ReviewError("Version interval is reversed")
        confirmed.add(uid)
    if confirmed != positives:
        raise ReviewError("Every positive source needs explicit version verification")
    groups = review.get("requirement_groups")
    if not isinstance(groups, list):
        raise ReviewError("requirement_groups must be a list")
    group_ids, covered = set(), set()
    for group in groups:
        members = group.get("unit_ids")
        if not group.get("group_id") or group["group_id"] in group_ids or not isinstance(members, list) or not members or len(members) != len(set(members)):
            raise ReviewError("Requirement groups need unique IDs and nonempty unique members")
        if not set(members) <= core:
            raise ReviewError("Requirement alternatives must be reviewed core sources")
        group_ids.add(group["group_id"])
        covered.update(members)
    if review["answerability"] == "answerable" and (not core or not groups or covered != core):
        raise ReviewError("Answerable gold needs core sources and requirement groups covering every core source")
    if review["answerability"] == "needs_clarification" and (qrels or groups or confirmations):
        raise ReviewError("needs_clarification gold must not assert uniquely correct sources")


def import_reviews(path, config, store, replace=False):
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if list(Draft202012Validator(REVIEW_SCHEMA).iter_errors(record)):
                raise ReviewError("Review import contains an invalid JSON record")
            row = store.connection.execute("SELECT payload FROM queries WHERE id=? AND run_hash=?", (record.get("query_id"), store.get_meta("active_run"))).fetchone()
            if not row:
                raise ReviewError("Review query_id is not in the active annotation run")
            validate_review(record, json.loads(row[0]), store, config)
            records.append(record)
    keys = [(r["query_id"], r["reviewer_id"]) for r in records]
    if len(keys) != len(set(keys)):
        raise ReviewError("Duplicate query/reviewer in import file")
    with store.transaction():
        for record in records:
            h = digest(record)
            prior = store.connection.execute("SELECT review_hash FROM reviews WHERE query_id=? AND reviewer_id=?", (record["query_id"], record["reviewer_id"])).fetchone()
            if prior and prior[0] != h and not replace:
                raise ReviewError("Reviewer already submitted different content; use --replace explicitly")
            if not prior or prior[0] != h:
                store.connection.execute("INSERT INTO review_events(query_id,reviewer_id,review_hash,payload,created) VALUES (?,?,?,?,?)",
                                         (record["query_id"], record["reviewer_id"], h, canonical(record), time.time()))
            store.connection.execute("INSERT INTO reviews VALUES (?,?,?,?) ON CONFLICT(query_id,reviewer_id) DO UPDATE SET review_hash=excluded.review_hash,payload=excluded.payload",
                                     (record["query_id"], record["reviewer_id"], h, canonical(record)))
    return {"imported": len(records)}


def target_signature(review):
    return digest({"answerability": review["answerability"], "facts": sorted(review["facts_sentence_ids"]),
                   "issue": review["issue_text"], "query": review["query_text"],
                   "qrels": sorted([{"unit_id": r["unit_id"], "grade": r["grade"], "evidence": interval_union(r["evidence"])}
                                    for r in review["qrels"]], key=lambda r: r["unit_id"]),
                   "groups": sorted([sorted(g["unit_ids"]) for g in review["requirement_groups"]]),
                   "versions": sorted([{k: v.get(k) for k in ["unit_id", "version_key", "effective_from", "effective_until"]}
                                       for v in review["version_confirmations"]], key=lambda v: v["unit_id"])})


def accepted_review(query, store):
    reviews = [json.loads(r[0]) for r in store.connection.execute("SELECT payload FROM reviews WHERE query_id=? ORDER BY reviewer_id", (query["id"],))]
    reviews = [r for r in reviews if r["base_hash"] == query_hash(query)]
    ordinary = [r for r in reviews if r["decision"] in {"approve", "reject"}]
    approvals = [r for r in ordinary if r["decision"] == "approve"]
    adjudications = [r for r in reviews if r["decision"] == "adjudicate"]
    if len(ordinary) >= 2 and adjudications:
        hashes = {digest(r) for r in ordinary}
        valid = [r for r in adjudications if set(r.get("resolves_review_hashes", [])) == hashes
                 and r["reviewer_id"] not in {o["reviewer_id"] for o in ordinary}]
        if len(valid) == 1:
            return valid[0], {"status": "adjudicated", "reviewers": [r["reviewer_id"] for r in ordinary], "adjudicator": valid[0]["reviewer_id"]}
        return None, {"status": "invalid_or_multiple_adjudications"}
    if len(approvals) >= 2 and len(approvals) == len(ordinary) and len({target_signature(r) for r in approvals}) == 1:
        return approvals[0], {"status": "independent_reviews_agreed", "reviewers": [r["reviewer_id"] for r in approvals]}
    return None, {"status": "awaiting_reviews_or_adjudication", "reviews": len(ordinary),
                  "review_hashes": [digest(r) for r in ordinary]}


def export_dataset(config, store, include_corpus=False):
    root = config.output_dir / "export"
    queries = list(active_queries(store))
    write_jsonl(root / "candidates.jsonl", queries)
    write_jsonl(root / "failures.jsonl", (dict(r) for r in store.connection.execute("SELECT * FROM failures ORDER BY id")))
    accepted, statuses = [], []
    for query in queries:
        review, status = accepted_review(query, store)
        statuses.append({"query_id": query["id"], **status})
        if review:
            validate_review(review, query, store, config)
            accepted.append((query, review, status))
    write_jsonl(root / "review_status.jsonl", statuses)
    gold_queries, gold_qrels, evidence, provenance = [], [], [], []
    for q, r, status in accepted:
        case = store.connection.execute("SELECT text FROM cases WHERE id=?", (q["case_id"],)).fetchone()
        sentence_map = {s["id"]: s for s in sentences(case[0])}
        gold_queries.append({"query_id": q["id"], "split": q["split"], "quality": "gold_human_reviewed",
                             "facts": [sentence_map[i]["text"] for i in r["facts_sentence_ids"]],
                             "issue": r["issue_text"], "question": r["query_text"], "corpus_snapshot_id": q["model_input"]["corpus_snapshot_id"]})
        gold_qrels.append({"query_id": q["id"], "answerability": r["answerability"], "qrels": r["qrels"], "requirement_groups": r["requirement_groups"]})
        for item in r["qrels"]:
            evidence.append({"query_id": q["id"], "unit_id": item["unit_id"], "spans": item["evidence"]})
        provenance.append({"query_id": q["id"], "family_id": q["family_id"], "private_provenance": q["private_provenance"],
                           "review": status, "version_confirmations": r["version_confirmations"], "review_hash": digest(r)})
    for name, values in [("gold_queries", gold_queries), ("gold_qrels", gold_qrels), ("gold_evidence", evidence), ("gold_provenance", provenance)]:
        write_jsonl(root / (name + ".jsonl"), values)
    corpus_count = None
    if include_corpus:
        def units():
            for row in store.connection.execute("SELECT id FROM units WHERE parent_id IS NULL ORDER BY id"):
                yield store.get_unit(row[0])
        corpus_count = write_jsonl(root / "corpus.jsonl", units())
    report = {"run_hash": store.get_meta("active_run"), "snapshot": store.snapshot(),
              "candidate_queries": len(queries), "gold_queries": len(accepted), "corpus_units_exported": corpus_count,
              "gold_policy": "Two distinct agreeing human reviews or an independent adjudication resolving >=2 reviews; explicit version/evidence checks",
              "source_artifacts": [dict(r) for r in store.connection.execute("SELECT * FROM artifacts ORDER BY path")],
              "ingestion_complete": store.ingestion_complete(),
              "counts": store.counts()}
    (root / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
