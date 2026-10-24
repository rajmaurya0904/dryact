import shutil
import sys
from pathlib import Path

import pytest

from auto_local_ci_dry_run_for_github_actions.model import Workflow, load_workflow


@pytest.fixture
def make_workflow(tmp_path: Path):
    def make(text: str) -> Workflow:
        path = tmp_path / "wf.yml"
        path.write_text(text, encoding="utf-8")
        return load_workflow(path)

    return make


def _has_bash() -> bool:
    if sys.platform == "win32":
        from auto_local_ci_dry_run_for_github_actions.backends import BackendError, find_bash

        try:
            find_bash()
        except BackendError:
            return False
        return True
    return shutil.which("bash") is not None


needs_bash = pytest.mark.skipif(not _has_bash(), reason="bash is required to run steps on the host")
