"""Section and citation parsing on strings taken from real judgments (OCR splits, lists, abbreviations)."""

import pytest

from legal_recall.citations import ActIndex, judgment_date, mentions, role, sections
from legal_recall.ingest import act_name


def act(id, doc_type, name_key, issued=None, effective=None, expiry=None, number=None, amendment=False):
    return {"id": id, "number": number, "number_key": number.lower() if number else None, "doc_type": doc_type, "name_key": name_key,
            "title": id, "issue_date": issued, "effective_date": effective, "expiry_date": expiry, "status": "", "is_amendment": int(amendment)}


ACTS = ActIndex([
    act("blds2005", "Bộ luật", "dansu", "2005-06-14", "2006-01-01", "2017-01-01", "33/2005/QH11"),
    act("blds2015", "Bộ luật", "dansu", "2015-11-24", "2017-01-01", None, "91/2015/QH13"),
    act("blhs1999", "Bộ luật", "hinhsu", "1999-12-21", None, None, "15/1999/QH10"),
    act("blhs2015", "Bộ luật", "hinhsu", "2015-11-27", "2018-01-01", None, "100/2015/QH13"),
    act("blhs2009", "Bộ luật", "hinhsu", "2009-06-19", None, None, "37/2009/QH12", amendment=True),
    act("blttds", "Bộ luật", "totungdansu", "2015-11-25", "2016-07-01", None, "92/2015/QH13"),
    act("bltths", "Bộ luật", "totunghinhsu", "2015-11-27", "2018-01-01", None, "101/2015/QH13"),
    act("thads", "Luật", "thihanhandansu", "2008-11-14", "2009-07-01", "2026-07-01", "26/2008/QH12"),
    act("thahs", "Luật", "thihanhanhinhsu", "2010-06-17", "2011-07-01", None, "53/2010/QH12"),
    act("xlvphc", "Luật", "xulyviphamhanhchinh"),
    act("hngd", "Luật", "honnhanvagiadinh", "2014-06-19", "2015-01-01", None, "52/2014/QH13"),
    act("pl2022", "Pháp lệnh", "trinhtuthutucxemxetquyetdinh", "2022-01-01", "2022-03-01", None, "03/2022/UBTVQH15"),
    act("nq326", "Nghị quyết", None, "2016-12-30", "2017-01-01", None, "326/2016/UBTVQH14-MUC-THU-AN-PHI"),
])


def found(text, section="decision"):
    return [(m["article"], m["clauses"], m["binding"], m["act_ref"]) for m in mentions(text, [(section, 0, len(text))], ACTS)]


def test_sections_tolerate_heading_variants_and_skip_body_lines():
    text = ("Quyết định đưa vụ án ra xét xử số 15/2019\nNỘI DUNG VỤ  ÁN:\nA vay tiền.\nNHẬN ĐỊNH CỦA HỘI ĐỒNG XÉT XỬ\n"
            "[1] Xét thấy có căn cứ.\nQUYẾT ĐỊNH:\nXử: chấp nhận.")
    spans = sections(text)
    assert set(spans) == {"facts", "reasoning", "decision"}
    assert text[slice(*spans["facts"])].strip() == "A vay tiền."
    assert text[slice(*spans["decision"])].strip() == "Xử: chấp nhận."
    assert "facts" not in sections("QUYẾT ĐỊNH\nCÔNG NHẬN THUẬN TÌNH LY HÔN\nXÉT THẤY\nTự nguyện.\nQUYẾT ĐỊNH\n1. Công nhận.")
    assert "facts" in sections("NỘI DUNG VIỆC DÂN SỰ:\nBà S yêu cầu.\nNHẬN ĐỊNH CỦA TÒA ÁN\nCó căn cứ.")


def test_article_lists_bind_to_the_following_instrument():
    assert found("theo khoản 3 Điều 26 của Bộ luật tố tụng dân sự") == [("26", ["3"], "explicit", {"doc_type": "Bộ luật", "name_key": "totungdansu"})]
    result = found("theo quy định tại các Điều 6,7, 7a và 9 , Điều 30 Luật thi hành án dân sự năm 2014")
    assert [r[0] for r in result] == ["6", "7", "7a", "9", "30"]
    assert all(r[3] == {"doc_type": "Luật", "name_key": "thihanhandansu", "year": "2014"} for r in result)
    # Whitespace removal must not turn "474 Bộ" into article "474b".
    assert [r[0] for r in found("đúng theo Điều 471, 474 Bộ luật dân sự năm 2005.")] == ["471", "474"]


def test_point_and_clause_chains_and_reversed_clause_order():
    result = found("Áp dụng khoản 1 Điều 191; điểm i,s khoản 1 Điều 51 BLHS.")
    assert [(r[0], r[1]) for r in result] == [("191", ["1"]), ("51", ["1"])]
    assert all(r[3]["name_key"] == "hinhsu" for r in result)
    result = found("khoản 1 Điều 173, Điều 15, các điểm b, h, r, s khoản 1, khoản 2 Điều 51 Bộ luật hình sự năm 2015")
    assert [r[0] for r in result] == ["173", "15", "51"] and all(r[3]["year"] == "2015" for r in result)
    assert found("cần áp dụng Điều 47 khoản 1 điểm a Bộ luật hình sự 2015")[0][3] == {"doc_type": "Bộ luật", "name_key": "hinhsu", "year": "2015"}


def test_ocr_splits_abbreviations_and_numbers():
    assert found("theo quy định tại Điều 30 Lu ật Thi  hành án dân sự.")[0][3]["name_key"] == "thihanhandansu"
    assert found("Điều 147 Bô ̣ luâ ̣t tố tu ̣ng dân sư;")[0][3]["name_key"] == "totungdansu"
    assert found("Điều 51/ BLHS  năm 2015.")[0][3] == {"doc_type": "Bộ luật", "name_key": "hinhsu", "abbreviation": "blhs", "year": "2015"}
    assert found("khoản 1, khoản 3 Điều 329 BLTTHS năm 2015")[0][3]["name_key"] == "totunghinhsu"
    ref = found("khoản 1 Điều 24 Nghị quyết số 326/2016/UBTVQH 14  ngày 30/12/2016")[0][3]
    assert ref["number"] == "326/2016/UBTVQH14" and ref["doc_type"] == "Nghị quyết"
    assert found("Điều 4 Nghị định số 136/2016/NĐ -CP về sửa đổi")[0][3]["number"] == "136/2016/NĐ-CP"


def test_names_with_punctuation_aliases_and_misspellings():
    assert found("Điều 8, 9 Luật Hôn nhân & gia đinh năm 2014.")[0][3]["name_key"] == "honnhanvagiadinh"
    assert found("khoản 1 Điều 51 Luật HNGĐ năm 2014")[0][3]["name_key"] == "honnhanvagiadinh"
    assert found("Điều 11, Điều 12 Pháp lệnh trình tự, thủ tục xem xét, quyết định áp dụng")[0][3]["name_key"] == "trinhtuthutucxemxetquyetdinh"
    ref = found("Điều 28, 35 của Bộ luật Tố tụng dân năm 2015;")[0][3]
    assert ref["name_key"] == "totungdansu" and ref["name_prefix_match"]
    # A shared prefix that fits two instruments is not accepted.
    assert found("Điều 30 Luật thi hành án.")[0][2] == "unrecognized_instrument"


def test_context_binding_and_no_borrowing_for_unreadable_or_quoted_instruments():
    result = found("khoản 2 Điều 468 Bộ luật dân sự năm 2015. Điều 468 quy định: lãi suất không vượt quá 20%.")
    assert [r[2] for r in result] == ["explicit", "context"] and result[1][3]["year"] == "2015"
    result = found("Điều 42 Bộ luật Hình sự; Điều 584, 585 Bộ luật D n sự năm 2015.")
    assert [r[2] for r in result] == ["explicit", "unrecognized_instrument", "unrecognized_instrument"]
    assert found("các tội quy định tại các Điều 168, 169 của Bộ luật này, chưa được xóa án tích") == []
    assert found("Điều 52 . Xét thấy cần phải có một mức hình phạt")[0][2] == "no_instrument"


def test_version_selection_by_year_judgment_date_and_succession():
    assert ACTS.pick("Bộ luật", "dansu", "2005", "2019-05-15")[:2] == (ACTS.by_name[("Bộ luật", "dansu")][0], "name_and_year")
    act_, method, flags = ACTS.pick("Bộ luật", "dansu", None, "2016-03-01")
    assert act_["id"] == "blds2005" and method == "in_force_on_judgment_date"
    # BLHS 1999 lacks an effective date: it runs from its issue date until BLHS 2015 takes effect (2018-01-01).
    act_, method, flags = ACTS.pick("Bộ luật", "hinhsu", None, "2017-06-01")
    assert act_["id"] == "blhs1999" and "effective_date_missing" in flags
    assert ACTS.pick("Bộ luật", "hinhsu", None, "2019-06-01")[0]["id"] == "blhs2015"
    act_, method, flags = ACTS.pick("Bộ luật", "dansu", "2010", "2019-05-15")
    assert act_["id"] == "blds2015" and "cited_year_not_found" in flags
    # An instrument issued after the judgment can never be the cited one.
    assert ACTS.pick("Pháp lệnh", "trinhtuthutucxemxetquyetdinh", None, "2018-04-01") == (None, "act_not_in_corpus", [])
    # A single undated instrument is accepted but flagged.
    act_, method, flags = ACTS.pick("Luật", "xulyviphamhanhchinh", None, "2018-04-01")
    assert act_["id"] == "xlvphc" and method == "only_act_with_name" and flags == ["act_dates_missing"]
    # A cited instrument that expired before the judgment and has no successor in the corpus stays unresolved.
    assert ACTS.pick("Luật", "thihanhandansu", None, "2027-01-01")[:2] == (None, "version_ambiguous")


def test_number_lookup_exact_slug_and_loose():
    assert ACTS.by_number("326/2016/ubtvqh14")[0][0]["id"] == "nq326"
    assert ACTS.by_number("326/2016/ubtvqh", "Nghị quyết") == ([ACTS.exact_number["326/2016/ubtvqh14-muc-thu-an-phi"][0]], "instrument_number_loose")
    assert ACTS.by_number("326/2016/ubtvqh", "Thông tư") == ([], "instrument_number")


@pytest.mark.parametrize("sentence,ref,expected", [
    ("Bà S phải chịu án phí theo Điều 27", {"doc_type": "Nghị quyết", "number_key": "326/2016/ubtvqh"}, "court_fee"),
    ("Về án phí: theo Điều 23 Nghị quyết 326", {"doc_type": "Nghị quyết", "unparsed": True}, "court_fee"),
    ("chịu lãi của số tiền còn phải thi hành án theo mức lãi suất quy định tại khoản 2 Điều 468", {"doc_type": "Bộ luật", "name_key": "dansu"}, "enforcement"),
    ("Thời hiệu thi hành án theo Điều 30", {"doc_type": "Luật", "name_key": "thihanhandansu"}, "enforcement"),
    ("thẩm quyền theo Điều 35", {"doc_type": "Bộ luật", "name_key": "totungdansu"}, "procedural"),
    ("Căn cứ các Điều 20, 22 Pháp lệnh 09/2014", {"doc_type": "Pháp lệnh", "number_key": "09/2014/utvqh13"}, "procedural"),
    ("buộc bà S trả số tiền vay theo Điều 466", {"doc_type": "Bộ luật", "name_key": "dansu"}, "substantive"),
])
def test_roles(sentence, ref, expected):
    assert role({"act_ref": ref}, None, sentence) == expected


def test_judgment_date_metadata_then_header():
    assert judgment_date("", {"issued_date": "2019-05-15"}) == "2019-05-15"
    assert judgment_date("Bản án số: 11/2019/DS-ST.\nNgày: 15 – 5 – 2019 .", {"issued_date": "1899-01-01"}) == "2019-05-15"
    assert judgment_date("Ngày 15 tháng 5 năm 2019, tại trụ sở", {}) == "2019-05-15"


@pytest.mark.parametrize("title,doc_type,expected", [
    ("Bo luat dan su", "Bộ luật", ("dansu", False)),
    ("Bo luat Dan su 2005 33 2005 QH11", "Bộ luật", ("dansu", False)),
    ("Bo luat hinh su 2009 sua doi 37 2009 QH12", "Bộ luật", ("hinhsu", True)),
    ("Luat Thi hanh an dan su sua doi", "Luật", ("thihanhandansusuadoi", True)),
    ("Phap lenh 09 2014 UBTVQH13 trinh tu thu tuc xem xet", "Pháp lệnh", ("trinhtuthutucxemxet", False)),
    ("Nghi quyet 326 2016 UBTVQH14 an phi", "Nghị quyết", (None, False)),
])
def test_act_names_from_tvpl_titles(title, doc_type, expected):
    assert act_name(title, doc_type) == expected
