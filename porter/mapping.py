"""Step 2-3: propose a mapping with the model, apply the user's answers, save the approval."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from porter.checks import check_mapping, fields_needing_review
from porter.llm import call_json, load_schema, model_for
from porter.runs import write_json


def propose_mapping(
    profile: dict, target: dict, source_name: str, row_count: int, run_dir: Path, client: Any = None
) -> tuple[dict, list[str]]:
    """Ask the model for a mapping. Returns it and the fields that need review."""
    mapping = call_json(
        "mapping",
        {
            "target_name": target["name"],
            "primary_key": target["primary_key"],
            "target_fields_json": json.dumps(target["fields"], indent=1),
            "source_name": source_name,
            "row_count": row_count,
            "profile_json": json.dumps(profile, indent=1),
            "output_schema_json": json.dumps(load_schema("mapping")),
        },
        "mapping",
        model_for("PORTER_MAPPING_MODEL", "muse-wonder-1.0"),
        run_dir=run_dir,
        check=lambda doc: check_mapping(doc, profile, target),
        client=client,
    )
    write_json(run_dir / "mapping.proposed.json", mapping)
    return mapping, fields_needing_review(mapping)


def apply_answers(mapping: dict, answers: dict[str, str]) -> dict:
    """Return the mapping with each question's chosen option applied. Every question needs an answer."""
    result = copy.deepcopy(mapping)
    by_field = {m["target_field"]: m for m in result["field_mappings"]}
    for question in result["questions"]:
        chosen = answers.get(question["id"])
        if chosen is None:
            raise ValueError(f"question {question['id']} has no answer")
        option = next((o for o in question["options"] if o["id"] == chosen), None)
        if option is None:
            raise ValueError(f"question {question['id']} has no option '{chosen}'")
        field = by_field[question["target_field"]]
        field["source_columns"] = copy.deepcopy(option["resolution"]["source_columns"])
        field["transform"] = copy.deepcopy(option["resolution"]["transform"])
    _refresh_unused(result, mapping)
    return result


def _refresh_unused(result: dict, original: dict) -> None:
    """An answer can free a column or start using one; keep the unused list truthful."""
    used = {c for m in result["field_mappings"] for c in m["source_columns"]}
    was_used = {c for m in original["field_mappings"] for c in m["source_columns"]}
    unused = [u for u in result["unused_source_columns"] if u["column"] not in used]
    known = {u["column"] for u in unused}
    for column in sorted(was_used - used - known):
        unused.append({"column": column, "reason": "Not used after the review answers."})
    result["unused_source_columns"] = unused


def approve(mapping: dict, answers: dict[str, str], profile: dict, target: dict, run_dir: Path) -> dict:
    """Apply the answers, re-check the result, and save runs/<run_id>/mapping.approved.json."""
    final = apply_answers(mapping, answers)
    errors = check_mapping(_without_questions(final), profile, target)
    if errors:
        raise ValueError(f"approved mapping is inconsistent: {errors}")
    approved = {
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "answers": answers,
        "mapping": final,
    }
    write_json(run_dir / "mapping.approved.json", approved)
    return approved


def _without_questions(mapping: dict) -> dict:
    # Questions are settled; a non-recommended answer no longer equals the recommended resolution.
    return {**mapping, "questions": []}
