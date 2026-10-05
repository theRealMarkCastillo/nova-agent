"""File operations tool — read, write, and patch files.

Supports reading with line ranges, writing with atomic saves,
and targeted patching with search/replace.
"""

import hashlib
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from nova.harness import VerificationResult
from nova.tools.path_safety import path_safety_error
from nova.tools.registry import registry


def _verify_write(args: dict[str, Any], result: Any, **kwargs: Any) -> VerificationResult:
    if not isinstance(result, str) or not result.startswith("Successfully wrote"):
        return VerificationResult("failed", reason="write tool did not report success")
    path = Path(args.get("path", "")).expanduser()
    content = args.get("content")
    if isinstance(content, str) and path.is_file():
        actual = path.read_text(encoding="utf-8")
        if actual == content:
            digest = hashlib.sha256(actual.encode()).hexdigest()[:16]
            return VerificationResult("verified", f"{path} sha256={digest}")
    return VerificationResult("failed", reason="write read-back did not match requested content")


def _verify_patch(args: dict[str, Any], result: Any, **kwargs: Any) -> VerificationResult:
    if not isinstance(result, str) or not result.startswith("Successfully patched"):
        return VerificationResult("failed", reason="patch tool did not report success")
    path = Path(args.get("path", "")).expanduser()
    if path.is_file():
        actual = path.read_text(encoding="utf-8")
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        if new in actual and (not old or old not in actual):
            return VerificationResult("verified", f"{path} replacement present")
    return VerificationResult("failed", reason="patch read-back did not establish replacement")


logger = logging.getLogger(__name__)

# Paths that should never be accessed by the agent
_BLOCKED_PATHS = {
    "/etc/shadow",
    "/etc/passwd",
    "/etc/sudoers",
    "/etc/ssh",
    "/etc/ssl",
}
_BLOCKED_PREFIXES = [
    "/etc/",
    "/private/etc/",
    "/proc/",
    "/sys/",
    "/dev/",
]

READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "Read the contents of a file. Supports optional line range and offset.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file.",
            },
            "offset": {
                "type": "integer",
                "description": "Starting line number (1-based). Default: 1.",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read. Default: 500.",
                "default": 500,
            },
        },
        "required": ["path"],
    },
}

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": "Write content to a file. Creates parent directories if needed. Overwrites existing content.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file.",
            },
            "content": {
                "type": "string",
                "description": "The content to write.",
            },
        },
        "required": ["path", "content"],
    },
}

PATCH_FILE_SCHEMA = {
    "name": "patch_file",
    "description": "Apply a search/replace patch to a file. Replaces the first occurrence of old_string with new_string.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact text to replace (must match exactly including whitespace).",
            },
            "new_string": {
                "type": "string",
                "description": "The replacement text.",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
}

_MAX_READ_CHARS = 8000
_MAX_WRITE_CHARS = 500000  # 500KB max write
_MAX_PATCH_CHARS = 100000  # 100KB max patch string


# Read once at import: os.umask can only be queried by setting it, which is
# not thread-safe once tool calls run concurrently.
_UMASK = os.umask(0)
os.umask(_UMASK)


def _atomic_write(path: Path, content: str) -> None:
    """Replace a file's content atomically, keeping its mode and any symlink."""
    target = path.resolve()
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = 0o666 & ~_UMASK
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    except Exception:
        os.unlink(tmp_path)
        raise


def _is_path_safe(path: Path, **kwargs: Any) -> str | None:
    """Check if a path is safe to access. Returns error message or None if safe."""
    return path_safety_error(path, **kwargs)


def _is_obviously_blocked(path: Path) -> bool:
    """Recognize protected paths even when the requested file does not exist."""
    raw = str(path)
    return raw in _BLOCKED_PATHS or any(raw.startswith(prefix) for prefix in _BLOCKED_PREFIXES)


def _validate_offset_limit(offset: Any, limit: Any) -> str | None:
    """Validate offset and limit parameters. Returns error message or None."""
    if not isinstance(offset, int) or offset < 1:
        return "Error: offset must be a positive integer (1-based line number)."
    if not isinstance(limit, int) or limit < 1 or limit > 10000:
        return "Error: limit must be between 1 and 10000."
    return None


def _read_file(args: dict[str, Any], **kwargs: Any) -> str:
    """Read a file with optional line range."""
    path = Path(args.get("path", "")).expanduser()
    path_str = str(path).strip()
    if not path_str:
        return "Error: No path provided."

    if _is_obviously_blocked(path):
        return f"Error: Access denied to protected path: {path}"

    if not path.exists():
        return f"Error: File not found: {path}"

    # Security check
    if error := _is_path_safe(path, **kwargs):
        return error

    offset = args.get("offset", 1)
    limit = args.get("limit", 500)

    if error := _validate_offset_limit(offset, limit):
        return error

    try:
        start = max(0, offset - 1)
        end = start + limit

        # Stream line by line so huge files are never loaded into memory.
        # Only the requested window is collected; the line count stays exact.
        total_lines = 0
        selected: list[str] = []
        collected_chars = 0
        truncated = False
        with open(path, encoding="utf-8") as f:
            for line in f:
                total_lines += 1
                if start <= total_lines - 1 < end:
                    selected.append(line)
                    collected_chars += len(line)
                    if collected_chars > _MAX_READ_CHARS:
                        truncated = True
                        break
            if truncated:
                for _ in f:
                    total_lines += 1

        content = "".join(selected)
        if truncated:
            content = content[:_MAX_READ_CHARS]
            shown_end = start + content.count("\n") + 1
            remaining = total_lines - shown_end
            content += f"\n\n[...truncated, {remaining:,} more lines...]"

        line_info = f"Lines {start + 1}-{min(end, total_lines)} of {total_lines}"
        return f"{line_info}:\n{content}"

    except Exception as e:
        return f"Error reading {path}: {e}"


def _write_file(args: dict[str, Any], **kwargs: Any) -> str:
    """Write content to a file atomically."""
    path = Path(args.get("path", "")).expanduser()
    content = args.get("content", "")

    if not isinstance(content, str):
        return "Error: Content must be a string."

    path_str = str(path).strip()
    if not path_str:
        return "Error: No path provided."

    # Security check
    if error := _is_path_safe(path, **kwargs):
        return error

    # Validate content size
    if len(content) > _MAX_WRITE_CHARS:
        return f"Error: Content too large (max {_MAX_WRITE_CHARS:,} chars, got {len(content):,})."

    try:
        _atomic_write(path, content)

        lines = content.count("\n") + 1
        return f"Successfully wrote {lines:,} lines to {path}"

    except Exception as e:
        return f"Error writing {path}: {e}"


def _patch_file(args: dict[str, Any], **kwargs: Any) -> str:
    """Apply a search/replace patch."""
    path = Path(args.get("path", "")).expanduser()
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")

    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return "Error: old_string and new_string must be strings."

    path_str = str(path).strip()
    if not path_str:
        return "Error: No path provided."

    if _is_obviously_blocked(path):
        return f"Error: Access denied to protected path: {path}"

    # Validate patch string sizes
    if len(old_string) > _MAX_PATCH_CHARS:
        return f"Error: Search text too large (max {_MAX_PATCH_CHARS:,} chars)."
    if len(new_string) > _MAX_PATCH_CHARS:
        return f"Error: Replacement text too large (max {_MAX_PATCH_CHARS:,} chars)."

    if not path.exists():
        return f"Error: File not found: {path}"

    # Security check
    if error := _is_path_safe(path, **kwargs):
        return error

    try:
        content = path.read_text(encoding="utf-8")

        if old_string not in content:
            return f"Error: Search text not found in {path}"

        new_content = content.replace(old_string, new_string, 1)

        _atomic_write(path, new_content)

        return f"Successfully patched {path}"

    except Exception as e:
        return f"Error patching {path}: {e}"


registry.register(
    name="read_file",
    toolset="file",
    schema=READ_FILE_SCHEMA,
    handler=_read_file,
    emoji="📖",
)

registry.register(
    name="write_file",
    toolset="file",
    schema=WRITE_FILE_SCHEMA,
    handler=_write_file,
    emoji="✏️",
    verifier=_verify_write,
)

registry.register(
    name="patch_file",
    toolset="file",
    schema=PATCH_FILE_SCHEMA,
    handler=_patch_file,
    emoji="🩹",
    verifier=_verify_patch,
)
