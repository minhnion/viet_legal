# Vietnamese legal rule recall dataset pipeline

Pipeline đọc corpus bản án và hai corpus luật từ Parquet, xây chỉ mục lexical, tạo annotation bằng LLM OpenAI-compatible, xuất gói kiểm chứng và chỉ xuất gold sau review hợp lệ.

Thiết kế và nguồn nghiên cứu: [phương pháp](docs/rule_recall_dataset_benchmark_methodology.md), [triển khai](docs/rule_recall_ground_truth_implementation.md), [prompt](docs/rule_recall_annotation_prompts.md). Hướng dẫn dưới đây mô tả CLI và `.env` thực tế.

[Báo cáo kiểm thử](docs/rule_recall_pipeline_validation.md): 82 test tự động đã qua; đã chạy P1–P6 trên mẫu dữ liệu thật qua endpoint Qwen, kiểm tra resume và xuất corpus. Chưa phát hành gold khi chưa có review chuyên gia.

## Cài đặt

Chạy tại thư mục repository; Python 3.10+ và SQLite có FTS5. Môi trường đã thử là Python 3.11.

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.lock.txt
```

Nếu không dùng `uv`, tạo virtualenv bằng Python rồi cài `requirements.txt` bằng pip. File lock ghi phiên bản thực tế của môi trường kiểm tra; `requirements.txt` khai báo khoảng phiên bản được phép.

```bash
cp .env.example .env
chmod 600 .env
```

Nếu `.env` đã được chuẩn bị trên server này, chỉnh file đó thay vì chép đè. `.env` và dữ liệu đầu ra được gitignore. Script không đọc credential từ một repository khác; tất cả kết nối model nằm trong `.env` hoặc biến môi trường bắt đầu bằng `RR_`.

## Cấu hình

Các đường dẫn tương đối được tính từ **thư mục chứa file env**, không từ working directory. Có thể dùng đường dẫn tuyệt đối. Không thực thi shell hoặc nội suy `$()` trong giá trị env.

| Biến | Ý nghĩa |
|---|---|
| `RR_JUDGMENTS_PATH` | Thư mục chứa `documents-*.parquet` của bản án |
| `RR_TVPL_PATH`, `RR_PHAPDIEN_PATH` | Thư mục documents của hai nguồn luật |
| `RR_OUTPUT_DIR` | SQLite, checkpoint, cache, logs, review và dataset |
| `RR_PROMPTS_FILE` | Markdown chứa các prompt P1–P6, P0V và AUGMENT |
| `RR_PROFILES` | Tên các profile, ngăn bằng dấu phẩy, ví dụ `LOCAL,REMOTE` |
| `RR_DEFAULT_PROFILE` | Profile mặc định cho các stage |
| `RR_PROFILE_P1` … `RR_PROFILE_P6` | Override profile theo vai trò |
| `RR_PROFILE_P0V` | Profile ảnh; để trống nếu không dùng VLM |
| `RR_<PROFILE>_BASE_URL`, `MODEL` | API root và đúng tên model do endpoint phục vụ |
| `RR_<PROFILE>_AUTH_MODE` | `bearer` hoặc `none` |
| `RR_<PROFILE>_API_KEY` | Bắt buộc nếu `bearer`; không gửi auth header nếu `none` |
| `RR_<PROFILE>_OUTPUT_MODE` | `json_schema`, `json_object`, `prompt_json`; đều kiểm tra schema local |
| `RR_<PROFILE>_TOKEN_PARAMETER` | `max_tokens` hoặc `max_completion_tokens` |
| `RR_<PROFILE>_EXTRA_BODY` | JSON cho tham số riêng provider; không được override model/messages/schema/credential |
| `RR_<PROFILE>_TEMPERATURE` | Để trống nếu model không nhận tham số này |
| `RR_<PROFILE>_CONTEXT_TOKENS`, `MAX_OUTPUT_TOKENS`, `MAX_INPUT_CHARS` | Budget riêng của model; script dùng ước lượng UTF-8 bảo thủ, không cắt input ngầm |
| `RR_<PROFILE>_CONCURRENCY`, `TIMEOUT`, `RETRIES`, `RETRY_BACKOFF` | Gọi model và xử lý lỗi |
| `RR_<PROFILE>_MODALITIES` | JSON array, mặc định `["text"]`; VLM cần thêm `"image"` |
| `RR_LAW_MAX_RECORDS`, `RR_CASE_MAX_RECORDS` | Giới hạn dòng input đã quét; `0` là toàn bộ. Giới hạn law áp dụng riêng cho từng corpus luật |
| `RR_ANNOTATION_LIMIT` | Số vụ được chọn để annotation; mặc định 50, `0` là tất cả |
| `RR_CATEGORIES`, `RR_CASE_IDS` | JSON arrays để chọn lĩnh vực/doc_name; `[]` nghĩa là không lọc |
| `RR_POOL_SIZE`, `RR_MAX_EXPANSION_ROUNDS` | Pool nguồn và số vòng tìm bổ sung |
| `RR_MAX_CITATIONS`, `RR_LINK_CANDIDATES` | Ngân sách phân giải citation |
| `RR_MAX_SOURCE_WINDOWS` | Giới hạn đọc nguồn dài; vượt giới hạn được giữ `uncertain`, không gán negative. `0` đọc toàn bộ |
| `RR_SENTENCE_CHUNK_SIZE` | Số câu/đoạn trong P1, có ngữ cảnh chồng lặp khi budget cho phép |
| `RR_VERIFIER_ENABLED` | Bật/tắt P6; không thay đổi yêu cầu review gold |
| `RR_TRAIN_RATIO`, `RR_DEV_RATIO`, `RR_SEED` | Split ổn định theo case family; phần còn lại là test |
| `RR_FAMILY_MAP` | CSV `case_id,family_id` đã kiểm tra, tùy chọn |
| `RR_FEWSHOT_FILE` | JSON ví dụ theo stage; chỉ nhận ví dụ train/dev có `human_reviewed=true` |
| `RR_FAIL_FAST` | Dừng ngay hoặc ghi failure queue và xử lý vụ tiếp theo |

Các biến `RR_` trong process environment được ưu tiên hơn file env, thuận tiện cho lệnh pilot mà không sửa cấu hình gốc.

Ví dụ đổi sang OpenAI hoặc server khác: thêm `REMOTE` vào `RR_PROFILES`, khai báo `RR_REMOTE_BASE_URL`, `RR_REMOTE_MODEL`, `RR_REMOTE_AUTH_MODE`, `RR_REMOTE_API_KEY`, các budget/capability tương ứng và gán `RR_PROFILE_P6=REMOTE`. Với OpenAI API, API root là `https://api.openai.com/v1`; chọn token parameter/model theo khả năng thực tế. Không chuyển `chat_template_kwargs` của Qwen sang profile khác. Không có cơ chế tự đổi endpoint khi lỗi.

## Kiểm tra trước khi chạy

```bash
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env doctor --offline
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env doctor
.venv/bin/pytest -q
```

`doctor` kiểm tra dữ liệu, FTS5 và một request JSON giả định trên từng text profile. Nó không chứng minh chất lượng pháp lý hoặc capability ảnh. Preflight không phụ thuộc `/health` hoặc `/models` của provider.

## Chạy toàn bộ pipeline

```bash
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env run
```

Lệnh trên lần lượt chạy preflight → ingest/index luật → ingest/gom family/split bản án → P1–P6 → review packages → export candidate và gold đã đủ review. Với `.env.example`, ingest đọc toàn bộ documents nhưng annotation chỉ xử lý tối đa **50 vụ Civil**. Mỗi vụ có thể tạo nhiều issue queries.

Để annotation **tất cả bản án ở mọi lĩnh vực**, sau khi đã hiệu chỉnh pilot:

```bash
RR_ANNOTATION_LIMIT=0 RR_CATEGORIES='[]' .venv/bin/python scripts/run_rule_recall_pipeline.py --env .env run --retry-failed
```

Đây là lệnh quét và gọi LLM trên toàn bộ corpus; thời gian phụ thuộc số legal units, độ dài bản án, pool và endpoint. Không có giả định rằng chạy full tự động sẽ tạo gold hoàn chỉnh. Những mẫu thất bại hoặc nguồn quá dài vẫn được ghi lại để review.

Có thể tách các bước để theo dõi:

```bash
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env build-laws
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env build-cases
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env annotate
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env review-export
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env export --include-corpus
```

Để giữ terminal gọn, có thể chuyển log ra file bằng `> pipeline.log 2>&1`. Theo dõi trạng thái từ terminal khác:

```bash
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env status
```

Exit code `0` là lệnh hoàn tất; `2` là lỗi cấu hình/validation hoặc có case annotation thất bại. `export/failures.jsonl`, bảng `failures` trong SQLite và `logs/model_calls.jsonl` cho biết lỗi để xử lý. Lỗi HTTP/schema/refusal/truncation không được chuyển thành nhãn pháp lý.

## Pilot nhỏ và chạy tiếp

Không dùng snapshot giới hạn để tuyên bố độ phủ toàn corpus. Dùng output directory riêng khi thử:

```bash
RR_OUTPUT_DIR=outputs/pilot RR_LAW_MAX_RECORDS=300 RR_CASE_MAX_RECORDS=512 RR_ANNOTATION_LIMIT=2 RR_CATEGORIES='[]' RR_POOL_SIZE=3 RR_MAX_SOURCE_WINDOWS=2 .venv/bin/python scripts/run_rule_recall_pipeline.py --env .env run
```

Lệnh pilot trên có thể bỏ lỡ đúng văn bản luật vì chỉ ingest một phần. Mục đích là kiểm tra vận hành; muốn đo retrieval cần ingest kho luật phù hợp và kiểm chứng độ phủ.

Chạy lại cùng lệnh sẽ bỏ qua shard/batch/case đã hoàn tất, dùng cache cho request giống hệt. Có thể dừng bằng Ctrl+C rồi chạy lại; checkpoint được commit cùng dữ liệu của batch. Chỉ một writer được phép dùng cùng output directory.

```bash
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env annotate --retry-failed
```

Case thất bại chỉ được thử lại khi có `--retry-failed`. Tăng budget hoặc sửa prompt khi cần; không lặp vô hạn đến khi model cho một đáp án chắc chắn. Response sai grounding/ID không được lưu thành cache thành công.

Đổi model/profile, prompt, code hoặc snapshot tạo annotation run khác. Dữ liệu cũ vẫn có provenance; `export` chỉ xuất active run. Thay nội dung shard hoặc parser version cần **output directory mới**, không ghi đè lịch sử nguồn. Tăng giới hạn ingest có thể tiếp tục từ checkpoint nhưng thay snapshot/family sẽ yêu cầu annotation lại trước review/export. `status`/manifest ghi rõ corpus nào chưa ingest hết.

## Đầu ra

```text
RR_OUTPUT_DIR/
  pipeline.sqlite3
  cache/                         response hợp lệ, có profile/prompt/request hash
  logs/model_calls.jsonl         usage, thời gian, lỗi; không ghi auth headers/key
  review/
    independent_bundles.jsonl    query và nguồn, ẩn nhãn model/citation bản án
    independent_template.jsonl   form trống nhãn nguồn cho vòng độc lập
    bundles.jsonl                bản án, nguồn, đề xuất model để đối soát
    template.jsonl               form review có đề xuất, chưa được duyệt
  export/
    candidates.jsonl             annotation ứng viên, không phải gold
    failures.jsonl
    review_status.jsonl
    gold_queries.jsonl           input cho model, tách provenance bản án
    gold_qrels.jsonl
    gold_evidence.jsonl
    gold_provenance.jsonl        nguồn, family, người kiểm tra, phiên bản
    manifest.json
    corpus.jsonl                chỉ khi export --include-corpus
```

`gold_*` rỗng trước review là hành vi đúng. Các ID unit chứa hash nội dung nguồn để phân biệt hai bản text; phiên bản pháp lý/hiệu lực vẫn cần xác nhận bằng nguồn chính thức. Toàn bộ corpus retrieval gồm các Điều đã index, không chỉ các Điều được gán gold.

## Review và xuất gold

Tạo hai file độc lập từ form chưa duyệt; ít nhất một lượt bắt đầu bằng `independent_*` trước khi xem đề xuất model. Reviewer có thể tìm thêm nguồn bằng `search`/`read`, thay vì chỉ chọn trong pool.

```bash
cp outputs/rule_recall/review/independent_template.jsonl reviewer_a.jsonl
cp outputs/rule_recall/review/independent_template.jsonl reviewer_b.jsonl
```

Người có chuyên môn cần chỉnh từng record:

- `reviewer_id` thật, `decision=approve` hoặc `reject`; giữ `query_id` và `base_hash`.
- Xác nhận riêng `facts_checked`, `query_checked`, `family_checked`, `coverage_checked`.
- Chỉnh facts bằng `facts_sentence_ids` trỏ tới câu gốc; sửa issue/query khi cần.
- `qrels`: `unit_id`, grade `0/1/2`, evidence offsets `[start,end)` trên `unit.text` theo Unicode code points. Nguồn dương phải có evidence.
- `requirement_groups`: mỗi nhóm có một hoặc nhiều nguồn core thay thế được cho cùng nhu cầu; nguồn bổ sung nhau nằm ở các nhóm khác nhau.
- `version_confirmations`: mọi nguồn dương có `verified=true`, URL nguồn chính thức, `version_key` thống nhất theo guideline, mô tả và ngày hiệu lực đã kiểm tra hoặc null khi không xác định được. Không suy ngày từ trí nhớ.
- `answerability=needs_clarification` không được ép một tập nguồn đúng duy nhất. Track `corpus_missing` chưa được tự tạo; cần thí nghiệm withholding có kiểm soát riêng.

```bash
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env review-import --file reviewer_a.jsonl
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env review-import --file reviewer_b.jsonl
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env export --include-corpus
```

Gold yêu cầu ít nhất hai reviewer khác ID đồng ý về target chuẩn hóa. Bất đồng cần reviewer thứ ba với `decision=adjudicate` và `resolves_review_hashes` khớp các review đã nhận; xem các hash trong `export/review_status.jsonl`. Import không thể xác thực chuyên môn hay sự độc lập thật của con người, nên đây vẫn là trách nhiệm của protocol annotation.

Không tự sửa review đã import; dùng `review-import --replace` nếu cần. Mọi phiên bản nhận vào được giữ trong `review_events`. Một review được sửa sẽ làm adjudication dựa trên hash cũ không còn hợp lệ.

## Tra cứu và kiểm tra nguồn

```bash
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env search --query 'nghĩa vụ hoàn trả tiền vay' --limit 10
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env read --id 'UNIT_ID'
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env versions --id 'UNIT_ID'
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env relations --id 'UNIT_ID'
```

`versions` liệt kê các bản thể hiện quan sát được có cùng số hiệu/Điều; không phải lịch sử hiệu lực hoàn chỉnh đã được xác minh. `relations` là crosswalk/dẫn chiếu ứng viên. Parser giữ dấu hiệu Điều trùng hoặc cấu trúc sửa đổi lồng để review, không tự hợp nhất luật thành một bản có giá trị pháp lý.

Case family tự động gom exact/layout duplicates và một số reference không mơ hồ; vẫn cần kiểm tra bằng người/CSV. Chưa có cam kết nhận diện mọi paraphrase hoặc mọi liên kết sơ thẩm–phúc thẩm. Không phát hành split gold chỉ dựa trên grouping heuristic.

## VLM và dữ liệu synthetic tùy chọn

Sau khi cấu hình profile ảnh và `RR_PROFILE_P0V`, tạo manifest JSONL:

```json
{"page_id":"judgment_x_page_1","image_path":"images/page1.png","existing_text":"Text cần đối chiếu"}
```

```bash
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env vision --manifest vision_pages.jsonl
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env augment --limit 10
```

Ảnh PNG/JPEG/WebP được gửi bằng content parts của Chat Completions. PDF cần render thành ảnh trước; pipeline không tự tải URL PDF hoặc tự áp text VLM vào corpus. Chọn token margin/budget phù hợp ảnh của provider. `vision_review.jsonl` luôn yêu cầu kiểm tra.

`augment` sinh `synthetic_train.jsonl`, tách khỏi gold và chỉ có split train. Round-trip ở đây là chẩn đoán lexical, không phải tái hiện việc huấn luyện retriever của Promptagator. Chưa có dense index/reranker huấn luyện riêng; pooling ban đầu dùng BM25, citation và relation expansion.

## Đánh giá offline

File dự đoán JSONL có `query_id`, `ranking` là danh sách unit IDs và tùy chọn `evidence=[{"unit_id":"...","spans":[{"start":0,"end":10}]}]`.

```bash
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env evaluate --predictions predictions.jsonl --k 20
```

Evaluator báo flat Recall/Precision/F2, nDCG, requirement coverage, complete coverage, evidence F1 và số kết quả unjudged. Group coverage xử lý alternative sources; flat recall vẫn đo từng ID riêng. Chưa chấm tự động đúng/sai pháp lý cho nguồn unjudged hoặc đo đầy đủ agent latency/tool budget. RR3 dùng lại gold nguồn/evidence; agent runner và SFT trajectory nằm ngoài CLI chuẩn bị dữ liệu này.
