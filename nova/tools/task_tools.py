"""Background task tools — create, monitor, and control background tasks.

Provides tools for fire-and-forget shell execution with status tracking:
- task_create: Start a background shell command
- task_status: Check task status
- task_output: Read task output
- task_stop: Stop a running task
- task_list: List all tasks
"""

import json
from typing import Any

from nova.tasks import get_task_manager
from nova.tools.registry import registry

TASK_CREATE_SCHEMA = {
    "name": "task_create",
    "description": (
        "Start a shell command running in the background. "
        "Returns a task ID that can be used to check status, read output, or stop the task. "
        "Use for long-running commands that don't need immediate results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute in the background.",
            },
            "description": {
                "type": "string",
                "description": "Short description of what the task does.",
            },
        },
        "required": ["command"],
    },
}

TASK_STATUS_SCHEMA = {
    "name": "task_status",
    "description": "Check the status of a background task.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID returned by task_create.",
            },
        },
        "required": ["task_id"],
    },
}

TASK_OUTPUT_SCHEMA = {
    "name": "task_output",
    "description": "Read the output of a background task (returns the tail of the log).",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID returned by task_create.",
            },
            "max_bytes": {
                "type": "integer",
                "description": "Maximum bytes to read from the end (default: 12000).",
                "default": 12000,
            },
        },
        "required": ["task_id"],
    },
}

TASK_STOP_SCHEMA = {
    "name": "task_stop",
    "description": "Stop a running background task (SIGTERM → wait 3s → SIGKILL).",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID returned by task_create.",
            },
        },
        "required": ["task_id"],
    },
}

TASK_LIST_SCHEMA = {
    "name": "task_list",
    "description": "List background tasks, optionally filtered by status.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "running", "completed", "failed", "killed"],
                "description": "Filter by status. Omit to list all tasks.",
            },
        },
    },
}


def _task_create(args: dict[str, Any], **kwargs) -> str:
    """Create and start a background task."""
    command = args.get("command", "")
    description = args.get("description", "")

    if not isinstance(command, str) or not command.strip():
        return json.dumps({"success": False, "error": "Command is required."})
    if not isinstance(description, str):
        description = ""

    mgr = get_task_manager()
    task_cfg = kwargs.get("config", {}).get("tasks", {})
    max_concurrent = task_cfg.get("max_concurrent", 4)
    running = sum(1 for task in mgr.list_tasks(status="running"))
    if isinstance(max_concurrent, int) and max_concurrent > 0 and running >= max_concurrent:
        return json.dumps(
            {"success": False, "error": f"Maximum of {max_concurrent} concurrent tasks reached."}
        )
    workspace = kwargs.get("workspace")
    cwd = str(workspace) if workspace is not None else None
    task_id = mgr.create_shell_task(command, description, cwd=cwd)
    return json.dumps(
        {
            "success": True,
            "task_id": task_id,
            "description": description or command[:80],
        },
        indent=2,
    )


def _task_status(args: dict[str, Any], **kwargs) -> str:
    """Check the status of a background task."""
    task_id = args.get("task_id", "")
    if not isinstance(task_id, str) or not task_id.strip():
        return json.dumps({"success": False, "error": "task_id is required."})

    mgr = get_task_manager()
    task = mgr.get_task(task_id)
    if not task:
        return json.dumps({"success": False, "error": f"Task '{task_id}' not found."})

    return json.dumps(
        {
            "task_id": task.id,
            "type": task.type,
            "status": task.status,
            "description": task.description,
            "command": task.command,
            "pid": task.pid,
            "return_code": task.return_code,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "ended_at": task.ended_at,
            "metadata": task.metadata,
        },
        indent=2,
    )


def _task_output(args: dict[str, Any], **kwargs) -> str:
    """Read the output of a background task."""
    task_id = args.get("task_id", "")
    if not isinstance(task_id, str) or not task_id.strip():
        return json.dumps({"success": False, "error": "task_id is required."})

    raw_max_bytes = args.get("max_bytes", 12000)
    max_bytes = raw_max_bytes if isinstance(raw_max_bytes, int) and raw_max_bytes >= 1 else 12000
    max_allowed = kwargs.get("config", {}).get("tasks", {}).get("max_output_bytes", 100000)
    if isinstance(max_allowed, int):
        max_bytes = min(max_bytes, max_allowed)
    mgr = get_task_manager()
    output = mgr.read_task_output(task_id, max_bytes=max_bytes)
    return output


def _task_stop(args: dict[str, Any], **kwargs) -> str:
    """Stop a running background task."""
    task_id = args.get("task_id", "")
    if not isinstance(task_id, str) or not task_id.strip():
        return json.dumps({"success": False, "error": "task_id is required."})

    mgr = get_task_manager()
    task = mgr.get_task(task_id)
    if not task:
        return json.dumps({"success": False, "error": f"Task '{task_id}' not found."})
    result = mgr.stop_task(task_id)
    return json.dumps({"success": True, "message": result}, indent=2)


def _task_list(args: dict[str, Any], **kwargs) -> str:
    """List background tasks."""
    status = args.get("status")
    mgr = get_task_manager()
    tasks = mgr.list_tasks(status=status)

    return json.dumps(
        {
            "tasks": [
                {
                    "task_id": t.id,
                    "type": t.type,
                    "status": t.status,
                    "description": t.description,
                    "command": t.command[:100] if t.command else "",
                    "pid": t.pid,
                    "return_code": t.return_code,
                }
                for t in tasks
            ],
            "count": len(tasks),
        },
        indent=2,
    )


registry.register(
    name="task_create",
    toolset="tasks",
    schema=TASK_CREATE_SCHEMA,
    handler=_task_create,
    emoji="🚀",
)

registry.register(
    name="task_status",
    toolset="tasks",
    schema=TASK_STATUS_SCHEMA,
    handler=_task_status,
    emoji="📊",
)

registry.register(
    name="task_output",
    toolset="tasks",
    schema=TASK_OUTPUT_SCHEMA,
    handler=_task_output,
    emoji="📄",
)

registry.register(
    name="task_stop",
    toolset="tasks",
    schema=TASK_STOP_SCHEMA,
    handler=_task_stop,
    emoji="🛑",
)

registry.register(
    name="task_list",
    toolset="tasks",
    schema=TASK_LIST_SCHEMA,
    handler=_task_list,
    emoji="📋",
)
