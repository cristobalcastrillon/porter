import copy
import json

import pandas as pd
import pytest

from porter.evaluate import RESULTS_FILE, SeedResult, cell_matches, evaluate_seed, render, results_path, score_cells, score_fields
from porter.make_dirty import GROUND_TRUTH, TARGET_COLUMNS
from porter.runs import list_runs

MAPPING = json.load(open("examples/mapping.example.json"))


def field(mapping, name):
    return next(m for m in mapping["field_mappings"] if m["target_field"] == name)


def test_example_mapping_scores_full_marks():
    assert score_fields(MAPPING, GROUND_TRUTH) == (9, 9, [])


def test_wrong_column_and_wrong_kind_count_against():
    bad = copy.deepcopy(MAPPING)
    field(bad, "species")["source_columns"] = ["Latin Name"]
    field(bad, "borough")["transform"]["kind"] = "copy"
    strict, lenient, wrong = score_fields(bad, GROUND_TRUTH)
    assert (strict, lenient) == (7, 7)
    assert set(wrong) == {"species", "borough"}


def test_copy_for_cast_is_only_accepted_by_the_lenient_score():
    bad = copy.deepcopy(MAPPING)
    field(bad, "external_id")["transform"]["kind"] = "copy"
    assert score_fields(bad, GROUND_TRUTH)[:2] == (8, 9)


def test_branch_order_matters():
    bad = copy.deepcopy(MAPPING)
    field(bad, "diameter_cm")["source_columns"] = ["Stump Dia", "DBH"]
    assert score_fields(bad, GROUND_TRUTH)[0] == 8


@pytest.mark.parametrize("column,got,want,ok", [
    ("species", None, "", True),
    ("species", "x", "", False),
    ("species", None, "x", False),
    ("species", "PIN OAK", "pin oak", False),
    ("diameter_cm", 30.5, "30.5", True),
    ("diameter_cm", 30.48, "30.5", True),
    ("diameter_cm", 30.6, "30.5", False),
    ("latitude", 40.7000001, "40.7", True),
    ("latitude", 40.71, "40.7", False),
    ("latitude", "40.7", "40.7", False),
    ("inspected_on", "2015-08-27", "2015-08-27", True),
])
def test_cell_matches(column, got, want, ok):
    assert cell_matches(column, got, want) is ok


def test_missing_rows_count_as_wrong():
    row = dict.fromkeys(TARGET_COLUMNS, "")
    expected = pd.DataFrame([{**row, "external_id": "1"}, {**row, "external_id": "2"}])
    correct, total, wrong = score_cells([{**dict.fromkeys(TARGET_COLUMNS), "external_id": "1"}], expected)
    assert total == 18 and correct == 9 and wrong["species"] == 1


def test_fake_end_to_end_is_perfect_and_is_labelled(census, tmp_path, monkeypatch):
    monkeypatch.setenv("PORTER_RUNS_DIR", str(tmp_path))
    result = evaluate_seed(census, 7, 500, "t")
    assert result.error is None
    assert result.fields_strict == 9
    assert result.cells_correct == result.cells_total > 0
    text = render([result], fake=True)
    assert "FAKE MODE" in text and "9.0 / 9" in text and "100.00 %" in text


def test_fake_runs_never_touch_results_md(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTER_RUNS_DIR", str(tmp_path))
    assert results_path(fake=True) != RESULTS_FILE
    assert results_path(fake=False) == RESULTS_FILE


def test_benchmark_runs_stay_out_of_the_ui_list(census, tmp_path, monkeypatch):
    monkeypatch.setenv("PORTER_RUNS_DIR", str(tmp_path))
    evaluate_seed(census, 7, 300, "t")
    assert list_runs() == []


def test_failed_seed_is_reported_not_hidden():
    text = render([SeedResult(7, error="model output invalid"), SeedResult(8, fields_strict=9, fields_lenient=9, cells_correct=1, cells_total=1)], fake=False)
    assert "failed" in text and "model output invalid" in text
    assert "averaged over 1 seeded" in text
