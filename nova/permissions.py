"""Permission system — tool execution approval with defense-in-depth.

Provides a configurable permission checker that evaluates tool calls
through a cascade of checks: sensitive paths, tool deny/allow lists,
path rules, command deny patterns, and permission modes.

Design inspired by OpenHarness's PermissionChecker, simplified for
Nova-Agent's lightweight ethos.
"""

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from nova.tools.registry import _READ_ONLY_TOOLS

logger = logging.getLogger(__name__)


class PermissionMode(StrEnum):
    """Permission modes controlling tool execution behavior."""

    AUTO = "auto"  # Allow all tools without confirmation
    ASK = "ask"  # Ask before mutating tools (default)


@dataclass
class PermissionResult:
    """Result of a permission evaluation."""

    allowed: bool
    requires_confirmation: bool = False
    reason: str = ""


# Built-in sensitive paths that can NEVER be overridden
_SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    "*/.ssh/*",
    "*/.ssh",
    "*/.aws/credentials",
    "*/.aws/config",
    "*/.config/gcloud/*",
    "*/.azure/*",
    "*/.gnupg/*",
    "*/.docker/config.json",
    "*/.kube/config",
    "*/.nova/credentials.json",
    "*/.nova/config.yaml",
    "*/.netrc",
    "*/.git-credentials",
    "*/.env",
    "*/.env.*",
    "*/.env.*/*",
    "*/.npmrc",
)

# Commands that are always denied (fnmatch patterns)
_DEFAULT_DENIED_COMMANDS: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf /*",
    "dd if=*",
    ":(){*};:*",  # fork bomb
    "mkfs*",
    "fdisk*",
    "format*",
    "shutdown*",
    "reboot*",
    "halt*",
    "poweroff*",
    "init 0*",
    "init 6*",
)

# Tools that mutate state (need confirmation in ask mode)
_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "patch_file",
        "terminal",
        "skill_manage",
        "wiki",
        "delegate_task",
        "http_post",
        "http_put",
        "http_delete",
    }
)


@dataclass
class PermissionSettings:
    """Configurable permission settings."""

    mode: PermissionMode = PermissionMode.ASK
    denied_tools: set[str] = field(default_factory=set)
    allowed_tools: set[str] = field(default_factory=set)
    denied_commands: list[str] = field(default_factory=lambda: list(_DEFAULT_DENIED_COMMANDS))
    path_rules: list[dict[str, Any]] = field(
        default_factory=list
    )  # [{"pattern": "...", "allow": bool}]


class PermissionChecker:
    """Evaluates whether a tool call should be allowed, denied, or require confirmation.

    Uses a defense-in-depth cascade:
    1. Built-in sensitive path protection (cannot be overridden)
    2. Explicit tool deny list
    3. Explicit tool allow list
    4. Path-level rules
    5. Command deny patterns
    6. Permission mode (auto vs ask)
    """

    def __init__(
        self, settings: PermissionSettings | None = None, *, workspace: Path | None = None
    ) -> None:
        self.settings = settings or PermissionSettings()
        self.workspace = (workspace or Path.cwd()).expanduser().resolve()

    def evaluate(
        self,
        tool_name: str,
        *,
        is_read_only: bool | None = None,
        file_path: str | None = None,
        command: str | None = None,
    ) -> PermissionResult:
        """Evaluate a tool call through the permission cascade.

        Args:
            tool_name: Name of the tool being called.
            is_read_only: Whether the tool is read-only. If None, inferred from tool name.
            file_path: File path argument (for path rule matching).
            command: Command string (for command deny matching).

        Returns:
            PermissionResult with allowed/requires_confirmation/reason.
        """
        # 1. Built-in sensitive path protection (cannot be overridden)
        if file_path and self._matches_sensitive_path(file_path):
            return PermissionResult(
                allowed=False,
                reason=f"Access denied: sensitive path '{file_path}'",
            )

        # 2. Explicit tool deny list
        if tool_name in self.settings.denied_tools:
            return PermissionResult(
                allowed=False,
                reason=f"Tool '{tool_name}' is explicitly denied",
            )

        # 3. Path-level rules. Allow lists must not bypass safety rules.
        if file_path and self.settings.path_rules:
            path_result = self._check_path_rules(file_path)
            if path_result is not None:
                return path_result

        # 4. Command deny patterns
        if command and self.settings.denied_commands and self._matches_denied_command(command):
            return PermissionResult(
                allowed=False,
                reason=f"Command denied by pattern: '{command[:80]}'",
            )

        # 5. Explicit tool allow list skips confirmation only.
        explicitly_allowed = tool_name in self.settings.allowed_tools

        # 6. Permission mode
        read_only = is_read_only if is_read_only is not None else tool_name in _READ_ONLY_TOOLS

        if read_only:
            return PermissionResult(allowed=True)

        if self.settings.mode == PermissionMode.AUTO or explicitly_allowed:
            return PermissionResult(allowed=True)

        # ASK mode — mutating tools require confirmation
        return PermissionResult(
            allowed=True,
            requires_confirmation=True,
            reason=f"Tool '{tool_name}' requires confirmation",
        )

    def _matches_sensitive_path(self, path: str) -> bool:
        """Check if a path matches any built-in sensitive pattern."""
        path = self._normalize_path(path)
        # Check both the path and path with trailing slash for directory matches
        for pattern in _SENSITIVE_PATH_PATTERNS:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path + "/", pattern):
                return True
        return False

    def _check_path_rules(self, path: str) -> PermissionResult | None:
        """Check path against user-defined path rules. Returns None if no match."""
        path = self._normalize_path(path)
        for rule in self.settings.path_rules:
            pattern = self._normalize_pattern(rule.get("pattern", ""))
            allow = rule.get("allow", True)
            if fnmatch.fnmatch(path, pattern):
                if allow:
                    return PermissionResult(allowed=True)
                return PermissionResult(
                    allowed=False,
                    reason=f"Path denied by rule: '{pattern}'",
                )
        return None

    def _normalize_path(self, path: str) -> str:
        try:
            expanded = Path(path).expanduser()
            if not expanded.is_absolute():
                expanded = self.workspace / expanded
            return str(expanded.resolve(strict=False))
        except (OSError, ValueError):
            return path

    def _normalize_pattern(self, pattern: str) -> str:
        if not pattern:
            return pattern
        if pattern[0] in "*?[":
            return pattern
        expanded = str(Path(pattern).expanduser())
        if not Path(expanded).is_absolute():
            expanded = str(self.workspace / expanded)
        prefix_length = len(expanded)
        for marker in ("*", "?", "["):
            index = expanded.find(marker)
            if index >= 0:
                prefix_length = min(prefix_length, index)
        prefix = expanded[:prefix_length]
        suffix = expanded[prefix_length:]
        try:
            normalized_prefix = str(Path(prefix or "/").resolve(strict=False))
        except (OSError, ValueError):
            return expanded
        if prefix.endswith("/") and not normalized_prefix.endswith("/"):
            normalized_prefix += "/"
        return normalized_prefix + suffix

    def _matches_denied_command(self, command: str) -> bool:
        """Check if a command matches any deny pattern."""
        cmd_lower = re.sub(r"\s+", " ", command).strip().lower()
        for pattern in self.settings.denied_commands:
            normalized_pattern = re.sub(r"\s+", " ", pattern).strip().lower()
            segments = [cmd_lower, *re.split(r"[;&|]+", cmd_lower)]
            if any(fnmatch.fnmatch(segment.strip(), normalized_pattern) for segment in segments):
                return True
        return False

    def is_mutating_tool(self, tool_name: str) -> bool:
        """Check if a tool is considered mutating (not read-only)."""
        return tool_name in _MUTATING_TOOLS


def build_permission_checker(config: dict, *, workspace: Path | None = None) -> PermissionChecker:
    """Build a PermissionChecker from Nova-Agent config."""
    perm_cfg = config.get("permissions", {})

    mode_str = perm_cfg.get("mode", "ask")
    try:
        mode = PermissionMode(mode_str)
    except ValueError:
        logger.warning("Unknown permission mode '%s', defaulting to 'ask'", mode_str)
        mode = PermissionMode.ASK

    settings = PermissionSettings(
        mode=mode,
        denied_tools=set(perm_cfg.get("denied_tools", [])),
        allowed_tools=set(perm_cfg.get("allowed_tools", [])),
        denied_commands=perm_cfg.get("denied_commands") or list(_DEFAULT_DENIED_COMMANDS),
        path_rules=perm_cfg.get("path_rules", []),
    )

    return PermissionChecker(settings, workspace=workspace)
