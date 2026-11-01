import json
import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from porter.make_dirty import build
from porter.pipeline import run_to_dry_run
from porter.write import (
    BATCH_SIZE, MAX_ATTEMPTS, WriteAborted, load_checkpoint, parse_faults, retry_after_seconds, write_run,
)


class FakeSupabase:
    """An in-memory PostgREST: upserts on the key and rejects repeats inside one batch."""

    def __init__(self):
        self.table: dict[str, dict] = {}
        self.posts: list[list[dict]] = []
        self.requests: list[httpx.Request] = []
        self.script: list = []  # per-POST behaviour, consumed in order
        self.crash_after_post: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/assets"):
            rows = [{"external_id": k} for k in sorted(self.table)]
            limit = min(int(request.url.params["limit"]), 1000)
            offset = int(request.url.params["offset"])
            return httpx.Response(200, json=rows[offset:offset + limit])
        if request.method == "GET":
            return httpx.Response(404)

        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            if step is not None:
                return step
        batch = json.loads(request.content)
        ids = [r["external_id"] for r in batch]
        if len(ids) != len(set(ids)):
            return httpx.Response(500, text="ON CONFLICT cannot affect row a second time")
        self.posts.append(batch)
        self.table.update({r["external_id"]: r for r in batch})
        if self.crash_after_post == len(self.posts):
            self.crash_after_post = None
            raise KeyboardInterrupt  # dies after the server committed, before the checkpoint
        return httpx.Response(201)


@pytest.fixture
def setup(census, tmp_path, monkeypatch):
    monkeypatch.setenv("PORTER_RUNS_DIR", str(tmp_path / "runs"))
    legacy, expected = build(census, 1000, 7)
    path = tmp_path / "legacy.csv"
    legacy.to_csv(path, index=False)
    run_dir, _, _ = run_to_dry_run(path, "w")
    server = FakeSupabase()
    client = httpx.Client(
        base_url="https://x.test/rest/v1", headers={"apikey": "sb_secret_x"},
        transport=httpx.MockTransport(server),
    )
    return run_dir, server, client, expected


def go(run_dir, client, **kwargs):
    sleeps: list[float] = []
    result = write_run(run_dir, client, sleep=sleeps.append, rng=random.Random(1), echo=False, **kwargs)
    return result, sleeps


def checkpoint(run_dir):
    return json.loads((run_dir / "checkpoint.json").read_text())


def test_clean_run_writes_each_row_once_in_batches(setup):
    run_dir, server, client, expected = setup
    result, _ = go(run_dir, client)
    assert set(server.table) == set(expected["external_id"])
    assert all(len(b) <= BATCH_SIZE for b in server.posts)
    assert sum(len(b) for b in server.posts) == len(expected)
    assert result["ok"] and result["missing_ids"] == 0
    assert checkpoint(run_dir)["rows_committed"] == len(expected)


def test_request_shape(setup):
    run_dir, server, client, _ = setup
    go(run_dir, client)
    post = next(r for r in server.requests if r.method == "POST")
    assert post.url.path == "/rest/v1/assets"
    assert post.url.params["on_conflict"] == "external_id"
    assert post.headers["prefer"] == "resolution=merge-duplicates,return=minimal"
    assert post.headers["apikey"] == "sb_secret_x"
    assert "authorization" not in post.headers


def test_retries_429_5xx_and_timeouts(setup):
    run_dir, server, client, expected = setup
    server.script = [
        httpx.Response(429), httpx.Response(503), httpx.ReadTimeout("slow"),
        httpx.ConnectError("down"),
    ]
    result, sleeps = go(run_dir, client)
    assert result["retries"] == 4 and len(sleeps) == 4
    assert set(server.table) == set(expected["external_id"])


def test_backoff_grows_and_has_jitter(setup):
    run_dir, server, client, _ = setup
    server.script = [httpx.Response(503) for _ in range(5)]
    _, sleeps = go(run_dir, client)
    for attempt, delay in enumerate(sleeps, start=1):
        ceiling = 0.5 * 2 ** (attempt - 1)
        assert ceiling * 0.5 <= delay <= ceiling
    assert len(set(sleeps)) == 5


def test_retry_after_is_honored(setup):
    run_dir, server, client, _ = setup
    server.script = [httpx.Response(429, headers={"Retry-After": "7"})]
    _, sleeps = go(run_dir, client)
    assert sleeps[0] == 7.0


def test_retry_after_formats():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    later = (now + timedelta(seconds=9)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert retry_after_seconds(later, now) == 9
    assert retry_after_seconds("3") == 3
    assert retry_after_seconds(None) is None
    assert retry_after_seconds("soon") is None


def test_gives_up_after_max_attempts_and_keeps_checkpoint_then_resumes(setup):
    run_dir, server, client, expected = setup
    # First batch succeeds, the second fails on every attempt.
    server.script = [None] + [httpx.Response(500) for _ in range(MAX_ATTEMPTS)]
    with pytest.raises(WriteAborted, match="gave up"):
        go(run_dir, client)
    assert checkpoint(run_dir)["batches_committed"] == 1
    go(run_dir, client)
    assert set(server.table) == set(expected["external_id"])


def test_client_errors_stop_at_once(setup):
    run_dir, server, client, _ = setup
    server.script = [httpx.Response(401, text="bad key")]
    with pytest.raises(WriteAborted, match="HTTP 401"):
        go(run_dir, client)
    assert sum(r.method == "POST" for r in server.requests) == 1


def test_crash_mid_run_resumes_without_duplicates(setup):
    run_dir, server, client, expected = setup
    server.crash_after_post = 2
    with pytest.raises(KeyboardInterrupt):
        go(run_dir, client)
    # Batch 2 reached the server, but the process died before recording it.
    assert checkpoint(run_dir)["batches_committed"] == 1
    assert len(server.table) == 2 * BATCH_SIZE

    go(run_dir, client)
    assert len(server.table) == len(expected)
    assert set(server.table) == set(expected["external_id"])
    sent = [r["external_id"] for b in server.posts for r in b]
    assert len(sent) == len(expected) + BATCH_SIZE  # only batch 2 went twice; first batch was not re-sent
    assert checkpoint(run_dir)["rows_committed"] == len(expected)


def test_finished_run_sends_nothing_again_but_restart_does(setup):
    run_dir, server, client, expected = setup
    go(run_dir, client)
    before = len(server.posts)
    go(run_dir, client)
    assert len(server.posts) == before
    go(run_dir, client, restart=True)
    assert len(server.posts) == 2 * before
    assert len(server.table) == len(expected)


def test_simulated_faults_never_reach_the_api(setup):
    run_dir, server, client, expected = setup
    result, sleeps = go(run_dir, client, fault_rate=0.4)
    assert result["simulated_429s"] > 0 and len(sleeps) == result["retries"]
    assert len(server.posts) == len(range(0, len(expected), BATCH_SIZE))
    assert "429 (simulated)" in (run_dir / "write.log").read_text()


def test_parse_faults():
    assert parse_faults("429:0.15") == 0.15
    assert parse_faults(None) == 0.0
    for bad in ("500:0.1", "429:2"):
        with pytest.raises(ValueError):
            parse_faults(bad)


def test_refuses_without_approval_or_dry_run(setup):
    run_dir, server, client, _ = setup
    (run_dir / "dry_run.json").unlink()
    with pytest.raises(WriteAborted, match="dry_run.json is missing"):
        go(run_dir, client)
    assert not server.requests


def test_refuses_rows_changed_after_dry_run(setup):
    run_dir, server, client, _ = setup
    with (run_dir / "valid.jsonl").open("a") as out:
        out.write('{"external_id": "evil"}\n')
    with pytest.raises(WriteAborted, match="changed after the dry run"):
        go(run_dir, client)
    assert not server.requests


def test_rejected_rows_need_explicit_permission(setup):
    run_dir, server, client, _ = setup
    report = json.loads((run_dir / "dry_run.json").read_text())
    report["rejected_rows"] = 3
    (run_dir / "dry_run.json").write_text(json.dumps(report))
    with pytest.raises(WriteAborted, match="rejected 3 rows"):
        go(run_dir, client)
    assert go(run_dir, client, allow_rejected=True)[0]["ok"]


def test_checkpoint_for_other_rows_is_refused(setup):
    run_dir, _, _, _ = setup
    (run_dir / "checkpoint.json").write_text(json.dumps({"valid_sha256": "other", "batch_size": BATCH_SIZE, "batches_committed": 3}))
    with pytest.raises(WriteAborted, match="different rows"):
        load_checkpoint(run_dir, "mine", restart=False)
    assert load_checkpoint(run_dir, "mine", restart=True) == 0


def test_verify_reports_rows_missing_after_table_was_emptied(setup):
    run_dir, server, client, _ = setup
    go(run_dir, client)
    server.table.clear()
    with pytest.raises(WriteAborted, match="--restart"):
        go(run_dir, client)


def test_verify_tolerates_rows_from_other_runs(setup):
    run_dir, server, client, _ = setup
    server.table["old"] = {"external_id": "old"}
    result, _ = go(run_dir, client)
    assert result["ok"] and result["extra_ids"] == 1


def test_schema_mismatch_stops_before_writing(setup):
    run_dir, server, client, _ = setup

    def handler(request):
        return httpx.Response(200, json={"definitions": {"assets": {"properties": {}, "required": []}}})

    live = httpx.Client(base_url="https://x.test/rest/v1", transport=httpx.MockTransport(handler))
    with pytest.raises(WriteAborted, match="does not match"):
        go(run_dir, live)
