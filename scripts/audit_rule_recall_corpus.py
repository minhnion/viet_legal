#!/usr/bin/env python3
"""Audit local judgment metadata and a reproducible uniform sample, without APIs.

Requires pyarrow. Does not read embedding/reduction shards or publish case text.
Example: uv run --with pyarrow python scripts/audit_rule_recall_corpus.py
"""

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import random
import re

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("dataset/ban-an"))
    parser.add_argument("--output", type=Path, default=Path("docs/rule_recall_corpus_audit.json"))
    parser.add_argument("--sample-size", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261115)
    args = parser.parse_args()
    files = sorted(args.corpus.glob("documents-*.parquet"))
    if not files:
        parser.error("No documents-*.parquet files found")
    sizes = [pq.ParquetFile(p).metadata.num_rows for p in files]
    total = sum(sizes)
    if not 1 <= args.sample_size <= total:
        parser.error("sample-size must be between 1 and corpus row count")
    sampled = sorted(random.Random(args.seed).sample(range(total), args.sample_size))
    columns = ["doc_name", "category", "instance_level", "court_level", "year",
               "issued_date", "num_law_citations", "num_citations", "num_articles",
               "markdown_chars", "confidence", "precedent_number"]
    histograms = {k: Counter() for k in ["category", "instance_level", "court_level", "year"]}
    totals = Counter()
    date_years = Counter()
    valid_dates = []
    id_arrays = []
    sample_stats = Counter()
    sample_law_names = Counter()
    sample_citation_sources = Counter()
    sample_citation_kinds = Counter()
    sampled_ids = []
    manifest = []
    global_offset = 0
    for path, rows in zip(files, sizes):
        pf = pq.ParquetFile(path)
        table = pf.read(columns=columns)
        id_arrays.append(table["doc_name"])
        for field, counts in histograms.items():
            for item in pc.value_counts(table[field]).to_pylist():
                value = item["values"]
                counts["<null>" if value is None else str(value)] += item["counts"]
        for field in ["num_law_citations", "num_citations", "num_articles"]:
            totals[field + "_sum"] += pc.sum(table[field]).as_py() or 0
            totals[field + "_positive_rows"] += pc.sum(pc.greater(table[field], 0)).as_py() or 0
        for field in ["confidence", "precedent_number", "issued_date", "doc_name"]:
            totals[field + "_null_rows"] += table[field].null_count
        totals["markdown_chars_lt_500_rows"] += pc.sum(pc.less(table["markdown_chars"], 500)).as_py() or 0
        for value in table["issued_date"].to_pylist():
            if value is None:
                continue
            try:
                parsed = date.fromisoformat(value)
                valid_dates.append(parsed.isoformat())
                date_years[str(parsed.year)] += 1
                if parsed > date(2026, 11, 15):
                    totals["issued_date_after_2026_11_15_rows"] += 1
            except (ValueError, TypeError):
                totals["issued_date_invalid_calendar_rows"] += 1
        local_indices = [i - global_offset for i in sampled if global_offset <= i < global_offset + rows]
        sample_columns = ["doc_name", "markdown", "citations_law", "citations", "parent_acts"]
        offset = 0
        for batch in pf.iter_batches(batch_size=2048, columns=sample_columns):
            indices = [i - offset for i in local_indices if offset <= i < offset + batch.num_rows]
            offset += batch.num_rows
            if not indices:
                continue
            for row in batch.take(pa.array(indices, type=pa.int64())).to_pylist():
                sampled_ids.append(row["doc_name"])
                body = row["markdown"] or ""
                law = row["citations_law"] or []
                structural = row["citations"] or []
                sample_stats["rows"] += 1
                sample_stats["rows_with_parent_acts"] += bool(row["parent_acts"])
                sample_stats["rows_with_regex_citations"] += bool(law)
                sample_stats["rows_with_structural_citations"] += bool(structural)
                sample_stats["rows_with_reasoning_heading_regex"] += bool(re.search(r"NHẬN\s+ĐỊNH\s+CỦA\s+T[ÒO]A", body, re.I))
                sample_stats["rows_with_decision_heading_regex"] += bool(re.search(r"QUYẾT\s+ĐỊNH\s*[:：]", body, re.I))
                regex_articles = {c["article"] for c in law if c.get("article")}
                structural_articles = {c["dieu"] for c in structural if c.get("dieu")}
                sample_stats["rows_with_different_raw_article_number_sets"] += regex_articles != structural_articles
                for citation in law:
                    sample_stats["regex_citations"] += 1
                    if citation.get("article"):
                        sample_stats["regex_article_citations"] += 1
                        sample_stats["regex_article_missing_document_id"] += not bool(citation.get("id"))
                        sample_stats["regex_article_missing_year"] += citation.get("year") is None
                        sample_stats["regex_article_missing_law_name"] += not bool(citation.get("law_name"))
                        sample_law_names[citation.get("law_name") or "<null>"] += 1
                    spans = citation.get("span") or []
                    sample_stats["regex_citations_without_spans"] += not bool(spans)
                    for span in spans:
                        sample_stats["regex_span_count"] += 1
                        sample_stats["regex_span_invalid_bounds"] += not (
                            len(span) == 2 and all(isinstance(x, int) for x in span)
                            and 0 <= span[0] < span[1] <= len(body)
                        )
                for citation in structural:
                    sample_stats["structural_citations"] += 1
                    sample_citation_sources[citation.get("source") or "<null>"] += 1
                    sample_citation_kinds[citation.get("kind") or "<null>"] += 1
                    if citation.get("dieu"):
                        sample_stats["structural_article_citations"] += 1
                        sample_stats["structural_article_missing_target"] += not bool(citation.get("target"))
        manifest.append({"file": path.name, "rows": rows, "bytes": path.stat().st_size,
                         "sampled_rows": len(local_indices)})
        global_offset += rows
        print(f"Audited {path.name}: {rows:,} rows, sampled {len(local_indices)}", flush=True)
    all_ids = pa.chunked_array([chunk for a in id_arrays for chunk in a.chunks])
    unique_ids = pc.count_distinct(all_ids).as_py()
    totals["unique_nonnull_doc_names"] = unique_ids
    totals["duplicate_nonnull_doc_name_rows"] = total - totals["doc_name_null_rows"] - unique_ids
    assert sample_stats["rows"] == args.sample_size
    report = {
        "audit_date": "2026-11-15", "corpus_path": str(args.corpus),
        "pyarrow_version": pa.__version__, "documents": total,
        "files": manifest, "full_scalar_metadata_counts": dict(totals),
        "full_metadata_histograms": {k: dict(v.most_common()) for k, v in histograms.items()},
        "parsed_issue_date_years": dict(sorted(date_years.items())),
        "parsed_issue_date_min": min(valid_dates) if valid_dates else None,
        "parsed_issue_date_max": max(valid_dates) if valid_dates else None,
        "sample_method": "Uniform sample without replacement over global row indices in sorted document shards",
        "sample_size": args.sample_size, "sample_seed": args.seed,
        "sample_counts": dict(sample_stats),
        "sample_structural_sources": dict(sample_citation_sources.most_common()),
        "sample_structural_kinds": dict(sample_citation_kinds.most_common()),
        "sample_top_30_regex_law_names": dict(sample_law_names.most_common(30)),
        "sample_doc_names": sampled_ids,
        "limitations": [
            "Full corpus results cover scalar metadata only; nested citations and text were sampled.",
            "Article-number set differences ignore law identity and are parser diagnostics, not error rates.",
            "Valid span bounds do not establish correct citation meaning or law association.",
            "Heading regex hit counts do not establish correct section boundaries.",
            "File bytes and row counts are provenance hints, not content hashes or an upstream revision lock.",
            "No human legal annotation, source PDF audit, or cross-corpus citation resolution was performed.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ["documents", "full_scalar_metadata_counts", "sample_counts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
