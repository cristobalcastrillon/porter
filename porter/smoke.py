"""One cheap real call to Muse Spark, to confirm the key and the JSON-schema mode work.

Usage: python -m porter.smoke
"""
from __future__ import annotations

import json

from porter.llm import cost_usd, make_client

MODEL = "muse-spark-1.3"
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok", "message"],
    "properties": {"ok": {"type": "boolean"}, "message": {"type": "string"}},
}


def main() -> None:
    response = make_client().chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[{"role": "user", "content": "Reply with ok=true and a greeting of at most five words."}],
        response_format={"type": "json_schema", "json_schema": {"name": "smoke", "schema": SCHEMA}},
    )
    usage = response.usage
    print("reply:", json.loads(response.choices[0].message.content))
    print(f"usage: {usage.prompt_tokens} input, {usage.completion_tokens} output")
    print(f"cost:  ${cost_usd(MODEL, usage.prompt_tokens, usage.completion_tokens):.6f}")


if __name__ == "__main__":
    main()
