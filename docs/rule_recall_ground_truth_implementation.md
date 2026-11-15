# Triển khai kỹ thuật pipeline ground truth rule recall

Ngày đối chiếu: 15/11/2026. Tài liệu này cụ thể hóa [thiết kế benchmark](rule_recall_dataset_benchmark_methodology.md) theo ba corpus đã tải, phương pháp annotation từ nghiên cứu liên quan và giao tiếp model theo chuẩn OpenAI-compatible. [Bộ prompt theo từng stage](rule_recall_annotation_prompts.md) đi kèm là đề xuất cần hiệu chỉnh trên pilot.

Đã có CLI triển khai tại [run_rule_recall_pipeline.py](../scripts/run_rule_recall_pipeline.py). Cấu hình thực tế dùng [.env.example](../.env.example), hướng dẫn chạy và phạm vi chức năng nằm trong [README](../README.md). Các ví dụ YAML bên dưới mô tả kiến trúc profile, không phải file input của CLI.

**Dữ liệu đủ để bắt đầu pilot và xây candidate/silver dataset.** Việc có đủ nguồn thô chưa chứng minh mọi citation của bản án đều nối được tới đúng phiên bản luật. Gold benchmark chỉ nhận những mẫu đã kiểm chứng facts, relevance, phiên bản và evidence theo guideline.

Phương án triển khai là **code và rule cho cấu trúc/kiểm tra; LLM cho đọc hiểu và đề xuất; người có chuyên môn cho gold**. VLM là nhánh tùy chọn để kiểm tra/phục hồi text từ ảnh trang khi cần. Qwen local hiện tại là profile khởi đầu; từng stage có thể chuyển sang model hoặc endpoint khác bằng cấu hình. Hai lượt gọi cùng model không được xem là hai chuyên gia độc lập.

Đây là một thiết kế kết hợp được lựa chọn từ các kết quả nghiên cứu phù hợp, chưa phải pipeline hoàn chỉnh đã được paper kiểm định trên chính corpus này. Các ngưỡng, số lượt gọi, few-shot examples và model được chốt bằng pilot có nhãn chuyên gia, thay vì suy ra một phương án “tối ưu nhất” từ tên phương pháp.

## 1 Kết quả kiểm tra các nguồn local

Số liệu được đo từ toàn bộ file `documents`, không lấy số tổng trên giao diện Hugging Face. [Script audit](../scripts/audit_legal_sources.py) và [kết quả JSON](rule_recall_legal_sources_audit.json).

| Chỉ số | TVPL | Pháp điển |
|---|---:|---:|
| Số shard documents | 96 | 7 |
| Số dòng | 573.851 | 65.967 |
| Có nội dung theo metadata độ dài | 572.914 | 65.538 |
| Trường số hiệu văn bản không rỗng | 185.828, tương đương 32,38% | Ghi trong source note, cần parse |
| Trường ngày hiệu lực không rỗng | 19.483, tương đương 3,40% | 65.959 note có cụm chỉ hiệu lực, chưa xác minh ngày |
| Trường tình trạng không rỗng | 19.962, tương đương 3,48% | Cần đối chiếu nguồn và lịch sử |

Pháp điển có 65.959 dòng có source note và liên kết VBPL; 65.322 note khớp mẫu số hiệu văn bản. Các mẫu regex này là đầu mối tạo crosswalk, chưa phải crosswalk đã được kiểm chứng. Dòng phân cách không có nội dung cần được loại khỏi đơn vị retrieval.

**Kết quả local khác với cách nối có thể suy ra từ README:** `record_id` của pháp điển duy nhất, nhưng `article_id` có 206 dòng vượt số khóa duy nhất; cặp `(subject_id, article_id)` vẫn có 141 dòng vượt số khóa duy nhất. Không nối bằng thứ tự dòng hoặc cặp khóa đó mà bỏ qua văn bản nguồn.

Ví dụ, mã pháp điển `Điều 2.2.TT.15.3` xuất hiện trong cùng một đề mục với hai note lần lượt dẫn Điều 3 của `30/2018/TT-BYT` và Điều 3 của `20/2020/TT-BYT`. Đây là các nguồn khác nhau trong dữ liệu; không tự xóa một dòng như bản sao chỉ vì trùng mã pháp điển.

Trong một văn bản TVPL đã kiểm tra, `doc_number` bị null nhưng dòng đầu toàn văn có `Số: 2099/QĐ-TTg`. Vì vậy, cần thử phục hồi metadata từ header trước khi đưa một văn bản vào hàng đợi “thiếu nguồn”.

Phạm vi audit là metadata và source note. Chưa đo độ chính xác nối bản án → nguồn luật, chưa xác minh lịch sử sửa đổi và chưa đánh giá toàn bộ chất lượng text.

## 2 Phân công giữa code LLM VLM và người kiểm chứng

| Bước | Xử lý bằng code và rule | Vai trò của LLM hoặc VLM | Đầu ra phải có |
|---|---|---|---|
| Chuẩn hóa kho luật | Đọc Parquet; chuẩn hóa Unicode; phục hồi số hiệu từ header; giữ nguồn và hash | Fallback cho tên/số hiệu bị phân mảnh khó | Bản ghi nguồn và trạng thái metadata |
| Tách Điều khoản điểm | Heading, state machine phân cấp, offset; phát hiện cấu trúc bất thường | Đề xuất ranh giới với đoạn khó, chọn từ các line ID đã cung cấp | Các legal unit có span gốc và parent |
| Nối pháp điển về luật | Parse source note, URL `ItemID`, số hiệu và Điều gốc; đối soát text | Gợi ý liên kết khi có nhiều nguồn ứng viên | Crosswalk có provenance và trạng thái mơ hồ |
| Phân đoạn bản án | Heading và quy tắc; gom vụ/trùng lặp trước split | Gán nhãn đoạn khó; chọn facts và tín hiệu issue bằng sentence ID | Section spans, facts spans, issue proposals |
| Phân giải citation | Hợp hai lớp parser; alias; mở rộng danh sách; kiểm tra Điều tồn tại | Chọn giữa candidate IDs đã truy hồi khi ngữ cảnh mơ hồ | Citation → nguồn/phiên bản hoặc unresolved |
| Tạo câu hỏi | Kiểm tra dữ kiện và leakage | Viết query và issue từ facts được phép | Query có evidence facts, không chứa đáp án |
| Đề xuất relevance và evidence | Tạo pool; kiểm tra ID và span | Đề xuất grade, vai trò và đoạn luật hỗ trợ | Nhãn candidate có căn cứ ngắn |
| Chốt gold | Kiểm tra schema, snapshot, phân cấp, split | Có thể nêu nghi vấn để người review | Người có chuyên môn sửa/duyệt/phân xử |
| Xuất và chấm benchmark | Qrels, evidence scoring, validation | Không cần gọi LLM cho metric ID/span | Artifact tái lập được |
| Phục hồi text khi cần | Xác định trang/crop, hash và vị trí nguồn | VLM chép/đối chiếu vùng khó đọc, đánh dấu không chắc | Text dẫn xuất có page provenance, qua review trước khi dùng |

Rule xác nhận được rằng một Điều có tồn tại và một quote nằm trong text. LLM hỗ trợ đánh giá quan hệ ngữ nghĩa. Cả hai chưa tự xác nhận rằng nguồn đó đủ và đúng cho vấn đề pháp lý của một vụ thực tế.

### 2.1 Cơ sở nghiên cứu cho từng lựa chọn

| Nghiên cứu và phần liên quan | Điều nghiên cứu hỗ trợ | Áp dụng vào corpus này và giới hạn |
|---|---|---|
| [DRiLL 2025, mục 2.2](https://aclanthology.org/2025.vlsp-1.16.pdf) | Annotation luật Việt Nam theo guideline, nhiều vòng với sinh viên luật và chuyên gia; xử lý bất đồng | Dùng làm cơ sở tổ chức guideline/review; không diễn giải thành bằng chứng rằng LLM có thể tự tạo gold |
| [A Reasoning-Focused Legal Retrieval Benchmark, mục 3 và phụ lục A](https://arxiv.org/abs/2505.03970) | Tìm quy tắc, nghiên cứu nguồn rồi chọn passage hỗ trợ theo quá trình làm việc pháp lý | Giữ query gắn nhu cầu tìm luật và nguồn có thể kiểm chứng. Đổi annotation từ bối cảnh Mỹ sang luật Việt Nam cần chuyên gia; các search steps trong paper không phải một pipeline LLM |
| [LegalBench-RAG](https://arxiv.org/abs/2408.10343) | Đánh giá truy hồi đoạn hỗ trợ cụ thể thay vì chỉ document ID | Lưu evidence span và ngân sách context. Phần giải quyết phiên bản/hiệu lực luật là yêu cầu bổ sung của dự án |
| [LegalSeg, Findings NAACL 2025](https://aclanthology.org/2025.findings-naacl.63/) | Phân loại vai trò câu trong bản án, khai thác ngữ cảnh và quan hệ tuần tự | Stage P1 đọc câu lân cận và giữ vai trò phát biểu. Taxonomy Việt Nam và cách dùng LLM chọn ID là điều chỉnh của dự án, không sao chép nguyên nhãn của bản án Ấn Độ |
| [InPars](https://arxiv.org/abs/2202.05144) và [Promptagator, mục 3](https://arxiv.org/abs/2209.11755) | Sinh query bằng few-shot cho huấn luyện retrieval; Promptagator có round-trip consistency filtering | Dùng để thiết kế nhánh silver train và ví dụ theo search intent. Không coi query sinh ngược từ Điều luật là query benchmark độc lập; không loại ca khó chỉ vì retriever không tìm lại được nguồn |
| [UMBRELA, phần relevance prompting](https://arxiv.org/abs/2406.06519) | LLM đánh giá query–passage theo rubric rõ ràng | Khởi đầu P4 bằng đánh giá từng nguồn và evidence. Thang core/supporting của dự án khác thang relevance gốc; cần kiểm định với chuyên gia |
| [Nghiên cứu UMBRELA tại TREC 2024, ICTIR 2025](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=959054) | Rankings tương quan tốt ở cấp run; tương quan theo topic thấp hơn | LLM judgments hữu ích để sàng lọc/đánh giá thăm dò. Kết quả đó không chứng minh từng nhãn nguồn luật Việt Nam, phiên bản hay evidence là đúng |
| [ALCE, EMNLP 2023](https://aclanthology.org/2023.emnlp-main.398/) | Tách việc citation hỗ trợ nội dung và độ đầy đủ hỗ trợ | P4 kiểm tra đoạn hỗ trợ, P5 kiểm tra tập nguồn/bằng chứng. Bộ rubric cụ thể cho LR02 là đề xuất, không phải evaluator ALCE áp dụng nguyên trạng |
| [Modeling Legal Reasoning, EMNLP 2023](https://aclanthology.org/2023.emnlp-main.575/) | Annotation pháp lý phức tạp có khó khăn cả về agreement và hiệu quả prompting của các model được nghiên cứu | Cần đo lỗi theo tiêu chí và giữ phân xử. Không suy ra chất lượng Qwen hiện tại từ kết quả của model/task khác |
| [OmniDocBench, CVPR 2025](https://arxiv.org/abs/2412.07626) | Đánh giá document parsing ở nhiều thành phần và loại tài liệu, gồm pipeline/VLM | Nhánh ảnh kiểm tra text, cấu trúc, thứ tự đọc và metadata quan trọng riêng. Chưa chứng minh VLM đủ chính xác cho PDF pháp lý Việt Nam |

Các paper trực tiếp về legal retrieval xác định mục tiêu nhãn và cách kiểm chứng; các paper về synthetic IR hoặc LLM judge cung cấp kỹ thuật hỗ trợ. Không có cơ sở để gọi toàn bộ nhãn do generator/verifier tạo ra là gold chỉ vì từng thành phần có paper tham khảo.

### 2.2 Luồng annotation và giới hạn thông tin

```text
Nguồn luật → legal units và assertions về phiên bản ────────────────┐
Bản án → P1 facts có nguồn → chia family/split → P2 issue/query ────┤
Citation trong bản án → P3 candidate nguồn ────────────────────────┤
Query → retrieval và tìm nguồn độc lập ────────────────────────────┘
       → P4 đánh giá từng nguồn → P5 kiểm tra tập nguồn và tìm bổ sung
       → P6 kiểm chứng độc lập và đối chiếu → chuyên gia → gold

P0V ảnh trang → text được kiểm tra → quay lại các bước xử lý text khi cần
```

Không phải mọi mẫu đều cần mọi lượt gọi LLM. P1/P3 có thể dùng rule với các trường hợp rõ; P0V chỉ dùng khi text/nguồn ảnh cần đối chiếu; số lượt verifier trên silver được quyết định từ pilot. Gold phải thỏa protocol review dù một stage đã được code giải quyết.

P2 chỉ thấy facts được phép và task scope. P3 có thể thấy citation/nhận định để xác định reference nhưng không chuyển những nội dung đó sang query. P4 độc lập nhận query và nguồn, không nhận nhãn trước hay thứ hạng retriever; P5 sau đó mới xem cả tập để phát hiện nguồn thiếu và alternative. Cách tách thông tin này là lựa chọn kiểm soát leakage và anchoring của dự án.

### 2.3 Hai nhánh dữ liệu phải tách biệt

**Gold từ bản án:** issue/query xuất phát từ facts có nguồn; citation là một đầu mối tìm luật; người annotation có thể bổ sung nguồn khác. Không thay facts chỉ để phù hợp gold đang có.

**Silver sinh ngược từ luật:** model đọc provision rồi sinh query/tình huống; có thể kiểm tra tìm lại nguồn theo hướng Promptagator. Nhánh này cần `query_origin`, generator profile và trạng thái synthetic, chỉ sinh trên phạm vi train. Việc passage nguồn được truy hồi tốt không chứng minh tính đúng pháp lý hoặc độ tự nhiên của tình huống.

Giữ một tập query do người viết từ facts, không được chọn chỉ theo agreement với LLM, để đối chiếu độ lệch của dữ liệu sinh tự động.

## 3 Kho dữ liệu trung gian

Triển khai hiện dùng Parquet làm input, JSONL làm output/review, SQLite cho checkpoint/lookup và FTS5 cho chỉ mục lexical. Các dependency đọc Parquet, HTTP, dotenv, schema validation và test đã được khai báo trong `requirements.txt`; `requirements.lock.txt` ghi môi trường kiểm tra.

Các bảng cần xây:

```text
source_records       mỗi dòng nguồn thô, kèm hash và đường dẫn shard/dòng
legal_acts           định danh văn bản đã chuẩn hóa cùng alias
legal_units          văn bản nguồn → Điều → khoản → điểm, kèm offsets
source_crosswalk     TVPL / VBPL / pháp điển và căn cứ nối
version_assertions   thông tin hiệu lực/sửa đổi cùng nguồn chứng minh
case_families        bản án, liên kết giữa các cấp, split
queries              facts và issue được phép cho model thấy
candidate_judgments  đề xuất nhãn do code/LLM tạo
human_judgments      sửa đổi, người review, bất đồng và quyết định cuối
```

Tách `source_record_id` của một bản tải khỏi `provision_version_id` của quy phạm đã kiểm chứng. Không coi nội dung đang hiển thị ở một trang là chắc chắn bản gốc tại ngày ban hành. Khi phiên bản chưa rõ, dùng `version_status=unverified`; chỉ tạo liên kết gold tới phiên bản sau khi đối chiếu nguồn.

Giữ offset trên text chuẩn hóa bất biến. Mỗi thao tác sửa text phải sinh version/hash mới hoặc có ánh xạ về bản cũ. ID gắn nhãn không phụ thuộc vào thứ tự hàng trong batch.

## 4 Cách xử lý từng bước

### 4.1 Chuẩn hóa luật bằng code trước

Đọc lần lượt các shard, chỉ lấy cột cần thiết. Parse số hiệu từ metadata rồi từ header; số hiệu trong phần “căn cứ” không được gán nhầm thành số hiệu của văn bản đang đọc. Ngày/loại/cơ quan cần có field provenance và trạng thái thiếu hoặc xung đột.

Tách heading Điều bằng vị trí đầu dòng, dấu phân cách và ngữ cảnh cấu trúc, sau đó phân khoản/điểm bằng state machine. Không dùng `split("Điều")`: từ này xuất hiện trong dẫn chiếu, tiêu đề và các đoạn sửa đổi trích dẫn bên trong một Điều khác.

Giữ nguyên văn bản ban hành kèm theo, phụ lục và các đoạn thay thế/sửa đổi như cấu trúc lồng nhau. Trường hợp parser không chắc cần `needs_structure_review`, không tự xem mọi Điều lồng là Điều cấp cao của văn bản hiện tại.

Parse nguồn pháp điển để xác định Điều trong văn bản gốc, rồi tìm nguồn TVPL bằng số hiệu và alias. Chỉ gộp hai bản thể hiện khi nội dung, phạm vi và phiên bản đã được đối chiếu. Tên luật gần nhau hoặc cosine similarity cao chỉ giúp sinh candidate.

### 4.2 Phân đoạn bản án và chọn facts

Code đánh số câu/đoạn và cung cấp cho LLM các `sentence_id` cùng text và ngữ cảnh lân cận. Với đoạn khó, yêu cầu model trả ID của facts, yêu cầu các bên, ý kiến, nhận định và kết luận theo [prompt P1](rule_recall_annotation_prompts.md#2-p1-phân-đoạn-và-chọn-facts). Code lấy nguyên văn từ các ID được chọn; không để model tự viết lại facts ở bước extraction. Giữ nguồn phát biểu và trạng thái tranh chấp để lời một bên không biến thành sự kiện đã xác nhận.

Tách metadata định danh vụ án và phần có thể lộ kết quả khỏi payload của query generator. Nếu chia văn bản dài thành nhiều đoạn, giữ overlap và nối các phần trước khi chốt facts; không bỏ cuối văn bản hoặc thay facts bằng một summary không có nguồn.

Case family được gom và chia split trước khi sinh query. LLM có thể đề xuất liên kết vụ án khó, nhưng việc chia split không dựa vào một phán đoán duy nhất chưa kiểm tra.

### 4.3 Phân giải citation theo mức chắc chắn

Code ưu tiên số hiệu đầy đủ, alias rõ và cấu trúc Điều/khoản/điểm tồn tại. Ví dụ “Bộ luật dân sự” chưa đủ để chọn duy nhất một phiên bản; resolver cần ngữ cảnh và nguồn thực tế.

Với citation mơ hồ, chuyển cho LLM một gói nhỏ gồm citation thô, đoạn chứa citation và danh sách nguồn ứng viên có nội dung/metadata theo [prompt P3](rule_recall_annotation_prompts.md#4-p3-phân-giải-citation). Model chỉ được chọn candidate ID có trong gói, hoặc trả trạng thái mơ hồ/không tìm thấy trong candidates; không được tự sinh ID mới. Không tìm thấy trong candidates chưa chứng minh toàn corpus thiếu nguồn.

Một liên kết vượt qua kiểm tra cấu trúc được ghi `candidate_resolved`, chưa phải `gold_resolved`. Việc chọn năm dựa riêng vào năm xét xử không được tự động chấp nhận.

### 4.4 Sinh query từ facts

Query generator chỉ nhận facts đã duyệt sơ bộ và giới hạn nhiệm vụ; không nhận phần nhận định, gold citation hay phán quyết mục tiêu. Yêu cầu đầu ra: issue, câu hỏi, các fact sentence IDs được dùng và dữ kiện còn thiếu.

Sử dụng [prompt P2](rule_recall_annotation_prompts.md#3-p2-tạo-issue-và-query-từ-facts), với nguyên tắc cốt lõi:

```text
Chỉ dùng facts được cung cấp. Tạo một vấn đề pháp lý cụ thể và câu hỏi
tìm nguồn luật cho vấn đề đó. Không kết luận kết quả vụ án, không thêm
tình tiết, không nêu số Điều hoặc số hiệu nguồn đích. Nếu thiếu dữ kiện
quan trọng thì ghi rõ. Mỗi issue phải dẫn các fact sentence IDs hỗ trợ.
```

Giai đoạn đầu tạo một query chính cho mỗi issue. Few-shot lấy từ ví dụ phát triển do người duyệt và giữ cùng search intent; thử instruction-only và few-shot để đo lợi ích. Paraphrase và augmentation chỉ sinh sau khi đã ổn định nhãn và split. Không lấy mẫu đã dùng để sửa prompt làm test độc lập.

### 4.5 Tạo pool và đề xuất relevance

Tạo pool ban đầu từ citation đã phân giải, BM25 theo query, các Điều liên quan qua dẫn chiếu và tìm kiếm của reviewer. Thử ngân sách ban đầu khoảng 20–40 Điều/issue trên pilot; mở rộng theo nhu cầu chứ không coi đây là giới hạn của gold.

Dense retrieval có thể được bổ sung sau khi có bộ legal units và chọn embedding model phù hợp. Chỉ mục từ title/summary sẵn có không thay thế embedding của đoạn luật. Nếu chưa có dense, ghi rõ pool pilot còn hạn chế, cho reviewer bổ sung nguồn và đo lại khi mở rộng.

Ở [P4](rule_recall_annotation_prompts.md#5-p4-đánh-giá-từng-nguồn-và-trích-bằng-chứng), LLM đánh giá từng candidate theo rubric, đề xuất `core_candidate`, `supporting`, `not_relevant` hoặc `uncertain`, kèm căn cứ ngắn và evidence quote lấy từ nguồn. Tách trạng thái relevance khỏi mức xác minh phiên bản; không coi hai nguồn chung chủ đề là cùng giá trị pháp lý. Không yêu cầu một chuỗi CoT dài.

Code kiểm tra quote có khớp trong nguồn hay không rồi tính offset. Nếu quote khớp nhiều vị trí, giữ danh sách vị trí ứng viên cho review; không tự chọn lần xuất hiện đầu. Một quote khớp text vẫn có thể không đủ nghĩa hoặc không liên quan.

Đánh giá ứng viên theo batch nhỏ, ẩn tên hệ thống/thứ hạng sinh candidate ở vòng độc lập. Sau đó [P5](rule_recall_annotation_prompts.md#6-p5-kiểm-tra-tập-nguồn-và-phát-hiện-chỗ-thiếu) xem tập nguồn để đề xuất nhóm bắt buộc, nguồn tương đương và các chỗ cần tìm bổ sung. Tính “cần và đủ” không luôn quyết định được theo từng passage riêng; `core_candidate` chỉ trở thành nhãn core/requirement group sau kiểm tra tập nguồn và review. Mọi nguồn chưa được xét là `unjudged`, không tự thành negative.

[P6](rule_recall_annotation_prompts.md#7-p6-kiểm-chứng-query-và-đề-xuất-nhãn) kiểm tra facts/query/source một lượt không thấy nhãn cũ, rồi mới đối chiếu khác biệt. Áp dụng thêm verifier khi pilot cho thấy giảm lỗi hoặc công sửa; không mặc định thêm nhiều model calls sẽ nâng chất lượng.

### 4.6 Review và xuất dataset

Gói review hiển thị facts, issue, nguồn ứng viên, phiên bản, bằng chứng, citation gốc và lý do đề xuất. Reviewer có thể sửa query, bỏ/thêm nguồn, đổi phiên bản, chọn đoạn khác hoặc đánh dấu thiếu dữ kiện. Ẩn nguồn sinh candidate và nhãn đề xuất ở vòng độc lập khi thiết kế annotation cho phép.

Chốt gold pilot/test bằng hai lượt annotation pháp lý độc lập và phân xử; nếu chỉ có một người kiểm chứng, ghi đúng mức kiểm chứng và chưa mô tả dữ liệu là đã qua hai annotator. Bố trí ít nhất một vòng đánh giá không thấy nhãn/giải thích của LLM trước khi đưa đề xuất vào gói phân xử. Một nhánh chuyên gia tìm nguồn từ query trước khi đọc citation tòa giúp kiểm tra bỏ sót; đây là phần điều chỉnh protocol của dự án.

Sau review, xuất `queries`, `qrels`, `evidence` và corpus manifest. Bronze/silver giữ cả nhãn đề xuất; gold giữ nhãn được duyệt cùng lịch sử sửa. Không trộn hai loại trong evaluator.

## 5 Hợp đồng input output cho LLM

Ví dụ dưới đây minh họa hợp đồng dữ liệu, chưa phải gold của một vụ án:

```json
{
  "input": {
    "citation_text": "Điều X của Bộ luật A",
    "context_sentence_ids": ["s21", "s22"],
    "candidate_ids": ["candidate_1", "candidate_2"]
  },
  "output": {
    "selected_candidate_id": "candidate_1",
    "resolution_status": "candidate_resolved",
    "evidence_sentence_ids": ["s22"],
    "needs_human_review": true
  }
}
```

Trong request thực tế, gói input phải chứa cả text của các câu và nội dung/metadata của candidate. ID chỉ là khóa tham chiếu, không phải toàn bộ thông tin model cần đọc.

JSON Schema chặn field thừa và ràng buộc kiểu/enum. Validator local kiểm tra thêm ID có trong input, spans hợp lệ, quote khớp text, cấu trúc pháp lý và query không chứa dấu hiệu đáp án. `confidence` do model tự sinh chỉ dùng như một tín hiệu phụ, không làm điều kiện tự động gán gold.

Nếu HTTP lỗi hoặc output sai schema: retry có giới hạn, ghi failure queue. Nếu facts/phiên bản/relevance còn mơ hồ: chuyển review, không lặp prompt tới khi model chịu trả một đáp án chắc chắn.

## 6 Giao tiếp model theo chuẩn OpenAI compatible

### 6.1 Hợp đồng chung và model profile

Giao diện chuẩn ban đầu là **Chat Completions**: `POST {base_url}/chat/completions`, với `model` và `messages`. `base_url` là API root đã chuẩn hóa, thường có `/v1`; không nối thêm `/v1` lần nữa. Chọn giao diện này để dùng chung cho vLLM local, server khác và OpenAI API; không trộn request schema của Responses API vào cùng adapter. [Chat Completions API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

Pipeline gọi **vai trò**, vai trò trỏ tới **model profile**, profile chứa kết nối và capabilities. Đổi server/model/key không yêu cầu sửa prompt nghiệp vụ, nhưng phải chạy kiểm tra capability và hiệu chỉnh chất lượng cho profile mới.

| Nhóm cấu hình | Nội dung cần biểu diễn |
|---|---|
| Kết nối | `base_url`, tên `model`, timeout, retry, concurrency và giới hạn request/token |
| Xác thực | `auth.mode=bearer` + tên biến môi trường chứa key, hoặc `auth.mode=none` cho server không yêu cầu key |
| Modalities | Text; image khi đã kiểm tra; tool calling là capability riêng, không bắt buộc cho batch annotation |
| Output | `json_schema`, `json_object` hoặc JSON theo prompt có validator; chế độ dùng thật phải được ghi lại |
| Giới hạn | Context window, output budget và tên tham số token limit phù hợp endpoint/model |
| Sampling và reasoning | Chỉ gửi tham số được profile hỗ trợ; tham số riêng nhà cung cấp nằm trong profile |
| Quy tắc chuyển profile | Stage binding, profile được phép fallback và cách ghi failure/provenance |

Key **optional theo yêu cầu của endpoint**, không phải thử bỏ auth khi gặp lỗi. Với `bearer`, thiếu key phải báo cấu hình lỗi; với `none`, không gửi Authorization header. Một SDK đòi giá trị key dù server không cần phải được adapter xử lý theo profile; không xem đó là bằng chứng server cần xác thực.

Các vai trò cấu hình độc lập: `extractor` cho P1, `query_generator` cho P2, `citation_linker` cho P3, `relevance_assessor` cho P4/P5, `verifier` cho P6 và `vision_reader` cho P0V. Tất cả vai trò text có thể dùng cùng profile lúc đầu. `vision_reader` có thể chưa cấu hình; chỉ kích hoạt nhánh ảnh khi đã có profile đủ khả năng.

### 6.2 Ví dụ đặc tả cấu hình

YAML dưới đây chỉ minh họa cấu trúc dự kiến, chưa phải file cấu hình hay code đã được triển khai:

```yaml
default_text_profile: local_qwen
model_profiles:
  local_qwen:
    base_url: http://127.0.0.1:8010/v1
    model: qwen3.5-35b-a3b
    auth:
      mode: bearer
      api_key_env: LEGAL_LOCAL_LLM_API_KEY
    capabilities:
      enabled_input_modalities: [text]
      structured_output_mode: json_schema
      tool_calling: unverified
    limits:
      context_window_tokens: 32768
      max_output_tokens: 2048
      token_limit_parameter: max_tokens
      max_concurrency: 1
    generation:
      temperature: 0
    provider_extra_body:
      chat_template_kwargs:
        enable_thinking: false

  remote_model:
    base_url: ${REMOTE_LLM_BASE_URL}
    model: ${REMOTE_LLM_MODEL}
    auth:
      mode: bearer
      api_key_env: REMOTE_LLM_API_KEY
    capability_status: needs_preflight

  openai_model:
    base_url: https://api.openai.com/v1
    model: ${OPENAI_MODEL}
    auth:
      mode: bearer
      api_key_env: OPENAI_API_KEY
    capability_status: needs_preflight

stage_profiles:
  extractor: local_qwen
  query_generator: local_qwen
  citation_linker: local_qwen
  relevance_assessor: local_qwen
  verifier: local_qwen
  vision_reader: null
```

Các profile remote/OpenAI chưa ghi đủ capabilities/limits; phải hoàn thiện theo model thực tế rồi preflight. Nếu server remote không cần key, khai báo rõ `auth.mode=none` và bỏ `api_key_env`. Khi thêm VLM, bật image trong profile đã kiểm tra và gán `vision_reader`; không suy ra khả năng nhìn ảnh từ tên model.

`provider_extra_body` là cấu hình để adapter ghép các field riêng vào request của đúng profile. Không gửi nguyên một wrapper có tên đó ra API. Tham số Qwen/vLLM như `chat_template_kwargs.enable_thinking` không được tự chuyển sang OpenAI hoặc server khác. [Tài liệu vLLM](https://docs.vllm.ai/en/stable/features/structured_outputs/), [model card Qwen](https://huggingface.co/Qwen/Qwen3.5-35B-A3B/blob/main/README.md).

### 6.3 Capability và khác biệt giữa các endpoint

“OpenAI-compatible” không bảo đảm mọi optional parameter giống nhau. Profile cần định nghĩa hoặc kiểm tra:

- **Output limit:** có endpoint dùng `max_tokens`, có model/API dùng `max_completion_tokens`; adapter ánh xạ budget nội bộ sang đúng field, không gửi cả hai. [API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
- **Schema:** ưu tiên JSON Schema khi hỗ trợ; JSON mode chỉ bảo đảm JSON hợp lệ, không bảo đảm đúng schema. Fallback JSON theo prompt phải qua validator và được ghi trong manifest. Không âm thầm hạ chế độ sau lỗi request. [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
- **Reasoning và sampling:** `temperature`, `top_p`, `seed`, `reasoning_effort` và switch thinking chỉ gửi khi profile cho phép. Không hardcode `temperature=0` cho mọi model. Không coi seed/temperature thấp là bảo đảm output hoàn toàn lặp lại.
- **Ảnh:** khi hỗ trợ Chat Completions vision, dùng content parts có text và `image_url`; URL/base64 phải phù hợp endpoint. Quyền đọc ảnh không chứng minh ảnh + schema cùng hoạt động. Không giả định mọi server nhận trực tiếp PDF. [Image input trong Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

Schema nghiệp vụ là chung; adapter có thể diễn đạt schema trong tập tính năng mà provider hỗ trợ nhưng validator local vẫn kiểm tra hợp đồng chuẩn. Thiếu field, refusal, output bị cắt, reasoning chưa kết thúc hoặc lỗi HTTP là trạng thái vận hành riêng, không phải nhãn `not_relevant` hay `uncertain` pháp lý.

Preflight cho profile mới gồm request text nhỏ, schema các stage cần dùng, giới hạn context/output, auth đúng chế độ và ảnh nhỏ nếu bật vision. `/models` hoặc `/health` có thể không được provider cung cấp; không lấy sự vắng mặt hai route đó làm kết luận server không hỗ trợ Chat Completions. Kiểm tra chỉ với nội dung giả định trước khi đưa vào batch.

Không tự chuyển sang endpoint khác khi bị lỗi. Nếu cần fallback, danh sách profile được phép phải được cấu hình rõ; mỗi mẫu ghi profile thực tế, prompt/schema version, cấu hình được gửi và mọi lần thử. Không trộn output của nhiều model mà mất provenance.

### 6.4 Profile Qwen đã kiểm tra và ngân sách ban đầu

Ở lần kiểm tra trước, endpoint local `http://127.0.0.1:8010/v1` trả served model `qwen3.5-35b-a3b`, context limit 32.768; một request giả định text + JSON Schema trả đúng cấu trúc và loại được câu phán quyết. [Kết quả smoke test](rule_recall_llm_smoke_test.json). Chưa kiểm tra vision, tool calling, workload dài hoặc chất lượng annotation pháp lý; các capability đó chưa được bật chỉ từ tên model.

Giữ concurrency 1 để đo pilot rồi thử 2 trong giới hạn server hiện tại. Với profile này có thể bắt đầu input khoảng 16k–24k token, output 1k–4k, tổng không quá giới hạn đã kiểm tra. Đây là budget riêng profile/stage; model khác có context và tokenizer khác phải khai báo lại. Nhánh VLM cần tính cả ngân sách ảnh theo endpoint.

Không đổi Docker trong công việc tài liệu này. Việc thay profile chỉ làm thay backend xử lý, không được âm thầm đổi mục tiêu task, taxonomy relevance hay quy tắc nhận gold.

## 7 Kiểm soát bias và hiệu chỉnh model prompt

Một model có thể chạy nhiều vai trò, nhưng các lỗi vẫn có thể tương quan. Hai model khác nhau cũng chưa bảo đảm độc lập nếu cùng dùng nguồn thiếu hoặc cùng rubric sai. Khi dùng model tạo dữ liệu làm baseline về sau, cần kiểm tra độ lệch về cách hỏi, lĩnh vực và dạng nguồn.

Giữ query do người viết, nguồn do chuyên gia tìm độc lập và một vòng annotation không thấy nhãn LLM. Chấm retrieval bằng ID/span đã duyệt thay vì gọi lại generator làm trọng tài cuối. Không cần thêm model để bắt đầu; một verifier mạnh hơn ở endpoint khác là phương án cần đo, không phải điều kiện tự động tạo gold.

### 7.1 Chọn phương án bằng tập phát triển

So sánh các biến thể trên cùng case families đã kiểm chứng, giữ nguyên corpus và ngân sách:

| Câu hỏi cần kiểm tra | Phương án so sánh | Thước đo quyết định |
|---|---|---|
| LLM giúp phân đoạn đến đâu? | Rule baseline và rule + LLM cho vùng khó | Facts bị thêm/bỏ, lời các bên bị đổi thành sự thật, leakage |
| Few-shot có ích không? | Instruction + schema và thêm ví dụ đã duyệt | Độ trung thành query, issue được chấp nhận, thời gian sửa |
| Verifier có đáng chi phí? | Generator + validator và thêm verifier độc lập | Lỗi còn sót, mẫu bị loại nhầm, công review/sample |
| Cần model mạnh hơn ở stage nào? | Một profile cho tất cả và đổi riêng linker/assessor/verifier | Đúng nguồn/phiên bản, relevance, evidence và chi phí |
| VLM có sửa được lỗi text? | Text extraction hiện tại và nhánh ảnh được review | Lỗi số hiệu/ngày/phủ định, thứ tự đọc và công kiểm chứng |
| Synthetic train có lệch không? | Query từ facts, query người viết và query sinh ngược từ luật | Phân phối câu hỏi, độ khó lexical và khả năng chuyển miền |

Đo riêng tỷ lệ JSON hợp lệ và độ đúng pháp lý. Ghi khoảng tin cậy khi quy mô cho phép; không đặt ngưỡng “confidence 0,9 là gold” khi chưa hiệu chuẩn. Xác suất model tự khai không thay thế tỷ lệ lỗi thực đo.

### 7.2 Quy tắc khóa phiên bản

Chốt guideline, prompt, few-shot examples, schema, corpus snapshot và profile trước xử lý test. Đổi model, endpoint phục vụ model cập nhật hoặc chế độ output cần run version mới và đánh giá lại trên dev; không chọn cấu hình theo điểm test.

Manifest giữ tên model/revision khi có, profile ID, endpoint identifier không chứa credential, hash request/input, cấu hình thực gửi, output/refusal/truncation, token usage và chi phí/thời gian đo được. Cache bao gồm profile/model/prompt/schema/corpus version; không tái dùng cache model cũ như thể đã chạy model mới.

## 8 Những gì cần chuẩn bị

| Hạng mục | Trạng thái và việc cần làm |
|---|---|
| Corpus bản án và hai corpus luật | Đã có, đã audit cấu trúc/metadata; chưa cần tải thêm corpus lớn |
| Profile LLM text mặc định | Qwen local đã qua smoke test; profile khác cấu hình base URL/model/auth và preflight riêng |
| Profile VLM tùy chọn | Chỉ cần khi xử lý ảnh; chưa có kiểm tra vision của endpoint local |
| Nhân lực pháp lý | Cần xác nhận người xây guideline, kiểm chứng nhãn và phân xử |
| Phạm vi đầu tiên | Đề xuất 30–50 vụ dân sự hợp đồng/nghĩa vụ làm tập phát triển; chưa gọi là test độc lập |
| Guideline và prompt | Duyệt [P1–P6 và P0V](rule_recall_annotation_prompts.md), core/supporting, phiên bản và alternatives trên dev |
| Nguồn chính thức còn thiếu | Bổ sung có mục tiêu khi pilot gặp thiếu văn bản/phiên bản, không tải đại trà trước |
| Lưu trữ trung gian | Giữ raw bất biến; có chỗ cho bảng units, index, cache, review và manifest |
| Embedding model | Có thể chọn sau vòng pilot lexical đầu; không chặn chuẩn hóa và citation resolution |
| Công cụ review | Bắt đầu bằng JSONL/CSV và nguồn mở kèm; giao diện annotation bổ sung khi khối lượng tăng |

Mỗi stage cần log số mẫu vào/ra, lý do loại, trạng thái ambiguity, prompt/schema version, token usage và thời gian thực đo. Trước chạy lớn, đo tỷ lệ người phải sửa facts, mapping, relevance và evidence trên pilot; dùng số đó để lập ngân sách annotation.

## 9 Thứ tự triển khai

1. Chuẩn hóa luật, crosswalk và chỉ mục lexical; lấy một mẫu citation có người kiểm tra để đo resolver.
2. Làm 30–50 vụ phát triển qua toàn bộ pipeline, tới gói review; so sánh prompt/profile theo mục 7, dùng Qwen local làm cấu hình khởi đầu.
3. Sửa guideline/parser/prompt, rồi mở rộng pilot theo phương án 200 case families trong tài liệu phương pháp.
4. Khi gold nguồn luật ổn định, xuất RR1/RR2 và chạy baseline.
5. Dùng lại queries/qrels/evidence để triển khai tools và benchmark agent RR3.

Ground truth của RR3 là nguồn và evidence đã duyệt. Tool trajectory là log của lần chạy; chỉ cần tạo trajectory huấn luyện nếu làm SFT về sau, không phải điều kiện để có benchmark retrieval.

Hiện đã triển khai CLI ingest/index, annotation P1–P6, review/import, xuất dataset, VLM proposals, synthetic train và evaluator cơ bản. Kiểm tra phần mềm gồm fixtures/API mô phỏng và smoke test với dữ liệu thật trên Qwen local; xem [hướng dẫn chạy](../README.md). Đây chưa phải gold dataset đã được chuyên gia gán nhãn hoặc kết quả benchmark pháp lý.

Các phần cần tiếp tục kiểm định trên pilot: chất lượng parser và facts, độ phủ nối citation/phiên bản, family heuristic, rubric/prompt, nguồn bổ sung và ngân sách. Dense retrieval/reranker, lịch sử sửa đổi pháp lý hoàn chỉnh, controlled withholding và agent runner chưa được tự động triển khai trong CLI này; không suy các khả năng đó từ việc pipeline chạy thành công.
