"""Execute a plan job by job and collect results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TextIO

from .backends import BackendError, JobSession, check_docker, open_session
from .exprs import Unsupported, condition
from .plan import JobPlan, Options, Plan, base_context, resolve_step


@dataclass
class StepResult:
    index: int
    name: str
    status: str  # passed | failed | failed-ignored | skipped | noop
    detail: str = ""
    seconds: float = 0.0


@dataclass
class JobResult:
    job_id: str
    name: str
    status: str  # passed | failed | skipped
    detail: str = ""
    steps: list[StepResult] = field(default_factory=list)


@dataclass
class RunResult:
    jobs: list[JobResult] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(j.status == "failed" for j in self.jobs)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for job in self.jobs:
            out[job.status] = out.get(job.status, 0) + 1
        return out


def run_plan(plan: Plan, opts: Options, out: TextIO) -> RunResult:
    """Run every runnable job in dependency order. Raises BackendError if Docker is unusable."""
    result = RunResult()
    docker = None
    if not opts.host and any(job.run for job in plan.jobs):
        docker = check_docker()
    outcome: dict[str, str] = {}
    for job in plan.jobs:
        jr = _run_job(plan, job, opts, docker, outcome, out)
        outcome[job.job_id] = jr.status
        result.jobs.append(jr)
    return result


def _run_job(
    plan: Plan, job: JobPlan, opts: Options, docker: str | None, outcome: dict[str, str],
    out: TextIO,
) -> JobResult:  # fmt: skip
    if not job.run:
        return JobResult(job.job_id, job.name, "skipped", "; ".join(job.reasons))
    blocked = [n for n in job.needs if outcome.get(n) != "passed"]
    if blocked and "always()" not in str(job.raw.get("if", "")):
        why = f"needs {', '.join(blocked)} which did not pass"
        out.write(f"\n== job {job.job_id}: skipped ({why}) ==\n")
        return JobResult(job.job_id, job.name, "skipped", why)
    target = "this machine (--host)" if opts.host else opts.image
    out.write(f"\n== job {job.job_id} ({job.name}) on {target} ==\n")
    out.flush()
    try:
        session = open_session(job.job_id, opts.workspace, opts.image, opts.host, docker, out)
    except BackendError as exc:
        out.write(f"   cannot start job: {exc}\n")
        return JobResult(job.job_id, job.name, "failed", str(exc))
    try:
        return _run_steps(plan, job, opts, session, out)
    finally:
        session.close()


def _run_steps(
    plan: Plan, job: JobPlan, opts: Options, session: JobSession, out: TextIO
) -> JobResult:
    jr = JobResult(job.job_id, job.name, "passed")
    ctx = base_context(opts)
    failed = False
    total = len(job.steps)
    for step in job.steps:
        header = f"-- step {step.index}/{total}: {step.name}"
        if step.kind in ("noop", "skip"):
            word = "not run" if step.kind == "skip" else "no-op"
            out.write(f"{header}  [{word}: {step.reason}]\n")
            jr.steps.append(StepResult(step.index, step.name, "skipped", step.reason))
            continue
        ctx.job_status = "failure" if failed else "success"
        ctx.env = _current_env(session)
        try:
            should_run = condition(step.raw.get("if"), ctx)
        except Unsupported as exc:
            out.write(f"{header}  [not run: condition not supported: {exc}]\n")
            jr.steps.append(StepResult(step.index, step.name, "skipped", f"if: {exc}"))
            continue
        if not should_run:
            reason = "an earlier step failed" if failed else "its `if:` condition is false"
            out.write(f"{header}  [skipped: {reason}]\n")
            jr.steps.append(StepResult(step.index, step.name, "skipped", reason))
            continue
        try:
            resolved = resolve_step(plan.workflow, job.raw, step.raw, ctx)
        except Unsupported as exc:
            out.write(f"{header}  [not run: {exc}]\n")
            jr.steps.append(StepResult(step.index, step.name, "skipped", str(exc)))
            continue
        out.write(f"{header}\n")
        lines = resolved.script.strip().splitlines() or [""]
        out.write(f"   $ {lines[0]}{' ...' if len(lines) > 1 else ''}\n")
        out.flush()
        try:
            res = session.run(resolved, resolved.env, "   | ", out)
        except BackendError as exc:
            out.write(f"   cannot run step: {exc}\n")
            jr.steps.append(StepResult(step.index, step.name, "failed", str(exc)))
            failed = True
            jr.status = "failed"
            continue
        session.collect_files()
        if res.timed_out:
            detail = f"timed out after {resolved.timeout:g}s"
        else:
            detail = f"exit code {res.code}"
        if res.code == 0:
            out.write(f"   ok ({res.seconds:.1f}s)\n")
            jr.steps.append(StepResult(step.index, step.name, "passed", "", res.seconds))
        elif resolved.continue_on_error:
            out.write(f"   failed ({detail}) but continue-on-error is set\n")
            jr.steps.append(
                StepResult(step.index, step.name, "failed-ignored", detail, res.seconds)
            )
        else:
            out.write(f"   FAILED ({detail}, {res.seconds:.1f}s)\n")
            jr.steps.append(StepResult(step.index, step.name, "failed", detail, res.seconds))
            failed = True
            jr.status = "failed"
            jr.detail = f"step {step.index} ({step.name}) failed: {detail}"
    return jr


def _current_env(session: JobSession) -> dict[str, str]:
    return dict(session.extra_env)

