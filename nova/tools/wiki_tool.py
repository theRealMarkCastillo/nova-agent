"""Wiki tool — manage Obsidian-compatible wiki notes."""

import json
import logging
from typing import Any

from nova.tools.registry import registry

logger = logging.getLogger(__name__)

WIKI_TOOL_SCHEMA = {
    "name": "wiki",
    "description": (
        "Manage wiki knowledge notes in an Obsidian-compatible vault. "
        "Pick an action from the enum below; behavioral rules live in the system prompt. "
        "Titles support path prefixes ('People/Mark', 'Projects/nova'). "
        "Use [[wikilinks]] and #tags in note content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "write",
                    "append",
                    "patch",
                    "replace",
                    "read",
                    "search",
                    "list",
                    "delete",
                    "rename",
                    "add_tag",
                    "remove_tag",
                    "list_tags",
                    "rename_tag",
                    "pin",
                    "unpin",
                    "maintenance",
                    "follow",
                    "backlinks",
                ],
                "description": "The wiki action to perform.",
            },
            "title": {
                "type": "string",
                "description": (
                    "Note title or path (e.g. 'People/Mark', 'Projects/nova'). "
                    "Required for write, append, read, delete."
                ),
            },
            "content": {
                "type": "string",
                "description": "Note content in markdown. Required for write and append.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags for the note (write only).",
            },
            "query": {
                "type": "string",
                "description": "Full-text search query (search only).",
            },
            "tag": {
                "type": "string",
                "description": "Tag value. Used to filter notes by tag (list), or as the tag to add/remove (add_tag, remove_tag).",
            },
            "stale_days": {
                "type": "integer",
                "description": "Notes not modified within this many days are flagged stale (maintenance only). Default 90.",
            },
            "depth": {
                "type": "integer",
                "description": "Max hops to follow from the start note (follow only). Default 2.",
            },
            "max_notes": {
                "type": "integer",
                "description": "Max notes to return (follow only). Default 10.",
            },
            "include_content": {
                "type": "boolean",
                "description": (
                    "Include full note content in each follow result node (follow only). "
                    "Default false. Use true to read a whole neighbourhood in one call "
                    "instead of following up with separate wiki read calls."
                ),
            },
            "old_text": {
                "type": "string",
                "description": "Exact text to find and replace (patch and replace actions).",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text; use empty string to delete (patch and replace actions).",
            },
            "count": {
                "type": "integer",
                "description": "Max replacements per note (patch/replace). 0 = replace all (default).",
            },
            "new_title": {
                "type": "string",
                "description": "New note title (rename only).",
            },
            "old_tag": {
                "type": "string",
                "description": "Tag to rename from (rename_tag only).",
            },
            "new_tag": {
                "type": "string",
                "description": "Tag to rename to (rename_tag only).",
            },
        },
        "required": ["action"],
    },
}


_CONTENT_EDITS = frozenset({"write", "append", "patch"})


def _prompt_wide_change(args: dict[str, Any], wiki: Any = None, **kwargs: Any) -> str | None:
    """Describe a change that would reach every future system prompt, if any.

    Such notes persist across sessions, so an instruction the model was
    tricked into saving there would steer every later conversation.
    """
    action = args.get("action")
    title = str(args.get("title") or "").strip()
    if action == "pin":
        return "would put a note into every future system prompt"
    if action == "replace":
        return "edits notes across the vault, which can include always-injected notes"
    if action == "rename":
        new_title = str(args.get("new_title") or "").strip()
        if wiki is not None and new_title and wiki.reaches_every_prompt(new_title):
            return "would move a note into every future system prompt"
        return None
    if action in _CONTENT_EDITS and title and wiki is not None and wiki.reaches_every_prompt(title):
        return "edits a note that appears in every future system prompt"
    return None


def _wiki_tool(args: dict[str, Any], **kwargs) -> str:
    wiki = kwargs.get("wiki")
    if wiki is None:
        return "Error: Wiki memory is not enabled."

    action = args.get("action", "")

    try:
        return _dispatch(wiki, action, args, kwargs)
    except ValueError as e:
        return f"Error: {e}"


def _dump(result: Any) -> str:
    """Serialize a result as compact JSON (no indent, ASCII-safe) for tool returns.

    The wiki tool can be called many times per turn, and indented JSON costs
    tokens. This is the single chokepoint so all results stay consistent.
    """
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _dispatch(wiki, action: str, args: dict[str, Any], kwargs: dict) -> str:
    if action == "write":
        title = args.get("title", "").strip()
        content = args.get("content", "")
        if not title:
            return "Error: 'title' is required for write."
        if not content:
            return "Error: 'content' is required for write."
        tags = args.get("tags") or []
        result = wiki.write(title, content, tags)
        _refresh(kwargs)
        return _dump(result)

    elif action == "append":
        title = args.get("title", "").strip()
        content = args.get("content", "")
        if not title:
            return "Error: 'title' is required for append."
        if not content:
            return "Error: 'content' is required for append."
        result = wiki.append(title, content)
        _refresh(kwargs)
        return _dump(result)

    elif action == "patch":
        title = args.get("title", "").strip()
        old_text = args.get("old_text")
        new_text = args.get("new_text")
        if not title:
            return "Error: 'title' is required for patch."
        if old_text is None:
            return "Error: 'old_text' is required for patch."
        if new_text is None:
            return "Error: 'new_text' is required for patch."
        count = args.get("count", 0)
        result = wiki.patch(title, old_text, new_text, count=count)
        if result.get("status") == "patched":
            _refresh(kwargs)
        return _dump(result)

    elif action == "replace":
        old_text = args.get("old_text")
        new_text = args.get("new_text")
        if old_text is None:
            return "Error: 'old_text' is required for replace."
        if new_text is None:
            return "Error: 'new_text' is required for replace."
        count = args.get("count", 0)
        result = wiki.vault_replace(old_text, new_text, count=count)
        if result["patched_notes"]:
            _refresh(kwargs)
        return _dump(result)

    elif action == "add_tag":
        title = args.get("title", "").strip()
        tag = (args.get("tag") or "").strip()
        if not title:
            return "Error: 'title' is required for add_tag."
        if not tag:
            return "Error: 'tag' is required for add_tag."
        result = wiki.add_tag(title, tag)
        if result.get("status") == "added":
            _refresh(kwargs)
        return _dump(result)

    elif action == "remove_tag":
        title = args.get("title", "").strip()
        tag = (args.get("tag") or "").strip()
        if not title:
            return "Error: 'title' is required for remove_tag."
        if not tag:
            return "Error: 'tag' is required for remove_tag."
        result = wiki.remove_tag(title, tag)
        if result.get("status") == "removed":
            _refresh(kwargs)
        return _dump(result)

    elif action == "pin":
        title = args.get("title", "").strip()
        if not title:
            return "Error: 'title' is required for pin."
        result = wiki.pin(title)
        if result.get("status") == "pinned":
            _refresh(kwargs)
        return _dump(result)

    elif action == "unpin":
        title = args.get("title", "").strip()
        if not title:
            return "Error: 'title' is required for unpin."
        result = wiki.unpin(title)
        if result.get("status") == "unpinned":
            _refresh(kwargs)
        return _dump(result)

    elif action == "read":
        title = args.get("title", "").strip()
        if not title:
            return "Error: 'title' is required for read."
        note = wiki.read(title)
        if note is None:
            return f"Note not found: '{title}'"
        fm = note["frontmatter"]
        header = f"# {fm.get('title', title)}"
        meta = []
        if fm.get("tags"):
            meta.append("tags: " + ", ".join(f"#{t}" for t in fm["tags"]))
        if fm.get("modified"):
            meta.append(f"modified: {fm['modified']}")
        meta_line = " | ".join(meta)
        parts = [header]
        if meta_line:
            parts.append(meta_line)
        parts.append("")
        parts.append(note["content"])
        return "\n".join(parts)

    elif action == "search":
        query = args.get("query", "").strip()
        if not query:
            return "Error: 'query' is required for search."
        results = wiki.search(query)
        if not results:
            return f"No notes found matching '{query}'."
        return _dump(results)

    elif action == "list":
        tag = args.get("tag")
        notes = wiki.list_notes(tag=tag)
        if not notes:
            return "No notes found."
        return _dump(notes)

    elif action == "delete":
        title = args.get("title", "").strip()
        if not title:
            return "Error: 'title' is required for delete."
        broken = len(wiki.backlinks(title))
        deleted = wiki.delete(title)
        _refresh(kwargs)
        response: dict = {"status": "deleted" if deleted else "not_found"}
        if deleted and broken > 0:
            response["warning"] = (
                f"{broken} note(s) still link to '[[{title}]]' — consider updating them."
            )
        return _dump(response)

    elif action == "rename":
        title = args.get("title", "").strip()
        new_title = args.get("new_title", "").strip()
        if not title:
            return "Error: 'title' is required for rename."
        if not new_title:
            return "Error: 'new_title' is required for rename."
        result = wiki.rename(title, new_title)
        if result.get("status") == "renamed":
            _refresh(kwargs)
        return _dump(result)

    elif action == "list_tags":
        tags = wiki.list_tags()
        if not tags:
            return "No tags found."
        return _dump(tags)

    elif action == "rename_tag":
        old_tag = args.get("old_tag", "").strip()
        new_tag = args.get("new_tag", "").strip()
        if not old_tag:
            return "Error: 'old_tag' is required for rename_tag."
        if not new_tag:
            return "Error: 'new_tag' is required for rename_tag."
        result = wiki.rename_tag(old_tag, new_tag)
        if result.get("updated_notes"):
            _refresh(kwargs)
        return _dump(result)

    elif action == "maintenance":
        stale_days = args.get("stale_days", 90)
        report = wiki.maintenance(stale_days=stale_days)
        return _dump(report)

    elif action == "follow":
        title = args.get("title", "").strip()
        if not title:
            return "Error: 'title' is required for follow."
        depth = args.get("depth", 2)
        max_notes = args.get("max_notes", 10)
        include_content = args.get("include_content", False)
        result = wiki.follow(
            title, depth=depth, max_notes=max_notes, include_content=include_content
        )
        if "error" in result:
            return f"Note not found: '{title}'"
        return _dump(result)

    elif action == "backlinks":
        title = args.get("title", "").strip()
        if not title:
            return "Error: 'title' is required for backlinks."
        results = wiki.backlinks(title)
        return _dump(results)

    return (
        f"Error: Unknown action '{action}'. "
        "Use write, append, patch, replace, read, search, list, delete, rename, "
        "add_tag, remove_tag, list_tags, rename_tag, pin, unpin, maintenance, follow, or backlinks."
    )


def _refresh(kwargs: dict) -> None:
    agent = kwargs.get("agent")
    if agent and hasattr(agent, "_refresh_system_prompt"):
        agent._refresh_system_prompt()


registry.register(
    name="wiki",
    toolset="wiki",
    schema=WIKI_TOOL_SCHEMA,
    handler=_wiki_tool,
    emoji="📓",
    always_confirm=_prompt_wide_change,
)
