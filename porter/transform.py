"""Step 4: the deterministic engine, and the model-written code for expression fields."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from porter.checks import condition_is_true, parse_condition
from porter.llm import call_json, load_schema, model_for
from porter.runs import read_json, write_json
from porter.sandbox import check_code, compile_function

MAX_DISTINCT_VALUES = 500
TRUE_WORDS = {"true", "t", "yes", "y", "1"}
FALSE_WORDS = {"false", "f", "no", "n", "0"}
NUMBER_PREFIX = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)")

Function = Callable[[str | None], object]


class TransformError(ValueError):
    """This value can't be transformed. The reason ends up in rejected.csv."""


def clean(raw: str | None) -> str | None:
    """Trim a cell. Blank becomes None."""
    text = (raw or "").strip()
    return text or None


def parse_number(text: str) -> float:
    """Read a number, ignoring trailing unit text like ' in' or a closing inch mark."""
    match = NUMBER_PREFIX.match(text.replace(",", ""))
    if not match:
        raise TransformError(f"'{text}' is not a number")
    rest = text.replace(",", "")[match.end():].strip()
    if rest and not re.fullmatch(r"[a-zA-Z\"'.°]+", rest):
        raise TransformError(f"'{text}' is not a number with a unit")
    return float(match.group())


def cast_value(text: str | None, target_type: str) -> Any:
    if text is None:
        return None
    if target_type == "text":
        return text
    if target_type == "decimal":
        try:
            number = float(text)
        except ValueError:
            raise TransformError(f"'{text}' is not a decimal") from None
        if not math.isfinite(number):
            raise TransformError(f"'{text}' is not a finite number")
        return number
    if target_type == "integer":
        try:
            return int(text)
        except ValueError:
            raise TransformError(f"'{text}' is not an integer") from None
    if target_type == "date":
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise TransformError(f"'{text}' is not an ISO date") from None
    if target_type == "boolean":
        word = text.lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise TransformError(f"'{text}' is not a boolean")
    raise TransformError(f"unknown target type '{target_type}'")


def map_value(text: str | None, params: dict) -> Any:
    table = {source.strip(): entry["to"] for entry in params["value_map"] for source in entry["from"]}
    key = text or ""
    if key in table:
        return table[key]
    unmatched = params["unmatched"]
    if unmatched == "null":
        return None
    if unmatched == "keep":
        return text
    raise TransformError(f"'{key}' is not in the value map")


def convert_value(text: str | None, params: dict) -> float | None:
    if text is None:
        return None
    number = parse_number(text) * params["factor"]
    decimals = params.get("decimals")
    return round(number, decimals) if decimals is not None else number


def parse_date_value(text: str | None, params: dict) -> str | None:
    if text is None:
        return None
    for fmt in params["input_formats"]:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise TransformError(f"'{text}' matches none of {params['input_formats']}")


def split_value(text: str | None, params: dict, field_type: str) -> Any:
    if text is None:
        return None
    if text[0] in "([" and text[-1] in ")]":
        text = text[1:-1]
    parts = text.split(params["separator"])
    if params["part_index"] >= len(parts):
        raise TransformError(f"'{text}' has no part {params['part_index']}")
    return cast_value(clean(parts[params["part_index"]]), field_type)


def conditional_value(mapping: dict, row: dict, computed: dict, field_type: str) -> Any:
    params = mapping["transform"]["params"]
    columns = mapping["source_columns"]
    is_true = condition_is_true(parse_condition(params["condition"]), computed)
    if is_true and params.get("null_when_true"):
        return None
    text = clean(row[columns[1] if is_true else columns[0]])
    if "factor" in params:
        return convert_value(text, params)
    return cast_value(text, field_type)


def apply_field(mapping: dict, row: dict, computed: dict, field: dict, function: Function | None) -> Any:
    """Compute one target field for one row. Raises TransformError with the reason."""
    transform = mapping["transform"]
    kind, params = transform["kind"], transform["params"]
    if kind == "unmapped":
        return None
    if kind == "conditional":
        return conditional_value(mapping, row, computed, field["type"])
    text = clean(row[mapping["source_columns"][0]])
    if kind == "copy":
        return text
    if kind == "cast":
        return cast_value(text, params["target_type"])
    if kind == "map_values":
        return map_value(text, params)
    if kind == "convert_unit":
        return convert_value(text, params)
    if kind == "parse_date":
        return parse_date_value(text, params)
    if kind == "split":
        return split_value(text, params, field["type"])
    if kind == "expression":
        if function is None:
            raise TransformError("no approved function for this field")
        try:
            return function(text)
        except Exception as exc:  # generated code may raise anything; it is a row problem, not a crash
            raise TransformError(f"function raised {type(exc).__name__}: {exc}") from None
    raise TransformError(f"unknown transform kind '{kind}'")


def transform_row(
    row: dict, mapping: dict, fields: dict[str, dict], functions: dict[str, Function]
) -> tuple[dict, dict[str, str]]:
    """Compute target fields in target order. Returns (values, {field: error reason})."""
    computed: dict = {}
    errors: dict[str, str] = {}
    for entry in mapping["field_mappings"]:
        name = entry["target_field"]
        try:
            computed[name] = apply_field(entry, row, computed, fields[name], functions.get(name))
        except TransformError as exc:
            computed[name] = None
            errors[name] = str(exc)
    return computed, errors


def expression_fields(mapping: dict) -> list[dict]:
    return [m for m in mapping["field_mappings"] if m["transform"]["kind"] == "expression"]


def distinct_values(series: pd.Series) -> dict:
    counts = Counter(clean(v) for v in series)
    counts.pop(None, None)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {
        "values": [{"value": v, "count": n} for v, n in ordered[:MAX_DISTINCT_VALUES]],
        "truncated": len(ordered) > MAX_DISTINCT_VALUES,
    }


def check_functions(doc: dict, wanted: list[str]) -> list[str]:
    """One function per expression field, and each must pass the AST check."""
    got = [f["target_field"] for f in doc["functions"]]
    errors = [] if sorted(got) == sorted(wanted) else [f"expected functions for {wanted}, got {got}"]
    for function in doc["functions"]:
        errors.extend(f"{function['target_field']}: {e}" for e in check_code(function["code"]))
    return errors


def generate_code(mapping: dict, df: pd.DataFrame, target: dict, run_dir: Path, client: Any = None) -> dict:
    """Ask the model for expression functions, once. Mappings without any skip the call."""
    fields = {f["name"]: f for f in target["fields"]}
    wanted = expression_fields(mapping)
    if not wanted:
        doc: dict = {"functions": []}
    else:
        payload = [
            {
                "target_field": m["target_field"],
                "target": fields[m["target_field"]],
                "source_column": m["source_columns"][0],
                "description": m["transform"]["description"],
                "distinct_values": distinct_values(df[m["source_columns"][0]]),
            }
            for m in wanted
        ]
        names = [m["target_field"] for m in wanted]
        doc = call_json(
            "transform",
            {
                "expression_fields_json": json.dumps(payload, indent=1),
                "output_schema_json": json.dumps(load_schema("transform")),
            },
            "transform",
            model_for("PORTER_MAPPING_MODEL", "muse-wonder-1.0"),
            run_dir=run_dir,
            check=lambda d: check_functions(d, names),
            client=client,
        )
    write_json(run_dir / "transform.proposed.json", doc)
    return doc


def code_hash(functions: list[dict]) -> str:
    return hashlib.sha256(json.dumps(functions, sort_keys=True).encode()).hexdigest()


def approve_code(doc: dict, run_dir: Path) -> dict:
    """Record that a human reviewed this code. Re-checks the AST before saving."""
    for function in doc["functions"]:
        compile_function(function["code"])
    approved = {
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "functions": doc["functions"],
        "sha256": code_hash(doc["functions"]),
    }
    write_json(run_dir / "transform.approved.json", approved)
    return approved


def load_approved_functions(run_dir: Path) -> dict[str, Function]:
    """Compile the approved code, refusing it if it changed after approval."""
    approved = read_json(run_dir / "transform.approved.json")
    if code_hash(approved["functions"]) != approved["sha256"]:
        raise ValueError("transform code changed after approval")
    return {f["target_field"]: compile_function(f["code"]) for f in approved["functions"]}
