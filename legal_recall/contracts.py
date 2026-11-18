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


RELEVANCE = ["core", "supporting", "not_relevant"]


def schema(stage, fact_ids=(), citation_ids=(), clause_ids=(), max_issues=3):
    if stage == "ISSUE":
        issue = obj({"issue_id": string([f"i{i + 1}" for i in range(max_issues)]),
                     "citation_ids": {**strings(citation_ids), "minItems": 1},
                     "fact_ids": {**strings(fact_ids), "minItems": 1},
                     "issue_text": {**string(), "maxLength": 400}, "query_text": {**string(), "maxLength": 500}})
        return obj({"issues": {**array(issue), "maxItems": max_issues},
                    "unused_citation_ids": strings(citation_ids)})
    if stage == "EVIDENCE":
        return obj({"assessments": array(obj({"source_id": string(citation_ids), "relevance": string(RELEVANCE),
                                              "clause_ids": strings(clause_ids), "reason": {**string(), "maxLength": 400}}))})
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
        headings = list(re.finditer(r"(?m)^##\s+\d+\s+(ISSUE|EVIDENCE|P0V|AUGMENT)\b[^\n]*", text))
        for i, heading in enumerate(headings):
            section = text[heading.end():headings[i + 1].start() if i + 1 < len(headings) else len(text)]
            prompt = re.search(r"```text\n(.*?)\n```", section, re.S)
            if prompt:
                self.prompts[heading.group(1)] = prompt.group(1)
        missing = {"ISSUE", "EVIDENCE"} - self.prompts.keys()
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
