"""Tests for the skills tool."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from nova.tools.skills_tool import _skill_export, _skill_manage, _skill_view, _skills_list


@pytest.fixture
def temp_skills_dir():
    tmpdir = tempfile.mkdtemp()
    return Path(tmpdir)


@pytest.fixture
def skills_config(temp_skills_dir):
    return {
        "skills": {
            "directory": str(temp_skills_dir),
        }
    }


def test_skills_list_no_skills(skills_config):
    result = _skills_list({}, config=skills_config)
    assert "No skills found" in result


def test_skill_manage_rejects_path_traversal(skills_config):
    result = _skill_manage(
        {"action": "create", "name": "../outside", "content": "secret"},
        config=skills_config,
    )
    assert "Invalid skill name" in result


def test_skills_list_with_skills(temp_skills_dir, skills_config):
    skill_dir = temp_skills_dir / "test_skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: test_skill\ndescription: Test\n---\nContent here")

    result = _skills_list({}, config=skills_config)
    assert '"name": "test_skill"' in result
    assert '"next_cursor": null' in result


def test_skill_view_success(temp_skills_dir, skills_config):
    skill_dir = temp_skills_dir / "my_skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: my_skill\n---\nSkill content")

    result = _skill_view({"name": "my_skill"}, config=skills_config)
    assert "Skill content" in result


def test_skill_view_not_found(temp_skills_dir, skills_config):
    with patch("nova.tools.skills_tool.load_skill_content") as mock_load:
        mock_load.return_value = None
        result = _skill_view({"name": "nonexistent"}, config=skills_config)
    assert "not found" in result


def test_skill_manage_create_success(temp_skills_dir, skills_config):
    result = _skill_manage(
        {
            "action": "create",
            "name": "new_skill",
            "category": "test",
            "description": "A test skill",
            "content": "# New Skill\nThis is new.",
        },
        config=skills_config,
    )
    assert "Created skill" in result
    skill_file = temp_skills_dir / "new_skill" / "SKILL.md"
    assert skill_file.exists()
    content = skill_file.read_text()
    assert "new_skill" in content
    assert "A test skill" in content
    assert "# New Skill" in content


def test_skill_manage_create_already_exists(temp_skills_dir, skills_config):
    skill_dir = temp_skills_dir / "existing"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: existing\n---\nContent")

    result = _skill_manage(
        {
            "action": "create",
            "name": "existing",
            "content": "new content",
        },
        config=skills_config,
    )
    assert "already exists" in result


def test_skill_manage_create_missing_name(temp_skills_dir, skills_config):
    result = _skill_manage(
        {
            "action": "create",
            "name": "",
            "content": "content",
        },
        config=skills_config,
    )
    assert "Error" in result
    assert "name" in result


def test_skill_manage_patch_success(temp_skills_dir, skills_config):
    skill_dir = temp_skills_dir / "existing"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: existing\ncategory: test\n---\nOld content")

    result = _skill_manage(
        {
            "action": "patch",
            "name": "existing",
            "content": "Updated content",
        },
        config=skills_config,
    )
    assert "Updated skill" in result
    updated = skill_file.read_text()
    assert "Updated content" in updated
    assert "name: existing" in updated  # frontmatter preserved


def test_skill_manage_patch_not_found(temp_skills_dir, skills_config):
    result = _skill_manage(
        {
            "action": "patch",
            "name": "nonexistent",
            "content": "content",
        },
        config=skills_config,
    )
    assert "not found" in result


def test_skill_manage_patch_missing_content(temp_skills_dir, skills_config):
    skill_dir = temp_skills_dir / "existing"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: existing\n---\nContent")

    result = _skill_manage(
        {
            "action": "patch",
            "name": "existing",
            "content": "",
        },
        config=skills_config,
    )
    assert "Error" in result
    assert "content" in result


def test_skill_manage_delete_success(temp_skills_dir, skills_config):
    skill_dir = temp_skills_dir / "to_delete"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: to_delete\n---\nContent")

    result = _skill_manage(
        {"action": "delete", "name": "to_delete"},
        config=skills_config,
    )
    assert "Deleted skill" in result
    assert not skill_file.exists()
    assert not skill_dir.exists()


def test_skill_manage_delete_not_found(temp_skills_dir, skills_config):
    result = _skill_manage(
        {"action": "delete", "name": "nonexistent"},
        config=skills_config,
    )
    assert "not found" in result


def test_skill_manage_delete_preserves_non_empty_dir(temp_skills_dir, skills_config):
    skill_dir = temp_skills_dir / "partial_delete"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: partial_delete\n---\nContent")
    other_file = skill_dir / "other.txt"
    other_file.write_text("keep this")

    result = _skill_manage(
        {"action": "delete", "name": "partial_delete"},
        config=skills_config,
    )
    assert "Deleted skill" in result
    assert not skill_file.exists()
    assert skill_dir.exists()  # Dir preserved because it's not empty
    assert other_file.exists()


def test_skill_manage_unknown_action(temp_skills_dir, skills_config):
    result = _skill_manage(
        {"action": "invalid_action", "name": "test"},
        config=skills_config,
    )
    assert "Error" in result
    assert "Unknown action" in result


def test_skill_manage_patch_with_list_in_frontmatter(temp_skills_dir, skills_config):
    skill_dir = temp_skills_dir / "with_list"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    # Frontmatter with a list field
    skill_file.write_text('---\nname: with_list\ntags: ["python", "data"]\n---\nContent')

    result = _skill_manage(
        {
            "action": "patch",
            "name": "with_list",
            "content": "New content",
        },
        config=skills_config,
    )
    assert "Updated skill" in result
    updated = skill_file.read_text()
    assert "New content" in updated
    assert "with_list" in updated


def test_skill_view_substitutes_skill_dir(temp_skills_dir, skills_config):
    """Test that skill_view resolves {skill_dir} placeholders."""
    skill_dir = temp_skills_dir / "path_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: path_skill\n---\nRef: {skill_dir}/references/file.md"
    )
    result = _skill_view({"name": "path_skill"}, config=skills_config)
    assert "{skill_dir}" not in result
    assert "references/file.md" in result


def test_skill_export_success(temp_skills_dir, skills_config):
    """Test exporting a skill with references inlined."""
    skill_dir = temp_skills_dir / "export_skill"
    refs_dir = skill_dir / "references"
    refs_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: export_skill\n---\n# Export Skill\n\nMain content."
    )
    (refs_dir / "example.md").write_text("# Example\n\nRef content.")
    result = _skill_export({"name": "export_skill"}, config=skills_config)
    assert "Main content" in result
    assert "Ref content" in result
    assert "Reference Examples" in result


def test_skill_export_not_found(temp_skills_dir, skills_config):
    """Test exporting a nonexistent skill returns an error message."""
    result = _skill_export({"name": "nonexistent"}, config=skills_config)
    assert "not found" in result


def test_skill_export_empty_name(temp_skills_dir, skills_config):
    """Test exporting with an empty name returns an error message."""
    result = _skill_export({"name": ""}, config=skills_config)
    assert "not found" in result


def test_skill_manage_create_default_category(temp_skills_dir, skills_config):
    result = _skill_manage(
        {
            "action": "create",
            "name": "default_cat",
            "content": "content",
        },
        config=skills_config,
    )
    assert "Created skill" in result
    skill_file = temp_skills_dir / "default_cat" / "SKILL.md"
    content = skill_file.read_text()
    assert "category: general" in content


def test_skill_manage_create_escapes_special_chars(temp_skills_dir, skills_config):
    """Descriptions with quotes, colons, and newlines must round-trip as valid YAML."""
    import yaml

    description = 'A "quoted" description: with colons\nand newlines!'
    result = _skill_manage(
        {
            "action": "create",
            "name": "special_chars",
            "category": "general",
            "description": description,
            "content": "Body text",
        },
        config=skills_config,
    )
    assert "Created skill" in result

    # Stored frontmatter must be parseable YAML with the exact value intact
    skill_file = temp_skills_dir / "special_chars" / "SKILL.md"
    raw = skill_file.read_text(encoding="utf-8")
    fm = yaml.safe_load(raw.split("---", 2)[1])
    assert fm["description"] == description
    assert fm["name"] == "special_chars"


def test_skill_manage_patch_preserves_quoted_frontmatter(temp_skills_dir, skills_config):
    """Patching a skill's body must not corrupt frontmatter with colons/quotes."""
    import yaml

    _skill_manage(
        {
            "action": "create",
            "name": "patch_me",
            "category": "general",
            "description": "plain: description",
            "content": "v1",
        },
        config=skills_config,
    )
    result = _skill_manage(
        {"action": "patch", "name": "patch_me", "content": "v2 body"},
        config=skills_config,
    )
    assert "Updated" in result

    # Frontmatter must survive the body rewrite intact
    raw = (temp_skills_dir / "patch_me" / "SKILL.md").read_text(encoding="utf-8")
    assert "v2 body" in raw
    fm = yaml.safe_load(raw.split("---", 2)[1])
    assert fm["description"] == "plain: description"
    assert fm["name"] == "patch_me"
