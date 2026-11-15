from dataclasses import replace
import json

import pytest
import requests

from legal_recall.client import BudgetError, ChatClient, ModelError, image_part
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


def test_context_budget_no_silent_truncation(tmp_path):
    c = client(profile("http://127.0.0.1:9/v1", max_input_chars=10), tmp_path)
    with pytest.raises(BudgetError):
        c.complete("P1", MESSAGES, SCHEMA, {})


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
