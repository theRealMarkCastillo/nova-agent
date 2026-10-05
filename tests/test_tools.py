"""Tests for tool handlers — terminal, file_ops, search_files.

Firecrawl-backed web tools are covered in tests/test_web_tools.py.
"""

import tempfile
from pathlib import Path

from nova.tools.file_ops import _patch_file, _read_file, _write_file
from nova.tools.search_files import _search_files
from nova.tools.terminal import _truncate_output, execute_terminal

# ── Terminal Tests ──────────────────────────────────────────────────────────


def test_terminal_echo_command():
    """Test basic echo command execution."""
    result = execute_terminal({"command": "echo hello"})
    assert "hello" in result
    assert "exit code: 0" in result


def test_terminal_command_with_workdir():
    """Test command execution with working directory."""
    tmpdir = tempfile.mkdtemp()
    result = execute_terminal({"command": "pwd", "workdir": tmpdir})
    assert tmpdir in result


def test_terminal_rejects_protected_workdir():
    result = execute_terminal({"command": "pwd", "workdir": "/etc"})
    assert "Access denied" in result


def test_terminal_command_timeout():
    """Test that long-running commands time out."""
    result = execute_terminal({"command": "sleep 10", "timeout": 1})
    assert "timed out" in result


def test_terminal_empty_command():
    """Test that empty command returns error."""
    result = execute_terminal({"command": ""})
    assert "Error" in result


def test_terminal_failing_command():
    """Test that failing commands return non-zero exit code."""
    result = execute_terminal({"command": "false"})
    assert "exit code: 1" in result


def test_terminal_output_truncation():
    """Test that long output is truncated."""
    # Generate a command that produces lots of output
    result = execute_terminal({"command": "python3 -c \"print('A' * 20000)\""})
    assert "truncated" in result or len(result) < 20000


def test_truncate_output_short():
    """Test that short output is not truncated."""
    output = "short output"
    result = _truncate_output(output, max_chars=100)
    assert result == output


def test_truncate_output_long():
    """Test that long output is truncated with head/tail."""
    output = "H" * 100 + "M" * 800 + "T" * 100
    result = _truncate_output(output, max_chars=100)
    assert "truncated" in result
    assert result.startswith("H")
    assert result.endswith("T")


# ── File Operations Tests ───────────────────────────────────────────────────


def test_read_file_basic():
    """Test reading a file."""
    tmpdir = Path(tempfile.mkdtemp())
    test_file = tmpdir / "test.txt"
    test_file.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")

    result = _read_file({"path": str(test_file)})
    assert "line 1" in result
    assert "line 2" in result
    assert "line 3" in result


def test_read_file_with_range():
    """Test reading a file with line range."""
    tmpdir = Path(tempfile.mkdtemp())
    test_file = tmpdir / "test.txt"
    test_file.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n", encoding="utf-8")

    result = _read_file({"path": str(test_file), "offset": 2, "limit": 2})
    assert "line 2" in result
    assert "line 3" in result
    assert "line 1" not in result
    assert "line 4" not in result


def test_read_file_nonexistent(tmp_path):
    """Test reading a nonexistent file."""
    result = _read_file({"path": str(tmp_path / "missing.txt")}, workspace=tmp_path)
    assert "Error" in result
    assert "not found" in result


def test_write_file_basic():
    """Test writing a file."""
    tmpdir = Path(tempfile.mkdtemp())
    test_file = tmpdir / "output.txt"

    result = _write_file({"path": str(test_file), "content": "hello world"})
    assert "Written" in result or "written" in result.lower() or "success" in result.lower()
    assert test_file.read_text() == "hello world"


def test_write_file_creates_parent_dirs():
    """Test that write_file creates parent directories."""
    tmpdir = Path(tempfile.mkdtemp())
    test_file = tmpdir / "sub" / "dir" / "output.txt"

    _write_file({"path": str(test_file), "content": "nested"})
    assert test_file.exists()
    assert test_file.read_text() == "nested"


def test_patch_file_basic():
    """Test basic search/replace patch."""
    tmpdir = Path(tempfile.mkdtemp())
    test_file = tmpdir / "test.txt"
    test_file.write_text("hello world\nfoo bar\n", encoding="utf-8")

    result = _patch_file(
        {
            "path": str(test_file),
            "old_string": "hello world",
            "new_string": "goodbye world",
        }
    )
    assert "Patched" in result or "patched" in result.lower() or "success" in result.lower()
    content = test_file.read_text()
    assert "goodbye world" in content
    assert "hello world" not in content


def test_patch_file_no_match():
    """Test patch with no matching string."""
    tmpdir = Path(tempfile.mkdtemp())
    test_file = tmpdir / "test.txt"
    test_file.write_text("hello world\n", encoding="utf-8")

    result = _patch_file(
        {
            "path": str(test_file),
            "old_string": "not found",
            "new_string": "replacement",
        }
    )
    assert "Error" in result or "not found" in result.lower()


# ── Search Files Tests ──────────────────────────────────────────────────────


def test_search_files_basic():
    """Test basic file search."""
    tmpdir = Path(tempfile.mkdtemp())
    (tmpdir / "file1.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    (tmpdir / "file2.py").write_text("def world():\n    pass\n", encoding="utf-8")

    result = _search_files({"pattern": "hello", "path": str(tmpdir)})
    assert "file1.py" in result
    assert "file2.py" not in result


def test_search_files_regex():
    """Test regex search mode."""
    tmpdir = Path(tempfile.mkdtemp())
    (tmpdir / "test.py").write_text("def test_foo():\n    pass\n", encoding="utf-8")

    result = _search_files(
        {
            "pattern": "def test_\\w+",
            "path": str(tmpdir),
            "mode": "regex",
        }
    )
    assert "test.py" in result


def test_search_files_with_glob():
    """Test search with glob filter."""
    tmpdir = Path(tempfile.mkdtemp())
    (tmpdir / "file.py").write_text("hello\n", encoding="utf-8")
    (tmpdir / "file.txt").write_text("hello\n", encoding="utf-8")

    result = _search_files(
        {
            "pattern": "hello",
            "path": str(tmpdir),
            "file_pattern": "*.py",
        }
    )
    assert "file.py" in result
    assert "file.txt" not in result


def test_search_files_no_results():
    """Test search with no matching results."""
    tmpdir = Path(tempfile.mkdtemp())
    (tmpdir / "file.txt").write_text("nothing here\n", encoding="utf-8")

    result = _search_files({"pattern": "notfound", "path": str(tmpdir)})
    assert "No matches" in result or "no matches" in result.lower() or "No results" in result


def test_search_files_large_file_truncated():
    """Files over the read cap are searched from their head only, with a note."""
    tmpdir = Path(tempfile.mkdtemp())
    big = tmpdir / "big.log"
    # ~68KB of filler lines, needle buried past the 64KB cap
    big.write_text(("x" * 200 + "\n") * 340 + "needle_here\n", encoding="utf-8")

    result = _search_files({"pattern": "needle_here", "path": str(tmpdir)})
    assert "No matches" in result
    assert "read cap" in result  # the truncation note is shown

    (tmpdir / "small.txt").write_text("needle_here\n", encoding="utf-8")
    result2 = _search_files({"pattern": "needle_here", "path": str(tmpdir)})
    assert "small.txt" in result2


def test_search_files_invalid_max_results():
    """A non-integer max_results must not crash the handler."""
    tmpdir = Path(tempfile.mkdtemp())
    (tmpdir / "a.txt").write_text("hello\n", encoding="utf-8")

    result = _search_files({"pattern": "hello", "path": str(tmpdir), "max_results": "abc"})
    assert "a.txt" in result


def test_search_files_clamps_nonpositive_max_results():
    tmpdir = Path(tempfile.mkdtemp())
    (tmpdir / "a.txt").write_text("hello\n", encoding="utf-8")

    result = _search_files({"pattern": "hello", "path": str(tmpdir), "max_results": 0})
    assert "a.txt" in result
    assert "limited to 1 results" in result
