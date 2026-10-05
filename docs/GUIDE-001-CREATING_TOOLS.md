# Creating Custom Tools

**Status:** ✅ Active  
**Last Updated:** October 2026  
**Type:** GUIDE (Developer Reference)

> Tools are the primary way Nova takes action in the world — running commands, reading files, calling APIs, and more. This guide walks you through building your own tools from scratch.

---

## How Tools Work

Every tool is a Python module in `nova/tools/` that:

1. Defines a **JSON schema** describing the tool's name, purpose, and parameters
2. Implements a **handler function** that executes the tool and returns a string
3. Calls `registry.register()` at module level to self-register

The registry is a global singleton. When `discover_builtin_tools()` runs at agent startup, it imports every module in `nova/tools/`, which triggers the `registry.register()` calls. No manual wiring needed.

### What the agent sees

The agent receives two representations of each tool:

- **Compact bullet** in the system prompt: `- my_tool: One-line description` (for efficient tokenization)
- **Full JSON schema** sent to the API: used by the model to construct valid tool calls

---

## Minimal Example

Here is the smallest possible tool — a "hello world" that echoes its input:

```python
# nova/tools/hello.py
from nova.tools.registry import registry

HELLO_SCHEMA = {
    "name": "hello",
    "description": "Echo a greeting message.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The name to greet.",
            },
        },
        "required": ["name"],
    },
}


def _hello(args: dict, **kwargs) -> str:
    name = args.get("name", "world")
    return f"Hello, {name}!"


registry.register(
    name="hello",
    toolset="custom",
    schema=HELLO_SCHEMA,
    handler=_hello,
    emoji="👋",
)
```

Then add it to `discover_builtin_tools()` in `nova/tools/registry.py`:

```python
tool_modules = [
    # ... existing modules ...
    "nova.tools.hello",  # ← add this line
]
```

That's it. Restart `nova chat` and the agent can call `hello(name="Nova")`.

---

## Read-Only vs Mutating Tools

Each tool declares whether it is **read-only** with `is_read_only` at registration. Tools that omit it are **mutating**. The flag is the only source of this classification for the permission system and parallel dispatch:

**Read-only tools** (never need confirmation):
- `read_file`, `search_files`, `web_search`
- `skills_list`, `skill_view`

**Mutating tools** (require confirmation in `ask` mode):
- `write_file`, `patch_file`, `terminal`
- `skill_manage`, `wiki`, `delegate_task`
- `web_crawl`, `web_extract` (they look read-only but cost credits per page), `web_parse` (uploads local file bytes)

Read-only tools are approved without confirmation and may run in parallel, so only set the flag on tools that change nothing (local or remote). To mark your custom tool as read-only:

```python
registry.register(
    name="my_read_tool",
    toolset="custom",
    schema=MY_READ_TOOL_SCHEMA,
    handler=_my_read_tool,
    is_read_only=True,  # ← Mark as read-only
)
```

For calls that must be confirmed even in `auto` mode, pass `always_confirm`: a function that receives the call's arguments (and the agent's `wiki`) and returns a short reason when confirmation is needed, or `None` otherwise.

See [docs/GUIDE-008-PERMISSIONS.md](GUIDE-008-PERMISSIONS.md) for details on the permission system.

---

## Hook Integration

Every tool call fires `pre_tool_call` and `post_tool_call` hooks automatically. Your tool doesn't need to do anything — hooks are fired by the registry's `dispatch()` method.

```python
# Hooks fire automatically when the agent calls your tool:
# 1. pre_tool_call(tool_name="my_tool", args={...})
# 2. Your handler executes
# 3. post_tool_call(tool_name="my_tool", args={...}, result="...")
```

See [docs/GUIDE-006-HOOKS.md](GUIDE-006-HOOKS.md) for details on registering hook callbacks.

---

## Handler Signature

Every handler must follow this signature:

```python
def _my_tool(args: dict, **kwargs) -> str: ...
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `args` | `dict` | The parsed tool call arguments from the model |
| `**kwargs` | | Injected context — see table below |

### Injected kwargs

The agent passes these keyword arguments to every tool handler:

| Key | Type | Description |
|-----|------|-------------|
| `config` | `dict` | Full agent config (budgets, model, etc.) |
| `wiki` | `WikiMemory \| None` | Wiki memory store (None if wiki disabled) |
| `session_store` | `SessionStore` | SQLite session storage |
| `agent` | `NovaAgent` | The agent instance itself |

Access them like this:

```python
def _my_tool(args: dict, **kwargs) -> str:
    config = kwargs.get("config", {})
    wiki = kwargs.get("wiki")
    agent = kwargs.get("agent")
    ...
```

---

## Return Values

**Handlers return a string** (or a `ToolResult`, below). The string is injected into the conversation as a tool result message. The model reads it and decides what to do next.

A string starting with `Error:` marks the call as failed. Failures whose text looks transient (timeouts, connection resets, rate limits) are retried automatically for read-only tools.

```python
# ✅ Good — plain text
return "File written successfully."

# ✅ Good — JSON for structured data
import json

return json.dumps({"status": "ok", "count": 42})

# ✅ Good — error message
return "Error: File not found at /path/to/file."

# ❌ Bad — never return None or non-string
return None
return {"status": "ok"}  # dict, not string
```

### Typed results

When the `Error:` convention is not precise enough, return a `ToolResult`. The agent uses its `status` and `retryable` flag directly, and the model sees `content`:

```python
from nova.tools.result import ToolResult

return ToolResult("failed", "Upstream busy; returned 40 of 100 rows.", retryable=True)
```

Every call ends as a `ToolResult` inside the agent (`nova/tool_executor.py`), with status `completed`, `failed`, `denied`, or `interrupted`. Traces, observability, and UI callbacks use that status rather than the text.

### Budget enforcement

Tool results are automatically truncated by the agent to `budgets.tool_result_max_chars` (default 8000 chars). You don't need to truncate yourself, but for very large outputs it's better to truncate early with a meaningful head/tail:

```python
def _truncate(text: str, max_chars: int = 6000) -> str:
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = int(max_chars * 0.2)
    return f"{text[:head]}\n\n[...{len(text) - head - tail:,} chars truncated...]\n\n{text[-tail:]}"
```

---

## JSON Schema Reference

The schema follows the [OpenAI function calling format](https://platform.openai.com/docs/guides/function-calling). Here is a complete example with all common field types:

```python
MY_TOOL_SCHEMA = {
    "name": "my_tool",
    "description": (
        "One or two sentences describing what the tool does and when to use it. "
        "Be specific — the model uses this to decide whether to call the tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # String parameter
            "path": {
                "type": "string",
                "description": "Absolute path to the file.",
            },
            # Integer with default
            "limit": {
                "type": "integer",
                "description": "Maximum number of results (default: 10).",
                "default": 10,
            },
            # Boolean
            "verbose": {
                "type": "boolean",
                "description": "Include extra detail in the output.",
            },
            # Enum (fixed choices)
            "mode": {
                "type": "string",
                "enum": ["read", "write", "append"],
                "description": "File access mode.",
            },
            # Array of strings
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tags to apply.",
            },
        },
        "required": ["path"],  # Only list truly required params
    },
}
```

### Schema writing tips

- **Description quality matters** — the model reads it to decide when and how to call the tool. Be specific about what the tool does, what it returns, and any gotchas.
- **Mark only truly required params** — optional params with sensible defaults should not be in `required`.
- **Use `enum` for fixed choices** — prevents the model from inventing invalid values.
- **Avoid overly long descriptions** — they count against your system prompt token budget.

---

## Real-World Example: GitHub API Tool

Here is a realistic tool that calls an external API, reads config, and handles errors:

```python
# nova/tools/github.py
"""GitHub tool — query GitHub repositories and issues."""

import json
import logging
import os
from typing import Any

import httpx

from nova.tools.registry import registry

logger = logging.getLogger(__name__)

GITHUB_SCHEMA = {
    "name": "github",
    "description": (
        "Query GitHub: list issues, get repo info, or search repositories. "
        "Requires GITHUB_TOKEN environment variable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_issues", "get_repo", "search_repos"],
                "description": "The GitHub action to perform.",
            },
            "repo": {
                "type": "string",
                "description": "Repository in 'owner/name' format (e.g. 'octocat/Hello-World').",
            },
            "query": {
                "type": "string",
                "description": "Search query (for search_repos action).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results to return (default: 10).",
                "default": 10,
            },
        },
        "required": ["action"],
    },
}

_GITHUB_API = "https://api.github.com"


def _check_requirements() -> bool:
    """Return True if GITHUB_TOKEN is set."""
    return bool(os.environ.get("GITHUB_TOKEN"))


def _github(args: dict[str, Any], **kwargs) -> str:
    """Handle GitHub tool calls."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return "Error: GITHUB_TOKEN environment variable is not set."

    action = args.get("action", "")
    repo = args.get("repo", "")
    query = args.get("query", "")
    limit = int(args.get("limit", 10))

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            if action == "get_repo":
                if not repo:
                    return "Error: 'repo' is required for get_repo action."
                resp = client.get(f"{_GITHUB_API}/repos/{repo}", headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return json.dumps(
                    {
                        "name": data["full_name"],
                        "description": data.get("description"),
                        "stars": data["stargazers_count"],
                        "forks": data["forks_count"],
                        "open_issues": data["open_issues_count"],
                        "url": data["html_url"],
                    },
                    indent=2,
                )

            elif action == "list_issues":
                if not repo:
                    return "Error: 'repo' is required for list_issues action."
                resp = client.get(
                    f"{_GITHUB_API}/repos/{repo}/issues",
                    headers=headers,
                    params={"per_page": limit, "state": "open"},
                )
                resp.raise_for_status()
                issues = resp.json()
                return json.dumps(
                    [
                        {"number": i["number"], "title": i["title"], "url": i["html_url"]}
                        for i in issues
                    ],
                    indent=2,
                )

            elif action == "search_repos":
                if not query:
                    return "Error: 'query' is required for search_repos action."
                resp = client.get(
                    f"{_GITHUB_API}/search/repositories",
                    headers=headers,
                    params={"q": query, "per_page": limit},
                )
                resp.raise_for_status()
                items = resp.json().get("items", [])
                return json.dumps(
                    [
                        {
                            "name": r["full_name"],
                            "stars": r["stargazers_count"],
                            "url": r["html_url"],
                        }
                        for r in items
                    ],
                    indent=2,
                )

            else:
                return f"Error: Unknown action '{action}'."

    except httpx.HTTPStatusError as e:
        return f"Error: GitHub API returned {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        logger.error("GitHub tool failed: %s", e)
        return f"Error: {e}"


registry.register(
    name="github",
    toolset="github",
    schema=GITHUB_SCHEMA,
    handler=_github,
    check_fn=_check_requirements,
    emoji="🐙",
)
```

---

## Using Config in a Tool

Read agent config via `kwargs.get("config", {})`:

```python
def _my_tool(args: dict, **kwargs) -> str:
    config = kwargs.get("config", {})

    # Read a budget
    max_chars = config.get("budgets", {}).get("tool_result_max_chars", 8000)

    # Read a custom config section (add to config.yaml)
    my_config = config.get("my_tool", {})
    api_key = my_config.get("api_key", "")

    ...
```

Add your tool's config section to `config.yaml`:

```yaml
my_tool:
  api_key: "${MY_TOOL_API_KEY}"
  max_results: 10
```

And add the default to `DEFAULT_CONFIG` in `nova/config.py`:

```python
DEFAULT_CONFIG = {
    # ... existing config ...
    "my_tool": {
        "api_key": "",
        "max_results": 10,
    },
}
```

---

## Using Wiki Memory in a Tool

Access the wiki memory store via `kwargs.get("wiki")`:

```python
def _my_tool(args: dict, **kwargs) -> str:
    wiki = kwargs.get("wiki")
    if wiki is None:
        return "Wiki memory is disabled."

    # Search for relevant notes
    results = wiki.search("user preferences")

    # Write a new note
    wiki.write("Core/Preferences", "User prefers JSON output", tags=["preferences"])

    # Append to an existing note
    wiki.append("Projects/nova", "Added new feature X")

    ...
```

See `nova/wiki_memory.py` for the full `WikiMemory` API.

---

## `registry.register()` Parameters

```python
registry.register(
    name="my_tool",  # Tool name — must match schema["name"]
    toolset="custom",  # Logical group (used for filtering)
    schema=MY_TOOL_SCHEMA,  # Full JSON schema dict
    handler=_my_tool,  # Callable: (args: dict, **kwargs) -> str | ToolResult
    check_fn=None,  # Optional: (config) -> bool — return False to hide the tool
    emoji="🔧",  # Displayed in tool call output
    is_read_only=False,  # True: no confirmation needed, may run in parallel
    verifier=None,  # Optional: (args, result, agent=...) -> VerificationResult
    always_confirm=None,  # Optional: (args, wiki=...) -> reason | None
)
```

### `check_fn` — conditional registration

Use `check_fn` to hide a tool from the model when requirements aren't met (e.g. missing API key). `get_definitions()` calls it with the agent's config on every request:

```python
def _check(config: dict) -> bool:
    return bool(os.environ.get("MY_API_KEY"))

registry.register(
    name="my_tool",
    check_fn=_check,
    ...
)
```


---

## Adding to `discover_builtin_tools()`

After creating your tool file, add it to the import list in `nova/tools/registry.py`:

```python
def discover_builtin_tools(config: dict | None = None):
    tool_modules = [
        "nova.tools.terminal",
        "nova.tools.file_ops",
        "nova.tools.search_files",
        "nova.tools.web",
        "nova.tools.skills_tool",
        "nova.tools.wiki_tool",
        "nova.tools.my_tool",  # ← add your module here
    ]
    ...
```

---

## Writing Tests

Follow the pattern in `tests/test_tools.py`. Use dependency injection — pass mock `config`, `wiki`, and `agent` via kwargs:

```python
# tests/test_tools.py (add to existing file) or tests/test_my_tool.py

from nova.tools.my_tool import _my_tool


def test_my_tool_basic():
    result = _my_tool({"arg": "hello"}, config={}, wiki=None, agent=None)
    assert "hello" in result


def test_my_tool_missing_required_arg():
    result = _my_tool({}, config={}, wiki=None, agent=None)
    assert "Error" in result


def test_my_tool_reads_config():
    config = {"my_tool": {"max_results": 5}}
    result = _my_tool({"arg": "test"}, config=config, wiki=None, agent=None)
    assert result is not None
```

---

## Checklist

Before shipping a new tool:

- [ ] Schema `name` matches `registry.register(name=...)`
- [ ] All required parameters listed in `"required"`
- [ ] Handler always returns a `str` (never `None` or a dict)
- [ ] Errors return `"Error: ..."` strings, not exceptions
- [ ] Large outputs are truncated with head/tail pattern
- [ ] Module added to `discover_builtin_tools()` in `registry.py`
- [ ] Tests written and passing (`pytest tests/`)
- [ ] Lint clean (`ruff check .`)
- [ ] Type hints on handler function

---

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Creating Skills](GUIDE-002-CREATING_SKILLS.md) | Add knowledge domains via markdown instead of code |
| [Customizing Nova](GUIDE-003-CUSTOMIZING.md) | Full tools reference and config |
| [Permissions](GUIDE-008-PERMISSIONS.md) | Mark tools as read-only or mutating |
| [Hooks](GUIDE-006-HOOKS.md) | Lifecycle callbacks that fire around tool calls |
| [Background Tasks](GUIDE-004-BACKGROUND_TASKS.md) | Build tools that run long commands asynchronously |
| [CONTRIBUTING](../CONTRIBUTING.md) | PR workflow and code quality standards |
