"""Smoke test: package imports cleanly. Replace/extend as modules land."""

import auto_local_ci_dry_run_for_github_actions


def test_version_is_set() -> None:
    assert auto_local_ci_dry_run_for_github_actions.__version__
