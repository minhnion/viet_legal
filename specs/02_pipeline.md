# 02. Pipeline build dataset

*Cập nhật 18/11/2026, theo code trong `legal_recall/`. Mục tiêu và định nghĩa task nằm ở [01_bai_toan_va_dataset.md](01_bai_toan_va_dataset.md).*

## 1. Tổng quan

```
TVPL + Pháp điển ─► build-laws ─► units (Điều/Khoản/Điểm) + FTS5 + crosswalk + acts (chỉ mục văn bản)
Bản án cbba ──────► build-cases ─► cases + case family + split train/dev/test

annotate, cho mỗi vụ:
  [code]  tách phần ─► trích căn cứ ─► gắn tên văn bản ─► tra cứu phiên bản/Điều ─► phân loại vai trò
  [LLM]   ISSUE: gom căn cứ nội dung thành issue + viết câu hỏi chỉ từ facts      (1 call, sửa tối đa 1 lần)
  [LLM]   EVIDENCE: mỗi issue, xác nhận từng Điều + chọn khoản làm bằng chứng     (1 call / issue)
  [code]  qrels, requirement groups (bản sao + phiên bản), offset evidence, confidence, silver / needs_review

export ─► candidates.jsonl, silver_*.jsonl, gold_*.jsonl (sau review), failures.jsonl, manifest.json (funnel)
```

| | Pipeline cũ (P1–P6, đến 16/11) | Pipeline hiện tại |
|---|---|---|
| Nguồn của issue | LLM tự nghĩ từ facts | Các vấn đề tòa giải quyết bằng căn cứ |
| Nguồn của đáp án | Pool BM25 + LLM chấm từng Điều | Căn cứ tòa áp dụng, tra cứu trong kho |
| Số call LLM mỗi vụ | khoảng 94 | 2–4 |
| 10 vụ sample | 54 phút; 8/10 xong; 0/168 căn cứ của tòa được chấm `core` | 26 giây; 7/7 vụ đủ điều kiện xong; 11 query silver |

## 2. Kho luật (code, chạy một lần bằng `build-laws`)

- **Units.** Văn bản TVPL được tách thành Điều → Khoản → Điểm, có offset vào text gốc (`text.legal_units`). Mỗi dòng Pháp điển là một Điều. ID có dạng `tvpl:<id>@<hash nội dung>:a<Điều>:<offset>`.
- **Chỉ mục FTS5/BM25.** Dùng cho CLI `search` và `augment`. Pipeline dataset không dùng BM25 để tìm đáp án.
- **Bảng `acts`** (`ingest.build_acts`). Mỗi văn bản quy phạm ở TVPL (Bộ luật, Luật, Pháp lệnh, Hiến pháp, Nghị quyết, Nghị định, Thông tư…) được lưu với: `doc_type`, `name_key`, `number_key`, ngày ban hành, ngày hiệu lực, ngày hết hiệu lực (đọc từ `status`), và cờ `is_amendment`.
  - `name_key` là tên đã bỏ dấu, bỏ khoảng trắng, lấy từ tiêu đề TVPL: "Bo luat Dan su 2005 33 2005 QH11" → `dansu`; "Phap lenh 09 2014 UBTVQH13 trinh tu …" → `trinhtuthutuc…`.
  - Văn bản sửa đổi (tiêu đề có "sửa đổi"/"bổ sung") không bao giờ được dùng để trả lời một căn cứ chỉ ghi tên luật gốc.
  - Bảng được dựng trong khoảng 1 phút. Nó chỉ đọc các cột metadata nhỏ của Parquet, còn số hiệu lấy từ bảng `sources` bằng khoá chính. Trên kho đầy đủ có 72.256 văn bản.

## 3. Phân tích bản án (code, `legal_recall/citations.py`)

### 3.1 Tách phần

Code nhận diện heading viết hoa và so khớp sau khi bỏ dấu, bỏ khoảng trắng:

| Phần | Các heading được nhận |
|---|---|
| `facts` | NỘI DUNG VỤ ÁN, NỘI DUNG VIỆC DÂN SỰ, NỘI DUNG VỤ VIỆC, NHẬN THẤY |
| `reasoning` | NHẬN ĐỊNH CỦA TÒA ÁN, NHẬN ĐỊNH CỦA HỘI ĐỒNG XÉT XỬ, NHẬN ĐỊNH, XÉT THẤY |
| `decision` | QUYẾT ĐỊNH, TUYÊN XỬ, XỬ |

Dòng thân văn bản kiểu "Quyết định đưa vụ án ra xét xử số …" không được coi là heading, vì code yêu cầu ít nhất 80% chữ cái viết hoa.

Văn bản **không có phần facts** bị loại với lý do `no_facts_section`. Phần lớn trường hợp này là quyết định công nhận thuận tình ly hôn, đình chỉ hoặc huỷ án.

Facts là các câu thuộc phần facts sau khi đã bỏ những câu có số Điều hoặc kết quả xử (`text.sentences`, `explicit_legal_or_outcome`).

### 3.2 Trích căn cứ

Code quét phần `reasoning` và `decision` trên một **stream**: văn bản đã bỏ dấu, bỏ khoảng trắng, kèm bảng ánh xạ về offset gốc. Cách này chịu được lỗi OCR tách chữ ("Lu ật", "Bô ̣ luâ ̣t").

- **Danh sách Điều.** Các dạng như "khoản 3 Điều 26", "các Điều 6,7, 7a và 9 , Điều 30", "điểm b, h, r, s khoản 1, khoản 2 Điều 51" được nối thành một nhóm. Hậu tố chữ cái chỉ được nhận khi dính liền với chữ số trong văn bản gốc: "7a" là hậu tố, nhưng "474 Bộ" không thành "474b".
- **Tên văn bản đứng sau nhóm Điều.** Trước khi đọc tên, code bỏ qua các từ nối ("của", "theo", "tại", "khoản 1 điểm a"). Các dạng được hỗ trợ:
  - Tên có loại văn bản: so tên dài nhất trong bảng `acts`, bỏ qua dấu câu bên trong tên ("Hôn nhân & gia đình"). Nếu tên bị thiếu chữ, chấp nhận khi tiền tố chung dài từ 8 ký tự trở lên **và** chỉ có đúng một ứng viên; trường hợp này gắn cờ `instrument_name_misspelt`.
  - Viết tắt: BLDS, BLHS, BLTTDS, BLTTHS, BLLĐ, Luật HNGĐ.
  - Số hiệu: đọc từ văn bản gốc và nối lại mã bị tách bởi khoảng trắng ("326/2016/UBTVQH 14" → `326/2016/UBTVQH14`, "NĐ -CP" → `NĐ-CP`).
  - Năm: "năm 2015", "2015" đứng ngay sau tên.
- **Không có tên văn bản đi kèm** ("Điều 468 quy định …"): mượn văn bản vừa được nêu trước đó, cách tối đa 400 ký tự (`binding=context`).
- **Có loại văn bản nhưng không đọc được tên** ("Bộ luật D n sự"): để là `unrecognized_instrument`, **không** mượn văn bản khác.
- **"của Bộ luật này"** nằm trong đoạn trích nguyên văn luật, nên bị bỏ qua.

### 3.3 Tra cứu: văn bản, phiên bản, Điều

| Căn cứ ghi | Cách chọn | `method` → confidence |
|---|---|---|
| Có số hiệu | Khớp chính xác `number_key`; nếu không có thì khớp số hiệu + slug tên của TVPL ("326/2016/UBTVQH14-MUC-THU-…"); nếu vẫn không có thì khớp số/năm khi chỉ có một văn bản cùng loại | `instrument_number` → high; `instrument_number_loose` → medium |
| Tên + năm | Văn bản cùng tên có năm ban hành (hoặc năm hiệu lực) bằng năm được ghi | `name_and_year` → high |
| Tên, không năm, nhưng **chính bản án đó** ghi đúng một năm cho văn bản này ở chỗ khác | Dùng năm đó | `year_from_same_judgment` → medium |
| Tên, không năm | Văn bản có hiệu lực vào ngày xét xử. Mỗi văn bản cùng tên có hiệu lực từ ngày hiệu lực (thiếu thì lấy ngày ban hành) đến khi hết hiệu lực hoặc đến khi văn bản kế tiếp cùng tên có hiệu lực | `in_force_on_judgment_date` → medium |
| Chỉ có một văn bản cùng tên và văn bản đó không có ngày | Chọn văn bản đó, gắn cờ `act_dates_missing` | `only_act_with_name` → low |

Hai quy tắc cứng:
- **Không bao giờ chọn văn bản bắt đầu có hiệu lực sau ngày xét xử.** Lỗi đã gặp trên dữ liệu thật: Pháp lệnh 09/2014 không có trong kho, nên bản án năm 2018 suýt bị gắn vào Pháp lệnh 03/2022.
- Ngày xét xử lấy từ metadata `issued_date`. Nếu giá trị không hợp lệ thì đọc từ header ("Ngày: 15 – 5 – 2019", "ngày 15 tháng 5 năm 2019").

Sau khi chọn được văn bản, code lấy unit cấp Điều của văn bản đó (`article_units`). Nếu có nhiều unit cùng số Điều, code ưu tiên unit không bị đánh dấu là nằm trong một văn bản sửa đổi, và gắn cờ `repeated_article_in_act`.

**Bản tương đương** là những bản ghi cùng số hiệu và cùng Điều:
- Pháp điển (theo source note).
- Trang TVPL có số hiệu kèm hậu tố (ví dụ trang dịch "91/2015/QH13-THE-CIVIL-CODE"), chỉ nhận khi **text trùng khớp**.

### 3.4 Vai trò của căn cứ

| Vai trò | Quy tắc | Có dùng làm đáp án không |
|---|---|---|
| `court_fee` | NQ 326/2016, PL 10/2009 (khớp theo tiền tố số/năm), hoặc NQ/PL trong câu có "án phí" | Không |
| `enforcement` | Luật thi hành án dân sự/hình sự; hoặc câu có "thi hành án" kèm "lãi suất"/"chịu lãi" (lãi chậm thi hành án) | Không |
| `procedural` | BLTTDS, BLTTHS, Luật TTHC; Pháp lệnh trình tự thủ tục xem xét áp dụng biện pháp hành chính tại tòa | Không (lưu lại cho track thủ tục sau này) |
| `substantive` | Các căn cứ còn lại | **Có** |

Vụ không còn căn cứ nội dung nào resolve được bị loại với lý do `no_resolved_substantive_citation`. Các căn cứ nội dung không resolve được vẫn được lưu lại (`unresolved_substantive_citations`) và làm confidence của query bị giới hạn ở mức `medium`.

## 4. Hai stage LLM

Prompt nằm ở [prompts.md](prompts.md). JSON Schema và validator nằm ở `contracts.py` và `pipeline.py`. Cả hai stage chỉ nhận **ID ngắn** (`c1`, `s12`, `c1.k2`) và chỉ trả ID. Model không bao giờ phải viết quote hay offset.

### 4.1 ISSUE (1 call mỗi vụ)

- **Input:**
  - `facts`: id + text.
  - `court_reasoning`: các câu nhận định/quyết định có trích căn cứ nội dung, kèm `citation_ids`.
  - `citations`: số Điều, tên Điều, văn bản, số hiệu, khoản tòa nêu.
  - `max_issues`.
- **Output:** `issues[{issue_id, citation_ids≥1, fact_ids≥1, issue_text, query_text}]` và `unused_citation_ids`.
- **Kiểm tra:**
  - Schema giới hạn ID trong danh sách enum và giới hạn độ dài chuỗi.
  - `issue_text`/`query_text` không được có số Điều, số hiệu, **tên văn bản** ("Bộ luật …", "Luật …", BLDS…) hay kết quả xử (`text.leakage` + `INSTRUMENT_RE`).
- **Khi bị loại:**
  - Issue nào vi phạm thì loại riêng issue đó (`issue_dropped:<id>:<lý do>`).
  - Nếu **không còn issue nào** dùng được, validator báo lỗi để response **không được cache**, rồi gọi lại đúng một lần với `revision_notes` (`issue_repair_attempted`). Vẫn hỏng thì vụ fail với mã `no_valid_issue`.

### 4.2 EVIDENCE (1 call mỗi issue)

- **Input:** issue, câu hỏi, các facts của issue, và từng Điều. Mỗi Điều được chia thành `clauses` có ID (từng khoản, hoặc `.all` là phần thân Điều đã bỏ dòng tiêu đề), kèm `court_cited_clauses`.
- **Output:** mỗi nguồn trả đúng một lần `relevance ∈ {core, supporting, not_relevant}`, `clause_ids`, `reason`.
- **Khi lỗi:**
  - Lỗi `context_budget` với nhiều nguồn: chia ra, mỗi Điều một call.
  - Lỗi mềm (`incomplete`, `schema`, `semantic_validation`, `context_budget`): nguồn đó vẫn giữ grade 2 (vì là căn cứ tòa áp dụng), evidence lấy từ khoản tòa nêu hoặc cả Điều, gắn cờ `evidence_model_error:<code>`, confidence `low`.

## 5. Dựng bản ghi và chấm độ tin cậy

- **Grade.** `core` → 2; `supporting` → 1. `not_relevant` → bỏ khỏi đáp án và ghi `court_citation_rejected_by_model:<cid>`. Trường hợp này thường là căn cứ thuộc issue khác, hoặc resolve nhầm.
- **Evidence.** Lấy theo các khoản đã chọn, tính offset trên `unit.text` từ offset của unit con. Với bản tương đương, code tìm lại cùng đoạn bằng so khớp bỏ qua khoảng trắng và dấu. Bản sao nào **không chứa đoạn đó** (thường do Pháp điển đã hợp nhất sửa đổi) thì không được nhận, và gắn cờ `copy_text_differs:<id>`.
- **Requirement group.** Mỗi Điều `core` là một nhóm, gồm bản chính và các bản tương đương. Hai Điều `core` cùng tên luật, khác phiên bản nhưng cùng tiêu đề Điều (ví dụ "Nghĩa vụ trả nợ của bên vay": Điều 474 BLDS 2005 và Điều 466 BLDS 2015) được gộp vào **một nhóm** với `cross_version=true`.
- **Confidence của từng nguồn.** Xuất phát từ `method` (§3.3); bị hạ xuống `medium` nếu tên văn bản được mượn theo ngữ cảnh; bị hạ xuống `low` nếu có bất kỳ cờ resolve hoặc evidence nào. **Confidence của query** là mức thấp nhất trong các nguồn `core`, và tối đa `medium` nếu vụ còn căn cứ nội dung chưa resolve.
- **Quality.** `silver` nếu có ít nhất một nguồn `core` và có facts; ngược lại là `needs_review` (`no_core_source`).
- **ID query** = hash(case_id, các unit đáp án). ID **không phụ thuộc `run_hash`**, nên review vẫn gắn đúng query khi code hoặc prompt thay đổi, miễn là nội dung không đổi.
- **Cờ cảnh báo** trong `review_reasons`:
  - `question_repeats_article_title:<cid>`: câu hỏi chứa nguyên tiêu đề Điều từ 4 từ trở lên, nên benchmark sẽ dễ một cách giả tạo.
  - `issue_dropped`, `issue_repair_attempted`.
  - `unresolved_substantive_citations_in_case`.

## 6. Xử lý lỗi và độ ổn định khi dùng LLM local

| Loại lỗi | Xử lý |
|---|---|
| Hạ tầng: `transport`, `http_*`, `tokenize`, `protocol`, `refusal` | **Dừng run.** Các vụ đang dở không bị đánh dấu `failed`; chạy lại đúng lệnh là tiếp tục |
| ISSUE vượt context hoặc output bị cắt | Vụ `failed` với đúng mã lỗi (`context_budget`, `incomplete`…). Chạy lại bằng `--retry-failed` sau khi tăng context hoặc đổi model |
| EVIDENCE lỗi mềm | Dùng fallback với confidence `low` (§4.2); vụ vẫn hoàn tất |
| Vụ không đủ điều kiện | `skipped` kèm lý do; không gọi LLM, không chạy lại |

Các vụ chạy song song: số luồng bằng `RR_<PROFILE>_CONCURRENCY`, và semaphore của client giới hạn số request HTTP đồng thời. Mọi truy vấn DB thực hiện ở luồng chính.

Cache response theo request + schema, nên đổi `.env` hay code không làm mất cache. Response không qua validator thì không được cache. Mọi lỗi model đều ghi vào `logs/model_calls.jsonl`; output bị cắt hoặc sai schema được lưu ở `logs/invalid_outputs/`.

## 7. Số liệu đo trên dữ liệu thật

**Audit 1000 bản án** (lệnh `audit`, không gọi LLM, chạy trong vài giây):

| | Giá trị |
|---|---|
| Đủ điều kiện | 423 (42,3%) |
| Loại: không có phần facts | 545 |
| Loại: không có căn cứ nội dung resolve được | 31 |
| Căn cứ trích được | 7.656, trong đó **92,3% resolve được** (pipeline cũ: 24%) |
| Vai trò | nội dung 4.260 · thủ tục 2.025 · thi hành án 1.045 · án phí 326 |
| Chưa resolve | `act_not_in_corpus` 245 · `unrecognized_instrument` 140 · `no_instrument` 108 · `article_not_found` 98 |
| Vụ đủ điều kiện nhưng còn căn cứ nội dung chưa resolve | 116 (confidence bị giới hạn ở `medium`) |

**Smoke 80 bản án đầu tiên** (Qwen3.5-35B-A3B-FP8 qua vLLM, context 16k, concurrency 8):

| | Giá trị |
|---|---|
| Thời gian | **89 giây** |
| Kết quả theo vụ | 26 hoàn tất · 52 loại (48 không có facts, 4 không có căn cứ) · 2 `context_budget` (bản án quá dài so với context 16k) |
| Query | 57: 53 `silver`, 4 `needs_review`. Confidence: high 15 · medium 36 · low 6 |
| Đánh giá của EVIDENCE | `core` 71 · `supporting` 15; model bác 22 căn cứ (ghi lý do) |
| `question_repeats_article_title` | 14 query |

Kiểm tra tay 25 căn cứ resolve ngẫu nhiên cho thấy: phiên bản đúng giai đoạn (BLHS 1999 cho các vụ 2017–2018), số hiệu đúng, tên Điều khớp ngữ cảnh. Hai lỗi phát hiện trong lần kiểm tra này đã được sửa và có test: chọn văn bản ra đời sau ngày xét xử, và BLHS không ghi năm trong giai đoạn chuyển tiếp.
