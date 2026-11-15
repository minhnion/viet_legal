# Kiểm thử pipeline rule recall

Ngày kiểm tra: 15/11/2026. Đây là kết quả kiểm tra vận hành và tính nhất quán dữ liệu; chưa phải đánh giá chất lượng nhãn pháp lý.

## Môi trường

- Python 3.11.15, SQLite FTS5; dependency thực tế được ghi trong `requirements.lock.txt`.
- `uv pip check --python .venv/bin/python`: 30 package tương thích, không có dependency conflict.
- Endpoint thật: OpenAI-compatible Chat Completions, model `qwen3.5-35b-a3b`, structured output `json_schema`. Credential chỉ nằm trong file env riêng.
- `doctor` đã kiểm tra kết nối model; các corpus hiện có lần lượt 16 shard bản án, 96 shard TVPL và 7 shard Pháp điển.

## Kiểm thử tự động

Lệnh: `.venv/bin/pytest -q`. Kết quả: **82 passed**, khoảng 21 giây.

| Nhóm | Trường hợp được kiểm tra |
|---|---|
| Cấu hình | Đường dẫn theo vị trí file env; process env override; URL/profile/budget sai; key bắt buộc hoặc không dùng auth; không lộ key trong cấu hình công khai |
| Client model | Ba chế độ JSON; hai tên token parameter; bearer/không auth; retry 429/5xx; dừng với 400/401; refusal, truncation, JSON/schema/transport lỗi; giới hạn context; cache và response sai semantic |
| Ingest | Streaming Parquet, checkpoint/resume, hash shard, giới hạn dòng đã quét, NFC/offset, Điều–Khoản–Điểm, ID chứa hash nội dung, crosswalk ứng viên, family mapping |
| Annotation | Luồng P1–P6 qua HTTP mock; P2 không nhận citation/nhận định/kết quả; kiểm tra ID và quote; vụ không có issue; pool rỗng; nguồn dài phải giữ uncertain; failed queue và retry tường minh |
| Review/gold | Import atomic; snapshot cũ bị từ chối; kiểm tra evidence/version; hai người đồng thuận; adjudication khi bất đồng; lịch sử review; tách input công khai và provenance |
| Tiện ích | CLI run/export, synthetic tách gold, ảnh VLM qua mock và không ghi đè corpus, metrics retrieval/requirement/evidence |

Đây là các trường hợp test chức năng, không phải tuyên bố đã phủ mọi tình huống pháp lý hoặc đo phần trăm line coverage.

## Chạy tích hợp trên dữ liệu và model thật

Cấu hình riêng `.env.smoke`, output `outputs/smoke_v3`; không dùng output pilot làm corpus benchmark hoàn chỉnh.

| Hạng mục | Kết quả |
|---|---|
| Dòng đã quét | 300 TVPL + 300 Pháp điển + 512 bản án |
| Nguồn luật giữ lại | 600 |
| Legal units trong SQLite | 19.294, gồm Điều và đơn vị con |
| Điều xuất vào corpus retrieval | 2.801 |
| Family heuristic | 474 trên 512 bản án; vẫn cần review |
| Vụ được chọn annotation | `doc_name=1392` |
| Active run | 1 query ứng viên, 0 failure, 0 gold |
| Model thật | Đã chạy các stage P1, P2, P3, P4, P5, P6; active run cuối có 20 request thực tế |
| Review/export | Có cả gói độc lập, gói đối soát, candidate và manifest |
| Chạy lại cùng cấu hình | `previously_complete=1`, `attempted=0`, thêm **0 request LLM** |

Snapshot và run hash đầy đủ nằm trong `outputs/pipeline_validation.json` và manifest của output pilot. Tổng query trong SQLite có thể lớn hơn active run vì giữ lịch sử các run; export chỉ lấy active run.

Đã kiểm tra key không xuất hiện trong source, docs, tests, `.env.example`, log model và cache response của lần chạy tích hợp. File `.env` và `.env.smoke` có quyền `600`.

## Phạm vi chưa xác nhận

- Chưa chạy hết corpus và chưa chạy annotation trên toàn bộ bản án. Snapshot pilot ghi rõ `ingestion_complete=false` cho cả ba corpus.
- Chưa có review chuyên gia trên dữ liệu thật, nên chưa phát hành gold hoặc báo cáo điểm benchmark pháp lý.
- Endpoint remote/OpenAI, chế độ không key và VLM được kiểm tra bằng HTTP mock; chưa gọi dịch vụ remote hoặc kiểm tra ảnh trên model thật.
- Chưa đo tải dài hạn, throughput và dung lượng khi index toàn bộ corpus. Chưa xây dense retriever, lịch sử hiệu lực đầy đủ, thí nghiệm corpus withholding hoặc agent runner.

## Lệnh tái hiện

Trên server đã có `.env.smoke` riêng:

```bash
.venv/bin/pytest -q
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env.smoke run
.venv/bin/python scripts/run_rule_recall_pipeline.py --env .env.smoke export --include-corpus
```

Trên server mới, tạo file env từ `.env.example`, điền đường dẫn và kết nối model; dùng lệnh pilot và full tại [README](../README.md). Không sao chép credential vào báo cáo hoặc source control.
