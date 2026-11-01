"""All state of a migration lives in runs/<run_id>/, shared by the CLI and the UI."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def runs_root() -> Path:
    return Path(os.environ.get("PORTER_RUNS_DIR", ROOT / "runs"))


def run_path(run_id: str) -> Path:
    return runs_root() / run_id


def new_run(source: Path, run_id: str | None = None) -> Path:
    """Create a run directory for one source file."""
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = run_path(run_id)
    path.mkdir(parents=True, exist_ok=True)
    write_json(path / "run.json", {"run_id": run_id, "source": str(Path(source).resolve())})
    return path


def list_runs() -> list[str]:
    """Runs to offer in the UI, newest first. Benchmark runs are left out."""
    root = runs_root()
    if not root.exists():
        return []
    runs = (p.name for p in root.iterdir() if (p / "run.json").exists() and not p.name.startswith("eval-"))
    return sorted(runs, reverse=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, doc: dict) -> None:
    """Write atomically, so a killed process never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, path)


def read_source(path: Path) -> pd.DataFrame:
    """Read every cell as text. Blanks stay '' and the word 'None' stays a word."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)
