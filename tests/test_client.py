from dataclasses import replace
import json

import pytest
import requests

from legal_recall.client import BudgetError, ChatClient, LangfuseTracer, ModelError, image_part
from legal_recall.config import Profile


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
MESSAGES = [{"role": "user", "content": 'Return JSON {"ok": true}.'}]


def profile(url, **kwargs):
    return Profile("TEST", url, "model-X", "", auth_mode="none", retry_backoff=0, **kwargs)


def client(p, tmp_path):
    return ChatClient(p, tmp_path / "cache", tmp_path / "calls.jsonl")


def ok_response(value=None, finish="stop", refusal=None):
    return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(value if value is not None else {"ok": True}), "refusal": refusal}}]}


@pytest.mark.parametrize("mode", ["json_schema", "json_object", "prompt_json"])
def test_modes_cache_and_no_auth(server, tmp_path, mode):
    with server(lambda body, count: (200, ok_response())) as (url, seen):
        c = client(profile(url, output_mode=mode), tmp_path)
        assert c.complete("P1", MESSAGES, SCHEMA, {}) == {"ok": True}
        assert c.complete("P1", MESSAGES, SCHEMA, {}) == {"ok": True}
        assert len(seen) == 1
        assert seen[0]["authorization"] is None
        assert seen[0]["path"] == "/v1/chat/completions"
        assert ("response_format" in seen[0]["body"]) == (mode != "prompt_json")


def test_bearer_and_provider_parameter_mapping(server, tmp_path):
    with server(lambda body, count: (200, ok_response())) as (url, seen):
        p = replace(profile(url), api_key="test-secret", auth_mode="bearer", token_parameter="max_completion_tokens", temperature=None)
        c = client(p, tmp_path)
        c.complete("P2", MESSAGES, SCHEMA, {})
        assert seen[0]["authorization"] == "Bearer test-secret"
        assert "max_completion_tokens" in seen[0]["body"] and "max_tokens" not in seen[0]["body"]
        assert "temperature" not in seen[0]["body"]
        assert "chat_template_kwargs" not in seen[0]["body"]
        assert "test-secret" not in (tmp_path / "calls.jsonl").read_text()
        assert all("test-secret" not in p.read_text() for p in (tmp_path / "cache").glob('*.json'))


@pytest.mark.parametrize("status,retry", [(429, True), (500, True), (401, False), (400, False)])
def test_http_retry_policy(server, tmp_path, status, retry):
    with server(lambda body, count: (status, {"error": "sensitive provider detail"}) if count == 1 else (200, ok_response())) as (url, seen):
        c = client(profile(url, retries=1), tmp_path)
        if retry:
            assert c.complete("P1", MESSAGES, SCHEMA, {}) == {"ok": True}
            assert len(seen) == 2
        else:
            with pytest.raises(ModelError, match=f"HTTP {status}"):
                c.complete("P1", MESSAGES, SCHEMA, {})
            assert len(seen) == 1
        assert "sensitive provider detail" not in (tmp_path / "calls.jsonl").read_text()


@pytest.mark.parametrize("response,code", [(ok_response(finish="length"), "incomplete"), (ok_response(refusal="No"), "refusal"), ({}, "protocol"), (ok_response({"ok": "wrong"}), "schema"), ("not-json", "protocol")])
def test_bad_responses_never_cache(server, tmp_path, response, code):
    with server(lambda body, count: (200, response)) as (url, seen):
        c = client(profile(url, retries=0), tmp_path)
        with pytest.raises(ModelError) as err:
            c.complete("P1", MESSAGES, SCHEMA, {})
        assert err.value.code == code
        assert not list((tmp_path / "cache").glob('*.json'))


def test_context_budget_no_silent_truncation(server, tmp_path):
    with server(tokenize=lambda body: (200, {"count": 99999})) as (url, seen):
        c = client(profile(url), tmp_path)
        with pytest.raises(BudgetError):
            c.complete("P1", MESSAGES, SCHEMA, {})
        assert len(seen) == 0 and len(seen.tokenize) == 1


def test_token_budget_uses_tokenize_endpoint_with_chat_template(server, tmp_path):
    with server(tokenize=lambda body: (200, {"count": 100})) as (url, seen):
        p = replace(profile(url, context_tokens=4096, max_output_tokens=2048, token_margin=512),
                    api_key="test-secret", auth_mode="bearer", extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        c = client(p, tmp_path)
        assert c.fits(MESSAGES, SCHEMA) and c.fits(MESSAGES, SCHEMA)
        assert len(seen.tokenize) == 1  # identical messages are counted once
        request = seen.tokenize[0]
        assert request["path"] == "/tokenize" and request["authorization"] == "Bearer test-secret"
        assert request["body"] == {"model": "model-X", "messages": MESSAGES, "add_generation_prompt": True,
                                   "chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("count,expected", [(1535, True), (1537, False)])
def test_token_budget_reserves_output_and_margin(server, tmp_path, count, expected):
    with server(tokenize=lambda body: (200, {"count": count})) as (url, seen):
        c = client(profile(url, context_tokens=4096, max_output_tokens=2048, token_margin=512), tmp_path)
        assert c.fits(MESSAGES, SCHEMA) is expected


@pytest.mark.parametrize("response", [(404, {"detail": "Not Found"}), (200, {"tokens": []}), (200, {"count": -1})])
def test_tokenize_failure_is_reported_not_guessed(server, tmp_path, response):
    with server(tokenize=lambda body: response) as (url, seen):
        c = client(profile(url, retries=0), tmp_path)
        with pytest.raises(ModelError) as e:
            c.complete("P1", MESSAGES, SCHEMA, {})
        assert e.value.code == "tokenize" and len(seen) == 0


def test_transport_error_is_redacted(tmp_path, monkeypatch):
    def fail(*a, **k):
        raise requests.ConnectionError("Authorization: test-secret")
    monkeypatch.setattr(requests, "post", fail)
    c = client(replace(profile("http://localhost/v1", retries=0), api_key="test-secret"), tmp_path)
    with pytest.raises(ModelError) as e:
        c.complete("P1", MESSAGES, SCHEMA, {})
    assert "test-secret" not in str(e.value)


def test_images_require_real_signature_and_enabled_profile(tmp_path):
    p = tmp_path / "image.png"
    p.write_bytes(b"%PDF-fake")
    with pytest.raises(ValueError, match="PDF"):
        image_part(p)
    c = client(profile("http://localhost/v1"), tmp_path)
    with pytest.raises(ModelError) as e:
        c.complete("P0V", MESSAGES, SCHEMA, {}, image=True)
    assert e.value.code == "unsupported_modality"


def otel_spans(seen):
    return [s for r in seen for rs in r["body"]["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]


def span_attrs(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


@pytest.mark.parametrize("capture", [False, True])
def test_langfuse_spans_for_success_error_and_cache(server, tmp_path, capture):
    with server(lambda body, count: (200, {})) as (lf_url, lf_seen), \
         server(lambda body, count: (500, {}) if count == 1 else (200, {**ok_response(), "usage": {"prompt_tokens": 7, "completion_tokens": 3}})) as (url, seen):
        tracer = LangfuseTracer(lf_url.removesuffix("/v1"), "pk-lf-test", "sk-lf-test", capture_io=capture, interval=60)
        c = ChatClient(profile(url, retries=1), tmp_path / "cache", tmp_path / "calls.jsonl", tracer)
        trace = {"trace_key": "run:case-1", "trace_name": "case case-1", "case_id": "case-1", "item_id": "case-1:chunk:0", "session_id": "run"}
        assert c.complete("P1", MESSAGES, SCHEMA, {}, trace=trace) == {"ok": True}
        assert c.complete("P1", MESSAGES, SCHEMA, {}, trace=trace) == {"ok": True}
        tracer.flush()
        assert lf_seen[0]["path"] == "/api/public/otel/v1/traces"
        assert lf_seen[0]["authorization"].startswith("Basic ")
        spans = otel_spans(lf_seen)
        error, success, cached = (span_attrs(s) for s in spans)
        assert len({s["traceId"] for s in spans}) == 1 and len(spans[0]["traceId"]) == 32
        assert error["langfuse.observation.level"] == "ERROR" and error["langfuse.observation.status_message"] == "http_retryable"
        assert success["gen_ai.usage.input_tokens"] == "7" and success["gen_ai.usage.output_tokens"] == "3"
        assert success["session.id"] == "run" and success["langfuse.observation.metadata.case_id"] == "case-1"
        assert cached["langfuse.observation.metadata.cache_hit"] is True
        assert ("langfuse.observation.input" in success) == capture and ("langfuse.observation.output" in success) == capture


def test_langfuse_export_failure_does_not_break_completion(server, tmp_path):
    with server(lambda body, count: (200, ok_response())) as (url, seen):
        tracer = LangfuseTracer("http://127.0.0.1:9", "pk", "sk", interval=60, timeout=1)
        c = ChatClient(profile(url), tmp_path / "cache", tmp_path / "calls.jsonl", tracer)
        assert c.complete("P1", MESSAGES, SCHEMA, {}) == {"ok": True}
        tracer.flush()
        assert tracer.dropped == 1


def test_cache_ignores_run_context_and_stage_cap_sets_max_tokens(server, tmp_path):
    with server(lambda body, count: (200, ok_response())) as (url, seen):
        c = client(profile(url), tmp_path)
        assert c.complete("P1", MESSAGES, SCHEMA, {"run_hash": "a", "item_id": "x"}) == {"ok": True}
        assert c.complete("P1", MESSAGES, SCHEMA, {"run_hash": "b", "item_id": "y"}) == {"ok": True}
        assert len(seen) == 1  # same request → cached across runs
        c.complete("P1", MESSAGES, SCHEMA, {}, max_output_tokens=777)
        assert len(seen) == 2 and seen[1]["body"]["max_tokens"] == 777  # different cap → different request


def test_invalid_model_output_is_saved_for_debugging(server, tmp_path):
    with server(lambda body, count: (200, ok_response({"ok": "not-a-bool"}))) as (url, seen):
        c = client(profile(url, retries=0), tmp_path)
        with pytest.raises(ModelError) as e:
            c.complete("P4", MESSAGES, SCHEMA, {})
        assert e.value.code == "schema"
        saved = list((tmp_path / "invalid_outputs").glob("*.schema.txt"))
        assert len(saved) == 1 and "not-a-bool" in saved[0].read_text() and saved[0].read_text().startswith("stage=P4")
