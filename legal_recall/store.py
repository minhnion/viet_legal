"""Transactional SQLite checkpoints, source provenance, lookup and lexical retrieval."""

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time

from .config import canonical, digest
from .text import fold, search_terms


DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(path TEXT PRIMARY KEY, corpus TEXT NOT NULL,
 hash TEXT NOT NULL, rows INTEGER NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
 complete INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, corpus TEXT NOT NULL,
 source_key TEXT NOT NULL, number TEXT, title TEXT, text TEXT NOT NULL,
 text_hash TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS source_number ON sources(number);
CREATE TABLE IF NOT EXISTS units(id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
 parent_id TEXT, article TEXT, clause TEXT, point TEXT, title TEXT,
 start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL, flags TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS unit_lookup ON units(source_id,article,parent_id);
CREATE VIRTUAL TABLE IF NOT EXISTS unit_fts USING fts5(id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS crosswalk(from_id TEXT NOT NULL, to_id TEXT NOT NULL,
 status TEXT NOT NULL, provenance TEXT NOT NULL, PRIMARY KEY(from_id,to_id));
CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY, text TEXT NOT NULL, text_hash TEXT NOT NULL,
 metadata TEXT NOT NULL, citations TEXT NOT NULL, family_id TEXT, split TEXT, family_flags TEXT);
CREATE INDEX IF NOT EXISTS cases_text_hash ON cases(text_hash);
CREATE TABLE IF NOT EXISTS annotations(case_id TEXT NOT NULL, run_hash TEXT NOT NULL,
 status TEXT NOT NULL, result TEXT, error_code TEXT, PRIMARY KEY(case_id,run_hash));
CREATE TABLE IF NOT EXISTS queries(id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
 run_hash TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS queries_run ON queries(run_hash);
CREATE TABLE IF NOT EXISTS reviews(query_id TEXT NOT NULL, reviewer_id TEXT NOT NULL,
 review_hash TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(query_id,reviewer_id));
CREATE TABLE IF NOT EXISTS review_events(id INTEGER PRIMARY KEY, query_id TEXT NOT NULL,
 reviewer_id TEXT NOT NULL, review_hash TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS failures(id INTEGER PRIMARY KEY, stage TEXT, item_id TEXT,
 error_code TEXT, message TEXT, created REAL);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=60)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript(DDL)
        version = self.get_meta("db_version")
        if version not in {None, "1"}:
            raise ValueError("Unsupported database version; use a new output directory")
        self.set_meta("db_version", "1")
        self.connection.commit()

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def get_meta(self, key):
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        self.connection.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, canonical(value)))

    def snapshot(self):
        artifacts = [dict(r) for r in self.connection.execute("SELECT path,corpus,hash,processed,complete FROM artifacts ORDER BY path")]
        return digest({"artifacts": artifacts, "expected_inputs": self.get_meta("expected_inputs"), "parser": self.get_meta("parser_version"),
                       "families": self.get_meta("family_revision")})

    def ingestion_complete(self):
        expected = self.get_meta("expected_inputs") or {}
        records = {r["path"]: bool(r["complete"]) for r in self.connection.execute("SELECT path,complete FROM artifacts")}
        return {corpus: bool(paths) and all(records.get(path, False) for path in paths) for corpus, paths in expected.items()}

    def add_source(self, record, units):
        old = self.connection.execute("SELECT text_hash FROM sources WHERE id=?", (record["id"],)).fetchone()
        if old:
            if old[0] != record["text_hash"]:
                raise ValueError("A source ID changed content; use a new output directory")
            return
        self.connection.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",
                                (record["id"], record["corpus"], record["source_key"], record.get("number"),
                                 record.get("title", ""), record["text"], record["text_hash"], canonical(record["metadata"])))
        self.connection.executemany("INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                    [(u["id"], record["id"], u["parent_id"], u["article"], u["clause"], u["point"],
                                      u["title"], u["start"], u["end"], u["text"], canonical(u["flags"])) for u in units])
        self.connection.executemany("INSERT INTO unit_fts(id,text) VALUES (?,?)",
                                    [(u["id"], fold(record.get("title", "") + " " + u["title"] + " " + u["text"]))
                                     for u in units if not u["parent_id"]])

    def get_unit(self, uid):
        row = self.connection.execute("""SELECT u.*,s.number,s.title source_title,s.corpus,s.metadata source_metadata,
            s.text_hash source_text_hash FROM units u JOIN sources s ON s.id=u.source_id WHERE u.id=?""", (uid,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["flags"] = json.loads(result["flags"])
        result["source_metadata"] = json.loads(result["source_metadata"])
        return result

    def search(self, query, limit=20, number=None, article=None):
        if number:
            rows = self.connection.execute("""SELECT u.id FROM units u JOIN sources s ON s.id=u.source_id
                WHERE s.number=? AND u.parent_id IS NULL AND (? IS NULL OR u.article=?) ORDER BY u.id LIMIT ?""",
                (number, article, article, limit)).fetchall()
            return [self.get_unit(r[0]) for r in rows]
        terms = search_terms(query)
        if not terms:
            return []
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        rows = self.connection.execute("SELECT id,bm25(unit_fts) score FROM unit_fts WHERE unit_fts MATCH ? ORDER BY score LIMIT ?",
                                       (expression, max(limit * 5, limit))).fetchall()
        result = []
        for row in rows:
            u = self.get_unit(row[0])
            if article is None or u["article"] == article:
                result.append(u)
                if len(result) == limit:
                    break
        return result

    def failure(self, stage, item_id, code, message):
        self.connection.execute("INSERT INTO failures(stage,item_id,error_code,message,created) VALUES (?,?,?,?,?)",
                                (stage, item_id, code, message, time.time()))

    def counts(self):
        return {table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ["sources", "units", "cases", "queries", "reviews", "failures"]}

    def versions(self, uid):
        unit = self.get_unit(uid)
        if not unit:
            return []
        rows = self.connection.execute("""SELECT u.id FROM units u JOIN sources s ON s.id=u.source_id
            WHERE s.number=? AND u.article=? AND u.parent_id IS NULL ORDER BY u.id""", (unit["number"], unit["article"])).fetchall()
        return [self.get_unit(r[0]) for r in rows]


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temp.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(canonical(row) + "\n")
            count += 1
    temp.replace(path)
    return count
