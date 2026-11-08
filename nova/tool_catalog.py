from __future__ import annotations

import copy
import re
from typing import Any

from nova.tokens import estimate_total_request_tokens

DISCOVERY_TOOL = "discover_tools"
DISCOVERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": DISCOVERY_TOOL,
        "description": "Search tools and configured MCP servers by name or task. Activate matching tool schemas for the next model request. To connect a server returned in servers, repeat with its exact name in server and a tool/task query. Activation never grants permission.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                "server": {
                    "type": "string",
                    "description": "Exact configured MCP server name to connect; omit to search without connecting.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}
DEFAULT_CORE = ("read_file", "list_files", "search_files", "terminal", "skills_list", "skill_view")


class ToolCatalog:
    def __init__(self, definitions: list[dict], *, max_tokens: int, core_names: list[str]) -> None:
        if type(max_tokens) is not int or max_tokens < 1:
            raise ValueError("Tool schema budget must be a positive integer")
        self.max_tokens = max_tokens
        self.core_names = tuple(core_names)
        self._all: dict[str, dict] = {}
        self._active: list[str] = []
        self._search_words: dict[str, set[str]] = {}
        self.update(definitions)

    def _fits(self, names: list[str]) -> bool:
        return (
            estimate_total_request_tokens([], tools=[self._all[name] for name in names])
            <= self.max_tokens
        )

    def update(self, definitions: list[dict]) -> None:
        catalog = {definition["function"]["name"]: definition for definition in definitions}
        if DISCOVERY_TOOL in catalog:
            raise ValueError("Tool name discover_tools is reserved")
        catalog[DISCOVERY_TOOL] = DISCOVERY_SCHEMA
        if catalog == self._all:
            return
        previous = self._all
        self._all = copy.deepcopy(catalog)
        core = [DISCOVERY_TOOL, *dict.fromkeys(name for name in self.core_names if name in catalog)]
        if not self._fits(core):
            self._all = previous
            raise ValueError("Core tools exceed tool schema budget")
        self._search_words = {
            name: set(
                re.findall(
                    r"[a-z0-9]+",
                    f"{name} {definition['function'].get('description', '')}".casefold(),
                )
            )
            for name, definition in self._all.items()
        }
        if self._fits(list(catalog)):
            self._active = list(catalog)
        else:
            active = core[:]
            for name in self._active:
                if name in catalog and name not in active and self._fits([*active, name]):
                    active.append(name)
            self._active = active

    def definitions(self) -> list[dict]:
        return [copy.deepcopy(self._all[name]) for name in self._active]

    def is_active(self, name: str) -> bool:
        return name in self._active

    def discover(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("query must contain 1 to 1000 characters")
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("limit must be between 1 and 5")
        exact = query.strip() if query.strip() in self._all else None
        terms = set(re.findall(r"[a-z0-9]+", query.casefold()))
        scored = []
        for name in self._all:
            if name == DISCOVERY_TOOL or (exact is not None and name != exact):
                continue
            score = len(terms & self._search_words[name])
            if name.casefold() == query.strip().casefold():
                score += 10000
            if score:
                scored.append((-score, name))
        matches = []
        protected = {DISCOVERY_TOOL, *self.core_names}
        for _, name in sorted(scored)[:limit]:
            proposed = [active for active in self._active if active != name] + [name]
            while not self._fits(proposed):
                evictable = next(
                    (active for active in proposed if active not in protected and active != name),
                    None,
                )
                if evictable is None:
                    break
                proposed.remove(evictable)
            fits = self._fits(proposed)
            if fits:
                self._active = proposed
                protected.add(name)
            matches.append(
                {
                    "name": name,
                    "description": self._all[name]["function"].get("description", "")[:200],
                    "active": fits,
                    "reason": None if fits else "schema_budget",
                }
            )
        return {
            "matches": matches,
            "total_matches": len(scored),
            "active_tools": list(self._active),
            "schema_tokens": estimate_total_request_tokens([], tools=self.definitions()),
            "schema_budget": self.max_tokens,
        }
