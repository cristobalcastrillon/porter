"""The example profile and mapping must pass the schemas and the semantic checks,
and the checks must catch the mistakes a model is likely to make."""
import copy
import json
from pathlib import Path

import jsonschema

from porter.checks import check_mapping, check_profile, fields_needing_review

ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


PROFILE_SCHEMA = load("schemas/profile.schema.json")
MAPPING_SCHEMA = load("schemas/mapping.schema.json")
PROFILE = load("examples/profile.example.json")
MAPPING = load("examples/mapping.example.json")
TARGET = load("examples/target.assets.json")
SOURCE_COLUMNS = [c["column"] for c in PROFILE["columns"]]


def schema_errors(doc: dict, schema: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    return [e.message for e in validator.iter_errors(doc)]


def test_schemas_are_valid():
    jsonschema.Draft202012Validator.check_schema(PROFILE_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(MAPPING_SCHEMA)


def test_examples_pass_schemas():
    assert schema_errors(PROFILE, PROFILE_SCHEMA) == []
    assert schema_errors(MAPPING, MAPPING_SCHEMA) == []


def test_examples_pass_semantic_checks():
    assert check_profile(PROFILE, SOURCE_COLUMNS) == []
    assert check_mapping(MAPPING, PROFILE, TARGET) == []


def test_review_queue():
    assert fields_needing_review(MAPPING) == ["diameter_cm"]


def test_schema_requires_params_for_kind():
    bad = copy.deepcopy(MAPPING)
    bad["field_mappings"][2]["transform"]["params"] = {}  # map_values without value_map
    assert schema_errors(bad, MAPPING_SCHEMA)


def test_catches_missing_variant():
    bad = copy.deepcopy(MAPPING)
    bad["field_mappings"][7]["transform"]["params"]["value_map"][4]["from"].remove("S.I.")
    assert any("S.I." in e for e in check_mapping(bad, PROFILE, TARGET))


def test_catches_invented_column():
    bad = copy.deepcopy(MAPPING)
    bad["field_mappings"][1]["source_columns"] = ["Species"]
    assert any("unknown source columns" in e for e in check_mapping(bad, PROFILE, TARGET))


def test_catches_value_outside_enum():
    bad = copy.deepcopy(MAPPING)
    bad["field_mappings"][2]["transform"]["params"]["value_map"][0]["to"] = "Alive"
    assert any("not one of" in e for e in check_mapping(bad, PROFILE, TARGET))


def test_catches_unaccounted_column():
    bad = copy.deepcopy(MAPPING)
    bad["unused_source_columns"] = bad["unused_source_columns"][1:]  # drop Latin Name
    assert any("Latin Name" in e for e in check_mapping(bad, PROFILE, TARGET))


def test_catches_missing_target_field():
    bad = copy.deepcopy(MAPPING)
    del bad["field_mappings"][3]  # condition
    assert any("must cover" in e for e in check_mapping(bad, PROFILE, TARGET))
