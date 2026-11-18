"""CLI for preparation, annotation, expert review and deterministic export."""

from dataclasses import replace
from pathlib import Path
import argparse
import json
import logging
import sys
from filelock import FileLock, Timeout

from .client import ModelError
from .config import ConfigError, canonical, load_config
from .contracts import schema
from .evaluate import evaluate
from .ingest import build_cases, build_laws, document_files
from .pipeline import AnnotationError, Pipeline, unit_payload
from .review import ReviewError, export_dataset, export_review, import_reviews
from .store import Store, write_jsonl
from .text import leakage, quote_spans


def doctor(config, store, offline=False):
    files = {}
    for name, root in [("judgments", config.judgment_path), ("tvpl", config.tvpl_path), ("phapdien", config.phapdien_path)]:
        files[name] = len(document_files(root))
    if not config.prompts_file.is_file():
        raise ConfigError("Configured prompt document does not exist")
    pipeline = Pipeline(config, store)
    report = {"document_shards": files, "output_dir": str(config.output_dir),
              "profiles": {n: p.public() for n, p in config.profiles.items()}, "sqlite_fts5": True,
              "model_checks": [] if offline else pipeline.preflight()}
    return report


def augment(config, store, limit):
    pipeline = Pipeline(config, store)
    if "AUGMENT" not in pipeline.prompts.prompts:
        raise ConfigError("Prompt document has no AUGMENT section")
    results = []
    cursor = store.connection.execute("SELECT id FROM units WHERE parent_id IS NULL ORDER BY id LIMIT ?", (limit,))
    for row in cursor:
        unit = store.get_unit(row[0])
        result = pipeline.call("AUGMENT", {"source": unit_payload(unit)}, schema("AUGMENT"), unit["id"], unit["id"])
        if leakage(result["issue_text"] + " " + result["query_text"]):
            raise AnnotationError("Synthetic query leaked a legal citation")
        evidence = []
        for quote in result["evidence_quotes"]:
            spans = quote_spans(unit["text"], quote)
            if len(spans) == 1:
                evidence.append(spans[0])
        ranking = store.search(result["query_text"], 20)
        results.append({"query_origin": "statute_conditioned", "quality": "synthetic_candidate", "split": "train",
                        "source_id": unit["id"], "model_output": result, "evidence": evidence,
                        "round_trip_source_retrieved": unit["id"] in {u["id"] for u in ranking},
                        "round_trip_scope": "lexical_diagnostic_not_Promptagator_training_or_gold_validation",
                        "run_hash": pipeline.run_hash})
    count = write_jsonl(config.output_dir / "synthetic_train.jsonl", results)
    return {"synthetic_candidates": count, "gold": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Vietnamese rule-recall dataset pipeline (court-citation anchored silver labels and explicit human gold gate)")
    parser.add_argument("--env", default=".env", help="Environment file; relative data paths resolve against its directory")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor"); p.add_argument("--offline", action="store_true")
    sub.add_parser("build-laws")
    sub.add_parser("build-cases")
    p = sub.add_parser("annotate"); p.add_argument("--retry-failed", action="store_true")
    p = sub.add_parser("audit", help="Deterministic case/citation funnel without model calls"); p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("run"); p.add_argument("--retry-failed", action="store_true"); p.add_argument("--skip-preflight", action="store_true")
    sub.add_parser("status")
    sub.add_parser("review-export")
    p = sub.add_parser("review-import"); p.add_argument("--file", type=Path, required=True); p.add_argument("--replace", action="store_true")
    p = sub.add_parser("export"); p.add_argument("--include-corpus", action="store_true")
    p = sub.add_parser("search"); p.add_argument("--query", required=True); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("read"); p.add_argument("--id", required=True)
    p = sub.add_parser("versions"); p.add_argument("--id", required=True)
    p = sub.add_parser("relations"); p.add_argument("--id", required=True)
    p = sub.add_parser("vision"); p.add_argument("--manifest", type=Path)
    p = sub.add_parser("augment"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("evaluate"); p.add_argument("--predictions", type=Path, required=True); p.add_argument("--k", type=int, default=20)
    p.add_argument("--labels", choices=["gold", "silver"], default="gold")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = None
    lock = None
    try:
        config = load_config(args.env)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        if args.command not in {"status", "search", "read", "versions", "relations", "evaluate", "audit"}:
            lock = FileLock(str(config.output_dir / ".pipeline.lock"))
            try:
                lock.acquire(timeout=0)
            except Timeout:
                raise ConfigError("Another pipeline writer is using this output directory") from None
        store = Store(config.db_path)
        cmd = args.command
        if cmd == "doctor":
            result = doctor(config, store, args.offline)
        elif cmd == "build-laws":
            build_laws(config, store); result = store.counts()
        elif cmd == "build-cases":
            build_cases(config, store); result = store.counts()
        elif cmd == "annotate":
            result = Pipeline(config, store).annotate(args.retry_failed)
        elif cmd == "audit":
            if args.limit <= 0:
                raise ValueError("Audit limit must be positive")
            result = Pipeline(config, store).audit(args.limit)
        elif cmd == "run":
            doctor(config, store, args.skip_preflight)
            build_laws(config, store)
            build_cases(config, store)
            result = Pipeline(config, store).annotate(args.retry_failed)
            result["review"] = export_review(config, store)
            result["export"] = export_dataset(config, store)
        elif cmd == "status":
            result = {"counts": store.counts(), "active_run": store.get_meta("active_run"),
                      "snapshot": store.snapshot(), "ingestion_complete": store.ingestion_complete(),
                      "artifacts": [dict(r) for r in store.connection.execute("SELECT * FROM artifacts ORDER BY path")]}
        elif cmd == "review-export":
            result = export_review(config, store)
        elif cmd == "review-import":
            result = import_reviews(args.file, config, store, args.replace)
        elif cmd == "export":
            result = export_dataset(config, store, args.include_corpus)
        elif cmd == "search":
            if not 1 <= args.limit <= 1000:
                raise ValueError("Search limit must be 1..1000")
            result = [unit_payload(u) for u in store.search(args.query, args.limit)]
        elif cmd == "read":
            result = store.get_unit(args.id)
            if result is None:
                raise ValueError("Unknown unit ID")
        elif cmd == "versions":
            result = store.versions(args.id)
        elif cmd == "relations":
            result = Pipeline(config, store).relations(args.id)
        elif cmd == "vision":
            result = Pipeline(config, store).vision(args.manifest)
        elif cmd == "augment":
            if args.limit <= 0:
                raise ValueError("Augment limit must be positive")
            result = augment(config, store, args.limit)
        elif cmd == "evaluate":
            result = evaluate(config.output_dir / "export", args.predictions, args.k, args.labels)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get("failed", 0):
            return 2
        return 0
    except (ConfigError, ReviewError, AnnotationError, ModelError, ValueError, OSError) as error:
        # All ModelErrors deliberately omit provider bodies/headers and credentials.
        print(f"ERROR {getattr(error, 'code', type(error).__name__)}: {error}", file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()
        if lock and lock.is_locked:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
