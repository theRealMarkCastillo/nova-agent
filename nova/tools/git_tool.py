"""Git tool — common git operations.

Supports status, log, diff, blame, branch, and commit inspection.
Integrates with permission system to prevent destructive operations.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from nova.tools.path_safety import path_safety_error
from nova.tools.registry import registry

logger = logging.getLogger(__name__)

GIT_STATUS_SCHEMA = {
    "name": "git_status",
    "description": "Get git status (staged, unstaged, untracked files).",
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "Path to git repository (default: current directory).",
                "default": ".",
            },
        },
        "required": [],
    },
}

GIT_LOG_SCHEMA = {
    "name": "git_log",
    "description": "Show git commit history with one-line summaries.",
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "Path to git repository (default: current directory).",
                "default": ".",
            },
            "limit": {
                "type": "integer",
                "description": "Number of commits to show (default: 20, max: 100).",
                "default": 20,
            },
        },
        "required": [],
    },
}

GIT_DIFF_SCHEMA = {
    "name": "git_diff",
    "description": "Show unstaged changes (git diff) or staged changes (git diff --cached).",
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "Path to git repository (default: current directory).",
                "default": ".",
            },
            "staged": {
                "type": "boolean",
                "description": "Show staged changes only (default: false, shows unstaged).",
                "default": False,
            },
            "file_path": {
                "type": "string",
                "description": "Optional file path to limit diff to that file.",
            },
        },
        "required": [],
    },
}

GIT_BLAME_SCHEMA = {
    "name": "git_blame",
    "description": "Show blame (commit + author) for each line in a file.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to file relative to repo root.",
            },
            "repo": {
                "type": "string",
                "description": "Path to git repository (default: current directory).",
                "default": ".",
            },
        },
        "required": ["file_path"],
    },
}

GIT_SHOW_SCHEMA = {
    "name": "git_show",
    "description": "Show a specific commit or file version.",
    "parameters": {
        "type": "object",
        "properties": {
            "rev": {
                "type": "string",
                "description": "Commit hash, branch, tag, or HEAD~N (e.g., 'HEAD', 'abc1234', 'main~2').",
            },
            "file_path": {
                "type": "string",
                "description": "Optional file path to show version in that commit.",
            },
            "repo": {
                "type": "string",
                "description": "Path to git repository (default: current directory).",
                "default": ".",
            },
        },
        "required": ["rev"],
    },
}

_MAX_OUTPUT_CHARS = 8000


def _reject_option(value: Any, name: str) -> str | None:
    """Reject values that could be interpreted as git options."""
    if not isinstance(value, str) or value.startswith("-"):
        return f"Error: Invalid {name}."
    return None


def _truncate_output(output: str, max_chars: int = _MAX_OUTPUT_CHARS) -> str:
    """Truncate output to fit within budget."""
    if len(output) <= max_chars:
        return output
    head = int(max_chars * 0.7)
    tail = int(max_chars * 0.2)
    return (
        f"{output[:head]}\n\n"
        f"[...{len(output) - head - tail:,} chars truncated...]\n\n"
        f"{output[-tail:]}"
    )


def _run_git_command(repo: str, *args: str, **kwargs: Any) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    repo_path = Path(repo).expanduser()
    if _reject_option(repo, "repository"):
        raise ValueError("Invalid repository")
    if not repo_path.exists() or not repo_path.is_dir():
        raise ValueError(f"Repository not found: {repo}")
    if error := path_safety_error(repo_path, **kwargs):
        raise ValueError(error)

    cmd = ["git"] + list(args)
    logger.info("Running git command: %s (in %s)", " ".join(cmd), repo_path)
    secret_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in secret_markers)
    }

    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30.0,
            env=env,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as e:
        raise TimeoutError("Git command timed out after 30s") from e


def _git_status(args: dict[str, Any], **kwargs: Any) -> str:
    """Handler for git_status."""
    repo = args.get("repo", ".")

    try:
        returncode, stdout, stderr = _run_git_command(repo, "status", "--short", **kwargs)
        if returncode != 0:
            return f"Error: {stderr.strip()}"
        if not stdout.strip():
            return "Working tree is clean."
        return _truncate_output(stdout)
    except Exception as e:
        return f"Error: {e}"


def _git_log(args: dict[str, Any], **kwargs: Any) -> str:
    """Handler for git_log."""
    repo = args.get("repo", ".")
    limit = min(int(args.get("limit", 20)), 100)

    try:
        returncode, stdout, stderr = _run_git_command(
            repo, "log", "--oneline", f"-{limit}", **kwargs
        )
        if returncode != 0:
            return f"Error: {stderr.strip()}"
        if not stdout.strip():
            return "No commits found."
        return _truncate_output(stdout)
    except Exception as e:
        return f"Error: {e}"


def _git_diff(args: dict[str, Any], **kwargs: Any) -> str:
    """Handler for git_diff."""
    repo = args.get("repo", ".")
    staged = bool(args.get("staged", False))
    file_path = args.get("file_path")

    try:
        cmd = ["diff"]
        if staged:
            cmd.append("--cached")
        if file_path:
            if error := _reject_option(file_path, "file_path"):
                return error
            if error := path_safety_error(Path(repo) / file_path, **kwargs):
                return error
            cmd.extend(("--", file_path))

        returncode, stdout, stderr = _run_git_command(repo, *cmd, **kwargs)
        if returncode != 0:
            return f"Error: {stderr.strip()}"
        if not stdout.strip():
            return "No differences found."
        return _truncate_output(stdout)
    except Exception as e:
        return f"Error: {e}"


def _git_blame(args: dict[str, Any], **kwargs: Any) -> str:
    """Handler for git_blame."""
    repo = args.get("repo", ".")
    file_path = args.get("file_path", "")

    if not file_path:
        return "Error: file_path is required."
    if error := _reject_option(file_path, "file_path"):
        return error
    if error := path_safety_error(Path(repo) / file_path, **kwargs):
        return error

    try:
        returncode, stdout, stderr = _run_git_command(repo, "blame", "--", file_path, **kwargs)
        if returncode != 0:
            return f"Error: {stderr.strip()}"
        return _truncate_output(stdout)
    except Exception as e:
        return f"Error: {e}"


def _git_show(args: dict[str, Any], **kwargs: Any) -> str:
    """Handler for git_show."""
    repo = args.get("repo", ".")
    rev = args.get("rev", "").strip()
    file_path = args.get("file_path")

    if not rev:
        return "Error: rev is required."
    if error := _reject_option(rev, "rev"):
        return error
    if not file_path and ":" in rev:
        revision, revision_path = rev.split(":", 1)
        if not revision or not revision_path:
            return "Error: Invalid rev."
        if error := path_safety_error(Path(repo) / revision_path, **kwargs):
            return error
    if file_path and (error := _reject_option(file_path, "file_path")):
        return error
    if file_path and (error := path_safety_error(Path(repo) / file_path, **kwargs)):
        return error

    try:
        spec = f"{rev}:{file_path}" if file_path else rev
        returncode, stdout, stderr = _run_git_command(repo, "show", spec, **kwargs)
        if returncode != 0:
            return f"Error: {stderr.strip()}"
        return _truncate_output(stdout)
    except Exception as e:
        return f"Error: {e}"


registry.register(
    name="git_status",
    toolset="git",
    schema=GIT_STATUS_SCHEMA,
    handler=_git_status,
    emoji="🔗",
    is_read_only=True,
)

registry.register(
    name="git_log",
    toolset="git",
    schema=GIT_LOG_SCHEMA,
    handler=_git_log,
    emoji="🔗",
    is_read_only=True,
)

registry.register(
    name="git_diff",
    toolset="git",
    schema=GIT_DIFF_SCHEMA,
    handler=_git_diff,
    emoji="🔗",
    is_read_only=True,
)

registry.register(
    name="git_blame",
    toolset="git",
    schema=GIT_BLAME_SCHEMA,
    handler=_git_blame,
    emoji="🔗",
    is_read_only=True,
)

registry.register(
    name="git_show",
    toolset="git",
    schema=GIT_SHOW_SCHEMA,
    handler=_git_show,
    emoji="🔗",
    is_read_only=True,
)
