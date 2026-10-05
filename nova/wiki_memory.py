"""Obsidian-compatible wiki memory.

Notes are stored as markdown files with YAML frontmatter in a configurable
vault directory. Supports wikilinks ([[title]]) and #tags natively.
"""

import contextlib
import fcntl
import functools
import logging
import os
import re
import tempfile
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from nova.context import find_content_threats

logger = logging.getLogger(__name__)

_INVALID_CHARS = re.compile(r'[\\:*?"<>|]')


def _synchronized(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._mutation_lock:
            outermost = self._mutation_depth == 0
            lock_file = self._mutation_file.open("a+") if outermost else None
            if lock_file is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._mutation_depth += 1
            try:
                return method(self, *args, **kwargs)
            finally:
                self._mutation_depth -= 1
                if lock_file is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    lock_file.close()

    return wrapper


class WikiMemory:
    """File-based wiki memory backed by an Obsidian-compatible vault."""

    def __init__(self, vault_path: Path, max_prompt_notes: int = 10):
        self.vault_path = vault_path
        self.max_prompt_notes = max_prompt_notes
        self._mutation_lock = threading.RLock()
        self._mutation_depth = 0
        vault_path.mkdir(parents=True, exist_ok=True)
        self._mutation_file = vault_path / ".nova-mutation.lock"
        self._mutation_file.touch(exist_ok=True)
        self._mutation_file.chmod(0o600)

    def _note_path(self, title: str, create_parent: bool = True) -> Path:
        """Convert a title (optionally with path prefix) to an absolute .md path.

        Rejects empty titles, dot-only titles, and titles that would escape
        the vault directory (e.g. via '..').
        """
        sanitized = _INVALID_CHARS.sub("_", title.strip())
        if not sanitized or sanitized in (".", "/"):
            raise ValueError(f"Invalid note title (empty or dot-only): {title!r}")
        parts = Path(sanitized)
        if parts.is_absolute() or any(p == ".." for p in parts.parts):
            raise ValueError(f"Invalid note title (path traversal): {title!r}")
        if not parts.name or parts.name == ".":
            raise ValueError(f"Invalid note title (no filename): {title!r}")
        path = self.vault_path / parts.parent / (parts.name + ".md")
        vault_resolved = self.vault_path.resolve()
        if (
            not str(path.resolve()).startswith(str(vault_resolved) + os.sep)
            and path.resolve() != vault_resolved
        ):
            raise ValueError(f"Invalid note title (escapes vault): {title!r}")
        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _parse_note(self, path: Path) -> dict:
        return self._parse_note_text(path.read_text(encoding="utf-8", errors="replace"))

    def _parse_note_text(self, text: str) -> dict:
        frontmatter: dict = {}
        content = text
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end != -1:
                with contextlib.suppress(yaml.YAMLError):
                    loaded = yaml.safe_load(text[4:end])
                    frontmatter = loaded if isinstance(loaded, dict) else {}
                content = text[end + 5 :]
        return {"frontmatter": frontmatter, "content": content.lstrip("\n")}

    def _format_note(self, frontmatter: dict, content: str) -> str:
        fm_str = yaml.dump(
            frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        return f"---\n{fm_str}---\n\n{content}"

    def _write_atomic(self, path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except Exception:
            os.unlink(tmp)
            raise

    @_synchronized
    def write(self, title: str, content: str, tags: list[str] | None = None) -> dict:
        """Create or overwrite a note."""
        path = self._note_path(title)
        now = datetime.now().isoformat(timespec="seconds")
        frontmatter: dict = {
            "title": title,
            "tags": tags or [],
            "created": now,
            "modified": now,
        }
        if path.exists():
            existing = self._parse_note(path)
            frontmatter["created"] = existing["frontmatter"].get("created", now)
        self._write_atomic(path, self._format_note(frontmatter, content))
        return {"status": "written", "path": str(path.relative_to(self.vault_path))}

    @_synchronized
    def append(self, title: str, content: str) -> dict:
        """Append content to an existing note, or create it if absent."""
        path = self._note_path(title)
        if not path.exists():
            return self.write(title, content)
        parsed = self._parse_note(path)
        parsed["frontmatter"]["modified"] = datetime.now().isoformat(timespec="seconds")
        new_content = parsed["content"].rstrip() + "\n\n" + content
        self._write_atomic(path, self._format_note(parsed["frontmatter"], new_content))
        return {"status": "appended", "path": str(path.relative_to(self.vault_path))}

    @_synchronized
    def patch(self, title: str, old_text: str, new_text: str, count: int = 0) -> dict:
        """Replace occurrences of old_text with new_text in a note's content.

        count=0 replaces all occurrences; count=N replaces the first N.
        Returns {"status": "patched", "replacements": N} or an error dict.
        """
        path = self._note_path(title, create_parent=False)
        if not path.exists():
            return {"status": "not_found", "error": f"Note not found: '{title}'"}
        parsed = self._parse_note(path)
        original = parsed["content"]
        if old_text not in original:
            return {"status": "no_match", "error": f"Text not found in '{title}'"}
        if count > 0:
            new_content = original.replace(old_text, new_text, count)
            replacements = min(original.count(old_text), count)
        else:
            new_content = original.replace(old_text, new_text)
            replacements = original.count(old_text)
        parsed["frontmatter"]["modified"] = datetime.now().isoformat(timespec="seconds")
        self._write_atomic(path, self._format_note(parsed["frontmatter"], new_content))
        return {
            "status": "patched",
            "path": str(path.relative_to(self.vault_path)),
            "replacements": replacements,
        }

    @_synchronized
    def vault_replace(self, old_text: str, new_text: str, count: int = 0) -> dict:
        """Replace old_text with new_text across every note in the vault.

        count=0 replaces all occurrences per note; count=N replaces first N per note.
        Returns {"patched_notes": [...titles], "total_replacements": N}.
        """
        patched: list[str] = []
        total = 0
        for md_file in sorted(self.vault_path.rglob("*.md")):
            try:
                parsed = self._parse_note(md_file)
            except (OSError, UnicodeError):
                continue
            original = parsed["content"]
            if old_text not in original:
                continue
            if count > 0:
                new_content = original.replace(old_text, new_text, count)
                n = min(original.count(old_text), count)
            else:
                new_content = original.replace(old_text, new_text)
                n = original.count(old_text)
            parsed["frontmatter"]["modified"] = datetime.now().isoformat(timespec="seconds")
            self._write_atomic(md_file, self._format_note(parsed["frontmatter"], new_content))
            patched.append(parsed["frontmatter"].get("title", md_file.stem))
            total += n
        return {"patched_notes": patched, "total_replacements": total}

    @_synchronized
    def add_tag(self, title: str, tag: str) -> dict:
        """Add a tag to a note's frontmatter. No-op if the tag already exists."""
        path = self._note_path(title, create_parent=False)
        if not path.exists():
            return {"status": "not_found", "error": f"Note not found: '{title}'"}
        parsed = self._parse_note(path)
        tags: list[str] = list(parsed["frontmatter"].get("tags") or [])
        if tag in tags:
            return {"status": "already_present", "tag": tag}
        tags.append(tag)
        parsed["frontmatter"]["tags"] = tags
        parsed["frontmatter"]["modified"] = datetime.now().isoformat(timespec="seconds")
        self._write_atomic(path, self._format_note(parsed["frontmatter"], parsed["content"]))
        return {"status": "added", "tag": tag, "path": str(path.relative_to(self.vault_path))}

    @_synchronized
    def remove_tag(self, title: str, tag: str) -> dict:
        """Remove a tag from a note's frontmatter. No-op if the tag is absent."""
        path = self._note_path(title, create_parent=False)
        if not path.exists():
            return {"status": "not_found", "error": f"Note not found: '{title}'"}
        parsed = self._parse_note(path)
        tags: list[str] = list(parsed["frontmatter"].get("tags") or [])
        if tag not in tags:
            return {"status": "not_present", "tag": tag}
        parsed["frontmatter"]["tags"] = [t for t in tags if t != tag]
        parsed["frontmatter"]["modified"] = datetime.now().isoformat(timespec="seconds")
        self._write_atomic(path, self._format_note(parsed["frontmatter"], parsed["content"]))
        return {"status": "removed", "tag": tag, "path": str(path.relative_to(self.vault_path))}

    @_synchronized
    def pin(self, title: str) -> dict:
        """Set inject:true on a note so its full content appears in every system prompt."""
        path = self._note_path(title, create_parent=False)
        if not path.exists():
            return {"status": "not_found", "error": f"Note not found: '{title}'"}
        parsed = self._parse_note(path)
        if parsed["frontmatter"].get("inject"):
            return {"status": "already_pinned"}
        parsed["frontmatter"]["inject"] = True
        parsed["frontmatter"]["modified"] = datetime.now().isoformat(timespec="seconds")
        self._write_atomic(path, self._format_note(parsed["frontmatter"], parsed["content"]))
        return {"status": "pinned", "path": str(path.relative_to(self.vault_path))}

    @_synchronized
    def unpin(self, title: str) -> dict:
        """Remove inject:true from a note's frontmatter."""
        path = self._note_path(title, create_parent=False)
        if not path.exists():
            return {"status": "not_found", "error": f"Note not found: '{title}'"}
        parsed = self._parse_note(path)
        if not parsed["frontmatter"].get("inject"):
            return {"status": "not_pinned"}
        parsed["frontmatter"].pop("inject", None)
        parsed["frontmatter"]["modified"] = datetime.now().isoformat(timespec="seconds")
        self._write_atomic(path, self._format_note(parsed["frontmatter"], parsed["content"]))
        return {"status": "unpinned", "path": str(path.relative_to(self.vault_path))}

    def read(self, title: str) -> dict | None:
        """Return parsed note or None if not found."""
        path = self._note_path(title, create_parent=False)
        if not path.exists():
            return None
        return self._parse_note(path)

    def search(self, query: str) -> list[dict]:
        """Case-insensitive full-text search across all notes."""
        query_lower = query.lower()
        results = []
        for md_file in sorted(self.vault_path.rglob("*.md")):
            try:
                text = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if query_lower not in text.lower():
                continue
            parsed = self._parse_note(md_file)
            results.append(
                {
                    "title": parsed["frontmatter"].get("title", md_file.stem),
                    "path": str(md_file.relative_to(self.vault_path)),
                    "tags": parsed["frontmatter"].get("tags", []),
                    "content": parsed["content"],
                }
            )
        return results

    def list_notes(self, tag: str | None = None) -> list[dict]:
        """List notes sorted by most recently modified, optionally filtered by tag."""
        notes = []
        for md_file in sorted(
            self.vault_path.rglob("*.md"),
            key=lambda f: _safe_mtime(f),
            reverse=True,
        ):
            try:
                parsed = self._parse_note(md_file)
            except (OSError, UnicodeError):
                continue
            note_tags = parsed["frontmatter"].get("tags") or []
            if tag and tag not in note_tags:
                continue
            notes.append(
                {
                    "title": parsed["frontmatter"].get("title", md_file.stem),
                    "path": str(md_file.relative_to(self.vault_path)),
                    "tags": note_tags,
                    "modified": parsed["frontmatter"].get("modified", ""),
                    "content": parsed["content"],
                    "inject": bool(parsed["frontmatter"].get("inject", False)),
                }
            )
        return notes

    @_synchronized
    def delete(self, title: str) -> bool:
        """Delete a note by title. Returns True if deleted."""
        path = self._note_path(title, create_parent=False)
        if path.exists():
            path.unlink()
            return True
        return False

    def maintenance(self, stale_days: int = 90) -> dict:
        """Read-only analysis of the vault.

        Returns a report of duplicate candidates, orphan notes (no wikilinks
        in or out), broken links (wikilinks to non-existent notes), and stale
        notes. Does not modify anything — the agent decides what action to take.
        """
        notes_data = []
        for md_file in self.vault_path.rglob("*.md"):
            try:
                parsed = self._parse_note(md_file)
            except (OSError, UnicodeError):
                continue
            title = parsed["frontmatter"].get("title", md_file.stem)
            content = parsed["content"]
            wikilinks = re.findall(r"\[\[([^\]]+)\]\]", content)
            modified_str = parsed["frontmatter"].get("modified", "")
            try:
                modified = datetime.fromisoformat(modified_str)
            except (ValueError, TypeError):
                modified = datetime.fromtimestamp(_safe_mtime(md_file))
            notes_data.append(
                {
                    "title": title,
                    "path": str(md_file.relative_to(self.vault_path)),
                    "wikilinks": wikilinks,
                    "modified": modified,
                    "tags": parsed["frontmatter"].get("tags") or [],
                }
            )

        duplicates = _find_title_duplicates(notes_data)

        # Build a resolution set (title, basename, and path stem) for link checking
        title_set: set[str] = set()
        for note in notes_data:
            title_set.add(note["title"].lower())
            title_set.add(note["title"].rsplit("/", 1)[-1].lower())
            title_set.add(Path(note["path"]).stem.lower())

        referenced: set[str] = set()
        broken_links: list[dict] = []
        for note in notes_data:
            for link_raw in note["wikilinks"]:
                link_target = link_raw.split("|")[0].strip()
                referenced.add(link_target.lower())
                if link_target.lower() not in title_set:
                    broken_links.append({"source": note["title"], "broken_link": link_target})

        orphans = [
            {"title": n["title"], "path": n["path"]}
            for n in notes_data
            if n["title"].lower() not in referenced and not n["wikilinks"]
        ]

        cutoff = datetime.now() - timedelta(days=stale_days)
        stale = [
            {
                "title": n["title"],
                "path": n["path"],
                "days_old": (datetime.now() - n["modified"]).days,
            }
            for n in notes_data
            if n["modified"] < cutoff
        ]

        tag_counts: dict[str, int] = {}
        for n in notes_data:
            for tag in n["tags"]:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        return {
            "total_notes": len(notes_data),
            "duplicate_candidates": duplicates,
            "broken_links": broken_links,
            "orphans": orphans,
            "stale": stale,
            "tag_counts": tag_counts,
        }

    def follow(
        self, title: str, depth: int = 2, max_notes: int = 10, include_content: bool = False
    ) -> dict:
        """BFS traversal from a starting note, following [[wikilinks]].

        Pure Python — zero LLM calls during traversal. Reads all reachable notes
        in a single pass and returns the full graph result.

        include_content=True embeds each note's full content so the caller can
        read the entire neighbourhood in one tool call instead of following up
        with individual wiki read calls.
        """
        index = self._title_index()
        start_key = title.lower()
        if start_key not in index:
            return {"error": f"Note not found: '{title}'"}

        visited: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(title, 0)])
        nodes: list[dict] = []

        while queue and len(nodes) < max_notes:
            current_title, current_depth = queue.popleft()
            key = current_title.lower()
            if key in visited:
                continue
            visited[key] = current_depth

            path = index.get(key)
            if path is None:
                continue
            try:
                parsed = self._parse_note(path)
            except (OSError, UnicodeError):
                continue

            content = parsed["content"]
            fm = parsed["frontmatter"]
            actual_title = fm.get("title", path.stem)
            tags = fm.get("tags") or []
            first_line = content.split("\n")[0][:120] if content else ""
            raw_links = re.findall(r"\[\[([^\]]+)\]\]", content)
            links = [lnk.split("|")[0].strip() for lnk in raw_links]

            node: dict = {
                "title": actual_title,
                "depth": current_depth,
                "tags": tags,
                "first_line": first_line,
                "links_to": links,
            }
            if include_content:
                node["content"] = content
            nodes.append(node)

            if current_depth < depth:
                for link in links:
                    link_key = link.lower()
                    if link_key not in visited and link_key in index:
                        queue.append((link, current_depth + 1))  # type: ignore[arg-type]

        return {"start": title, "depth": depth, "nodes_found": len(nodes), "nodes": nodes}

    def backlinks(self, title: str) -> list[dict]:
        """Find all notes that link to the given title via [[wikilinks]].

        Matches [[Title]], [[Title|alias]], and is case-insensitive.
        """
        pattern = re.compile(r"\[\[" + re.escape(title) + r"(?:\|[^\]]+)?\]\]", re.IGNORECASE)
        results = []
        for md_file in sorted(self.vault_path.rglob("*.md")):
            try:
                text = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if not pattern.search(text):
                continue
            parsed = self._parse_note_text(text)
            results.append(
                {
                    "title": parsed["frontmatter"].get("title", md_file.stem),
                    "path": str(md_file.relative_to(self.vault_path)),
                    "excerpt": _excerpt(text, "[[" + title.lower()),
                }
            )
        return results

    @_synchronized
    def rename(self, old_title: str, new_title: str) -> dict:
        """Rename a note and update all [[wikilinks]] that point to the old title.

        Atomically: writes to new path, deletes old path, then rewrites backlinks
        in all other notes. Returns not_found if the source doesn't exist, or an
        error dict if the destination already exists.
        """
        old_path = self._note_path(old_title)
        if not old_path.exists():
            return {"status": "not_found"}
        new_path = self._note_path(new_title)
        if new_path.exists():
            return {"status": "error", "reason": f"A note named '{new_title}' already exists."}

        parsed = self._parse_note(old_path)
        parsed["frontmatter"]["title"] = new_title
        parsed["frontmatter"]["modified"] = datetime.now().isoformat(timespec="seconds")
        self._write_atomic(new_path, self._format_note(parsed["frontmatter"], parsed["content"]))
        old_path.unlink()

        # Rewrite [[OldTitle]] → [[NewTitle]] (preserving any |alias suffix) in all other notes
        pattern = re.compile(r"\[\[" + re.escape(old_title) + r"((?:\|[^\]]+)?)\]\]", re.IGNORECASE)
        updated_notes: list[str] = []
        for md_file in self.vault_path.rglob("*.md"):
            if md_file == new_path:
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if not pattern.search(text):
                continue
            new_text = pattern.sub(lambda m: f"[[{new_title}{m.group(1)}]]", text)
            self._write_atomic(md_file, new_text)
            bl_parsed = self._parse_note(md_file)
            updated_notes.append(bl_parsed["frontmatter"].get("title", md_file.stem))

        return {
            "status": "renamed",
            "old_title": old_title,
            "new_title": new_title,
            "old_path": str(old_path.relative_to(self.vault_path)),
            "new_path": str(new_path.relative_to(self.vault_path)),
            "backlinks_updated": updated_notes,
        }

    def list_tags(self) -> dict[str, int]:
        """Return all tags across the vault with counts, sorted by frequency."""
        tag_counts: dict[str, int] = {}
        for md_file in self.vault_path.rglob("*.md"):
            try:
                parsed = self._parse_note(md_file)
            except (OSError, UnicodeError):
                continue
            for tag in parsed["frontmatter"].get("tags") or []:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return dict(sorted(tag_counts.items(), key=lambda x: (-x[1], x[0])))

    @_synchronized
    def rename_tag(self, old_tag: str, new_tag: str) -> dict:
        """Rename (or delete when new_tag is empty) a tag globally across all notes."""
        updated: list[str] = []
        for md_file in self.vault_path.rglob("*.md"):
            try:
                parsed = self._parse_note(md_file)
            except (OSError, UnicodeError):
                continue
            tags = parsed["frontmatter"].get("tags") or []
            if old_tag not in tags:
                continue
            if new_tag:
                parsed["frontmatter"]["tags"] = [new_tag if t == old_tag else t for t in tags]
            else:
                parsed["frontmatter"]["tags"] = [t for t in tags if t != old_tag]
            parsed["frontmatter"]["modified"] = datetime.now().isoformat(timespec="seconds")
            self._write_atomic(md_file, self._format_note(parsed["frontmatter"], parsed["content"]))
            updated.append(parsed["frontmatter"].get("title", md_file.stem))
        return {
            "status": "renamed",
            "old_tag": old_tag,
            "new_tag": new_tag,
            "updated_notes": updated,
        }

    def _title_index(self) -> dict[str, Path]:
        """Case-insensitive map of title/stem → file path for all vault notes."""
        index: dict[str, Path] = {}
        for md_file in self.vault_path.rglob("*.md"):
            try:
                parsed = self._parse_note(md_file)
            except (OSError, UnicodeError):
                continue
            title = parsed["frontmatter"].get("title", md_file.stem)
            index[title.lower()] = md_file
            stem_lower = md_file.stem.lower()
            if stem_lower != title.lower():
                index[stem_lower] = md_file
        return index

    def format_for_prompt(self, max_chars: int = 3000, core_max_chars: int = 2000) -> str:
        """Compose the wiki section of the system prompt.

        Three parts:
        1. Full content of Core/ notes — always-in-context identity/preferences.
        2. Full content of inject:true notes — agent-pinned context anywhere in vault.
        3. Compact index of recent notes — searchable handles for everything else.
        """
        # Single vault scan instead of three separate rglob passes.
        core: list[tuple[Path, dict]] = []
        pinned: list[tuple[Path, dict]] = []
        recent: list[tuple[Path, dict]] = []

        for md_file in self.vault_path.rglob("*.md"):
            try:
                parsed = self._parse_note(md_file)
            except (OSError, UnicodeError):
                continue
            rel = md_file.relative_to(self.vault_path)
            if rel.parts and rel.parts[0] == "Core":
                core.append((md_file, parsed))
            elif parsed["frontmatter"].get("inject"):
                pinned.append((md_file, parsed))
            else:
                recent.append((md_file, parsed))

        core.sort(key=lambda x: x[0])
        pinned.sort(key=lambda x: x[0])
        recent.sort(key=lambda x: _safe_mtime(x[0]), reverse=True)

        core_block = self._format_full_block(
            core, core_max_chars, "wiki_core", "Always-in-context facts"
        )
        pinned_block = self._format_full_block(
            pinned, core_max_chars, "wiki_pinned", "Pinned notes (always in context)"
        )
        index_block = self._format_index_block(recent[: self.max_prompt_notes])

        if not core_block and not pinned_block and not index_block:
            return ""

        parts = [b for b in (core_block, pinned_block, index_block) if b]
        result = ""
        for block in parts:
            separator = "\n\n" if result else ""
            remaining = max_chars - len(result) - len(separator)
            if remaining <= 0:
                break
            if len(block) > remaining:
                block = _truncate_xml(block, remaining)
            result += separator + block
            if len(block) < remaining:
                continue
            break
        return result

    def _format_full_block(
        self,
        notes: list[tuple[Path, dict]],
        max_chars: int,
        xml_tag: str,
        header: str,
    ) -> str:
        """Render a list of pre-parsed notes as a full-content XML block."""
        sections: list[str] = []
        total = len(f"<{xml_tag}>\n{header}:\n\n</{xml_tag}>")
        for md_file, parsed in notes:
            title = parsed["frontmatter"].get("title", md_file.stem)
            content = parsed["content"].strip()
            if not content:
                continue
            # These notes reach every future system prompt; anything the model
            # was tricked into writing here would otherwise persist.
            if threats := find_content_threats(content):
                logger.warning("Wiki note %s blocked: %s", md_file.name, ", ".join(threats))
                content = (
                    f"[BLOCKED: note contained potential prompt injection ({', '.join(threats)})]"
                )
            section = f"### [[{title}]]\n{content}"
            remaining = max_chars - total - (2 if sections else 0)
            if remaining <= 0:
                continue
            if len(section) > remaining:
                # Do not let one large Core note hide all subsequent notes.
                continue
            sections.append(section)
            total += len(section) + (2 if len(sections) > 1 else 0)
        if not sections:
            return ""
        return f"<{xml_tag}>\n{header}:\n\n" + "\n\n".join(sections) + f"\n</{xml_tag}>"

    def _format_index_block(self, notes: list[tuple[Path, dict]]) -> str:
        """Render a compact index of pre-parsed notes sorted by recency."""
        if not notes:
            return ""
        lines = ["<wiki_memory>", "Recent knowledge notes (use wiki tool to read or search):"]
        for md_file, parsed in notes:
            fm = parsed["frontmatter"]
            title = fm.get("title", md_file.stem)
            note_tags = fm.get("tags") or []
            first_line = parsed["content"].split("\n")[0][:120] if parsed["content"] else ""
            tags_str = " ".join(f"#{t}" for t in note_tags) if note_tags else ""
            line = f"- [[{title}]]"
            if tags_str:
                line += f" {tags_str}"
            if first_line:
                line += f" — {first_line}"
            lines.append(line)
        lines.append("</wiki_memory>")
        return "\n".join(lines)


def _find_title_duplicates(notes: list[dict]) -> list[dict]:
    """Find pairs of notes with very similar titles."""
    duplicates = []
    for i, a in enumerate(notes):
        for b in notes[i + 1 :]:
            if _title_similar(a["title"], b["title"]):
                duplicates.append(
                    {
                        "titles": [a["title"], b["title"]],
                        "paths": [a["path"], b["path"]],
                    }
                )
    return duplicates


def _title_similar(a: str, b: str) -> bool:
    """Heuristic for flagging duplicate candidates.

    True if: titles match exactly, share a basename (Obsidian wikilinks
    resolve by basename), one contains the other, or word overlap >=60%.
    """
    a_low = a.lower().strip()
    b_low = b.lower().strip()
    if a_low == b_low:
        return True
    if a_low.rsplit("/", 1)[-1] == b_low.rsplit("/", 1)[-1]:
        return True
    if a_low in b_low or b_low in a_low:
        return True
    a_words = set(re.findall(r"\w+", a_low))
    b_words = set(re.findall(r"\w+", b_low))
    if not a_words or not b_words:
        return False
    overlap = len(a_words & b_words)
    min_len = min(len(a_words), len(b_words))
    return overlap / min_len >= 0.6


def _excerpt(text: str, query: str, window: int = 120) -> str:
    """Return a short excerpt around the first match of query in text."""
    idx = text.lower().find(query)
    if idx == -1:
        return text[:window]
    start = max(0, idx - window // 2)
    end = min(len(text), idx + window // 2)
    snippet = text[start:end].replace("\n", " ")
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet += "…"
    return snippet


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _truncate_xml(text: str, max_chars: int) -> str:
    """Trim rendered memory while retaining its outer XML element."""
    if max_chars <= 0:
        return ""
    opening_end = text.find("\n")
    closing_start = text.rfind("\n</")
    if opening_end == -1 or closing_start <= opening_end:
        return text[:max_chars]
    opening = text[: opening_end + 1]
    closing = text[closing_start:]
    if len(opening) + len(closing) >= max_chars:
        return (opening + closing)[:max_chars]
    return (
        opening
        + text[opening_end + 1 : opening_end + 1 + max_chars - len(opening) - len(closing)]
        + closing
    )
