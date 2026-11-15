"""Strict stage schemas and portable prompt assembly."""

from pathlib import Path
import json
import re

from .config import canonical, digest


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def string(enum=None):
    return {"type": "string", **({"enum": list(enum)} if enum else {})}


def array(items):
    return {"type": "array", "items": items}


def strings(enum=None):
    return array(string(enum))


ROLES = ["facts_reported", "party_request", "procedural_history", "party_argument", "prosecution_position", "court_reasoning", "operative_decision", "administration", "mixed", "uncertain"]
LABELS = ["core_candidate", "supporting", "not_relevant", "uncertain"]


def schema(stage, sentence_ids=(), candidate_ids=(), max_issues=3):
    if stage == "P1":
        return obj({"sentence_roles": array(obj({"sentence_id": string(sentence_ids), "role": string(ROLES),
                    "speaker": string(), "dispute_status": string(["disputed", "undisputed", "unknown"])})),
                    "usable_fact_ids": strings(sentence_ids), "context_needed": strings(), "uncertainties": strings()})
    if stage == "P2":
        result = obj({"issues": array(obj({"issue_id": string([f"i{i+1}" for i in range(max_issues)]),
                     "issue_text": string(), "query_text": string(), "fact_support_ids": strings(sentence_ids),
                     "missing_facts": strings(), "query_status": string(["answerable", "needs_clarification", "insufficient_facts"])}))})
        result["properties"]["issues"]["maxItems"] = max_issues
        return result
    if stage == "P3":
        return obj({"selected_candidate_ids": strings(candidate_ids),
                    "resolution_status": string(["candidate_resolved", "ambiguous_act", "ambiguous_version", "not_found_in_candidates", "conflict"]),
                    "support_sentence_ids": strings(sentence_ids), "ambiguity_reason": string(), "missing_evidence": strings()})
    if stage == "P4":
        return obj({"candidate_id": string(candidate_ids), "relevance_proposal": string(LABELS),
                    "version_status": string(["supported", "contradicted", "unresolved"]),
                    "evidence_quotes": strings(), "source_roles": strings(),
                    "short_justification": string(), "missing_context": strings()})
    if stage == "P5":
        return obj({"proposed_requirement_groups": array(obj({"group_id": string(), "candidate_ids": strings(candidate_ids), "reason": string()})),
                    "redundant_sources": strings(candidate_ids), "unresolved_version_conflicts": strings(),
                    "missing_source_requests": strings(), "review_reasons": strings()})
    if stage == "P6":
        return obj({"checks": array(obj({"criterion": string(), "status": string(["pass", "fail", "uncertain"]),
                    "evidence_ids": strings(candidate_ids), "reason": string()})),
                    "independent_relevance": array(obj({"candidate_id": string(candidate_ids), "relevance_proposal": string(LABELS)})),
                    "review_reasons": strings()})
    if stage == "P0V":
        return obj({"transcription": string(), "uncertain_regions": strings(), "proposed_corrections": strings()})
    if stage == "AUGMENT":
        return obj({"facts": string(), "issue_text": string(), "query_text": string(),
                    "evidence_quotes": strings(), "missing_facts": strings()})
    raise ValueError(f"Unknown stage {stage}")


class PromptBank:
    def __init__(self, path: Path, fewshot: Path | None = None):
        text = path.read_text(encoding="utf-8")
        common = re.search(r"```text\n(.*?)\n```", text, re.S)
        if not common:
            raise ValueError("Prompt document has no common text block")
        self.common = common.group(1)
        self.prompts = {}
        headings = list(re.finditer(r"(?m)^##\s+\d+\s+(P\dV?|AUGMENT)\b[^\n]*", text))
        for i, heading in enumerate(headings):
            section = text[heading.end():headings[i + 1].start() if i + 1 < len(headings) else len(text)]
            prompt = re.search(r"```text\n(.*?)\n```", section, re.S)
            if prompt:
                self.prompts[heading.group(1)] = prompt.group(1)
        missing = {"P1", "P2", "P3", "P4", "P5", "P6", "P0V"} - self.prompts.keys()
        if missing:
            raise ValueError(f"Missing stage prompts: {sorted(missing)}")
        self.fewshot = {}
        if fewshot:
            self.fewshot = json.loads(fewshot.read_text())
            if not isinstance(self.fewshot, dict):
                raise ValueError("Few-shot file must be an object keyed by stage")
            for examples in self.fewshot.values():
                for example in examples:
                    if example.get("split") not in {"train", "dev"} or example.get("human_reviewed") is not True:
                        raise ValueError("Few-shot examples must be human-reviewed train/dev examples")
        self.version = digest({"text": text, "fewshot": self.fewshot})

    def messages(self, stage, payload, output_schema, profile):
        if stage not in self.prompts:
            raise ValueError(f"No prompt configured for stage {stage}")
        instruction = self.common + "\n\n" + self.prompts[stage]
        instruction += "\nTrả một JSON object duy nhất theo JSON Schema sau:\n" + canonical(output_schema)
        messages = [{"role": profile.instruction_role, "content": instruction}]
        for example in self.fewshot.get(stage, []):
            messages.extend([{"role": "user", "content": canonical(example["input"])},
                             {"role": "assistant", "content": canonical(example["output"])}])
        messages.append({"role": "user", "content": canonical(payload)})
        return messages
