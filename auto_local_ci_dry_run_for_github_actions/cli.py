"""Command line interface: ``dryact check`` and ``dryact run``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .backends import BackendError, DockerUnavailable
from .model import WorkflowError, load_workflow
from .plan import DEFAULT_IMAGE, Options, build_plan
from .report import render_check, render_summary
from .runner import run_plan

EXIT_OK = 0
EXIT_STEP_FAILED = 1
EXIT_INVALID = 2  # bad workflow file or arguments
EXIT_NO_DOCKER = 3
EXIT_UNSUPPORTED = 4  # --strict and something could not be reproduced


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dryact",
        description="Check a GitHub Actions workflow and run its shell steps locally.",
    )
    parser.add_argument("--version", action="version", version=f"dryact {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (
        ("check", "validate the workflow and report what cannot run locally"),
        ("run", "run the workflow's shell steps in Docker"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("workflow", help="path to a workflow YAML file")
        p.add_argument("--job", action="append", default=[], metavar="ID", help="only this job")
        p.add_argument("--image", default=DEFAULT_IMAGE, help="Docker image (default %(default)s)")
        p.add_argument("--host", action="store_true", help="run on this machine, not in Docker")
        p.add_argument(
            "--secret", action="append", default=[], metavar="NAME",
            help="make secrets.NAME available, read from the environment variable NAME",
        )  # fmt: skip
        p.add_argument("--strict", action="store_true", help="exit 4 if anything is unsupported")
    return parser


def _secrets(names: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in names:
        if name not in os.environ:
            raise WorkflowError(f"--secret {name}: environment variable {name} is not set")
        values[name] = os.environ[name]
    return values


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        wf = load_workflow(args.workflow)
        opts = Options(
            image=args.image,
            host=args.host,
            secrets=_secrets(args.secret),
            only_jobs=args.job,
            workspace=Path.cwd(),
        )
        unknown = [j for j in args.job if j not in {str(k) for k in wf.jobs}]
        if unknown:
            raise WorkflowError(
                f"no such job: {', '.join(unknown)} (jobs: {', '.join(map(str, wf.jobs))})"
            )
        if wf.errors:
            print(render_check(build_plan(wf, opts)) if wf.jobs else _errors_only(wf))
            return EXIT_INVALID
        plan = build_plan(wf, opts)
    except WorkflowError as exc:
        print(f"dryact: error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    unsupported = any(f.level == "unsupported" for f in plan.findings)
    if args.command == "check":
        print(render_check(plan))
        return EXIT_UNSUPPORTED if args.strict and unsupported else EXIT_OK
    print(render_check(plan))
    print()
    try:
        result = run_plan(plan, opts, sys.stdout)
    except DockerUnavailable as exc:
        print(f"dryact: error: {exc}", file=sys.stderr)
        return EXIT_NO_DOCKER
    except BackendError as exc:
        print(f"dryact: error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    print(render_summary(result))
    if result.failed:
        return EXIT_STEP_FAILED
    return EXIT_UNSUPPORTED if args.strict and unsupported else EXIT_OK


def _errors_only(wf) -> str:  # type: ignore[no-untyped-def]
    return "\n".join(str(i) for i in wf.issues)


if __name__ == "__main__":
    sys.exit(main())
