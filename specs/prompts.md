# Prompt bank: annotation rule recall

File này vừa là tài liệu vừa là **prompt bank được nạp lúc chạy**, qua biến `RR_PROMPTS_FILE`.

`PromptBank` (trong [contracts.py](../legal_recall/contracts.py)) đọc file theo quy ước sau:

- Block ```` ```text ```` **đầu tiên** của file là phần hướng dẫn chung.
- Mỗi heading dạng `## <số> <STAGE>` mở đầu prompt của một stage; block `text` đầu tiên dưới heading đó là prompt của stage.

Bắt buộc phải có hai stage `ISSUE` và `EVIDENCE`. Hai stage `P0V` và `AUGMENT` là tuỳ chọn.

Lưu ý khi chỉnh sửa:

- Sửa bất kỳ chữ nào trong file đều làm đổi `prompt_version` và `run_hash`, tức là tạo ra một run mới.
- Không đổi định dạng heading, nếu không loader sẽ không nạp được prompt.
- Schema đầu ra không nằm trong file này. Code tự nối JSON Schema của stage vào cuối instruction. Schema và validator nằm trong [contracts.py](../legal_recall/contracts.py) và [pipeline.py](../legal_recall/pipeline.py).

## 1 Quy tắc dùng chung

```text
Bạn hỗ trợ annotation dữ liệu pháp luật Việt Nam theo nhiệm vụ được giao.
Chỉ sử dụng dữ liệu và các nguồn được cung cấp trong request này.
Không dùng trí nhớ về pháp luật để bổ sung số hiệu, phiên bản, ngày hoặc nội dung còn thiếu.
Giữ riêng lời trình bày của đương sự, quan điểm cơ quan tố tụng và nhận định của tòa.
Chỉ trả các ID có trong input. Không tự tạo ID.
Trả đúng một JSON object theo schema của stage; lý do ngắn, gắn với input.
Nhiệm vụ là chuẩn bị nhãn dữ liệu, không phải đưa ra phán quyết hoặc tư vấn kết quả vụ án.
```

## 2 ISSUE Gom căn cứ của tòa thành issue và viết câu hỏi

**Input:**

- `facts`: các câu thuộc phần NỘI DUNG VỤ ÁN. Code đã loại những câu có số Điều hoặc chứa kết quả xử.
- `court_reasoning`: các câu trong phần NHẬN ĐỊNH/QUYẾT ĐỊNH có trích căn cứ, kèm `citation_ids`.
- `citations`: các Điều mà tòa áp dụng, đã được code tra cứu ra trong kho luật (số Điều, tên Điều, văn bản, khoản tòa nêu).

Chỉ các căn cứ **nội dung** được đưa vào. Căn cứ về tố tụng, án phí và thi hành án đã được code loại trước.

**Output:** tối đa `max_issues` issue. Mỗi issue gồm các `citation_ids` dùng cho nó, các `fact_ids` cần thiết, `issue_text` và `query_text`.

Code kiểm tra lại toàn bộ ID. Code cũng chặn số Điều, số hiệu, tên văn bản và kết quả xử, rồi cho model sửa đúng một lần.

```text
Nhiệm vụ: dựng câu hỏi tìm luật (rule recall) từ một bản án đã có lời giải.
Tòa đã áp dụng các Điều trong "citations" để giải quyết vụ án; "court_reasoning" cho biết
tòa dùng chúng ở đâu. Hãy:
1. Gom các citation vào từng vấn đề pháp lý (issue) mà tòa thực sự giải quyết bằng chúng.
   Một issue có thể dùng nhiều citation; các citation cùng một quy định ở hai phiên bản luật
   (ví dụ luật cũ và luật mới cùng nội dung) để chung một issue. Không tạo issue không có
   citation. Citation không gắn với vấn đề tranh chấp nào thì đưa vào unused_citation_ids.
2. Với mỗi issue, chọn fact_ids: các câu facts mà người nghiên cứu cần biết để tự tìm ra
   các quy định đó. Không chọn câu không liên quan.
3. Viết issue_text: vấn đề pháp lý trung tính, ngắn gọn (ví dụ: "Nghĩa vụ trả nợ của bên vay
   khi hết thời hạn vay").
4. Viết query_text: câu hỏi tìm nguồn luật, đứng từ góc độ người chỉ biết facts, hỏi những
   quy định nào điều chỉnh vấn đề đó.
Cấm trong issue_text và query_text: số Điều/khoản/điểm, số hiệu văn bản, tên văn bản
("Bộ luật ...", "Luật ...", "Nghị quyết ...", "Nghị định ...", "Thông tư ...", BLDS, BLHS...),
và kết quả hay nhận định của Tòa (chấp nhận, bác, tuyên, xử phạt...).
Không chép nguyên tên Điều (article_title) hay tên tội danh vào query_text; mô tả hành vi và
quan hệ bằng lời của facts (ví dụ "lén lấy xe máy của người khác" thay vì tên tội).
Ưu tiên ít issue nhưng đúng; không chia nhỏ một vấn đề thành nhiều issue gần giống nhau.
Nếu có revision_notes thì sửa đúng các lỗi được nêu.
```

## 3 EVIDENCE Xác nhận Điều luật và chọn khoản làm bằng chứng

**Input:**

- Issue, câu hỏi và các facts của issue.
- `sources`: các Điều thuộc issue. Mỗi Điều được tách thành `clauses` có ID (khoản, hoặc toàn bộ Điều nếu Điều không chia khoản), kèm `court_cited_clauses` là các khoản tòa đã nêu.

**Output:** với mỗi nguồn trả về `relevance` (`core`, `supporting` hoặc `not_relevant`), các `clause_ids` hỗ trợ issue và `reason`.

Code đổi `clause_ids` thành offset trên `unit.text`. Model không tự viết quote hay offset nào.

```text
Nhiệm vụ: với từng Điều luật trong "sources", đánh giá quan hệ với issue và question.
- core: Điều trực tiếp quy định quyền, nghĩa vụ, điều kiện hoặc hậu quả pháp lý cần để trả
  lời question.
- supporting: Điều hữu ích nhưng không trực tiếp giải quyết (định nghĩa, nguyên tắc chung,
  dẫn chiếu).
- not_relevant: Điều không liên quan đến issue này.
Chọn clause_ids là các khoản chứa nội dung hỗ trợ issue; nếu Điều chỉ có một lựa chọn
(".all") thì chọn lựa chọn đó khi Điều là core hoặc supporting. court_cited_clauses là khoản
tòa đã nêu, dùng làm gợi ý nhưng vẫn phải đối chiếu nội dung. Mỗi nguồn trả đúng một lần.
reason: một câu, dựa trên nội dung Điều được cung cấp.
```

## 4 P0V Nhánh VLM khi text không đủ tin cậy

Đây là nhánh tuỳ chọn, chạy bằng lệnh `vision`. Nhánh này cần một profile hỗ trợ ảnh (`RR_PROFILE_P0V`). Kết quả chỉ là đề xuất để người kiểm tra; không được tự áp vào corpus.

```text
Đọc nội dung có thể nhìn thấy trên trang/crop được cung cấp.
Giữ chính xác số Điều/khoản/điểm, số hiệu, ngày, phủ định, số tiền và thứ tự đọc.
Đánh dấu ký tự hoặc vùng không đọc rõ; không điền bằng kiến thức pháp luật.
Nếu text trích xuất kèm theo khác với ảnh, chỉ ra vị trí và cách chép lại đề nghị.
Không suy diễn phần trang bị cắt hoặc chưa được cung cấp.
Chưa gán relevance hoặc kết luận pháp lý ở bước chép/đối chiếu này.
```

## 5 AUGMENT Nhánh synthetic train tùy chọn

Đây là nhánh tuỳ chọn, chạy bằng lệnh `augment`. Kết quả chỉ dùng cho tập train và không bao giờ trở thành gold.

```text
Từ nguồn luật được cung cấp, tạo một tình huống hoàn toàn giả định và một issue/query
tìm nguồn phù hợp. Không tái sử dụng vụ án thật hoặc suy đoán tình tiết của bản án test.
Ghi rõ dữ kiện cần thiết; không thêm số Điều hay số hiệu nguồn đích vào issue/query.
Chỉ dùng nội dung nguồn đã cho, không tự bổ sung luật khác. Trích nguyên văn đoạn
hỗ trợ từ nguồn và ghi missing_facts nếu không đủ để tạo tình huống rõ ràng.
Đây là dữ liệu synthetic để đề xuất huấn luyện, không phải gold benchmark.
```
