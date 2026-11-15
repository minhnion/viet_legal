from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


LAW = "QUỐC HỘI\nSố: 91/2015/QH13\nBỘ LUẬT DÂN SỰ\nĐiều 463. Hợp đồng vay tài sản\n1. Bên vay nhận tiền và phải hoàn trả khi đến hạn.\na) Thực hiện nghĩa vụ theo thỏa thuận.\nĐiều 466. Nghĩa vụ trả nợ\n1. Bên vay phải trả đủ tiền khi đến hạn.\n2. Việc trả tiền được xác định theo thỏa thuận."
CASE = "NỘI DUNG VỤ ÁN\nNgày 01/06/2019, A giao cho B một khoản tiền theo giấy vay sáu tháng.\nB chưa hoàn trả khi đến hạn.\nA yêu cầu B hoàn trả khoản tiền đã vay.\nNHẬN ĐỊNH CỦA TÒA ÁN\nÁp dụng Điều 466 Bộ luật dân sự.\nQUYẾT ĐỊNH:\nTòa án quyết định chấp nhận yêu cầu của A."


def fake_output(body):
    s = body.get("response_format", {}).get("json_schema", {}).get("schema")
    if s is None:
        # Fallback mode still carries the schema in the instruction for portability.
        instruction = body["messages"][0]["content"]
        s = json.loads(instruction.split("JSON Schema sau:\n", 1)[1])
    props = s["properties"]
    if set(props) == {"ok"}:
        return {"ok": True}
    user = body["messages"][-1]["content"]
    if isinstance(user, list):
        return {"transcription": "Điều 1. Phạm vi", "uncertain_regions": [], "proposed_corrections": []}
    payload = json.loads(user)
    if "sentence_roles" in props:
        roles, ids = [], []
        for sentence in payload["sentences"]:
            usable = not sentence["forbidden_section"] and not sentence["explicit_legal_or_outcome"] and "NỘI DUNG" not in sentence["text"]
            roles.append({"sentence_id": sentence["id"], "role": "facts_reported" if usable else "court_reasoning", "speaker": "party_A", "dispute_status": "unknown"})
            if usable:
                ids.append(sentence["id"])
        return {"sentence_roles": roles, "usable_fact_ids": ids, "context_needed": [], "uncertainties": []}
    if "issues" in props:
        return {"issues": [{"issue_id": "i1", "issue_text": "Nghĩa vụ hoàn trả tiền vay", "query_text": "Cần tìm nguồn quy định nghĩa vụ hoàn trả tiền vay khi đến hạn?",
                            "fact_support_ids": [f["id"] for f in payload["facts"]], "missing_facts": [], "query_status": "answerable"}]}
    if "selected_candidate_ids" in props:
        return {"selected_candidate_ids": [payload["candidates"][0]["candidate_id"]], "resolution_status": "candidate_resolved",
                "support_sentence_ids": [c["id"] for c in payload["context"]], "ambiguity_reason": "", "missing_evidence": []}
    if "relevance_proposal" in props:
        return {"candidate_id": payload["source"]["candidate_id"], "relevance_proposal": "core_candidate", "version_status": "unresolved",
                "evidence_quotes": [payload["window"]["text"]], "source_roles": ["substantive"], "short_justification": "Supports repayment issue", "missing_context": []}
    if "proposed_requirement_groups" in props:
        ids = [u["source"]["candidate_id"] for u in payload["sources"]]
        return {"proposed_requirement_groups": [{"group_id": f"g{i}", "candidate_ids": [uid], "reason": "Needed source"} for i, uid in enumerate(ids)],
                "redundant_sources": [], "unresolved_version_conflicts": [], "missing_source_requests": [], "review_reasons": []}
    if "checks" in props:
        return {"checks": [{"criterion": "grounding", "status": "pass", "evidence_ids": [], "reason": "Synthetic fixture"}],
                "independent_relevance": [{"candidate_id": s["source"]["candidate_id"], "relevance_proposal": "core_candidate"} for s in payload["source_evidence"]], "review_reasons": []}
    if "facts" in props:
        return {"facts": "A giao tiền cho B và B chưa trả khi đến hạn.", "issue_text": "Nghĩa vụ hoàn trả", "query_text": "Nguồn nào quy định nghĩa vụ hoàn trả tiền vay?", "evidence_quotes": [payload["source"]["text"]], "missing_facts": []}
    raise AssertionError("Unexpected stage")


@pytest.fixture
def server():
    @contextmanager
    def start(handler=None):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"body": body, "authorization": self.headers.get("Authorization"), "path": self.path})
                if handler:
                    status, response = handler(body, len(requests))
                else:
                    status = 200
                    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(fake_output(body), ensure_ascii=False)}}], "usage": {"total_tokens": 10}}
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(response.encode() if isinstance(response, str) else json.dumps(response).encode())

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_port}/v1", requests
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)
    return start


@pytest.fixture
def fixture_env(tmp_path):
    for name in ["cases", "tvpl", "phapdien"]:
        (tmp_path / name).mkdir()
    law_rows = [{"id": "law1", "vn_text": LAW, "doc_number": None, "title": "Bo luat dan su", "url": "https://example.test/law",
                 "issue_date": "24/11/2015", "effective_date": "01/01/2017", "citations": []},
                {"id": "empty", "vn_text": "", "doc_number": None, "title": "empty", "url": "", "issue_date": None, "effective_date": None, "citations": []}]
    pq.write_table(pa.Table.from_pylist(law_rows), tmp_path / "tvpl/documents-00000.parquet")
    notes = ["(Điều 466 Bộ luật số 91/2015/QH13, có hiệu lực thi hành kể từ ngày 01/01/2017)", "(Điều 466 Luật số 12/2017/QH14, có hiệu lực thi hành kể từ ngày 01/01/2018)"]
    pd_rows = [{"record_id": f"pd{i}", "content_text": "Bên vay phải trả đủ tiền khi đến hạn.", "article_id": "Điều X.1", "subject_id": "subject1", "article_title": "Nghĩa vụ trả nợ",
                "source_note_text": note, "source_links": [{"href": "https://vbpl.vn/TW/Pages/vbpq-toanvan.aspx?ItemID=1", "text": note}], "source_url": "https://phapdien.moj.gov.vn/test"} for i, note in enumerate(notes)]
    pq.write_table(pa.Table.from_pylist(pd_rows), tmp_path / "phapdien/documents-00000.parquet")
    case_rows = [{"doc_name": cid, "markdown": CASE, "category": "Civil", "court": "Tòa A", "instance_level": "First-instance", "official_document_id": f"{i}/2020/DS-ST", "issued_date": "2020-01-01",
                  "citations_law": [{"ref": "Điều 466 Bộ luật dân sự", "article": "466", "law_name": "dân sự", "id": "91/2015/QH13", "span": []}], "citations": [], "citations_case": []} for i, cid in enumerate(["case1", "case2"], 1)]
    pq.write_table(pa.Table.from_pylist(case_rows), tmp_path / "cases/documents-00000.parquet")
    prompts = Path(__file__).resolve().parents[1] / "docs/rule_recall_annotation_prompts.md"
    env = tmp_path / ".env"
    env.write_text(f'''RR_JUDGMENTS_PATH=cases
RR_TVPL_PATH=tvpl
RR_PHAPDIEN_PATH=phapdien
RR_OUTPUT_DIR=out
RR_PROMPTS_FILE={prompts}
RR_PROFILES=TEST
RR_DEFAULT_PROFILE=TEST
RR_TEST_BASE_URL=http://127.0.0.1:9/v1
RR_TEST_MODEL=fixture-model
RR_TEST_AUTH_MODE=none
RR_TEST_RETRIES=0
RR_TEST_RETRY_BACKOFF=0
RR_TEST_MAX_INPUT_CHARS=60000
RR_TEST_CONTEXT_TOKENS=100000
RR_TEST_MAX_OUTPUT_TOKENS=4096
RR_TEST_EXTRA_BODY={{}}
RR_TEST_TEMPERATURE=
RR_POOL_SIZE=3
RR_MAX_ISSUES=1
RR_MAX_EXPANSION_ROUNDS=0
RR_ANNOTATION_LIMIT=1
RR_TRAIN_RATIO=0
RR_DEV_RATIO=1
''')
    return env
