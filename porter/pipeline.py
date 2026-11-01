"""Run the steps up to the dry run in one call. Used by the evaluation and the tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from porter.mapping import approve, propose_mapping
from porter.profile import profile_source
from porter.runs import new_run, read_source
from porter.target import load_target
from porter.transform import approve_code, generate_code
from porter.validate import dry_run


def run_to_dry_run(source: Path, run_id: str | None = None, answers: dict[str, str] | None = None,
                   client: Any = None) -> tuple[Path, dict, dict]:
    """Profile, map, approve and dry-run a file. Unanswered questions take the recommended option.

    Returns (run directory, proposed mapping, dry-run report). Auto-approving is only
    for benchmarks; the UI is where a person reviews a real migration.
    """
    run_dir = new_run(source, run_id)
    target = load_target()
    df = read_source(source)
    profile = profile_source(source, run_dir, client=client)
    proposed, _ = propose_mapping(profile, target, Path(source).name, len(df), run_dir, client=client)
    chosen = {q["id"]: q["recommended"] for q in proposed["questions"]} | (answers or {})
    approved = approve(proposed, chosen, profile, target, run_dir)
    code = generate_code(approved["mapping"], df, target, run_dir, client=client)
    approve_code(code, run_dir)
    return run_dir, proposed, dry_run(run_dir, target)
