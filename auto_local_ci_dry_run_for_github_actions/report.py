"""Plain-language reports for `dryact check` and the end of `dryact run`."""

from __future__ import annotations

from .plan import Plan
from .runner import RunResult

_LEVEL_TITLES = {
    "unsupported": "Will NOT run locally",
    "partial": "Runs, but not like GitHub",
    "ignored": "Ignored (no effect locally)",
}


def render_check(plan: Plan) -> str:
    wf = plan.workflow
    lines = [f"Workflow: {wf.name} ({wf.path.name})"]
    errors, warnings = wf.errors, wf.warnings
    if errors:
        lines.append(f"Structure: {len(errors)} error(s), {len(warnings)} warning(s)")
    else:
        lines.append(f"Structure: valid ({len(warnings)} warning(s))")
    for issue in [*errors, *warnings]:
        lines.append(f"  {issue}")
    lines.append("")
    lines.append("Jobs:")
    for job in plan.jobs:
        runnable = [s for s in job.steps if s.kind == "run"]
        if job.run:
            skipped = len(job.steps) - len(runnable)
            note = f", {skipped} not run" if skipped else ""
            count = f"{len(runnable)} of {len(job.steps)} steps"
            lines.append(f"  {job.job_id}: will run {count}{note}")
        else:
            lines.append(f"  {job.job_id}: skipped, {'; '.join(job.reasons)}")
    for level in ("unsupported", "partial", "ignored"):
        found = [f for f in plan.findings if f.level == level]
        if not found:
            continue
        lines.append("")
        lines.append(f"{_LEVEL_TITLES[level]}:")
        for finding in found:
            lines.append(f"  - {finding.where}: {finding.text}")
    lines.append("")
    lines.append(verdict(plan))
    return "\n".join(lines)


def verdict(plan: Plan) -> str:
    total = len(plan.jobs)
    runnable = sum(1 for j in plan.jobs if j.run)
    unsupported = sum(1 for f in plan.findings if f.level == "unsupported")
    if plan.workflow.errors:
        return "Verdict: fix the errors above first; GitHub would reject this workflow."
    if runnable == 0:
        return f"Verdict: nothing can run locally (0 of {total} jobs)."
    if unsupported == 0:
        return f"Verdict: all {total} job(s) can run locally."
    return (
        f"Verdict: {runnable} of {total} job(s) can run locally; "
        f"{unsupported} unsupported item(s) will be skipped, so a green local run is not a "
        "guarantee for the skipped parts."
    )


def render_summary(result: RunResult) -> str:
    lines = ["", "Summary:"]
    for job in result.jobs:
        extra = f" ({job.detail})" if job.detail else ""
        lines.append(f"  {job.status.upper():7} {job.job_id}{extra}")
        for step in job.steps:
            if step.status in ("failed", "failed-ignored"):
                lines.append(f"            step {step.index} {step.name}: {step.detail}")
    counts = result.counts()
    lines.append(
        "  " + ", ".join(f"{n} {name}" for name, n in sorted(counts.items())) if counts else ""
    )
    return "\n".join(lines)
