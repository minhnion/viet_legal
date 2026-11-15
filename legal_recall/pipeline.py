"""P1–P6 annotation with source isolation, validated evidence and resumable cases."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import logging
import re

from .client import BudgetError, ChatClient, ModelError, image_part
from .config import canonical, digest, file_hash
from .contracts import PromptBank, schema
from .store import write_jsonl
from .text import document_number, leakage, quote_spans, sentences, sha, window_text, timeline_mentions


LOG = logging.getLogger(__name__)


class AnnotationError(ValueError):
    pass


def validate_stage_output(stage, payload, output):
    if stage == "P1":
        expected = {s["id"] for s in payload["sentences"]}
        returned = [r["sentence_id"] for r in output["sentence_roles"]]
        if set(returned) != expected or len(returned) != len(expected) or not set(output["usable_fact_ids"]) <= expected:
            raise AnnotationError("P1 sentence coverage/IDs invalid")
    elif stage == "P2":
        allowed = {s["id"] for s in payload["facts"]}
        ids = [i["issue_id"] for i in output["issues"]]
        if len(ids) != len(set(ids)):
            raise AnnotationError("Duplicate issue IDs")
        for i in output["issues"]:
            if not i["fact_support_ids"] or not set(i["fact_support_ids"]) <= allowed or not i["issue_text"].strip() or not i["query_text"].strip() or leakage(i["issue_text"] + " " + i["query_text"]):
                raise AnnotationError("P2 grounding/leakage checks failed")
    elif stage == "P3":
        allowed = {u["candidate_id"] for u in payload["candidates"]}
        if not set(output["selected_candidate_ids"]) <= allowed or not set(output["support_sentence_ids"]) <= {s["id"] for s in payload["context"]}:
            raise AnnotationError("P3 source/context IDs invalid")
    elif stage == "P4":
        if output["candidate_id"] != payload["source"]["candidate_id"]:
            raise AnnotationError("P4 source ID invalid")
    elif stage == "P5":
        allowed = {s["source"]["candidate_id"] for s in payload["sources"]}
        ids = [g["group_id"] for g in output["proposed_requirement_groups"]]
        if len(ids) != len(set(ids)) or any(not g["candidate_ids"] or not set(g["candidate_ids"]) <= allowed for g in output["proposed_requirement_groups"]):
            raise AnnotationError("P5 group IDs invalid")
    elif stage == "P6":
        allowed = {s["source"]["candidate_id"] for s in payload["source_evidence"]}
        if any(item["candidate_id"] not in allowed for item in output["independent_relevance"]):
            raise AnnotationError("P6 source IDs invalid")


def unit_payload(unit, include_text=True):
    metadata = unit["source_metadata"]
    result = {"candidate_id": unit["id"], "instrument_number": unit.get("number"),
              "source_title": unit.get("source_title"), "article": unit.get("article"),
              "structure_flags": unit.get("flags", []),
              "version_status": metadata.get("version_status", "unverified"),
              "observed_effective_date": metadata.get("normalized_effective_date"),
              "source_note": metadata.get("source_note_text"),
              "source_url": metadata.get("url") or metadata.get("source_url")}
    if include_text:
        result["text"] = unit["text"]
    return result


class Pipeline:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.prompts = PromptBank(config.prompts_file, config.fewshot_file)
        self.clients = {name: ChatClient(p, config.output_dir / "cache", config.output_dir / "logs" / "model_calls.jsonl")
                        for name, p in config.profiles.items()}
        self.snapshot = store.snapshot()
        self.code_fingerprint = digest({p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))})
        self.run_hash = digest({"snapshot": self.snapshot, "prompts": self.prompts.version,
                               "code": self.code_fingerprint,
                               "profiles": {k: v.public() for k, v in config.profiles.items()},
                               "bindings": config.stage_profiles,
                               "policy": {k: getattr(config, k) for k in ["pool_size", "link_candidates", "max_issues", "max_citations", "max_source_windows", "max_expansion_rounds", "verifier_enabled", "sentence_chunk_size"]}})

    def client(self, stage):
        return self.clients[self.config.profile(stage).name]

    def call(self, stage, payload, output_schema, item_id):
        client = self.client(stage)
        messages = self.prompts.messages(stage, payload, output_schema, client.profile)
        return client.complete(stage, messages, output_schema,
                               {"run_hash": self.run_hash, "prompt_version": self.prompts.version, "item_id": item_id},
                               semantic_validator=lambda output: validate_stage_output(stage, payload, output))

    def fits(self, stage, payload, output_schema):
        client = self.client(stage)
        return client.fits(self.prompts.messages(stage, payload, output_schema, client.profile), output_schema)

    def preflight(self, include_vision=False):
        results = []
        seen = set()
        for stage, name in self.config.stage_profiles.items():
            if stage == "P0V" or name in seen:
                continue
            seen.add(name)
            client = self.clients[name]
            output_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
            messages = [{"role": client.profile.instruction_role, "content": "Return JSON {\"ok\": true}."},
                        {"role": "user", "content": "Synthetic connection test. No legal case data."}]
            output = client.complete("preflight", messages, output_schema, {"purpose": "synthetic_preflight"})
            if output != {"ok": True}:
                raise AnnotationError("Preflight returned unexpected content")
            results.append({"profile": name, "mode": client.profile.output_mode, "passed": True})
        if include_vision:
            if "P0V" not in self.config.stage_profiles:
                raise AnnotationError("Vision preflight requested without a P0V profile")
            # Use supplied manifest rather than assuming a provider accepts arbitrary image sizes.
            results.append({"profile": self.config.stage_profiles["P0V"], "vision": "requires vision command with a local test image"})
        return results

    def extract(self, case):
        indexed = sentences(case["text"])
        if not indexed:
            raise AnnotationError("No usable text sentences")
        role_map, fact_ids, notes = {}, set(), []
        chunks = []
        current = []
        for sentence in indexed:
            trial = current + [sentence]
            payload = {"sentences": trial, "target_instance": case["metadata"].get("instance_level"), "target_time": "before_target_court_decision"}
            contract = schema("P1", sentence_ids=[s["id"] for s in trial])
            if current and (len(trial) > self.config.sentence_chunk_size or not self.fits("P1", payload, contract)):
                chunks.append(current)
                overlap = [current[-1], sentence]
                overlap_payload = {"sentences": overlap, "target_instance": case["metadata"].get("instance_level"), "target_time": "before_target_court_decision"}
                if self.fits("P1", overlap_payload, schema("P1", sentence_ids=[s["id"] for s in overlap])):
                    current = overlap
                else:
                    current = [sentence]
                    notes.append("chunk_boundary_context_did_not_fit")
            else:
                current = trial
        if current:
            chunks.append(current)
        for i, chunk in enumerate(chunks):
            ids = {s["id"] for s in chunk}
            result = self.call("P1", {"sentences": chunk, "target_instance": case["metadata"].get("instance_level"),
                               "target_time": "before_target_court_decision"}, schema("P1", sentence_ids=sorted(ids)), f"{case['id']}:chunk:{i}")
            returned = [r["sentence_id"] for r in result["sentence_roles"]]
            if set(returned) != ids or len(returned) != len(ids) or not set(result["usable_fact_ids"]) <= ids:
                raise AnnotationError("P1 must classify each input sentence exactly once and use only supplied IDs")
            for r in result["sentence_roles"]:
                if r["sentence_id"] in role_map and role_map[r["sentence_id"]]["role"] != r["role"]:
                    notes.append("overlap_role_disagreement:" + r["sentence_id"])
                role_map[r["sentence_id"]] = r
            fact_ids.update(result["usable_fact_ids"])
            notes.extend(result["context_needed"] + result["uncertainties"])
        allowed_roles = {"facts_reported", "party_request", "procedural_history", "party_argument"}
        facts, removed = [], []
        for sentence in indexed:
            if sentence["id"] not in fact_ids:
                continue
            if (sentence["forbidden_section"] or sentence["explicit_legal_or_outcome"]
                    or role_map[sentence["id"]]["role"] not in allowed_roles):
                removed.append(sentence["id"])
            else:
                facts.append({**sentence, "attribution": role_map[sentence["id"]]})
        if not facts:
            raise AnnotationError("No facts survived source-role and leakage validation")
        return {"facts": facts, "sentence_roles": role_map, "uncertainties": notes,
                "removed_unsafe_fact_ids": removed, "all_sentences": indexed}

    def generate_queries(self, case, extraction):
        # Deliberately excludes judgment IDs/URLs, citations, reasoning and outcome.
        facts = [{"id": s["id"], "text": s["text"], "attribution": s["attribution"]} for s in extraction["facts"]]
        output = self.call("P2", {"facts": facts, "task": "find_legal_sources_for_one_issue",
                           "target_time": "before_target_court_decision"},
                           schema("P2", sentence_ids=[s["id"] for s in facts], max_issues=self.config.max_issues), case["id"])
        allowed = {s["id"] for s in facts}
        seen = set()
        for item in output["issues"]:
            if item["issue_id"] in seen:
                raise AnnotationError("P2 repeated an issue ID")
            seen.add(item["issue_id"])
            if not item["fact_support_ids"] or not set(item["fact_support_ids"]) <= allowed:
                raise AnnotationError("P2 issue has no valid fact evidence")
            if not item["issue_text"].strip() or not item["query_text"].strip():
                raise AnnotationError("P2 produced an empty issue/query")
            if leakage(item["issue_text"] + " " + item["query_text"]):
                raise AnnotationError("P2 output contains an explicit statute citation or court-outcome signal")
        return output["issues"]

    def resolve_citations(self, case, indexed):
        raw = case["citations"]
        citations = []
        for c in raw.get("regex", []):
            citations.append({"raw": c.get("ref") or "", "article": c.get("article"),
                              "law_name": c.get("law_name"), "number": document_number(c.get("id")), "spans": c.get("span") or []})
        for c in raw.get("structural", []):
            citations.append({"raw": c.get("raw") or "", "article": c.get("dieu"), "law_name": c.get("target"),
                              "number": document_number(c.get("target")), "spans": []})
        distinct = {}
        for c in citations:
            key = canonical([c["article"], c["law_name"], c["number"]])
            if key not in distinct:
                distinct[key] = c
        resolved, all_candidates = [], {}
        for index, c in enumerate(list(distinct.values())[:self.config.max_citations]):
            article = str(c["article"]).lower().lstrip("0") if c["article"] else None
            candidates = self.store.search(c["raw"] + " " + (c["law_name"] or ""), self.config.link_candidates,
                                           number=c["number"], article=article)
            if not candidates and c["number"]:
                candidates = self.store.search(c["raw"] + " " + (c["law_name"] or ""), self.config.link_candidates, article=article)
            for unit in candidates:
                all_candidates[unit["id"]] = unit
            if not candidates:
                resolved.append({"citation": c, "resolution_status": "not_found_in_candidates", "selected_candidate_ids": []})
                continue
            exact = [u for u in candidates if c["number"] and u["number"] == c["number"] and u["article"] == article]
            if len(exact) == 1:
                resolved.append({"citation": c, "resolution_status": "candidate_resolved", "selected_candidate_ids": [exact[0]["id"]],
                                 "method": "unique_number_and_article_unverified_version"})
                continue
            context_ids = set()
            for span in c["spans"]:
                if len(span) == 2 and isinstance(span[0], int) and isinstance(span[1], int):
                    context_ids.update(s["id"] for s in indexed if s["start"] < span[1] + 200 and s["end"] > span[0] - 200)
            if not context_ids and c["raw"]:
                # Locate rather than invent offsets for structural-parser citations.
                context_ids.update(s["id"] for s in indexed if c["raw"] in s["text"])
            context = [s for s in indexed if s["id"] in context_ids]
            payload = {"citation": c, "context": context,
                       "candidates": [{**unit_payload(u, False), "text_excerpt": u["text"][:1000],
                                       "excerpt_is_partial": len(u["text"]) > 1000} for u in candidates]}
            contract = schema("P3", sentence_ids=sorted(context_ids), candidate_ids=[u["id"] for u in candidates])
            try:
                result = self.call("P3", payload, contract, f"{case['id']}:citation:{index}")
                if not set(result["selected_candidate_ids"]) <= {u["id"] for u in candidates}:
                    raise AnnotationError("P3 selected a candidate outside its input")
                if not set(result["support_sentence_ids"]) <= context_ids:
                    raise AnnotationError("P3 cited unknown context sentences")
                resolved.append({"citation": c, **result, "method": "llm_candidate_link"})
            except BudgetError:
                resolved.append({"citation": c, "resolution_status": "ambiguous_version", "selected_candidate_ids": [], "reason": "context_budget"})
        return resolved, all_candidates, len(distinct) > self.config.max_citations

    def assess(self, query, unit):
        # Chunk long sources without silently treating an unread suffix as irrelevant.
        budget = max(128, self.client("P4").profile.max_input_chars // 4)
        windows = list(window_text(unit["text"], budget))
        if self.config.max_source_windows and len(windows) > self.config.max_source_windows:
            return {"candidate_id": unit["id"], "relevance_proposal": "uncertain", "version_status": "unresolved",
                    "evidence": [], "source_roles": [], "short_justification": "Source window limit exceeded; full source not assessed",
                    "flags": ["incomplete_source_read"], "window_assessments": []}
        outputs, evidence, flags = [], [], []
        for index, window in enumerate(windows):
            payload = {"query": query["model_input"], "source": unit_payload(unit, False),
                       "window": {**window, "window_index": index, "total_windows": len(windows)},
                       "parent_context": "Source is an article or codified extract; supplied window may require other windows."}
            result = self.call("P4", payload, schema("P4", candidate_ids=[unit["id"]]), f"{query['id']}:{unit['id']}:window:{index}")
            outputs.append(result)
            for quote in result["evidence_quotes"]:
                matches = quote_spans(window["text"], quote)
                if len(matches) != 1:
                    flags.append("quote_not_found" if not matches else "quote_ambiguous")
                    continue
                span = matches[0]
                evidence.append({"start": window["start"] + span["start"], "end": window["start"] + span["end"], "quote": quote})
        order = {"core_candidate": 3, "supporting": 2, "uncertain": 1, "not_relevant": 0}
        label = max((o["relevance_proposal"] for o in outputs), key=order.get)
        if label in {"core_candidate", "supporting"} and not evidence:
            flags.append("positive_without_valid_evidence")
            label = "uncertain"
        # All ingested version data are observations, not verified temporal assertions.
        version = "unresolved"
        if any(o["version_status"] == "contradicted" for o in outputs):
            flags.append("proposed_version_conflict")
        return {"candidate_id": unit["id"], "relevance_proposal": label, "version_status": version,
                "evidence": list({(e["start"], e["end"]): e for e in evidence}.values()),
                "source_roles": sorted({role for o in outputs for role in o["source_roles"]}),
                "short_justification": " ".join(o["short_justification"] for o in outputs),
                "flags": sorted(set(flags)), "window_assessments": outputs}

    def annotate_issue(self, case, issue, extraction, citation_results, citation_pool, citations_truncated):
        qid = "rr:" + digest([case["id"], issue["issue_id"], self.run_hash])[:24]
        facts = [{"sentence_id": s["id"], "text": s["text"], "speaker": s["attribution"]["speaker"],
                  "dispute_status": s["attribution"]["dispute_status"]} for s in extraction["facts"]]
        query = {"id": qid, "case_id": case["id"], "family_id": case["family_id"], "split": case["split"],
                 "run_hash": self.run_hash, "query_origin": "judgment_facts", "quality": "candidate",
                 "model_input": {"facts": facts, "issue": issue["issue_text"], "question": issue["query_text"],
                                 "timeline": timeline_mentions(facts),
                                 "fact_support_ids": issue["fact_support_ids"], "missing_facts": issue["missing_facts"],
                                 "answerability_proposal": issue["query_status"], "corpus_snapshot_id": self.snapshot},
                 "private_provenance": {"judgment_id": case["id"], "metadata": case["metadata"],
                                        "text_hash": case["text_hash"], "family_flags": case["family_flags"],
                                        "query_fact_spans": [{"sentence_id": s["id"], "start": s["start"], "end": s["end"]} for s in extraction["facts"]]},
                 "annotation": {"citations": citation_results, "extraction_uncertainties": extraction["uncertainties"],
                                "removed_unsafe_fact_ids": extraction["removed_unsafe_fact_ids"], "citations_truncated": citations_truncated}}
        lexical = self.store.search(issue["query_text"] + " " + issue["issue_text"], self.config.pool_size)
        selected = {cid for r in citation_results for cid in r.get("selected_candidate_ids", [])}
        pool = {}
        # Reserve half the pool for independently retrieved sources; do not let boilerplate exhaust it.
        for u in lexical[:max(1, self.config.pool_size // 2)]:
            pool[u["id"]] = u
        for cid in sorted(selected):
            if len(pool) >= self.config.pool_size:
                break
            if cid in citation_pool:
                pool[cid] = citation_pool[cid]
        for u in lexical:
            if len(pool) >= self.config.pool_size:
                break
            pool[u["id"]] = u
        assessments = {}
        set_review = {"proposed_requirement_groups": [], "redundant_sources": [], "unresolved_version_conflicts": [],
                      "missing_source_requests": [], "review_reasons": ["empty_pool"]}
        for expansion_round in range(self.config.max_expansion_rounds + 1):
            todo = [u for uid, u in sorted(pool.items()) if uid not in assessments]
            workers = self.client("P4").profile.concurrency
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for item in executor.map(lambda u: self.assess(query, u), todo):
                    assessments[item["candidate_id"]] = item
            if not assessments:
                break
            summaries = [{"source": unit_payload(pool[uid], False), "assessment": {
                k: a[k] for k in ["relevance_proposal", "version_status", "evidence", "flags"]}} for uid, a in sorted(assessments.items())]
            try:
                set_review = self.call("P5", {"query": query["model_input"], "sources": summaries,
                                       "scope": "assessed_pool_only"}, schema("P5", candidate_ids=list(pool)), f"{qid}:set:{expansion_round}")
            except BudgetError:
                set_review = {"proposed_requirement_groups": [], "redundant_sources": [], "unresolved_version_conflicts": [],
                              "missing_source_requests": [], "review_reasons": ["set_review_exceeds_context_budget"]}
            for group in set_review["proposed_requirement_groups"]:
                if not group["candidate_ids"] or not set(group["candidate_ids"]) <= pool.keys():
                    raise AnnotationError("P5 requirement group references invalid candidates")
            if expansion_round == self.config.max_expansion_rounds:
                break
            extra = []
            for request in set_review["missing_source_requests"][:3]:
                extra.extend(self.store.search(request, max(1, self.config.pool_size // 4)))
            for a in assessments.values():
                if a["relevance_proposal"] not in {"core_candidate", "supporting"}:
                    continue
                extra.extend(self.relations(a["candidate_id"], max(1, self.config.pool_size // 4)))
            cap = self.config.pool_size * (expansion_round + 2)
            for u in extra:
                if len(pool) >= cap:
                    break
                pool[u["id"]] = u
            if all(uid in assessments for uid in pool):
                break
        verification = None
        if self.config.verifier_enabled and assessments:
            # No previous labels/rationales: independent view first, comparison is deterministic.
            verifier_sources = []
            for uid, assessment in sorted(assessments.items()):
                verifier_sources.append({"source": unit_payload(pool[uid], False),
                                         "evidence_quotes": [e["quote"] for e in assessment["evidence"]]})
            try:
                verification = self.call("P6", {"query": query["model_input"], "source_evidence": verifier_sources,
                                         "scope": "provided_evidence_only_full_sources_not_supplied"}, schema("P6", candidate_ids=list(pool)), qid)
                verification["disagreements"] = [r["candidate_id"] for r in verification["independent_relevance"]
                    if r["candidate_id"] not in assessments or assessments[r["candidate_id"]]["relevance_proposal"] != r["relevance_proposal"]]
            except BudgetError:
                verification = {"review_reasons": ["verification_exceeds_context_budget"], "checks": [], "disagreements": []}
        query["annotation"].update({"assessments": list(assessments.values()), "set_review": set_review,
                                    "verification": verification, "pool_ids": sorted(pool), "pool_exhaustive": False,
                                    "review_required": True})
        return query

    def relations(self, uid, limit=10):
        unit = self.store.get_unit(uid)
        if not unit:
            return []
        result = {}
        for row in self.store.connection.execute("SELECT to_id FROM crosswalk WHERE from_id=? UNION SELECT from_id FROM crosswalk WHERE to_id=?", (uid, uid)):
            other = self.store.get_unit(row[0])
            if other:
                result[other["id"]] = other
        for c in unit["source_metadata"].get("citations") or []:
            article = c.get("dieu")
            number = document_number(c.get("target"))
            if number:
                for other in self.store.search("", limit, number=number, article=article):
                    result[other["id"]] = other
            if len(result) >= limit:
                break
        return list(result.values())[:limit]

    def annotate(self, retry_failed=False):
        if not self.store.get_meta("family_revision"):
            raise AnnotationError("Run build-cases before annotate")
        if not self.store.connection.execute("SELECT 1 FROM units LIMIT 1").fetchone():
            raise AnnotationError("No law units available; run build-laws first")
        for cid in self.config.case_ids:
            if not self.store.connection.execute("SELECT 1 FROM cases WHERE id=?", (cid,)).fetchone():
                raise AnnotationError("Requested case ID was not ingested; check RR_CASE_IDS and ingestion limits")
        with self.store.transaction():
            self.store.set_meta("active_run", self.run_hash)
            self.store.set_meta("active_snapshot", self.snapshot)
            self.store.set_meta("run:" + self.run_hash, {"config": self.config.public(), "prompt_hash": self.prompts.version,
                                                       "snapshot": self.snapshot, "code_fingerprint": self.code_fingerprint})
        done = self.store.connection.execute("SELECT count(*) FROM annotations WHERE run_hash=? AND status='complete'", (self.run_hash,)).fetchone()[0]
        selected, attempted, failed = 0, 0, 0
        cursor = self.store.connection.execute("SELECT * FROM cases ORDER BY id")
        for row in cursor:
            case = dict(row)
            case["metadata"] = json.loads(case["metadata"])
            if self.config.categories and case["metadata"].get("category") not in self.config.categories:
                continue
            if self.config.case_ids and case["id"] not in self.config.case_ids:
                continue
            selected += 1
            if self.config.annotation_limit and selected > self.config.annotation_limit:
                break
            prior = self.store.connection.execute("SELECT status FROM annotations WHERE case_id=? AND run_hash=?", (case["id"], self.run_hash)).fetchone()
            if prior and (prior[0] == "complete" or not retry_failed):
                continue
            case["family_flags"] = json.loads(case["family_flags"] or "[]")
            case["citations"] = json.loads(case["citations"])
            attempted += 1
            try:
                extraction = self.extract(case)
                issues = self.generate_queries(case, extraction)
                if not issues:
                    raise AnnotationError("No issue queries proposed; human fact/issue review required")
                links, candidates, truncated = self.resolve_citations(case, extraction["all_sentences"])
                queries = [self.annotate_issue(case, issue, extraction, links, candidates, truncated) for issue in issues]
                with self.store.transaction():
                    self.store.connection.execute("DELETE FROM queries WHERE case_id=? AND run_hash=?", (case["id"], self.run_hash))
                    for query in queries:
                        self.store.connection.execute("INSERT INTO queries VALUES (?,?,?,?)", (query["id"], case["id"], self.run_hash, canonical(query)))
                    self.store.connection.execute("INSERT INTO annotations VALUES (?,?,?,?,?) ON CONFLICT(case_id,run_hash) DO UPDATE SET status=excluded.status,result=excluded.result,error_code=NULL",
                                                   (case["id"], self.run_hash, "complete", canonical({"query_ids": [q["id"] for q in queries]}), None))
                LOG.info("Annotated case %s: %s issue queries", case["id"], len(queries))
            except (ModelError, AnnotationError, ValueError) as error:
                failed += 1
                code = getattr(error, "code", "annotation_validation")
                # Do not put arbitrary exception strings (possibly provider output) into persisted logs.
                with self.store.transaction():
                    self.store.failure("annotation", case["id"], code, "Case failed; inspect source and stage logs")
                    self.store.connection.execute("INSERT INTO annotations VALUES (?,?,?,?,?) ON CONFLICT(case_id,run_hash) DO UPDATE SET status=excluded.status,error_code=excluded.error_code",
                                                   (case["id"], self.run_hash, "failed", None, code))
                LOG.warning("Case %s failed: %s", case["id"], code)
                if self.config.fail_fast:
                    raise
        if not selected:
            raise AnnotationError("No judgments match the configured selection")
        return {"run_hash": self.run_hash, "previously_complete": done, "attempted": attempted, "failed": failed,
                "complete": self.store.connection.execute("SELECT count(*) FROM annotations WHERE run_hash=? AND status='complete'", (self.run_hash,)).fetchone()[0]}

    def vision(self, manifest=None):
        manifest = manifest or self.config.vision_manifest
        if not manifest:
            raise AnnotationError("Configure RR_VISION_MANIFEST or pass --manifest")
        manifest = Path(manifest).resolve()
        results = []
        client = self.client("P0V")
        for line in manifest.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            path = Path(row["image_path"]).expanduser()
            if not path.is_absolute():
                path = manifest.parent / path
            contract = schema("P0V")
            payload = {"page_id": row["page_id"], "existing_text": row.get("existing_text", "")}
            messages = self.prompts.messages("P0V", payload, contract, client.profile)
            messages[-1]["content"] = [{"type": "text", "text": canonical(payload)}, image_part(path)]
            result = client.complete("P0V", messages, contract,
                                     {"page_id": row["page_id"], "image_hash": file_hash(path), "prompt_hash": self.prompts.version}, image=True)
            results.append({"page_id": row["page_id"], "image_path": str(path), "image_hash": file_hash(path),
                            "model_proposal": result, "review_status": "unreviewed", "applied_to_corpus": False})
        write_jsonl(self.config.output_dir / "vision_review.jsonl", results)
        return {"pages": len(results), "applied_to_corpus": False}
