"""Load a GitHub Actions workflow file and validate its structure."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class WorkflowError(Exception):
    """The workflow file cannot be read or is not valid YAML."""


@dataclass(frozen=True)
class Issue:
    level: str  # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


@dataclass
class Workflow:
    path: Path
    data: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.path.name)

    @property
    def jobs(self) -> dict[str, dict[str, Any]]:
        jobs = self.data.get("jobs")
        return jobs if isinstance(jobs, dict) else {}

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]


KNOWN_STEP_KEYS = {
    "id", "name", "if", "uses", "run", "with", "env", "shell", "working-directory",
    "continue-on-error", "timeout-minutes",
}  # fmt: skip
KNOWN_JOB_KEYS = {
    "name", "runs-on", "needs", "if", "steps", "env", "defaults", "strategy", "services",
    "container", "outputs", "permissions", "environment", "concurrency", "timeout-minutes",
    "continue-on-error", "uses", "with", "secrets",
}  # fmt: skip
KNOWN_TOP_KEYS = {"name", "run-name", "on", "env", "defaults", "jobs", "permissions", "concurrency"}


def load_workflow(path: str | Path) -> Workflow:
    """Read ``path`` and validate it. Raises WorkflowError for unreadable or non-YAML files."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise WorkflowError(f"workflow file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowError(f"cannot read {path}: {exc}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise WorkflowError(f"{path} is not valid YAML{where}: {problem}") from None
    if not isinstance(data, dict):
        raise WorkflowError(f"{path} must contain a YAML mapping at the top level")
    # YAML 1.1 reads the bare key `on` as the boolean True.
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    wf = Workflow(path=path, data=data)
    wf.issues.extend(validate(data))
    return wf


def _err(out: list[Issue], where: str, message: str) -> None:
    out.append(Issue("error", where, message))


def _warn(out: list[Issue], where: str, message: str) -> None:
    out.append(Issue("warning", where, message))


def validate(data: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    for key in data:
        if key not in KNOWN_TOP_KEYS:
            _warn(issues, "workflow", f"unknown top-level key {key!r}")
    if "on" not in data:
        _err(issues, "workflow", "missing 'on' (what triggers this workflow)")
    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        _err(issues, "workflow", "'jobs' must be a non-empty mapping")
        return issues
    for job_id, job in jobs.items():
        _validate_job(issues, str(job_id), job, set(map(str, jobs)))
    _check_cycles(issues, jobs)
    return issues


def needs_of(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    if isinstance(needs, list):
        return [str(n) for n in needs]
    return []


def _validate_job(issues: list[Issue], job_id: str, job: Any, all_jobs: set[str]) -> None:
    where = f"job '{job_id}'"
    if not isinstance(job, dict):
        _err(issues, where, "must be a mapping")
        return
    for key in job:
        if key not in KNOWN_JOB_KEYS:
            _warn(issues, where, f"unknown key {key!r}")
    if "uses" in job:
        if "steps" in job:
            _err(issues, where, "a job cannot have both 'uses' (reusable workflow) and 'steps'")
        return
    if "runs-on" not in job:
        _err(issues, where, "missing 'runs-on'")
    elif not isinstance(job["runs-on"], str | list | dict):
        _err(issues, where, "'runs-on' must be a string, list or mapping")
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        _err(issues, where, "'steps' must be a non-empty list")
        steps = []
    raw_needs = job.get("needs", [])
    if raw_needs and not isinstance(raw_needs, str | list):
        _err(issues, where, "'needs' must be a job id or a list of job ids")
    for need in needs_of(job):
        if need not in all_jobs:
            _err(issues, where, f"needs unknown job {need!r}")
        if need == job_id:
            _err(issues, where, "a job cannot need itself")
    timeout = job.get("timeout-minutes")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int | float)):
        _err(issues, where, "'timeout-minutes' must be a number")
    seen_ids: set[str] = set()
    for index, step in enumerate(steps, start=1):
        _validate_step(issues, f"{where} step {index}", step, seen_ids)


def _validate_step(issues: list[Issue], where: str, step: Any, seen_ids: set[str]) -> None:
    if not isinstance(step, dict):
        _err(issues, where, "must be a mapping")
        return
    if step.get("name"):
        where = f"{where} ({step['name']})"
    for key in step:
        if key not in KNOWN_STEP_KEYS:
            _warn(issues, where, f"unknown key {key!r}")
    has_run, has_uses = "run" in step, "uses" in step
    if has_run and has_uses:
        _err(issues, where, "has both 'run' and 'uses'; a step needs exactly one")
    elif not has_run and not has_uses:
        _err(issues, where, "needs either 'run' or 'uses'")
    if has_run and not isinstance(step["run"], str):
        _err(issues, where, "'run' must be a string")
    if has_uses:
        uses = step["uses"]
        if not isinstance(uses, str) or not uses.strip():
            _err(issues, where, "'uses' must be a non-empty string")
        elif not uses.startswith(("./", "docker://")) and "@" not in uses:
            _warn(issues, where, f"'uses: {uses}' has no @version; GitHub requires one")
    if "with" in step and not has_uses:
        _warn(issues, where, "'with' only applies to steps that use an action")
    step_id = step.get("id")
    if step_id is not None:
        if str(step_id) in seen_ids:
            _err(issues, where, f"duplicate step id {step_id!r}")
        seen_ids.add(str(step_id))
    shell = step.get("shell")
    if shell is not None and not isinstance(shell, str):
        _err(issues, where, "'shell' must be a string")


def _check_cycles(issues: list[Issue], jobs: dict[str, Any]) -> None:
    graph = {str(k): needs_of(v) if isinstance(v, dict) else [] for k, v in jobs.items()}
    state: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        if state.get(node) == 2 or node not in graph:
            return
        if state.get(node) == 1:
            cycle = trail[trail.index(node) :] + [node]
            _err(issues, "workflow", "jobs depend on each other in a loop: " + " -> ".join(cycle))
            return
        state[node] = 1
        for dep in graph[node]:
            visit(dep, [*trail, node])
        state[node] = 2

    for node in graph:
        visit(node, [])
