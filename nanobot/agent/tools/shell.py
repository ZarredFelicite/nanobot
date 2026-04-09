"""Shell execution tool with background tasks, progress tracking, and safety guards."""

import asyncio
import os
import re
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.security.prompt_injection import wrap_untrusted_content

# ---------------------------------------------------------------------------
# Semantic exit-code interpretation for common CLI tools
# ---------------------------------------------------------------------------
_EXIT_CODE_MEANINGS: dict[str, dict[int, str]] = {
    "grep": {1: "no matches found", 2: "error"},
    "rg": {1: "no matches found", 2: "error"},
    "ag": {1: "no matches found"},
    "ack": {1: "no matches found"},
    "diff": {1: "files differ", 2: "error"},
    "cmp": {1: "files differ", 2: "error"},
    "test": {1: "condition is false"},
    "curl": {6: "could not resolve host", 7: "failed to connect", 22: "HTTP error", 28: "timeout"},
    "wget": {4: "network failure", 5: "SSL error", 8: "server error"},
    "git": {1: "command failed or nothing to do", 128: "fatal error"},
    "fd": {1: "no matches found"},
    "find": {1: "error in expression or path"},
    "ssh": {255: "connection failed"},
    "make": {2: "error in makefile"},
    "python": {1: "exception raised", 2: "invalid usage"},
    "python3": {1: "exception raised", 2: "invalid usage"},
    "node": {1: "exception raised"},
    "cargo": {101: "test failures"},
    "pytest": {1: "tests failed", 2: "interrupted", 3: "internal error", 4: "usage error", 5: "no tests collected"},
    "go": {1: "build/test failed", 2: "usage error"},
}


def _interpret_exit_code(command: str, code: int) -> str | None:
    """Return a human-readable interpretation of a non-zero exit code, or None."""
    if code == 0:
        return None
    # Extract the base command (first word, ignore env/sudo/pkexec prefixes)
    parts = command.strip().split()
    skip = {"env", "sudo", "pkexec", "nix-shell", "nix", "run", "timeout"}
    base = None
    for p in parts:
        if p.startswith("-"):
            continue
        if p in skip:
            continue
        base = Path(p).name
        break
    if base and base in _EXIT_CODE_MEANINGS:
        meaning = _EXIT_CODE_MEANINGS[base].get(code)
        if meaning:
            return meaning
    # Generic signals
    if code < 0 or code >= 128:
        sig_num = code - 128 if code >= 128 else -code
        try:
            sig_name = signal.Signals(sig_num).name
            return f"killed by {sig_name}"
        except (ValueError, AttributeError):
            pass
    return None


# ---------------------------------------------------------------------------
# Background task tracker
# ---------------------------------------------------------------------------
class _BackgroundTask:
    """Tracks a single background shell process."""

    __slots__ = ("task_id", "command", "process", "output_path", "start_time", "_done")

    def __init__(self, task_id: str, command: str, process: asyncio.subprocess.Process,
                 output_path: Path, start_time: float):
        self.task_id = task_id
        self.command = command
        self.process = process
        self.output_path = output_path
        self.start_time = start_time
        self._done = False

    @property
    def is_done(self) -> bool:
        if self._done:
            return True
        if self.process.returncode is not None:
            self._done = True
        return self._done

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def summary(self) -> str:
        status = "done" if self.is_done else "running"
        rc = self.process.returncode
        rc_str = f", exit={rc}" if rc is not None else ""
        return (
            f"[{self.task_id}] {status}{rc_str}  "
            f"elapsed={self.elapsed:.1f}s  cmd={self.command!r}  "
            f"output={self.output_path}"
        )


# Module-level registry so background tasks survive across tool calls
_background_tasks: dict[str, _BackgroundTask] = {}


class ExecTool(Tool):
    """Tool to execute shell commands with background support and progress tracking."""

    _MAX_TIMEOUT_S = 3600
    _OUTPUT_PERSIST_THRESHOLD = 30_000   # chars before persisting to disk
    _OUTPUT_MAX_BYTES = 64 * 1024 * 1024  # 64 MB hard cap on captured output
    _PROGRESS_INTERVAL_S = 2.0            # how often to log progress for long commands
    _PROGRESS_START_AFTER_S = 3.0         # don't report progress for fast commands

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        untrusted_programs: list[str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",
            r"\bdel\s+/[fq]\b",
            r"\brmdir\s+/s\b",
            r"(?:^|[;&|]\s*)format\b",
            r"\b(mkfs|diskpart)\b",
            r"\bdd\s+if=",
            r">\s*/dev/sd",
            r"\b(shutdown|reboot|poweroff)\b",
            r":\(\)\s*\{.*\};\s*:",  # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self.untrusted_programs = [
            p.strip().lower() for p in (untrusted_programs or []) if p.strip()
        ]

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command. Supports run_in_background for long-running "
            "commands and bg_task_status to check on background tasks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "description": {
                    "type": "string",
                    "description": "Brief human-readable summary of what this command does",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"Optional timeout in seconds "
                        f"(default {self.timeout}, max {self._MAX_TIMEOUT_S})"
                    ),
                    "minimum": 1,
                    "maximum": self._MAX_TIMEOUT_S,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Run the command as a background task. Returns a task ID "
                        "immediately; use bg_task_status to check results later."
                    ),
                },
                "bg_task_status": {
                    "type": "string",
                    "description": (
                        "Check status of a background task by ID. "
                        "Pass 'all' to list all background tasks."
                    ),
                },
            },
            "required": ["command", "description"],
        }

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    async def execute(self, **kwargs: Any) -> str:
        # Handle bg_task_status queries (command/description can be placeholders)
        bg_status = kwargs.get("bg_task_status")
        if bg_status:
            return self._check_background_task(bg_status)

        command = str(kwargs.get("command", ""))
        working_dir = kwargs.get("working_dir")
        timeout = kwargs.get("timeout")
        run_bg = kwargs.get("run_in_background", False)
        cwd = working_dir or self.working_dir or os.getcwd()

        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        effective_timeout = self.timeout if timeout is None else timeout

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        if run_bg:
            return await self._run_background(command, cwd, env)

        return await self._run_foreground(command, cwd, env, effective_timeout)

    # ------------------------------------------------------------------
    # Foreground execution with progress tracking
    # ------------------------------------------------------------------
    async def _run_foreground(self, command: str, cwd: str, env: dict,
                              timeout: int) -> str:
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                preexec_fn=os.setsid,  # new process group for clean kills
            )

            start = time.monotonic()
            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []
            total_bytes = 0
            last_progress = start

            try:
                # Read stdout/stderr concurrently with progress reporting
                async def _read_stream(stream: asyncio.StreamReader | None,
                                       dest: list[bytes]) -> None:
                    nonlocal total_bytes, last_progress
                    if stream is None:
                        return
                    while True:
                        chunk = await stream.read(8192)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes <= self._OUTPUT_MAX_BYTES:
                            dest.append(chunk)
                        now = time.monotonic()
                        elapsed = now - start
                        if (elapsed > self._PROGRESS_START_AFTER_S
                                and now - last_progress >= self._PROGRESS_INTERVAL_S):
                            last_progress = now
                            logger.info(
                                f"exec progress: {elapsed:.0f}s elapsed, "
                                f"{total_bytes:,} bytes captured  cmd={command[:60]}"
                            )

                read_task = asyncio.gather(
                    _read_stream(process.stdout, stdout_chunks),
                    _read_stream(process.stderr, stderr_chunks),
                )
                await asyncio.wait_for(read_task, timeout=timeout)
                await process.wait()

            except asyncio.TimeoutError:
                self._kill_process_group(process)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return f"Error: Command timed out after {timeout} seconds"

            stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
            stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")

            return self._format_result(command, process.returncode or 0,
                                       stdout, stderr, total_bytes)

        except Exception as e:
            return f"Error executing command: {e}"

    # ------------------------------------------------------------------
    # Background execution
    # ------------------------------------------------------------------
    async def _run_background(self, command: str, cwd: str, env: dict) -> str:
        task_id = uuid.uuid4().hex[:8]
        output_dir = Path(self.working_dir or cwd) / ".exec_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"bg_{task_id}.log"

        out_fd = open(output_path, "wb")
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=out_fd,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=env,
                preexec_fn=os.setsid,
            )
        except Exception as e:
            out_fd.close()
            return f"Error spawning background task: {e}"

        # Close our fd copy — the subprocess has its own
        out_fd.close()

        task = _BackgroundTask(
            task_id=task_id,
            command=command,
            process=process,
            output_path=output_path,
            start_time=time.monotonic(),
        )
        _background_tasks[task_id] = task
        logger.info(f"Background task {task_id} started: {command[:80]}")

        return (
            f"Background task started.\n"
            f"  task_id: {task_id}\n"
            f"  output:  {output_path}\n"
            f"Use bg_task_status='{task_id}' to check progress."
        )

    def _check_background_task(self, task_id: str) -> str:
        if task_id == "all":
            if not _background_tasks:
                return "No background tasks."
            return "\n".join(t.summary() for t in _background_tasks.values())

        task = _background_tasks.get(task_id)
        if not task:
            return f"No background task with id '{task_id}'"

        lines: list[str] = [task.summary()]

        # If done, read tail of output
        if task.is_done and task.output_path.exists():
            try:
                raw = task.output_path.read_text(errors="replace")
                if len(raw) > 4000:
                    raw = f"... (truncated, showing last 4000 chars)\n{raw[-4000:]}"
                lines.append(f"\n--- output ---\n{raw}")
            except OSError:
                lines.append("(could not read output file)")
        elif not task.is_done:
            # Show tail of in-progress output
            try:
                size = task.output_path.stat().st_size
                lines.append(f"Output so far: {size:,} bytes")
                if size > 0:
                    tail = task.output_path.read_bytes()[-2000:]
                    lines.append(f"\n--- tail ---\n{tail.decode('utf-8', errors='replace')}")
            except OSError:
                pass

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Result formatting & persistence
    # ------------------------------------------------------------------
    def _format_result(self, command: str, returncode: int,
                       stdout: str, stderr: str, total_bytes: int) -> str:
        output_parts: list[str] = []

        if stdout:
            output_parts.append(stdout)
        if stderr and stderr.strip():
            output_parts.append(f"STDERR:\n{stderr}")

        if returncode != 0:
            interpretation = _interpret_exit_code(command, returncode)
            if interpretation:
                output_parts.append(f"\nExit code: {returncode} ({interpretation})")
            else:
                output_parts.append(f"\nExit code: {returncode}")

        result = "\n".join(output_parts) if output_parts else "(no output)"

        # Persist large output to disk instead of truncating
        if len(result) > self._OUTPUT_PERSIST_THRESHOLD:
            return self._persist_large_output(command, result, total_bytes)

        if self._command_matches_untrusted_program(command):
            return wrap_untrusted_content(result, source="exec output", sanitize=True)
        return result

    def _persist_large_output(self, command: str, result: str,
                              total_bytes: int) -> str:
        output_dir = Path(self.working_dir or os.getcwd()) / ".exec_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"out_{uuid.uuid4().hex[:8]}.log"
        try:
            out_path.write_text(result, errors="replace")
        except OSError as e:
            # Fall back to truncation if we can't write
            truncated = result[:10000]
            return truncated + f"\n... (truncated; failed to persist: {e})"

        # Return a preview (first + last lines) plus the path
        preview_head = result[:3000]
        preview_tail = result[-1000:] if len(result) > 4000 else ""
        parts = [
            f"Output too large ({total_bytes:,} bytes) — persisted to {out_path}",
            f"\n--- first 3000 chars ---\n{preview_head}",
        ]
        if preview_tail:
            parts.append(f"\n--- last 1000 chars ---\n{preview_tail}")

        full = "\n".join(parts)
        if self._command_matches_untrusted_program(command):
            return wrap_untrusted_content(full, source="exec output", sanitize=True)
        return full

    # ------------------------------------------------------------------
    # Process group kill
    # ------------------------------------------------------------------
    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        """Kill the entire process group to clean up child processes."""
        pid = process.pid
        if pid is None:
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        # Give processes a moment then force-kill
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # ------------------------------------------------------------------
    # Safety guards
    # ------------------------------------------------------------------
    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        # Check deny patterns
        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        # Check allow patterns (if set, command must match at least one)
        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        # Block long sleeps (>=10s) — use run_in_background instead
        sleep_match = re.search(r"\bsleep\s+(\d+(?:\.\d+)?)", lower)
        if sleep_match:
            sleep_secs = float(sleep_match.group(1))
            if sleep_secs >= 10:
                return (
                    f"Error: sleep {sleep_match.group(1)} blocked — "
                    f"use run_in_background=true for long-running commands"
                )

        # Workspace restriction checks
        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()
            for raw in self._extract_absolute_paths(cmd):
                try:
                    p = Path(raw.strip()).resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)
        posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", command)
        return win_paths + posix_paths

    def _command_matches_untrusted_program(self, command: str) -> bool:
        lower = command.lower()
        return any(program in lower for program in self.untrusted_programs)
