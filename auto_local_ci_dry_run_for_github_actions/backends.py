"""Where steps actually execute: a long-lived Docker container per job, or the host shell."""

from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .plan import CONTAINER_WORKSPACE, ResolvedStep

DEFAULT_LINUX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CONTAINER_TMP = "/dryact-tmp"


class BackendError(Exception):
    """The chosen execution backend is unavailable or failed to start."""


class DockerUnavailable(BackendError):
    """Docker is not installed or its daemon is not reachable."""


def shell_command(shell: str, script: str) -> list[str]:
    """Build the argv for a GitHub-style ``shell:`` value ('' means the default shell)."""
    name = shell.split()[0] if shell else "bash"
    if name == "bash":
        # GitHub runs the default as `bash -e`; an explicit `shell: bash` adds pipefail.
        if shell:
            return ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script]
        return ["bash", "-e", "-c", script]
    if name == "sh":
        return ["sh", "-e", "-c", script]
    raise BackendError(f"unsupported shell {shell!r}")


class StepOutcome:
    def __init__(self, code: int, seconds: float, timed_out: bool = False):
        self.code = code
        self.seconds = seconds
        self.timed_out = timed_out


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the step and anything it spawned, so a timed-out `sleep` cannot hold the pipe open."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def _stream(
    argv: list[str], env: dict[str, str] | None, cwd: str | None, prefix: str, out: TextIO,
    timeout: float | None,
) -> StepOutcome:  # fmt: skip
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            start_new_session=sys.platform != "win32",
        )  # fmt: skip
    except OSError as exc:
        raise BackendError(f"cannot start {argv[0]}: {exc}") from None
    timed_out = threading.Event()

    def kill() -> None:
        timed_out.set()
        _kill_tree(proc)

    timer = threading.Timer(timeout, kill) if timeout else None
    if timer:
        timer.start()
    assert proc.stdout is not None
    for line in proc.stdout:
        out.write(f"{prefix}{line.rstrip(chr(10)).rstrip(chr(13))}\n")
        out.flush()
    code = proc.wait()
    if timer:
        timer.cancel()
    return StepOutcome(code, time.monotonic() - started, timed_out.is_set())


class JobSession:
    """Base class: one per job; keeps the GITHUB_ENV / GITHUB_PATH files."""

    def __init__(self, job_id: str, workspace: Path):
        self.job_id = job_id
        self.workspace = workspace
        self.tmp = Path(tempfile.mkdtemp(prefix="dryact-"))
        for name in ("env", "path", "output", "summary"):
            (self.tmp / name).write_text("", encoding="utf-8")
        self.extra_env: dict[str, str] = {}
        self.extra_path: list[str] = []

    # -- paths the step sees ---------------------------------------------------------------
    def file_var(self, name: str) -> str:
        return str(self.tmp / name)

    def base_env(self) -> dict[str, str]:
        return {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "GITHUB_ENV": self.file_var("env"),
            "GITHUB_PATH": self.file_var("path"),
            "GITHUB_OUTPUT": self.file_var("output"),
            "GITHUB_STEP_SUMMARY": self.file_var("summary"),
        }

    def collect_files(self) -> None:
        """Absorb ``echo X=Y >> $GITHUB_ENV`` / ``$GITHUB_PATH`` writes made by the last step."""
        env_file, path_file = self.tmp / "env", self.tmp / "path"
        env_text = env_file.read_text(encoding="utf-8", errors="replace")
        self.extra_env.update(parse_env_file(env_text))
        path_text = path_file.read_text(encoding="utf-8", errors="replace")
        for line in path_text.splitlines():
            if line.strip():
                self.extra_path.insert(0, line.strip())
        env_file.write_text("", encoding="utf-8")
        path_file.write_text("", encoding="utf-8")

    def run(self, step: ResolvedStep, env: dict[str, str], prefix: str, out: TextIO) -> StepOutcome:
        raise NotImplementedError

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def parse_env_file(text: str) -> dict[str, str]:
    """Parse the GITHUB_ENV format: ``NAME=value`` lines and ``NAME<<DELIM`` blocks."""
    result: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            name, _, delim = line.partition("<<")
            block: list[str] = []
            while i < len(lines) and lines[i] != delim:
                block.append(lines[i])
                i += 1
            i += 1
            result[name] = "\n".join(block)
        elif "=" in line:
            name, _, value = line.partition("=")
            if name:
                result[name] = value
    return result


class HostSession(JobSession):
    """Runs steps directly on this machine in the current directory. Not isolated."""

    def __init__(self, job_id: str, workspace: Path):
        super().__init__(job_id, workspace)
        self.bash = find_bash()

    def run(self, step: ResolvedStep, env: dict[str, str], prefix: str, out: TextIO) -> StepOutcome:
        full = {**os.environ, **self.base_env(), **self.extra_env, **env}
        if self.extra_path:
            full["PATH"] = os.pathsep.join([*self.extra_path, full.get("PATH", "")])
        argv = shell_command(step.shell, step.script)
        if argv[0] == "bash":
            argv[0] = self.bash
        cwd = self.workspace / step.workdir if step.workdir else self.workspace
        if not cwd.is_dir():
            out.write(f"{prefix}working directory does not exist: {cwd}\n")
            return StepOutcome(1, 0.0)
        return _stream(argv, full, str(cwd), prefix, out, step.timeout)


def find_bash() -> str:
    if sys.platform == "win32":
        git_dir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git"
        candidates = [git_dir / "bin" / "bash.exe", git_dir / "usr" / "bin" / "bash.exe"]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        found = shutil.which("bash")
        if found and "system32" not in found.lower():
            return found
        raise BackendError("--host needs bash; install Git for Windows (Git Bash) and retry")
    found = shutil.which("bash")
    if not found:
        raise BackendError("--host needs bash on PATH")
    return found


class DockerSession(JobSession):
    """One long-lived container per job so installs persist between steps, like a real runner."""

    def __init__(self, job_id: str, workspace: Path, image: str, docker: str):
        super().__init__(job_id, workspace)
        self.docker = docker
        self.name = f"dryact-{os.getpid()}-{job_id}".replace("_", "-").lower()
        self.image = image

    def file_var(self, name: str) -> str:
        return posixpath.join(CONTAINER_TMP, name)

    def start(self, out: TextIO) -> None:
        argv = [
            self.docker, "run", "-d", "--rm", "--name", self.name,
            "-v", f"{self.workspace}:{CONTAINER_WORKSPACE}",
            "-v", f"{self.tmp}:{CONTAINER_TMP}",
            "-w", CONTAINER_WORKSPACE, self.image, "tail", "-f", "/dev/null",
        ]  # fmt: skip
        out.write(f"   starting container from {self.image} ...\n")
        out.flush()
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise BackendError(
                f"docker could not start {self.image}: {proc.stderr.strip() or proc.stdout.strip()}"
            )

    def run(self, step: ResolvedStep, env: dict[str, str], prefix: str, out: TextIO) -> StepOutcome:
        full = {**self.base_env(), **self.extra_env, **env}
        if self.extra_path:
            full["PATH"] = ":".join([*self.extra_path, DEFAULT_LINUX_PATH])
        workdir = posixpath.join(CONTAINER_WORKSPACE, step.workdir) if step.workdir else None
        argv = [self.docker, "exec"]
        if workdir:
            argv += ["-w", workdir]
        for key, value in full.items():
            argv += ["-e", f"{key}={value}"]
        argv += [self.name, *shell_command(step.shell, step.script)]
        return _stream(argv, None, None, prefix, out, step.timeout)

    def close(self) -> None:
        subprocess.run([self.docker, "rm", "-f", self.name], capture_output=True, check=False)
        super().close()


def check_docker(which: Callable[[str], str | None] = shutil.which) -> str:
    """Return the docker executable, or raise DockerUnavailable with advice."""
    docker = which("docker")
    if not docker:
        raise DockerUnavailable(
            "Docker is not installed or not on PATH. Install Docker, or use --host to run the "
            "steps directly on this machine (no isolation)."
        )
    try:
        info = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=30, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerUnavailable(f"could not run docker: {exc}") from None
    if info.returncode != 0:
        raise DockerUnavailable(
            "Docker is installed but its daemon is not reachable. Start Docker and retry. "
            f"({info.stderr.strip().splitlines()[-1] if info.stderr.strip() else 'no details'})"
        )
    return docker


def open_session(
    job_id: str, workspace: Path, image: str, host: bool, docker: str | None, out: TextIO
) -> JobSession:
    if host:
        return HostSession(job_id, workspace)
    assert docker is not None
    session = DockerSession(job_id, workspace, image, docker)
    try:
        session.start(out)
    except BackendError:
        session.close()
        raise
    return session
