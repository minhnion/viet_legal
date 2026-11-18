from dataclasses import replace
import copy
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from conftest import CASE, fake_output
from legal_recall.cli import main
from legal_recall.client import ModelError
from legal_recall.config import load_config
from legal_recall.ingest import build_cases, build_laws
from legal_recall.pipeline import Pipeline, body_start
from legal_recall.review import ReviewError, active_queries, export_dataset, export_review, import_reviews, review_template, validate_review
from legal_recall.store import Store


def prepared(fixture_env):
    c = load_config(fixture_env)
    s = Store(c.db_path)
    build_laws(c, s)
    build_cases(c, s)
    return c, s


def use(fixture_env, url, extra=""):
    fixture_env.write_text(fixture_env.read_text() + f"\nRR_TEST_BASE_URL={url}\n{extra}\n")


def annotated(fixture_env, url):
    use(fixture_env, url)
    c, s = prepared(fixture_env)
    result = Pipeline(c, s).annotate()
    assert result["failed"] == 0 and result["complete"] == 1 and result["skipped:no_facts_section"] == 1
    return c, s, list(active_queries(s))[0]


def reply(content, finish="stop"):
    return 200, {"choices": [{"finish_reason": finish, "message": {"content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)}}]}


def stage(body):
    return body["response_format"]["json_schema"]["name"]


def unit_id(s, source, article):
    return s.connection.execute("SELECT id FROM units WHERE source_id=? AND article=? AND parent_id IS NULL", (source, article)).fetchone()[0]


def approved(query, reviewer):
    r = review_template(query)
    r.update({"reviewer_id": reviewer, "decision": "approve", "facts_checked": True,
              "query_checked": True, "family_checked": True, "coverage_checked": True})
    for v in r["version_confirmations"]:
        v.update({"verified": True, "official_source_url": "https://vbpl.vn/TW/Pages/vbpq-toanvan.aspx?ItemID=1",
                  "version_key": "fixture-original", "version_description": "Synthetic fixture reviewed version", "effective_from": "2017-01-01"})
    return r


def import_items(tmp_path, c, s, items, name="reviews.jsonl", **kwargs):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in items) + "\n")
    return import_reviews(p, c, s, **kwargs)


def test_ingestion_resume_offsets_crosswalk_family_and_instrument_index(fixture_env):
    c, s = prepared(fixture_env)
    try:
        counts = s.counts()
        assert counts["sources"] == 7 and counts["cases"] == 2
        assert s.connection.execute("SELECT number FROM sources WHERE id='tvpl:law1'").fetchone()[0] == "91/2015/QH13"
        assert {r[0] for r in s.connection.execute("SELECT split FROM cases")} == {"dev"}
        assert s.connection.execute("SELECT count(*) FROM crosswalk").fetchone()[0] == 1
        acts = {r["id"]: dict(r) for r in s.connection.execute("SELECT * FROM acts")}
        assert set(acts) == {"tvpl:law1", "tvpl:law2005", "tvpl:tt", "tvpl:nq326"}
        assert acts["tvpl:law2005"]["name_key"] == "dansu" and acts["tvpl:law2005"]["expiry_date"] == "2017-01-01"
        assert acts["tvpl:nq326"]["number_key"].startswith("326/2016/ubtvqh14")
        for row in s.connection.execute("SELECT * FROM units"):
            text = s.connection.execute("SELECT text FROM sources WHERE id=?", (row["source_id"],)).fetchone()[0]
            assert row["text"] == text[row["start"]:row["end"]]
        before = s.snapshot()
        build_laws(c, s); build_cases(c, s)
        assert s.counts() == counts and s.snapshot() == before
        assert s.search('tiền " OR DROP TABLE sources; --', 2)
        assert s.search("!!!", 2) == []
    finally:
        s.close()


def test_resume_after_partial_input_limit(fixture_env):
    c = load_config(fixture_env)
    s = Store(c.db_path)
    try:
        build_laws(replace(c, law_max_records=1), s)
        assert s.connection.execute("SELECT sum(processed) FROM artifacts").fetchone()[0] == 2
        build_laws(c, s)
        assert s.connection.execute("SELECT sum(processed) FROM artifacts").fetchone()[0] == 8
        assert s.counts()["sources"] == 7
    finally:
        s.close()


def test_changed_source_shard_refused(fixture_env):
    c, s = prepared(fixture_env)
    try:
        pq.write_table(pa.Table.from_pylist([{"id": "new", "vn_text": "Điều 1. Khác"}]), c.tvpl_path / "documents-00000.parquet")
        with pytest.raises(ValueError, match="changed"):
            build_laws(c, s)
    finally:
        s.close()


def test_family_csv_must_reference_known_cases(fixture_env):
    c, s = prepared(fixture_env)
    try:
        f = fixture_env.parent / "families.csv"
        f.write_text("case_id,family_id\nmissing,family-a\n")
        with pytest.raises(ValueError, match="known"):
            build_cases(replace(c, family_map=f), s)
    finally:
        s.close()


def test_court_anchored_query_targets_versions_copies_and_evidence(fixture_env, server):
    with server() as (url, seen):
        c, s, q = annotated(fixture_env, url)
        try:
            assert q["quality"] == "silver" and q["review_status"] == "unreviewed" and q["query_origin"] == "court_citation_anchored"
            assert [f["text"] for f in q["model_input"]["facts"]][-1] == "Bà T khởi kiện yêu cầu bà S trả số tiền vay."
            assert {"event": "judgment_date", "date": "2019-05-15"} in q["model_input"]["timeline"]
            assert any(t["date"] == "2014-08-24" for t in q["model_input"]["timeline"])
            # Substantive sources only: BLDS 2005 (reasoning) and BLDS 2015 (decision); procedure and court fees excluded.
            sources = {(a["instrument_number"], a["article"]) for a in q["annotation"]["sources"]}
            assert sources == {("33/2005/QH11", "471"), ("33/2005/QH11", "474"), ("91/2015/QH13", "463"), ("91/2015/QH13", "466")}
            roles = {(r["raw"], r["role"]) for r in q["annotation"]["case_citations"]}
            assert ("khoản 3 Điều 26", "procedural") in roles and any(role == "court_fee" for _, role in roles)
            # Same article in two versions is one requirement group; copies (Pháp điển, translated TVPL page) are alternatives.
            u466, u474 = unit_id(s, "tvpl:law1", "466"), unit_id(s, "tvpl:law2005", "474")
            group = next(g for g in q["target"]["requirement_groups"] if u466 in g["unit_ids"])
            assert group["cross_version"] and set(group["canonical_unit_ids"]) == {u466, u474}
            copies = {u for u in group["unit_ids"] if u.startswith(("phapdien:", "tvpl:law1en"))}
            assert len(copies) == 2 and len(q["target"]["requirement_groups"]) == 2
            qrels = {r["unit_id"]: r for r in q["target"]["qrels"]}
            assert all(r["grade"] == 2 and r["label_source"] == "court_applied" for r in qrels.values())
            assert qrels[u466]["confidence"] == "high"
            # Evidence offsets point into unit.text, and the Pháp điển copy gets the same clause despite line breaks.
            text = s.get_unit(u466)["text"]
            span = qrels[u466]["evidence"][0]
            assert text[span["start"]:span["end"]].startswith("1. Bên vay tài sản là tiền") and text[span["end"] - 1] == "."
            u463 = unit_id(s, "tvpl:law1", "463")
            body = qrels[u463]["evidence"][0]
            assert s.get_unit(u463)["text"][body["start"]:body["end"]].startswith("Hợp đồng vay tài sản là sự thỏa thuận")
            pd = next(u for u in copies if u.startswith("phapdien:"))
            pd_span = qrels[pd]["evidence"][0]
            assert s.get_unit(pd)["text"][pd_span["start"]:pd_span["end"]] == "1. Bên vay tài sản là tiền thì phải trả đủ tiền khi đến hạn."
            # Payload isolation: no judgment identity anywhere; EVIDENCE never sees the judgment; questions carry no law.
            for request in seen:
                payload = request["body"]["messages"][-1]["content"]
                assert "case1" not in payload and "family:" not in payload
                if stage(request["body"]) == "legal_evidence":
                    assert "NHẬN ĐỊNH" not in payload and "Chấp nhận yêu cầu" not in payload
            assert sum(stage(r["body"]) == "legal_issue" for r in seen) == 1
            prior = len(seen)
            result = Pipeline(c, s).annotate()
            assert result["previously_done"] == 2 and len(seen) == prior
            report = export_dataset(c, s)
            assert report["silver_queries"] == 1 and report["gold_queries"] == 0
            assert report["funnel"]["cases"] == {"complete": 1, "skipped:no_facts_section": 1}
            exported = json.loads((c.output_dir / "export/silver_queries.jsonl").read_text())
            assert set(exported) == {"query_id", "split", "facts", "issue", "question", "timeline", "corpus_snapshot_id"}
            assert exported["facts"] == [" ".join(f["text"] for f in q["model_input"]["facts"])]  # consecutive lines rejoined
            assert "Điều" not in json.dumps(exported, ensure_ascii=False)
            qrel_row = json.loads((c.output_dir / "export/silver_qrels.jsonl").read_text())
            assert {g["group_id"] for g in qrel_row["requirement_groups"]} == {"g1", "g2"}
            export_review(c, s)
            bundle = json.loads((c.output_dir / "review/independent_bundles.jsonl").read_text())
            assert "sources" not in bundle and "target" not in json.dumps(bundle)
        finally:
            s.close()


def test_query_ids_survive_a_new_run(fixture_env, server):
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            other = Pipeline(replace(c, max_issues=3), s)
            assert other.run_hash != q["run_hash"]
            assert other.annotate()["complete"] == 1
            again = list(active_queries(s))
            assert [x["id"] for x in again] == [q["id"]] and again[0]["run_hash"] == other.run_hash
        finally:
            s.close()


def test_human_gate_two_reviews_and_no_leak_in_model_export(fixture_env, server, tmp_path):
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            import_items(tmp_path, c, s, [approved(q, "expert_1")])
            assert export_dataset(c, s)["gold_queries"] == 0
            import_items(tmp_path, c, s, [approved(q, "expert_2")])
            assert export_dataset(c, s, True)["gold_queries"] == 1
            exported = json.loads((c.output_dir / "export/gold_queries.jsonl").read_text())
            assert "private_provenance" not in exported and "case_id" not in exported and "family_id" not in exported
            assert exported["quality"] == "gold_human_reviewed"
            assert (c.output_dir / "export/corpus.jsonl").is_file()
        finally:
            s.close()


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(base_hash="stale"),
    lambda r: r.update(decision="unreviewed"),
    lambda r: r.update(family_checked=False),
    lambda r: r.update(query_text="Áp dụng Điều 466"),
    lambda r: r.update(facts_sentence_ids=["not_found"]),
    lambda r: r.update(answerability="corpus_missing"),
    lambda r: r["qrels"][0].update(grade=True),
    lambda r: r["qrels"][0]["evidence"][0].update(end=999999),
    lambda r: r["version_confirmations"][0].update(verified=False),
    lambda r: r["version_confirmations"][0].update(official_source_url="https://vbpl.vn.evil.test/fake"),
    lambda r: r["version_confirmations"][0].update(effective_from="bad"),
    lambda r: r["version_confirmations"][0].update(effective_from="2020-01-01", effective_until="2017-01-01"),
    lambda r: r.update(requirement_groups=[]),
])
def test_bad_reviews_rejected(fixture_env, server, mutation):
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            r = approved(q, "expert")
            validate_review(copy.deepcopy(r), q, s, c)
            mutation(r)
            with pytest.raises(ReviewError):
                validate_review(r, q, s, c)
        finally:
            s.close()


def test_conflicting_reviews_need_independent_adjudication(fixture_env, server, tmp_path):
    from legal_recall.config import digest
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            one, two = approved(q, "expert_1"), approved(q, "expert_2")
            two["query_text"] += " Xin xác định nguồn."
            import_items(tmp_path, c, s, [one, two])
            assert export_dataset(c, s)["gold_queries"] == 0
            third = approved(q, "senior_3")
            third.update(decision="adjudicate", resolves_review_hashes=[digest(one), digest(two)])
            import_items(tmp_path, c, s, [third], name="adjudication.jsonl")
            assert export_dataset(c, s)["gold_queries"] == 1
        finally:
            s.close()


def test_import_is_atomic_and_replace_explicit(fixture_env, server, tmp_path):
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            r = approved(q, "reviewer")
            import_items(tmp_path, c, s, [r])
            modified = copy.deepcopy(r); modified["notes"] = "correction"
            with pytest.raises(ReviewError, match="replace"):
                import_items(tmp_path, c, s, [approved(q, "another"), modified])
            assert s.counts()["reviews"] == 1
            import_items(tmp_path, c, s, [modified], replace=True)
            assert s.counts()["reviews"] == 1
        finally:
            s.close()


def test_profile_change_invalidates_annotation_run(fixture_env, server):
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            p = c.profiles["TEST"]
            assert Pipeline(replace(c, profiles={"TEST": replace(p, model="new-model")}), s).run_hash != q["run_hash"]
        finally:
            s.close()


def test_stale_snapshot_cannot_be_exported(fixture_env, server):
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            with s.transaction():
                s.set_meta("family_revision", "changed")
            with pytest.raises(ReviewError, match="changed"):
                list(active_queries(s))
        finally:
            s.close()


def leaking_issue(body, leak):
    result = fake_output(body)
    if stage(body) == "legal_issue" and leak(json.loads(body["messages"][-1]["content"])):
        result["issues"][0]["query_text"] = "Theo Bộ luật dân sự thì bên vay có phải trả nợ không?"
    return result


def test_instrument_name_in_question_is_repaired_once(fixture_env, server):
    with server(lambda body, n: reply(leaking_issue(body, lambda p: "revision_notes" not in p))) as (url, seen):
        use(fixture_env, url)
        c, s = prepared(fixture_env)
        try:
            assert Pipeline(c, s).annotate()["complete"] == 1
            q = list(active_queries(s))[0]
            assert "issue_repair_attempted" in q["annotation"]["review_reasons"]
            assert sum(stage(r["body"]) == "legal_issue" for r in seen) == 2
            # The rejected first answer was not cached: the leak check runs inside the client's semantic validator.
            assert not any("Theo Bộ luật dân sự" in p.read_text() for p in (c.output_dir / "cache").glob("*.json"))
        finally:
            s.close()


def test_only_the_leaking_issue_is_dropped(fixture_env, server):
    def handler(body, n):
        result = fake_output(body)
        if stage(body) == "legal_issue":
            bad = dict(result["issues"][0], issue_id="i2", query_text="Áp dụng Điều 466 để quyết định.")
            result["issues"].append(bad)
        return reply(result)
    with server(handler) as (url, seen):
        use(fixture_env, url)
        c, s = prepared(fixture_env)
        try:
            assert Pipeline(c, s).annotate()["complete"] == 1 and s.counts()["queries"] == 1
            q = list(active_queries(s))[0]
            assert "issue_dropped:i2:leakage" in q["annotation"]["review_reasons"]
            assert sum(stage(r["body"]) == "legal_issue" for r in seen) == 1
        finally:
            s.close()


def test_unrepairable_leak_fails_case_and_retry_is_explicit(fixture_env, server):
    leak = [True]
    with server(lambda body, n: reply(leaking_issue(body, lambda p: leak[0]))) as (url, seen):
        use(fixture_env, url)
        c, s = prepared(fixture_env)
        try:
            result = Pipeline(c, s).annotate()
            assert result["failed"] == 1 and result["failed:no_valid_issue"] == 1 and s.counts()["queries"] == 0
            export_dataset(c, s)
            failure = json.loads((c.output_dir / "export/failures.jsonl").read_text())
            assert failure["case_id"] == "case1" and failure["error_code"] == "no_valid_issue" and "leakage" in failure["message"]
            calls = len(seen)
            assert Pipeline(c, s).annotate()["previously_done"] == 2 and len(seen) == calls
            leak[0] = False
            assert Pipeline(c, s).annotate(retry_failed=True)["complete"] == 1
        finally:
            s.close()


def test_truncated_evidence_falls_back_with_low_confidence(fixture_env, server):
    def handler(body, n):
        if stage(body) == "legal_evidence":
            return reply('{"assessments": [', finish="length")
        return reply(fake_output(body))
    with server(handler) as (url, _):
        use(fixture_env, url)
        c, s = prepared(fixture_env)
        try:
            assert Pipeline(c, s).annotate()["complete"] == 1
            q = list(active_queries(s))[0]
            assert q["confidence"] == "low" and q["quality"] == "silver"
            assert all("evidence_model_error:incomplete" in a["flags"] and a["grade"] == 2 for a in q["annotation"]["sources"])
            assert all(r["evidence"] for r in q["target"]["qrels"])
        finally:
            s.close()


def test_model_rejection_removes_a_court_citation_and_is_recorded(fixture_env, server):
    def handler(body, n):
        result = fake_output(body)
        if stage(body) == "legal_evidence":
            for a in result["assessments"]:
                payload = json.loads(body["messages"][-1]["content"])
                title = next(src["article_title"] for src in payload["sources"] if src["id"] == a["source_id"])
                if title == "Hợp đồng vay tài sản":
                    a.update(relevance="not_relevant", clause_ids=[])
        return reply(result)
    with server(handler) as (url, _):
        use(fixture_env, url)
        c, s = prepared(fixture_env)
        try:
            Pipeline(c, s).annotate()
            q = list(active_queries(s))[0]
            assert len(q["target"]["requirement_groups"]) == 1 and q["quality"] == "silver"
            assert sum(r.startswith("court_citation_rejected_by_model:") for r in q["annotation"]["review_reasons"]) == 2
        finally:
            s.close()


def test_infrastructure_errors_stop_the_run_without_failing_cases(fixture_env, server):
    down = [True]
    with server(lambda body, n: (503, {"error": "down"}) if down[0] else reply(fake_output(body))) as (url, _):
        use(fixture_env, url)
        c, s = prepared(fixture_env)
        try:
            with pytest.raises(ModelError) as error:
                Pipeline(c, s).annotate()
            assert error.value.code == "http_retryable"
            assert s.connection.execute("SELECT count(*) FROM annotations WHERE status='failed'").fetchone()[0] == 0
            down[0] = False
            assert Pipeline(c, s).annotate()["complete"] == 1
        finally:
            s.close()


def test_issue_over_context_budget_fails_case_not_run(fixture_env, server):
    with server(tokenize=lambda body: (200, {"count": 10 ** 6})) as (url, seen):
        use(fixture_env, url)
        c, s = prepared(fixture_env)
        try:
            result = Pipeline(c, s).annotate()
            assert result["failed:context_budget"] == 1 and len(seen) == 0
        finally:
            s.close()


def test_cases_run_in_parallel_and_unresolved_citations_cap_confidence(fixture_env, server):
    variant = CASE.replace("Chấp nhận yêu cầu khởi kiện", "Buộc bồi thường theo Điều 584 Bộ luật D n sự năm 2015. Chấp nhận yêu cầu khởi kiện")
    rows = [{"doc_name": f"case{i}", "markdown": text.replace("bà T", f"bà T{i}"), "category": "Civil", "issued_date": "2019-05-15",
             "official_document_id": f"{i}/2019/DS-ST", "citations_law": [], "citations": [], "citations_case": []}
            for i, text in enumerate([CASE, CASE, variant, CASE], 1)]
    pq.write_table(pa.Table.from_pylist(rows), fixture_env.parent / "cases/documents-00000.parquet")
    with server() as (url, seen):
        use(fixture_env, url, "RR_TEST_CONCURRENCY=3")
        c, s = prepared(fixture_env)
        try:
            result = Pipeline(c, s).annotate()
            assert result["complete"] == 4 and result["silver_queries"] == 4
            queries = {q["case_id"]: q for q in active_queries(s)}
            assert queries["case3"]["confidence"] == "medium" and queries["case3"]["annotation"]["unresolved_substantive_citations"]
            assert queries["case1"]["confidence"] == "high"
            assert len({q["id"] for q in queries.values()}) == 4
        finally:
            s.close()


def test_cli_run_status_audit_evaluate_and_augment(fixture_env, server, tmp_path):
    with server() as (url, _):
        use(fixture_env, url)
        assert main(["--env", str(fixture_env), "run"]) == 0
        assert main(["--env", str(fixture_env), "status"]) == 0
        assert main(["--env", str(fixture_env), "audit", "--limit", "5"]) == 0
        c = load_config(fixture_env)
        audit = json.loads((c.output_dir / "audit.json").read_text())
        assert audit["cases"] == {"cases": 2, "eligible": 1, "skipped:no_facts_section": 1}
        qrels = json.loads((c.output_dir / "export/silver_qrels.jsonl").read_text())
        ranking = [g["unit_ids"][-1] for g in qrels["requirement_groups"]]
        predictions = tmp_path / "pred.jsonl"
        predictions.write_text(json.dumps({"query_id": qrels["query_id"], "ranking": ranking}) + "\n")
        assert main(["--env", str(fixture_env), "evaluate", "--predictions", str(predictions), "--labels", "silver"]) == 0
        assert main(["--env", str(fixture_env), "augment", "--limit", "1"]) == 0
        item = json.loads((c.output_dir / "synthetic_train.jsonl").read_text())
        assert item["split"] == "train" and item["quality"] == "synthetic_candidate"
        assert main(["--env", str(fixture_env), "read", "--id", "missing"]) == 2


def test_optional_vision_proposals_do_not_modify_corpus(fixture_env, server, tmp_path):
    with server() as (url, _):
        use(fixture_env, url, 'RR_PROFILE_P0V=TEST\nRR_TEST_MODALITIES=["text","image"]')
        c, s = prepared(fixture_env)
        try:
            image = tmp_path / "page.png"; image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            manifest = tmp_path / "vision.jsonl"; manifest.write_text(json.dumps({"page_id": "page1", "image_path": str(image)}))
            before = s.snapshot()
            assert Pipeline(c, s).vision(manifest) == {"pages": 1, "applied_to_corpus": False}
            assert s.snapshot() == before
        finally:
            s.close()


@pytest.mark.parametrize("text,body", [
    ("Điều\n463. Hợp đồng vay tài sản\nHợp đồng vay là sự thỏa thuận.", "Hợp đồng vay là sự thỏa thuận."),
    ("Điều 194.\nTội tàng trữ trái phép chất ma túy\n1. Người nào tàng trữ.", "1. Người nào tàng trữ."),
    ("Điều 5. Hiệu lực\n1. Có hiệu lực.", "1. Có hiệu lực."),
])
def test_article_body_skips_heading_and_title_line(text, body):
    assert text[body_start({"text": text}):] == body


def test_question_repeating_an_article_title_is_flagged(fixture_env, server):
    def handler(body, n):
        result = fake_output(body)
        if stage(body) == "legal_issue":
            result["issues"][0]["query_text"] = "Quy định nào về nghĩa vụ trả nợ của bên vay áp dụng cho bà S?"
        return reply(result)
    with server(handler) as (url, _):
        use(fixture_env, url)
        c, s = prepared(fixture_env)
        try:
            Pipeline(c, s).annotate()
            reasons = list(active_queries(s))[0]["annotation"]["review_reasons"]
            assert sum(r.startswith("question_repeats_article_title:") for r in reasons) == 2
        finally:
            s.close()


def test_fact_lines_are_rejoined_into_passages():
    from legal_recall.review import fact_passages
    facts = [{"sentence_id": "s3", "text": "nợ cho doanh nghiệp"}, {"sentence_id": "s4", "text": "96.899.000 đồng."}, {"sentence_id": "s9", "text": "Bị đơn vắng mặt."}]
    assert fact_passages(facts) == ["nợ cho doanh nghiệp 96.899.000 đồng.", "Bị đơn vắng mặt."]
