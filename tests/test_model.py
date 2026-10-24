from pathlib import Path

import pytest

from auto_local_ci_dry_run_for_github_actions.model import (
    WorkflowError,
    load_workflow,
    needs_of,
)

GOOD = """
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo hi
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "wf.yml"
    path.write_text(text, encoding="utf-8")
    return path


def messages(wf) -> list[str]:
    return [str(i) for i in wf.issues]


def test_valid_workflow_has_no_issues(tmp_path: Path) -> None:
    wf = load_workflow(write(tmp_path, GOOD))
    assert wf.issues == []
    assert wf.name == "CI"
    assert list(wf.jobs) == ["build"]


def test_on_key_parsed_as_boolean_is_repaired(tmp_path: Path) -> None:
    wf = load_workflow(write(tmp_path, GOOD))
    assert "on" in wf.data and True not in wf.data


def test_missing_file_and_bad_yaml_and_non_mapping(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError, match="not found"):
        load_workflow(tmp_path / "nope.yml")
    with pytest.raises(WorkflowError, match="not valid YAML at line"):
        load_workflow(write(tmp_path, "jobs:\n  a: [unclosed\n"))
    with pytest.raises(WorkflowError, match="mapping"):
        load_workflow(write(tmp_path, "- just\n- a list\n"))


def test_missing_on_and_jobs(tmp_path: Path) -> None:
    wf = load_workflow(write(tmp_path, "name: x\n"))
    text = " ".join(messages(wf))
    assert "missing 'on'" in text and "'jobs' must be a non-empty mapping" in text


def test_job_without_runs_on_or_steps(tmp_path: Path) -> None:
    wf = load_workflow(write(tmp_path, "on: push\njobs:\n  a:\n    name: x\n"))
    text = " ".join(messages(wf))
    assert "missing 'runs-on'" in text and "'steps' must be a non-empty list" in text


def test_step_needs_exactly_one_of_run_and_uses(tmp_path: Path) -> None:
    text = (
        "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n"
        "      - run: a\n        uses: b@v1\n      - name: empty\n"
    )
    out = " ".join(messages(load_workflow(write(tmp_path, text))))
    assert "both 'run' and 'uses'" in out
    assert "needs either 'run' or 'uses'" in out


def test_uses_without_version_and_unknown_keys_warn(tmp_path: Path) -> None:
    text = (
        "on: push\nbogus: 1\njobs:\n  a:\n    runs-on: x\n    wat: 1\n    steps:\n"
        "      - uses: actions/checkout\n        extra: 1\n"
    )
    wf = load_workflow(write(tmp_path, text))
    assert wf.errors == []
    out = " ".join(messages(wf))
    assert "no @version" in out and "unknown top-level key 'bogus'" in out
    assert "unknown key 'wat'" in out and "unknown key 'extra'" in out


def test_needs_unknown_self_and_cycles(tmp_path: Path) -> None:
    text = (
        "on: push\njobs:\n"
        "  a:\n    runs-on: x\n    needs: [b, ghost]\n    steps: [{run: a}]\n"
        "  b:\n    runs-on: x\n    needs: a\n    steps: [{run: a}]\n"
        "  c:\n    runs-on: x\n    needs: c\n    steps: [{run: a}]\n"
    )
    out = " ".join(messages(load_workflow(write(tmp_path, text))))
    assert "unknown job 'ghost'" in out
    assert "a -> b -> a" in out or "b -> a -> b" in out
    assert "cannot need itself" in out


def test_duplicate_step_ids_and_bad_timeout(tmp_path: Path) -> None:
    text = (
        "on: push\njobs:\n  a:\n    runs-on: x\n    timeout-minutes: soon\n    steps:\n"
        "      - {id: s, run: a}\n      - {id: s, run: b}\n"
    )
    out = " ".join(messages(load_workflow(write(tmp_path, text))))
    assert "duplicate step id 's'" in out and "'timeout-minutes' must be a number" in out


def test_reusable_workflow_job_is_valid_without_steps(tmp_path: Path) -> None:
    text = "on: push\njobs:\n  call:\n    uses: org/repo/.github/workflows/x.yml@main\n"
    assert load_workflow(write(tmp_path, text)).errors == []


def test_needs_of_accepts_string_and_list() -> None:
    assert needs_of({"needs": "a"}) == ["a"]
    assert needs_of({"needs": ["a", "b"]}) == ["a", "b"]
    assert needs_of({}) == []
