import csv
import json

import pytest

from porter.make_dirty import build
from porter.mapping import approve
from porter.pipeline import run_to_dry_run
from porter.runs import read_json
from porter.target import load_target
from porter.transform import approve_code, load_approved_functions
from porter.validate import dry_run, field_error

FIELDS = {f["name"]: f for f in load_target()["fields"]}


@pytest.mark.parametrize("field,value,message", [
    ("status", None, "required"),
    ("status", "zombie", "not one of"),
    ("species", 3, "expected text"),
    ("diameter_cm", "12", "expected a decimal"),
    ("diameter_cm", True, "expected a decimal"),
    ("diameter_cm", float("inf"), "finite"),
    ("diameter_cm", -1, "below the minimum"),
    ("latitude", 50.0, "above the maximum"),
    ("inspected_on", "08/27/2015", "ISO date"),
])
def test_field_errors(field, value, message):
    assert message in field_error(FIELDS[field], value)


@pytest.mark.parametrize("field,value", [
    ("species", None), ("condition", "good"), ("diameter_cm", 0), ("latitude", 40.7),
    ("inspected_on", "2015-08-27"), ("borough", "Queens"),
])
def test_field_accepts(field, value):
    assert field_error(FIELDS[field], value) is None


@pytest.fixture
def legacy_file(census, tmp_path, monkeypatch):
    monkeypatch.setenv("PORTER_RUNS_DIR", str(tmp_path / "runs"))
    legacy, expected = build(census, 1000, 7)
    path = tmp_path / "legacy.csv"
    legacy.to_csv(path, index=False)
    return path, legacy, expected


def read_valid(run_dir):
    return [json.loads(l) for l in (run_dir / "valid.jsonl").read_text().splitlines()]


def test_end_to_end_fake_run_matches_ground_truth(legacy_file):
    path, legacy, expected = legacy_file
    run_dir, proposed, report = run_to_dry_run(path, "e2e")
    assert report["total_rows"] == 1000
    assert report["rejected_rows"] == 0
    assert report["duplicates_dropped"] == 1000 - len(expected)
    assert report["rows_to_write"] == len(expected)
    rows = {r["external_id"]: r for r in read_valid(run_dir)}
    for record in expected.to_dict("records"):
        got = rows[record["external_id"]]
        assert got["species"] == (record["species"] or None)
        assert got["status"] == record["status"]
        assert got["condition"] == (record["condition"] or None)
        assert got["diameter_cm"] == record["diameter_cm"]
        assert got["latitude"] == record["latitude"]
        assert got["longitude"] == record["longitude"]
        assert got["borough"] == record["borough"]
        assert got["inspected_on"] == record["inspected_on"]


def test_bad_rows_are_rejected_with_reasons(legacy_file):
    path, legacy, _ = legacy_file
    legacy = legacy.copy()
    legacy.loc[0, "Status"] = "Zombie"
    legacy.loc[1, "Inspection Date"] = "soon"
    legacy.loc[2, "Location"] = "(10.0, -73.9)"
    legacy.to_csv(path, index=False)
    run_dir, _, report = run_to_dry_run(path, "bad")
    with (run_dir / "rejected.csv").open() as source:
        rejected = list(csv.DictReader(source))
    # Exact repeats of a corrupted row fail too, so match on the rows we changed.
    by_row = {r["row"]: r for r in rejected}
    assert report["rejected_rows"] >= 3
    assert by_row["2"]["field"] == "status" and "not in the value map" in by_row["2"]["reason"]
    assert by_row["3"]["field"] == "inspected_on" and by_row["3"]["value"] == "soon"
    assert by_row["4"]["field"] == "latitude" and "below the minimum" in by_row["4"]["reason"]


def test_other_answers_flow_through(legacy_file):
    path, _, _ = legacy_file
    run_dir, _, report = run_to_dry_run(path, "nulls", answers={"q1": "b"})
    stumps = [r for r in read_valid(run_dir) if r["status"] == "stump"]
    assert stumps and all(r["diameter_cm"] is None for r in stumps)
    assert report["rejected_rows"] == 0


def test_code_changed_after_approval_is_refused(legacy_file):
    path, _, _ = legacy_file
    run_dir, _, _ = run_to_dry_run(path, "tamper")
    approved = read_json(run_dir / "transform.approved.json")
    approved["functions"][0]["code"] = "def f(value):\n    return 'x'\n"
    (run_dir / "transform.approved.json").write_text(json.dumps(approved))
    with pytest.raises(ValueError, match="changed after approval"):
        load_approved_functions(run_dir)
    with pytest.raises(ValueError):
        dry_run(run_dir)
