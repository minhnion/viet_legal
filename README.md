# Vietnamese legal rule recall dataset pipeline

Pipeline dựng dataset và ground truth cho bài toán **Rule Recall (LR02)**: cho facts của một vụ án và một issue, tìm các Điều luật cần nghiên cứu trong một kho luật cố định.

Ground truth được **neo vào căn cứ mà tòa đã áp dụng**:

1. Code tách bản án thành các phần facts, nhận định và quyết định.
2. Code trích các căn cứ tòa nêu, rồi tra cứu từng căn cứ ra đúng văn bản, phiên bản và Điều trong kho TVPL + Pháp điển.
3. LLM (chuẩn OpenAI-compatible) gom các căn cứ thành issue và viết câu hỏi chỉ dựa trên facts.
4. LLM xác nhận từng Điều và chọn khoản làm bằng chứng.

Mỗi query đầu ra có mức chất lượng (`silver` hoặc `needs_review`), mức `confidence` và lý do cần kiểm tra. Query chỉ trở thành `gold` khi đã có người duyệt.

**Tài liệu đầy đủ nằm trong [specs/](specs/README.md):**

- [bài toán và dataset](specs/01_bai_toan_va_dataset.md)
- [pipeline](specs/02_pipeline.md)
- [triển khai và vận hành](specs/03_trien_khai_van_hanh.md)
- [prompt](specs/prompts.md)

## Cài đặt

Python 3.10+ và SQLite có FTS5. Trên server này dùng conda env `vietlegal` (`conda activate vietlegal`). Nếu dùng virtualenv:

```bash
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.lock.txt
```

Cấu hình nằm trong `.env` (đã gitignore). Danh sách biến xem tại [specs/03 §2](specs/03_trien_khai_van_hanh.md#2-cài-đặt-và-cấu-hình). Lưu ý: `RR_<PROFILE>_CONTEXT_TOKENS` phải bằng `max_model_len` của server vLLM.

## Chạy

```bash
pytest -q                                                                  # test, không gọi mạng
python scripts/run_rule_recall_pipeline.py --env .env doctor               # kiểm tra dữ liệu + endpoint
python scripts/run_rule_recall_pipeline.py --env .env audit --limit 1000   # funnel bằng code, không gọi LLM
python scripts/run_rule_recall_pipeline.py --env .env run                  # build-laws → build-cases → annotate → review-export → export
python scripts/run_rule_recall_pipeline.py --env .env status
```

Chạy lại đúng lệnh sẽ tiếp tục từ checkpoint. Vụ `failed` chỉ được chạy lại khi thêm `--retry-failed`. Khi gặp lỗi hạ tầng (endpoint ngừng phản hồi, HTTP lỗi), run sẽ dừng mà không đánh dấu vụ thất bại.

Đầu ra chính nằm trong `RR_OUTPUT_DIR/export/`:

- `silver_queries.jsonl`: input dành cho model.
- `silver_qrels.jsonl`: đáp án, gồm qrels và requirement groups.
- `silver_evidence.jsonl`, `silver_provenance.jsonl`.
- `candidates.jsonl`: toàn bộ query, kèm annotation đầy đủ.
- `gold_*.jsonl`: chỉ có khi đủ review.
- `manifest.json`: funnel của run.
