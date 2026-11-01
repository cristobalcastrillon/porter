import json

import pytest

from porter.sandbox import check_code, compile_function
from porter.target import load_target
from porter.transform import TransformError, apply_field, check_functions, distinct_values, transform_row

import pandas as pd

TARGET = load_target()
FIELDS = {f["name"]: f for f in TARGET["fields"]}
MAPPING = json.load(open("examples/mapping.example.json"))


def entry(kind, cols, **params):
    return {"source_columns": cols, "transform": {"kind": kind, "description": "d", "params": params}}


def run(mapping, row, field="species", computed=None, function=None):
    return apply_field(mapping, row, computed or {}, FIELDS[field], function)


def test_copy_trims_and_blank_is_null():
    assert run(entry("copy", ["a"]), {"a": "  x "}) == "x"
    assert run(entry("copy", ["a"]), {"a": "   "}) is None


@pytest.mark.parametrize("target_type,raw,expected", [
    ("text", " 180683 ", "180683"),
    ("integer", "12", 12),
    ("decimal", "1.5", 1.5),
    ("date", "2015-08-27", "2015-08-27"),
    ("boolean", "Yes", True),
    ("boolean", "n", False),
    ("integer", "", None),
])
def test_cast(target_type, raw, expected):
    assert run(entry("cast", ["a"], target_type=target_type), {"a": raw}) == expected


@pytest.mark.parametrize("target_type,raw", [("integer", "x"), ("decimal", "nan"), ("date", "08/27/2015"), ("boolean", "maybe")])
def test_cast_rejects(target_type, raw):
    with pytest.raises(TransformError):
        run(entry("cast", ["a"], target_type=target_type), {"a": raw})


def test_map_values():
    params = {"value_map": [{"from": ["A", "alive"], "to": "alive"}, {"from": [""], "to": None}], "unmatched": "error"}
    mapping = entry("map_values", ["a"], **params)
    assert run(mapping, {"a": " A "}) == "alive"
    assert run(mapping, {"a": ""}) is None
    with pytest.raises(TransformError, match="not in the value map"):
        run(mapping, {"a": "zombie"})
    assert run(entry("map_values", ["a"], **{**params, "unmatched": "null"}), {"a": "zombie"}) is None
    assert run(entry("map_values", ["a"], **{**params, "unmatched": "keep"}), {"a": "zombie"}) == "zombie"


@pytest.mark.parametrize("raw,expected", [("12", 30.5), ("12 in", 30.5), ('12"', 30.5), (" 3.5 ", 8.9), ("", None)])
def test_convert_unit(raw, expected):
    mapping = entry("convert_unit", ["a"], from_unit="in", to_unit="cm", factor=2.54, decimals=1)
    assert run(mapping, {"a": raw}) == expected


@pytest.mark.parametrize("raw", ["abc", "12 3", "1-2"])
def test_convert_unit_rejects(raw):
    with pytest.raises(TransformError):
        run(entry("convert_unit", ["a"], from_unit="in", to_unit="cm", factor=2.54), {"a": raw})


@pytest.mark.parametrize("raw", ["08/27/2015", "2015-08-27", "Aug 27 2015"])
def test_parse_date(raw):
    mapping = entry("parse_date", ["a"], input_formats=["%m/%d/%Y", "%Y-%m-%d", "%b %d %Y"], day_first=False)
    assert run(mapping, {"a": raw}) == "2015-08-27"


def test_parse_date_rejects():
    mapping = entry("parse_date", ["a"], input_formats=["%Y-%m-%d"], day_first=None)
    with pytest.raises(TransformError, match="matches none"):
        run(mapping, {"a": "soon"})


@pytest.mark.parametrize("raw", ["40.7, -73.9", "(40.7, -73.9)", " [40.7,-73.9] "])
def test_split_strips_wrapping_and_converts(raw):
    assert run(entry("split", ["a"], separator=",", part_index=0), {"a": raw}, "latitude") == 40.7
    assert run(entry("split", ["a"], separator=",", part_index=1), {"a": raw}, "longitude") == -73.9


def test_split_missing_part():
    with pytest.raises(TransformError, match="no part"):
        run(entry("split", ["a"], separator=",", part_index=1), {"a": "40.7"}, "latitude")


def test_conditional_branches_and_unit():
    mapping = entry("conditional", ["dbh", "stump"], condition="status == 'stump'", factor=2.54, decimals=1)
    row = {"dbh": '10"', "stump": "20"}
    assert run(mapping, row, "diameter_cm", {"status": "alive"}) == 25.4
    assert run(mapping, row, "diameter_cm", {"status": "stump"}) == 50.8


def test_conditional_null_when_true():
    mapping = entry("conditional", ["dbh"], condition="status == 'stump'", null_when_true=True, factor=2.54, decimals=1)
    assert run(mapping, {"dbh": "0"}, "diameter_cm", {"status": "stump"}) is None
    assert run(mapping, {"dbh": "10"}, "diameter_cm", {"status": "alive"}) == 25.4


def test_expression_runs_function_and_catches_errors():
    mapping = entry("expression", ["a"])
    assert run(mapping, {"a": " x "}, function=lambda v: v.upper()) == "X"
    with pytest.raises(TransformError, match="ZeroDivisionError"):
        run(mapping, {"a": "x"}, function=lambda v: 1 / 0)
    with pytest.raises(TransformError, match="no approved function"):
        run(mapping, {"a": "x"})


def test_unmapped_is_null():
    assert run(entry("unmapped", []), {}) is None


def test_fields_compute_in_target_order_so_conditions_see_status():
    row = {
        "TreeID": " 9 ", "Species (common)": "", "Status": "STUMP", "Cond.": "", "DBH": "0",
        "Stump Dia": "10", "Location": "(40.7, -73.9)", "Boro": "S.I.", "Inspection Date": "Aug 27 2015",
    }
    values, errors = transform_row(row, MAPPING, FIELDS, {"species": lambda v: v})
    assert errors == {}
    assert values == {
        "external_id": "9", "species": None, "status": "stump", "condition": None, "diameter_cm": 25.4,
        "latitude": 40.7, "longitude": -73.9, "borough": "Staten Island", "inspected_on": "2015-08-27",
    }


def test_distinct_values_ignores_blanks_and_trims():
    result = distinct_values(pd.Series(["a", " a", "b", "", "  "]))
    assert result == {"values": [{"value": "a", "count": 2}, {"value": "b", "count": 1}], "truncated": False}


GOOD = "import re\nTABLE = {'a': 'A'}\n\ndef f(value):\n    return TABLE.get(value) if value else None\n"


def test_good_code_compiles_and_runs():
    assert check_code(GOOD) == []
    assert compile_function(GOOD)("a") == "A"


def test_signature_annotations_are_allowed_but_object_in_the_body_is_not():
    typed = "def f(value: str | None) -> object:\n    return value\n"
    assert compile_function(typed)("x") == "x"
    assert check_code("def f(value: str | None) -> object:\n    return object\n")


def test_allowed_imports_work_at_runtime():
    code = ("import re\nimport math\nfrom datetime import date\nfrom decimal import Decimal\n"
            "def f(value):\n    return re.sub('x', 'y', value) + str(math.floor(1.5)) + str(date(2015, 1, 1)) + str(Decimal('1'))\n")
    assert compile_function(code)("x") == "y12015-01-011"


@pytest.mark.parametrize("line", [
    "import os",
    "import subprocess as s",
    "from os import path",
    "from re import *",
    "from re import enum",
    "from . import x",
    "exec('1')",
    "eval('1')",
    "open('/etc/passwd')",
    "__import__('os')",
    "getattr(value, 'x')",
    "globals()",
    "value.__class__",
    "().__class__.__bases__",
    "re.enum.sys.modules",
    "re._compiler",
    "'{0.__class__}'.format(value)",
    "'{x}'.format_map({})",
    "type(value)",
    "print(value)",
])
def test_ast_check_refuses(line):
    code = f"import re\n\ndef f(value):\n    return {line}\n" if not line.startswith(("import", "from")) else f"{line}\n\ndef f(value):\n    return value\n"
    assert check_code(code), line
    with pytest.raises(ValueError):
        compile_function(code)


@pytest.mark.parametrize("code", [
    "x = 1",
    "def g(value):\n    return value",
    "def f(a, b):\n    return a",
    "def f(*a):\n    return a",
    "def f(v):\n    while True:\n        pass",
    "class A:\n    pass\ndef f(v):\n    return v",
    "def f(v):\n    return v\ndef f(v):\n    return v",
    "import re\nre.purge()\ndef f(v):\n    return v",
    "def f(v:\n",
])
def test_ast_check_refuses_structure(code):
    assert check_code(code), code


def test_restricted_builtins_stop_what_the_ast_misses():
    code = "def f(value):\n    return bytes(1)\n"
    assert check_code(code) == []
    with pytest.raises(NameError):
        compile_function(code)("x")


def test_check_functions_needs_every_field():
    function = {"target_field": "species", "code": GOOD, "explanation": "x"}
    assert check_functions({"functions": [function]}, ["species"]) == []
    assert check_functions({"functions": []}, ["species"])
    assert check_functions({"functions": [{**function, "code": "import os\ndef f(v):\n    return v"}]}, ["species"])
