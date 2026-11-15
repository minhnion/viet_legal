"""Conservative extraction with offsets into immutable NFC text."""

from collections import Counter
from datetime import date
import hashlib
import re
import unicodedata


PARSER_VERSION = "nfc-legal-units-v3"
NUMBER_RE = re.compile(r"\b\d{1,5}\s*/\s*\d{4}\s*/\s*[A-Za-zĐđ0-9]+(?:[-–][A-Za-zĐđ0-9]+)*")
ARTICLE_RE = re.compile(r"(?mi)^[ \t]*(?:\*\*|#{1,6}\s*)?Điều\s+(\d+[a-zđ]?)[ \t]*[.:][ \t]*([^\n]*)")
CLAUSE_RE = re.compile(r"(?m)^[ \t]*(\d+)\s*\.\s+(\S[^\n]*)")
POINT_RE = re.compile(r"(?m)^[ \t]*([a-zđ])\)\s+(\S[^\n]*)")
LAW_LEAK = re.compile(r"(?i)\b(?:điều|khoản)\s+\d+|\bđiểm\s+[a-zđ]\s+(?:khoản|điều)\s+\d+|\b(?:áp dụng|căn cứ)\s+(?:điều|bộ luật|luật)|\b(?:tuyên xử|tòa án (?:quyết định|chấp nhận|bác))\b")
DECISION_RE = re.compile(r"(?i)\b(?:tuyên xử|xử phạt|tòa án quyết định|hội đồng xét xử (?:nhận định|xét thấy)|áp dụng điều|căn cứ điều)\b")
REASON_HEADING = re.compile(r"(?i)^(?:\[\d+\]\s*)?(?:NHẬN\s+ĐỊNH\s+CỦA\s+T[ÒO]A|XÉT\s+THẤY|QUYẾT\s+ĐỊNH\s*[:：]|TUYÊN\s+XỬ\s*[:：])")


def normalize(text):
    return unicodedata.normalize("NFC", (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " "))


def fold(text):
    text = normalize(text).lower().replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_number(text):
    match = NUMBER_RE.search(text or "")
    return re.sub(r"\s+", "", match.group()).upper().replace("–", "-") if match else None


def header_number(text):
    # A cited instrument in a legal-basis paragraph is not the current instrument.
    match = re.search(r"(?mi)^\s*(?:Số|Luật\s+số|Bộ\s+luật\s+số)\s*:\s*([^\n]+)", text[:5000])
    return document_number(match.group(1)) if match else None


def iso_date(value):
    if not value:
        return None
    for pattern in [r"^(\d{4})-(\d{1,2})-(\d{1,2})$", r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$"]:
        m = re.fullmatch(pattern, value.strip())
        if m:
            parts = list(map(int, m.groups()))
            if len(m.group(1)) != 4:
                parts.reverse()
            try:
                return date(*parts).isoformat()
            except ValueError:
                return None
    return None


def leakage(text):
    return bool(LAW_LEAK.search(text) or NUMBER_RE.search(text))


def sentences(text):
    """Paragraph/sentence spans; retain headings and never change text for offsets."""
    results = []
    forbidden_section = False
    for line in re.finditer(r"[^\n]+", text):
        raw = line.group()
        if REASON_HEADING.search(raw.strip()):
            forbidden_section = True
        if re.match(r"^\s*##\s*Page\s+\d+", raw, re.I) or not raw.strip():
            continue
        # Do not split decimals, initials or Điều 1. into misleading standalone fragments.
        for part in re.finditer(r".+?(?:[.!?;](?=\s+[A-ZÀ-ỸĐ])|$)", raw):
            start, end = line.start() + part.start(), line.start() + part.end()
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if start == end:
                continue
            value = text[start:end]
            results.append({"id": f"s{len(results)}", "start": start, "end": end,
                            "text": value, "forbidden_section": forbidden_section,
                            "explicit_legal_or_outcome": leakage(value) or bool(DECISION_RE.search(value))})
    return results


def legal_units(text, source_id):
    """Return articles and clause/point children; ambiguous amendment structures flagged."""
    articles = list(ARTICLE_RE.finditer(text))
    counts = Counter(m.group(1) for m in articles)
    units = []
    version_prefix = f"{source_id}@{sha(text)[:24]}"
    for index, match in enumerate(articles):
        start = match.start()
        end = articles[index + 1].start() if index + 1 < len(articles) else len(text)
        article = match.group(1).lower().lstrip("0") or "0"
        uid = f"{version_prefix}:a{article}:{start}"
        before = text[max(0, start - 180):start]
        flags = []
        if counts[match.group(1)] > 1:
            flags.append("repeated_article_number")
        if re.search(r"(?i)(?:sửa đổi|bổ sung|thay thế|như sau|ban hành kèm)", before) or match.group(2).strip().startswith(('“', '"')):
            flags.append("possible_embedded_or_amending_unit")
        units.append({"id": uid, "parent_id": None, "article": article, "clause": None,
                      "point": None, "title": match.group(2).strip(), "start": start,
                      "end": end, "text": text[start:end], "flags": flags})
        clauses = list(CLAUSE_RE.finditer(text, match.end(), end))
        for i, clause in enumerate(clauses):
            cs, ce = clause.start(), clauses[i + 1].start() if i + 1 < len(clauses) else end
            cid = f"{uid}:k{clause.group(1)}:{cs}"
            units.append({"id": cid, "parent_id": uid, "article": article,
                          "clause": clause.group(1), "point": None, "title": "", "start": cs,
                          "end": ce, "text": text[cs:ce], "flags": flags[:]})
            points = list(POINT_RE.finditer(text, cs, ce))
            for j, point in enumerate(points):
                ps, pe = point.start(), points[j + 1].start() if j + 1 < len(points) else ce
                units.append({"id": f"{cid}:p{point.group(1)}:{ps}", "parent_id": cid,
                              "article": article, "clause": clause.group(1), "point": point.group(1),
                              "title": "", "start": ps, "end": pe, "text": text[ps:pe], "flags": flags[:]})
    return units


def quote_spans(text, quote):
    if not quote or not quote.strip():
        return []
    return [{"start": m.start(), "end": m.start() + len(quote)} for m in re.finditer(re.escape(quote), text)]


def timeline_mentions(facts):
    result = []
    for fact in facts:
        for m in re.finditer(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b|\b\d{4}-\d{1,2}-\d{1,2}\b", fact["text"]):
            parsed = iso_date(m.group())
            if parsed:
                result.append({"event": "date_mention_requires_event_annotation", "date": parsed,
                               "raw": m.group(), "fact_sentence_id": fact.get("sentence_id", fact.get("id"))})
    return result


def search_terms(text, limit=24):
    words = re.findall(r"[^\W_]+", fold(text), re.UNICODE)
    stop = {"va", "la", "cua", "cho", "voi", "cac", "nhung", "mot", "de", "duoc", "trong", "theo", "can", "nao", "tim", "nguon", "quy", "dinh", "phap", "luat"}
    counts = Counter(w for w in words if len(w) > 1 and w not in stop)
    return [w for w, _ in counts.most_common(limit)]


def window_text(text, max_chars):
    if max_chars < 32:
        raise ValueError("Text window budget is too small")
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + max_chars // 2, end)
            if boundary > start:
                end = boundary + 1
        yield {"start": start, "end": end, "text": text[start:end]}
        start = end
