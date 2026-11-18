"""Court-citation anchored annotation: one rule-recall query per legal issue the court actually decided.

Per case, code splits the judgment into facts / reasoning / decision, extracts the court's citations and resolves
each to an article unit (instrument name or number, cited year or judgment date -> version). The ISSUE call groups
the substantive citations into issues and writes a facts-only research question; the EVIDENCE call confirms each
cited article for its issue and picks the supporting clauses. Code then builds qrels, requirement groups (with
equivalent copies and cross-version alternatives) and evidence offsets, and grades the query silver or needs_review.
"""

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import json
import logging
import re

from .citations import ActIndex, fold_char_string, judgment_date, mentions, role, sections, stream
from .client import ChatClient, LangfuseTracer, ModelError, image_part
from .config import canonical, digest, file_hash
from .contracts import PromptBank, schema
from .store import write_jsonl
from .text import document_number, leakage, sentences, timeline_mentions


LOG = logging.getLogger(__name__)


class AnnotationError(ValueError):
    code = "annotation_validation"


class Skip(Exception):
    """A case that cannot yield a query by construction (no facts section, no resolvable substantive citation)."""

    def __init__(self, reason, stats=None):
        super().__init__(reason)
        self.reason, self.stats = reason, stats or {}


# Model-quality failures of one call. In ISSUE they fail the case; in EVIDENCE they degrade one source to a
# fallback with low confidence. Every other client error (transport, HTTP, tokenize, protocol, refusal) is an
# infrastructure/configuration problem: the run stops and untouched cases are picked up by the next run.
SOFT_MODEL_ERRORS = {"context_budget", "incomplete", "schema", "semantic_validation"}

# Research questions must not name the instrument either ("Bộ luật dân sự", "Luật Đất đai", "BLHS").
INSTRUMENT_RE = re.compile(r"(?i)\b(?:bộ\s+luật|nghị\s+quyết|nghị\s+định|thông\s+tư|pháp\s+lệnh|hiến\s+pháp|blds|blhs|blttds|blttths|blld)\b"
                           r"|(?<!pháp )\bluật\b")
REPAIR_NOTES = ("Lần trả lời trước bị loại vì issue_text/query_text lộ đáp án. Viết lại: không ghi số Điều, khoản, điểm, "
                "số hiệu hay tên văn bản (kể cả 'Bộ luật ...', 'Luật ...', 'Nghị quyết ...'); không nêu kết quả hay nhận "
                "định của Tòa; câu hỏi chỉ dựa trên facts được cung cấp.")
CONFIDENCE = {"high": 2, "medium": 1, "low": 0}
METHOD_CONFIDENCE = {"instrument_number": "high", "name_and_year": "high", "year_from_same_judgment": "medium", "in_force_on_judgment_date": "medium",
                     "only_act_with_name": "medium", "instrument_number_loose": "medium", "instrument_number_phapdien": "medium"}


def leaks(text):
    return bool(leakage(text) or INSTRUMENT_RE.search(text))


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


def trimmed(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def clause_options(unit, clauses, cid):
    """Selectable evidence spans of an article, as offsets into unit.text: its clauses, or the article body."""
    text, options = unit["text"], []
    for clause in clauses:
        start, end = trimmed(text, clause["start"] - unit["start"], clause["end"] - unit["start"])
        if start < end:
            options.append({"id": f"{cid}.k{clause['clause']}", "clause": clause["clause"], "start": start, "end": end})
    if not options:
        start, end = trimmed(text, body_start(unit), len(text))
        if start >= end:
            start, end = trimmed(text, 0, len(text))
        options.append({"id": f"{cid}.all", "clause": None, "start": start, "end": end})
    return options


def body_start(unit):
    """Offset after the article heading ("Điều\n463. Hợp đồng vay tài sản"), and after the title line when the
    title sits on its own line ("Điều 194.\nTội ..."); Pháp điển copies carry the body only."""
    text = unit["text"]
    m = re.match(r"\s*(?:\*\*|#{1,6}\s*)?Điều\s*\d+[a-zđ]?\s*[.:]?[ \t]*([^\n]*)\n?", text, re.I)
    if not m:
        return 0
    if m.group(1).strip():
        return m.end()
    title = re.match(r"[ \t]*[^\n]*\n?", text[m.end():])
    return m.end() + title.end() if text[m.end():].strip() else m.end()


def locate(haystack, needle):
    """Span of needle in haystack ignoring whitespace, case and diacritics (copies differ in line breaks)."""
    hay, hay_index = stream(haystack)
    pin = stream(needle)[0]
    pos = hay.find(pin) if pin else -1
    return {"start": hay_index[pos], "end": hay_index[pos + len(pin) - 1] + 1} if pos >= 0 else None


def article_title(unit):
    """Article heading, or its first body line when the heading sits on its own line ("Điều 194.\nTội ...")."""
    if unit["title"].strip():
        return unit["title"].strip()
    lines = [l.strip() for l in unit["text"].split("\n")[1:] if l.strip()]
    return lines[0][:160] if lines else ""


def same_text(a, b):
    return stream(a["text"])[0] == stream(b["text"])[0]


class Pipeline:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.prompts = PromptBank(config.prompts_file, config.fewshot_file)
        self.tracer = (LangfuseTracer(config.langfuse_host, config.langfuse_public_key, config.langfuse_secret_key,
                                      config.langfuse_capture_io) if config.langfuse_host else None)
        self.clients = {name: ChatClient(p, config.output_dir / "cache", config.output_dir / "logs" / "model_calls.jsonl", self.tracer)
                        for name, p in config.profiles.items()}
        self.snapshot = store.snapshot()
        self.code_fingerprint = digest({p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))})
        self.run_hash = digest({"snapshot": self.snapshot, "prompts": self.prompts.version,
                                "code": self.code_fingerprint,
                                "profiles": {k: v.public() for k, v in config.profiles.items()},
                                "bindings": config.stage_profiles,
                                "policy": {k: getattr(config, k) for k in ["max_issues", "stage_max_output_tokens"]}})
        self._acts = None

    @property
    def acts(self):
        if self._acts is None:
            self._acts = ActIndex(self.store.connection.execute("SELECT * FROM acts"))
        return self._acts

    def client(self, stage):
        return self.clients[self.config.profile(stage).name]

    def call(self, stage, payload, output_schema, item_id, case_id, validator=None):
        client = self.client(stage)
        messages = self.prompts.messages(stage, payload, output_schema, client.profile)
        trace = {"trace_key": f"{self.run_hash}:{case_id}", "trace_name": f"case {case_id}", "case_id": case_id,
                 "item_id": str(item_id), "session_id": self.run_hash}
        return client.complete(stage, messages, output_schema,
                               {"run_hash": self.run_hash, "prompt_version": self.prompts.version, "item_id": item_id},
                               semantic_validator=validator, trace=trace,
                               max_output_tokens=self.config.stage_max_output_tokens.get(stage))

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
            output = client.complete("preflight", messages, output_schema, {"purpose": "synthetic_preflight"},
                                     trace={"trace_key": f"preflight:{name}:{self.run_hash}", "trace_name": f"preflight {name}",
                                            "session_id": self.run_hash})
            if output != {"ok": True}:
                raise AnnotationError("Preflight returned unexpected content")
            results.append({"profile": name, "mode": client.profile.output_mode, "passed": True})
        if include_vision:
            if "P0V" not in self.config.stage_profiles:
                raise AnnotationError("Vision preflight requested without a P0V profile")
            # Use supplied manifest rather than assuming a provider accepts arbitrary image sizes.
            results.append({"profile": self.config.stage_profiles["P0V"], "vision": "requires vision command with a local test image"})
        return results

    # ---- deterministic preparation (main thread: all database reads happen here) ----

    def resolve(self, mention, on_date, document_years=None):
        ref, article = mention["act_ref"], mention["article"]
        if not ref:
            return {"status": "no_instrument"}
        if ref.get("unparsed"):
            return {"status": "unrecognized_instrument", "doc_type": ref["doc_type"]}
        if ref.get("number_key"):
            acts, method = self.acts.by_number(ref["number_key"], ref["doc_type"])
            acts = [a for a in acts if a["doc_type"] == ref["doc_type"]] or acts
            if not acts:
                copies = self.store.number_units(ref["number"], article, "phapdien")
                if copies:
                    return {"status": "resolved", "unit_id": copies[0]["id"], "act_id": copies[0]["source_id"], "number": ref["number"],
                            "doc_type": ref["doc_type"], "name_key": None, "method": "instrument_number_phapdien", "flags": []}
                return {"status": "act_not_in_corpus", "number": ref["number"]}
            act, flags = acts[0], ([] if len(acts) == 1 else ["several_acts_same_number"])
            number = ref["number"]  # the clean cited number also finds Pháp điển copies
        else:
            year, inferred = ref.get("year"), []
            if not year:
                # A bare "Bộ luật hình sự" means the version this judgment names elsewhere, when it names exactly one.
                years = (document_years or {}).get((ref["doc_type"], ref["name_key"]), set())
                if len(years) == 1:
                    year, inferred = next(iter(years)), ["year_from_same_judgment"]
                elif len(years) > 1:
                    inferred = ["several_versions_cited_in_judgment"]
            act, method, flags = self.acts.pick(ref["doc_type"], ref["name_key"], year, on_date)
            if not act:
                return {"status": method, "flags": flags + inferred}
            if inferred == ["year_from_same_judgment"] and method == "name_and_year":
                method, inferred = "year_from_same_judgment", []
            flags = flags + inferred
            if ref.get("name_prefix_match"):
                flags = flags + ["instrument_name_misspelt"]
            number = act["number"]
        units = self.store.article_units(act["id"], article)
        if not units:
            return {"status": "article_not_found", "act_id": act["id"], "number": number, "method": method, "flags": flags}
        unit = next((u for u in units if "possible_embedded_or_amending_unit" not in u["flags"]), units[0])
        if len(units) > 1:
            flags = flags + ["repeated_article_in_act"]
        return {"status": "resolved", "unit_id": unit["id"], "act_id": act["id"], "number": number, "doc_type": act["doc_type"],
                "name_key": act["name_key"], "method": method, "flags": flags}

    def prepare(self, case):
        text = case["text"]
        spans = sections(text)
        if "facts" not in spans:
            raise Skip("no_facts_section")
        if not {"reasoning", "decision"} & spans.keys():
            raise Skip("no_reasoning_or_decision_section")
        all_sentences = sentences(text)
        fact_start, fact_end = spans["facts"]
        facts = [s for s in all_sentences if fact_start <= s["start"] and s["end"] <= fact_end and not s["explicit_legal_or_outcome"]]
        if not facts:
            raise Skip("no_usable_facts")
        on_date = judgment_date(text, case["metadata"])
        found = mentions(text, [(n, *spans[n]) for n in ("reasoning", "decision") if n in spans], self.acts)
        document_years = {}
        for m in found:
            ref = m["act_ref"] or {}
            if ref.get("name_key") and ref.get("year"):
                document_years.setdefault((ref["doc_type"], ref["name_key"]), set()).add(ref["year"])
        stats, cache, records, targets = Counter(), {}, [], {}
        for m in found:
            key = canonical([m["act_ref"], m["article"]])
            if key not in cache:
                cache[key] = self.resolve(m, on_date, document_years)
            resolution = cache[key]
            sentence = next((s for s in all_sentences if s["start"] <= m["start"] < s["end"]), None)
            kind = role(m, resolution if resolution["status"] == "resolved" else None, sentence["text"] if sentence else m["raw"])
            records.append({**{k: m[k] for k in ["article", "clauses", "act_ref", "binding", "section", "start", "end", "raw"]},
                            "sentence_id": sentence["id"] if sentence else None, "role": kind, "resolution": resolution})
            stats["mentions"] += 1
            stats[f"role:{kind}"] += 1
            stats[f"resolution:{resolution['status']}"] += 1
            if kind == "substantive" and resolution["status"] == "resolved":
                target = targets.setdefault(resolution["unit_id"], {"resolution": resolution, "mention_indexes": [], "cited_clauses": set(), "sentence_ids": set()})
                target["mention_indexes"].append(len(records) - 1)
                target["cited_clauses"].update(m["clauses"])
                if sentence:
                    target["sentence_ids"].add(sentence["id"])
        unresolved = sorted({r["raw"] for r in records if r["role"] == "substantive" and r["resolution"]["status"] != "resolved"})
        stats["substantive_targets"] = len(targets)
        if not targets:
            raise Skip("no_resolved_substantive_citation", dict(stats))
        prepared = {}
        for i, (uid, target) in enumerate(targets.items(), 1):
            cid = f"c{i}"
            unit = self.store.get_unit(uid)
            options = clause_options(unit, self.store.clauses(unit), cid)
            unit["title"] = article_title(unit)
            number = target["resolution"]["number"]
            equivalents = []
            if number:
                equivalents = [u for u in self.store.number_units(number, unit["article"], "phapdien") if u["id"] != uid]
                equivalents += [u for u in self.store.number_units(number, unit["article"], "tvpl") if u["id"] != uid and same_text(u, unit)]
            prepared[cid] = {"cid": cid, "unit": unit, "options": options,
                             "equivalents": equivalents, "cited_clauses": sorted(target["cited_clauses"]),
                             "sentence_ids": sorted(target["sentence_ids"], key=lambda s: int(s[1:])),
                             "resolution": target["resolution"], "mention_indexes": target["mention_indexes"]}
        by_sentence = {}
        for cid, target in prepared.items():
            for sid in target["sentence_ids"]:
                by_sentence.setdefault(sid, []).append(cid)
        excerpts = [{"id": s["id"], "text": s["text"], "citation_ids": by_sentence[s["id"]]} for s in all_sentences if s["id"] in by_sentence]
        return {"case": case, "spans": spans, "facts": facts, "judgment_date": on_date, "citations": records,
                "targets": prepared, "excerpts": excerpts, "unresolved_substantive": unresolved, "stats": dict(stats)}

    # ---- model calls (worker threads: no database access) ----

    def issue_payload(self, ctx, revision=False):
        payload = {"task": "group_court_citations_into_issues_and_write_facts_only_questions",
                   "facts": [{"id": s["id"], "text": s["text"]} for s in ctx["facts"]],
                   "court_reasoning": ctx["excerpts"],
                   "citations": [{"id": cid, "article": t["unit"]["article"], "article_title": t["unit"]["title"],
                                  "instrument": t["unit"]["source_title"], "instrument_number": t["resolution"]["number"],
                                  "court_cited_clauses": t["cited_clauses"]} for cid, t in ctx["targets"].items()],
                   "max_issues": self.config.max_issues}
        if revision:
            payload["revision_notes"] = REPAIR_NOTES
        return payload

    def issues(self, ctx):
        case_id = ctx["case"]["id"]
        contract = schema("ISSUE", [s["id"] for s in ctx["facts"]], list(ctx["targets"]), max_issues=self.config.max_issues)

        def problem(item):
            if not item["issue_text"].strip() or not item["query_text"].strip():
                return "empty_text"
            return "leakage" if leaks(item["issue_text"] + " " + item["query_text"]) else None

        def usable(output):
            # Raising here keeps a response with no usable issue out of the cache, so a retry really asks again.
            ids = [i["issue_id"] for i in output["issues"]]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate issue id")
            if not any(problem(i) is None for i in output["issues"]):
                raise ValueError("no usable issue")

        for attempt in range(2):
            try:
                output = self.call("ISSUE", self.issue_payload(ctx, attempt > 0), contract, case_id + (":repair" if attempt else ""), case_id, usable)
            except ModelError as error:
                if error.code != "semantic_validation":
                    raise
                if attempt:
                    failure = AnnotationError("ISSUE produced no usable issue (empty text or leakage of law/outcome) after one repair")
                    failure.code = "no_valid_issue"
                    raise failure from None
                continue
            dropped = [f"{i['issue_id']}:{problem(i)}" for i in output["issues"] if problem(i)]
            return output, [i for i in output["issues"] if problem(i) is None], dropped, attempt > 0

    def evidence(self, ctx, issue):
        """Assessment per citation id, or {"error": code} when the call failed softly (caller falls back)."""
        case_id = ctx["case"]["id"]
        facts = {s["id"]: s["text"] for s in ctx["facts"]}

        def run(cids, item):
            options = [o["id"] for cid in cids for o in ctx["targets"][cid]["options"]]
            payload = {"issue": issue["issue_text"], "question": issue["query_text"],
                       "facts": [facts[i] for i in issue["fact_ids"]],
                       "sources": [{"id": cid, "article": ctx["targets"][cid]["unit"]["article"], "article_title": ctx["targets"][cid]["unit"]["title"],
                                    "instrument": ctx["targets"][cid]["unit"]["source_title"], "court_cited_clauses": ctx["targets"][cid]["cited_clauses"],
                                    "clauses": [{"id": o["id"], "text": " ".join(ctx["targets"][cid]["unit"]["text"][o["start"]:o["end"]].split())}
                                                for o in ctx["targets"][cid]["options"]]} for cid in cids]}

            def complete_and_consistent(output):
                seen = [a["source_id"] for a in output["assessments"]]
                if sorted(seen) != sorted(cids):
                    raise ValueError("every source must be assessed exactly once")
                for a in output["assessments"]:
                    if any(not c.startswith(a["source_id"] + ".") for c in a["clause_ids"]):
                        raise ValueError("clause belongs to another source")

            output = self.call("EVIDENCE", payload, schema("EVIDENCE", citation_ids=cids, clause_ids=options), item, case_id, complete_and_consistent)
            return {a["source_id"]: a for a in output["assessments"]}

        cids = sorted(set(issue["citation_ids"]), key=lambda c: int(c[1:]))
        try:
            return run(cids, f"{case_id}:{issue['issue_id']}")
        except ModelError as error:
            if error.code not in SOFT_MODEL_ERRORS:
                raise
            if error.code != "context_budget" or len(cids) == 1:
                return {cid: {"error": error.code} for cid in cids}
        result = {}
        for cid in cids:  # Too long together: assess each article on its own.
            try:
                result.update(run([cid], f"{case_id}:{issue['issue_id']}:{cid}"))
            except ModelError as error:
                if error.code not in SOFT_MODEL_ERRORS:
                    raise
                result[cid] = {"error": error.code}
        return result

    def run_model(self, ctx):
        output, kept, dropped, repaired = self.issues(ctx)
        return {"issue_output": output, "issues": kept, "dropped_issues": dropped, "repaired": repaired,
                "evidence": {issue["issue_id"]: self.evidence(ctx, issue) for issue in kept}}

    # ---- dataset records (pure) ----

    def build(self, ctx, model):
        case, queries, seen = ctx["case"], [], set()
        case_reasons = [f"issue_dropped:{d}" for d in model["dropped_issues"]] + (["issue_repair_attempted"] if model["repaired"] else [])
        if ctx["unresolved_substantive"]:
            case_reasons.append("unresolved_substantive_citations_in_case")
        order = {s["id"]: i for i, s in enumerate(ctx["facts"])}
        for issue in model["issues"]:
            assessments = model["evidence"][issue["issue_id"]]
            positives, reasons = [], list(case_reasons)
            for cid in sorted(set(issue["citation_ids"]), key=lambda c: int(c[1:])):
                target, assessment = ctx["targets"][cid], assessments.get(cid, {"error": "missing"})
                options = {o["id"]: o for o in target["options"]}
                flags = list(target["resolution"].get("flags", []))
                if "error" in assessment:
                    grade, chosen = 2, [o for o in target["options"] if o["clause"] in target["cited_clauses"]]
                    flags.append(f"evidence_model_error:{assessment['error']}")
                elif assessment["relevance"] == "not_relevant":
                    reasons.append(f"court_citation_rejected_by_model:{cid}")
                    continue
                else:
                    grade = 2 if assessment["relevance"] == "core" else 1
                    chosen = [options[c] for c in assessment["clause_ids"] if c in options] or \
                             [o for o in target["options"] if o["clause"] in target["cited_clauses"]]
                if not chosen:
                    chosen = [{"start": 0, "end": len(target["unit"]["text"])}] if len(target["options"]) > 1 else target["options"]
                    flags.append("evidence_whole_article")
                level = METHOD_CONFIDENCE.get(target["resolution"]["method"], "low")
                if any(ctx["citations"][i]["binding"] == "context" for i in target["mention_indexes"]) and level == "high":
                    level = "medium"
                if flags:
                    level = "low"
                spans = [{"start": o["start"], "end": o["end"]} for o in chosen]
                positives.append({"cid": cid, "target": target, "grade": grade, "spans": spans, "confidence": level, "flags": flags,
                                  "assessment": assessment})
            core = [p for p in positives if p["grade"] == 2]
            asked = re.sub(r"\s+", " ", fold_char_string(issue["issue_text"] + " " + issue["query_text"]))
            for p in positives:
                title = re.sub(r"\s+", " ", fold_char_string(p["target"]["unit"]["title"])).strip(" .")
                if len(title.split()) >= 4 and title in asked:
                    reasons.append(f"question_repeats_article_title:{p['cid']}")
            groups, qrels, evidence = [], {}, []
            merged = {}
            for p in core:
                unit, res = p["target"]["unit"], p["target"]["resolution"]
                key = (res.get("doc_type"), res.get("name_key"), fold_char_string(unit["title"]).strip()) if res.get("name_key") and unit["title"] else p["cid"]
                merged.setdefault(key, []).append(p)
            for p in positives:
                canonical_unit = p["target"]["unit"]
                qrels[canonical_unit["id"]] = {"unit_id": canonical_unit["id"], "grade": p["grade"], "evidence": p["spans"],
                                               "label_source": "court_applied", "confidence": p["confidence"], "copy_of": None}
                for copy in p["target"]["equivalents"]:
                    mapped = [locate(copy["text"], canonical_unit["text"][s["start"]:s["end"]]) for s in p["spans"]]
                    if all(mapped):
                        qrels[copy["id"]] = {"unit_id": copy["id"], "grade": p["grade"], "evidence": mapped, "label_source": "court_applied",
                                             "confidence": p["confidence"], "copy_of": canonical_unit["id"]}
                    else:
                        p["flags"].append(f"copy_text_differs:{copy['id']}")  # e.g. Pháp điển holds the amended text
            for i, members in enumerate(merged.values(), 1):
                ids = []
                for p in members:
                    ids.append(p["target"]["unit"]["id"])
                    ids += [c["id"] for c in p["target"]["equivalents"] if c["id"] in qrels]
                groups.append({"group_id": f"g{i}", "unit_ids": ids, "canonical_unit_ids": [p["target"]["unit"]["id"] for p in members],
                               "cross_version": len(members) > 1})
            for row in qrels.values():
                evidence.append({"unit_id": row["unit_id"], "spans": row["evidence"]})
            facts = sorted({f: True for f in issue["fact_ids"]}, key=order.get)
            fact_rows = [{"sentence_id": s["id"], "text": s["text"]} for s in ctx["facts"] if s["id"] in set(facts)]
            if not core:
                reasons.append("no_core_source")
            confidence = min((p["confidence"] for p in core), key=CONFIDENCE.get, default="low")
            if ctx["unresolved_substantive"] and confidence == "high":
                confidence = "medium"
            quality = "silver" if core and fact_rows else "needs_review"
            key_ids = sorted(p["target"]["unit"]["id"] for p in positives) or [issue["issue_id"]]
            qid = "rr:" + digest([case["id"], key_ids])[:24]
            if qid in seen:
                qid = "rr:" + digest([case["id"], key_ids, issue["issue_id"]])[:24]
            seen.add(qid)
            timeline = timeline_mentions(fact_rows) + ([{"event": "judgment_date", "date": ctx["judgment_date"]}] if ctx["judgment_date"] else [])
            queries.append({
                "id": qid, "case_id": case["id"], "family_id": case["family_id"], "split": case["split"], "run_hash": self.run_hash,
                "query_origin": "court_citation_anchored", "quality": quality, "confidence": confidence, "review_status": "unreviewed",
                "model_input": {"facts": fact_rows, "issue": issue["issue_text"], "question": issue["query_text"],
                                "timeline": timeline, "corpus_snapshot_id": self.snapshot},
                "target": {"answerability": "answerable", "qrels": list(qrels.values()), "requirement_groups": groups, "evidence": evidence},
                "annotation": {
                    "sources": [{"citation_id": p["cid"], "unit_id": p["target"]["unit"]["id"], "article": p["target"]["unit"]["article"],
                                 "article_title": p["target"]["unit"]["title"], "instrument": p["target"]["unit"]["source_title"],
                                 "instrument_number": p["target"]["resolution"]["number"], "resolution": p["target"]["resolution"],
                                 "court_mentions": [ctx["citations"][i]["raw"] for i in p["target"]["mention_indexes"]],
                                 "court_cited_clauses": p["target"]["cited_clauses"], "equivalent_unit_ids": [c["id"] for c in p["target"]["equivalents"]],
                                 "grade": p["grade"], "confidence": p["confidence"], "flags": p["flags"], "model_assessment": p["assessment"]}
                                for p in positives],
                    "issue_citation_ids": issue["citation_ids"], "review_reasons": reasons,
                    "unresolved_substantive_citations": ctx["unresolved_substantive"], "case_citations": ctx["citations"]},
                "private_provenance": {"judgment_id": case["id"], "metadata": case["metadata"], "text_hash": case["text_hash"],
                                       "family_flags": case["family_flags"], "judgment_date": ctx["judgment_date"],
                                       "sections": {k: list(v) for k, v in ctx["spans"].items()},
                                       "query_fact_spans": [{"sentence_id": s["id"], "start": s["start"], "end": s["end"]}
                                                            for s in ctx["facts"] if s["id"] in set(facts)]}})
        return queries

    # ---- orchestration ----

    def selected_cases(self):
        selected = 0
        for row in self.store.connection.execute("SELECT * FROM cases ORDER BY id"):
            case = dict(row)
            case["metadata"] = json.loads(case["metadata"])
            if self.config.categories and case["metadata"].get("category") not in self.config.categories:
                continue
            if self.config.case_ids and case["id"] not in self.config.case_ids:
                continue
            selected += 1
            if self.config.annotation_limit and selected > self.config.annotation_limit:
                return
            case["family_flags"] = json.loads(case["family_flags"] or "[]")
            yield case

    def record(self, case_id, status, result, code=None):
        with self.store.transaction():
            if status == "failed":
                self.store.failure("annotation", case_id, code, result.get("message", ""))
            self.store.connection.execute("INSERT INTO annotations VALUES (?,?,?,?,?) ON CONFLICT(case_id,run_hash) DO UPDATE SET "
                                          "status=excluded.status,result=excluded.result,error_code=excluded.error_code",
                                          (case_id, self.run_hash, status, canonical(result), code))

    def annotate(self, retry_failed=False):
        if not self.store.get_meta("family_revision"):
            raise AnnotationError("Run build-cases before annotate")
        if not self.store.connection.execute("SELECT 1 FROM units LIMIT 1").fetchone():
            raise AnnotationError("No law units available; run build-laws first")
        if not self.store.connection.execute("SELECT 1 FROM acts LIMIT 1").fetchone():
            raise AnnotationError("No instrument index; run build-laws to build it")
        for cid in self.config.case_ids:
            if not self.store.connection.execute("SELECT 1 FROM cases WHERE id=?", (cid,)).fetchone():
                raise AnnotationError("Requested case ID was not ingested; check RR_CASE_IDS and ingestion limits")
        with self.store.transaction():
            self.store.set_meta("active_run", self.run_hash)
            self.store.set_meta("active_snapshot", self.snapshot)
            self.store.set_meta("run:" + self.run_hash, {"config": self.config.public(), "prompt_hash": self.prompts.version,
                                                       "snapshot": self.snapshot, "code_fingerprint": self.code_fingerprint})
        summary = Counter()
        workers = max(self.client(s).profile.concurrency for s in ("ISSUE", "EVIDENCE"))
        stop = None
        with ThreadPoolExecutor(max_workers=workers) as executor:
            pending = {}

            def collect(done):
                nonlocal stop
                for future in done:
                    ctx = pending.pop(future)
                    case_id = ctx["case"]["id"]
                    try:
                        queries = self.build(ctx, future.result())
                    except ModelError as error:
                        if error.code not in SOFT_MODEL_ERRORS:
                            stop = stop or error
                            summary["interrupted"] += 1
                            continue
                        self.fail(case_id, error.code, str(error), ctx["stats"], summary)
                        continue
                    except AnnotationError as error:
                        self.fail(case_id, error.code, str(error), ctx["stats"], summary)
                        continue
                    with self.store.transaction():
                        self.store.connection.execute("DELETE FROM queries WHERE case_id=? AND run_hash=?", (case_id, self.run_hash))
                        for query in queries:
                            # Query IDs are stable across runs (case + sources), so reviews survive code/prompt changes.
                            self.store.connection.execute("INSERT INTO queries VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                                                          "case_id=excluded.case_id,run_hash=excluded.run_hash,payload=excluded.payload",
                                                          (query["id"], case_id, self.run_hash, canonical(query)))
                    self.record(case_id, "complete", {"query_ids": [q["id"] for q in queries], "stats": ctx["stats"],
                                                      "quality": dict(Counter(q["quality"] for q in queries))})
                    summary["complete"] += 1
                    summary["queries"] += len(queries)
                    summary["silver_queries"] += sum(q["quality"] == "silver" for q in queries)
                    LOG.info("Annotated case %s: %s queries (%s silver)", case_id, len(queries), sum(q["quality"] == "silver" for q in queries))

            for case in self.selected_cases():
                if stop:
                    break
                summary["selected"] += 1
                prior = self.store.connection.execute("SELECT status FROM annotations WHERE case_id=? AND run_hash=?", (case["id"], self.run_hash)).fetchone()
                if prior and (prior[0] in {"complete", "skipped"} or not retry_failed):
                    summary["previously_done"] += 1
                    continue
                try:
                    ctx = self.prepare(case)
                except Skip as skip:
                    self.record(case["id"], "skipped", {"stats": skip.stats}, skip.reason)
                    summary["skipped"] += 1
                    summary[f"skipped:{skip.reason}"] += 1
                    continue
                pending[executor.submit(self.run_model, ctx)] = ctx
                if len(pending) >= 2 * workers:
                    collect(wait(pending, return_when=FIRST_COMPLETED).done)
            if stop:
                for future in pending:
                    future.cancel()
            while pending:
                collect(wait(pending, return_when=FIRST_COMPLETED).done)
        if self.tracer:
            self.tracer.flush()
        if not summary["selected"]:
            raise AnnotationError("No judgments match the configured selection")
        if stop:
            LOG.error("Run stopped by %s; rerun the same command to continue", stop.code)
            raise stop
        counts = dict(self.store.connection.execute("SELECT status,count(*) FROM annotations WHERE run_hash=? GROUP BY status", (self.run_hash,)).fetchall())
        return {"run_hash": self.run_hash, **{k: v for k, v in summary.items()}, "failed": summary.get("failed", 0), "run_totals": counts}

    def fail(self, case_id, code, message, stats, summary):
        self.record(case_id, "failed", {"message": message, "stats": stats}, code)
        summary["failed"] += 1
        summary[f"failed:{code}"] += 1
        LOG.warning("Case %s failed: %s", case_id, code)
        if self.config.fail_fast:
            raise AnnotationError(f"Case {case_id} failed: {code}")

    def audit(self, limit=200):
        """Deterministic part only (no model call): how many cases can yield queries and why the others cannot."""
        totals, citation_totals, examples = Counter(), Counter(), {}
        for n, case in enumerate(self.selected_cases()):
            if n >= limit:
                break
            totals["cases"] += 1
            try:
                ctx = self.prepare(case)
            except Skip as skip:
                totals[f"skipped:{skip.reason}"] += 1
                citation_totals.update(skip.stats)
                examples.setdefault(skip.reason, case["id"])
                continue
            totals["eligible"] += 1
            citation_totals.update(ctx["stats"])
            if ctx["unresolved_substantive"]:
                totals["eligible_with_unresolved_substantive"] += 1
        substantive = sum(v for k, v in citation_totals.items() if k == "role:substantive")
        report = {"cases": dict(totals), "citations": dict(citation_totals), "example_case_per_skip_reason": examples,
                  "eligible_rate": totals["eligible"] / totals["cases"] if totals["cases"] else 0,
                  "resolution_rate": citation_totals["resolution:resolved"] / citation_totals["mentions"] if citation_totals["mentions"] else 0,
                  "substantive_mentions": substantive}
        (self.config.output_dir / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report

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
