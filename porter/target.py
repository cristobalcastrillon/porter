"""The target description: examples/target.assets.json, checked against PostgREST's OpenAPI spec."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET_FILE = ROOT / "examples" / "target.assets.json"

# Our type -> what PostgREST reports as (type, format). None means any format.
OPENAPI_TYPES = {
    "text": ("string", None),
    "decimal": ("number", None),
    "integer": ("integer", None),
    "date": ("string", "date"),
    "boolean": ("boolean", None),
}


def load_target(path: Path = TARGET_FILE) -> dict:
    return json.loads(path.read_text())


def check_against_openapi(target: dict, spec: dict) -> list[str]:
    """Compare field names, types and nullability with the live spec. CHECK constraints aren't in it."""
    definition = spec.get("definitions", {}).get(target["name"])
    if definition is None:
        return [f"table '{target['name']}' is not in the OpenAPI spec"]
    properties = definition.get("properties", {})
    required = set(definition.get("required", []))
    errors: list[str] = []
    for field in target["fields"]:
        name = field["name"]
        live = properties.get(name)
        if live is None:
            errors.append(f"{name}: not in the live table")
            continue
        want_type, want_format = OPENAPI_TYPES[field["type"]]
        if live.get("type") != want_type:
            errors.append(f"{name}: live type is {live.get('type')}, expected {want_type}")
        if want_format and live.get("format") != want_format:
            errors.append(f"{name}: live format is {live.get('format')}, expected {want_format}")
        if not field.get("nullable", True) and name not in required:
            errors.append(f"{name}: required here but nullable in the live table")
        if field.get("nullable", True) and name in required:
            errors.append(f"{name}: nullable here but required in the live table")
    return errors
