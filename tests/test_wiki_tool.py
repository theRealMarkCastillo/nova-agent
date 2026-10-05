"""Tests for the wiki tool."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nova.tools.wiki_tool import _prompt_wide_change, _wiki_tool
from nova.wiki_memory import WikiMemory


@pytest.fixture
def wiki(tmp_path: Path) -> WikiMemory:
    return WikiMemory(tmp_path / "wiki")


def test_wiki_tool_no_wiki_store():
    result = _wiki_tool({"action": "write", "title": "x", "content": "y"})
    assert "Error" in result
    assert "not enabled" in result


def test_write_action(wiki: WikiMemory):
    result = _wiki_tool(
        {"action": "write", "title": "Test", "content": "Hello", "tags": ["demo"]},
        wiki=wiki,
    )
    data = json.loads(result)
    assert data["status"] == "written"


def test_write_missing_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "write", "content": "x"}, wiki=wiki)
    assert "Error" in result


def test_write_missing_content(wiki: WikiMemory):
    result = _wiki_tool({"action": "write", "title": "T"}, wiki=wiki)
    assert "Error" in result


def test_append_action(wiki: WikiMemory):
    wiki.write("Note", "First")
    result = _wiki_tool(
        {"action": "append", "title": "Note", "content": "Second"},
        wiki=wiki,
    )
    data = json.loads(result)
    assert data["status"] == "appended"


def test_append_missing_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "append", "content": "x"}, wiki=wiki)
    assert "Error" in result


def test_read_existing(wiki: WikiMemory):
    wiki.write("My Note", "Some content", tags=["tag1"])
    result = _wiki_tool({"action": "read", "title": "My Note"}, wiki=wiki)
    assert "My Note" in result
    assert "Some content" in result
    assert "#tag1" in result


def test_read_missing(wiki: WikiMemory):
    result = _wiki_tool({"action": "read", "title": "Ghost"}, wiki=wiki)
    assert "not found" in result.lower()


def test_read_missing_title_param(wiki: WikiMemory):
    result = _wiki_tool({"action": "read"}, wiki=wiki)
    assert "Error" in result


def test_search_found(wiki: WikiMemory):
    wiki.write("Alpha", "contains the keyword needle")
    result = _wiki_tool({"action": "search", "query": "needle"}, wiki=wiki)
    data = json.loads(result)
    assert len(data) == 1
    assert data[0]["title"] == "Alpha"


def test_search_returns_compact_json(wiki: WikiMemory):
    """Wiki tool results are returned as compact (no-indent) JSON to save
    tokens across many tool calls in a single turn.
    """
    wiki.write("A", "x")
    wiki.write("B", "y")
    result = _wiki_tool({"action": "list"}, wiki=wiki)
    # Compact JSON must have no newlines and no leading whitespace
    assert "\n" not in result
    assert "  " not in result


def test_search_not_found(wiki: WikiMemory):
    wiki.write("Note", "no match here")
    result = _wiki_tool({"action": "search", "query": "zzz_absent"}, wiki=wiki)
    assert "No notes found" in result


def test_search_missing_query(wiki: WikiMemory):
    result = _wiki_tool({"action": "search"}, wiki=wiki)
    assert "Error" in result


def test_list_action(wiki: WikiMemory):
    wiki.write("A", "content")
    wiki.write("B", "content")
    result = _wiki_tool({"action": "list"}, wiki=wiki)
    data = json.loads(result)
    titles = {n["title"] for n in data}
    assert "A" in titles
    assert "B" in titles


def test_list_empty(wiki: WikiMemory):
    result = _wiki_tool({"action": "list"}, wiki=wiki)
    assert "No notes found" in result


def test_list_filter_by_tag(wiki: WikiMemory):
    wiki.write("Tagged", "x", tags=["keep"])
    wiki.write("Other", "y", tags=["drop"])
    result = _wiki_tool({"action": "list", "tag": "keep"}, wiki=wiki)
    data = json.loads(result)
    assert len(data) == 1
    assert data[0]["title"] == "Tagged"


def test_delete_action(wiki: WikiMemory):
    wiki.write("Bye", "farewell")
    result = _wiki_tool({"action": "delete", "title": "Bye"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "deleted"
    assert wiki.read("Bye") is None


def test_delete_not_found(wiki: WikiMemory):
    result = _wiki_tool({"action": "delete", "title": "Ghost"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "not_found"


def test_delete_missing_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "delete"}, wiki=wiki)
    assert "Error" in result


def test_unknown_action(wiki: WikiMemory):
    result = _wiki_tool({"action": "explode"}, wiki=wiki)
    assert "Unknown action" in result


def test_maintenance_action_returns_report(wiki: WikiMemory):
    wiki.write("A", "content")
    wiki.write("B", "linked to [[A]]")
    result = _wiki_tool({"action": "maintenance"}, wiki=wiki)
    report = json.loads(result)
    assert report["total_notes"] == 2
    assert "duplicate_candidates" in report
    assert "orphans" in report
    assert "stale" in report
    assert "tag_counts" in report


def test_maintenance_does_not_refresh_prompt(wiki: WikiMemory):
    """Maintenance is read-only — should not trigger system prompt rebuild."""
    from unittest.mock import MagicMock

    agent = MagicMock()
    _wiki_tool({"action": "maintenance"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_not_called()


def test_path_traversal_returns_error_not_raise(wiki: WikiMemory):
    result = _wiki_tool(
        {"action": "write", "title": "../../etc/passwd", "content": "evil"},
        wiki=wiki,
    )
    assert "Error" in result
    assert "path traversal" in result


def test_write_triggers_agent_refresh(wiki: WikiMemory):
    agent = MagicMock()
    _wiki_tool(
        {"action": "write", "title": "T", "content": "c"},
        wiki=wiki,
        agent=agent,
    )
    agent._refresh_system_prompt.assert_called_once()


# --- follow action tests ---


def test_follow_action(wiki: WikiMemory):
    wiki.write("Python", "A language. See [[Django]].")
    wiki.write("Django", "A web framework.")
    result = _wiki_tool({"action": "follow", "title": "Python"}, wiki=wiki)
    data = json.loads(result)
    titles = {n["title"] for n in data["nodes"]}
    assert "Python" in titles
    assert "Django" in titles


def test_follow_missing_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "follow"}, wiki=wiki)
    assert "Error" in result


def test_follow_not_found(wiki: WikiMemory):
    result = _wiki_tool({"action": "follow", "title": "Ghost"}, wiki=wiki)
    assert "not found" in result.lower()


def test_follow_custom_depth_and_max(wiki: WikiMemory):
    wiki.write("A", "Links to [[B]].")
    wiki.write("B", "Links to [[C]].")
    wiki.write("C", "End.")
    result = _wiki_tool({"action": "follow", "title": "A", "depth": 1, "max_notes": 2}, wiki=wiki)
    data = json.loads(result)
    assert data["nodes_found"] <= 2
    assert data["depth"] == 1


def test_follow_does_not_refresh_prompt(wiki: WikiMemory):
    wiki.write("Note", "Content.")
    agent = MagicMock()
    _wiki_tool({"action": "follow", "title": "Note"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_not_called()


def test_follow_include_content(wiki: WikiMemory):
    wiki.write("Root", "Full body. See [[Child]].")
    wiki.write("Child", "Child body.")
    result = _wiki_tool({"action": "follow", "title": "Root", "include_content": True}, wiki=wiki)
    data = json.loads(result)
    root = next(n for n in data["nodes"] if n["title"] == "Root")
    assert "content" in root
    assert "Full body" in root["content"]


def test_follow_no_content_by_default(wiki: WikiMemory):
    wiki.write("Root", "body")
    result = _wiki_tool({"action": "follow", "title": "Root"}, wiki=wiki)
    data = json.loads(result)
    assert "content" not in data["nodes"][0]


# --- backlinks action tests ---


def test_backlinks_action(wiki: WikiMemory):
    wiki.write("Python", "A language.")
    wiki.write("Django", "Uses [[Python]].")
    result = _wiki_tool({"action": "backlinks", "title": "Python"}, wiki=wiki)
    data = json.loads(result)
    assert any(r["title"] == "Django" for r in data)


def test_backlinks_missing_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "backlinks"}, wiki=wiki)
    assert "Error" in result


def test_backlinks_none_found(wiki: WikiMemory):
    wiki.write("Isolated", "No links.")
    result = _wiki_tool({"action": "backlinks", "title": "Isolated"}, wiki=wiki)
    data = json.loads(result)
    assert data == []


def test_backlinks_does_not_refresh_prompt(wiki: WikiMemory):
    wiki.write("Target", "Content.")
    agent = MagicMock()
    _wiki_tool({"action": "backlinks", "title": "Target"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_not_called()


# --- delete backlink warning ---


def test_delete_with_backlinks_warns(wiki: WikiMemory):
    wiki.write("Target", "content")
    wiki.write("Linker", "See [[Target]].")
    result = _wiki_tool({"action": "delete", "title": "Target"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "deleted"
    assert "warning" in data
    assert "1" in data["warning"]


def test_delete_without_backlinks_no_warning(wiki: WikiMemory):
    wiki.write("Isolated", "content")
    result = _wiki_tool({"action": "delete", "title": "Isolated"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "deleted"
    assert "warning" not in data


# --- rename action ---


def test_rename_action(wiki: WikiMemory):
    wiki.write("Old", "content")
    result = _wiki_tool({"action": "rename", "title": "Old", "new_title": "New"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "renamed"
    assert wiki.read("New") is not None
    assert wiki.read("Old") is None


def test_rename_missing_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "rename", "new_title": "New"}, wiki=wiki)
    assert "Error" in result


def test_rename_missing_new_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "rename", "title": "Old"}, wiki=wiki)
    assert "Error" in result


def test_rename_not_found(wiki: WikiMemory):
    result = _wiki_tool({"action": "rename", "title": "Ghost", "new_title": "New"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "not_found"


def test_rename_triggers_refresh(wiki: WikiMemory):
    wiki.write("Old", "content")
    agent = MagicMock()
    _wiki_tool({"action": "rename", "title": "Old", "new_title": "New"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_called_once()


def test_rename_conflict_no_refresh(wiki: WikiMemory):
    wiki.write("A", "content")
    wiki.write("B", "content")
    agent = MagicMock()
    _wiki_tool({"action": "rename", "title": "A", "new_title": "B"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_not_called()


# --- list_tags action ---


def test_list_tags_action(wiki: WikiMemory):
    wiki.write("A", "x", tags=["python", "api"])
    wiki.write("B", "y", tags=["python"])
    result = _wiki_tool({"action": "list_tags"}, wiki=wiki)
    data = json.loads(result)
    assert data["python"] == 2
    assert data["api"] == 1


def test_list_tags_empty(wiki: WikiMemory):
    wiki.write("Note", "no tags")
    result = _wiki_tool({"action": "list_tags"}, wiki=wiki)
    assert "No tags found" in result


def test_list_tags_does_not_refresh(wiki: WikiMemory):
    wiki.write("A", "x", tags=["t"])
    agent = MagicMock()
    _wiki_tool({"action": "list_tags"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_not_called()


# --- rename_tag action ---


def test_rename_tag_action(wiki: WikiMemory):
    wiki.write("A", "x", tags=["py"])
    wiki.write("B", "y", tags=["py"])
    result = _wiki_tool({"action": "rename_tag", "old_tag": "py", "new_tag": "python"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "renamed"
    assert set(data["updated_notes"]) == {"A", "B"}


def test_rename_tag_missing_old_tag(wiki: WikiMemory):
    result = _wiki_tool({"action": "rename_tag", "new_tag": "python"}, wiki=wiki)
    assert "Error" in result


def test_rename_tag_missing_new_tag(wiki: WikiMemory):
    result = _wiki_tool({"action": "rename_tag", "old_tag": "py"}, wiki=wiki)
    assert "Error" in result


def test_rename_tag_triggers_refresh_when_updated(wiki: WikiMemory):
    wiki.write("A", "x", tags=["old"])
    agent = MagicMock()
    _wiki_tool({"action": "rename_tag", "old_tag": "old", "new_tag": "new"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_called_once()


def test_rename_tag_no_refresh_when_nothing_updated(wiki: WikiMemory):
    wiki.write("A", "x", tags=["other"])
    agent = MagicMock()
    _wiki_tool(
        {"action": "rename_tag", "old_tag": "missing", "new_tag": "x"}, wiki=wiki, agent=agent
    )
    agent._refresh_system_prompt.assert_not_called()


# --- maintenance broken_links ---


def test_maintenance_reports_broken_links(wiki: WikiMemory):
    wiki.write("Real", "content")
    wiki.write("Linker", "See [[Real]] and [[Ghost]].")
    result = _wiki_tool({"action": "maintenance"}, wiki=wiki)
    data = json.loads(result)
    assert "broken_links" in data
    broken_targets = {b["broken_link"] for b in data["broken_links"]}
    assert "Ghost" in broken_targets
    assert "Real" not in broken_targets


# --- patch action ---


def test_patch_replaces_text(wiki: WikiMemory):
    wiki.write("Note", "foo bar foo")
    result = _wiki_tool(
        {"action": "patch", "title": "Note", "old_text": "foo", "new_text": "qux"}, wiki=wiki
    )
    data = json.loads(result)
    assert data["status"] == "patched"
    assert data["replacements"] == 2


def test_patch_missing_old_text_param(wiki: WikiMemory):
    wiki.write("Note", "content")
    result = _wiki_tool({"action": "patch", "title": "Note", "new_text": "x"}, wiki=wiki)
    assert "Error" in result


def test_patch_missing_new_text_param(wiki: WikiMemory):
    wiki.write("Note", "content")
    result = _wiki_tool({"action": "patch", "title": "Note", "old_text": "x"}, wiki=wiki)
    assert "Error" in result


def test_patch_no_match_returns_no_match_status(wiki: WikiMemory):
    wiki.write("Note", "hello")
    result = _wiki_tool(
        {"action": "patch", "title": "Note", "old_text": "xyz", "new_text": ""}, wiki=wiki
    )
    data = json.loads(result)
    assert data["status"] == "no_match"


def test_patch_triggers_refresh(wiki: WikiMemory):
    wiki.write("Note", "old text")
    agent = MagicMock()
    _wiki_tool(
        {"action": "patch", "title": "Note", "old_text": "old", "new_text": "new"},
        wiki=wiki,
        agent=agent,
    )
    agent._refresh_system_prompt.assert_called_once()


def test_patch_no_refresh_on_no_match(wiki: WikiMemory):
    wiki.write("Note", "hello")
    agent = MagicMock()
    _wiki_tool(
        {"action": "patch", "title": "Note", "old_text": "xyz", "new_text": "abc"},
        wiki=wiki,
        agent=agent,
    )
    agent._refresh_system_prompt.assert_not_called()


# --- replace action ---


def test_replace_vault_wide(wiki: WikiMemory):
    wiki.write("A", "see [[Hasu]]")
    wiki.write("B", "also [[Hasu]]")
    result = _wiki_tool({"action": "replace", "old_text": "[[Hasu]]", "new_text": ""}, wiki=wiki)
    data = json.loads(result)
    assert data["total_replacements"] == 2
    assert len(data["patched_notes"]) == 2


def test_replace_missing_old_text(wiki: WikiMemory):
    result = _wiki_tool({"action": "replace", "new_text": "x"}, wiki=wiki)
    assert "Error" in result


def test_replace_triggers_refresh(wiki: WikiMemory):
    wiki.write("A", "foo")
    agent = MagicMock()
    _wiki_tool({"action": "replace", "old_text": "foo", "new_text": "bar"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_called_once()


def test_replace_no_refresh_when_no_matches(wiki: WikiMemory):
    agent = MagicMock()
    _wiki_tool({"action": "replace", "old_text": "xyz", "new_text": "abc"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_not_called()


# --- add_tag / remove_tag actions ---


def test_add_tag_action(wiki: WikiMemory):
    wiki.write("Note", "content", tags=[])
    result = _wiki_tool({"action": "add_tag", "title": "Note", "tag": "newtag"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "added"
    assert "newtag" in wiki.read("Note")["frontmatter"]["tags"]


def test_add_tag_missing_tag_param(wiki: WikiMemory):
    wiki.write("Note", "content")
    result = _wiki_tool({"action": "add_tag", "title": "Note"}, wiki=wiki)
    assert "Error" in result


def test_remove_tag_action(wiki: WikiMemory):
    wiki.write("Note", "content", tags=["a", "b"])
    result = _wiki_tool({"action": "remove_tag", "title": "Note", "tag": "a"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "removed"
    assert "a" not in wiki.read("Note")["frontmatter"]["tags"]


def test_remove_tag_missing_tag_param(wiki: WikiMemory):
    wiki.write("Note", "content")
    result = _wiki_tool({"action": "remove_tag", "title": "Note"}, wiki=wiki)
    assert "Error" in result


# --- pin / unpin actions ---


def test_pin_action(wiki: WikiMemory):
    wiki.write("Note", "content")
    result = _wiki_tool({"action": "pin", "title": "Note"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "pinned"
    assert wiki.read("Note")["frontmatter"].get("inject") is True


def test_unpin_action(wiki: WikiMemory):
    wiki.write("Note", "content")
    wiki.pin("Note")
    result = _wiki_tool({"action": "unpin", "title": "Note"}, wiki=wiki)
    data = json.loads(result)
    assert data["status"] == "unpinned"
    assert not wiki.read("Note")["frontmatter"].get("inject")


def test_pin_triggers_refresh(wiki: WikiMemory):
    wiki.write("Note", "content")
    agent = MagicMock()
    _wiki_tool({"action": "pin", "title": "Note"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_called_once()


def test_unpin_triggers_refresh(wiki: WikiMemory):
    wiki.write("Note", "content")
    wiki.pin("Note")
    agent = MagicMock()
    _wiki_tool({"action": "unpin", "title": "Note"}, wiki=wiki, agent=agent)
    agent._refresh_system_prompt.assert_called_once()


def test_replace_missing_new_text(wiki: WikiMemory):
    result = _wiki_tool({"action": "replace", "old_text": "x"}, wiki=wiki)
    assert "Error" in result


def test_add_tag_missing_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "add_tag", "tag": "foo"}, wiki=wiki)
    assert "Error" in result


def test_remove_tag_missing_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "remove_tag", "tag": "foo"}, wiki=wiki)
    assert "Error" in result


def test_pin_missing_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "pin"}, wiki=wiki)
    assert "Error" in result


def test_unpin_missing_title(wiki: WikiMemory):
    result = _wiki_tool({"action": "unpin"}, wiki=wiki)
    assert "Error" in result


class TestPromptWideChanges:
    @pytest.mark.parametrize(
        "args",
        [
            {"action": "write", "title": "Core/Rules", "content": "x"},
            {"action": "write", "title": "core/rules", "content": "x"},
            {"action": "append", "title": "Core/Rules", "content": "x"},
            {"action": "patch", "title": "Core/Rules", "old_text": "a", "new_text": "b"},
            {"action": "pin", "title": "Ideas"},
            {"action": "rename", "title": "Ideas", "new_title": "Core/Ideas"},
            {"action": "replace", "old_text": "a", "new_text": "b"},
        ],
    )
    def test_changes_reaching_every_prompt_are_flagged(self, wiki, args):
        assert _prompt_wide_change(args, wiki=wiki)

    def test_edit_to_pinned_note_is_flagged(self, wiki):
        wiki.write("Ideas", "draft")
        wiki.pin("Ideas")
        assert _prompt_wide_change(
            {"action": "append", "title": "Ideas", "content": "more"}, wiki=wiki
        )

    @pytest.mark.parametrize(
        "args",
        [
            {"action": "write", "title": "Projects/nova", "content": "x"},
            {"action": "read", "title": "Core/Rules"},
            {"action": "unpin", "title": "Ideas"},
            {"action": "delete", "title": "Core/Rules"},
            {"action": "rename", "title": "Core/Old", "new_title": "Archive/Old"},
            {"action": "add_tag", "title": "Core/Rules", "tag": "x"},
        ],
    )
    def test_other_actions_are_not_flagged(self, wiki, args):
        assert _prompt_wide_change(args, wiki=wiki) is None
