"""Step 5: batched, retried, resumable upserts.

Usage: python -m porter.write --run <run_id>

A checkpoint is saved atomically after each committed batch, so a killed run resumes
from it. A batch cut off mid-send is sent again, which is safe because writes are upserts.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

import httpx
from dotenv import load_dotenv

from porter.runs import ROOT, read_json, run_path, write_json
from porter.target import check_against_openapi, load_target
from porter.validate import file_hash

BATCH_SIZE = 200
MAX_ATTEMPTS = 6
BASE_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 30.0
TIMEOUT_SECONDS = 30.0
RETRY_STATUSES = {429, 500, 502, 503, 504}
VERIFY_PAGE_SIZE = 1000


class WriteAborted(RuntimeError):
    """The run stopped. The checkpoint still marks the last committed batch."""


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The secret key goes in the apikey header only; it is not a JWT."""
    load_dotenv(ROOT / ".env")
    return httpx.Client(
        base_url=os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1",
        headers={"apikey": os.environ["SUPABASE_SECRET_KEY"]},
        timeout=TIMEOUT_SECONDS,
        transport=transport,
    )


def parse_faults(spec: str | None) -> float:
    """PORTER_FAULTS=429:0.15 gives 0.15. Only 429 is supported."""
    if not spec:
        return 0.0
    status, _, probability = spec.partition(":")
    if status != "429":
        raise ValueError(f"PORTER_FAULTS supports only 429, got '{spec}'")
    value = float(probability)
    if not 0 <= value <= 1:
        raise ValueError(f"PORTER_FAULTS probability must be between 0 and 1, got '{spec}'")
    return value


def retry_after_seconds(header: str | None, now: datetime | None = None) -> float | None:
    """Retry-After is either a number of seconds or an HTTP date."""
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    return max(0.0, (moment - (now or datetime.now(timezone.utc))).total_seconds())


class Run:
    """One write run: its log, retry policy and clock."""

    def __init__(
        self,
        run_dir: Path,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        fault_rate: float = 0.0,
        echo: bool = True,
    ):
        self.run_dir = run_dir
        self.client = client
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.fault_rate = fault_rate
        self.echo = echo
        self.retries = 0
        self.simulated = 0

    def log(self, message: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {message}"
        if self.echo:
            print(line, flush=True)
        with (self.run_dir / "write.log").open("a") as log:
            log.write(line + "\n")

    def backoff(self, attempt: int, retry_after: float | None) -> float:
        """Exponential backoff with jitter, and never shorter than the server asked for."""
        ceiling = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (attempt - 1))
        delay = ceiling * self.rng.uniform(0.5, 1.0)
        return max(delay, retry_after or 0.0)

    def request(self, method: str, url: str, label: str, inject_faults: bool = False, **kwargs) -> httpx.Response:
        """Send with retries on 429, 5xx and timeouts. Other errors stop the run at once."""
        problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry_after = None
            if inject_faults and self.rng.random() < self.fault_rate:
                self.simulated += 1
                problem = "429 (simulated)"  # nothing was sent to the API
            else:
                try:
                    response = self.client.request(method, url, **kwargs)
                except httpx.TransportError as exc:
                    problem = type(exc).__name__
                else:
                    if response.status_code < 400:
                        return response
                    if response.status_code not in RETRY_STATUSES:
                        raise WriteAborted(f"{label}: HTTP {response.status_code}: {response.text[:300]}")
                    problem = f"HTTP {response.status_code}"
                    retry_after = retry_after_seconds(response.headers.get("Retry-After"))
            if attempt == MAX_ATTEMPTS:
                break
            self.retries += 1
            delay = self.backoff(attempt, retry_after)
            self.log(f"{label}: {problem}, attempt {attempt}/{MAX_ATTEMPTS}, retrying in {delay:.2f}s")
            self.sleep(delay)
        raise WriteAborted(f"{label}: gave up after {MAX_ATTEMPTS} attempts, last error {problem}")


def preflight(run_dir: Path, allow_rejected: bool) -> dict:
    """Refuse to write unless approval and a matching dry run exist. Returns the dry-run report."""
    for name, step in [
        ("mapping.approved.json", "approve the mapping"),
        ("transform.approved.json", "approve the code"),
        ("dry_run.json", "run the dry run"),
        ("valid.jsonl", "run the dry run"),
    ]:
        if not (run_dir / name).exists():
            raise WriteAborted(f"{name} is missing: {step} first")
    report = read_json(run_dir / "dry_run.json")
    for name, key in [
        ("mapping.approved.json", "mapping_sha256"),
        ("transform.approved.json", "transform_sha256"),
        ("valid.jsonl", "valid_sha256"),
    ]:
        if file_hash(run_dir / name) != report[key]:
            raise WriteAborted(f"{name} changed after the dry run: run the dry run again")
    if report["rejected_rows"] and not allow_rejected:
        raise WriteAborted(
            f"the dry run rejected {report['rejected_rows']} rows (see rejected.csv). "
            "Fix them, or pass --allow-rejected to write only the valid rows."
        )
    return report


def load_checkpoint(run_dir: Path, valid_sha256: str, restart: bool) -> int:
    """How many batches are already committed. A checkpoint for other rows is refused."""
    path = run_dir / "checkpoint.json"
    if restart or not path.exists():
        return 0
    checkpoint = read_json(path)
    if checkpoint["valid_sha256"] != valid_sha256 or checkpoint["batch_size"] != BATCH_SIZE:
        raise WriteAborted("checkpoint.json belongs to different rows: pass --restart to write from the start")
    return checkpoint["batches_committed"]


def save_checkpoint(run_dir: Path, valid_sha256: str, batches: int, total_batches: int, rows: int, total_rows: int) -> None:
    write_json(run_dir / "checkpoint.json", {
        "valid_sha256": valid_sha256,
        "batch_size": BATCH_SIZE,
        "batches_committed": batches,
        "total_batches": total_batches,
        "rows_committed": rows,
        "total_rows": total_rows,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def check_live_schema(run: Run, target: dict) -> None:
    """Compare the target file with the live spec. An unreadable spec is a warning, not a stop."""
    try:
        response = run.client.get("/")
    except httpx.TransportError as exc:
        run.log(f"warning: could not read the OpenAPI spec ({type(exc).__name__}); skipping the schema check")
        return
    if response.status_code != 200:
        run.log(f"warning: OpenAPI spec returned HTTP {response.status_code}; skipping the schema check")
        return
    errors = check_against_openapi(target, response.json())
    if errors:
        raise WriteAborted(f"the target description does not match the live table: {errors}")
    run.log("schema check: target description matches the live table")


def verify(run: Run, table: str, key: str, expected_ids: set[str]) -> dict:
    """Read back every key. Row count vs distinct is guaranteed by the primary key, so also
    compare with this run's IDs. Rows from other runs show up as 'extra' and aren't an error."""
    ids: list[str] = []
    while True:
        response = run.request(
            "GET", f"/{table}", "verify",
            params={"select": key, "order": key, "limit": VERIFY_PAGE_SIZE, "offset": len(ids)},
        )
        page = response.json()
        if not page:
            break
        ids.extend(row[key] for row in page)
    found = set(ids)
    result = {
        "table_rows": len(ids),
        "distinct_ids": len(found),
        "expected_ids": len(expected_ids),
        "missing_ids": len(expected_ids - found),
        "extra_ids": len(found - expected_ids),
    }
    result["ok"] = result["table_rows"] == result["distinct_ids"] and result["missing_ids"] == 0
    return result


def write_run(
    run_dir: Path,
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    fault_rate: float = 0.0,
    allow_rejected: bool = False,
    restart: bool = False,
    pace: float = 0.0,
    echo: bool = True,
) -> dict:
    """Write valid.jsonl to the target, resuming from the checkpoint, then verify."""
    report = preflight(run_dir, allow_rejected)
    target = load_target()
    table, key = target["name"], target["primary_key"]
    run = Run(run_dir, client, sleep, rng, fault_rate, echo)

    rows = [json.loads(line) for line in (run_dir / "valid.jsonl").read_text().splitlines()]
    batches = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    committed = load_checkpoint(run_dir, report["valid_sha256"], restart)
    sent_rows = sum(len(b) for b in batches[:committed])

    run.log(f"run {run_dir.name}: {len(rows):,} rows in {len(batches)} batches; "
            f"{report['duplicates_dropped']} duplicates dropped, {report['rejected_rows']} rejected rows left out")
    if committed:
        run.log(f"resuming after batch {committed}/{len(batches)} ({sent_rows:,} rows already committed)")
    if fault_rate:
        run.log(f"fault injection on: {fault_rate:.0%} simulated 429s, which are never sent to the API")
    check_live_schema(run, target)

    save_checkpoint(run_dir, report["valid_sha256"], committed, len(batches), sent_rows, len(rows))
    for number, batch in enumerate(batches[committed:], start=committed + 1):
        run.request(
            "POST", f"/{table}", f"batch {number}/{len(batches)}", inject_faults=True,
            params={"on_conflict": key},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=batch,
        )
        sent_rows += len(batch)
        save_checkpoint(run_dir, report["valid_sha256"], number, len(batches), sent_rows, len(rows))
        run.log(f"batch {number}/{len(batches)} committed ({sent_rows:,}/{len(rows):,} rows)")
        if pace and number < len(batches):
            sleep(pace)

    check = verify(run, table, key, {r[key] for r in rows})
    run.log(f"check: {check['table_rows']:,} rows, {check['distinct_ids']:,} distinct {key}; "
            f"{check['missing_ids']} of this run's IDs missing, {check['extra_ids']} from other runs")
    result = {**check, "retries": run.retries, "simulated_429s": run.simulated}
    write_json(run_dir / "write_result.json", result)
    run.log(f"done: {run.retries} retries, {run.simulated} simulated 429s")
    if not check["ok"]:
        raise WriteAborted(f"check failed: {check}. If the table was emptied after this run, use --restart.")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="run id, a folder under runs/")
    parser.add_argument("--allow-rejected", action="store_true", help="write valid rows even if the dry run rejected some")
    parser.add_argument("--restart", action="store_true", help="ignore the checkpoint and write from the first batch")
    parser.add_argument("--pace", type=float, default=0.0, help="seconds to wait between batches, for demos")
    args = parser.parse_args()

    # A plain `kill` behaves like Ctrl+C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    load_dotenv(ROOT / ".env")
    run_dir = run_path(args.run)
    if not (run_dir / "run.json").exists():
        print(f"no such run: {run_dir}", file=sys.stderr)
        return 2
    try:
        with make_client() as client:
            write_run(
                run_dir, client, fault_rate=parse_faults(os.environ.get("PORTER_FAULTS")),
                allow_rejected=args.allow_rejected, restart=args.restart, pace=args.pace,
            )
    except WriteAborted as exc:
        print(f"stopped: {exc}\nRe-run the same command to resume.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        checkpoint = read_json(run_dir / "checkpoint.json") if (run_dir / "checkpoint.json").exists() else {}
        message = f"interrupted; checkpoint at batch {checkpoint.get('batches_committed', 0)}. Re-run the same command to resume."
        print(message, file=sys.stderr)
        with (run_dir / "write.log").open("a") as log:
            log.write(f"{datetime.now().strftime('%H:%M:%S')} {message}\n")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
