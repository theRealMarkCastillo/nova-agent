"""Tests for git_tool."""

from unittest.mock import Mock, patch

import pytest

from nova.tools.git_tool import (
    _git_blame,
    _git_diff,
    _git_log,
    _git_show,
    _git_status,
)


@pytest.fixture
def mock_run_git_command():
    """Fixture for mocking _run_git_command."""
    with patch("nova.tools.git_tool._run_git_command") as mock:
        yield mock


class TestGitStatus:
    """Tests for git_status tool."""

    def test_git_status_clean(self, mock_run_git_command):
        """Test status when working tree is clean."""
        mock_run_git_command.return_value = (0, "", "")
        result = _git_status({"repo": "."})
        assert "clean" in result.lower()

    def test_git_status_with_changes(self, mock_run_git_command):
        """Test status with staged changes."""
        mock_run_git_command.return_value = (0, " M file.py\n?? new.py\n", "")
        result = _git_status({"repo": "."})
        assert "file.py" in result
        assert "new.py" in result

    def test_git_status_error(self, mock_run_git_command):
        """Test status error handling."""
        mock_run_git_command.return_value = (128, "", "fatal: not a git repository")
        result = _git_status({"repo": "."})
        assert "Error:" in result

    def test_git_status_rejects_protected_repository(self):
        result = _git_status({"repo": "/etc"})
        assert "Access denied" in result


class TestGitLog:
    """Tests for git_log tool."""

    def test_git_log_success(self, mock_run_git_command):
        """Test successful git log."""
        log_output = "abc1234 feat: add new feature\ndef5678 fix: bug fix\n"
        mock_run_git_command.return_value = (0, log_output, "")
        result = _git_log({"repo": ".", "limit": 2})
        assert "abc1234" in result
        assert "def5678" in result

    def test_git_log_no_commits(self, mock_run_git_command):
        """Test git log with no commits."""
        mock_run_git_command.return_value = (0, "", "")
        result = _git_log({"repo": "."})
        assert "No commits" in result


class TestGitDiff:
    """Tests for git_diff tool."""

    def test_git_diff_unstaged(self, mock_run_git_command):
        """Test unstaged diff."""
        diff_output = "--- a/file.py\n+++ b/file.py\n@@ -1,3 +1,4 @@\n"
        mock_run_git_command.return_value = (0, diff_output, "")
        result = _git_diff({"repo": ".", "staged": False})
        assert "---" in result or "file.py" in result

    def test_git_diff_staged(self, mock_run_git_command):
        """Test staged diff."""
        diff_output = "diff --git a/file.py b/file.py\n"
        mock_run_git_command.return_value = (0, diff_output, "")
        result = _git_diff({"repo": ".", "staged": True})
        assert "file.py" in result or "diff" in result.lower()

    def test_git_diff_no_changes(self, mock_run_git_command):
        """Test diff with no changes."""
        mock_run_git_command.return_value = (0, "", "")
        result = _git_diff({"repo": "."})
        assert "No differences" in result

    def test_git_diff_rejects_output_option_without_modifying_file(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("keep")
        result = _git_diff({"repo": ".", "file_path": f"--output={target}"})
        assert "invalid file_path" in result.lower()
        assert target.read_text() == "keep"


class TestGitBlame:
    """Tests for git_blame tool."""

    def test_git_blame_success(self, mock_run_git_command):
        """Test successful blame."""
        blame_output = "abc1234 (Author Name 2024-01-01) line 1\n"
        mock_run_git_command.return_value = (0, blame_output, "")
        result = _git_blame({"repo": ".", "file_path": "file.py"})
        assert "abc1234" in result or "Author Name" in result

    def test_git_blame_no_file(self):
        """Test blame without file path."""
        result = _git_blame({"repo": ".", "file_path": ""})
        assert "Error:" in result


class TestGitShow:
    """Tests for git_show tool."""

    def test_git_show_commit(self, mock_run_git_command):
        """Test showing a commit."""
        show_output = "commit abc1234\nAuthor: Name <email>\n"
        mock_run_git_command.return_value = (0, show_output, "")
        result = _git_show({"repo": ".", "rev": "abc1234"})
        assert "commit" in result.lower() or "abc1234" in result

    def test_git_show_file_version(self, mock_run_git_command):
        """Test showing file version from commit."""
        file_content = "def main():\n    pass\n"
        mock_run_git_command.return_value = (0, file_content, "")
        result = _git_show({"repo": ".", "rev": "HEAD", "file_path": "main.py"})
        assert "def main" in result or "pass" in result

    def test_git_show_no_rev(self):
        """Test show without rev."""
        result = _git_show({"repo": ".", "rev": ""})
        assert "Error:" in result

    def test_git_show_rejects_path_outside_repository(self, tmp_path):
        result = _git_show({"repo": str(tmp_path), "rev": "HEAD", "file_path": "/etc/passwd"})
        assert "Access denied" in result

    def test_git_show_rejects_sensitive_path_embedded_in_rev(self, tmp_path):
        result = _git_show({"repo": str(tmp_path), "rev": "HEAD:.env"})
        assert "Access denied" in result


def test_git_status_rejects_repository_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    repository = tmp_path / "other"
    workspace.mkdir()
    repository.mkdir()

    result = _git_status({"repo": str(repository)}, workspace=workspace)

    assert "outside known workspaces" in result


class TestGitIntegration:
    """Integration tests with mocked subprocess."""

    @patch("nova.tools.git_tool.subprocess.run")
    def test_git_status_integration(self, mock_run):
        """Test git_status with real subprocess mock."""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = " M test.py\n"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        result = _git_status({"repo": "."})
        assert "test.py" in result

    @patch("nova.tools.git_tool.subprocess.run")
    def test_git_command_timeout(self, mock_run):
        """Test git command timeout."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired("git", 30)

        result = _git_log({"repo": "."})
        assert "Error:" in result
        assert "timeout" in result.lower() or "timed out" in result.lower()

    @patch.dict("os.environ", {"LLM_API_KEY": "secret", "PATH": "/usr/bin"}, clear=True)
    @patch("nova.tools.git_tool.subprocess.run")
    def test_git_command_does_not_inherit_credentials(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

        _git_status({"repo": "."})

        env = mock_run.call_args.kwargs["env"]
        assert "LLM_API_KEY" not in env
        assert env["PATH"] == "/usr/bin"
