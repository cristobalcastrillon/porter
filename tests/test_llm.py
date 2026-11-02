import json
from types import SimpleNamespace

import pytest

from porter.llm import (
    API_MAX_DEPTH, LLMError, api_schema, call_json, cost_usd, load_prompt, load_schema, run_cost, schema_depth,
)

VALUES = {
    "source_name": "x.csv", "row_count": 1, "column_count": 1,
    "column_stats_json": "{{not_a_placeholder}}", "output_schema_json": "{}",
}


class ScriptedClient:
    """Stands in for the OpenAI client and replays canned replies."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs)))
        message = SimpleNamespace(content=self.replies.pop(0))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100),
        )


def test_prompt_is_split_and_filled():
    system, user = load_prompt("profile", VALUES)
    assert system.startswith("You are the profiling step")
    assert not system.endswith("---")
    assert "## User" not in user and "x.csv" in user


def test_placeholder_inside_a_value_is_not_expanded():
    _, user = load_prompt("profile", VALUES)
    assert "{{not_a_placeholder}}" in user


def test_api_schema_has_no_allof():
    assert "allOf" in json.dumps(load_schema("mapping"))
    assert "allOf" not in json.dumps(api_schema(load_schema("mapping")))


@pytest.mark.parametrize("name", ["profile", "mapping", "transform"])
def test_api_schema_is_inlined_and_within_depth_limit(name):
    sent = api_schema(load_schema(name))
    assert "$ref" not in json.dumps(sent) and "$defs" not in sent
    assert schema_depth(sent) <= API_MAX_DEPTH


def test_api_schema_keeps_top_level_field_mapping_structure():
    sent = api_schema(load_schema("mapping"))
    params = sent["properties"]["field_mappings"]["items"]["properties"]["transform"]["properties"]["params"]
    assert params["properties"]["value_map"]["items"]["properties"]["from"]["type"] == "array"


def test_fake_mode_returns_example(tmp_path):
    doc = call_json("profile", {}, "profile", "muse-spark-1.3", run_dir=tmp_path)
    assert doc["candidate_keys"] == ["TreeID"]
    call = json.loads((tmp_path / "calls.jsonl").read_text())
    assert call["fake"] is True and call["cost_usd"] == 0.0


def test_retry_sends_errors_back_and_logs_cost(tmp_path):
    column = json.load(open("examples/profile.example.json"))["columns"][0]
    bad = json.dumps({"columns": [{"column": "x"}], "candidate_keys": [], "notes": []})
    good = json.dumps({"columns": [column], "candidate_keys": [], "notes": []})
    client = ScriptedClient(bad, good)

    doc = call_json("profile", VALUES, "profile", "muse-spark-1.3", run_dir=tmp_path, client=client)

    assert doc["columns"][0]["column"] == "TreeID"
    assert len(client.requests) == 2
    assert client.requests[0]["temperature"] == 0
    assert "strict" not in client.requests[0]["response_format"]["json_schema"]
    assert "failed validation" in client.requests[1]["messages"][-1]["content"]
    calls = [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [c["ok"] for c in calls] == [False, True]
    assert calls[0]["cost_usd"] == pytest.approx(cost_usd("muse-spark-1.3", 1000, 100))
    assert run_cost(tmp_path) == pytest.approx(2 * calls[0]["cost_usd"])


def test_semantic_check_failure_retries_once_then_raises():
    client = ScriptedClient("not json", "still not json", "never used")
    with pytest.raises(LLMError):
        call_json("profile", VALUES, "profile", "muse-spark-1.3", client=client)
    assert len(client.requests) == 2


def test_check_callback_rejects_valid_schema():
    column = json.load(open("examples/profile.example.json"))["columns"][0]
    reply = json.dumps({"columns": [column], "candidate_keys": [], "notes": []})
    client = ScriptedClient(reply, reply)
    with pytest.raises(LLMError, match="nope"):
        call_json("profile", VALUES, "profile", "muse-spark-1.3", client=client, check=lambda d: ["nope"])
