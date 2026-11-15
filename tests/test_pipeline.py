from dataclasses import replace
from pathlib import Path
import copy
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from legal_recall.cli import main, augment
from legal_recall.config import load_config
from legal_recall.ingest import build_cases, build_laws
from legal_recall.pipeline import Pipeline, AnnotationError
from legal_recall.review import ReviewError, active_queries, export_dataset, export_review, import_reviews, review_template, validate_review
from legal_recall.store import Store
from legal_recall.text import sha


def prepared(fixture_env):
    c = load_config(fixture_env)
    s = Store(c.db_path)
    build_laws(c, s)
    build_cases(c, s)
    return c, s


def annotated(fixture_env, url):
    fixture_env.write_text(fixture_env.read_text() + f"\nRR_TEST_BASE_URL={url}\n")
    c, s = prepared(fixture_env)
    result = Pipeline(c, s).annotate()
    assert result["failed"] == 0 and result["complete"] == 1
    return c, s, list(active_queries(s))[0]


def approved(query, reviewer):
    r = review_template(query)
    r.update({"reviewer_id": reviewer, "decision": "approve", "facts_checked": True,
              "query_checked": True, "family_checked": True, "coverage_checked": True})
    core = {q["unit_id"] for q in r["qrels"] if q["grade"] == 2}
    r["requirement_groups"] = [{"group_id": f"g{i}", "unit_ids": [uid]} for i, uid in enumerate(sorted(core))]
    for v in r["version_confirmations"]:
        v.update({"verified": True, "official_source_url": "https://vbpl.vn/TW/Pages/vbpq-toanvan.aspx?ItemID=1",
                  "version_key": "fixture-original", "version_description": "Synthetic fixture reviewed version", "effective_from": "2017-01-01"})
    return r


def import_items(tmp_path, c, s, items, name="reviews.jsonl", **kwargs):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in items) + "\n")
    return import_reviews(p, c, s, **kwargs)


def test_ingestion_resume_offsets_crosswalk_and_family(fixture_env):
    c, s = prepared(fixture_env)
    try:
        counts = s.counts()
        assert counts["sources"] == 3
        assert counts["cases"] == 2
        source = s.connection.execute("SELECT number FROM sources WHERE id='tvpl:law1'").fetchone()
        assert source[0] == "91/2015/QH13"
        assert len({r[0] for r in s.connection.execute("SELECT family_id FROM cases")}) == 1
        assert {r[0] for r in s.connection.execute("SELECT split FROM cases")} == {"dev"}
        assert s.connection.execute("SELECT count(*) FROM crosswalk").fetchone()[0] == 1
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
        assert s.connection.execute("SELECT sum(processed) FROM artifacts").fetchone()[0] == 4
        assert s.counts()["sources"] == 3
    finally:
        s.close()


def test_changed_source_shard_refused(fixture_env):
    c, s = prepared(fixture_env)
    try:
        p = c.tvpl_path / "documents-00000.parquet"
        pq.write_table(pa.Table.from_pylist([{"id": "new", "vn_text": "Điều 1. Khác"}]), p)
        with pytest.raises(ValueError, match="changed"):
            build_laws(c, s)
    finally:
        s.close()


def test_family_csv_and_ambiguous_bare_identity(fixture_env):
    c, s = prepared(fixture_env)
    try:
        f = fixture_env.parent / "families.csv"
        f.write_text("case_id,family_id\nmissing,family-a\n")
        with pytest.raises(ValueError, match="known"):
            build_cases(replace(c, family_map=f), s)
    finally:
        s.close()


def test_end_to_end_annotation_and_payload_isolation(fixture_env, server):
    with server() as (url, seen):
        c, s, query = annotated(fixture_env, url)
        try:
            assert query["quality"] == "candidate"
            assert query["annotation"]["assessments"]
            assert query["annotation"]["review_required"]
            assert all(a["version_status"] == "unresolved" for a in query["annotation"]["assessments"])
            for req in seen:
                body = req["body"]
                name = body["response_format"]["json_schema"]["name"]
                payload = json.loads(body["messages"][-1]["content"])
                if name == "legal_p2":
                    assert "NHẬN ĐỊNH" not in json.dumps(payload, ensure_ascii=False)
                    assert "466" not in json.dumps(payload)
                    assert "case1" not in json.dumps(payload)
                    assert "citations" not in payload
                if name == "legal_p6":
                    assert "relevance_proposal" not in json.dumps(payload)
            prior_calls = len(seen)
            result = Pipeline(c, s).annotate()
            assert result["attempted"] == 0 and len(seen) == prior_calls
            assert export_dataset(c, s)["gold_queries"] == 0
            export_review(c, s)
            assert (c.output_dir / "review/template.jsonl").is_file()
        finally:
            s.close()


def test_human_gate_two_reviews_and_no_leak_in_model_export(fixture_env, server, tmp_path):
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            one = approved(q, "expert_1")
            two = approved(q, "expert_2")
            import_items(tmp_path, c, s, [one])
            assert export_dataset(c, s)["gold_queries"] == 0
            import_items(tmp_path, c, s, [two])
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
            altered = replace(c, profiles={"TEST": replace(p, model="new-model")})
            assert Pipeline(altered, s).run_hash != q["run_hash"]
        finally:
            s.close()


def test_stale_snapshot_cannot_be_exported(fixture_env, server):
    from legal_recall.review import active_queries
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            with s.transaction():
                s.set_meta("family_revision", "changed")
            with pytest.raises(ReviewError, match="changed"):
                list(active_queries(s))
        finally:
            s.close()


def test_failed_case_queue_and_explicit_retry(fixture_env, server):
    from conftest import fake_output
    fail = [True]
    def handler(body, count):
        name = body["response_format"]["json_schema"]["name"]
        if fail[0] and name == "legal_p2":
            result = fake_output(body)
            result["issues"][0]["query_text"] = "Áp dụng Điều 466 để quyết định."
        else:
            result = fake_output(body)
        return 200, {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
    with server(handler) as (url, seen):
        fixture_env.write_text(fixture_env.read_text() + f"\nRR_TEST_BASE_URL={url}\n")
        c, s = prepared(fixture_env)
        try:
            assert Pipeline(c, s).annotate()["failed"] == 1
            assert s.counts()["queries"] == 0 and s.counts()["failures"] == 1
            assert Pipeline(c, s).annotate()["attempted"] == 0
            fail[0] = False
            assert not any(json.loads(p.read_text())["stage"] == "P2" for p in (c.output_dir / "cache").glob('*.json'))
            assert Pipeline(c, s).annotate(retry_failed=True)["complete"] == 1
        finally:
            s.close()


def test_empty_pool_is_review_required_not_fake_negative(fixture_env, server):
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            pipeline = Pipeline(c, s)
            original = s.search
            s.search = lambda *a, **k: []
            raw = dict(s.connection.execute("SELECT * FROM cases WHERE id='case1'").fetchone())
            raw['metadata'] = json.loads(raw['metadata']);raw['family_flags'] = []
            extraction = pipeline.extract(raw)
            issue = {'issue_id':'i1','issue_text':'Hoàn trả','query_text':'Tìm nguồn về hoàn trả','fact_support_ids':[extraction['facts'][0]['id']],'missing_facts':[],'query_status':'answerable'}
            item = pipeline.annotate_issue(raw,issue,extraction,[],{},False)
            assert item['annotation']['assessments'] == []
            assert item['annotation']['review_required']
            assert item['model_input']['answerability_proposal'] != 'corpus_missing'
            s.search = original
        finally:
            s.close()


def test_long_sources_are_flagged_not_silently_cut(fixture_env, server):
    with server() as (url, _):
        c, s, q = annotated(fixture_env, url)
        try:
            p = Pipeline(replace(c,max_source_windows=1),s)
            unit = s.get_unit(q['annotation']['pool_ids'][0])
            unit['text'] = 'Nội dung.\n' * 20000
            result = p.assess(q,unit)
            assert result['relevance_proposal'] == 'uncertain'
            assert result['flags'] == ['incomplete_source_read']
            assert result['evidence'] == []
        finally:
            s.close()


def test_cli_run_status_and_augment(fixture_env, server):
    with server() as (url, _):
        fixture_env.write_text(fixture_env.read_text() + f"\nRR_TEST_BASE_URL={url}\n")
        assert main(["--env", str(fixture_env), "run"]) == 0
        assert main(["--env", str(fixture_env), "status"]) == 0
        assert main(["--env", str(fixture_env), "augment", "--limit", "1"]) == 0
        c = load_config(fixture_env)
        item = json.loads((c.output_dir / "synthetic_train.jsonl").read_text())
        assert item["split"] == "train" and item["quality"] == "synthetic_candidate"
        assert main(["--env", str(fixture_env), "read", "--id", "missing"]) == 2


def test_optional_vision_proposals_do_not_modify_corpus(fixture_env, server, tmp_path):
    with server() as (url, _):
        fixture_env.write_text(fixture_env.read_text() + f'\nRR_TEST_BASE_URL={url}\nRR_PROFILE_P0V=TEST\nRR_TEST_MODALITIES=["text","image"]\n')
        c, s = prepared(fixture_env)
        try:
            image = tmp_path / "page.png"; image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            manifest = tmp_path / "vision.jsonl"; manifest.write_text(json.dumps({"page_id": "page1", "image_path": str(image)}))
            before = s.snapshot()
            result = Pipeline(c, s).vision(manifest)
            assert result == {"pages": 1, "applied_to_corpus": False}
            assert s.snapshot() == before
        finally:
            s.close()
