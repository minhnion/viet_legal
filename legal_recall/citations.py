"""Judgment sections and court citations, parsed with offsets into the immutable case text.

Matching runs on a "stream": the text folded to lowercase ASCII with all whitespace removed, plus a map
back to original offsets. This tolerates OCR splits such as "Lu ật" or "Bô ̣ luâ ̣t" without changing
the stored text.
"""

import re
import unicodedata

from .text import iso_date


# Heading keys are the folded heading text with every non-letter removed.
HEADINGS = {
    "facts": {"noidungvuan", "noidungviecdansu", "noidungvuviec", "noidungsuviec", "nhanthay"},
    "reasoning": {"nhandinhcuatoaan", "nhandinhcuahoidongxetxu", "nhandinhcuahdxx", "nhandinh", "xetthay"},
    "decision": {"quyetdinh", "tuyenxu", "xu"},
}
ORDER = ("facts", "reasoning", "decision")

# Instrument types as they appear in the stream; longer keys first so "nghiquyetlientich" wins over "nghiquyet".
TYPES = [("nghiquyetlientich", "Nghị quyết liên tịch"), ("thongtulientich", "Thông tư liên tịch"),
         ("boluat", "Bộ luật"), ("phaplenh", "Pháp lệnh"), ("hienphap", "Hiến pháp"),
         ("nghiquyet", "Nghị quyết"), ("nghidinh", "Nghị định"), ("thongtu", "Thông tư"), ("luat", "Luật")]
# Common court abbreviations: type and name key of the code they abbreviate.
ABBREVIATIONS = [("luathngd", ("Luật", "honnhanvagiadinh")), ("bltths", ("Bộ luật", "totunghinhsu")), ("blttds", ("Bộ luật", "totungdansu")),
                 ("blhs", ("Bộ luật", "hinhsu")), ("blds", ("Bộ luật", "dansu")), ("blld", ("Bộ luật", "laodong"))]
NAMED_TYPES = {"Bộ luật", "Luật", "Pháp lệnh", "Hiến pháp"}
# Spellings courts use that differ from the TVPL title.
NAME_ALIASES = {("Luật", "honnhangiadinh"): "honnhanvagiadinh"}

ARTICLE_RE = re.compile(r"(?:khoan(\d+)(?:,|va)?)?(?:cac)?dieu(\d+)(?![0-9])")
MORE_ARTICLE = re.compile(r"(?:,|;|va)(?:cac)?(\d+)(?![0-9/])")
# Text allowed between two article references of one list ("Điều 35, điểm a khoản 1 Điều 39").
CHAIN_GAP = re.compile(r"^(?:[,;]|va|cua|cac|diem[a-z](?:(?:,|va)[a-z])*|khoan\d+(?:(?:,|va)\d+)*|-)*$")
CONNECTOR = re.compile(r"^(?:[,;:/()\-]|cua|theo|tai|thuoc|quydinhtai|khoan\d+|diem[a-z](?:,[a-z])*)*")
# Minimum shared prefix to accept a misspelt instrument name ("Bộ luật Tố tụng dân năm 2015") when unique.
MIN_NAME_PREFIX = 8

PROCEDURAL = {"totungdansu", "totunghinhsu", "totunghanhchinh"}
ENFORCEMENT = {"thihanhandansu", "thihanhanhinhsu"}
# Instruments cited by number (prefix "number/year/"), matched loosely because courts misspell the code part:
# court fees (NQ 326/2016/UBTVQH14, PL 10/2009/PL-UBTVQH12) and the procedure for administrative measures
# at court (PL 09/2014/UBTVQH13).
FEE_NUMBER_PREFIXES = ("326/2016/", "10/2009/")
PROCEDURAL_NUMBER_PREFIXES = ("09/2014/",)
PROCEDURAL_NAME_PREFIXES = ("trinhtuthutucxemxet",)
NUMBER_RE = re.compile(r"\s*(?:số\s*:?\s*)?(\d{1,5})\s*/\s*(\d{4})\s*/\s*((?:[A-ZĐ0-9]+[ \t\-–]*)+)")


def fold_char(ch):
    ch = unicodedata.normalize("NFC", ch).replace("ƣ", "ư").replace("Ƣ", "Ư").lower().replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFD", ch) if not unicodedata.combining(c))


def stream(text, start=0, end=None):
    """Folded, whitespace-free view of text[start:end] and the original offset of every stream char."""
    chars, index = [], []
    for i in range(start, len(text) if end is None else end):
        if text[i].isspace():
            continue
        for c in fold_char(text[i]):
            chars.append(c)
            index.append(i)
    return "".join(chars), index


def heading_key(line):
    s = line.strip().strip("*#").strip()
    if not s or len(s) > 60:
        return None
    letters = [c for c in s if c.isalpha()]
    # Headings are upper case; this rejects body lines such as "Quyết định đưa vụ án ra xét xử số ...".
    if not letters or sum(c.isupper() for c in letters) < 0.8 * len(letters):
        return None
    key = re.sub(r"[^a-z]", "", "".join(fold_char(c) for c in s))
    return next((name for name, keys in HEADINGS.items() if key in keys), None)


def sections(text):
    """First facts heading, then the first reasoning and decision headings after it; spans exclude headings."""
    marks = [(heading_key(m.group()), m.start(), m.end()) for m in re.finditer(r"[^\n]+", text)]
    found, pos = {}, 0
    for name in ORDER:
        hit = next((m for m in marks if m[0] == name and m[1] >= pos), None)
        if hit:
            found[name] = hit
            pos = hit[2]
    names = [n for n in ORDER if n in found]
    return {n: (found[n][2], found[names[i + 1]][1] if i + 1 < len(names) else len(text)) for i, n in enumerate(names)}


def judgment_date(text, metadata):
    value = iso_date(str(metadata.get("issued_date") or "")[:10])
    if value and "1990-01-01" <= value <= "2100-01-01":
        return value
    head = text[:3000]
    m = re.search(r"(?i)ngày\s*:?\s*(\d{1,2})\s*[-–/.]\s*(\d{1,2})\s*[-–/.]\s*(\d{4})", head) or \
        re.search(r"(?i)ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})", head)
    return iso_date(f"{m.group(1)}/{m.group(2)}/{m.group(3)}") if m else None


class ActIndex:
    """Name/number lookup over the `acts` table, with in-force selection by date."""

    def __init__(self, rows):
        self.by_name, self.exact_number, self.number_prefix = {}, {}, {}
        for row in rows:
            row = dict(row)
            if row["number_key"]:
                self.exact_number.setdefault(row["number_key"], []).append(row)
                # TVPL sometimes stores "326/2016/UBTVQH14-MUC-THU-..." (number + title slug).
                parts = row["number_key"].split("-")
                for i in range(1, len(parts)):
                    self.number_prefix.setdefault("-".join(parts[:i]), []).append(row)
            if row["name_key"] and not row["is_amendment"]:
                self.by_name.setdefault((row["doc_type"], row["name_key"]), []).append(row)
        self.names, self.alias = {}, {}
        for doc_type, name in self.by_name:
            self.names.setdefault(doc_type, []).append(name)
        for (doc_type, alias), name in NAME_ALIASES.items():
            if (doc_type, name) in self.by_name:
                self.names[doc_type].append(alias)
                self.alias[(doc_type, alias)] = name
        for names in self.names.values():
            names.sort(key=len, reverse=True)

    def match_name(self, doc_type, s):
        """(name_key, stream length consumed, exact?) of the instrument name at the start of s; punctuation inside a
        name is ignored ("trình tự, thủ tục", "Hôn nhân & gia đình")."""
        letters, ends = [], []
        for i, c in enumerate(s):
            if c.isalnum():
                letters.append(c)
                ends.append(i + 1)
        flat = "".join(letters)
        names = self.names.get(doc_type, [])
        name = next((n for n in names if flat.startswith(n)), None)
        if name:
            return self.alias.get((doc_type, name)) or name, ends[len(name) - 1], True
        best, length = [], 0
        for n in names:
            k = 0
            while k < min(len(n), len(flat)) and n[k] == flat[k]:
                k += 1
            if k > length:
                best, length = [n], k
            elif k == length and k:
                best.append(n)
        if length >= MIN_NAME_PREFIX and len({self.alias.get((doc_type, n)) or n for n in best}) == 1:
            return self.alias.get((doc_type, best[0])) or best[0], ends[length - 1], False
        return None, 0, False

    def by_number(self, key, doc_type=None):
        """Exact number, then number + title slug, then (same type only) a unique act with the same number/year:
        courts misspell the code part ("326/2016/UBTVQH", "326/2016/BUTVQH 14")."""
        found = self.exact_number.get(key) or self.number_prefix.get(key, [])
        if found or not doc_type:
            return found, "instrument_number"
        head = "/".join(key.split("/")[:2]) + "/"
        loose = {a["id"]: a for k, rows in self.exact_number.items() if k.startswith(head) for a in rows if a["doc_type"] == doc_type}
        return (list(loose.values()), "instrument_number_loose") if len(loose) == 1 else ([], "instrument_number")

    def pick(self, doc_type, name, year, on_date):
        """Return (act, method, flags) or (None, reason, flags).

        Without a usable cited year, the act in force on the judgment date is chosen: each act of the same
        type and name runs from its effective date (issue date when missing) until its expiry or until the
        next act of that name takes effect. This is the version to research, not a ruling on which applies."""
        candidates = self.by_name.get((doc_type, name), [])
        start = lambda a: a["effective_date"] or a["issue_date"]
        if on_date:
            # An instrument that took effect after the judgment cannot be the one the court cited.
            candidates = [a for a in candidates if not start(a) or start(a) <= on_date]
        if not candidates:
            return None, "act_not_in_corpus", []
        if len(candidates) == 1 and not year and not start(candidates[0]):
            return candidates[0], "only_act_with_name", ["act_dates_missing"]
        flags = []
        if year:
            same = [a for a in candidates if (a["issue_date"] or "")[:4] == year] or \
                   [a for a in candidates if (a["effective_date"] or "")[:4] == year]
            if len(same) == 1:
                return same[0], "name_and_year", flags
            if len(same) > 1:
                candidates, flags = same, ["several_acts_same_year"]
            else:
                flags.append("cited_year_not_found")
        if on_date:
            dated = sorted((a for a in candidates if start(a)), key=start)
            current = None
            for i, act in enumerate(dated):
                end = act["expiry_date"] or (start(dated[i + 1]) if i + 1 < len(dated) else None)
                if start(act) <= on_date and (not end or on_date < end):
                    current = act
            if current:
                return current, "in_force_on_judgment_date", flags + ([] if current["effective_date"] else ["effective_date_missing"])
        return None, "version_ambiguous", flags


def parse_act(text, index, s, pos, acts):
    """Parse an instrument reference starting at stream position pos; return (act_ref, end_pos) or (None, pos)."""
    rest = s[pos:pos + 160]
    skip = CONNECTOR.match(rest).end()
    rest, pos = rest[skip:], pos + skip
    for key, (doc_type, name) in ABBREVIATIONS:
        if rest.startswith(key):
            return year_after({"doc_type": doc_type, "name_key": name, "abbreviation": key}, text, index, s, pos + len(key)), pos + len(key)
    for key, doc_type in TYPES:
        if not rest.startswith(key):
            continue
        after = pos + len(key)
        if doc_type in NAMED_TYPES:
            if s.startswith("nay", after):
                return {"doc_type": doc_type, "self_reference": True}, after + 3  # "của Bộ luật này": quoted law text
            name, length, exact = acts.match_name(doc_type, s[after:after + 120])
            if name:
                ref = {"doc_type": doc_type, "name_key": name, **({} if exact else {"name_prefix_match": True})}
                return year_after(ref, text, index, s, after + length), after + length
        # Numbered instruments (and named ones written with a number): read the number from the original text,
        # rejoining a code part split by OCR spaces ("326/2016/UBTVQH 14", "136/2016/NĐ -CP").
        if after < len(index):
            m = NUMBER_RE.match(text, index[after])
            if m:
                code = re.sub(r"[\s–]+", "", m.group(3).replace("–", "-")).strip("-")
                number = f"{int(m.group(1))}/{m.group(2)}/{code}" if code else None
                if number:
                    return {"doc_type": doc_type, "number_key": number_key(number), "number": number}, after + len(stream(m.group(0))[0])
        return {"doc_type": doc_type, "unparsed": True}, after
    return None, pos


def year_after(ref, text, index, s, pos):
    if pos < len(index):
        m = re.match(r"\s*[,(]?\s*(?:năm\s*)?((?:19|20)\d{2})\b", text[index[pos]:index[pos] + 20], re.I)
        if m:
            ref["year"] = m.group(1)
    return ref


def number_key(number):
    return re.sub(r"\s+", "", fold_char_string(number))


def fold_char_string(value):
    return "".join(fold_char(c) for c in value or "")


def with_suffix(text, index, s, number, pos):
    """Attach a letter suffix ("Điều 7a") only when it touches the digits in the original text and ends a word;
    whitespace removal would otherwise turn "Điều 474 Bộ luật" into article "474b"."""
    if pos < len(s) and "a" <= s[pos] <= "z" and index[pos] == index[pos - 1] + 1:
        after = index[pos] + 1
        if after >= len(text) or not text[after].isalpha():
            return number + s[pos], pos + 1
    return number, pos


def mentions(text, spans, acts):
    """Article references in the given (section, start, end) spans, each bound to an instrument when possible."""
    result = []
    for section, start, end in spans:
        s, index = stream(text, start, end)
        groups = []
        for m in ARTICLE_RE.finditer(s):
            article, s_end = with_suffix(text, index, s, m.group(2), m.end())
            items = [{"article": article, "clauses": [m.group(1)] if m.group(1) else [], "s_start": m.start(), "s_end": s_end}]
            # Bare numbers continue the list: "Điều 6, 7, 7a và 9" (a date such as 30/12/2016 does not).
            while True:
                more = MORE_ARTICLE.match(s, items[-1]["s_end"])
                if not more:
                    break
                article, s_end = with_suffix(text, index, s, more.group(1), more.end())
                items.append({"article": article, "clauses": [], "s_start": items[-1]["s_end"], "s_end": s_end})
            if groups and CHAIN_GAP.match(s[groups[-1][-1]["s_end"]:m.start()]):
                groups[-1].extend(items)
            else:
                groups.append(items)
        last_act = None
        for group in groups:
            ref, _ = parse_act(text, index, s, group[-1]["s_end"], acts)
            method = "explicit"
            if ref is not None and ref.get("self_reference"):
                continue
            if ref is not None and ref.get("unparsed"):
                method = "unrecognized_instrument"  # an instrument is named but not readable: never borrow another
            elif ref is None:
                # "Điều 468 quy định ..." reuses the instrument named just before, in the same section.
                if last_act and index[group[0]["s_start"]] - last_act[1] < 400:
                    ref, method = dict(last_act[0]), "context"
                else:
                    method = "no_instrument"
            else:
                last_act = (ref, index[group[-1]["s_end"] - 1])
            o_start, o_end = index[group[0]["s_start"]], index[group[-1]["s_end"] - 1] + 1
            for item in group:
                result.append({"article": item["article"].lstrip("0") or "0", "clauses": item["clauses"],
                               "act_ref": ref, "binding": method, "section": section,
                               "start": o_start, "end": o_end, "raw": text[o_start:o_end]})
    return result


def role(mention, act, sentence_text):
    """substantive / procedural / court_fee / enforcement: only substantive citations seed queries."""
    folded = re.sub(r"\s+", "", fold_char_string(sentence_text))
    name = (act or {}).get("name_key") or (mention.get("act_ref") or {}).get("name_key")
    number = number_key((act or {}).get("number") or "") or (mention.get("act_ref") or {}).get("number_key")
    doc_type = (act or {}).get("doc_type") or (mention.get("act_ref") or {}).get("doc_type")
    numbered = doc_type in {"Nghị quyết", "Pháp lệnh"} and number
    if numbered and number.startswith(FEE_NUMBER_PREFIXES) or ("anphi" in folded and doc_type in {"Nghị quyết", "Pháp lệnh"}):
        return "court_fee"
    if numbered and number.startswith(PROCEDURAL_NUMBER_PREFIXES):
        return "procedural"
    if name in ENFORCEMENT or ("thihanhan" in folded and ("laisuat" in folded or "chiulai" in folded)):
        return "enforcement"
    if name in PROCEDURAL or (name or "").startswith(PROCEDURAL_NAME_PREFIXES):
        return "procedural"
    return "substantive"
