"""Drive the whole page in fake mode, the way a person would."""
import json

from streamlit.testing.v1 import AppTest


def click(app: AppTest, key: str) -> AppTest:
    app.button(key=key).click()
    app.run(timeout=60)
    assert not app.exception, app.exception
    return app


def test_full_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTER_RUNS_DIR", str(tmp_path))
    app = AppTest.from_file("../app.py", default_timeout=60).run()
    assert not app.exception, app.exception

    click(app, "start")
    run_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    click(app, "profile")
    assert (run_dir / "profile.json").exists()
    click(app, "propose")
    assert app.radio(key="answer_q1").value == "a"
    app.radio(key="answer_q1").set_value("b")
    click(app, "approve")
    assert json.loads((run_dir / "mapping.approved.json").read_text())["answers"] == {"q1": "b"}

    click(app, "generate")
    assert "PROPER_NAMES" in app.code[0].value
    assert any("Leave diameter empty" in m.value for m in app.success)
    assert not (run_dir / "dry_run.json").exists()
    click(app, "approve_code")

    report = json.loads((run_dir / "dry_run.json").read_text())
    assert report["rows_to_write"] == 4903
    commands = " ".join(c.value for c in app.code)
    assert f"python -m porter.write --run {run_dir.name}" in commands
    assert [m.value for m in app.metric][:2] == ["5,000", "4,903"]


def test_redoing_a_step_clears_what_was_built_on_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTER_RUNS_DIR", str(tmp_path))
    app = AppTest.from_file("../app.py", default_timeout=60).run()
    for key in ("start", "profile", "propose", "approve", "generate", "approve_code"):
        click(app, key)
    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    assert (run_dir / "valid.jsonl").exists()
    click(app, "approve")
    assert not (run_dir / "valid.jsonl").exists()
    assert not (run_dir / "transform.approved.json").exists()
