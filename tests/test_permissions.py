"""Tests for the permission system."""

from pathlib import Path

from nova.permissions import (
    PermissionChecker,
    PermissionMode,
    PermissionSettings,
    build_permission_checker,
)

# ── PermissionMode Tests ────────────────────────────────────────────────────


def test_permission_mode_enum_values():
    assert PermissionMode.AUTO == "auto"
    assert PermissionMode.ASK == "ask"


# ── PermissionChecker — Auto Mode ───────────────────────────────────────────


def test_auto_mode_allows_mutating_tool():
    settings = PermissionSettings(mode=PermissionMode.AUTO)
    checker = PermissionChecker(settings)
    result = checker.evaluate("write_file")
    assert result.allowed is True
    assert result.requires_confirmation is False


def test_auto_mode_allows_read_only_tool():
    settings = PermissionSettings(mode=PermissionMode.AUTO)
    checker = PermissionChecker(settings)
    result = checker.evaluate("read_file")
    assert result.allowed is True
    assert result.requires_confirmation is False


# ── PermissionChecker — Ask Mode ────────────────────────────────────────────


def test_ask_mode_read_only_tool_allowed():
    settings = PermissionSettings(mode=PermissionMode.ASK)
    checker = PermissionChecker(settings)
    result = checker.evaluate("read_file")
    assert result.allowed is True
    assert result.requires_confirmation is False


def test_ask_mode_mutating_tool_requires_confirmation():
    settings = PermissionSettings(mode=PermissionMode.ASK)
    checker = PermissionChecker(settings)
    result = checker.evaluate("write_file")
    assert result.allowed is True
    assert result.requires_confirmation is True


def test_ask_mode_terminal_requires_confirmation():
    settings = PermissionSettings(mode=PermissionMode.ASK)
    checker = PermissionChecker(settings)
    result = checker.evaluate("terminal")
    assert result.allowed is True
    assert result.requires_confirmation is True


# ── Sensitive Path Protection ───────────────────────────────────────────────


def test_sensitive_path_blocked_ssh():
    settings = PermissionSettings(mode=PermissionMode.AUTO)
    checker = PermissionChecker(settings)
    result = checker.evaluate("read_file", file_path="/home/user/.ssh/id_rsa")
    assert result.allowed is False
    assert "sensitive path" in result.reason


def test_sensitive_path_blocked_aws():
    settings = PermissionSettings(mode=PermissionMode.AUTO)
    checker = PermissionChecker(settings)
    result = checker.evaluate("read_file", file_path="/home/user/.aws/credentials")
    assert result.allowed is False


def test_sensitive_path_blocked_kube():
    settings = PermissionSettings(mode=PermissionMode.AUTO)
    checker = PermissionChecker(settings)
    result = checker.evaluate("read_file", file_path="/home/user/.kube/config")
    assert result.allowed is False


def test_sensitive_path_blocked_docker():
    settings = PermissionSettings(mode=PermissionMode.AUTO)
    checker = PermissionChecker(settings)
    result = checker.evaluate("read_file", file_path="/home/user/.docker/config.json")
    assert result.allowed is False


def test_normal_path_allowed():
    settings = PermissionSettings(mode=PermissionMode.AUTO)
    checker = PermissionChecker(settings)
    result = checker.evaluate("read_file", file_path="/home/user/project/file.txt")
    assert result.allowed is True


# ── Tool Deny/Allow Lists ───────────────────────────────────────────────────


def test_denied_tool_blocked():
    settings = PermissionSettings(
        mode=PermissionMode.AUTO,
        denied_tools={"terminal"},
    )
    checker = PermissionChecker(settings)
    result = checker.evaluate("terminal")
    assert result.allowed is False
    assert "explicitly denied" in result.reason


def test_allowed_tool_short_circuits():
    settings = PermissionSettings(
        mode=PermissionMode.ASK,
        allowed_tools={"write_file"},
    )
    checker = PermissionChecker(settings)
    result = checker.evaluate("write_file")
    assert result.allowed is True
    assert result.requires_confirmation is False


def test_allowed_tool_does_not_bypass_denied_command():
    settings = PermissionSettings(
        mode=PermissionMode.ASK,
        allowed_tools={"terminal"},
        denied_commands=["rm *"],
    )
    result = PermissionChecker(settings).evaluate("terminal", command="rm important.txt")
    assert result.allowed is False


def test_http_mutations_require_confirmation():
    result = PermissionChecker(PermissionSettings(mode=PermissionMode.ASK)).evaluate("http_post")
    assert result.requires_confirmation is True


# ── Command Deny Patterns ───────────────────────────────────────────────────


def test_denied_command_rm_rf_root():
    settings = PermissionSettings(mode=PermissionMode.AUTO)
    checker = PermissionChecker(settings)
    result = checker.evaluate("terminal", command="rm -rf /")
    assert result.allowed is False
    assert "denied" in result.reason


def test_denied_command_fork_bomb():
    settings = PermissionSettings(mode=PermissionMode.AUTO)
    checker = PermissionChecker(settings)
    result = checker.evaluate("terminal", command=":(){ :|:& };:")
    assert result.allowed is False


def test_safe_command_allowed():
    settings = PermissionSettings(mode=PermissionMode.AUTO)
    checker = PermissionChecker(settings)
    result = checker.evaluate("terminal", command="ls -la")
    assert result.allowed is True


def test_custom_denied_commands():
    settings = PermissionSettings(
        mode=PermissionMode.AUTO,
        denied_commands=["rm *", "curl *"],
    )
    checker = PermissionChecker(settings)
    result = checker.evaluate("terminal", command="rm important_file.txt")
    assert result.allowed is False


def test_denied_command_normalizes_whitespace_and_shell_segments():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.AUTO))
    result = checker.evaluate("terminal", command="printf x;  rm\t-rf   /")
    assert result.allowed is False


def test_sensitive_credential_paths_blocked():
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.AUTO))
    for path in (
        "/tmp/.env.production",
        "/tmp/.netrc",
        "/tmp/.git-credentials",
        "/tmp/.nova/config.yaml",
    ):
        assert checker.evaluate("read_file", file_path=path).allowed is False


# ── Path Rules ──────────────────────────────────────────────────────────────


def test_path_rule_deny():
    settings = PermissionSettings(
        mode=PermissionMode.AUTO,
        path_rules=[{"pattern": "/etc/*", "allow": False}],
    )
    checker = PermissionChecker(settings)
    result = checker.evaluate("read_file", file_path="/etc/hosts")
    assert result.allowed is False


def test_path_rule_allow():
    settings = PermissionSettings(
        mode=PermissionMode.ASK,
        path_rules=[{"pattern": "/tmp/*", "allow": True}],
    )
    checker = PermissionChecker(settings)
    result = checker.evaluate("write_file", file_path="/tmp/test.txt")
    assert result.allowed is True
    assert result.requires_confirmation is False  # short-circuited by allow rule


def test_path_rule_cannot_be_bypassed_with_parent_segments(tmp_path: Path):
    denied = tmp_path / "private" / "secret.txt"
    settings = PermissionSettings(
        mode=PermissionMode.AUTO,
        path_rules=[{"pattern": str(denied), "allow": False}],
    )
    checker = PermissionChecker(settings)

    disguised = tmp_path / "private" / ".." / "private" / "secret.txt"
    assert checker.evaluate("read_file", file_path=str(disguised)).allowed is False


def test_relative_path_rule_matches_relative_path(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checker = PermissionChecker(
        PermissionSettings(
            mode=PermissionMode.AUTO, path_rules=[{"pattern": "private/*", "allow": False}]
        )
    )

    assert checker.evaluate("read_file", file_path="private/secret.txt").allowed is False


# ── build_permission_checker ────────────────────────────────────────────────


def test_relative_rules_use_session_workspace(tmp_path, monkeypatch):
    server = tmp_path / "server"
    workspace = tmp_path / "project"
    server.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(server)
    checker = build_permission_checker(
        {"permissions": {"path_rules": [{"pattern": "private/*", "allow": False}]}},
        workspace=workspace,
    )

    for path in ("private/secret.txt", str(workspace / "private/secret.txt")):
        assert not checker.evaluate("read_file", file_path=path).allowed


def test_leading_wildcard_rules_match_outside_workspace(tmp_path):
    checker = PermissionChecker(
        PermissionSettings(path_rules=[{"pattern": "*/private/*", "allow": False}]),
        workspace=tmp_path / "server",
    )

    assert not checker.evaluate(
        "read_file", file_path=str(tmp_path / "project/private/secret.txt")
    ).allowed


def test_build_checker_from_config_default():
    config = {"permissions": {}}
    checker = build_permission_checker(config)
    assert checker.settings.mode == PermissionMode.ASK


def test_build_checker_from_config_auto():
    config = {"permissions": {"mode": "auto"}}
    checker = build_permission_checker(config)
    assert checker.settings.mode == PermissionMode.AUTO


def test_build_checker_from_config_denied_tools():
    config = {"permissions": {"denied_tools": ["terminal", "write_file"]}}
    checker = build_permission_checker(config)
    assert "terminal" in checker.settings.denied_tools
    assert "write_file" in checker.settings.denied_tools


def test_build_checker_from_config_denied_commands():
    config = {"permissions": {"denied_commands": ["rm *"]}}
    checker = build_permission_checker(config)
    assert "rm *" in checker.settings.denied_commands


def test_build_checker_missing_config():
    checker = build_permission_checker({})
    assert checker.settings.mode == PermissionMode.ASK


def test_empty_denied_commands_keeps_builtin_protection():
    checker = build_permission_checker({"permissions": {"denied_commands": []}})
    result = checker.evaluate("terminal", command="rm -rf /")
    assert result.allowed is False


# ── is_mutating_tool ────────────────────────────────────────────────────────


def test_is_mutating_tool_terminal():
    settings = PermissionSettings()
    checker = PermissionChecker(settings)
    assert checker.is_mutating_tool("terminal") is True


def test_is_mutating_tool_read_file():
    settings = PermissionSettings()
    checker = PermissionChecker(settings)
    assert checker.is_mutating_tool("read_file") is False


def test_is_mutating_tool_unknown():
    settings = PermissionSettings()
    checker = PermissionChecker(settings)
    assert checker.is_mutating_tool("unknown_tool") is False
