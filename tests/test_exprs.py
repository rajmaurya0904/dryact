import pytest

from auto_local_ci_dry_run_for_github_actions.exprs import (
    Context,
    Unsupported,
    condition,
    evaluate,
    has_expression,
    interpolate,
)

CTX = Context(
    env={"NAME": "world"},
    secrets={"TOKEN": "s3cret"},
    github={"ref_name": "main", "event_name": "push"},
    runner={"os": "Linux"},
)


def test_interpolate_env_github_runner_and_secrets() -> None:
    text = "hi ${{ env.NAME }} on ${{ github.ref_name }}/${{runner.os}} ${{ secrets.TOKEN }}"
    assert interpolate(text, CTX) == "hi world on main/Linux s3cret"


def test_missing_env_is_empty_like_github() -> None:
    assert interpolate("[${{ env.NOPE }}]", CTX) == "[]"


@pytest.mark.parametrize(
    "expr,fragment",
    [
        ("matrix.os", "matrix values"),
        ("needs.build.outputs.x", "outputs from other jobs"),
        ("steps.a.outputs.b", "earlier steps"),
        ("vars.FOO", "repository variables"),
        ("secrets.MISSING", "--secret MISSING"),
        ("github.nothing", "github.nothing"),
        ("contains(github.ref, 'x')", "cannot evaluate"),
    ],
)
def test_unsupported_expressions_say_why(expr: str, fragment: str) -> None:
    with pytest.raises(Unsupported) as exc:
        interpolate("${{ " + expr + " }}", CTX)
    assert fragment in str(exc.value)


def test_comparisons() -> None:
    assert evaluate("github.event_name == 'push'", CTX) == "true"
    assert evaluate("github.ref_name != 'main'", CTX) == "false"
    assert evaluate("runner.os == 'linux'", CTX) == "true"  # case-insensitive like GitHub


def test_has_expression() -> None:
    assert has_expression("a ${{ b }} c") and not has_expression("plain")


def test_condition_defaults_and_status_functions() -> None:
    ok, bad = Context(job_status="success"), Context(job_status="failure")
    assert condition(None, ok) and not condition(None, bad)
    assert condition("always()", bad) and condition("failure()", bad)
    assert not condition("failure()", ok)
    assert condition("success()", ok) and not condition("cancelled()", ok)
    assert condition("${{ !cancelled() }}", ok)


def test_condition_literals_and_booleans() -> None:
    assert condition(True, CTX) and not condition(False, CTX)
    assert not condition("false", CTX)
    assert condition("github.event_name == 'push'", CTX)
    assert not condition("github.event_name == 'pull_request'", CTX)


def test_condition_and_or_precedence() -> None:
    assert condition("github.ref_name == 'x' || github.ref_name == 'main' && always()", CTX)
    assert not condition("github.ref_name == 'x' && always()", CTX)


def test_implicit_success_is_added_unless_a_status_function_is_used() -> None:
    bad = Context(github={"event_name": "push"}, job_status="failure")
    assert not condition("github.event_name == 'push'", bad)
    assert condition("github.event_name == 'push' && always()", bad)


def test_condition_with_unsupported_value_raises() -> None:
    with pytest.raises(Unsupported):
        condition("matrix.os == 'linux'", CTX)
