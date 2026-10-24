from pathlib import Path

from auto_local_ci_dry_run_for_github_actions.exprs import Context
from auto_local_ci_dry_run_for_github_actions.plan import (
    Options,
    build_plan,
    resolve_step,
)
from auto_local_ci_dry_run_for_github_actions.report import render_check, verdict

WF = """
name: CI
on: push
permissions: read-all
env:
  TOP: top
jobs:
  test:
    runs-on: ubuntu-latest
    environment: prod
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {node-version: 20}
      - uses: actions/cache@v4
      - uses: some/marketplace-action@v1
      - uses: ./local-action
      - name: build
        run: echo "$TOP ${{ env.TOP }}"
      - name: needs secret
        run: echo ${{ secrets.KEY }}
      - name: pwsh
        shell: pwsh
        run: Write-Host hi
  matrix:
    runs-on: ubuntu-latest
    strategy: {matrix: {v: [1, 2]}}
    steps: [{run: echo hi}]
  svc:
    runs-on: ubuntu-latest
    services: {db: {image: postgres}}
    steps: [{run: echo hi}]
  win:
    runs-on: windows-latest
    steps: [{run: echo hi}]
  container:
    runs-on: ubuntu-latest
    container: node:20
    steps: [{run: echo hi}]
  reuse:
    uses: org/repo/.github/workflows/x.yml@main
  last:
    runs-on: ubuntu-latest
    needs: test
    steps: [{run: echo done}]
"""


def levels(plan, level: str) -> str:
    return " | ".join(f.text for f in plan.findings if f.level == level)


def test_jobs_with_unsupported_features_are_skipped_with_reasons(make_workflow) -> None:
    plan = build_plan(make_workflow(WF), Options())
    runnable = {j.job_id for j in plan.jobs if j.run}
    assert runnable == {"test", "last"}
    reasons = {j.job_id: " ".join(j.reasons) for j in plan.jobs if not j.run}
    assert "build matrix" in reasons["matrix"]
    assert "service containers" in reasons["svc"]
    assert "windows-latest" in reasons["win"]
    assert "container:" in reasons["container"]
    assert "reusable workflow" in reasons["reuse"]


def test_step_classification(make_workflow) -> None:
    plan = build_plan(make_workflow(WF), Options())
    kinds = {s.index: s.kind for s in plan.job("test").steps}
    assert kinds == {1: "noop", 2: "noop", 3: "noop", 4: "skip", 5: "skip", 6: "run",
                     7: "skip", 8: "skip"}  # fmt: skip
    unsupported = levels(plan, "unsupported")
    assert "marketplace action" in unsupported
    assert "local composite" in unsupported
    assert "secret 'KEY'" in unsupported
    assert "pwsh" in unsupported


def test_partial_and_ignored_findings(make_workflow) -> None:
    plan = build_plan(make_workflow(WF), Options())
    partial = levels(plan, "partial")
    assert "actions/checkout is skipped" in partial
    assert "version 20" in partial and "--image" in partial
    assert "no cache service" in partial
    ignored = levels(plan, "ignored")
    assert "`permissions`" in ignored and "`environment`" in ignored


def test_needs_order_is_topological(make_workflow) -> None:
    text = """
on: push
jobs:
  c: {runs-on: ubuntu-latest, needs: [b], steps: [{run: x}]}
  b: {runs-on: ubuntu-latest, needs: a, steps: [{run: x}]}
  a: {runs-on: ubuntu-latest, steps: [{run: x}]}
"""
    assert [j.job_id for j in build_plan(make_workflow(text)).jobs] == ["a", "b", "c"]


def test_only_jobs_filter(make_workflow) -> None:
    plan = build_plan(make_workflow(WF), Options(only_jobs=["last"]))
    assert [j.job_id for j in plan.jobs if j.run] == ["last"]
    assert "not selected" in " ".join(plan.job("test").reasons)


def test_job_if_false_and_unsupported(make_workflow) -> None:
    text = """
on: push
jobs:
  a: {runs-on: ubuntu-latest, if: "github.event_name == 'release'", steps: [{run: x}]}
  b: {runs-on: ubuntu-latest, if: "github.event.pull_request.draft", steps: [{run: x}]}
"""
    plan = build_plan(make_workflow(text))
    assert "false locally" in " ".join(plan.job("a").reasons)
    assert not plan.job("b").run


def test_resolve_step_merges_env_defaults_and_timeout(make_workflow) -> None:
    text = """
on: push
env: {A: one, B: "${{ env.A }}-two"}
defaults: {run: {shell: sh, working-directory: sub}}
jobs:
  j:
    runs-on: ubuntu-latest
    timeout-minutes: 2
    env: {C: three}
    steps:
      - run: echo "${{ env.A }}${{ env.C }}"
        env: {D: "${{ env.B }}!"}
        continue-on-error: true
"""
    wf = make_workflow(text)
    job = wf.jobs["j"]
    res = resolve_step(wf, job, job["steps"][0], Context())
    assert res.env == {"A": "one", "B": "one-two", "C": "three", "D": "one-two!"}
    assert res.script == "echo \"onethree\""
    assert (res.shell, res.workdir) == ("sh", "sub")
    assert (res.timeout, res.continue_on_error) == (120.0, True)


def test_report_text_is_plain_language(make_workflow) -> None:
    plan = build_plan(make_workflow(WF), Options())
    text = render_check(plan)
    assert "Will NOT run locally:" in text
    assert "Runs, but not like GitHub:" in text
    assert "test: will run 1 of 8 steps, 7 not run" in text
    assert "2 of 7 job(s) can run locally" in verdict(plan)


def test_verdict_for_clean_and_invalid_workflows(make_workflow) -> None:
    clean = make_workflow("on: push\njobs:\n  a: {runs-on: ubuntu-latest, steps: [{run: x}]}\n")
    assert verdict(build_plan(clean)) == "Verdict: all 1 job(s) can run locally."
    broken = make_workflow("name: x\n")
    assert "fix the errors" in verdict(build_plan(broken))


def test_github_context_comes_from_git_when_available(tmp_path: Path, make_workflow) -> None:
    from auto_local_ci_dry_run_for_github_actions.plan import base_context

    ctx = base_context(Options(workspace=tmp_path))  # not a git repo
    assert "sha" not in ctx.github and ctx.github["workspace"] == "/workspace"
    host = base_context(Options(workspace=tmp_path, host=True))
    assert host.github["workspace"] == str(tmp_path)
