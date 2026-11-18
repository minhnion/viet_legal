# 01. Bài toán, task và dataset mong muốn

*Cập nhật 18/11/2026. Tài liệu này mô tả **mục tiêu** của dataset. Cách pipeline tạo ra dataset nằm ở [02_pipeline.md](02_pipeline.md); cách chạy và vận hành nằm ở [03_trien_khai_van_hanh.md](03_trien_khai_van_hanh.md).*

## 1. Rule Recall trong IRAC

Dự án đánh giá năng lực lập luận pháp lý theo khung IRAC (Issue, Rule, Application, Conclusion). Phần **R (Rule Recall)** được đánh mã **LR02**:

> Cho trước tình tiết của một vụ việc và một vấn đề pháp lý, tìm ra **các quy định pháp luật cần nghiên cứu** trong một kho luật cố định.

Ranh giới của LR02 với các bước khác:

| Bài toán | Câu hỏi | Thuộc bước |
|---|---|---|
| Nhận ra vấn đề pháp lý từ tình tiết | Vụ việc có những issue nào? | LR01 (I) |
| **Tìm nguồn luật** | **Những Điều luật nào điều chỉnh issue này?** | **LR02 (R), là đối tượng của repo này** |
| Chọn quy định thực sự áp dụng | Trong các nguồn tìm được, quy định nào áp dụng? | LR03-A |
| Áp dụng vào tình tiết | Tình tiết thoả điều kiện nào, dẫn đến hệ quả gì? | LR03-B |

## 2. Các task dùng chung một bộ ground truth

| Task | Model nhận gì | Model trả gì | Chấm bằng |
|---|---|---|---|
| **RR1: Truy hồi Điều luật theo issue** | facts + issue + câu hỏi + timeline; tìm trên toàn kho luật | danh sách `unit_id` (cấp Điều) xếp hạng | `qrels` + `requirement_groups` |
| **RR2: Định vị bằng chứng trong luật** | như RR1. Có chế độ oracle: được đưa sẵn đúng Điều | Điều kèm span ký tự hỗ trợ | `evidence` (offset trên `unit.text`) |
| **RR3: Agent tìm nguồn luật** | như RR1, kèm công cụ `search`/`read` và một ngân sách cố định | nguồn cuối cùng, evidence, log công cụ | dùng lại gold của RR1/RR2 |
| Masked citation retrieval (task phụ) | đoạn nhận định của tòa đã che căn cứ | Điều bị che | căn cứ gốc của tòa |

Repo này **chỉ build dataset và ground truth**. Benchmark và agent runner làm sau. Lệnh `evaluate` đã chấm được file dự đoán (RR1/RR2) theo nhãn `gold` hoặc `silver`.

## 3. Nguồn ground truth: căn cứ mà tòa đã áp dụng

Bản án đã có sẵn lời giải: phần NHẬN ĐỊNH và QUYẾT ĐỊNH ghi rõ tòa áp dụng Điều nào, của văn bản nào. Vì vậy dataset được dựng theo hướng **neo vào căn cứ của tòa (court-citation anchored)**:

1. Đáp án lõi là **các Điều tòa áp dụng cho từng vấn đề**. Mỗi căn cứ được tra ra đúng bản ghi trong kho luật: đúng văn bản, đúng phiên bản theo ngày, đúng Điều.
2. Issue là vấn đề tòa **thực sự giải quyết** bằng các căn cứ đó. Câu hỏi được viết **chỉ từ facts**, không lộ số Điều, tên văn bản hay kết quả xử.
3. Căn cứ về thủ tục, án phí và thi hành án được loại khỏi đáp án, vì gần như bản án nào cũng có.

Đây cũng là cách các bộ statute identification lớn xây nhãn: CAIL2018, ILSI/IL-TUR (facts lấy từ bản án, nhãn là điều luật tòa trích), CLERC và LePaRD (che citation trong ngữ cảnh). Khác biệt của dataset này: nhãn gắn với **từng issue**, có **phiên bản luật** và có **evidence ở cấp khoản**.

**Vì sao vẫn phải đi vào kho luật?** Bản án chỉ ghi *tên* căn cứ ("Điều 466 Bộ luật dân sự năm 2015"), không ghi nội dung. Kho luật là nơi cung cấp:

- **ID** mà benchmark dùng để chấm điểm.
- **Nội dung Điều**, để lấy evidence.
- **Phiên bản đúng**: BLDS 2005 hay BLDS 2015, BLHS 1999 hay BLHS 2015.
- **Các bản sao tương đương**: cùng một Điều có thể nằm ở bản TVPL gốc, trang dịch của TVPL và bản Pháp điển.

Việc này là **tra cứu theo tên hoặc số hiệu và ngày**. Nó không phải tìm kiếm ngữ nghĩa. BM25 và dense chỉ cần sau này: (a) là baseline khi chạy benchmark, (b) để bổ sung những nguồn tòa không trích (xem §6).

## 4. Ví dụ thật: vụ 301776

**Dữ liệu gốc** (bản án 11/2019/DS-ST, xét xử ngày 15/5/2019):

```
NỘI DUNG VỤ ÁN: Ngày 24/8/2014 bà T cho bà S vay 10.000.000đồng ... thời hạn vay 30 ngày ...
                đến ngày trả nợ bà T có gặp và điện thoại đòi nhiều lần nhưng bà S không trả.
NHẬN ĐỊNH [1] Về tố tụng: khoản 3 Điều 26, điểm a khoản 1 Điều 35 ... Bộ luật tố tụng dân sự   → thủ tục
NHẬN ĐỊNH [2] Về nội dung: ... buộc bà S trả ... đúng theo Điều 471, 474 Bộ luật dân sự năm 2005 → nội dung
NHẬN ĐỊNH [4] Về án phí: Điều 27 Nghị quyết 326/2016/UBTVQH14                                → án phí
QUYẾT ĐỊNH: Áp dụng các Điều 463, 466 Bộ luật dân sự năm 2015; ... khoản 2 Điều 468 (lãi chậm thi hành án) ...
```

**Đầu ra** (rút gọn từ `export/candidates.jsonl` của lần chạy smoke):

```json
{
  "id": "rr:…", "quality": "silver", "confidence": "high", "review_status": "unreviewed",
  "model_input": {
    "facts": [{"sentence_id": "s39", "text": "Ngày 24 / 8 / 2014 bà Nguyễn Thu T có cho bà Võ Thị Thu S vay"}, "…"],
    "issue": "Quy định pháp luật về hợp đồng vay tài sản và nghĩa vụ trả nợ của bên vay …",
    "question": "Khi một bên cho vay và bên kia vay tiền có làm biên nhận … thì các quy định pháp luật nào điều chỉnh …?",
    "timeline": [{"event": "date_mention_requires_event_annotation", "date": "2014-08-24"}, {"event": "judgment_date", "date": "2019-05-15"}]
  },
  "target": {
    "requirement_groups": [
      {"group_id": "g1", "cross_version": true, "canonical_unit_ids": ["tvpl:2463…:a471", "tvpl:296215…:a463"], "unit_ids": ["… + bản sao"]},
      {"group_id": "g2", "cross_version": true, "canonical_unit_ids": ["tvpl:2463…:a474", "tvpl:296215…:a466"], "unit_ids": ["… + phapdien:…:a466 + tvpl:303230…:a466"]}
    ],
    "qrels": [{"unit_id": "tvpl:296215…:a466", "grade": 2, "label_source": "court_applied", "confidence": "high",
               "evidence": [{"start": "<offset khoản 1>", "end": "<…>"}]}, "…"]
  }
}
```

Khi chạy benchmark, model chỉ thấy `model_input`. Nếu model tìm ra **Điều 466 (BLDS 2015) hoặc Điều 474 (BLDS 2005)** thì đều được tính là đã đáp ứng nhóm `g2`.

## 5. Các tầng chất lượng

| Tầng | Điều kiện | Dùng cho |
|---|---|---|
| `silver` | Căn cứ tòa áp dụng, tra được trong kho; model EVIDENCE xác nhận có ít nhất một nguồn `core`; có evidence; câu hỏi không lộ đáp án | Train/SFT, dev và benchmark nội bộ trong lúc chưa có người duyệt |
| `needs_review` | Không còn nguồn `core` (model bác các căn cứ, hoặc căn cứ không liên quan đến issue) | Chỉ để người kiểm tra; không xuất ra tập `silver_*` |
| `gold` | Có ít nhất 2 người duyệt độc lập đồng ý, hoặc có người phân xử | Benchmark chính thức |

Mỗi query đều có `confidence` (`high`/`medium`/`low`), `review_reasons` và cờ ở từng nguồn. Nhờ vậy người tham gia sau này có thể **lọc ra các mục rủi ro để kiểm tra trước**, không phải đọc lại từ đầu. Ý nghĩa của từng cờ xem ở [02_pipeline.md §5](02_pipeline.md#5-dựng-bản-ghi-và-chấm-độ-tin-cậy).

## 6. Giới hạn đã biết

- **Thiếu những nguồn tòa không trích.** Dataset chưa có nhãn cho những nguồn này. Khi chấm benchmark, nguồn ngoài `qrels` phải được coi là `unjudged`, không phải sai. Hướng mở rộng là pooling bằng BM25 + dense, cho LLM chấm, rồi gắn nhãn `label_source=retrieval_added`.
- **Phiên bản luật là "phiên bản cần nghiên cứu".** Với căn cứ không ghi năm, code chọn văn bản đang có hiệu lực vào ngày xét xử (confidence `medium`). Việc quy định nào *thực sự áp dụng* (điều khoản chuyển tiếp) thuộc LR03-A.
- **Văn bản không có phần facts thì không dùng được.** Quyết định công nhận thuận tình ly hôn, đình chỉ hay huỷ án là các ví dụ. Trên mẫu 1000 văn bản, có 54,5% thuộc loại này.
- **Câu hỏi đôi khi chép tên Điều hoặc tên tội danh.** Code gắn cờ `question_repeats_article_title`; chưa tự viết lại.
- Bộ dữ liệu này **thiên về câu chữ của người làm luật** (lấy từ bản án), khác với câu hỏi của người dân (như STARD). Nên đánh giá chéo khi so sánh với các benchmark khác.

## 7. Cơ sở nghiên cứu chính

| Công trình | Cách lấy ground truth | Bài học áp dụng |
|---|---|---|
| [CAIL2018](https://arxiv.org/abs/1807.02478), [ILSI/LeSICiN (AAAI 2022)](https://ojs.aaai.org/index.php/AAAI/article/view/21363), [IL-TUR (ACL 2024)](https://aclanthology.org/2024.acl-long.618) | Điều luật tòa trích trong bản án | Citation của tòa là nhãn rẻ, phủ rộng; phải loại phần lộ đáp án khỏi facts |
| [CLERC (NAACL Findings 2025)](https://aclanthology.org/2025.findings-naacl.441), [LePaRD (ACL 2024)](https://aclanthology.org/2024.acl-long.532/) | Che citation trong ngữ cảnh và yêu cầu tìm lại | Thiết kế của task masked citation retrieval |
| [STARD (EMNLP Findings 2024)](https://aclanthology.org/2024.findings-emnlp.625), [COLIEE](https://coliee.org/), [ALQAC/DRiLL](https://aclanthology.org/2025.vlsp-1.16/) | Chuyên gia gán nhãn trên câu hỏi | Gold do chuyên gia tạo chỉ có quy mô 1–3 nghìn query; cần quy trình phân xử |
| [LeCaRDv2](https://arxiv.org/abs/2310.17609), TREC pooling | Pool ứng viên từ nhiều retriever, rồi chuyên gia chấm | Hướng mở rộng độ phủ; tránh pool bias do chỉ dùng một retriever |
| [LegalBench-RAG](https://arxiv.org/abs/2408.10343) | Span bằng chứng trong văn bản | Evidence lưu bằng offset, chấm theo ký tự |
| [Temporal Misgrounding / FiscalQA Pro (2026)](https://arxiv.org/pdf/2608.09393), [LawInForce-eCFR (AACL 2026)](https://aclanthology.org/2026.aacl-long.42.pdf) | Kho luật có nhiều phiên bản | RAG trên kho tĩnh 0% lần lấy đúng phiên bản; phiên bản phải là ràng buộc cứng của kho |
| [Clarke & Dietz (2024–2025)](https://arxiv.org/abs/2412.17156), [UMBRELA](https://arxiv.org/abs/2406.06519) | LLM làm người chấm relevance | LLM dùng để lọc sơ bộ được, nhưng không thay được người; silver do Qwen tạo không nên dùng làm test cho hệ thống dùng Qwen |
| [The LLM Effect (EMNLP 2024)](https://arxiv.org/abs/2410.04699), [Bias in the Loop (2025)](https://arxiv.org/abs/2509.08514) | Người duyệt xem gợi ý của LLM | Hiệu ứng neo; vì vậy giữ gói review "independent" ẩn đáp án |

Các tài liệu nội bộ gốc (slide kiến trúc, khảo sát VietLegal Bench, báo cáo 05/11/2026, LegalBench) nằm trong [docs/](../docs/).
