"""Skills management tool — list, view, and manage skills.

Provides skill_view, skills_list, and skill_manage tools for the agent
to discover and use specialized knowledge.
"""

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from nova.skills import (
    build_skills_prompt,
    discover_skills,
    export_skill,
    load_skill_content,
    parse_frontmatter,
)
from nova.tools.registry import registry

logger = logging.getLogger(__name__)

SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": "List all available skills with their names, categories, and descriptions.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": "Load the full content of a skill by name. Use when a skill is relevant to the current task.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The name of the skill to load.",
            },
        },
        "required": ["name"],
    },
}

SKILL_EXPORT_SCHEMA = {
    "name": "skill_export",
    "description": "Export a skill as a single self-contained markdown file with all reference examples inlined. Use to generate a portable version for CLAUDE.md, system prompts, or other agents that don't support the nova skill protocol.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The name of the skill to export.",
            },
        },
        "required": ["name"],
    },
}

SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": "Create, update, or delete skills. Use action='create' to make a new skill, 'patch' to update, or 'delete' to remove.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "patch", "delete"],
                "description": "The action to perform.",
            },
            "name": {
                "type": "string",
                "description": "The skill name.",
            },
            "category": {
                "type": "string",
                "description": "The skill category (for create).",
            },
            "description": {
                "type": "string",
                "description": "One-line description (for create).",
            },
            "content": {
                "type": "string",
                "description": "The skill content (for create/patch).",
            },
        },
        "required": ["action", "name"],
    },
}


def _get_skills_dir(config: dict) -> Path:
    """Get the skills directory from config."""
    return Path(config.get("skills", {}).get("directory", "~/.nova/skills")).expanduser()


def _safe_skill_dir(skills_dir: Path, name: str) -> Path | None:
    """Resolve a skill directory without allowing traversal or symlink escapes."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        return None
    root = skills_dir.resolve()
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _skills_list(args: dict[str, Any], **kwargs) -> str:
    """List all available skills."""
    config = kwargs.get("config", {})
    skills_dir = _get_skills_dir(config)
    skills = discover_skills(skills_dir)

    if not skills:
        return "No skills found. Create skills in ~/.nova/skills/."

    return build_skills_prompt(skills)


def _skill_view(args: dict[str, Any], **kwargs) -> str:
    """Load the full content of a skill."""
    config = kwargs.get("config", {})
    skills_dir = _get_skills_dir(config)
    skill_name = args.get("name", "")

    if not skill_name:
        return f"Error: Skill '{skill_name}' not found."

    skill_dir_path = _safe_skill_dir(skills_dir, skill_name)
    if skill_dir_path is None:
        return "Error: Invalid skill name."
    skill_path = skill_dir_path / "SKILL.md"
    content = load_skill_content(str(skill_path), skill_dir=skill_dir_path)
    return content or f"Error: Skill '{skill_name}' not found at {skill_path}."


def _skill_export(args: dict[str, Any], **kwargs) -> str:
    """Export a skill as a single self-contained markdown file."""
    config = kwargs.get("config", {})
    skills_dir = _get_skills_dir(config)
    skill_name = args.get("name", "")

    if not skill_name:
        return f"Error: Skill '{skill_name}' not found."

    skill_dir_path = _safe_skill_dir(skills_dir, skill_name)
    if skill_dir_path is None:
        return "Error: Invalid skill name."
    result = export_skill(skill_dir_path)
    return result or f"Error: Skill '{skill_name}' not found at {skill_dir_path}."


def _build_frontmatter(frontmatter: dict) -> str:
    """Serialize skill frontmatter with YAML-safe escaping.

    Raw string interpolation breaks on descriptions containing ': ',
    '#', or newlines — the skill then parses as having no frontmatter
    and silently disappears from discovery. safe_dump quotes exactly
    what needs quoting.
    """
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        + "\n---\n\n"
    )


def _skill_manage(args: dict[str, Any], **kwargs) -> str:
    """Create, update, or delete a skill."""
    config = kwargs.get("config", {})
    skills_dir = _get_skills_dir(config)
    action = args.get("action", "")
    name = args.get("name", "")

    if not name:
        return "Error: 'name' is required."

    skill_dir = _safe_skill_dir(skills_dir, name)
    if skill_dir is None:
        return "Error: Invalid skill name. Use letters, numbers, '-' or '_'."
    skill_file = skill_dir / "SKILL.md"

    if action == "create":
        if skill_file.exists():
            return f"Error: Skill '{name}' already exists."

        category = args.get("category", "general")
        description = args.get("description", "")
        content = args.get("content", "")

        if not isinstance(content, str):
            return "Error: 'content' must be a string."
        if not isinstance(description, str):
            return "Error: 'description' must be a string."
        if not isinstance(category, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]*", category
        ):
            return "Error: Invalid category. Use letters, numbers, '-' or '_'."

        skill_dir.mkdir(parents=True, exist_ok=True)
        frontmatter = _build_frontmatter(
            {"name": name, "category": category, "description": description}
        )
        skill_file.write_text(frontmatter + content, encoding="utf-8")
        return f"Created skill '{name}' at {skill_file}."

    elif action == "patch":
        if not skill_file.exists():
            return f"Error: Skill '{name}' not found."

        content = args.get("content", "")
        if not isinstance(content, str) or not content:
            return "Error: 'content' is required for patch action."

        existing = skill_file.read_text(encoding="utf-8")
        frontmatter_dict, body = parse_frontmatter(existing)
        if not frontmatter_dict:
            return f"Error: Skill '{name}' has no valid frontmatter; refusing to overwrite it."

        # Rebuild with updated body (safe_dump preserves lists, quotes, and special chars)
        fm_text = _build_frontmatter(frontmatter_dict)

        # Atomic write — same pattern as write_file
        import os as _os
        import tempfile as _tempfile

        fd, tmp_path = _tempfile.mkstemp(dir=skill_dir, suffix=".tmp")
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(fm_text + content)
            _os.replace(tmp_path, skill_file)
        except Exception:
            _os.unlink(tmp_path)
            raise
        return f"Updated skill '{name}'."

    elif action == "delete":
        if not skill_file.exists():
            return f"Error: Skill '{name}' not found."

        skill_file.unlink()
        # Remove empty directory
        if skill_dir.exists() and not any(skill_dir.iterdir()):
            skill_dir.rmdir()
        return f"Deleted skill '{name}'."

    else:
        return f"Error: Unknown action '{action}'. Use 'create', 'patch', or 'delete'."


registry.register(
    name="skill_export",
    toolset="skills",
    schema=SKILL_EXPORT_SCHEMA,
    handler=_skill_export,
    emoji="📤",
    is_read_only=True,
)

registry.register(
    name="skills_list",
    toolset="skills",
    schema=SKILLS_LIST_SCHEMA,
    handler=_skills_list,
    emoji="📚",
    is_read_only=True,
)

registry.register(
    name="skill_view",
    toolset="skills",
    schema=SKILL_VIEW_SCHEMA,
    handler=_skill_view,
    emoji="📖",
    is_read_only=True,
)

registry.register(
    name="skill_manage",
    toolset="skills",
    schema=SKILL_MANAGE_SCHEMA,
    handler=_skill_manage,
    emoji="✏️",
)
