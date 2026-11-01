"""Step 4: dry run. Transform every row, validate against the target, and list every failure."""
from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import date
from pathlib import Path

from porter.runs import read_json, read_source, write_json
from porter.target import load_target
from porter.transform import load_approved_functions, transform_row

FIRST_DATA_LINE = 2  # the header is line 1 of the CSV


def field_error(field: dict, value: object) -> str | None:
    """Why this value can't go in this field, or None if it can."""
    name_type = field["type"]
    if value is None:
        return None if field.get("nullable", True) else "required field is empty"
    if name_type == "text" and not isinstance(value, str):
        return f"expected text, got {value!r}"
    if name_type == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"expected a decimal, got {value!r}"
        if not math.isfinite(value):
            return f"expected a finite number, got {value!r}"
    if name_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return f"expected an integer, got {value!r}"
    if name_type == "boolean" and not isinstance(value, bool):
        return f"expected a boolean, got {value!r}"
    if name_type == "date":
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            return f"expected an ISO date, got {value!r}"
    if "enum" in field and value not in field["enum"]:
        return f"{value!r} is not one of {field['enum']}"
    if "minimum" in field and value < field["minimum"]:
        return f"{value} is below the minimum {field['minimum']}"
    if "maximum" in field and value > field["maximum"]:
        return f"{value} is above the maximum {field['maximum']}"
    return None


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dry_run(run_dir: Path, target: dict | None = None) -> dict:
    """Run the approved mapping and code over every row. Writes nothing to the target.

    Outputs in the run: dry_run.json, rejected.csv, and valid.jsonl (the rows to
    write, one per external_id, keeping the first occurrence).
    """
    target = target or load_target()
    run = read_json(run_dir / "run.json")
    mapping = read_json(run_dir / "mapping.approved.json")["mapping"]
    functions = load_approved_functions(run_dir)
    fields = {f["name"]: f for f in target["fields"]}
    key = target["primary_key"]
    columns = {m["target_field"]: m["source_columns"] for m in mapping["field_mappings"]}

    rejected: list[dict] = []
    rejected_by_field: dict[str, int] = {}
    kept: dict[str, dict] = {}
    rejected_rows = duplicates = conflicting = 0
    df = read_source(Path(run["source"]))

    for index, row in enumerate(df.to_dict("records")):
        values, errors = transform_row(row, mapping, fields, functions)
        for name, field in fields.items():
            if name not in errors:
                reason = field_error(field, values[name])
                if reason:
                    errors[name] = reason
        if errors:
            rejected_rows += 1
            for name, reason in errors.items():
                rejected_by_field[name] = rejected_by_field.get(name, 0) + 1
                rejected.append({
                    "row": index + FIRST_DATA_LINE,
                    "field": name,
                    "value": " | ".join(row[c] for c in columns[name]),
                    "reason": reason,
                })
            continue
        identifier = values[key]
        if identifier in kept:
            duplicates += 1
            conflicting += kept[identifier] != values
            continue
        kept[identifier] = values

    with (run_dir / "rejected.csv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=["row", "field", "value", "reason"])
        writer.writeheader()
        writer.writerows(rejected)
    valid_path = run_dir / "valid.jsonl"
    with valid_path.open("w") as out:
        for values in kept.values():
            out.write(json.dumps(values) + "\n")

    report = {
        "total_rows": len(df),
        "rows_to_write": len(kept),
        "rejected_rows": rejected_rows,
        "rejected_by_field": rejected_by_field,
        "duplicates_dropped": duplicates,
        "conflicting_duplicates": conflicting,
        "valid_sha256": file_hash(valid_path),
        "mapping_sha256": file_hash(run_dir / "mapping.approved.json"),
        "transform_sha256": file_hash(run_dir / "transform.approved.json"),
    }
    write_json(run_dir / "dry_run.json", report)
    return report
