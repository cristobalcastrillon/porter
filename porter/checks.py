"""Semantic checks for model output that JSON Schema cannot express.

Run these after schema validation. Each check returns a list of readable
errors; an empty list means the document is consistent. On failure, Porter
sends the errors back to the model and retries once.
"""
from __future__ import annotations

import re

REVIEW_THRESHOLD = 0.85


def parse_condition(text: str) -> tuple[str, str, list[str]]:
    """Parse "field == 'v'", "field != 'v'" or "field in ['a', 'b']" into (field, op, values)."""
    match = re.fullmatch(r"\s*(\w+)\s*(==|!=)\s*'([^']*)'\s*", text)
    if match:
        return match.group(1), match.group(2), [match.group(3)]
    match = re.fullmatch(r"\s*(\w+)\s+in\s+\[((?:\s*'[^']*'\s*,?)+)\]\s*", text)
    if match:
        return match.group(1), "in", re.findall(r"'([^']*)'", match.group(2))
    raise ValueError(f"condition {text!r} is not field == 'value', field != 'value' or field in ['a', 'b']")


def condition_is_true(condition: tuple[str, str, list[str]], computed: dict) -> bool:
    """Evaluate a parsed condition against target fields computed so far."""
    field, op, values = condition
    value = computed.get(field)
    if op == "!=":
        return value != values[0]
    return value in values


def check_transform(
    name: str, cols: list[str], transform: dict, fields: dict, source_cols: list[str], variants: dict
) -> list[str]:
    """Checks that apply to a field's mapping and to each option that could replace it."""
    errors: list[str] = []
    kind, params = transform["kind"], transform["params"]
    field = fields.get(name, {})

    unknown = [c for c in cols if c not in source_cols]
    if unknown:
        errors.append(f"{name}: unknown source columns {unknown}")

    if kind == "unmapped":
        if cols:
            errors.append(f"{name}: kind 'unmapped' must not list source columns")
        if field.get("nullable") is False:
            errors.append(f"{name}: required field is left unmapped")
    elif not cols:
        errors.append(f"{name}: kind '{kind}' needs at least one source column")

    if kind == "conditional":
        if params.get("null_when_true"):
            if len(cols) != 1:
                errors.append(f"{name}: 'null_when_true' needs exactly one source column")
            if field.get("nullable") is False:
                errors.append(f"{name}: 'null_when_true' on a required field")
        elif len(cols) < 2:
            errors.append(f"{name}: kind 'conditional' needs at least two source columns")
        errors.extend(_check_condition(name, params.get("condition", ""), list(fields)))

    if kind == "map_values":
        sources = [v for entry in params["value_map"] for v in entry["from"]]
        repeated = sorted({v for v in sources if sources.count(v) > 1})
        if repeated:
            errors.append(f"{name}: value_map lists {repeated} more than once")
        for col in cols:
            uncovered = sorted(variants.get(col, set()) - set(sources))
            if uncovered:
                errors.append(f"{name}: value_map misses variants of '{col}': {uncovered}")
        allowed = field.get("enum")
        for entry in params["value_map"]:
            to = entry["to"]
            if to is None:
                if field.get("nullable") is False:
                    errors.append(f"{name}: maps {entry['from']} to null but the field is required")
            elif allowed and to not in allowed:
                errors.append(f"{name}: '{to}' is not one of {allowed}")
    return errors


def _check_condition(name: str, condition: str, order: list[str]) -> list[str]:
    try:
        field, _, _ = parse_condition(condition)
    except ValueError as exc:
        return [f"{name}: {exc}"]
    if field not in order:
        return [f"{name}: condition uses unknown target field '{field}'"]
    if name in order and order.index(field) >= order.index(name):
        return [f"{name}: condition uses '{field}', which is not computed yet"]
    return []


def check_profile(profile: dict, source_columns: list[str]) -> list[str]:
    """The profile must describe exactly the source columns, in order."""
    errors: list[str] = []
    names = [c["column"] for c in profile["columns"]]
    missing = [c for c in source_columns if c not in names]
    extra = [c for c in names if c not in source_columns]
    if missing:
        errors.append(f"profile is missing columns: {missing}")
    if extra:
        errors.append(f"profile describes columns that do not exist: {extra}")
    if not missing and not extra and names != source_columns:
        errors.append("profile columns are not in source order")

    for key in profile["candidate_keys"]:
        if key not in source_columns:
            errors.append(f"candidate key '{key}' is not a source column")

    for col in profile["columns"]:
        seen: set[str] = set()
        for group in col["canonical_values"]:
            if group["canonical"] not in group["variants"]:
                errors.append(f"{col['column']}: canonical '{group['canonical']}' is not in its own variants")
            repeated = seen & set(group["variants"])
            if repeated:
                errors.append(f"{col['column']}: variants {sorted(repeated)} appear in more than one group")
            seen |= set(group["variants"])
    return errors


def check_mapping(mapping: dict, profile: dict, target: dict) -> list[str]:
    """The mapping must cover every target field and only use real source columns."""
    errors: list[str] = []
    fields = {f["name"]: f for f in target["fields"]}
    source_cols = [c["column"] for c in profile["columns"]]
    variants = {
        c["column"]: {v for g in c["canonical_values"] for v in g["variants"]}
        for c in profile["columns"]
    }

    mapped = [m["target_field"] for m in mapping["field_mappings"]]
    if mapped != list(fields):
        errors.append(f"field_mappings must cover {list(fields)} in this order; got {mapped}")

    used: set[str] = set()
    for m in mapping["field_mappings"]:
        used.update(m["source_columns"])
        errors.extend(
            check_transform(m["target_field"], m["source_columns"], m["transform"], fields, source_cols, variants)
        )

    unused = [u["column"] for u in mapping["unused_source_columns"]]
    if used & set(unused):
        errors.append(f"columns listed as both used and unused: {sorted(used & set(unused))}")
    if [c for c in unused if c not in source_cols]:
        errors.append(f"unused_source_columns lists unknown columns: {[c for c in unused if c not in source_cols]}")
    unaccounted = [c for c in source_cols if c not in used and c not in unused]
    if unaccounted:
        errors.append(f"source columns neither used nor listed as unused: {unaccounted}")

    question_ids = [q["id"] for q in mapping["questions"]]
    if len(question_ids) != len(set(question_ids)):
        errors.append("question ids must be unique")
    for q in mapping["questions"]:
        option_ids = [o["id"] for o in q["options"]]
        if q["target_field"] not in fields:
            errors.append(f"{q['id']}: unknown target field '{q['target_field']}'")
        if len(option_ids) != len(set(option_ids)):
            errors.append(f"{q['id']}: option ids must be unique")
        if q["recommended"] not in option_ids:
            errors.append(f"{q['id']}: recommended option '{q['recommended']}' is not one of {option_ids}")
        for option in q["options"]:
            res = option["resolution"]
            for error in check_transform(
                q["target_field"], res["source_columns"], res["transform"], fields, source_cols, variants
            ):
                errors.append(f"{q['id']} option {option['id']}: {error}")
            mapped_field = next((m for m in mapping["field_mappings"] if m["target_field"] == q["target_field"]), None)
            if option["id"] == q["recommended"] and mapped_field and (
                res["source_columns"] != mapped_field["source_columns"] or res["transform"] != mapped_field["transform"]
            ):
                errors.append(f"{q['id']}: the recommended option's resolution must equal the mapping of '{q['target_field']}'")
    return errors


def fields_needing_review(mapping: dict) -> list[str]:
    """Fields shown to the user: low confidence or an open question."""
    asked = {q["target_field"] for q in mapping["questions"]}
    return [
        m["target_field"]
        for m in mapping["field_mappings"]
        if m["confidence"] < REVIEW_THRESHOLD or m["target_field"] in asked
    ]
