"""Worktree Delegate 的严格命令执行后端。

首版只提供 Docker Runner。所有 Docker 与容器参数都由本模块生成，模型只能
提供容器内执行的命令正文，不能控制挂载、网络、用户或镜像。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol


_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


class WorkspaceRunnerError(RuntimeError):
    """Runner 配置或启动门禁失败。"""

    def __init__(self, reason_code: str, message: str = ""):
        self.reason_code = reason_code
        super().__init__(message or reason_code)


@dataclass(frozen=True)
class RunnerProbe:
    backend: str
    image_digest: str


@dataclass(frozen=True)
class WorkspaceCommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    termination_reason: str = "spawn_error"
    error_code: str | None = None


class WorkspaceCommandRunner(Protocol):
    backend: str

    def probe(self) -> RunnerProbe:
        ...

    def run(
        self,
        *,
        workspace_id: str,
        workspace_root: Path,
        task_temp_root: Path,
        command: str,
        cwd_relative: str = ".",
        timeout: float = 30.0,
        cancel_check: Callable[[], bool] | None = None,
    ) -> WorkspaceCommandResult:
        ...

    def verify_workspace(
        self,
        *,
        workspace_id: str,
        workspace_root: Path,
        task_temp_root: Path,
    ) -> None:
        ...

    def has_active_processes(self, workspace_id: str) -> bool:
        ...


class DockerWorkspaceCommandRunner:
    """用固定安全参数启动一次性 Docker 容器。"""

    backend = "docker"

    def __init__(
        self,
        image: str,
        *,
        docker_executable: str = "docker",
        container_user: str = "65532:65532",
        pids_limit: int = 256,
        memory_limit: str = "1g",
        probe_timeout_seconds: float = 15.0,
    ):
        if not isinstance(image, str) or not image.strip():
            raise WorkspaceRunnerError(
                "docker_image_required", "worktree.docker_image is not configured"
            )
        self.image = image.strip()
        self.docker_executable = docker_executable
        self.container_user = self._validate_user(container_user)
        self.pids_limit = min(max(int(pids_limit), 16), 4096)
        if not isinstance(memory_limit, str) or not re.fullmatch(
            r"[1-9][0-9]*(?:[kKmMgG])?", memory_limit.strip()
        ):
            raise WorkspaceRunnerError("invalid_memory_limit")
        self.memory_limit = memory_limit.strip().lower()
        self.probe_timeout_seconds = min(
            max(float(probe_timeout_seconds), 1.0), 120.0
        )
        self._probe_result: RunnerProbe | None = None
        self._active_lock = threading.Lock()
        self._active_containers: set[str] = set()

    @staticmethod
    def _validate_user(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+:[0-9]+", value):
            raise WorkspaceRunnerError("invalid_container_user")
        uid, _ = value.split(":", 1)
        if int(uid) == 0:
            raise WorkspaceRunnerError(
                "root_container_user_forbidden", "Docker Runner must use a non-root uid"
            )
        return value

    def probe(self) -> RunnerProbe:
        if self._probe_result is not None:
            return self._probe_result
        try:
            completed = subprocess.run(
                [
                    self.docker_executable,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    self.image,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.probe_timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise WorkspaceRunnerError(
                "docker_unavailable", "Docker CLI was not found"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceRunnerError(
                "docker_probe_timed_out", "Docker image inspection timed out"
            ) from exc
        except OSError as exc:
            raise WorkspaceRunnerError("docker_unavailable", str(exc)[:300]) from exc
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout).strip()
            raise WorkspaceRunnerError(
                "docker_image_unavailable",
                message[:300] or "configured Docker image is not available locally",
            )
        digest = completed.stdout.strip().lower()
        if not _IMAGE_ID.fullmatch(digest):
            raise WorkspaceRunnerError(
                "docker_image_identity_invalid",
                "Docker did not return an immutable sha256 image id",
            )
        self._probe_result = RunnerProbe(self.backend, digest)
        return self._probe_result

    def build_command(
        self,
        *,
        workspace_id: str,
        workspace_root: Path,
        task_temp_root: Path,
        command: str,
        cwd_relative: str = ".",
        container_name: str | None = None,
    ) -> tuple[list[str], str]:
        """生成可测试的结构化 Docker argv，不执行 Shell 拼接。"""
        probe = self.probe()
        workspace = workspace_root.resolve(strict=True)
        task_temp = task_temp_root.resolve(strict=True)
        if not workspace.is_dir() or not task_temp.is_dir():
            raise WorkspaceRunnerError("workspace_mount_unavailable")
        if "," in str(workspace) or "," in str(task_temp):
            raise WorkspaceRunnerError(
                "docker_mount_path_unsupported", "Docker --mount source contains a comma"
            )
        relative = _validate_container_cwd(cwd_relative)
        sentinel = task_temp / "git-sentinel"
        if not sentinel.is_file() or sentinel.is_symlink():
            raise WorkspaceRunnerError("git_sentinel_unavailable")
        home = task_temp / "home"
        tmp = task_temp / "tmp"
        if not home.is_dir() or not tmp.is_dir():
            raise WorkspaceRunnerError("task_temp_unavailable")

        name = container_name or f"minihermes-{workspace_id[:24]}-{uuid.uuid4().hex[:12]}"
        if not _CONTAINER_NAME.fullmatch(name):
            raise WorkspaceRunnerError("invalid_container_name")
        workdir = "/workspace"
        if relative != ".":
            workdir += "/" + relative
        argv = [
            self.docker_executable,
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--user",
            self.container_user,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory_limit,
            "--workdir",
            workdir,
            "--mount",
            f"type=bind,source={workspace},target=/workspace",
            "--mount",
            f"type=bind,source={task_temp},target=/tmp/minihermes",
            "--mount",
            f"type=bind,source={sentinel},target=/workspace/.git,readonly",
            "--env",
            "HOME=/tmp/minihermes/home",
            "--env",
            "TMPDIR=/tmp/minihermes/tmp",
            probe.image_digest,
            "/bin/sh",
            "-lc",
            command,
        ]
        return argv, name

    def run(
        self,
        *,
        workspace_id: str,
        workspace_root: Path,
        task_temp_root: Path,
        command: str,
        cwd_relative: str = ".",
        timeout: float = 30.0,
        cancel_check: Callable[[], bool] | None = None,
    ) -> WorkspaceCommandResult:
        timeout = max(0.01, float(timeout))
        try:
            argv, container_name = self.build_command(
                workspace_id=workspace_id,
                workspace_root=workspace_root,
                task_temp_root=task_temp_root,
                command=command,
                cwd_relative=cwd_relative,
            )
        except WorkspaceRunnerError as exc:
            return WorkspaceCommandResult(
                stderr=str(exc), error_code=exc.reason_code
            )

        stdout_file = tempfile.TemporaryFile(
            mode="w+t", encoding="utf-8", errors="replace"
        )
        stderr_file = tempfile.TemporaryFile(
            mode="w+t", encoding="utf-8", errors="replace"
        )
        process: subprocess.Popen | None = None
        reason = "spawn_error"
        exit_code = None
        error_code = None
        with self._active_lock:
            self._active_containers.add(container_name)
        try:
            process = subprocess.Popen(
                argv,
                shell=False,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                start_new_session=(os.name != "nt"),
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
            started = time.monotonic()
            while True:
                if cancel_check and cancel_check():
                    self._stop_container(container_name, process)
                    reason = "cancelled"
                    error_code = "cancelled"
                    break
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    self._stop_container(container_name, process)
                    reason = "timed_out"
                    error_code = "timeout"
                    break
                try:
                    process.wait(timeout=min(0.1, remaining))
                    exit_code = process.returncode
                    reason = "exited"
                    break
                except subprocess.TimeoutExpired:
                    continue
        except FileNotFoundError as exc:
            error_code = "docker_unavailable"
            stderr_file.write(str(exc))
        except OSError as exc:
            error_code = "docker_spawn_failed"
            stderr_file.write(f"{type(exc).__name__}: {exc}")
        finally:
            if process is not None and process.poll() is None:
                self._stop_container(container_name, process)
            with self._active_lock:
                self._active_containers.discard(container_name)
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
            stdout_file.close()
            stderr_file.close()
        return WorkspaceCommandResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            termination_reason=reason,
            error_code=error_code,
        )

    def verify_workspace(
        self,
        *,
        workspace_id: str,
        workspace_root: Path,
        task_temp_root: Path,
    ) -> None:
        """用真实严格容器确认非 root 身份和两个可写挂载。"""
        probe_name = f".minihermes-runner-probe-{uuid.uuid4().hex}"
        command = (
            "set -eu; "
            "test \"$(id -u)\" -ne 0; "
            "test -f /workspace/.git; test ! -d /workspace/.git; "
            f"workspace_probe=/workspace/{probe_name}; "
            f"temp_probe=/tmp/minihermes/{probe_name}; "
            ": > \"$workspace_probe\"; : > \"$temp_probe\"; "
            "rm -f \"$workspace_probe\" \"$temp_probe\""
        )
        result = self.run(
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            task_temp_root=task_temp_root,
            command=command,
            timeout=self.probe_timeout_seconds,
        )
        if (
            result.error_code
            or result.exit_code != 0
            or result.termination_reason != "exited"
        ):
            detail = (result.stderr or result.stdout or result.error_code or "").strip()
            raise WorkspaceRunnerError(
                "runner_workspace_probe_failed",
                detail[:300] or "strict Docker workspace probe failed",
            )

    def has_active_processes(self, workspace_id: str) -> bool:
        """同时检查本进程记录和 Docker 中遗留的同工作区容器。"""
        if not isinstance(workspace_id, str) or not workspace_id:
            raise WorkspaceRunnerError("runner_state_unavailable")
        prefix = f"minihermes-{workspace_id[:24]}-"
        with self._active_lock:
            if any(name.startswith(prefix) for name in self._active_containers):
                return True
        try:
            completed = subprocess.run(
                [
                    self.docker_executable,
                    "ps",
                    "--filter",
                    f"name={prefix}",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.probe_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceRunnerError(
                "runner_state_unavailable", str(exc)[:300]
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise WorkspaceRunnerError(
                "runner_state_unavailable",
                detail[:300] or "Docker container state query failed",
            )
        return any(
            name.strip().startswith(prefix)
            for name in completed.stdout.splitlines()
            if name.strip()
        )

    def _stop_container(self, container_name: str, process: subprocess.Popen) -> None:
        try:
            subprocess.run(
                [self.docker_executable, "rm", "-f", container_name],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        _terminate_process(process)


def _validate_container_cwd(value: str) -> str:
    if value == ".":
        return value
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise WorkspaceRunnerError("invalid_container_cwd")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise WorkspaceRunnerError("invalid_container_cwd")
    return path.as_posix()


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
