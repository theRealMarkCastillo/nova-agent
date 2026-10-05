# Permission System

**Status:** ✅ Active  
**Last Updated:** October 2026  
**Type:** GUIDE (Feature Reference)

> Nova Agent includes a configurable permission system that controls tool execution through a defense-in-depth cascade. This prevents accidental or malicious actions while maintaining flexibility.

## Quick Start

Add to your `config.yaml`:

```yaml
permissions:
  mode: "ask"    # "auto" (allow all) or "ask" (confirm mutating tools)
```

## Permission Modes

| Mode | Behavior |
|------|----------|
| `auto` | All tools execute without confirmation |
| `ask` | Read-only tools execute freely; mutating tools require confirmation (this is the **default**) |

In `ask` mode, mutating tool calls prompt for interactive confirmation (`Allow? [y/N]`) both in the TUI and in the plain CLI. If confirmation is unavailable (e.g., no TTY), the tool call is denied rather than silently auto-approved.

## Defense-in-Depth Cascade

Every tool call is evaluated through these checks, in order:

1. **Built-in sensitive path protection** — Cannot be overridden. Blocks access to:
   - `~/.ssh/*`, `~/.aws/credentials`, `~/.aws/config`
   - `~/.config/gcloud/*`, `~/.azure/*`, `~/.gnupg/*`
   - `~/.docker/config.json`, `~/.kube/config`
   - `~/.nova/credentials.json`

2. **Explicit tool deny list** — Tools the agent can never use:
   ```yaml
   permissions:
     denied_tools: ["terminal", "write_file"]
   ```

3. **Explicit tool allow list** — Tools that bypass confirmation in `ask` mode:
   ```yaml
   permissions:
     allowed_tools: ["patch_file"]
   ```

4. **Path-level rules** — fnmatch patterns for file access control:
   ```yaml
   permissions:
     path_rules:
       - pattern: "/etc/*"
         allow: false
       - pattern: "/tmp/*"
         allow: true
   ```
   A matching `allow: false` rule denies the call. A matching `allow: true` rule
   skips confirmation for file tools, but not for shell commands: for `terminal`
   the path is only the working directory, so command deny patterns and the
   permission mode still apply.

5. **Command deny patterns** — Shell commands that are always blocked:
   ```yaml
   permissions:
     denied_commands:
       - "rm -rf /"
       - "rm -rf /*"
       - ":(){*};:*"        # Fork bomb
       - "mkfs*"
       - "shutdown*"
   ```

6. **Permission mode** — Final check based on `auto` vs `ask` mode

## Read-Only vs Mutating Tools

Tools are classified as read-only or mutating:

**Read-only** (never need confirmation):
- `read_file`, `search_files`, `list_files`, `search_sessions`
- `web_search`, `web_scrape`, `web_map`, `web_dev_search`, `web_usage`
- `http_get`
- `skills_list`, `skill_view`, `skill_export`
- `task_status`, `task_list`, `task_output`

**Mutating** (require confirmation in `ask` mode):
- `write_file`, `patch_file`, `terminal`
- `skill_manage`, `wiki`, `delegate_task`
- `http_post`, `http_put`, `http_delete`
- `task_create`, `task_stop`
- `web_crawl`, `web_extract` — every page they process costs Firecrawl credits and starts a server-side job
- `web_parse` — uploads local file contents to a third-party API

## Tool-Level Permission Checking

The terminal tool also checks denied commands independently:

```yaml
permissions:
  denied_commands:
    - "rm -rf /"
    - "curl *"
    - "wget *"
```

File operation tools (`read_file`, `write_file`, `patch_file`) check sensitive paths and path rules.

## Configuration Reference

```yaml
permissions:
  mode: "ask"                     # "ask" (confirm mutating tools, default) or "auto"
  denied_tools: []                # Tools the agent can never use
  allowed_tools: []               # Tools that bypass confirmation
  denied_commands: []             # Shell command patterns (fnmatch)
  path_rules: []                  # Path-level rules
    # - pattern: "/etc/*"
    #   allow: false
```

---

## Opinionated Profiles

Three ready-to-use configs for common situations.

### Developer workstation — trust the agent, move fast

```yaml
permissions:
  mode: "auto"
  denied_tools: []
  denied_commands:
    - "rm -rf /"
    - "rm -rf /*"
    - ":(){*};:*"
    - "mkfs*"
    - "shutdown*"
    - "reboot*"
```

All tools run without confirmation. Catastrophic shell commands are blocked. Good for a personal dev machine where you're watching the session.

### Shared or sensitive environment — confirm before writing

```yaml
permissions:
  mode: "ask"
  denied_tools: []
  allowed_tools:
    - "read_file"
    - "search_files"
    - "web_search"
    - "skills_list"
    - "skill_view"
  denied_commands:
    - "rm -rf /"
    - "rm -rf /*"
    - ":(){*};:*"
    - "mkfs*"
    - "shutdown*"
    - "curl *"
    - "wget *"
  path_rules:
    - pattern: "/etc/*"
      allow: false
    - pattern: "/var/*"
      allow: false
```

Read-only tools run freely. Anything that writes, executes, or modifies requires confirmation. Network commands blocked.

### Read-only audit — no writes at all

```yaml
permissions:
  mode: "auto"
  denied_tools:
    - "terminal"
    - "write_file"
    - "patch_file"
    - "skill_manage"
    - "wiki"
    - "delegate_task"
    - "task_create"
    - "task_stop"
```

Nova can read, search, and answer questions but cannot modify anything. Useful for code review sessions, audits, or onboarding.

---

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Customizing Nova](GUIDE-003-CUSTOMIZING.md) | Full configuration reference |
| [Hooks](GUIDE-006-HOOKS.md) | Register callbacks that fire before/after permission checks |
| [Creating Tools](GUIDE-001-CREATING_TOOLS.md) | Mark tools as read-only or mutating |
| [SECURITY.md](../SECURITY.md) | Reporting security vulnerabilities |
