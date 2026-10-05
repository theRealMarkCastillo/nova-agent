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

In `ask` mode, outbound network tools (`http_*`, `web_*`) also require confirmation once untrusted content is in the conversation. See [Untrusted Content](#untrusted-content).

In `ask` mode, mutating tool calls prompt for interactive confirmation (`Allow? [y/N]`) both in the TUI and in the plain CLI. If confirmation is unavailable (e.g., no TTY), the tool call is denied rather than silently auto-approved.

## Defense-in-Depth Cascade

Every tool call is evaluated through these checks, in order:

1. **Built-in sensitive path protection**: configuration cannot turn this off. It blocks any path argument
   inside or naming:
   - the directories `.ssh`, `.aws`, `.gnupg`, `.azure`, `.kube`, `.docker`, `.terraform`, `.nova`, or `.config/gcloud`
   - the files `.netrc`, `.git-credentials`, `.npmrc`, or anything starting with `.env` (`.env`, `.env.local`, `.envrc`)

   The file tools enforce the same list, from one shared definition (`nova/tools/path_safety.py`).
   It applies to path arguments only: see [What This Does Not Guarantee](#what-this-does-not-guarantee).

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

## Always-Confirmed Changes

A few changes persist into every future session, so they need confirmation in every mode, even for tools in `allowed_tools`. Currently these are wiki changes that put content into every system prompt (see [GUIDE-013](GUIDE-013-MEMORY_SYSTEM.md#the-core-convention)). Tools declare these with the `always_confirm` registration hook.

## Untrusted Content

Output from `http_*`, `web_*`, and MCP tools comes from outside your control and
can contain instructions written to steer the agent (prompt injection). A common
goal of such instructions is to make the agent read a local file and send its
contents out, for example inside an `http_get` URL.

So in `ask` mode, once any of those tools has returned output in the session,
every later `http_*` or `web_*` call needs confirmation, including the normally
read-only ones. Local read-only tools such as `read_file` still run freely, and
the rule also applies when resuming a session that already contains such output.
Tools listed in `allowed_tools` and all tools in `auto` mode are not affected.

## What This Does Not Guarantee

The real boundary is **confirmation in `ask` mode** plus the **workspace
restriction** on file tools. The pattern-based checks above (sensitive paths,
command deny patterns, prompt-injection scanning) are guardrails. They catch
mistakes and obvious attacks, but they are not a sandbox:

- `terminal` runs arbitrary shell commands as your user. `cat ~/.ssh/id_rsa` or
  `python -c ...` reads anything you can read; sensitive path protection only
  inspects path arguments, not command text.
- Command deny patterns match text, so `rm -fr /` or `/bin/rm -rf /` are not
  matched by `rm -rf /`.
- In `auto` mode, or for tools in `allowed_tools`, outbound tools run without
  confirmation even after untrusted content was read.
- Untrusted content can also reach the agent through files in the workspace
  (for example a cloned repository), which does not trigger the rule above.

In `ask` mode you see every mutating call before it runs. In `auto` mode nothing
stops a command the model decides to run, so when using `auto` with untrusted
input (web pages, unfamiliar repositories, third-party MCP servers), run Nova
somewhere that limits the damage: a container or VM with only the project
mounted, or a dedicated OS user without access to your credentials.

## Read-Only vs Mutating Tools

Each tool declares this at registration (`is_read_only`); tools that do not are mutating. MCP tools are always mutating.

**Read-only** (never need confirmation):
- `read_file`, `search_files`, `list_files`
- `search_sessions`, `search_messages`, `read_session`
- `git_status`, `git_log`, `git_diff`, `git_blame`, `git_show`
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

File operation tools (`read_file`, `write_file`, `patch_file`, `search_files`, `list_files`) and the git tools also check sensitive paths and the workspace boundary themselves.

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

Read-only tools run freely. Anything that writes, executes, or modifies requires confirmation. Common download commands are denied as a guardrail; other commands can still reach the network, so review each confirmation.

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
    - "http_post"
    - "http_put"
    - "http_delete"
    - "web_crawl"
    - "web_extract"
    - "web_parse"
```

Nova can read, search, and answer questions but cannot use the built-in tools that modify anything. MCP tools are not covered by this list. Deny them by name or leave MCP servers unconfigured. Useful for code review sessions, audits, or onboarding.

---

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Customizing Nova](GUIDE-003-CUSTOMIZING.md) | Full configuration reference |
| [Hooks](GUIDE-006-HOOKS.md) | Register callbacks that fire before/after permission checks |
| [Creating Tools](GUIDE-001-CREATING_TOOLS.md) | Mark tools as read-only or mutating |
| [SECURITY.md](../SECURITY.md) | Reporting security vulnerabilities |
