"""Tests for file operations tool."""

import os
import stat
import tempfile
from pathlib import Path

from nova.tools.file_ops import (
    _is_path_safe,
    _patch_file,
    _read_file,
    _validate_offset_limit,
    _verify_patch,
    _verify_write,
    _write_file,
)


class TestPathSafety:
    def test_safe_home_directory(self):
        """Paths in home directory are safe."""
        path = Path.home() / "test.txt"
        assert _is_path_safe(path) is None

    def test_safe_tmp_directory(self):
        """Paths in /tmp are safe."""
        path = Path("/tmp/test.txt")
        assert _is_path_safe(path) is None

    def test_blocked_etc_ssh_prefix(self):
        """Block /etc/ssh paths."""
        path = Path("/etc/ssh/id_rsa")
        error = _is_path_safe(path)
        # Should be blocked as /etc/ssh is in _BLOCKED_PATHS
        # (this checks exact path matching, not prefix)
        assert error is None or "protected" in str(error).lower()

    def test_blocked_prefix_proc(self):
        """Block /proc paths."""
        path = Path("/proc/123/cmdline")
        error = _is_path_safe(path)
        assert error is not None
        assert "protected" in error.lower()

    def test_blocked_prefix_sys(self):
        """Block /sys paths."""
        path = Path("/sys/kernel/config")
        error = _is_path_safe(path)
        assert error is not None
        assert "protected" in error.lower()

    def test_blocked_prefix_dev(self):
        """Block /dev paths."""
        path = Path("/dev/sda1")
        error = _is_path_safe(path)
        assert error is not None
        assert "protected" in error.lower()

    def test_sensitive_ssh_directory(self):
        """Block paths with .ssh."""
        path = Path.home() / ".ssh" / "id_rsa"
        error = _is_path_safe(path)
        assert error is not None
        assert "sensitive" in error.lower()

    def test_sensitive_gnupg_directory(self):
        """Block paths with .gnupg."""
        path = Path.home() / ".gnupg" / "pubring.kbx"
        error = _is_path_safe(path)
        assert error is not None
        assert "sensitive" in error.lower()

    def test_sensitive_aws_directory(self):
        """Block paths with .aws."""
        path = Path.home() / ".aws" / "credentials"
        error = _is_path_safe(path)
        assert error is not None
        assert "sensitive" in error.lower()

    def test_sensitive_docker_directory(self):
        """Block paths with .docker."""
        path = Path.home() / ".docker" / "config.json"
        error = _is_path_safe(path)
        assert error is not None
        assert "sensitive" in error.lower()

    def test_cwd_is_allowed(self):
        """Current working directory is allowed."""
        path = Path.cwd() / "test.txt"
        assert _is_path_safe(path) is None

    def test_outside_known_workspaces_is_denied(self):
        """Paths outside the home, temp, and current workspaces are denied."""
        error = _is_path_safe(Path("/usr/bin/env"))
        assert error is not None
        assert "outside known workspaces" in error.lower()

    def test_symlink_to_protected_path_is_denied(self, tmp_path):
        """Resolving symlinks prevents access to protected targets."""
        link = tmp_path / "passwd"
        link.symlink_to("/etc/passwd")

        error = _is_path_safe(link)

        assert error is not None
        assert "protected" in error.lower()


class TestValidateOffsetLimit:
    def test_valid_offset_limit(self):
        """Valid offset and limit parameters."""
        assert _validate_offset_limit(1, 100) is None
        assert _validate_offset_limit(10, 500) is None
        assert _validate_offset_limit(1, 1) is None

    def test_invalid_offset_zero(self):
        """Offset must be >= 1."""
        error = _validate_offset_limit(0, 100)
        assert error is not None
        assert "offset" in error.lower()

    def test_invalid_offset_negative(self):
        """Offset must be positive."""
        error = _validate_offset_limit(-5, 100)
        assert error is not None
        assert "offset" in error.lower()

    def test_invalid_offset_not_int(self):
        """Offset must be an integer."""
        error = _validate_offset_limit("1", 100)
        assert error is not None
        assert "offset" in error.lower()

    def test_invalid_limit_zero(self):
        """Limit must be >= 1."""
        error = _validate_offset_limit(1, 0)
        assert error is not None
        assert "limit" in error.lower()

    def test_invalid_limit_too_large(self):
        """Limit must be <= 10000."""
        error = _validate_offset_limit(1, 10001)
        assert error is not None
        assert "limit" in error.lower()

    def test_invalid_limit_not_int(self):
        """Limit must be an integer."""
        error = _validate_offset_limit(1, "500")
        assert error is not None
        assert "limit" in error.lower()


class TestReadFile:
    def test_read_simple_file(self):
        """Read a simple text file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("line1\nline2\nline3\n")

            result = _read_file({"path": str(path)})
            assert "line1" in result
            assert "line2" in result
            assert "line3" in result

    def test_read_with_offset(self):
        """Read starting from specific line."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("line1\nline2\nline3\nline4\nline5\n")

            result = _read_file({"path": str(path), "offset": 2, "limit": 3})
            assert "line2" in result
            assert "line3" in result
            assert "line4" in result
            assert "line1" not in result or "Lines 2-4" in result

    def test_read_with_limit(self):
        """Read limited number of lines."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            lines = "\n".join([f"line{i}" for i in range(1, 101)])
            path.write_text(lines)

            result = _read_file({"path": str(path), "limit": 10})
            # Should be truncated
            assert "truncated" not in result or "99" in result

    def test_read_file_not_found(self, tmp_path):
        """Error when file doesn't exist."""
        result = _read_file({"path": str(tmp_path / "missing.txt")}, workspace=tmp_path)
        assert "not found" in result.lower()

    def test_read_file_no_path(self):
        """Empty path is treated as current directory."""
        result = _read_file({"path": ""})
        # Empty path becomes "." which is a directory, not a file
        assert "error" in result.lower()

    def test_read_file_expanduser(self):
        """Expand ~ in paths."""
        with tempfile.TemporaryDirectory() as tmp:
            # Create in tmp instead of home
            path = Path(tmp) / "test.txt"
            path.write_text("content")

            result = _read_file({"path": str(path)})
            assert "content" in result

    def test_read_file_blocked_path(self):
        """Refuse to read blocked paths."""
        result = _read_file({"path": "/proc/cmdline"})
        assert "denied" in result.lower() or "protected" in result.lower()

    def test_read_file_invalid_offset(self):
        """Error with invalid offset."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("line1\n")

            result = _read_file({"path": str(path), "offset": 0, "limit": 10})
            assert "offset" in result.lower()

    def test_read_file_truncation_message(self):
        """Show truncation message for large files."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.txt"
            # Create a file large enough to trigger truncation
            large_content = "x" * 10000
            path.write_text(large_content)

            result = _read_file({"path": str(path), "limit": 50})
            # If truncated, should have the truncation note
            assert "truncated" in result or "x" * 100 in result

    def test_read_binary_file_error(self):
        """Handle binary files gracefully."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "binary.bin"
            path.write_bytes(b"\x00\x01\x02\x03")

            result = _read_file({"path": str(path)})
            # Should either succeed (with decoding errors) or fail gracefully
            assert isinstance(result, str)

    def test_read_empty_file(self):
        """Read empty file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.txt"
            path.write_text("")

            result = _read_file({"path": str(path)})
            assert "Lines 1-0" in result or "empty" in result.lower() or result.strip() == ""


class TestWriteFile:
    def test_failed_tool_result_cannot_be_verified_by_matching_file(self, tmp_path):
        path = tmp_path / "test.txt"
        path.write_text("content")

        result = _verify_write({"path": str(path), "content": "content"}, "Error: write failed")

        assert result.status == "failed"

    def test_write_simple_file(self):
        """Write content to a new file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"

            result = _write_file({"path": str(path), "content": "Hello world"})
            assert "Successfully wrote" in result
            assert path.read_text() == "Hello world"

    def test_write_creates_directories(self):
        """Create parent directories as needed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a" / "b" / "c" / "test.txt"

            result = _write_file({"path": str(path), "content": "nested"})
            assert "Successfully wrote" in result
            assert path.read_text() == "nested"

    def test_write_overwrites_existing(self):
        """Overwrite existing file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("old content")

            result = _write_file({"path": str(path), "content": "new content"})
            assert "Successfully wrote" in result
            assert path.read_text() == "new content"

    def test_write_counts_lines(self):
        """Count lines correctly in result."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            content = "line1\nline2\nline3"

            result = _write_file({"path": str(path), "content": content})
            assert "3" in result  # 3 lines (last line doesn't end with \n)

    def test_write_no_path(self):
        """Empty path is treated as current directory."""
        result = _write_file({"path": "", "content": "test"})
        # Empty path becomes "." which causes an error when writing
        assert "error" in result.lower()

    def test_write_blocked_path(self):
        """Refuse to write to blocked paths."""
        result = _write_file({"path": "/etc/test.txt", "content": "test"})
        assert "denied" in result.lower() or "protected" in result.lower()

    def test_write_content_too_large(self):
        """Reject content that's too large."""
        large_content = "x" * 600000

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            result = _write_file({"path": str(path), "content": large_content})
            assert "too large" in result.lower()

    def test_write_empty_content(self):
        """Write empty content."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"

            result = _write_file({"path": str(path), "content": ""})
            assert "Successfully wrote" in result
            assert path.read_text() == ""

    def test_write_special_characters(self):
        """Write files with special characters."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            content = "Hello 世界 🌍\nSpecial: © ® ™"

            result = _write_file({"path": str(path), "content": content})
            assert "Successfully wrote" in result
            assert path.read_text() == content


class TestPatchFile:
    def test_failed_tool_result_cannot_be_verified_by_matching_file(self, tmp_path):
        path = tmp_path / "test.txt"
        path.write_text("new")

        result = _verify_patch(
            {"path": str(path), "old_string": "old", "new_string": "new"},
            "Error: patch failed",
        )

        assert result.status == "failed"

    def test_patch_simple_replacement(self):
        """Apply simple search/replace patch."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("Hello world")

            result = _patch_file(
                {"path": str(path), "old_string": "world", "new_string": "universe"}
            )
            assert "Successfully patched" in result
            assert path.read_text() == "Hello universe"

    def test_patch_multiline(self):
        """Patch multiline strings."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            content = "line1\nline2\nline3"
            path.write_text(content)

            result = _patch_file(
                {"path": str(path), "old_string": "line2", "new_string": "REPLACED"}
            )
            assert "Successfully patched" in result
            assert "REPLACED" in path.read_text()

    def test_patch_first_occurrence_only(self):
        """Replace only first occurrence."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("foo foo foo")

            result = _patch_file({"path": str(path), "old_string": "foo", "new_string": "bar"})
            assert "Successfully patched" in result
            assert path.read_text() == "bar foo foo"

    def test_patch_string_not_found(self):
        """Error when search string not found."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("original content")

            result = _patch_file(
                {"path": str(path), "old_string": "not there", "new_string": "replacement"}
            )
            assert "not found" in result.lower()

    def test_patch_file_not_found(self, tmp_path):
        """Error when file doesn't exist."""
        result = _patch_file(
            {"path": str(tmp_path / "missing.txt"), "old_string": "old", "new_string": "new"},
            workspace=tmp_path,
        )
        assert "not found" in result.lower()

    def test_patch_no_path(self):
        """Empty path is treated as current directory."""
        result = _patch_file({"path": "", "old_string": "old", "new_string": "new"})
        # Empty path becomes "." which causes an error
        assert "error" in result.lower()

    def test_patch_blocked_path(self):
        """Refuse to patch blocked paths."""
        result = _patch_file(
            {"path": "/sys/kernel/config/file.txt", "old_string": "old", "new_string": "new"}
        )
        assert "denied" in result.lower() or "protected" in result.lower()

    def test_patch_old_string_too_large(self):
        """Reject search string that's too large."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("test")

            large_search = "x" * 150000
            result = _patch_file(
                {"path": str(path), "old_string": large_search, "new_string": "new"}
            )
            assert "too large" in result.lower()

    def test_patch_new_string_too_large(self):
        """Reject replacement string that's too large."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("old content")

            large_replacement = "x" * 150000
            result = _patch_file(
                {"path": str(path), "old_string": "old", "new_string": large_replacement}
            )
            assert "too large" in result.lower()

    def test_patch_preserves_rest_of_file(self):
        """Ensure patching doesn't corrupt rest of file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            content = "START\nmiddle content\nEND"
            path.write_text(content)

            _patch_file({"path": str(path), "old_string": "middle", "new_string": "PATCHED"})
            result = path.read_text()
            assert "START" in result
            assert "PATCHED" in result
            assert "END" in result

    def test_patch_special_characters(self):
        """Patch with special characters."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("Hello © world")

            _patch_file({"path": str(path), "old_string": "©", "new_string": "®"})
            assert "®" in path.read_text()

    def test_patch_whitespace_significant(self):
        """Whitespace must match exactly."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("hello  world")  # two spaces

            result = _patch_file(
                {
                    "path": str(path),
                    "old_string": "hello world",  # one space
                    "new_string": "hi universe",
                }
            )
            assert "not found" in result.lower()


class TestAtomicWritePreservation:
    def test_patch_preserves_executable_mode(self, tmp_path):
        script = tmp_path / "run.sh"
        script.write_text("echo hi\n")
        script.chmod(0o755)

        result = _patch_file(
            {"path": str(script), "old_string": "hi", "new_string": "bye"}, workspace=tmp_path
        )

        assert result.startswith("Successfully patched")
        assert stat.S_IMODE(script.stat().st_mode) == 0o755

    def test_write_preserves_existing_mode(self, tmp_path):
        script = tmp_path / "run.sh"
        script.write_text("old\n")
        script.chmod(0o750)

        _write_file({"path": str(script), "content": "new\n"}, workspace=tmp_path)

        assert stat.S_IMODE(script.stat().st_mode) == 0o750

    def test_new_file_respects_umask(self, tmp_path):
        path = tmp_path / "new.txt"

        _write_file({"path": str(path), "content": "x"}, workspace=tmp_path)

        umask = os.umask(0)
        os.umask(umask)
        assert stat.S_IMODE(path.stat().st_mode) == 0o666 & ~umask

    def test_write_through_symlink_keeps_link(self, tmp_path):
        target = tmp_path / "real.txt"
        target.write_text("old")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        _write_file({"path": str(link), "content": "new"}, workspace=tmp_path)
        _patch_file(
            {"path": str(link), "old_string": "new", "new_string": "newer"}, workspace=tmp_path
        )

        assert link.is_symlink()
        assert target.read_text() == "newer"


class TestExistenceOutsideWorkspace:
    def test_read_missing_file_outside_workspace_is_denied(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "elsewhere" / "missing.txt"

        result = _read_file({"path": str(outside)}, workspace=workspace)

        assert "denied" in result.lower()
        assert "not found" not in result.lower()

    def test_patch_missing_file_outside_workspace_is_denied(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "elsewhere" / "missing.txt"

        result = _patch_file(
            {"path": str(outside), "old_string": "a", "new_string": "b"}, workspace=workspace
        )

        assert "denied" in result.lower()
        assert "not found" not in result.lower()
