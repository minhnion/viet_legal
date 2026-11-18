# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A pipeline that builds a **Vietnamese legal rule-recall (LR02) dataset**: each query is facts + issue + a "which provisions govern this?" question taken from a court judgment; the answer is the provisions **the court actually applied** for that issue, resolved to article units of a frozen law corpus (TVPL + Pháp điển) with version, equivalent copies and clause-level evidence. Output is `silver` (automated, `review_status=unreviewed`) or `needs_review`; *gold* still requires ≥2 agreeing human reviews (or an adjudication). There is no benchmark agent here; `evaluate` scores an existing predictions file against `gold` or `silver` labels.

All design/ops docs are in Vietnamese under `specs/` (`specs/README.md` is the index; `02_pipeline.md` explains every rule and the measured numbers). `specs/prompts.md` is the prompt bank actually loaded at runtime (`RR_PROMPTS_FILE`). `docs/` holds only original project material (slides, report, LegalBench PDF) — do not add generated docs there.

## Commands

The working environment on this server is the conda env `vietlegal` (`command.md`); base-env pyarrow 19 cannot read the judgment shards. README documents a `.venv` alternative. Python 3.10+ with SQLite FTS5.

```bash
pytest -q                                   # full suite (~35s, no network; uses a fake local HTTP model server)
pytest tests/test_citations.py -q           # citation/section/version parsing on real-world strings
python scripts/run_rule_recall_pipeline.py --env .env doctor --offline   # config/data/FTS5 check, no model call
python scripts/run_rule_recall_pipeline.py --env .env audit --limit 1000 # deterministic funnel, no model call, writes audit.json
python scripts/run_rule_recall_pipeline.py --env .env run                # preflight → build-laws → build-cases → annotate → review-export → export
python scripts/run_rule_recall_pipeline.py --env .env status
```

Individual subcommands: `build-laws`, `build-cases`, `audit`, `annotate [--retry-failed]`, `review-export`, `review-import --file F [--replace]`, `export [--include-corpus]`, `search/read/versions/relations`, `vision`, `augment`, `evaluate [--labels gold|silver]`. `python -m legal_recall` is equivalent to the script.

Pilot/smoke runs: override `RR_*` inline (process env wins over `.env`). `/data/minhdh/law_bench/output/sample` already holds the full ingested law corpus (53 GB) + 20k judgments; point `RR_OUTPUT_DIR` there and run `audit`/`annotate`/`export` instead of `run` to avoid re-ingesting (~50 min). Never start a full run unasked. `RR_<PROFILE>_CONTEXT_TOKENS` must equal the vLLM `max_model_len` (`GET /v1/models`; 16384 on 18/11/2026).

Exit code `2` = config/validation error **or** any case failed annotation. Infrastructure errors (transport/HTTP/tokenize) stop the run without marking cases failed; rerunning continues.

## Architecture

Package `legal_recall/`; `cli.py` dispatches everything and holds a `FileLock` on `RR_OUTPUT_DIR/.pipeline.lock` (single writer per output dir; read-only commands incl. `audit` skip it).

- **config.py** — config only from the env file + `RR_*` env vars; no provider defaults. Relative paths resolve against the **env file's directory**. Model connections are named *profiles*, bound to stages `ISSUE`, `EVIDENCE`, `AUGMENT` (+ optional `P0V`) via `RR_DEFAULT_PROFILE` / `RR_PROFILE_<STAGE>`.
- **ingest.py** — Parquet shards → `sources`/`units` (Điều→Khoản→Điểm with offsets, FTS5), Pháp điển→TVPL `crosswalk`, **`acts`** instrument index (`build_acts`: type, folded `name_key` from the TVPL title slug, `number_key`, issue/effective/expiry dates, `is_amendment`), `meta.law_counts`; judgments → `cases`, families (union-find), split by family. Per-shard checkpoints keyed by file hash; a changed shard is refused.
- **citations.py** — judgment `sections` (facts/reasoning/decision headings), `mentions` parsed on a folded, whitespace-free *stream* with offset map (OCR-tolerant), instrument binding (names, abbreviations, numbers, context reuse, never borrowing for an unreadable name), `ActIndex.pick` version choice (cited year → same-judgment year → in force on judgment date via succession; never an act starting after the judgment), `role` (substantive/procedural/court_fee/enforcement).
- **pipeline.py** — `prepare` (main thread, all DB reads: sections, facts, mentions, `resolve`, targets with clause options and equivalent copies) → `run_model` in worker threads (ISSUE: group substantive citations into issues + facts-only question, leak check inside the semantic validator so rejected answers are not cached, one repair; EVIDENCE: per-issue relevance + clause ids, soft errors fall back to court-cited clauses with low confidence) → `build` (grades, requirement groups merging cross-version articles and copies, evidence offsets, confidence, silver/needs_review, stable query IDs). `annotate` runs cases in parallel (`CONCURRENCY`), records `complete`/`skipped`/`failed` per (case, run_hash).
- **contracts.py** — strict JSON Schemas per stage (enum-constrained short IDs) and `PromptBank` (first ```` ```text ```` block = common rules, then `## N ISSUE|EVIDENCE|P0V|AUGMENT` sections). Changing heading format breaks prompt loading.
- **client.py** — Chat Completions per profile with local schema validation, exact token counting via vLLM `/tokenize` (over budget → `context_budget`, never silent truncation), retries, cache keyed by request+schema (not run), redacted call log, invalid outputs saved for debugging.
- **review.py** — review templates/bundles from `target`, validated import, export of `candidates`, `silver_*`, `gold_*` (gold only after agreeing reviews), `failures` (active run), `manifest` with funnel.
- **evaluate.py** — Recall/Precision/F2, nDCG, requirement-group coverage, evidence F1.

### Run identity, cache and resume (important when changing code)

`run_hash` = digest of corpus snapshot, prompt bank (+few-shot), hash of every `legal_recall/*.py`, profiles, stage bindings, `max_issues`, stage output caps. Any change creates a new run (cases re-annotated; `export` emits only the active run). The response cache and **query IDs** (hash of case + answer units) do not depend on `run_hash`, so identical requests are reused and imported reviews keep matching. `complete`/`skipped` cases are not rerun; `failed` only with `--retry-failed`.

## Invariants to preserve

- Never turn HTTP/schema/refusal/truncation errors into legal labels; soft model errors may only degrade confidence with an explicit flag, infrastructure errors stop the run.
- Answers come from court-applied **substantive** citations resolved in the corpus; procedural, court-fee and enforcement citations are recorded but never answers. A cited instrument that cannot be read or resolved is never replaced by another one.
- Queries (`issue`/`question`) must not contain article numbers, instrument numbers or names, or court outcomes; the model-visible export (`silver_queries`, `gold_queries`) carries no judgment identity.
- Don't persist provider response bodies, auth headers or API keys in logs/failures (`ModelError` messages intentionally omit them).
- Gold requires the human-review gate; silver is never promoted automatically; synthetic `augment` output is train-only.
- Few-shot examples must be `human_reviewed=true` from train/dev splits.
- Do not change `text.normalize`/`legal_units`/`PARSER_VERSION` casually: unit IDs and the ingested 53 GB corpus depend on them.


## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
