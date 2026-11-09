"""Terminal tool — execute shell commands.

Supports local execution with timeout and output size limits.
Integrates with the permission system for command deny checking.
"""

import logging
import os
import subprocess
import tempfile
import time
from typing import Any

from nova.tasks import sanitize_environment
from nova.tools.path_safety import path_safety_error
from nova.tools.registry import registry

logger = logging.getLogger(__name__)

# Destructive commands that should be flagged in logs
_DESTRUCTIVE_PATTERNS = [
    "rm -rf",
    "rm -r /",
    "dd if=",
    "mkfs",
    "fdisk",
    "bash -c",
    "sh -c",
    "eval",
    "chmod 777",
    "chown",
    "sudo",
]

# Commands that are suspicious but not destructive (flagged for logging only)
_SUSPICIOUS_PATTERNS = [
    "curl",
    "wget",
]

TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": "Execute a shell command and return its output. Use when no dedicated tool covers the task (system queries, running scripts, package installs). Prefer the dedicated read_file/write_file/patch_file, search_files, or git_* tools when they apply.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum seconds to wait (default: 60).",
                "default": 60,
            },
            "workdir": {
                "type": "string",
                "description": "Working directory for the command.",
            },
        },
        "required": ["command"],
    },
}

_MAX_OUTPUT_CHARS = 8000
_MAX_COMMAND_LENGTH = 10000


def _truncate_output(
    output: str, max_chars: int = _MAX_OUTPUT_CHARS, total_bytes: int | None = None
) -> str:
    """Truncate output to fit within budget.

    total_bytes is the real output size in bytes when the caller only read a
    slice — used to report an accurate truncation count.
    """
    if len(output) <= max_chars:
        if total_bytes is not None and total_bytes > len(output):
            return (
                f"{output}\n\n[...{total_bytes - len(output):,} more bytes of output not shown...]"
            )
        return output
    head = int(max_chars * 0.7)
    tail = int(max_chars * 0.2)
    hidden = (total_bytes - head - tail) if total_bytes is not None else (len(output) - head - tail)
    return f"{output[:head]}\n\n[...{hidden:,} bytes truncated...]\n\n{output[-tail:]}"


def _is_destructive(command: str) -> bool:
    """Check if a command matches destructive patterns."""
    cmd_lower = command.lower()
    return any(pattern in cmd_lower for pattern in _DESTRUCTIVE_PATTERNS)


def execute_terminal(args: dict[str, Any], **kwargs: Any) -> str:
    """Execute a terminal command."""
    command = args.get("command", "")
    timeout = args.get("timeout", 60)
    workdir = args.get("workdir")

    if not isinstance(command, str) or not command:
        return "Error: No command provided."

    # Validate command length
    if len(command) > _MAX_COMMAND_LENGTH:
        return f"Error: Command too long (max {_MAX_COMMAND_LENGTH} chars)."

    # Validate timeout range
    if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 3600:
        return "Error: Timeout must be between 1 and 3600 seconds."

    # Validate workdir if provided
    if workdir is not None:
        from pathlib import Path as _Path

        wd = _Path(workdir).expanduser()
        if not wd.exists():
            return f"Error: Working directory not found: {workdir}"
        if not wd.is_dir():
            return f"Error: Working directory is not a directory: {workdir}"
        if error := path_safety_error(wd, **kwargs):
            return error
        workdir = str(wd)

    # Permission check — denied commands
    config = kwargs.get("config")
    if config:
        from nova.permissions import build_permission_checker

        checker = build_permission_checker(config)
        perm_result = checker.evaluate("terminal", command=command)
        if not perm_result.allowed:
            logger.warning("Terminal command denied: %s", perm_result.reason)
            return f"Error: {perm_result.reason}"

    # Log with destructive flag
    destructive_flag = " [DESTRUCTIVE]" if _is_destructive(command) else ""
    logger.info("Executing%s: %s", destructive_flag, command[:200])

    try:
        with tempfile.TemporaryFile() as output_file:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                cwd=workdir,
                env=sanitize_environment(),
                start_new_session=True,
            )
            try:
                control = getattr(kwargs.get("agent"), "invocation_control", None)
                if control is None:
                    process.wait(timeout=timeout)
                else:
                    deadline = time.monotonic() + timeout
                    while process.poll() is None:
                        control.checkpoint()
                        remaining = min(deadline - time.monotonic(), control.remaining())
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, timeout)
                        try:
                            process.wait(timeout=min(0.05, remaining))
                        except subprocess.TimeoutExpired:
                            continue
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), 9)
                except OSError:
                    process.kill()
                process.wait()
                return f"Error: Command timed out after {timeout}s."
            finally:
                if getattr(kwargs.get("agent"), "invocation_control", None) is not None:
                    try:
                        os.killpg(process.pid, 9)
                    except ProcessLookupError:
                        pass
                    finally:
                        process.wait()
            output_file.seek(0)
            raw = output_file.read(_MAX_OUTPUT_CHARS * 4)
            try:
                total_bytes = os.path.getsize(output_file.name)
            except OSError:
                total_bytes = None
        output = raw.decode("utf-8", errors="replace")

        if not output:
            return f"exit code: {process.returncode}\n(no output)"
        return (
            f"exit code: {process.returncode}\n{_truncate_output(output, total_bytes=total_bytes)}"
        )
    except Exception as e:
        return f"Error: {e}"


registry.register(
    name="terminal",
    toolset="terminal",
    schema=TERMINAL_SCHEMA,
    handler=execute_terminal,
    emoji="💻",
)
