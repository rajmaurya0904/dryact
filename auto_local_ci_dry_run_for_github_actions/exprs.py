r"""A deliberately small subset of GitHub's ``${{ }}`` expression language.

Supported: ``env.NAME``, ``secrets.NAME`` (only when passed with ``--secret``), a handful of
``github.*`` and ``runner.*`` values, string/boolean literals, ``==`` / ``!=`` between those,
and the status functions ``success()``, ``failure()``, ``always()``, ``cancelled()``.
Anything else raises :class:`Unsupported` so the caller can skip the step and say why.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

EXPR_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}", re.S)


class Unsupported(Exception):
    """An expression uses something this tool cannot evaluate."""


@dataclass
class Context:
    env: Mapping[str, str] = field(default_factory=dict)
    secrets: Mapping[str, str] = field(default_factory=dict)
    github: Mapping[str, str] = field(default_factory=dict)
    runner: Mapping[str, str] = field(default_factory=dict)
    job_status: str = "success"  # "success" | "failure"


_UNSUPPORTED_ROOTS = {
    "matrix": "matrix values",
    "needs": "outputs from other jobs",
    "steps": "outputs from earlier steps",
    "vars": "repository variables",
    "inputs": "workflow inputs",
    "job": "the job context",
    "strategy": "the strategy context",
}


def _lookup(ref: str, ctx: Context) -> str:
    root, _, rest = ref.partition(".")
    if root in _UNSUPPORTED_ROOTS:
        raise Unsupported(
            f"${{{{ {ref} }}}} needs {_UNSUPPORTED_ROOTS[root]}, which dryact cannot provide"
        )
    if not rest:
        raise Unsupported(f"cannot evaluate '{ref}'")
    if root == "env":
        return ctx.env.get(rest, "")
    if root == "secrets":
        if rest in ctx.secrets:
            return ctx.secrets[rest]
        raise Unsupported(
            f"secret '{rest}' is not available locally (pass --secret {rest} to read it from "
            "your environment)"
        )
    if root == "github":
        if rest in ctx.github:
            return ctx.github[rest]
        raise Unsupported(f"github.{rest} is not available locally")
    if root == "runner":
        if rest in ctx.runner:
            return ctx.runner[rest]
        raise Unsupported(f"runner.{rest} is not available locally")
    raise Unsupported(f"cannot evaluate '{ref}'")


def _operand(text: str, ctx: Context) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+(\.\d+)?|true|false|null", text):
        return text
    if re.fullmatch(r"[A-Za-z_][\w-]*(\.[\w-]+)+", text):
        return _lookup(text, ctx)
    raise Unsupported(f"cannot evaluate '{text}'")


def evaluate(expr: str, ctx: Context) -> str:
    """Evaluate one expression (the part inside ``${{ }}``) to a string."""
    expr = expr.strip()
    for op in ("==", "!="):
        if op in expr:
            left, _, right = expr.partition(op)
            equal = _operand(left, ctx).lower() == _operand(right, ctx).lower()
            return str(equal if op == "==" else not equal).lower()
    return _operand(expr, ctx)


def interpolate(text: str, ctx: Context) -> str:
    """Replace every ``${{ ... }}`` in ``text``; raises Unsupported if any cannot be evaluated."""
    return EXPR_RE.sub(lambda m: evaluate(m.group(1), ctx), text)


def has_expression(text: str) -> bool:
    return EXPR_RE.search(text) is not None


def condition(raw: object, ctx: Context) -> bool:
    """Evaluate a step ``if:`` value. Raises Unsupported for conditions beyond the subset."""
    if raw is None:
        return ctx.job_status == "success"
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip()
    match = EXPR_RE.fullmatch(text)
    if match:
        text = match.group(1).strip()
    negate = False
    while text.startswith("!"):
        negate = not negate
        text = text[1:].strip()
    value = _status_or_value(text, ctx)
    value = (not value) if negate else value
    if not re.search(r"(success|failure|always|cancelled)\(\)", text):
        value = value and ctx.job_status == "success"  # GitHub adds an implicit success()
    return value


def _status_or_value(text: str, ctx: Context) -> bool:
    funcs = {
        "success()": ctx.job_status == "success",
        "failure()": ctx.job_status == "failure",
        "always()": True,
        "cancelled()": False,
    }
    if text in funcs:
        return funcs[text]
    for combiner in ("||", "&&"):
        if combiner in text:
            parts = [_status_or_value(p.strip(), ctx) for p in text.split(combiner)]
            return all(parts) if combiner == "&&" else any(parts)
    value = evaluate(text, ctx)
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value not in ("", "0", "null")
