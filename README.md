# Nova Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/eidolonlabs-ai/nova-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/eidolonlabs-ai/nova-agent/actions/workflows/ci.yml)

**Status:** 🟡 Active — release hardening
**Last Updated:** August 2026
**By:** [Eidolon Labs LLC](https://github.com/eidolonlabs-ai)

> A lightweight personal AI agent with explicit token budgets and smart context management.


<img width="1324" height="444" alt="image" src="https://github.com/user-attachments/assets/8e7bfe5d-5b88-4ef4-a587-4a295bcb1aa6" />


<img width="1141" height="626" alt="image" src="https://github.com/user-attachments/assets/98df7b6c-84d8-4fb8-9a42-5af226f2ab16" />


## Design Philosophy

Nova Agent combines the best patterns from two mature agent frameworks:

- **From Hermes-Agent**: Tool registry pattern, skills system, context file discovery, session storage, prompt injection scanning
- **From OpenClaw**: Explicit token budgets, prompt mode gating, head/tail truncation, turn limits, two-tier tool descriptions

## Features

| Feature | Status | Details |
|---------|--------|---------|
| Explicit token budgets | ✅ Active | Budgets at every layer — system prompt, skills, context, history |
| Smart context management | ✅ Active | Head/tail truncation (70/20), deterministic compaction, searchable history |
| Automatic retry | ✅ Active | Exponential backoff and jitter for transient API errors |
| Permission system | ✅ Active | Defense-in-depth cascade (sensitive paths, deny/allow lists) |
| Hook/callback system | ✅ Active | Lifecycle events: pre/post tool call, LLM call, session start/end |
| Cost tracking | ✅ Active | Per-model pricing and dollar cost estimation |
| Background tasks | ✅ Active | Fire-and-forget shell execution with status tracking |
| MCP integration | ✅ Active | Connect configured stdio, HTTP, and SSE Model Context Protocol servers |
| Tool registry | ✅ Active | Extensible tools with JSON schema definitions |
| Skills system | ✅ Active | SKILL.md files with starter skills for coding, git, file editing |
| Context file discovery | ✅ Active | Global personality (SOUL.md) + NOVA.md, AGENTS.md with injection scanning |
| Session storage | ✅ Active | SQLite with FTS5 full-text search |
| Prompt mode gating | ✅ Active | Full mode for main agent, minimal for sub-agents |
| OpenRouter API | ✅ Active | 100+ models via OpenRouter |
| Streaming responses | ✅ Active | Rich terminal UI |
| Wiki memory (Obsidian-compatible) | ✅ Active | Persistent markdown notes with `[[wikilinks]]`, tags, `Core/` auto-inject |
| Web tools (Firecrawl) | ✅ Active | Search, scrape, map, crawl, extract, document parsing — see [GUIDE-015-WEB_TOOLS](docs/GUIDE-015-WEB_TOOLS.md) |
| Harness verification | ✅ Active | Tool postcondition verification and structured run traces — see [SPEC-001-HARNESS_ENGINEERING](docs/SPEC-001-HARNESS_ENGINEERING.md) |
| Langfuse observability | ✅ Active | Optional telemetry for runs, LLM calls, tools, policy, verification, timing, and usage |

## Built-in Tools

| Tool | Description |
|------|-------------|
| `terminal` | Execute shell commands with timeout and output limits |
| `read_file` | Read files with line range support |
| `write_file` | Write/overwrite files with atomic saves |
| `patch_file` | Search/replace patches for targeted edits |
| `search_files` | Grep/regex search across project files |
| `list_files` | List files/directories under a root |
| `git_status` | Show working tree status |
| `git_diff` | Show diffs (working tree, staged, or between revisions) |
| `git_log` | Show commit history |
| `git_show` | Show a commit's changes |
| `git_blame` | Show line-by-line commit attribution for a file |
| `http_get` | Make a GET request with SSRF-hardened URL/DNS validation |
| `http_post` | Make a POST request (requires confirmation) |
| `http_put` | Make a PUT request (requires confirmation) |
| `http_delete` | Make a DELETE request (requires confirmation) |
| `web_search` | Search the web, optionally scraping each result |
| `web_scrape` | Scrape 1–10 URLs to clean markdown (handles JS and PDFs) |
| `web_map` | Discover URLs on a site via sitemap + link graph |
| `web_crawl` | Crawl a site — job-based: `start`, `status`, `cancel`, `errors` |
| `web_extract` | LLM-extract structured JSON from pages — job-based: `start`, `status` |
| `web_parse` | Convert a local PDF/DOCX/XLSX/PPTX to markdown (requires confirmation) |
| `web_dev_search` | Search code, docs, issues and PRs across GitHub repos |
| `web_usage` | Report remaining Firecrawl credits, tokens and concurrency |
| `skills_list` | List all available skills by category |
| `skill_view` | Load a skill's full instructions |
| `skill_manage` | Create, update, or delete skills |
| `skill_export` | Export a skill as a shareable bundle |
| `wiki` | Manage Obsidian-compatible wiki notes: write, append, read, search, list, delete, maintenance |
| `delegate_task` | Spawn a sub-agent to handle an isolated task (opt-in) |
| `task_create` | Start a background shell command |
| `task_status` | Check a background task's status |
| `task_output` | Read the tail of a task's log |
| `task_stop` | Stop a running background task |
| `task_list` | List all background tasks |
| `read_session` | Read messages from a past session, optionally centered on an index |
| `search_sessions` | Full-text search across session titles/summaries |
| `search_messages` | Full-text search across individual messages |
| `mcp_read_resource` | Read a resource exposed by a connected MCP server (when MCP is configured) |

## Installation

### For End Users

**One-line install:**

```bash
curl -fsSL https://raw.githubusercontent.com/eidolonlabs-ai/nova-agent/main/scripts/install.sh | bash
```

This clones Nova to `~/.nova/nova-agent/`, creates a virtual environment, installs dependencies, and adds the `nova` command to your PATH.

**Configure:**

```bash
nova setup
```

The interactive wizard walks you through setting your OpenRouter API key and choosing a model.

**Update:**

```bash
nova update
```

Pulls the latest code and reinstalls dependencies.

---

### For Developers

**Clone and install:**

```bash
git clone https://github.com/eidolonlabs-ai/nova-agent.git
cd nova-agent
uv sync --all-extras
```

*(Alternatively, traditional venv: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`)*

**Run tests:**

```bash
uv run ruff check . && uv run mypy nova/ && uv run pytest
```

**Run:**

```bash
uv run nova chat
```

Nova can also be launched as an Agent Client Protocol (ACP) stdio server:

```bash
nova acp
```

ACP clients communicate with the process over stdin/stdout; diagnostics are
kept off the protocol stream. The current implementation supports session
creation/loading, text prompts, streamed assistant and tool-call updates, and
cancellation.

Developer installs use the repo's local `.venv` and `config.yaml` in the project root. Changes to source code take effect immediately (editable install).

### Optional Firecrawl web tools

The eight `web_*` tools require the Firecrawl SDK and an API key:

```bash
pip install -e ".[web]"            # or: pip install 'nova-agent[web]'
export FIRECRAWL_API_KEY=fc-...    # get one at https://firecrawl.dev
```

Without the SDK or a key the web tools are **not registered**, so they cost
nothing in tool-schema tokens. See [GUIDE-015-WEB_TOOLS](docs/GUIDE-015-WEB_TOOLS.md).

### Optional Langfuse observability

Install the optional integration with `pip install -e ".[observability]"`. Telemetry is disabled by default. Enable it in `~/.nova/config.yaml` and provide `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`; `LANGFUSE_BASE_URL` is optional and defaults to `https://cloud.langfuse.com`. Input/output capture is opt-in. See [Customizing Nova](docs/GUIDE-003-CUSTOMIZING.md#langfuse-observability) and [SPEC-001](docs/SPEC-001-HARNESS_ENGINEERING.md#52-optional-langfuse-telemetry-sink).

## Configuration

Nova loads config in layers (later overrides earlier):

1. **Built-in defaults** — sensible fallbacks for everything
2. **`~/.nova/config.yaml`** — global config (API key, model, budgets)
3. **`config.yaml`** in current directory — project-specific overrides

### For End Users

Run the interactive wizard:

```bash
nova setup
```

Or create `~/.nova/config.yaml` manually:

```yaml
llm:
  api_key: "sk-..."           # or set LLM_API_KEY env var
  model: "qwen/qwen3.6-flash" # Fast, capable, affordable
  base_url: "https://openrouter.ai/api/v1"  # any OpenAI-compatible endpoint
```

### For Developers

Copy the example config to the project root:

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` in the repo root. This overrides the global config for this project only.

### Default Files (Recommended)

Copy starter files to your Nova home directory:

```bash
mkdir -p ~/.nova/skills
cp config/SOUL.md.example ~/.nova/SOUL.md
cp -r config/skills/* ~/.nova/skills/
```

See [docs/GUIDE-003-CUSTOMIZING.md](docs/GUIDE-003-CUSTOMIZING.md) for the full customization guide.

## Sub-Agent Delegation

Nova supports spawning sub-agents to handle isolated or parallelizable tasks. Sub-agents run in a worker thread with their own context, budget, and timeout.

**Enable in `config.yaml`:**

```yaml
delegation:
  enabled: true
  max_spawn_depth: 2        # root → child → grandchild (leaf)
  default_timeout_seconds: 60
  subagent_budgets:
    max_iterations: 30
```

**How it works:**

1. The root agent (orchestrator) calls `delegate_task(task="...")` as a tool
2. A child `NovaAgent` is spawned in a worker thread with a fresh conversation
3. The child runs with a minimal system prompt (no skills index, no context files) focused on the task
4. Results are returned as JSON and aggregated into the parent's response

**Parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `task` | required | Self-contained task description |
| `label` | auto | Short label for logging |
| `model` | parent's | Override model (e.g. cheaper model for simple tasks) |
| `timeout_seconds` | 60 | Hard timeout (max 300s) |
| `context_mode` | `isolated` | `isolated` (fresh) or `fork` (inherit parent transcript) |

**Depth & roles:**

- **Orchestrator** (depth < `max_spawn_depth`): can call `delegate_task`
- **Leaf** (depth ≥ `max_spawn_depth`): cannot delegate further

Check delegation state with `/status` in the chat UI.

## Usage

```bash
# Interactive chat
nova chat

# One-shot query
nova ask "What's the weather today?"

# Check session history
nova sessions

# Clear current session
nova reset

# Update to latest version (end users)
nova update
```

## Slash Commands (in `nova chat`)

While chatting, use slash commands to control the session. Press `/` to see the command menu with autocomplete:

| Command | Alias | Description |
|---------|-------|-------------|
| `/new` | `/reset` | Start a fresh session |
| `/history` | | Show conversation history |
| `/status` | | Show session info (tokens, model, delegation state) |
| `/sessions` | | List recent sessions |
| `/resume [id]` | | Resume a previous session |
| `/model [name]` | | Show or switch model |
| `/tools` | | List available tools |
| `/skills [list\|view]` | | List or view skills |
| `/undo` | | Remove the last exchange |
| `/compact` | | Compress context to last 4 messages |
| `/copy` | | Copy last response to clipboard |
| `/usage` | | Show token usage and cost for this session |
| `/help` | | Show all commands |
| `/quit` | `/exit`, `/q` | Exit Nova |

**How to use**:
1. Press `/` → Shows menu of all commands
2. Type to filter (e.g., `/st` → `/status`)
3. Use arrow keys to scroll
4. Press Tab or Enter to select
5. Press Escape to cancel

## Project Structure

```
nova/
  __init__.py
  __main__.py       # Package entry point
  agent.py          # Main agent loop with tool calling & streaming
  cli.py            # CLI entry point (chat, ask, sessions, reset)
  config.py         # YAML config loading with env var resolution
  context.py        # Context file discovery, budgets, truncation
  cost_tracker.py   # Dollar cost tracking with per-model pricing
  hooks.py          # Lifecycle hook/callback system
  mcp_client.py     # MCP (Model Context Protocol) — stdio, HTTP, SSE
  wiki_memory.py    # Obsidian-compatible wiki memory (markdown notes, [[wikilinks]], Core/ auto-inject)
  microcompact.py   # Cheap context compaction (no LLM call)
  model_metadata.py # Model context window sizes for 20+ models
  permissions.py    # Permission system with defense-in-depth cascade
  prompt.py         # System prompt assembly with mode gating
  retry.py          # API retry with exponential backoff and jitter
  session.py        # SQLite session storage with FTS5 search
  skills.py         # Skill discovery, frontmatter parsing, prompt gen
  tasks.py          # Background task manager
  tokens.py         # Token estimation via tiktoken
  tools/
    __init__.py
    registry.py     # Central tool registry with auto-discovery
    terminal.py     # Shell command execution with timeout
    file_ops.py     # read_file, write_file, patch_file
    search_files.py # Grep/regex search across project files
    web.py          # Firecrawl web tools (search/scrape/map/crawl/extract/parse)
    firecrawl_client.py # Firecrawl SDK client, error translation, formatting
    skills_tool.py  # skills_list, skill_view, skill_manage
    wiki_tool.py    # wiki tool (write/append/read/search/list/delete/maintenance)
    delegate_tool.py # delegate_task sub-agent spawning (opt-in)
    task_tools.py   # Background task tools (create/status/output/stop/list)
config/
  SOUL.md.example   # Agent personality template
  NOVA.md.example   # Project instructions template
  skills/           # starter skills (see config/skills/)
    python-coding/
    git-workflow/
    file-editing/
docs/
  GUIDE-003-CUSTOMIZING.md           # Comprehensive customization guide
  GUIDE-001-CREATING_TOOLS.md        # Developer guide: building custom tools
  GUIDE-002-CREATING_SKILLS.md       # Developer guide: building custom skills
  GUIDE-008-PERMISSIONS.md           # Permission system configuration
  GUIDE-006-HOOKS.md                 # Hook/callback system
  GUIDE-004-BACKGROUND_TASKS.md      # Background task management
  GUIDE-007-MCP_INTEGRATION.md       # MCP server integration
  GUIDE-005-COST_TRACKING.md         # Cost tracking and usage monitoring
  GUIDE-010-ROADMAP.md               # Project phases, timeline, and targets
  GUIDE-011-CONTEXT_COMPRESSION.md   # Three-tier context management strategy
  GUIDE-012-SESSION_MANAGEMENT.md    # SQLite sessions, FTS5 search, commands
  GUIDE-013-MEMORY_SYSTEM.md         # Obsidian-compatible wiki memory
  GUIDE-014-RETRY_AND_ERROR_HANDLING.md  # Retry logic and error classification
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all checks
ruff check .          # Lint
mypy nova/            # Type check
pytest                 # Tests and coverage (see CI for current counts)

# Full CI check
ruff check . && mypy nova/ && pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.

## Documentation

| Document | Type | Status | Purpose |
|----------|------|--------|---------|
| [Customizing Nova](docs/GUIDE-003-CUSTOMIZING.md) | GUIDE | ✅ Active | Config, SOUL.md, context files, wiki memory, sessions |
| [Creating Tools](docs/GUIDE-001-CREATING_TOOLS.md) | GUIDE | ✅ Active | Build custom tools with schemas, handlers, and tests |
| [Creating Skills](docs/GUIDE-002-CREATING_SKILLS.md) | GUIDE | ✅ Active | Write effective SKILL.md files for specialized knowledge |
| [Permissions](docs/GUIDE-008-PERMISSIONS.md) | GUIDE | ✅ Active | Permission system, defense-in-depth, config reference |
| [Hooks](docs/GUIDE-006-HOOKS.md) | GUIDE | ✅ Active | Lifecycle callbacks, audit logging, event system |
| [Background Tasks](docs/GUIDE-004-BACKGROUND_TASKS.md) | GUIDE | ✅ Active | Fire-and-forget execution, status tracking |
| [MCP Integration](docs/GUIDE-007-MCP_INTEGRATION.md) | GUIDE | ✅ Active | Connect to MCP servers (stdio, HTTP, SSE) |
| [Cost Tracking](docs/GUIDE-005-COST_TRACKING.md) | GUIDE | ✅ Active | Token usage, dollar cost estimation |
| [Roadmap](docs/GUIDE-010-ROADMAP.md) | GUIDE | ✅ Active | Project phases, timeline, and targets |
| [Context Compaction](docs/GUIDE-011-CONTEXT_COMPRESSION.md) | GUIDE | ✅ Active | Deterministic compaction and searchable historical retrieval |
| [Session Management](docs/GUIDE-012-SESSION_MANAGEMENT.md) | GUIDE | ✅ Active | SQLite sessions, FTS5 search, slash commands |
| [Memory System](docs/GUIDE-013-MEMORY_SYSTEM.md) | GUIDE | ✅ Active | Obsidian-compatible wiki memory: markdown notes, wikilinks, `Core/` auto-inject, maintenance |
| [Retry & Error Handling](docs/GUIDE-014-RETRY_AND_ERROR_HANDLING.md) | GUIDE | ✅ Active | Exponential backoff, error classification, retries |
| [Documentation Index](docs/DOCUMENTATION_INDEX.md) | INDEX | ✅ Active | Full inventory of all docs |

## License

MIT — see [LICENSE](LICENSE) for details.

© 2026 Eidolon Labs LLC

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.
For security issues, see [SECURITY.md](SECURITY.md).
