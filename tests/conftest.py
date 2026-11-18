from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


BLDS_2015 = ("QUỐC HỘI\nLuật số: 91/2015/QH13\nBỘ LUẬT DÂN SỰ\n"
             "Điều 463. Hợp đồng vay tài sản\nHợp đồng vay tài sản là sự thỏa thuận giữa các bên, theo đó bên cho vay giao tài sản cho bên vay;\n"
             "khi đến hạn trả, bên vay phải hoàn trả cho bên cho vay tài sản cùng loại.\n"
             "Điều 466. Nghĩa vụ trả nợ của bên vay\n1. Bên vay tài sản là tiền thì phải trả đủ tiền\nkhi đến hạn.\n"
             "2. Trường hợp bên vay không thể trả vật thì có thể trả bằng tiền.\n"
             "Điều 468. Lãi suất\n1. Lãi suất vay do các bên thỏa thuận.\n2. Lãi suất phát sinh do chậm trả tiền.")
BLDS_2005 = ("QUỐC HỘI\nSố: 33/2005/QH11\nBỘ LUẬT DÂN SỰ\n"
             "Điều 471. Hợp đồng vay tài sản\nHợp đồng vay tài sản là sự thoả thuận giữa các bên, theo đó bên cho vay giao tài sản cho bên vay;\n"
             "khi đến hạn trả, bên vay phải hoàn trả cho bên cho vay tài sản cùng loại.\n"
             "Điều 474. Nghĩa vụ trả nợ của bên vay\n1. Bên vay tài sản là tiền thì phải trả đủ tiền khi đến hạn.\n"
             "2. Trong trường hợp bên vay không thể trả vật thì có thể trả bằng tiền.")
BLTTDS = ("QUỐC HỘI\nLuật số: 92/2015/QH13\nBỘ LUẬT TỐ TỤNG DÂN SỰ\nĐiều 26. Những tranh chấp về dân sự\n3. Tranh chấp về hợp đồng dân sự.\n"
          "Điều 35. Thẩm quyền của Tòa án nhân dân cấp huyện\n1. Tòa án nhân dân cấp huyện có thẩm quyền giải quyết.")
NQ_326 = "ỦY BAN THƯỜNG VỤ QUỐC HỘI\nNghị quyết số: 326/2016/UBTVQH14\nĐiều 27. Nghĩa vụ chịu án phí\n1. Bị đơn phải chịu án phí."
CASE = ("TÒA ÁN NHÂN DÂN HUYỆN A\nBản án số: 11/2019/DS-ST\nNgày: 15 – 5 – 2019\n"
        "NỘI DUNG VỤ ÁN:\nNgày 24/8/2014 bà T cho bà S vay 10.000.000 đồng, có biên nhận do bà S viết và ký, thời hạn vay 30 ngày.\n"
        "Đến hạn bà T đòi nhiều lần nhưng bà S không trả.\nBà T khởi kiện yêu cầu bà S trả số tiền vay.\n"
        "NHẬN ĐỊNH CỦA TÒA ÁN:\n[1] Về tố tụng: Quan hệ tranh chấp là hợp đồng vay tài sản theo khoản 3 Điều 26 của Bộ luật tố tụng dân sự.\n"
        "[2] Về nội dung: Buộc bà S trả bà T số tiền vay là đúng theo quy định tại Điều 471, 474 Bộ luật dân sự năm 2005.\n"
        "[3] Về án phí: Bà S phải chịu án phí theo Điều 27 Nghị quyết 326/2016/UBTVQH 14.\n"
        "QUYẾT ĐỊNH:\nÁp dụng các Điều 463, 466 Bộ luật dân sự năm 2015; Điều 27 Nghị quyết số 326/2016/UBTVQH14.\n"
        "Chấp nhận yêu cầu khởi kiện của bà T.")
# A decision without a facts section: skipped by construction, never sent to the model.
DECISION_ONLY = ("QUYẾT ĐỊNH\nCÔNG NHẬN THUẬN TÌNH LY HÔN\nCăn cứ vào Điều 212 và 213 của Bộ luật Tố tụng dân sự;\n"
                 "XÉT THẤY\nViệc thuận tình ly hôn là hoàn toàn tự nguyện.\nQUYẾT ĐỊNH\n1. Công nhận sự thuận tình ly hôn.")


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
    if "issues" in props:
        return {"issues": [{"issue_id": "i1", "citation_ids": [c["id"] for c in payload["citations"]],
                            "fact_ids": [f["id"] for f in payload["facts"]], "issue_text": "Nghĩa vụ trả nợ của bên vay khi hết thời hạn vay",
                            "query_text": "Bên vay không trả tiền khi đến hạn thì những quy định nào điều chỉnh nghĩa vụ trả nợ?"}],
                "unused_citation_ids": []}
    if "assessments" in props:
        return {"assessments": [{"source_id": src["id"], "relevance": "core", "clause_ids": [src["clauses"][0]["id"]],
                                 "reason": "Quy định trực tiếp nghĩa vụ trả nợ"} for src in payload["sources"]]}
    if "facts" in props:
        return {"facts": "A giao tiền cho B và B chưa trả khi đến hạn.", "issue_text": "Nghĩa vụ hoàn trả", "query_text": "Nguồn nào quy định nghĩa vụ hoàn trả tiền vay?", "evidence_quotes": [payload["source"]["text"]], "missing_facts": []}
    raise AssertionError("Unexpected stage")


class Seen(list):
    """Chat requests; tokenize requests are recorded separately so call counts stay comparable."""

    def __init__(self):
        super().__init__()
        self.tokenize = []


def approximate_tokens(body):
    return len(json.dumps(body.get("messages"), ensure_ascii=False)) // 4


@pytest.fixture
def server():
    @contextmanager
    def start(handler=None, tokenize=None):
        requests = Seen()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                record = {"body": body, "authorization": self.headers.get("Authorization"), "path": self.path}
                if self.path.endswith("/tokenize"):
                    requests.tokenize.append(record)
                    status, response = tokenize(body) if tokenize else (200, {"count": approximate_tokens(body)})
                elif handler:
                    requests.append(record)
                    status, response = handler(body, len(requests))
                else:
                    requests.append(record)
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
    def act(id, text, title, doc_type, issued, effective, status, doc_number=None):
        return {"id": id, "vn_text": text, "doc_number": doc_number, "doc_type": doc_type, "title": title, "url": f"https://example.test/{id}",
                "issue_date": issued, "effective_date": effective, "status": status, "is_english": False, "citations": []}
    law_rows = [act("law1", BLDS_2015, "Bo luat dan su", "Bộ luật", "24/11/2015", "01/01/2017", "Còn hiệu lực"),
                act("law2005", BLDS_2005, "Bo luat Dan su 2005 33 2005 QH11", "Bộ luật", "14/06/2005", "01/01/2006", "Hết hiệu lực: 01/01/2017", "33/2005/QH11"),
                act("tt", BLTTDS, "Bo luat to tung dan su", "Bộ luật", "25/11/2015", "01/07/2016", "Còn hiệu lực"),
                act("nq326", NQ_326, "Nghi quyet 326 2016 UBTVQH14 an phi le phi Toa an", "Nghị quyết", "30/12/2016", "01/01/2017", "Còn hiệu lực",
                    "326/2016/UBTVQH14-MUC-THU-AN-PHI"),
                # TVPL also stores translated pages with the Vietnamese text under a suffixed number.
                act("law1en", BLDS_2015, "Law No 91 2015 QH13 The Civil Code", None, None, None, None, "91/2015/QH13-THE-CIVIL-CODE"),
                act("empty", "", "empty", None, None, None, None)]
    pq.write_table(pa.Table.from_pylist(law_rows), tmp_path / "tvpl/documents-00000.parquet")
    notes = ["(Điều 466 Bộ luật số 91/2015/QH13, có hiệu lực thi hành kể từ ngày 01/01/2017)", "(Điều 466 Luật số 12/2017/QH14, có hiệu lực thi hành kể từ ngày 01/01/2018)"]
    pd_rows = [{"record_id": f"pd{i}", "content_text": "1. Bên vay tài sản là tiền thì phải trả đủ tiền khi đến hạn.\n2. Trường hợp bên vay không thể trả vật thì có thể trả bằng tiền.",
                "article_id": "Điều X.1", "subject_id": "subject1", "article_title": "Nghĩa vụ trả nợ của bên vay",
                "source_note_text": note, "source_links": [{"href": "https://vbpl.vn/TW/Pages/vbpq-toanvan.aspx?ItemID=1", "text": note}], "source_url": "https://phapdien.moj.gov.vn/test"} for i, note in enumerate(notes)]
    pq.write_table(pa.Table.from_pylist(pd_rows), tmp_path / "phapdien/documents-00000.parquet")
    case_rows = [{"doc_name": cid, "markdown": text, "category": "Civil", "court": "Tòa A", "instance_level": "First-instance", "official_document_id": f"{i}/2019/DS-ST",
                  "issued_date": "2019-05-15", "citations_law": [], "citations": [], "citations_case": []}
                 for i, (cid, text) in enumerate([("case1", CASE), ("case2", DECISION_ONLY)], 1)]
    pq.write_table(pa.Table.from_pylist(case_rows), tmp_path / "cases/documents-00000.parquet")
    prompts = Path(__file__).resolve().parents[1] / "specs/prompts.md"
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
RR_TEST_CONTEXT_TOKENS=100000
RR_TEST_MAX_OUTPUT_TOKENS=4096
RR_TEST_EXTRA_BODY={{}}
RR_TEST_TEMPERATURE=
RR_MAX_ISSUES=2
RR_ANNOTATION_LIMIT=0
RR_TRAIN_RATIO=0
RR_DEV_RATIO=1
''')
    return env
