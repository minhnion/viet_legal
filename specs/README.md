# Specs: dataset Rule Recall (LR02) cho pháp luật Việt Nam

Pipeline dựng dataset và ground truth cho bài toán **tìm nguồn luật (R trong IRAC)**:

- **Input** của mỗi query: facts của một vụ án, một issue và một câu hỏi.
- **Đáp án**: các Điều luật mà tòa đã áp dụng cho issue đó, đã được tra cứu ra đúng văn bản, phiên bản và Điều trong kho luật cố định. Kèm theo là các bản tương đương và evidence ở cấp khoản.

| Tài liệu | Nội dung |
|---|---|
| [01_bai_toan_va_dataset.md](01_bai_toan_va_dataset.md) | Bài toán, các task RR1/RR2/RR3, ví dụ input/output thật, các tầng chất lượng, giới hạn, cơ sở nghiên cứu |
| [02_pipeline.md](02_pipeline.md) | Từng bước pipeline: kho luật, tách bản án, trích và tra cứu căn cứ, hai stage LLM, dựng bản ghi, xử lý lỗi, số liệu đo trên dữ liệu thật |
| [03_trien_khai_van_hanh.md](03_trien_khai_van_hanh.md) | Module, cấu hình `.env`, lệnh CLI, đầu ra, định danh run và cache, review/gold, kiểm thử, việc tiếp theo |
| [prompts.md](prompts.md) | Prompt bank được nạp lúc chạy (`RR_PROMPTS_FILE`) |

Tài liệu gốc của dự án (slide, báo cáo, LegalBench) nằm trong [../docs/](../docs/).

Chạy nhanh:

```bash
conda activate vietlegal && cd /home/ai4life/minhdh/law_bench/viet_legal
pytest -q                                                                  # 139 test, không gọi mạng
python scripts/run_rule_recall_pipeline.py --env .env doctor               # kiểm tra cấu hình + endpoint
python scripts/run_rule_recall_pipeline.py --env .env audit --limit 1000   # funnel, không gọi LLM
python scripts/run_rule_recall_pipeline.py --env .env run                  # ingest → annotate → export
```
