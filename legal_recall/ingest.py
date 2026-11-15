"""Streaming Parquet ingestion, resumable batches and conservative case-family grouping."""

from collections import defaultdict
from pathlib import Path
import csv
import json
import logging
import re

import pyarrow.parquet as pq

from .config import canonical, digest, file_hash
from .text import PARSER_VERSION, document_number, fold, header_number, iso_date, legal_units, normalize, sha


LOG = logging.getLogger(__name__)


def document_files(path):
    files = sorted(path.glob("documents-*.parquet")) if path.is_dir() else ([path] if path.suffix == ".parquet" and path.is_file() else [])
    if not files:
        raise ValueError(f"No document Parquet shards found at {path}")
    return files


def rows(store, corpus, root, limit, columns, transform):
    """Commit input rows and checkpoint together; limits count scanned rows, not retained rows."""
    total_processed = store.connection.execute("SELECT coalesce(sum(processed),0) FROM artifacts WHERE corpus=?", (corpus,)).fetchone()[0]
    for path in document_files(root):
        pf = pq.ParquetFile(path)
        artifact_id = f"{corpus}:{path.name}"
        old = store.connection.execute("SELECT * FROM artifacts WHERE path=?", (artifact_id,)).fetchone()
        signature = file_hash(path)
        if old and old["hash"] != signature:
            raise ValueError(f"Source shard changed: {path.name}; use a new output directory")
        if old and old["complete"]:
            continue
        if limit and total_processed >= limit:
            break
        if not old:
            with store.transaction():
                store.connection.execute("INSERT INTO artifacts(path,corpus,hash,rows) VALUES (?,?,?,?)",
                                         (artifact_id, corpus, signature, pf.metadata.num_rows))
        processed = old["processed"] if old else 0
        offset = 0
        selected_columns = [c for c in columns if c in pf.schema_arrow.names]
        for batch in pf.iter_batches(batch_size=128, columns=selected_columns):
            end = offset + batch.num_rows
            if end <= processed:
                offset = end
                continue
            start = max(0, processed - offset)
            take = batch.num_rows - start
            if limit:
                take = min(take, limit - total_processed)
            if take <= 0:
                break
            with store.transaction():
                for i, row in enumerate(batch.slice(start, take).to_pylist(), offset + start):
                    transform(row, {"file": path.name, "row": i, "file_hash": signature})
                processed = offset + start + take
                store.connection.execute("UPDATE artifacts SET processed=?,complete=? WHERE path=?",
                                         (processed, int(processed == pf.metadata.num_rows), artifact_id))
            total_processed += take
            offset = end
            if limit and total_processed >= limit:
                break
        LOG.info("%s %s: %s/%s input rows", corpus, path.name, processed, pf.metadata.num_rows)


def build_laws(config, store):
    prior = store.get_meta("parser_version")
    if prior not in {None, PARSER_VERSION}:
        raise ValueError("Parser version changed; use a new output directory")
    with store.transaction():
        store.set_meta("parser_version", PARSER_VERSION)
        expected = store.get_meta("expected_inputs") or {}
        for corpus, root in [("tvpl", config.tvpl_path), ("phapdien", config.phapdien_path)]:
            expected[corpus] = [f"{corpus}:{p.name}" for p in document_files(root)]
        store.set_meta("expected_inputs", expected)

    def tvpl(row, location):
        text = normalize(row.get("vn_text"))
        if not text.strip():
            return
        key = str(row.get("id") or digest(location))
        sid = "tvpl:" + key
        number = document_number(row.get("doc_number")) or header_number(text)
        metadata = {k: row.get(k) for k in ["doc_number", "doc_type", "title", "url", "pdf_url", "issue_date", "effective_date", "status", "issuer", "is_english", "citations"]}
        metadata.update({"location": location, "raw_text_hash": sha(row.get("vn_text") or ""),
                         "number_source": "metadata" if document_number(row.get("doc_number")) else "header_or_unknown",
                         "normalized_issue_date": iso_date(row.get("issue_date")),
                         "normalized_effective_date": iso_date(row.get("effective_date")),
                         "version_status": "unverified"})
        store.add_source({"id": sid, "source_key": key, "corpus": "tvpl", "number": number,
                          "title": row.get("title") or "", "text": text, "text_hash": sha(text), "metadata": metadata}, legal_units(text, sid))

    def phapdien(row, location):
        text = normalize(row.get("content_text"))
        if not text.strip():
            return
        key = str(row.get("record_id") or digest(location))
        sid = "phapdien:" + key
        note = row.get("source_note_text") or ""
        m = re.match(r"\s*\(?\s*Điều\s+(\d+[a-zđ]?)\b", note, re.I)
        article = (m.group(1).lstrip("0") or "0") if m else None
        # Number is a candidate, never a verified version: a note may mention amendments too.
        number = document_number(note)
        metadata = {k: row.get(k) for k in ["article_id", "subject_id", "source_note_text", "source_links", "related_note_text", "source_url", "scraped_at"]}
        metadata.update({"location": location, "raw_text_hash": sha(row.get("content_text") or ""),
                         "version_status": "unverified", "source_number_candidates": [document_number(x.group()) for x in re.finditer(r"\d{1,5}/\d{4}/[\wĐđ-]+", note)]})
        unit = {"id": f"{sid}@{sha(text)[:24]}:a{article or 'unknown'}:0", "parent_id": None, "article": article,
                "clause": None, "point": None, "title": row.get("article_title") or "",
                "start": 0, "end": len(text), "text": text, "flags": ["codified_extract", "source_version_unverified"]}
        store.add_source({"id": sid, "source_key": key, "corpus": "phapdien", "number": number,
                          "title": row.get("article_title") or "", "text": text, "text_hash": sha(text), "metadata": metadata}, [unit])

    rows(store, "tvpl", config.tvpl_path, config.law_max_records,
         ["id", "vn_text", "doc_number", "doc_type", "title", "url", "pdf_url", "issue_date", "effective_date", "status", "issuer", "is_english", "citations"], tvpl)
    rows(store, "phapdien", config.phapdien_path, config.law_max_records,
         ["record_id", "content_text", "article_id", "article_title", "subject_id", "source_note_text", "source_links", "related_note_text", "source_url", "scraped_at"], phapdien)
    with store.transaction():
        store.connection.execute("DELETE FROM crosswalk")
        cursor = store.connection.execute("SELECT u.id,u.article,s.number,s.metadata FROM units u JOIN sources s ON s.id=u.source_id WHERE s.corpus='phapdien'")
        for row in cursor:
            if not row["number"] or not row["article"]:
                continue
            candidates = store.search("", 100, number=row["number"], article=row["article"])
            for target in candidates:
                if target["corpus"] == "tvpl":
                    store.connection.execute("INSERT OR IGNORE INTO crosswalk VALUES (?,?,?,?)", (row["id"], target["id"], "candidate_only",
                                             canonical({"method": "number_and_original_article", "source_note": json.loads(row["metadata"]).get("source_note_text")})))
    LOG.info("Law sources and units: %s", store.counts())


def build_cases(config, store):
    with store.transaction():
        expected = store.get_meta("expected_inputs") or {}
        expected["judgments"] = [f"judgments:{p.name}" for p in document_files(config.judgment_path)]
        store.set_meta("expected_inputs", expected)
    def transform(row, location):
        text = normalize(row.get("markdown"))
        if not text.strip():
            return
        cid = str(row.get("doc_name") or digest(location))
        metadata = {k: row.get(k) for k in ["category", "court", "court_level", "instance_level", "official_document_id", "official_document_id_normalized", "issued_date", "web_url", "pdf_url", "citations_case"]}
        metadata.update({"location": location, "raw_text_hash": sha(row.get("markdown") or ""),
                         "layout_normalized_hash": sha(" ".join(text.lower().split()))})
        citations = {"regex": row.get("citations_law") or [], "structural": row.get("citations") or []}
        old = store.connection.execute("SELECT text_hash FROM cases WHERE id=?", (cid,)).fetchone()
        if old and old[0] != sha(text):
            raise ValueError("Judgment ID changed text; use a new output directory")
        store.connection.execute("INSERT OR IGNORE INTO cases VALUES (?,?,?,?,?,?,?,?)",
                                 (cid, text, sha(text), canonical(metadata), canonical(citations), None, None, "[]"))
    rows(store, "judgments", config.judgment_path, config.case_max_records,
         ["doc_name", "markdown", "category", "court", "court_level", "instance_level", "official_document_id", "official_document_id_normalized", "issued_date", "web_url", "pdf_url", "citations_case", "citations_law", "citations"], transform)
    build_families(config, store)


def judgment_key(value):
    value = fold(value or "")
    m = re.search(r"(\d+)\s*/\s*(\d{4})\s*/\s*([a-z-]+)", value)
    return f"{int(m.group(1))}/{m.group(2)}/{re.sub('[^a-z]', '', m.group(3))}" if m else None


def build_families(config, store):
    parent, flags = {}, defaultdict(set)

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        x, y = find(x), find(y)
        parent[max(x, y)] = min(x, y)

    hashes, layout_hashes, identities = {}, {}, defaultdict(list)
    for row in store.connection.execute("SELECT id,text_hash,metadata FROM cases ORDER BY id"):
        cid = row["id"]
        find(cid)
        if row["text_hash"] in hashes:
            union(cid, hashes[row["text_hash"]])
        hashes[row["text_hash"]] = cid
        meta = json.loads(row["metadata"])
        layout_hash = meta.get("layout_normalized_hash")
        if layout_hash:
            if layout_hash in layout_hashes:
                union(cid, layout_hashes[layout_hash])
                flags[cid].add("layout_near_duplicate")
            layout_hashes[layout_hash] = cid
        key = judgment_key(meta.get("official_document_id_normalized") or meta.get("official_document_id"))
        if key:
            identities[key].append(cid)
    if config.family_map:
        with config.family_map.open(encoding="utf-8", newline="") as stream:
            mapping = list(csv.DictReader(stream))
        groups = {}
        assigned = {}
        for item in mapping:
            cid, family = item.get("case_id"), item.get("family_id")
            if cid not in parent or not family:
                raise ValueError("Family CSV must contain known case_id and nonempty family_id")
            if cid in assigned and assigned[cid] != family:
                raise ValueError("A case cannot have conflicting family IDs in the mapping CSV")
            assigned[cid] = family
            if family in groups:
                union(cid, groups[family])
            groups[family] = cid
    for row in store.connection.execute("SELECT id,metadata FROM cases ORDER BY id"):
        meta = json.loads(row["metadata"])
        for citation in meta.get("citations_case") or []:
            key = judgment_key(citation.get("id"))
            if key and len(identities.get(key, [])) == 1:
                target = identities[key][0]
                union(row["id"], target)
                flags[row["id"]].add("reference_link_requires_family_review")
            elif key:
                flags[row["id"]].add("ambiguous_or_external_case_reference")
    members = defaultdict(list)
    for cid in parent:
        members[find(cid)].append(cid)
    with store.transaction():
        for ids in members.values():
            family = "family:" + min(ids)
            bucket = int(digest({"family": family, "seed": config.seed})[:16], 16) / 16 ** 16
            split = "train" if bucket < config.train_ratio else "dev" if bucket < config.train_ratio + config.dev_ratio else "test"
            combined = sorted({f for cid in ids for f in flags[cid]} | {"heuristic_family_requires_human_check"})
            store.connection.executemany("UPDATE cases SET family_id=?,split=?,family_flags=? WHERE id=?",
                                         [(family, split, canonical(combined), cid) for cid in ids])
        revision = digest({"families": sorted((cid, find(cid)) for cid in parent),
                           "seed": config.seed, "train": config.train_ratio, "dev": config.dev_ratio,
                           "family_map_hash": file_hash(config.family_map) if config.family_map else None})
        store.set_meta("family_revision", revision)
    LOG.info("Grouped %s judgments into %s conservative families; human family review remains required", len(parent), len(members))
