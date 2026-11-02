"""Model client: prompt files in, validated JSON out, every call logged with its cost.

Set PORTER_FAKE_LLM=1 to get the matching examples/*.json without any network,
so the tests and the UI run with no API key.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import jsonschema
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

# USD per 1M tokens: (input, output)
PRICES = {
    "muse-spark-1.3": (1.25, 4.25),
    "muse-wonder-1.0": (4.00, 20.00),
}
FAKE_REPLIES = {
    "profile": ROOT / "examples" / "profile.example.json",
    "mapping": ROOT / "examples" / "mapping.example.json",
    "transform": ROOT / "examples" / "transform.example.json",
}
REQUEST_TIMEOUT_SECONDS = 600
API_MAX_DEPTH = 10

Check = Callable[[dict], list[str]]


class LLMError(RuntimeError):
    """The model's reply still failed validation after the retry."""

    def __init__(self, step: str, errors: list[str]):
        super().__init__(f"{step}: model output invalid after retry: {errors}")
        self.errors = errors


def is_fake() -> bool:
    return os.environ.get("PORTER_FAKE_LLM") == "1"


def load_prompt(name: str, values: dict[str, Any]) -> tuple[str, str]:
    """Return (system, user) from prompts/<name>.md with its placeholders filled."""
    text = (ROOT / "prompts" / f"{name}.md").read_text()
    system = text.split("\n## System\n", 1)[1].split("\n## User\n", 1)[0]
    user = text.split("\n## User\n", 1)[1]
    system = system.strip().removesuffix("---").strip()

    def fill(match: re.Match) -> str:
        return str(values[match.group(1)])

    # One regex pass, so a placeholder-like string inside a cell value is not expanded.
    return system, re.sub(r"\{\{(\w+)\}\}", fill, user).strip()


def load_schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())


def api_schema(schema: dict) -> dict:
    """The schema as sent to the API.

    The API rejects allOf and schemas nested more than 10 levels deep, so refs are
    inlined, allOf is dropped, and any object or array at the depth limit keeps only
    its type. Replies are still validated locally against the full schema.
    """
    defs = schema.get("$defs", {})

    def walk(node: Any, depth: int) -> Any:
        if isinstance(node, list):
            return [walk(v, depth) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].removeprefix("#/$defs/")], depth)
        level = depth + 1 if node.get("type") in ("object", "array") else depth
        if level >= API_MAX_DEPTH and ("properties" in node or "items" in node):
            return {k: node[k] for k in ("type", "description") if k in node}
        out = {}
        for key, value in node.items():
            if key in ("allOf", "$defs", "$schema"):
                continue
            if key == "properties":
                out[key] = {name: walk(sub, level) for name, sub in value.items()}
            elif key == "items":
                out[key] = walk(value, level)
            else:
                out[key] = value
        return out

    return walk(schema, 0)


def schema_depth(node: Any) -> int:
    """Object and array levels in an inlined schema, counting the root as 1."""
    if not isinstance(node, dict):
        return 0
    children = list(node.get("properties", {}).values())
    if "items" in node:
        children.append(node["items"])
    below = max((schema_depth(c) for c in children), default=0)
    return below + (1 if node.get("type") in ("object", "array") else 0)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    if model not in PRICES:
        return None
    price_in, price_out = PRICES[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def make_client() -> Any:
    from openai import OpenAI

    load_dotenv(ROOT / ".env")
    return OpenAI(
        base_url=os.environ["META_BASE_URL"],
        api_key=os.environ["MODEL_API_KEY"],
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def model_for(env_var: str, default: str) -> str:
    load_dotenv(ROOT / ".env")
    return os.environ.get(env_var, default)


def validate_reply(content: str | None, schema: dict, check: Check | None) -> tuple[dict | None, list[str]]:
    """Parse and validate one reply. Returns (document, errors)."""
    try:
        doc = json.loads(content or "")
    except json.JSONDecodeError as exc:
        return None, [f"reply is not valid JSON: {exc}"]
    validator = jsonschema.Draft202012Validator(schema)
    errors = [
        f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
        for e in validator.iter_errors(doc)
    ]
    if errors:
        return doc, errors
    return doc, check(doc) if check else []


def log_call(run_dir: Path | None, record: dict) -> None:
    if run_dir is None:
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "calls.jsonl").open("a") as log:
        log.write(json.dumps(record) + "\n")


def call_json(
    step: str,
    values: dict[str, Any],
    schema_name: str,
    model: str,
    run_dir: Path | None = None,
    check: Check | None = None,
    client: Any = None,
) -> dict:
    """Call the model for one step, validate the reply, and retry once with the errors."""
    schema = load_schema(schema_name)
    if is_fake() and client is None:
        content = FAKE_REPLIES[step].read_text()
        doc, errors = validate_reply(content, schema, check)
        log_call(run_dir, {
            "step": step, "model": model, "attempt": 1, "fake": True, "input_tokens": 0,
            "output_tokens": 0, "cost_usd": 0.0, "latency_s": 0.0, "ok": not errors, "errors": errors,
        })
        if errors:
            raise LLMError(step, errors)
        return doc

    client = client or make_client()
    system, user = load_prompt(step, values)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "schema": api_schema(schema)},
    }

    errors: list[str] = []
    for attempt in (1, 2):
        started = time.monotonic()
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=0, response_format=response_format,
        )
        latency = time.monotonic() - started
        content = response.choices[0].message.content
        doc, errors = validate_reply(content, schema, check)
        usage = response.usage
        log_call(run_dir, {
            "step": step, "model": model, "attempt": attempt, "fake": False,
            "input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens,
            "cost_usd": cost_usd(model, usage.prompt_tokens, usage.completion_tokens),
            "latency_s": round(latency, 2), "ok": not errors, "errors": errors,
        })
        if not errors:
            return doc
        messages.append({"role": "assistant", "content": content or ""})
        messages.append({
            "role": "user",
            "content": "Your reply failed validation. Fix these errors and return the full JSON object again:\n"
            + "\n".join(f"- {e}" for e in errors),
        })
    raise LLMError(step, errors)


def run_cost(run_dir: Path) -> float:
    """Total model cost logged for a run."""
    path = run_dir / "calls.jsonl"
    if not path.exists():
        return 0.0
    calls = [json.loads(line) for line in path.read_text().splitlines()]
    return sum(c["cost_usd"] or 0.0 for c in calls)
