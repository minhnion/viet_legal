from dataclasses import replace
import json

import pytest

from legal_recall.config import ConfigError, load_config
from legal_recall.contracts import PromptBank
from legal_recall.text import document_number, header_number, iso_date, legal_units, leakage, normalize, quote_spans, sentences


def test_env_paths_are_relative_to_env(fixture_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path.parent)
    c = load_config(fixture_env)
    assert c.judgment_path == fixture_env.parent / "cases"
    assert c.profile("ISSUE").auth_mode == "none"
    assert c.profile("ISSUE").temperature is None
    assert "api_key" not in c.profile("ISSUE").public()


@pytest.mark.parametrize("line", ["RR_TEST_AUTH_MODE=wrong", "RR_TEST_AUTH_MODE=bearer", "RR_TEST_OUTPUT_MODE=auto", "RR_TEST_TOKEN_PARAMETER=wrong", "RR_TRAIN_RATIO=0.9\nRR_DEV_RATIO=0.9", "RR_TEST_TEMPERATURE=3", "RR_PROFILE_ISSUE=MISSING", "RR_TEST_BASE_URL=http://user:secret@example.test/v1", "RR_TEST_BASE_URL=http://example.test/v1/chat/completions", "RR_TEST_EXTRA_BODY={\"model\":\"override\"}", "RR_TEST_MAX_OUTPUT_TOKENS=999999", "RR_TEST_RETRIES=-1", "RR_TEST_MODALITIES=[\"audio\"]"])
def test_invalid_config_rejected(fixture_env, line):
    fixture_env.write_text(fixture_env.read_text() + "\n" + line + "\n")
    with pytest.raises(ConfigError):
        load_config(fixture_env)


def test_process_rr_environment_override(fixture_env, monkeypatch):
    monkeypatch.setenv("RR_TEST_MODEL", "other-model")
    assert load_config(fixture_env).profile("ISSUE").model == "other-model"


def test_langfuse_optional_and_secret_not_serialized(fixture_env):
    assert load_config(fixture_env).langfuse_host is None
    fixture_env.write_text(fixture_env.read_text() + "\nRR_LANGFUSE_HOST=http://127.0.0.1:3010/\nRR_LANGFUSE_PUBLIC_KEY=pk-lf-test\nRR_LANGFUSE_SECRET_KEY=sk-lf-private\n")
    c = load_config(fixture_env)
    assert c.langfuse_host == "http://127.0.0.1:3010" and c.langfuse_capture_io is False
    assert "sk-lf-private" not in repr(c) and "sk-lf-private" not in json.dumps(c.public())


@pytest.mark.parametrize("line", ["RR_LANGFUSE_HOST=http://127.0.0.1:3010", "RR_LANGFUSE_HOST=ftp://host\nRR_LANGFUSE_PUBLIC_KEY=a\nRR_LANGFUSE_SECRET_KEY=b",
                                  "RR_LANGFUSE_HOST=http://h\nRR_LANGFUSE_PUBLIC_KEY=a\nRR_LANGFUSE_SECRET_KEY=b\nRR_LANGFUSE_CAPTURE_IO=maybe"])
def test_invalid_langfuse_config_rejected(fixture_env, line):
    fixture_env.write_text(fixture_env.read_text() + "\n" + line + "\n")
    with pytest.raises(ConfigError):
        load_config(fixture_env)


def test_profile_auth_secret_not_serialized(fixture_env):
    fixture_env.write_text(fixture_env.read_text() + "\nRR_TEST_AUTH_MODE=bearer\nRR_TEST_API_KEY=private-test-key\n")
    c = load_config(fixture_env)
    assert "private-test-key" not in repr(c)
    assert "private-test-key" not in json.dumps(c.public())


def test_normalization_and_numbers():
    assert normalize("Điều\r\n1\u00a0") == "Điều\n1 "
    assert document_number("Luật số 91 / 2015 / QH13") == "91/2015/QH13"
    assert header_number("Căn cứ Luật số 91/2015/QH13\nSố: 1/2020/NĐ-CP") == "1/2020/NĐ-CP"
    assert header_number("Căn cứ Luật số 91/2015/QH13") is None


@pytest.mark.parametrize("value, expected", [("01/01/2017", "2017-01-01"), ("2017-01-01", "2017-01-01"), ("31/02/2020", None), (None, None), ("bad", None)])
def test_date_validation(value, expected):
    assert iso_date(value) == expected


def test_legal_hierarchy_and_offsets():
    text = "Điều 1. Nội dung\n1. Khoản đầu\na) Điểm thứ nhất\n2. Khoản hai\nĐiều 2. Khác\nNội dung khác."
    units = legal_units(text, "source")
    assert len([u for u in units if u["parent_id"] is None]) == 2
    assert any(u["point"] == "a" and u["clause"] == "1" for u in units)
    assert all(text[u["start"]:u["end"]] == u["text"] for u in units)
    assert legal_units("Theo Điều 1 thì thực hiện nghĩa vụ.", "s") == []


def test_repeated_articles_flagged():
    units = legal_units("Điều 1. Gốc\nNội dung\nĐiều 1. Sửa đổi\nNội dung mới", "s")
    assert all("repeated_article_number" in u["flags"] for u in units)
    assert len({u["id"] for u in units}) == len(units)


def test_empty_article_title_does_not_consume_first_clause():
    text = "Điều 1.\n1. Khoản đầu\n2. Khoản sau"
    units = legal_units(text, "s")
    assert [u["clause"] for u in units if u["parent_id"]] == ["1", "2"]


def test_source_content_versions_have_different_unit_ids():
    old = legal_units("Điều 1. Nội dung cũ", "same-source-id")
    new = legal_units("Điều 1. Nội dung mới", "same-source-id")
    assert old[0]["id"] != new[0]["id"]


def test_sentence_offsets_and_reasoning_exclusion():
    text = "A giao tiền cho B. B chưa trả.\nNHẬN ĐỊNH CỦA TÒA ÁN\nTòa án quyết định chấp nhận yêu cầu."
    rows = sentences(text)
    assert all(text[r["start"]:r["end"]] == r["text"] for r in rows)
    assert rows[-1]["forbidden_section"]
    assert not rows[0]["forbidden_section"]


def test_quote_ambiguity_and_unicode():
    assert quote_spans("Đúng. Đúng.", "Đúng") == [{"start": 0, "end": 4}, {"start": 6, "end": 10}]
    assert quote_spans("Điều 1", "wrong") == []
    assert quote_spans("abc", "") == []


@pytest.mark.parametrize("text", ["Áp dụng Điều 466", "91/2015/QH13", "Tòa án quyết định bác yêu cầu"])
def test_leakage(text):
    assert leakage(text)


def test_factual_location_is_not_a_point_citation():
    assert not leakage("Hai bên gặp nhau tại địa điểm A.")
    assert leakage("Theo điểm a khoản 1 Điều 5")


def test_prompt_bank_and_fewshot_policy(fixture_env, tmp_path):
    c = load_config(fixture_env)
    bank = PromptBank(c.prompts_file)
    assert {"ISSUE", "EVIDENCE", "P0V", "AUGMENT"} <= bank.prompts.keys()
    assert bank.common.startswith("Bạn hỗ trợ annotation")
    f = tmp_path / "fewshot.json"
    f.write_text('{"ISSUE":[{"split":"test","human_reviewed":true,"input":{},"output":{}}]}')
    with pytest.raises(ValueError, match="train/dev"):
        PromptBank(c.prompts_file, f)


@pytest.mark.parametrize("text", ["điều kiện để Tòa án chấp nhận đơn xin ly hôn khi một bên vắng mặt",
                                  "liệu Tòa án chấp nhận yêu cầu chia tài sản trong trường hợp này không",
                                  "Việc xác định tư cách tái phạm khi N đã bị Tòa án xử phạt 28 tháng tù"])
def test_research_questions_are_not_outcome_leaks(text):
    assert not leakage(text)


@pytest.mark.parametrize("text", ["Tòa án chấp nhận yêu cầu khởi kiện của ông A", "Tòa án đã bác toàn bộ kháng cáo", "Tuyên xử: buộc B trả tiền"])
def test_court_rulings_are_outcome_leaks(text):
    assert leakage(text)


def test_stage_output_caps_config(fixture_env):
    assert load_config(fixture_env).stage_max_output_tokens == {}
    fixture_env.write_text(fixture_env.read_text() + '\nRR_STAGE_MAX_OUTPUT_TOKENS={"ISSUE": 2048, "EVIDENCE": 1536}\n')
    assert load_config(fixture_env).stage_max_output_tokens == {"ISSUE": 2048, "EVIDENCE": 1536}


@pytest.mark.parametrize("line", ['RR_STAGE_MAX_OUTPUT_TOKENS={"P4": 1536}', 'RR_STAGE_MAX_OUTPUT_TOKENS={"EVIDENCE": 10}', "RR_STAGE_MAX_OUTPUT_TOKENS=[1]"])
def test_invalid_stage_output_caps_rejected(fixture_env, line):
    fixture_env.write_text(fixture_env.read_text() + "\n" + line + "\n")
    with pytest.raises(ConfigError):
        load_config(fixture_env)
