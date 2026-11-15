#!/usr/bin/env python3
"""Count local legal-source metadata and codification source-note coverage.

Requires pyarrow. Reads documents only, makes no network or model calls.
Source-note patterns produce diagnostics, not verified legal crosswalks.
"""

import argparse
from collections import Counter
from pathlib import Path
import json
import re

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


def nonblank(column):
    strings = pc.cast(column, pa.string())
    return pc.sum(pc.fill_null(pc.not_equal(pc.utf8_trim_whitespace(strings), ""), False)).as_py() or 0


def histogram(column):
    return {"<null>" if x["values"] is None else str(x["values"]): x["counts"]
            for x in pc.value_counts(column).to_pylist()}


def audit_tvpl(root):
    files = sorted(root.glob("documents-*.parquet"))
    if not files:
        raise ValueError(f"No document shards at {root}")
    counts = Counter()
    types, sections = Counter(), Counter()
    ids = set()
    fields = ["doc_number", "issue_date", "effective_date", "status", "pdf_url"]
    columns = ["id", "doc_type", "section", "is_english", "vn_chars", "num_articles"] + fields
    for path in files:
        table = pq.read_table(path, columns=columns)
        counts["rows"] += table.num_rows
        for field in fields:
            counts[field + "_nonblank_rows"] += nonblank(table[field])
        counts["metadata_vn_chars_positive_rows"] += pc.sum(pc.greater(table["vn_chars"], 0)).as_py() or 0
        counts["metadata_num_articles_positive_rows"] += pc.sum(pc.greater(table["num_articles"], 0)).as_py() or 0
        counts["is_english_true_rows"] += pc.sum(table["is_english"]).as_py() or 0
        counts["id_null_rows"] += table["id"].null_count
        types.update(histogram(table["doc_type"]))
        sections.update(histogram(table["section"]))
        ids.update(x for x in table["id"].to_pylist() if x is not None)
    counts["unique_nonnull_ids"] = len(ids)
    counts["duplicate_nonnull_id_rows"] = counts["rows"] - counts["id_null_rows"] - len(ids)
    return {"path": str(root), "document_shards": len(files), "counts": dict(counts),
            "doc_types": dict(types.most_common()), "sections": dict(sections.most_common()),
            "scope": "All document rows; scalar metadata only; text completeness not independently checked"}


def audit_phapdien(root):
    files = sorted(root.glob("documents-*.parquet"))
    if not files:
        raise ValueError(f"No document shards at {root}")
    counts = Counter()
    record_ids, article_ids, composite_ids = set(), set(), set()
    example = None
    columns = ["record_id", "subject_id", "article_id", "source_note_text", "source_links", "content_char_len"]
    for path in files:
        for row in pq.read_table(path, columns=columns).to_pylist():
            counts["rows"] += 1
            content = (row["content_char_len"] or 0) > 0
            counts["metadata_content_positive_rows"] += content
            counts["metadata_content_empty_or_null_rows"] += not content
            for field in ["record_id", "article_id"]:
                counts[field + "_null_rows"] += row[field] is None
            if row["record_id"] is not None:
                record_ids.add(row["record_id"])
            if row["article_id"] is not None:
                article_ids.add(row["article_id"])
            if row["subject_id"] is not None and row["article_id"] is not None:
                composite_ids.add((row["subject_id"], row["article_id"]))
            else:
                counts["composite_key_incomplete_rows"] += 1
            note = row["source_note_text"] or ""
            links = row["source_links"] or []
            counts["source_note_nonblank_rows"] += bool(note.strip())
            counts["source_links_nonempty_rows"] += bool(links)
            counts["note_with_instrument_number_pattern_rows"] += bool(re.search(r"\b\d{1,5}/\d{4}/[\wĐđ-]+", note))
            counts["note_with_original_article_prefix_rows"] += bool(re.search(r"^\s*\(?\s*Điều\s+\d+[a-zđ]?\b", note, re.I))
            counts["note_with_effective_date_phrase_rows"] += "có hiệu lực" in note.lower()
            counts["rows_with_vbpl_itemid_link"] += any(
                "vbpl.vn/" in (link.get("href") or "").lower()
                and "itemid=" in (link.get("href") or "").lower() for link in links)
            if example is None and content and links:
                example = {k: row[k] for k in ["record_id", "article_id", "source_note_text", "source_links"]}
    for field, values in [("record_id", record_ids), ("article_id", article_ids)]:
        counts["unique_nonnull_" + field + "s"] = len(values)
        counts["duplicate_nonnull_" + field + "_rows"] = counts["rows"] - counts[field + "_null_rows"] - len(values)
    counts["unique_complete_subject_article_keys"] = len(composite_ids)
    counts["duplicate_complete_subject_article_key_rows"] = counts["rows"] - counts["composite_key_incomplete_rows"] - len(composite_ids)
    return {"path": str(root), "document_shards": len(files), "counts": dict(counts),
            "source_note_example": example,
            "scope": "All document rows; source-note regex matches are unverified extraction candidates"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("dataset/nguon-luat"))
    parser.add_argument("--output", type=Path, default=Path("docs/rule_recall_legal_sources_audit.json"))
    args = parser.parse_args()
    report = {
        "pyarrow_version": pa.__version__,
        "tvpl": audit_tvpl(args.root / "thuvienphapluat-vn-vbpl"),
        "phapdien": audit_phapdien(args.root / "phapdien-moj-gov-vn"),
        "limitations": [
            "No judgment-to-statute resolution, temporal validity checking, or legal annotation was performed.",
            "Positive length and article counts are publisher metadata, not independent full-text quality checks.",
            "Source-note patterns and source links do not prove correctness of a version or cross-corpus mapping.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for name in ["tvpl", "phapdien"]:
        print(name, json.dumps(report[name]["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
