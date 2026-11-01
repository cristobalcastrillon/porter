"""Benchmark: run seeded variants of the demo file and score the result against ground truth.

Usage: python -m porter.evaluate --seeds 3

Questions take the model's recommended option, so a wrong recommendation lowers the cell
score. Fake-mode runs only prove the plumbing, so they never write results.md.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from porter.llm import LLMError, is_fake, run_cost
from porter.make_dirty import GROUND_TRUTH, TARGET_COLUMNS, build, download_census, load_census, write_outputs
from porter.pipeline import run_to_dry_run
from porter.runs import ROOT, read_json, runs_root


FIRST_SEED = 7
RESULTS_FILE = ROOT / "results.md"
# Half of the last kept digit for diameters (one decimal), and floating-point noise for coordinates.
TOLERANCE = {"diameter_cm": 0.05 + 1e-9, "latitude": 1e-6, "longitude": 1e-6}


@dataclass
class SeedResult:
    seed: int
    error: str | None = None
    fields_strict: int = 0
    fields_lenient: int = 0
    wrong_fields: list[str] = field(default_factory=list)
    cells_correct: int = 0
    cells_total: int = 0
    wrong_cells_by_field: dict[str, int] = field(default_factory=dict)
    rejected_rows: int = 0
    cost_usd: float = 0.0


def score_fields(mapping: dict, truth: dict) -> tuple[int, int, list[str]]:
    """Same source columns and same kind. Lenient also accepts kinds that are equally correct."""
    strict = lenient = 0
    wrong: list[str] = []
    proposed = {m["target_field"]: m for m in mapping["field_mappings"]}
    for name, answer in truth.items():
        entry = proposed.get(name)
        same_columns = entry is not None and entry["source_columns"] == answer["source_columns"]
        kind = entry["transform"]["kind"] if entry else None
        strict_ok = same_columns and kind == answer["kind"]
        lenient_ok = strict_ok or (same_columns and kind in answer.get("also_accept", []))
        strict += strict_ok
        lenient += lenient_ok
        if not strict_ok:
            wrong.append(name)
    return strict, lenient, wrong


def cell_matches(column: str, got: Any, want: Any) -> bool:
    if want == "" or want is None:
        return got is None or got == ""
    if got is None:
        return False
    if column in TOLERANCE:
        return isinstance(got, (int, float)) and abs(float(got) - float(want)) <= TOLERANCE[column]
    return str(got) == str(want)


def score_cells(valid_rows: list[dict], expected: pd.DataFrame) -> tuple[int, int, dict[str, int]]:
    """Compare dry-run output with the expected rows. A rejected or missing row counts as all wrong."""
    by_id = {r["external_id"]: r for r in valid_rows}
    correct = 0
    wrong: dict[str, int] = {}
    for record in expected.to_dict("records"):
        row = by_id.get(str(record["external_id"]), {})
        for column in TARGET_COLUMNS:
            if row and cell_matches(column, row.get(column), record[column]):
                correct += 1
            else:
                wrong[column] = wrong.get(column, 0) + 1
    return correct, len(expected) * len(TARGET_COLUMNS), wrong


def evaluate_seed(census: pd.DataFrame, seed: int, rows: int, stamp: str, client: Any = None) -> SeedResult:
    result = SeedResult(seed)
    data_dir = runs_root() / f"eval-{stamp}-data" / f"seed-{seed}"
    write_outputs(*build(census, rows, seed), data_dir)
    expected = pd.read_csv(data_dir / "expected_assets.csv", dtype=str, keep_default_na=False)
    run_id = f"eval-{stamp}-seed-{seed}"
    try:
        run_dir, proposed, report = run_to_dry_run(data_dir / "legacy_tree_inventory.csv", run_id, client=client)
    except (LLMError, ValueError) as exc:
        result.error = str(exc)
        result.cost_usd = run_cost(runs_root() / run_id)
        return result
    result.fields_strict, result.fields_lenient, result.wrong_fields = score_fields(proposed, GROUND_TRUTH)
    valid = [json.loads(line) for line in (run_dir / "valid.jsonl").read_text().splitlines()]
    result.cells_correct, result.cells_total, result.wrong_cells_by_field = score_cells(valid, expected)
    result.rejected_rows = report["rejected_rows"]
    result.cost_usd = run_cost(run_dir)
    return result


def render(results: list[SeedResult], fake: bool) -> str:
    done = [r for r in results if r.error is None]
    fields = len(GROUND_TRUTH)
    lines = ["# Porter benchmark", ""]
    if fake:
        lines += ["**FAKE MODE: replies came from `examples/`. These numbers do not measure the model.**", ""]
    if done:
        count = len(done)
        strict = sum(r.fields_strict for r in done) / count
        lenient = sum(r.fields_lenient for r in done) / count
        cells = sum(r.cells_correct for r in done) / max(sum(r.cells_total for r in done), 1) * 100
        rejected = sum(r.rejected_rows for r in done) / count
        cost = sum(r.cost_usd for r in done) / count
        lines += [
            "| Metric | Result |",
            "| --- | --- |",
            f"| Target fields mapped correctly | {strict:.1f} / {fields} |",
            f"| Cell values matching ground truth after transform | {cells:.2f} % |",
            f"| Rows rejected by the dry run, each with a reason | {rejected:.1f} |",
            f"| Model cost per migration | {cost:.2f} USD |",
            "",
            f"Results are averaged over {count} seeded variants of the file.",
            "",
        ]
    lines += [
        "## Per seed",
        "",
        "| Seed | Fields (strict) | Fields (copy = cast) | Cells | Rejected rows | Cost (USD) | Fields differing from ground truth |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        if r.error:
            lines.append(f"| {r.seed} | failed | | | | {r.cost_usd:.2f} | {r.error[:160]} |")
        else:
            share = r.cells_correct / max(r.cells_total, 1) * 100
            lines.append(
                f"| {r.seed} | {r.fields_strict}/{fields} | {r.fields_lenient}/{fields} | {share:.2f}% | "
                f"{r.rejected_rows} | {r.cost_usd:.2f} | {', '.join(r.wrong_fields) or '–'} |"
            )
    wrong_cells: dict[str, int] = {}
    for r in done:
        for column, count_wrong in r.wrong_cells_by_field.items():
            wrong_cells[column] = wrong_cells.get(column, 0) + count_wrong
    if wrong_cells:
        lines += ["", "Wrong cells by field: " + ", ".join(f"{k} {v:,}" for k, v in sorted(wrong_cells.items())) + "."]
    return "\n".join(lines) + "\n"


def results_path(fake: bool) -> Path:
    return runs_root() / "results.fake.md" if fake else RESULTS_FILE


def main() -> None:
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--rows", type=int, default=5000)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")

    census = load_census(download_census())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    results = []
    for seed in range(FIRST_SEED, FIRST_SEED + args.seeds):
        print(f"seed {seed}…", flush=True)
        results.append(evaluate_seed(census, seed, args.rows, stamp))
    text = render(results, is_fake())
    runs_root().mkdir(parents=True, exist_ok=True)
    path = results_path(is_fake())
    path.write_text(text)
    print(text)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
