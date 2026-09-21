"""Tests for background task tools."""

import json
from unittest.mock import MagicMock, patch

import pytest

from nova.tasks import STATUS_COMPLETED, STATUS_RUNNING, TaskRecord
from nova.tools.task_tools import (
    _task_create,
    _task_list,
    _task_output,
    _task_status,
    _task_stop,
)


@pytest.fixture
def mock_task_manager():
    return MagicMock()


@pytest.fixture
def sample_task():
    return TaskRecord(
        id="b12345",
        type="shell",
        status=STATUS_RUNNING,
        description="test task",
        command="echo hello",
        pid=12345,
        created_at=1000000.0,
    )


def test_task_create_success(mock_task_manager):
    mock_task_manager.create_shell_task.return_value = "b12345"

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_create(
            {
                "command": "echo hello",
                "description": "test task",
            }
        )

    data = json.loads(result)
    assert data["success"] is True
    assert data["task_id"] == "b12345"
    assert "test task" in data["description"]


def test_task_create_uses_agent_workspace(mock_task_manager, tmp_path):
    mock_task_manager.create_shell_task.return_value = "b12345"

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        _task_create({"command": "pwd"}, workspace=tmp_path)

    mock_task_manager.create_shell_task.assert_called_once_with("pwd", "", cwd=str(tmp_path))


def test_task_create_long_command_preview(mock_task_manager):
    long_cmd = "x" * 100
    mock_task_manager.create_shell_task.return_value = "bxyz"

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_create(
            {
                "command": long_cmd,
            }
        )

    data = json.loads(result)
    assert len(data["description"]) == 80  # Truncated to 80 chars


def test_task_create_missing_command(mock_task_manager):
    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_create({"command": ""})

    data = json.loads(result)
    assert data["success"] is False
    assert "required" in data["error"].lower()


def test_task_status_success(mock_task_manager, sample_task):
    mock_task_manager.get_task.return_value = sample_task

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_status({"task_id": "b12345"})

    data = json.loads(result)
    assert data["task_id"] == "b12345"
    assert data["status"] == STATUS_RUNNING
    assert data["command"] == "echo hello"
    assert data["pid"] == 12345


def test_task_status_not_found(mock_task_manager):
    mock_task_manager.get_task.return_value = None

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_status({"task_id": "nonexistent"})

    data = json.loads(result)
    assert data["success"] is False
    assert "not found" in data["error"].lower()


def test_task_status_missing_task_id(mock_task_manager):
    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_status({"task_id": ""})

    data = json.loads(result)
    assert data["success"] is False
    assert "task_id" in data["error"].lower()


def test_task_output_success(mock_task_manager):
    mock_task_manager.read_task_output.return_value = "output content"

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_output({"task_id": "b12345"})

    assert result == "output content"
    mock_task_manager.read_task_output.assert_called_with("b12345", max_bytes=12000)


def test_task_output_custom_max_bytes(mock_task_manager):
    mock_task_manager.read_task_output.return_value = "short output"

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_output({"task_id": "b12345", "max_bytes": 5000})

    assert result == "short output"
    mock_task_manager.read_task_output.assert_called_with("b12345", max_bytes=5000)


def test_task_output_clamps_max_bytes(mock_task_manager):
    mock_task_manager.read_task_output.return_value = "short output"

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_output(
            {"task_id": "b12345", "max_bytes": 999999},
            config={"tasks": {"max_output_bytes": 1000}},
        )

    assert result == "short output"
    mock_task_manager.read_task_output.assert_called_with("b12345", max_bytes=1000)


def test_task_create_rejects_concurrency_limit(mock_task_manager, sample_task):
    mock_task_manager.list_tasks.return_value = [sample_task] * 2
    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_create(
            {"command": "echo hello"},
            config={"tasks": {"max_concurrent": 2}},
        )

    data = json.loads(result)
    assert data["success"] is False
    mock_task_manager.create_shell_task.assert_not_called()


def test_task_output_missing_task_id(mock_task_manager):
    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_output({"task_id": ""})

    data = json.loads(result)
    assert data["success"] is False
    assert "task_id" in data["error"].lower()


def test_task_stop_success(mock_task_manager):
    mock_task_manager.stop_task.return_value = "Task stopped"

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_stop({"task_id": "b12345"})

    data = json.loads(result)
    assert data["success"] is True
    assert "stopped" in data["message"].lower()


def test_task_stop_missing_task_id(mock_task_manager):
    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_stop({"task_id": ""})

    data = json.loads(result)
    assert data["success"] is False
    assert "task_id" in data["error"].lower()


def test_task_list_all(mock_task_manager, sample_task):
    task2 = TaskRecord(
        id="b67890",
        type="shell",
        status=STATUS_COMPLETED,
        description="another task",
        command="sleep 5",
    )
    mock_task_manager.list_tasks.return_value = [sample_task, task2]

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_list({})

    data = json.loads(result)
    assert data["count"] == 2
    assert len(data["tasks"]) == 2
    assert data["tasks"][0]["task_id"] == "b12345"
    assert data["tasks"][1]["task_id"] == "b67890"


def test_task_list_filtered_by_status(mock_task_manager, sample_task):
    mock_task_manager.list_tasks.return_value = [sample_task]

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_list({"status": STATUS_RUNNING})

    data = json.loads(result)
    assert data["count"] == 1
    mock_task_manager.list_tasks.assert_called_with(status=STATUS_RUNNING)


def test_task_list_empty(mock_task_manager):
    mock_task_manager.list_tasks.return_value = []

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_list({})

    data = json.loads(result)
    assert data["count"] == 0
    assert data["tasks"] == []


def test_task_list_truncates_long_command(mock_task_manager):
    long_cmd = "x" * 200
    task = TaskRecord(
        id="blong",
        type="shell",
        status=STATUS_RUNNING,
        description="long",
        command=long_cmd,
    )
    mock_task_manager.list_tasks.return_value = [task]

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_list({})

    data = json.loads(result)
    assert len(data["tasks"][0]["command"]) == 100


def test_task_list_handles_none_command(mock_task_manager):
    task = TaskRecord(
        id="bnone",
        type="agent",
        status=STATUS_COMPLETED,
        description="agent task",
        command="",  # No command
    )
    mock_task_manager.list_tasks.return_value = [task]

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_list({})

    data = json.loads(result)
    assert data["tasks"][0]["command"] == ""


def test_task_status_with_return_code(mock_task_manager):
    completed_task = TaskRecord(
        id="bcompleted",
        type="shell",
        status=STATUS_COMPLETED,
        description="test",
        command="true",
        pid=999,
        return_code=0,
    )
    mock_task_manager.get_task.return_value = completed_task

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_status({"task_id": "bcompleted"})

    data = json.loads(result)
    assert data["return_code"] == 0
    assert data["status"] == STATUS_COMPLETED


def test_task_stop_unknown_task_returns_failure(mock_task_manager):
    """Stopping a nonexistent task reports success: false instead of pretending."""
    mock_task_manager.get_task.return_value = None

    with patch("nova.tools.task_tools.get_task_manager", return_value=mock_task_manager):
        result = _task_stop({"task_id": "ghost"})

    data = json.loads(result)
    assert data["success"] is False
    assert "not found" in data["error"].lower()
    mock_task_manager.stop_task.assert_not_called()
