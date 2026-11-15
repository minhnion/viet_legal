import pytest

from legal_recall.evaluate import interval_union, score


GOLD = {"qrels": [{"unit_id": "u1", "grade": 2, "evidence": [{"start": 0, "end": 10}]},
                  {"unit_id": "u2", "grade": 2, "evidence": [{"start": 5, "end": 15}]}],
        "requirement_groups": [{"unit_ids": ["u1", "u2"]}]}


def test_ranking_and_alternative_group_metrics():
    result = score(GOLD, {"ranking": ["u1", "u1", "unknown"]}, 2)
    assert result["recall"] == .5
    assert result["precision"] == .5
    assert result["requirement_coverage"] == 1
    assert result["complete_coverage"] == 1
    assert result["unjudged_returned"] == 1


def test_evidence_union_no_overlap_bonus():
    pred = {"ranking": ["u1", "u2"], "evidence": [{"unit_id": "u1", "spans": [{"start": 0, "end": 10}, {"start": 0, "end": 10}]}]}
    result = score(GOLD, pred, 2)
    assert result["evidence_precision"] == 1
    assert result["evidence_recall"] == .5
    assert result["evidence_f1"] == pytest.approx(2/3)


def test_empty_results_and_wrong_versions():
    result = score(GOLD, {"ranking": ["u1-wrong-version"]}, 2)
    assert result["recall"] == 0
    assert score(GOLD, {}, 2)["f2"] == 0


def test_interval_validation_and_merge():
    assert interval_union([{"start": 0, "end": 5}, {"start": 4, "end": 10}]) == [[0, 10]]
    with pytest.raises(ValueError):
        interval_union([{"start": 1, "end": 1}])
