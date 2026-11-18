# Commands

```bash
conda activate vietlegal
cd /home/ai4life/minhdh/law_bench/viet_legal
pytest -q
```

## Sample (dùng kho luật đã ingest trong output/sample, không ingest lại)

```bash
export RR_OUTPUT_DIR=/data/minhdh/law_bench/output/sample RR_ANNOTATION_LIMIT=0 RR_CATEGORIES='[]'

# Funnel bằng code, không gọi LLM
python scripts/run_rule_recall_pipeline.py --env .env audit --limit 1000

# 10 bản án sample
RR_CASE_IDS='["301776","358560","103088","102192","53152","33472","76720","200480","323920","234672"]' \
python scripts/run_rule_recall_pipeline.py --env .env annotate

# 80 bản án đầu tiên
RR_CASE_IDS='[]' RR_ANNOTATION_LIMIT=80 python scripts/run_rule_recall_pipeline.py --env .env annotate

python scripts/run_rule_recall_pipeline.py --env .env export
python scripts/run_rule_recall_pipeline.py --env .env review-export
```

## Full run

```bash
python scripts/run_rule_recall_pipeline.py --env .env run
```

Lệnh này dùng `RR_OUTPUT_DIR` trong `.env` (`output/full`), nên sẽ ingest lại kho luật: khoảng 50 phút và trên 53 GB đĩa.

Dừng bằng `Ctrl+C`. Chạy lại đúng lệnh sẽ tiếp tục từ checkpoint; vụ `failed` chỉ chạy lại khi thêm `--retry-failed`.
