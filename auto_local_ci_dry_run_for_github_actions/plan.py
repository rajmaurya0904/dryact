"""Decide what can run locally and explain what cannot, in plain language."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exprs import Context, Unsupported, condition, has_expression, interpolate
from .model import Workflow, needs_of

DEFAULT_IMAGE = "ubuntu:22.04"
CONTAINER_WORKSPACE = "/workspace"

# Actions that need no work locally because the workspace is already mounted / there is no cloud.
NOOP_ACTIONS = {
    "actions/checkout": "the current directory is mounted as the workspace instead",
    "actions/cache": "there is no cache service locally, so every run starts cold",
    "actions/upload-artifact": "artifacts are not uploaded anywhere locally",
    "actions/download-artifact": "no artifacts exist locally to download",
}
SETUP_ACTIONS = {
    "actions/setup-python", "actions/setup-node", "actions/setup-java", "actions/setup-go",
    "actions/setup-dotnet", "actions/setup-ruby",
}  # fmt: skip


@dataclass
class Finding:
    level: str  # "unsupported" | "partial" | "ignored"
    where: str
    text: str


@dataclass
class StepPlan:
    index: int
    name: str
    kind: str  # "run" | "noop" | "skip"
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobPlan:
    job_id: str
    name: str
    raw: dict[str, Any]
    run: bool
    reasons: list[str] = field(default_factory=list)
    steps: list[StepPlan] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)


@dataclass
class Plan:
    workflow: Workflow
    jobs: list[JobPlan]
    findings: list[Finding]

    def job(self, job_id: str) -> JobPlan:
        return next(j for j in self.jobs if j.job_id == job_id)


@dataclass
class Options:
    image: str = DEFAULT_IMAGE
    host: bool = False
    secrets: Mapping[str, str] = field(default_factory=dict)
    only_jobs: list[str] = field(default_factory=list)
    workspace: Path = field(default_factory=Path.cwd)


def _git(args: list[str], cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def base_context(opts: Options) -> Context:
    """The static part of the expression context (github.*, runner.*, secrets)."""
    workspace = str(opts.workspace) if opts.host else CONTAINER_WORKSPACE
    github = {
        "workspace": workspace,
        "event_name": "push",
        "actor": "local",
        "run_id": "0",
        "run_number": "0",
    }
    if sha := _git(["rev-parse", "HEAD"], opts.workspace):
        github["sha"] = sha
    if ref := _git(["rev-parse", "--abbrev-ref", "HEAD"], opts.workspace):
        github["ref_name"] = ref
        github["ref"] = f"refs/heads/{ref}"
    return Context(
        secrets=dict(opts.secrets),
        github=github,
        runner={"os": "Linux", "arch": "X64", "temp": "/tmp"},
    )


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def runs_on_label(job: dict[str, Any]) -> str:
    runs_on = job.get("runs-on")
    if isinstance(runs_on, list):
        return ", ".join(map(str, runs_on))
    if isinstance(runs_on, dict):
        return str(runs_on.get("labels") or runs_on.get("group") or runs_on)
    return str(runs_on)


def _is_linux(label: str) -> bool:
    lowered = label.lower()
    if "self-hosted" in lowered:
        return False
    return "ubuntu" in lowered or "linux" in lowered


def classify_step(
    index: int, step: dict[str, Any], job_id: str, findings: list[Finding]
) -> StepPlan:
    label = step.get("name") or step.get("id") or (step.get("uses") or "run step")
    name = str(label).strip().splitlines()[0] if str(label).strip() else f"step {index}"
    where = f"job '{job_id}', step {index} ({name})"
    if "run" in step:
        return StepPlan(index, name, "run", raw=step)
    uses = str(step.get("uses", ""))
    action = uses.split("@")[0]
    if action in NOOP_ACTIONS:
        findings.append(Finding("partial", where, f"{action} is skipped: {NOOP_ACTIONS[action]}."))
        return StepPlan(index, name, "noop", NOOP_ACTIONS[action], step)
    if action in SETUP_ACTIONS:
        wanted = step.get("with") if isinstance(step.get("with"), dict) else {}
        version = next((str(v) for k, v in wanted.items() if k.endswith("-version")), "")
        findings.append(
            Finding(
                "partial",
                where,
                f"{action} does not install anything locally"
                + (f" (workflow asks for version {version})" if version else "")
                + "; the tool must already exist in the image, so pick one with --image.",
            )
        )
        return StepPlan(index, name, "noop", "tool must exist in the image", step)
    if uses.startswith("./"):
        reason = "local composite/JavaScript actions are not supported"
    elif uses.startswith("docker://"):
        reason = "docker:// actions are not supported"
    else:
        reason = f"{action} is a marketplace action and dryact cannot run actions"
    findings.append(Finding("unsupported", where, f"step skipped: {reason}."))
    return StepPlan(index, name, "skip", reason, step)


def _job_blockers(job: dict[str, Any], opts: Options) -> list[str]:
    blockers: list[str] = []
    if "uses" in job:
        blockers.append("it calls a reusable workflow (`uses:`), which is not supported")
    strategy = job.get("strategy")
    if isinstance(strategy, dict) and "matrix" in strategy:
        blockers.append(
            "it uses a build matrix; dryact would have to guess which combination to run"
        )
    if job.get("services"):
        blockers.append("it needs service containers (`services:`), which are not started locally")
    if job.get("container"):
        blockers.append("it sets a job `container:`; use --image to choose the image instead")
    label = runs_on_label(job)
    if "uses" not in job and not _is_linux(label):
        blockers.append(f"it runs on '{label}', and only Linux runners can be imitated")
    return blockers


_IGNORED_TOP = {
    "permissions": "token permissions have no effect locally",
    "concurrency": "concurrency groups are not enforced locally",
}
_IGNORED_JOB = {
    "permissions": "token permissions have no effect locally",
    "concurrency": "concurrency groups are not enforced locally",
    "environment": "deployment environments and their protection rules are not applied",
    "outputs": "job outputs are not passed on to dependent jobs",
}


def build_plan(wf: Workflow, opts: Options | None = None) -> Plan:
    """Turn a (valid) workflow into an ordered plan plus a list of unsupported features."""
    opts = opts or Options()
    findings: list[Finding] = []
    for key, text in _IGNORED_TOP.items():
        if key in wf.data:
            findings.append(Finding("ignored", "workflow", f"`{key}`: {text}."))
    ctx = base_context(opts)
    plans: dict[str, JobPlan] = {}
    for job_id, job in wf.jobs.items():
        job_id = str(job_id)
        where = f"job '{job_id}'"
        name = str(job.get("name") or job_id)
        plan = JobPlan(job_id, name, job, run=True, needs=needs_of(job))
        plans[job_id] = plan
        for blocker in _job_blockers(job, opts):
            plan.run = False
            plan.reasons.append(blocker)
            findings.append(Finding("unsupported", where, f"job skipped: {blocker}."))
        for key, text in _IGNORED_JOB.items():
            if key in job:
                findings.append(Finding("ignored", where, f"`{key}`: {text}."))
        if "timeout-minutes" in job:
            findings.append(Finding("partial", where, "`timeout-minutes` is applied per step."))
        if "if" in job and plan.run:
            try:
                if not _job_condition(job["if"], ctx):
                    plan.run = False
                    plan.reasons.append("its `if:` condition is false locally")
            except Unsupported as exc:
                plan.run = False
                plan.reasons.append(f"its `if:` condition cannot be evaluated ({exc})")
                findings.append(Finding("unsupported", where, f"job skipped: {exc}."))
        steps = job.get("steps") if isinstance(job.get("steps"), list) else []
        for index, step in enumerate(steps, start=1):
            if isinstance(step, dict):
                plan.steps.append(classify_step(index, step, job_id, findings))
        if opts.only_jobs and job_id not in opts.only_jobs:
            plan.run = False
            plan.reasons.append("not selected with --job")
    ordered = _topological(plans)
    _static_step_checks(wf, ordered, ctx, findings)
    return Plan(wf, ordered, findings)


def _job_condition(raw: Any, ctx: Context) -> bool:
    return condition(raw, ctx)


def _static_step_checks(
    wf: Workflow, jobs: list[JobPlan], ctx: Context, findings: list[Finding]
) -> None:
    """Trial-resolve every run script, env value and `if:` so unsupported ones show up early."""
    for job in jobs:
        if not job.run:
            continue
        for step in job.steps:
            if step.kind != "run":
                continue
            where = f"job '{job.job_id}', step {step.index} ({step.name})"
            try:
                resolve_step(wf, job.raw, step.raw, ctx)
            except Unsupported as exc:
                step.kind = "skip"
                step.reason = str(exc)
                findings.append(Finding("unsupported", where, f"step skipped: {exc}."))
            if step.raw.get("shell") and str(step.raw["shell"]).split()[0] not in ("bash", "sh"):
                shell = step.raw["shell"]
                step.kind = "skip"
                step.reason = f"shell '{shell}' is not available (only bash and sh)"
                findings.append(Finding("unsupported", where, f"step skipped: {step.reason}."))
            if "continue-on-error" in step.raw:
                findings.append(
                    Finding("partial", where, "`continue-on-error` is honoured only as true/false.")
                )


def _topological(plans: dict[str, JobPlan]) -> list[JobPlan]:
    ordered: list[JobPlan] = []
    done: set[str] = set()

    def visit(job_id: str, trail: tuple[str, ...] = ()) -> None:
        if job_id in done or job_id in trail or job_id not in plans:
            return
        for dep in plans[job_id].needs:
            visit(dep, (*trail, job_id))
        done.add(job_id)
        ordered.append(plans[job_id])

    for job_id in plans:
        visit(job_id)
    return ordered


@dataclass
class ResolvedStep:
    script: str
    shell: str
    workdir: str
    env: dict[str, str]
    timeout: float | None
    continue_on_error: bool


def resolve_step(
    wf: Workflow, job: dict[str, Any], step: dict[str, Any], ctx: Context
) -> ResolvedStep:
    """Substitute expressions into a run step. Raises Unsupported if any cannot be resolved."""
    env: dict[str, str] = {}
    local = Context(
        env=dict(ctx.env), secrets=ctx.secrets, github=ctx.github, runner=ctx.runner,
        job_status=ctx.job_status,
    )  # fmt: skip
    for source in (wf.data.get("env"), job.get("env"), step.get("env")):
        if isinstance(source, dict):
            for key, value in source.items():
                text = _scalar(value)
                env[str(key)] = interpolate(text, local) if has_expression(text) else text
                local.env = {**local.env, str(key): env[str(key)]}
    script = interpolate(str(step["run"]), local)
    defaults = _run_defaults(wf, job)
    shell = str(step.get("shell") or defaults.get("shell") or "bash")
    workdir = step.get("working-directory") or defaults.get("working-directory") or ""
    workdir = interpolate(str(workdir), local) if workdir else ""
    timeout = step.get("timeout-minutes")
    if timeout is None:
        timeout = job.get("timeout-minutes")
    coe = step.get("continue-on-error", False)
    if not isinstance(coe, bool):
        raise Unsupported("`continue-on-error` is an expression and cannot be evaluated")
    return ResolvedStep(
        script, shell, workdir, env,
        float(timeout) * 60 if isinstance(timeout, int | float) else None, coe,
    )  # fmt: skip


def _run_defaults(wf: Workflow, job: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in (wf.data.get("defaults"), job.get("defaults")):
        if isinstance(source, dict) and isinstance(source.get("run"), dict):
            merged.update(source["run"])
    return merged
