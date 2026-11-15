# Prompt annotation cho ground truth rule recall

Các prompt dưới đây là **bản đề xuất để hiệu chỉnh trên pilot**, dùng với hợp đồng dữ liệu trong [kế hoạch triển khai](rule_recall_ground_truth_implementation.md). Chúng được viết cho corpus bản án và nguồn luật Việt Nam của dự án; không phải nguyên văn prompt từ paper và chưa được xác nhận hiệu quả trên corpus này.

CLI hiện đọc trực tiếp các block `text` chung và theo stage trong tài liệu này qua `RR_PROMPTS_FILE`. Sửa prompt làm đổi fingerprint/run; không chỉnh heading stage hoặc bỏ block khi đang dùng nó làm prompt bank. Schema và semantic validator nằm trong package `legal_recall`.

Các thiết kế tham khảo chính: phân đoạn có ngữ cảnh của [LegalSeg](https://aclanthology.org/2025.findings-naacl.63/), annotation theo nghiên cứu pháp lý của [Zheng và cộng sự](https://arxiv.org/abs/2505.03970), evidence cụ thể của [LegalBench-RAG](https://arxiv.org/abs/2408.10343), đánh giá relevance theo rubric của [UMBRELA](https://arxiv.org/abs/2406.06519) và sự phân biệt citation correctness/completeness trong [ALCE](https://aclanthology.org/2023.emnlp-main.398/). Bảng đối chiếu phương pháp và giới hạn áp dụng nằm trong tài liệu triển khai.

## 1 Quy tắc dùng chung

Mỗi stage có task instruction, rubric, input được phép và schema đầu ra riêng. Không nối toàn bộ lịch sử hội thoại từ stage trước vào stage sau. Prompt/schema có version; profile model và nội dung input được lưu trong run manifest.

Đoạn hướng dẫn chung để đưa vào role instruction mà endpoint hỗ trợ:

```text
Bạn hỗ trợ annotation dữ liệu pháp luật Việt Nam theo nhiệm vụ được giao.
Chỉ sử dụng dữ liệu và các nguồn được cung cấp trong request này.
Không dùng trí nhớ về pháp luật để bổ sung số hiệu, phiên bản, ngày hoặc nội dung còn thiếu.
Giữ riêng lời trình bày của đương sự, quan điểm cơ quan tố tụng và nhận định của tòa.
Chỉ trả các ID có trong input. Không tự tạo ID nguồn hoặc offset ký tự.
Nếu dữ liệu không đủ hoặc có nhiều cách hiểu hợp lý, trả trạng thái chưa xác định
và nêu dữ kiện cần bổ sung. Không đoán để hoàn thành schema.
Trả JSON theo schema của stage; giải thích ngắn và gắn với bằng chứng nếu được yêu cầu.
Nhiệm vụ là chuẩn bị nhãn, không phải đưa ra phán quyết hoặc tư vấn kết quả vụ án.
```

ID, quote và ngày có nguồn được validator kiểm tra lại. Prompt không bảo đảm model tuân thủ các yêu cầu đó. Không yêu cầu model trình bày chuỗi suy nghĩ nội bộ dài; cần căn cứ có thể đối chiếu vào input.

Few-shot examples lấy từ tập phát triển đã được người kiểm chứng, cùng phiên bản guideline. Chọn ví dụ bao gồm ca đúng rõ, ca có citation trong lời một bên, ca thiếu ngày và ca mơ hồ phiên bản. Số ví dụ chọn theo kết quả pilot và ngân sách context; không mặc định rằng một con số từ paper sẽ tối ưu cho dự án.

## 2 P1 Phân đoạn và chọn facts

**Input:** bản án đã đánh số câu/đoạn, một phần ngữ cảnh trước/sau, heading nếu có, cấp xét xử và thời điểm câu hỏi mục tiêu. Cho phép đọc nhận định để phân loại vùng văn bản, nhưng không chuyển nội dung đó thành facts trước phán quyết.

**Output:** `sentence_roles`, `usable_fact_ids`, `context_needed`, `uncertainties`. Một câu có nhiều chức năng được đánh dấu `mixed` và đưa ra review hoặc tách thành span nhỏ hơn bằng bước xử lý có kiểm chứng.

```text
Gán vai trò cho từng câu trong phạm vi cần annotation, có xét các câu lân cận:
facts_reported, party_request, procedural_history, party_argument,
prosecution_position, court_reasoning, operative_decision, administration,
mixed hoặc uncertain. Đây là taxonomy của dự án.

Chọn các sentence IDs có thể dùng làm facts trước thời điểm hỏi đã chỉ định.
Không chọn kết luận của tòa ở cấp xét xử mục tiêu, câu áp dụng luật hay mệnh đề
chép quy phạm làm facts. Không chuyển một lời cáo buộc thành sự kiện được xác nhận.
Giữ nguồn phát biểu và trạng thái disputed/undisputed/unknown khi input cho phép.
Nếu lịch sử xét xử trước đó được phép biết, giữ nhãn procedural_history riêng.
Không bổ sung sự kiện và không viết lại toàn văn các câu đã chọn.
Đánh dấu phần thiếu ngữ cảnh, có dấu hiệu lộ đáp án hoặc không phân loại chắc chắn.
```

**Kiểm tra:** ID hợp lệ; các câu kết quả/nhận định không lọt sang query payload; phủ đủ facts về mốc thời gian và quan hệ tranh chấp. Nhãn facts vẫn cần đối chiếu thực tế, không chỉ kiểm tra regex.

## 3 P2 Tạo issue và query từ facts

**Input:** chỉ facts được phép dùng, yêu cầu các bên, procedural history phù hợp, timeline và task scope. Không cung cấp gold citation, số hiệu nguồn đích, nhận định/phán quyết mục tiêu hoặc source IDs cho phép tìm ngược bản án.

**Output:** `issues` gồm `issue_id`, `issue_text`, `query_text`, `fact_support_ids`, `missing_facts`, `query_status`. `issue_id` do chương trình cấp hoặc được chọn từ danh sách có sẵn.

```text
Từ các facts được cung cấp, đề xuất các vấn đề pháp lý cụ thể cần tìm nguồn luật.
Mỗi issue tương ứng một nhu cầu nghiên cứu; tránh chia lại cùng một ý bằng từ khác.
Cho mỗi issue, viết một câu hỏi tìm nguồn, chỉ rõ phạm vi khi facts cho phép.
Liệt kê fact sentence IDs làm phát sinh issue.

Không đưa số Điều, số hiệu văn bản, đoạn chép luật hoặc kết quả giải quyết vào query.
Không gán tội danh, hiệu lực giao dịch, trách nhiệm hay kết quả mà facts chưa xác nhận.
Không sáng tác thêm facts để tạo một câu hỏi dễ trả lời.
Nếu thiếu ngày hoặc tình tiết làm thay đổi phạm vi nguồn cần tìm, ghi missing_facts;
không tự lựa chọn một giả định. Trả needs_clarification nếu sự thiếu đó chặn nhiệm vụ.
Nếu không đủ facts để có issue rõ, trả insufficient_facts.
```

Một phần query do người viết trực tiếp từ facts được giữ làm đối chứng. Các query không thể trả lời tốt được đưa về bổ sung annotation hoặc loại có lý do, không tự thay facts cho khớp một Điều đã biết.

Nhánh riêng cho **silver train sinh ngược từ luật** có thể cung cấp passage rồi yêu cầu sinh tình huống/query và lọc consistency, theo hướng [InPars](https://arxiv.org/abs/2202.05144) và [Promptagator](https://arxiv.org/abs/2209.11755). Phải ghi `query_origin=statute_conditioned`; không trộn nhánh đó vào gold test chính lấy từ facts bản án.

## 4 P3 Phân giải citation

**Input:** citation thô, đoạn chứa citation và ngữ cảnh cần thiết; candidates có ID, tên/số hiệu, nội dung, cấu trúc và các khẳng định thời gian kèm nguồn. Gói này tách khỏi payload P2.

**Output:** `selected_candidate_ids`, `resolution_status`, `support_sentence_ids`, `ambiguity_reason`, `missing_evidence`. Phân giải citation trả lời “bản án đang nói tới nguồn nào”, chưa trả lời “nguồn đó áp dụng cho vụ việc không”.

```text
Xác định citation trong input đang tham chiếu tới candidate nào, nếu đủ chứng cứ.
Đối chiếu số hiệu/tên, số Điều/khoản/điểm và ngữ cảnh dẫn chiếu.
Không gán luật chỉ dựa trên tên gần nhất hoặc năm xét xử.
Kiểm tra các từ “luật này”, “bộ luật trên” và danh sách nhiều Điều theo ngữ cảnh.

Chỉ chọn candidate IDs có trong input. Nếu có nhiều nguồn còn hợp lý, giữ tất cả
candidate cần review và trả ambiguous_act hoặc ambiguous_version.
Nếu không đủ nguồn trong danh sách, trả not_found_in_candidates.
Nếu mâu thuẫn giữa trích dẫn và nội dung nguồn, nêu mâu thuẫn cụ thể.
Không tự tạo phiên bản, ngày hiệu lực, số khoản hoặc số điểm chưa có trong dữ liệu.
```

**Kiểm tra:** unit thực sự tồn tại; evidence ID thuộc đúng nguồn; lịch sử nguồn được truy vết. `not_found_in_candidates` không được tự đổi thành `corpus_missing`.

## 5 P4 Đánh giá từng nguồn và trích bằng chứng

**Input:** facts, query/issue/timeline và một nguồn ứng viên có ngữ cảnh phân cấp đủ nghĩa. Không cung cấp nhãn của generator, tên retriever, thứ hạng retrieval hoặc thông tin tòa đã trích nguồn đó ở vòng annotation độc lập.

**Output:** `candidate_id`, `relevance_proposal`, `version_status`, `evidence_quotes`, `source_roles`, `short_justification`, `missing_context`. Trạng thái thời gian ở đây là mức được chứng cứ hỗ trợ, không phải kết luận cuối về applicability.

```text
Đánh giá nguồn này có giúp nghiên cứu issue đã cho hay không, không chỉ có chung từ khóa.
Đề xuất core_candidate nếu chứa căn cứ trực tiếp hoặc một phần căn cứ quan trọng;
supporting nếu hữu ích nhưng chưa cho thấy bắt buộc; not_relevant nếu đã đủ ngữ cảnh
để loại trong scope; uncertain nếu còn thiếu nội dung, facts hoặc thông tin phiên bản.
Core_candidate chưa khẳng định rằng nguồn này là yêu cầu bắt buộc độc lập.

Đối chiếu riêng nội dung và căn cứ xác định phiên bản. Nếu thời gian chưa được xác minh,
ghi unresolved; không chọn bản hiện hành theo trí nhớ. Luật cũ có thể cần nghiên cứu
để so sánh hoặc xử lý chuyển tiếp; ghi vai trò đó khi có căn cứ.
Nguồn tố tụng/án phí chỉ liên quan nếu phù hợp issue cụ thể, không tự chấp nhận mọi citation.

Trích nguyên văn các đoạn nhỏ nhưng đủ điều kiện, ngoại lệ và câu dẫn cần thiết.
Nếu cần thêm đoạn/Điều liên quan thì nêu thiếu gì. Không tạo quote bằng cách diễn giải lại.
Giải thích ngắn vì sao đoạn đó hỗ trợ việc tìm nguồn cho issue, không kết luận bên nào thắng.
```

**Kiểm tra:** quote exact-match hoặc chuyển review nếu không khớp; khớp nhiều vị trí phải phân giải; không tự cắt ngoại lệ. Một nguồn có nhiều đoạn cần thiết được lưu nhiều span. Local validator không xác nhận được relevance chỉ từ việc quote tồn tại.

Rubric legal dùng `core_candidate/supporting/not_relevant/uncertain` là điều chỉnh của dự án. Không dùng nguyên thang điểm passage “chứa đáp án” trong UMBRELA cho mọi quy phạm và không coi thang đó đã được kiểm định ở pháp luật Việt Nam.

## 6 P5 Kiểm tra tập nguồn và phát hiện chỗ thiếu

**Input:** query, các nguồn đã đọc cùng evidence, các đề xuất P4 và giới hạn pool. Cho phép thấy toàn tập đề xuất để tìm mâu thuẫn, nhưng đó vẫn là annotation candidate.

**Output:** `proposed_requirement_groups`, `redundant_sources`, `unresolved_version_conflicts`, `missing_source_requests`, `review_reasons`. Cơ chế retrieval/human xử lý yêu cầu tìm bổ sung; model không tự viết một nguồn “đáng lẽ tồn tại”.

```text
Kiểm tra tập nguồn hiện tại theo từng nhu cầu của issue.
Phân biệt các nguồn bổ sung cho nhau với nhiều bản thể hiện cùng quy phạm.
Đề xuất nhóm nguồn cần thiết; chỉ coi hai nguồn là alternative khi input có chứng cứ
chúng thể hiện quy phạm tương đương trong phạm vi và phiên bản đang xét.
Nếu chưa đủ chứng cứ tương đương thì đánh dấu cần review.

Tìm khoảng trống: định nghĩa, ngoại lệ, quy định chuyển tiếp, văn bản sửa đổi hoặc
hướng dẫn mà nội dung nguồn đã đọc thực sự dẫn tới. Nêu quan hệ/đoạn dẫn cần theo,
không tự phát minh số hiệu. Không biến mọi cạnh citation thành gold.
Chỉ kết luận về độ phủ của pool đã xét; không tuyên bố đã bao phủ toàn bộ pháp luật.
Không đưa ra kết quả áp dụng quy định vào facts hoặc phán quyết cuối.
```

`core_candidate` của P4 có thể được hạ thành supporting hoặc ghép vào alternative group ở P5/human review. Cần kiểm tra tập nguồn, vì tính “đủ và cần” không thể luôn quyết định từ từng passage riêng lẻ.

## 7 P6 Kiểm chứng query và đề xuất nhãn

Thực hiện hai phần với phạm vi thông tin khác nhau:

1. **Kiểm tra độc lập:** nhận facts, query và nguồn nhưng không nhận nhãn/giải thích trước; đề xuất lại các kiểm tra cốt lõi.
2. **Đối chiếu sai khác:** sau đó mới thấy annotation trước để lập danh sách chỗ cần sửa hoặc người phân xử.

Đây là cách giảm anchoring do dự án đề xuất. Nó không tạo ra hai nguồn chứng cứ độc lập khi cùng model/corpus được dùng.

```text
Kiểm tra lần lượt:
Mỗi chi tiết trong query có được facts hỗ trợ không?
Issue có dự đoán kết quả hoặc lộ nguồn đích không?
Từng ID/quote có đúng nguồn và đủ nghĩa không?
Những khẳng định về phiên bản có chứng cứ hay chỉ là suy đoán?
Nguồn nào chỉ chung chủ đề, nguồn nào còn cần tìm bổ sung?

Trả danh sách checks với pass/fail/uncertain, evidence IDs và sửa đổi đề nghị.
Một check fail hoặc uncertain quan trọng phải được giữ lại cho review.
Không tự sửa facts hoặc tự thêm nguồn để làm sample vượt qua kiểm tra.
Không tuyên bố gold chỉ vì hai annotation trùng nhau.
```

Kiểm tra độc lập có thể chạy toàn pilot để đo giá trị, sau đó dùng theo rủi ro cho silver và lấy mẫu ngẫu nhiên trong nhóm auto-pass. Gold vẫn theo protocol chuyên gia trong tài liệu phương pháp. Chỉ giữ các stage verifier giúp giảm lỗi còn sót hoặc công review trong phép so sánh pilot.

## 8 P0V Nhánh VLM khi text không đủ tin cậy

**Input:** ảnh trang/crop từ PDF nguồn, `page_id`, hash ảnh và text đang nghi ngờ nếu cần đối chiếu. Nhánh này cần profile hỗ trợ ảnh; trường hợp chưa có file/ảnh gốc phải ghi thiếu nguồn, không gửi mỗi URL PDF như thể đã nhìn được nội dung.

**Output:** các đoạn chép lại theo trang/region, `uncertain_regions`, đề xuất lỗi text và bằng chứng hình ảnh. Giữ liên kết giữa text dẫn xuất, trang ảnh và bản gốc. Bbox chỉ có giá trị annotation sau kiểm tra phù hợp, không tự coi tọa độ model đoán là gold.

```text
Đọc nội dung có thể nhìn thấy trên trang/crop được cung cấp.
Giữ chính xác số Điều/khoản/điểm, số hiệu, ngày, phủ định, số tiền và thứ tự đọc.
Đánh dấu ký tự hoặc vùng không đọc rõ; không điền bằng kiến thức pháp luật.
Nếu text trích xuất kèm theo khác với ảnh, chỉ ra vị trí và cách chép lại đề nghị.
Không suy diễn phần trang bị cắt hoặc chưa được cung cấp.
Chưa gán relevance hoặc kết luận pháp lý ở bước chép/đối chiếu này.
```

Text đã sửa phải qua kiểm tra và sinh text version/hash mới trước khi tính evidence span. Cần đo lỗi số hiệu/ngày và thứ tự đọc riêng. [OmniDocBench](https://arxiv.org/abs/2412.07626) hỗ trợ cách đánh giá nhiều thành phần của document parsing; không chứng minh VLM đã đủ chính xác cho bản án Việt Nam.

## 9 Cách chọn prompt trên pilot

So sánh tối thiểu instruction + schema với instruction + schema + few-shot đã duyệt. Với nguồn pháp lý khó, thử thêm căn cứ ngắn và verifier độc lập; không mặc định prompt dài hay reasoning nhiều hơn sẽ tốt hơn.

Đánh giá trên tập phát triển cố định bằng: facts bị thêm/bỏ, câu lộ đáp án, mapping đúng nguồn/phiên bản, relevance theo chuyên gia, evidence đủ nghĩa, tỷ lệ phải sửa và chi phí trên sample được chấp nhận. Round-trip retrieval chỉ là tín hiệu bổ sung cho nhánh silver; không loại gold khó chỉ vì baseline truy hồi kém.

Chốt prompt, schema, profile và guideline trước khi xử lý gold test. Mọi thay đổi sau đó cần version mới và đánh giá lại trên tập phát triển; không chọn prompt theo điểm test hoặc theo model judge tự chấm.

## 10 AUGMENT Nhánh synthetic train tùy chọn

```text
Từ nguồn luật được cung cấp, tạo một tình huống hoàn toàn giả định và một issue/query
tìm nguồn phù hợp. Không tái sử dụng vụ án thật hoặc suy đoán tình tiết của bản án test.
Ghi rõ dữ kiện cần thiết; không thêm số Điều hay số hiệu nguồn đích vào issue/query.
Chỉ dùng nội dung nguồn đã cho, không tự bổ sung luật khác. Trích nguyên văn đoạn
hỗ trợ từ nguồn và ghi missing_facts nếu không đủ để tạo tình huống rõ ràng.
Đây là dữ liệu synthetic để đề xuất huấn luyện, không phải gold benchmark.
```
