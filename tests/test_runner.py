import io
from pathlib import Path

from auto_local_ci_dry_run_for_github_actions.plan import Options, build_plan
from auto_local_ci_dry_run_for_github_actions.report import render_summary
from auto_local_ci_dry_run_for_github_actions.runner import run_plan
from tests.conftest import needs_bash

pytestmark = needs_bash


def run(make_workflow, text: str, tmp_path: Path, **kw):
    opts = Options(host=True, workspace=tmp_path, **kw)
    plan = build_plan(make_workflow(text), opts)
    out = io.StringIO()
    result = run_plan(plan, opts, out)
    return result, out.getvalue()


def job(body: str) -> str:
    return f"on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n{body}"


def test_successful_job_streams_output(make_workflow, tmp_path: Path) -> None:
    result, out = run(make_workflow, job("      - run: echo hello-from-step\n"), tmp_path)
    assert not result.failed
    assert "| hello-from-step" in out
    assert result.jobs[0].status == "passed"


def test_failing_step_stops_job_and_skips_later_steps(make_workflow, tmp_path: Path) -> None:
    body = "      - run: exit 3\n      - run: echo never-printed\n"
    result, out = run(make_workflow, job(body), tmp_path)
    assert result.failed
    assert [s.status for s in result.jobs[0].steps] == ["failed", "skipped"]
    assert "exit code 3" in out and "| never-printed" not in out


def test_always_and_failure_steps_run_after_failure(make_workflow, tmp_path: Path) -> None:
    body = (
        "      - run: exit 1\n"
        "      - if: always()\n        run: echo cleanup\n"
        "      - if: failure()\n        run: echo on-failure\n"
        "      - if: success()\n        run: echo nope\n"
    )
    result, out = run(make_workflow, job(body), tmp_path)
    assert [s.status for s in result.jobs[0].steps] == ["failed", "passed", "passed", "skipped"]
    assert "| cleanup" in out and "| on-failure" in out and "| nope" not in out


def test_continue_on_error_keeps_job_green(make_workflow, tmp_path: Path) -> None:
    body = "      - run: exit 1\n        continue-on-error: true\n      - run: echo after\n"
    result, _ = run(make_workflow, job(body), tmp_path)
    assert not result.failed
    assert [s.status for s in result.jobs[0].steps] == ["failed-ignored", "passed"]


GITHUB_ENV_WORKFLOW = r"""
on: push
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - run: |
          echo "FOO=bar" >> "$GITHUB_ENV"
          printf 'MULTI<<EOF\nline1\nline2\nEOF\n' >> "$GITHUB_ENV"
          echo "$PWD/bin" >> "$GITHUB_PATH"
      - run: |
          test "$FOO" = bar
          test "$(printf '%s' "$MULTI" | wc -l | tr -d ' ')" = 1
          mytool
"""


def test_github_env_and_github_path_carry_between_steps(make_workflow, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "mytool"
    tool.write_text("#!/bin/sh\necho tool-ran\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)
    result, out = run(make_workflow, GITHUB_ENV_WORKFLOW, tmp_path)
    assert not result.failed, out
    assert "| tool-ran" in out


def test_workflow_job_and_step_env_and_expressions(make_workflow, tmp_path: Path) -> None:
    text = (
        "on: push\nenv: {A: a}\njobs:\n  j:\n    runs-on: ubuntu-latest\n    env: {B: b}\n"
        '    steps:\n      - env: {C: c}\n        run: echo "$A$B$C ${{ env.C }}"\n'
    )
    _, out = run(make_workflow, text, tmp_path)
    assert "| abc c" in out


def test_working_directory_and_missing_directory(make_workflow, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("x", encoding="utf-8")
    body = "      - run: ls\n        working-directory: sub\n"
    result, out = run(make_workflow, job(body), tmp_path)
    assert "| f.txt" in out and not result.failed
    body = "      - run: ls\n        working-directory: missing\n"
    result, out = run(make_workflow, job(body), tmp_path)
    assert result.failed and "working directory does not exist" in out


def test_sh_shell_is_used_when_requested(make_workflow, tmp_path: Path) -> None:
    body = "      - run: echo shell=$0\n        shell: sh\n"
    result, out = run(make_workflow, job(body), tmp_path)
    assert not result.failed and "| shell=sh" in out


def test_pipefail_only_with_explicit_bash(make_workflow, tmp_path: Path) -> None:
    plain = job("      - run: false | true\n")
    explicit = job("      - run: false | true\n        shell: bash\n")
    assert not run(make_workflow, plain, tmp_path)[0].failed  # default `bash -e`: no pipefail
    assert run(make_workflow, explicit, tmp_path)[0].failed


def test_dependent_job_is_skipped_when_dependency_fails(make_workflow, tmp_path: Path) -> None:
    text = (
        "on: push\njobs:\n"
        "  a: {runs-on: ubuntu-latest, steps: [{run: exit 1}]}\n"
        "  b: {runs-on: ubuntu-latest, needs: a, steps: [{run: echo b}]}\n"
        "  c: {runs-on: ubuntu-latest, needs: a, if: always(), steps: [{run: echo c-ran}]}\n"
    )
    result, out = run(make_workflow, text, tmp_path)
    statuses = {j.job_id: j.status for j in result.jobs}
    assert statuses == {"a": "failed", "b": "skipped", "c": "passed"}
    assert "| c-ran" in out


def test_secrets_are_substituted_only_when_provided(make_workflow, tmp_path: Path) -> None:
    text = job("      - run: echo key=${{ secrets.K }}\n")
    without, out = run(make_workflow, text, tmp_path)
    assert without.jobs[0].steps[0].status == "skipped" and "| key=" not in out
    _, out = run(make_workflow, text, tmp_path, secrets={"K": "v1"})
    assert "| key=v1" in out


def test_timeout_kills_the_step(make_workflow, tmp_path: Path) -> None:
    body = "      - run: sleep 30\n        timeout-minutes: 0.01\n"
    result, out = run(make_workflow, job(body), tmp_path)
    assert result.failed and "timed out" in out


def test_summary_lists_failures(make_workflow, tmp_path: Path) -> None:
    result, _ = run(make_workflow, job("      - name: boom\n        run: exit 2\n"), tmp_path)
    summary = render_summary(result)
    assert "FAILED  j" in summary and "boom: exit code 2" in summary
