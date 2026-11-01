"""Step 1: deterministic column statistics, then the model's semantic profile."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from porter.checks import check_profile
from porter.llm import call_json, load_schema, model_for
from porter.runs import read_source, write_json

TOP_VALUES = 15
MAX_SAMPLES = 20
MAX_SHAPES = 20
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d %Y")
BOOLEANS = {"true", "false", "yes", "no", "y", "n", "t", "f"}


def value_shape(value: str) -> str:
    """Letters become a/A and digits become 9. Everything else stays."""
    shape = re.sub(r"[a-z]", "a", value)
    shape = re.sub(r"[A-Z]", "A", shape)
    return re.sub(r"[0-9]", "9", shape)


def _is_date(value: str) -> bool:
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            pass
    return False


def infer_type(values: list[str]) -> str:
    """The narrowest type every non-blank value fits; 'text' when they disagree."""
    if not values:
        return "empty"
    stripped = [v.strip() for v in values]
    if all(re.fullmatch(r"[+-]?\d+", v) for v in stripped):
        return "integer"
    if all(re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)", v) for v in stripped):
        return "decimal"
    if all(v.lower() in BOOLEANS for v in stripped):
        return "boolean"
    if all(_is_date(v) for v in stripped):
        return "date"
    return "text"


def stratified_samples(counts: Counter, limit: int = MAX_SAMPLES) -> list[str]:
    """Pick values evenly across distinct values, from most to least frequent."""
    ordered = [v for v, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    if len(ordered) <= limit:
        return ordered
    step = (len(ordered) - 1) / (limit - 1)
    return [ordered[round(i * step)] for i in range(limit)]


def column_stats(column: str, series: pd.Series) -> dict[str, Any]:
    values = series.tolist()
    present = [v for v in values if v.strip() != ""]
    counts = Counter(present)
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_VALUES]
    shapes = Counter(value_shape(v) for v in present)
    top_shapes = sorted(shapes.items(), key=lambda item: (-item[1], item[0]))[:MAX_SHAPES]
    return {
        "column": column,
        "inferred_type": infer_type(present),
        "null_count": len(values) - len(present),
        "distinct_count": len(counts),
        "top_values": [{"value": v, "count": n} for v, n in top],
        "samples": stratified_samples(counts),
        "shapes": [{"shape": s, "count": n} for s, n in top_shapes],
    }


def compute_stats(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [column_stats(col, df[col]) for col in df.columns]


def profile_source(source: Path, run_dir: Path, client: Any = None) -> dict:
    """Compute statistics, ask the model what they mean, and save both in the run."""
    df = read_source(source)
    stats = compute_stats(df)
    write_json(run_dir / "stats.json", {"row_count": len(df), "columns": stats})
    columns = list(df.columns)
    profile = call_json(
        "profile",
        {
            "source_name": Path(source).name,
            "row_count": len(df),
            "column_count": len(columns),
            "column_stats_json": json.dumps(stats, indent=1),
            "output_schema_json": json.dumps(load_schema("profile")),
        },
        "profile",
        model_for("PORTER_PROFILE_MODEL", "muse-spark-1.3"),
        run_dir=run_dir,
        check=lambda doc: check_profile(doc, columns),
        client=client,
    )
    write_json(run_dir / "profile.json", profile)
    return profile
