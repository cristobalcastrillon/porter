"""Porter's one-page UI. All state lives in runs/<run_id>/, shared with the CLI."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from porter.checks import REVIEW_THRESHOLD, fields_needing_review
from porter.llm import LLMError, is_fake, run_cost
from porter.mapping import approve, propose_mapping
from porter.profile import profile_source
from porter.runs import ROOT, list_runs, new_run, read_json, read_source, run_path
from porter.sandbox import check_code
from porter.target import load_target
from porter.transform import approve_code, generate_code
from porter.validate import dry_run

load_dotenv(ROOT / ".env")

DEMO_SOURCE = ROOT / "data" / "legacy_tree_inventory.csv"
LOG_LINES = 14
ROW_HEIGHT = 35
CODE_HEIGHT = 340
# Everything a step produces, so redoing a step clears what was built on it.
AFTER_PROFILE = ["mapping.proposed.json"]
AFTER_PROPOSAL = ["mapping.approved.json"]
AFTER_APPROVAL = ["transform.proposed.json", "transform.approved.json"]
AFTER_CODE = ["dry_run.json", "rejected.csv", "valid.jsonl", "checkpoint.json", "write_result.json", "write.log"]

st.set_page_config(page_title="Porter", page_icon="📦", layout="wide")
st.markdown(
    """
    <style>
    .block-container { max-width: 1180px; padding-top: 2rem; }
    h3 { margin-top: 1.5rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def clear(run_dir: Path, *groups: list[str]) -> None:
    for group in groups:
        for name in group:
            (run_dir / name).unlink(missing_ok=True)


def load(run_dir: Path, name: str) -> dict | None:
    path = run_dir / name
    return read_json(path) if path.exists() else None


def table_height(rows: int) -> int:
    """Tall enough to show every row without an inner scrollbar."""
    return (rows + 1) * ROW_HEIGHT + 3


def attempt(label: str, action) -> bool:
    """Run a step with a spinner. Model and check failures become a message, not a stack trace."""
    try:
        with st.spinner(label):
            action()
        return True
    except (LLMError, ValueError) as exc:
        st.error(str(exc))
        return False


def pick_run() -> Path | None:
    st.subheader("1 · Choose a file")
    left, right = st.columns(2)
    with left:
        choice = st.radio("Source", ["Demo: legacy tree inventory", "Upload a CSV"], key="source_choice")
        upload = st.file_uploader("CSV file", type="csv", key="upload") if choice == "Upload a CSV" else None
        ready = choice != "Upload a CSV" or upload is not None
        if st.button("Start a new run", type="primary", disabled=not ready, key="start"):
            run_dir = new_run(DEMO_SOURCE)
            if upload is not None:
                saved = run_dir / upload.name
                saved.write_bytes(upload.getvalue())
                run_dir = new_run(saved, run_dir.name)
            st.session_state["run_id"] = run_dir.name
            st.rerun()
    with right:
        runs = list_runs()
        if runs:
            current = st.session_state.get("run_id")
            index = runs.index(current) if current in runs else 0
            st.session_state["run_id"] = st.selectbox("Or continue a run", runs, index=index, key="run_select_" + str(current))
    run_id = st.session_state.get("run_id")
    return run_path(run_id) if run_id else None


def profile_step(run_dir: Path, source: Path) -> dict | None:
    st.subheader("2 · Profile")
    st.caption("Python computes the statistics. Muse Spark reads them and describes each column.")
    if st.button("Profile the file", type="primary", key="profile"):
        clear(run_dir, AFTER_PROFILE, AFTER_PROPOSAL, AFTER_APPROVAL, AFTER_CODE)
        if attempt("Profiling…", lambda: profile_source(source, run_dir)):
            st.rerun()
    profile = load(run_dir, "profile.json")
    stats = load(run_dir, "stats.json")
    if not profile or not stats:
        return None
    by_column = {c["column"]: c for c in stats["columns"]}
    st.dataframe(
        pd.DataFrame([
            {
                "Column": c["column"],
                "Meaning": c["meaning"],
                "Type": c["semantic_type"],
                "Blank": by_column[c["column"]]["null_count"],
                "Distinct": by_column[c["column"]]["distinct_count"],
                "Confidence": c["confidence"],
                "Issues": "; ".join(i["kind"].replace("_", " ") for i in c["issues"]) or "–",
            }
            for c in profile["columns"]
        ]),
        hide_index=True,
        width="stretch",
        height=table_height(len(profile["columns"])),
        column_config={
            "Meaning": st.column_config.TextColumn(width="medium"),
            "Blank": st.column_config.NumberColumn(width="small"),
            "Distinct": st.column_config.NumberColumn(width="small"),
            "Confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f", width="small"),
        },
    )
    st.caption(f"{stats['row_count']:,} rows. Candidate keys: {', '.join(profile['candidate_keys']) or 'none'}.")
    return profile


def map_step(run_dir: Path, source: Path, profile: dict, target: dict) -> dict | None:
    st.subheader("3 · Map and approve")
    st.caption(f"Muse Wonder proposes each field. Below {REVIEW_THRESHOLD} confidence, or with an open question, it needs your review.")
    if st.button("Propose a mapping", type="primary", key="propose"):
        clear(run_dir, AFTER_PROPOSAL, AFTER_APPROVAL, AFTER_CODE)
        rows = len(read_source(source))
        if attempt("Mapping…", lambda: propose_mapping(profile, target, source.name, rows, run_dir)):
            st.rerun()
    mapping = load(run_dir, "mapping.proposed.json")
    if not mapping:
        return None

    review = set(fields_needing_review(mapping))
    st.dataframe(
        pd.DataFrame([
            {
                "Target field": m["target_field"],
                "Source": ", ".join(m["source_columns"]) or "–",
                "Transform": m["transform"]["kind"],
                "Confidence": m["confidence"],
                "Review": "⚠️" if m["target_field"] in review else "✓",
                "Why": m["rationale"],
            }
            for m in mapping["field_mappings"]
        ]),
        hide_index=True,
        width="stretch",
        height=table_height(len(mapping["field_mappings"])),
        column_config={
            "Confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f", width="small"),
            "Review": st.column_config.TextColumn(width="small"),
            "Why": st.column_config.TextColumn(width="large"),
        },
    )
    for m in mapping["field_mappings"]:
        if m["target_field"] in review:
            st.warning(f"**{m['target_field']}** needs review ({m['confidence']:.2f}). {m['rationale']}")
    if mapping["unused_source_columns"]:
        with st.expander(f"Unused columns ({len(mapping['unused_source_columns'])}) and notes"):
            for unused in mapping["unused_source_columns"]:
                st.markdown(f"- **{unused['column']}**: {unused['reason']}")
            for note in mapping["notes"]:
                st.markdown(f"- {note}")

    answers: dict[str, str] = {}
    for question in mapping["questions"]:
        with st.container(border=True):
            st.markdown(f"**{question['target_field']}:** {question['question']}")
            options = {o["id"]: o for o in question["options"]}
            ids = list(options)
            answers[question["id"]] = st.radio(
                "Your answer",
                ids,
                index=ids.index(question["recommended"]),
                format_func=lambda i: options[i]["label"] + (" (recommended)" if i == question["recommended"] else ""),
                captions=[options[i]["effect"] for i in ids],
                key=f"answer_{question['id']}",
            )

    if st.button("Approve mapping", type="primary", key="approve"):
        clear(run_dir, AFTER_APPROVAL, AFTER_CODE)
        if attempt("Saving…", lambda: approve(mapping, answers, profile, target, run_dir)):
            st.rerun()
    approved = load(run_dir, "mapping.approved.json")
    if approved:
        labels = [
            f"{q['target_field']}: {o['label']}"
            for q in mapping["questions"]
            for o in q["options"]
            if approved["answers"].get(q["id"]) == o["id"]
        ]
        st.success("Mapping approved. " + ("; ".join(labels) if labels else "No questions to answer."))
    return approved


def code_step(run_dir: Path, source: Path, approved: dict, target: dict) -> bool:
    st.subheader("4 · Review the generated code")
    st.caption("Muse Wonder writes one small function per expression field, once. Nothing runs until you approve it.")
    if st.button("Generate code", type="primary", key="generate"):
        clear(run_dir, AFTER_APPROVAL, AFTER_CODE)
        action = lambda: generate_code(approved["mapping"], read_source(source), target, run_dir)
        if attempt("Writing code…", action):
            st.rerun()
    proposal = load(run_dir, "transform.proposed.json")
    if not proposal:
        return False
    if not proposal["functions"]:
        st.info("No expression fields: the deterministic engine handles every field.")
    for function in proposal["functions"]:
        st.markdown(f"**{function['target_field']}:** {function['explanation']}")
        st.code(function["code"], language="python", height=CODE_HEIGHT)
        problems = check_code(function["code"])
        if problems:
            st.error("AST check failed: " + "; ".join(problems))
        else:
            st.caption("✓ AST check passed: only allowed imports, no exec, eval, open or dunder access.")
    if st.button("Approve code and run the dry run", type="primary", key="approve_code"):
        clear(run_dir, AFTER_CODE)
        def action() -> None:
            approve_code(proposal, run_dir)
            dry_run(run_dir, target)
        if attempt("Running every row…", action):
            st.rerun()
    return (run_dir / "transform.approved.json").exists()


def dry_run_step(run_dir: Path) -> dict | None:
    st.subheader("5 · Dry run")
    report = load(run_dir, "dry_run.json")
    if not report:
        return None
    st.caption("Every row was transformed and validated. Nothing has been written.")
    total, to_write, rejected, duplicates = st.columns(4)
    total.metric("Rows read", f"{report['total_rows']:,}")
    to_write.metric("Rows to write", f"{report['rows_to_write']:,}")
    rejected.metric("Rows rejected", f"{report['rejected_rows']:,}")
    duplicates.metric("Duplicates dropped", f"{report['duplicates_dropped']:,}")
    if report["conflicting_duplicates"]:
        st.warning(f"{report['conflicting_duplicates']} duplicates differ from the first occurrence; the first was kept.")
    if report["rejected_rows"]:
        rows = pd.read_csv(run_dir / "rejected.csv", dtype=str, keep_default_na=False)
        st.dataframe(rows, hide_index=True, width="stretch", height=300)
    else:
        st.success("No rows were rejected.")
    return report


def write_step(run_dir: Path, report: dict) -> None:
    st.subheader("6 · Write")
    st.caption("Run this in a terminal. Kill it whenever you like; the same command resumes from the checkpoint.")
    flag = " --allow-rejected" if report["rejected_rows"] else ""
    st.code(f"python -m porter.write --run {run_dir.name}{flag}", language="bash")
    with st.expander("With simulated 429s and a pace you can follow"):
        st.code(f"PORTER_FAULTS=429:0.15 python -m porter.write --run {run_dir.name}{flag} --pace 0.5", language="bash")
    live_view(run_dir)


@st.fragment(run_every="1s")
def live_view(run_dir: Path) -> None:
    checkpoint = load(run_dir, "checkpoint.json")
    if checkpoint:
        share = checkpoint["rows_committed"] / max(checkpoint["total_rows"], 1)
        st.progress(share, text=(
            f"Checkpoint: batch {checkpoint['batches_committed']}/{checkpoint['total_batches']}, "
            f"{checkpoint['rows_committed']:,}/{checkpoint['total_rows']:,} rows committed"
        ))
    else:
        st.progress(0.0, text="Waiting for the writer to start…")
    log = run_dir / "write.log"
    if log.exists():
        st.code("\n".join(log.read_text().splitlines()[-LOG_LINES:]), language="text")
    result = load(run_dir, "write_result.json")
    if result and result["ok"]:
        st.success(
            f"Check passed: {result['table_rows']:,} rows, {result['distinct_ids']:,} distinct IDs, "
            f"{result['missing_ids']} of this run's IDs missing."
        )


def main() -> None:
    st.title("Porter")
    st.markdown("Migrates messy data into any API, safely. Nothing is written until you approve the mapping and the dry run.")
    if is_fake():
        st.info("Fake mode: model replies come from `examples/`, with no network and no cost.")

    run_dir = pick_run()
    if run_dir is None:
        return
    st.caption(f"Run `{run_dir.name}`. Model cost so far: ${run_cost(run_dir):.4f}")
    source = Path(read_json(run_dir / "run.json")["source"])
    target = load_target()

    profile = profile_step(run_dir, source)
    if profile is None:
        return
    approved = map_step(run_dir, source, profile, target)
    if approved is None:
        return
    if not code_step(run_dir, source, approved, target):
        return
    report = dry_run_step(run_dir)
    if report is None:
        return
    write_step(run_dir, report)


main()
