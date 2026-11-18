# 03. Triển khai và vận hành

*Cập nhật 18/11/2026. Thiết kế pipeline nằm ở [02_pipeline.md](02_pipeline.md).*

## 1. Cấu trúc code

| Module | Vai trò |
|---|---|
| `cli.py` | Định tuyến lệnh; giữ `FileLock` trên `RR_OUTPUT_DIR/.pipeline.lock` để mỗi output dir chỉ có một writer |
| `config.py` | Toàn bộ cấu hình lấy từ file env và biến `RR_*`; quản lý các profile model; stage `ISSUE`, `EVIDENCE`, `AUGMENT` (+ `P0V`) |
| `ingest.py` | Đọc Parquet theo shard có checkpoint; tách units; crosswalk Pháp điển → TVPL; dựng bảng `acts`; dựng case family và split |
| `store.py` | Schema SQLite (WAL); tra cứu unit, khoản, bản tương đương; FTS5 |
| `citations.py` | Tách phần bản án; trích căn cứ trên stream đã bỏ dấu và khoảng trắng; `ActIndex` (tra theo tên, số hiệu, phiên bản); phân loại vai trò |
| `pipeline.py` | `prepare` (code) → `run_model` (ISSUE, EVIDENCE) → `build` (dựng bản ghi); điều phối song song, ghi lại kết quả, `audit` |
| `contracts.py` | JSON Schema của từng stage; `PromptBank` đọc [prompts.md](prompts.md) |
| `client.py` | Gọi Chat Completions chuẩn OpenAI-compatible; đếm token qua `/tokenize` của vLLM; retry; cache; log đã che thông tin nhạy cảm; Langfuse (tuỳ chọn) |
| `review.py` | Template và gói review; import review; export `candidates`/`silver_*`/`gold_*`/`manifest` (có funnel) |
| `evaluate.py` | Recall/Precision/F2, nDCG, requirement coverage, evidence F1 trên nhãn `gold` hoặc `silver` |

## 2. Cài đặt và cấu hình

Môi trường đang dùng trên server là conda env `vietlegal` (Python 3.11, pyarrow 26). Lưu ý: pyarrow 19 của base env **không đọc được** shard bản án. Nếu dùng virtualenv thì cài theo `requirements.lock.txt`.

Mọi đường dẫn tương đối trong `.env` được tính từ **thư mục chứa file env**. Biến `RR_*` đặt trong môi trường của process sẽ ghi đè giá trị trong file.

| Biến | Ý nghĩa |
|---|---|
| `RR_JUDGMENTS_PATH`, `RR_TVPL_PATH`, `RR_PHAPDIEN_PATH` | Thư mục chứa `documents-*.parquet` |
| `RR_OUTPUT_DIR` | SQLite, cache, logs, review, export. **Mỗi lần thử nghiệm dùng một thư mục riêng** |
| `RR_PROMPTS_FILE` | `specs/prompts.md` |
| `RR_PROFILES`, `RR_DEFAULT_PROFILE`, `RR_PROFILE_ISSUE`, `RR_PROFILE_EVIDENCE`, `RR_PROFILE_AUGMENT`, `RR_PROFILE_P0V` | Profile model, và profile dùng cho từng stage |
| `RR_<P>_BASE_URL`, `MODEL`, `AUTH_MODE`, `API_KEY`, `OUTPUT_MODE`, `TOKEN_PARAMETER`, `EXTRA_BODY`, `TEMPERATURE` | Kết nối endpoint OpenAI-compatible |
| `RR_<P>_CONTEXT_TOKENS`, `MAX_OUTPUT_TOKENS`, `TOKEN_MARGIN` | Ngân sách token. `CONTEXT_TOKENS` **phải bằng `max_model_len` của server**: xem `GET /v1/models`. Ngày 18/11 server phục vụ 16384 |
| `RR_<P>_CONCURRENCY`, `TIMEOUT`, `RETRIES`, `RETRY_BACKOFF` | Số vụ chạy song song và chính sách retry HTTP |
| `RR_STAGE_MAX_OUTPUT_TOKENS` | Trần output theo stage, ví dụ `{"ISSUE":2048,"EVIDENCE":1536}` |
| `RR_MAX_ISSUES` | Số issue tối đa mỗi vụ (mặc định 3) |
| `RR_LAW_MAX_RECORDS`, `RR_CASE_MAX_RECORDS` | Giới hạn số dòng ingest (0 = tất cả) |
| `RR_ANNOTATION_LIMIT`, `RR_CATEGORIES`, `RR_CASE_IDS` | Chọn vụ để annotate (0 / `[]` = không lọc) |
| `RR_TRAIN_RATIO`, `RR_DEV_RATIO`, `RR_SEED`, `RR_FAMILY_MAP` | Chia split theo case family |
| `RR_FEWSHOT_FILE` | Few-shot theo stage; chỉ nhận ví dụ train/dev có `human_reviewed=true` |
| `RR_FAIL_FAST`, `RR_LANGFUSE_*` | Dừng ngay khi có vụ lỗi; tracing |

**Đổi sang model cloud hoặc model mạnh hơn:** thêm một profile mới (ví dụ `REMOTE`) với `BASE_URL`, `MODEL`, `API_KEY`, ngân sách token, rồi gán `RR_PROFILE_ISSUE=REMOTE` và/hoặc `RR_PROFILE_EVIDENCE=REMOTE`. Không cần sửa code.

## 3. Lệnh

```bash
conda activate vietlegal
cd /home/ai4life/minhdh/law_bench/viet_legal
python scripts/run_rule_recall_pipeline.py --env .env <lệnh>
```

| Lệnh | Việc làm |
|---|---|
| `doctor [--offline]` | Kiểm tra dữ liệu, FTS5, prompt, và gửi một request thử tới mỗi profile |
| `build-laws` | Ingest TVPL và Pháp điển (bỏ qua shard đã xong), dựng lại crosswalk và bảng `acts`, lưu số lượng nguồn/unit |
| `build-cases` | Ingest bản án, gom family, chia split |
| `audit [--limit N]` | Chỉ chạy code (không gọi LLM): thống kê funnel về văn bản đủ điều kiện, tỉ lệ resolve, vai trò căn cứ; ghi ra `audit.json` |
| `annotate [--retry-failed]` | Chạy ISSUE + EVIDENCE cho các vụ được chọn; bỏ qua vụ đã `complete`/`skipped` |
| `run` | Lần lượt `doctor` → `build-laws` → `build-cases` → `annotate` → `review-export` → `export` |
| `review-export`, `review-import --file F [--replace]` | Tạo gói review, nhập kết quả review |
| `export [--include-corpus]` | Ghi dataset ra `export/` |
| `status`, `search`, `read`, `versions`, `relations` | Tra cứu (chỉ đọc, không giữ lock) |
| `evaluate --predictions F [--k 20] [--labels gold\|silver]` | Chấm một file dự đoán |
| `vision`, `augment` | Các nhánh tuỳ chọn: VLM đối chiếu ảnh trang; synthetic train |

Exit code `2` nghĩa là lỗi cấu hình hoặc validation, **hoặc** có vụ annotate thất bại. Khi run dừng do lỗi hạ tầng, lệnh in ra `ERROR <code>`; chạy lại đúng lệnh để tiếp tục.

**Chạy smoke trên output dir sample đã có kho luật** (không ingest lại):

```bash
RR_OUTPUT_DIR=/data/minhdh/law_bench/output/sample RR_ANNOTATION_LIMIT=80 RR_CASE_IDS='[]' RR_CATEGORIES='[]' \
  python scripts/run_rule_recall_pipeline.py --env .env audit --limit 1000
RR_OUTPUT_DIR=/data/minhdh/law_bench/output/sample RR_ANNOTATION_LIMIT=80 RR_CASE_IDS='[]' RR_CATEGORIES='[]' \
  python scripts/run_rule_recall_pipeline.py --env .env annotate
RR_OUTPUT_DIR=/data/minhdh/law_bench/output/sample python scripts/run_rule_recall_pipeline.py --env .env export
```

**Chạy toàn bộ** (`python scripts/run_rule_recall_pipeline.py --env .env run`) với `RR_OUTPUT_DIR` mới sẽ ingest lại kho luật. Bước này mất khoảng 50 phút và cần trên 53 GB đĩa. Ngày 18/11, `/data` đã dùng 95%. Nếu muốn dùng lại kho luật đã ingest, hãy trỏ vào `output/sample` và đặt `RR_CASE_MAX_RECORDS=0`: pipeline sẽ ingest tiếp phần bản án còn lại từ checkpoint.

## 4. Đầu ra

```text
RR_OUTPUT_DIR/
  pipeline.sqlite3             kho luật, acts, cases, annotations, queries, reviews
  cache/                       response model đã qua validator (khoá = request + schema)
  logs/model_calls.jsonl       usage, thời gian, mã lỗi (không có key hay header)
  logs/invalid_outputs/        output bị cắt hoặc sai schema, để debug
  audit.json                   kết quả của lệnh audit
  review/
    template.jsonl             form review đã điền sẵn đề xuất (qrels, groups, evidence)
    independent_template.jsonl form trống cho vòng review độc lập
    bundles.jsonl              query + toàn văn bản án + các unit đáp án
    independent_bundles.jsonl  chỉ có model_input (người review tự tìm nguồn)
  export/
    candidates.jsonl           toàn bộ query của run đang active (silver + needs_review), đầy đủ annotation
    silver_queries.jsonl       phần model được thấy: query_id, split, facts (các dòng liền nhau đã nối thành đoạn), issue, question, timeline
    silver_qrels.jsonl         qrels (unit_id, grade, evidence) + requirement_groups, cùng định dạng gold
    silver_evidence.jsonl      span theo từng unit (gồm cả bản tương đương)
    silver_provenance.jsonl    confidence, review_reasons, nguồn đã resolve, provenance của bản án
    gold_*.jsonl               chỉ khi đủ review
    failures.jsonl             các vụ failed của run đang active (mã lỗi + thông điệp + thống kê)
    review_status.jsonl
    manifest.json              run_hash, snapshot, funnel (vụ, căn cứ, query theo quality/confidence)
```

## 5. Định danh run, cache và chạy tiếp

- `run_hash` = hash(snapshot kho, prompt bank + few-shot, mã nguồn `legal_recall/*.py`, profile model, binding stage, `max_issues`, trần output). Đổi bất kỳ thứ nào trong số này là tạo một run mới, và các vụ được annotate lại.
- **Cache không phụ thuộc `run_hash`**, nên request giống hệt sẽ được dùng lại qua các run.
- **ID query không phụ thuộc `run_hash`**, nên review đã nhập vẫn khớp nếu nội dung không đổi (`base_hash`).
- Trạng thái mỗi vụ trong mỗi run: `complete` / `skipped` (không chạy lại) / `failed` (chỉ chạy lại khi có `--retry-failed`).
- `export` chỉ xuất run đang active, và từ chối xuất nếu snapshot kho luật hoặc family đã thay đổi kể từ lúc annotate.
- Số lượng nguồn và unit được lưu trong `meta.law_counts` khi chạy `build-laws`, nên `status`/`export` không phải đếm lại 17 triệu dòng.

## 6. Review và gold

Gold vẫn cần **ít nhất 2 người duyệt độc lập đồng ý**, hoặc có người thứ 3 phân xử. Validator nằm trong `review.py` và không thay đổi so với trước. Quy trình khi có người tham gia:

1. `review-export`, rồi chép `review/template.jsonl` cho mỗi người. Nếu muốn có một vòng độc lập thì dùng `independent_template.jsonl`.
2. Người duyệt xác nhận facts, câu hỏi, nguồn và phiên bản (URL nguồn chính thức), sửa evidence hoặc nhóm nếu cần, rồi đặt `decision=approve` hoặc `reject`.
3. `review-import --file …` cho từng người, sau đó `export`.

**Gợi ý thứ tự kiểm tra:** các query `confidence=low` → các query có `review_reasons` (`court_citation_rejected_by_model`, `question_repeats_article_title`, `unresolved_substantive_citations_in_case`) → các query `needs_review` → lấy mẫu ngẫu nhiên các query `high` để ước lượng tỉ lệ sai.

## 7. Kiểm thử

`pytest -q` chạy 139 test trong khoảng 35 giây. Test không gọi mạng; model được giả lập bằng một server HTTP local.

| File | Phạm vi |
|---|---|
| `tests/test_citations.py` | Tách phần; danh sách Điều, khoản, điểm; lỗi OCR; viết tắt; số hiệu bị tách; tên có dấu câu, alias, tên thiếu chữ; mượn văn bản theo ngữ cảnh và trường hợp không được mượn; chọn phiên bản (năm, ngày, kế nhiệm, không chọn văn bản tương lai); tra số hiệu; vai trò; ngày xét xử; tên văn bản từ tiêu đề TVPL. Đầu vào lấy từ các mẫu lỗi thật đã gặp khi audit |
| `tests/test_pipeline.py` | Ingest, resume, bảng acts; end-to-end (nhóm cross-version, bản tương đương, offset evidence, payload không lộ thông tin bản án); ID ổn định giữa các run; review gate; sửa leakage có/không cache; loại riêng issue lộ đáp án; EVIDENCE bị cắt thì fallback; model bác căn cứ; lỗi hạ tầng làm dừng run; vượt context; chạy song song nhiều vụ; CLI `run`/`audit`/`evaluate --labels silver`/`augment`; vision |
| `tests/test_client.py`, `tests/test_config_text.py`, `tests/test_evaluate.py` | Client (chế độ output, retry, cache, đếm token, Langfuse), cấu hình, chuẩn hoá text, metric |

Smoke test thật đã chạy: 10 vụ sample và 80 vụ đầu tiên. Kết quả ở [02_pipeline.md §7](02_pipeline.md#7-số-liệu-đo-trên-dữ-liệu-thật).

## 8. Việc tiếp theo

1. **Bổ sung nguồn tòa không trích:** pool BM25 + dense trên `question`, cho EVIDENCE chấm, gắn `label_source=retrieval_added`, confidence thấp hơn.
2. **Viết lại câu hỏi chép tiêu đề Điều:** tự repair khi có cờ `question_repeats_article_title`, hoặc dùng model mạnh hơn cho stage ISSUE.
3. **Xử lý vụ vượt context 16k:** dùng profile có context lớn hơn cho ISSUE, hoặc chọn facts theo đoạn.
4. **Track thủ tục:** dùng các căn cứ `procedural` đã lưu để tạo nhóm query riêng.
5. **Chấm chéo bằng model khác họ** (cloud) trên các mục `medium`/`low` và một mẫu `high`, để ước lượng tỉ lệ sai của silver.
