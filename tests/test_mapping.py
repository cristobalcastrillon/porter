import copy
import json

import pytest

from porter.checks import check_mapping, condition_is_true, parse_condition
from porter.mapping import apply_answers, approve, propose_mapping
from porter.target import check_against_openapi, load_target

PROFILE = json.load(open("examples/profile.example.json"))
MAPPING = json.load(open("examples/mapping.example.json"))
TARGET = load_target()


def field(mapping, name):
    return next(m for m in mapping["field_mappings"] if m["target_field"] == name)


@pytest.mark.parametrize("text,computed,expected", [
    ("status == 'stump'", {"status": "stump"}, True),
    ("status == 'stump'", {"status": "alive"}, False),
    ("status != 'stump'", {"status": "alive"}, True),
    ("status != 'stump'", {"status": None}, True),
    ("status in ['stump', 'dead']", {"status": "dead"}, True),
    ("status in ['stump', 'dead']", {"status": "alive"}, False),
])
def test_condition_grammar(text, computed, expected):
    assert condition_is_true(parse_condition(text), computed) is expected


@pytest.mark.parametrize("text", ["status = 'x'", "len(status) > 1", "status == stump", "__import__('os')", ""])
def test_condition_grammar_rejects_the_rest(text):
    with pytest.raises(ValueError):
        parse_condition(text)


def test_check_catches_bad_conditions():
    for condition, message in [
        ("borough == 'Queens'", "not computed yet"),
        ("nope == 'x'", "unknown target field"),
        ("1 + 1", "is not field"),
    ]:
        bad = copy.deepcopy(MAPPING)
        field(bad, "diameter_cm")["transform"]["params"]["condition"] = condition
        assert any(message in e for e in check_mapping(bad, PROFILE, TARGET)), condition


def test_check_requires_recommended_resolution_to_match():
    bad = copy.deepcopy(MAPPING)
    bad["questions"][0]["recommended"] = "b"
    assert any("must equal the mapping" in e for e in check_mapping(bad, PROFILE, TARGET))


def test_check_refuses_copying_a_mixed_case_column():
    assert check_mapping(MAPPING, PROFILE, TARGET) == []
    bad = copy.deepcopy(MAPPING)
    field(bad, "species")["transform"] = {"kind": "copy", "description": "Copy the name.", "params": {}}
    assert any("species: the profile reports inconsistent casing" in e for e in check_mapping(bad, PROFILE, TARGET))


def test_check_covers_option_resolutions():
    bad = copy.deepcopy(MAPPING)
    bad["questions"][0]["options"][2]["resolution"]["source_columns"] = ["Diameter"]
    assert any("q1 option c" in e and "unknown source" in e for e in check_mapping(bad, PROFILE, TARGET))


def test_propose_in_fake_mode(tmp_path):
    mapping, review = propose_mapping(PROFILE, TARGET, "x.csv", 5000, tmp_path)
    assert review == ["diameter_cm"]
    assert (tmp_path / "mapping.proposed.json").exists()


def test_recommended_answer_keeps_the_mapping():
    assert apply_answers(MAPPING, {"q1": "a"})["field_mappings"] == MAPPING["field_mappings"]


def test_other_answers_change_field_and_free_the_column():
    for answer, kind in (("b", "conditional"), ("c", "convert_unit")):
        result = apply_answers(MAPPING, {"q1": answer})
        assert field(result, "diameter_cm")["transform"]["kind"] == kind
        assert field(result, "diameter_cm")["source_columns"] == ["DBH"]
        assert "Stump Dia" in [u["column"] for u in result["unused_source_columns"]]


def test_unanswered_and_invalid_answers_are_refused():
    with pytest.raises(ValueError, match="no answer"):
        apply_answers(MAPPING, {})
    with pytest.raises(ValueError, match="no option"):
        apply_answers(MAPPING, {"q1": "z"})


@pytest.mark.parametrize("answer", ["a", "b", "c"])
def test_approve_saves_a_consistent_mapping(tmp_path, answer):
    approve(MAPPING, {"q1": answer}, PROFILE, TARGET, tmp_path)
    saved = json.loads((tmp_path / "mapping.approved.json").read_text())
    assert saved["answers"] == {"q1": answer}


def test_openapi_check():
    spec = {"definitions": {"assets": {
        "required": ["external_id", "status", "latitude", "longitude", "borough", "migrated_at"],
        "properties": {
            "external_id": {"type": "string"}, "species": {"type": "string"}, "status": {"type": "string"},
            "condition": {"type": "string"}, "diameter_cm": {"type": "number"},
            "latitude": {"type": "number"}, "longitude": {"type": "number"},
            "borough": {"type": "string"}, "inspected_on": {"type": "string", "format": "date"},
            "migrated_at": {"type": "string"},
        },
    }}}
    assert check_against_openapi(TARGET, spec) == []
    spec["definitions"]["assets"]["properties"]["diameter_cm"]["type"] = "string"
    del spec["definitions"]["assets"]["properties"]["borough"]
    errors = check_against_openapi(TARGET, spec)
    assert len(errors) == 2
