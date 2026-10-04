"""Configuration loading and validation."""

import copy
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any, cast

import yaml

DEFAULT_CONFIG = {
    "llm": {
        "provider": "openai",
        "api_key": "",
        "model": "qwen/qwen3.6-flash",
        "base_url": "https://openrouter.ai/api/v1",
        "max_tokens": 8192,
        # 0 = auto: use provider-reported context window, else the 128k
        # fallback. Set explicitly (e.g. 1_000_000 for 1M-token models)
        # to override what the provider reports.
        "context_window": 0,
    },
    "web": {
        "enabled": True,
        "firecrawl_api_key": "",
        "timeout_seconds": 30,
    },
    "agent": {
        "identity": (
            "You are Nova, a capable personal AI agent. "
            "You are direct, efficient, and focused on being genuinely useful. "
            "You take action using tools rather than describing what you would do. "
            "Admit uncertainty when appropriate. Prioritize completing tasks over explaining them."
        ),
        "max_iterations": 50,
        "temperature": 0.7,
        "top_p": 1.0,
        "stream_include_usage": True,
    },
    "budgets": {
        "system_prompt_max": 16000,
        "skills_max_chars": 15000,
        "skills_max_count": 50,
        "context_file_max_chars": 10000,
        "context_total_max_chars": 50000,
        "tool_result_max_chars": 8000,
        "tool_result_max_tokens": 12000,
        "conversation_turn_limit": 15,
    },
    "context_files": ["NOVA.md", "AGENTS.md"],
    "wiki": {
        "enabled": True,
        "vault_path": "~/.nova/wiki",
        "max_prompt_notes": 10,
    },
    "skills": {
        "enabled": True,
        "directory": "~/.nova/skills",
    },
    "session": {
        "enabled": True,
        "directory": "~/.nova/sessions",
    },
    "logging": {
        "level": "INFO",
        "file": "~/.nova/nova.log",
    },
    "delegation": {
        "enabled": False,
        "max_spawn_depth": 2,
        "default_timeout_seconds": 60,
        "subagent_budgets": {
            "max_iterations": 30,
            "system_prompt_max": 8000,
            "tool_result_max_chars": 4000,
            "tool_result_max_tokens": 4000,
        },
    },
    "permissions": {
        "mode": "ask",
        "denied_tools": [],
        "allowed_tools": [],
        "denied_commands": [],
        "path_rules": [],
    },
    "mcp": {
        "servers": {},
    },
    "cost_tracking": {
        "enabled": True,
    },
    "observability": {
        "enabled": False,
        "provider": "langfuse",
        "sample_rate": 1.0,
        "capture_input": False,
        "capture_output": False,
        "environment": "",
        "release": "",
        "langfuse": {
            "public_key": "",
            "secret_key": "",
            "base_url": "https://cloud.langfuse.com",
            "flush_at_shutdown": True,
        },
    },
    "microcompact": {
        "enabled": True,
        "keep_recent": 6,
    },
    "tasks": {
        "max_concurrent": 4,
        "max_output_bytes": 100000,
    },
    "retry": {
        "max_retries": 3,
        "base_delay": 1.0,
        "max_delay": 60.0,
    },
}


class ConfigError(ValueError):
    """Raised when configuration values cannot be used safely."""


def set_model(config: dict[str, Any], model: str) -> None:
    llm = config.setdefault("llm", {})
    if llm.get("model") != model:
        llm["context_window"] = 0
    llm["model"] = model


def _validate_config(config: dict[str, Any]) -> None:
    """Validate resource and model controls before they reach the agent loop."""
    sections = ("llm", "agent", "budgets", "microcompact", "retry")
    for section in sections:
        if not isinstance(config.get(section), dict):
            raise ConfigError(f"Config section '{section}' must be a mapping")

    agent = config["agent"]
    llm = config["llm"]
    if llm.get("provider", "openai") != "openai":
        raise ConfigError("llm.provider must be 'openai'")
    if not isinstance(llm.get("model"), str) or not llm["model"]:
        raise ConfigError("llm.model must be a non-empty string")
    if not isinstance(llm.get("api_key", ""), str):
        raise ConfigError("llm.api_key must be a string")
    if not isinstance(llm.get("base_url", ""), str):
        raise ConfigError("llm.base_url must be a string")
    if not isinstance(llm.get("max_tokens", 8192), int) or llm.get("max_tokens", 8192) < 1:
        raise ConfigError("llm.max_tokens must be a positive integer")
    if type(llm.get("context_window", 0)) is not int or llm.get("context_window", 0) < 0:
        raise ConfigError("llm.context_window must be a non-negative integer (0 = auto)")
    if not isinstance(agent.get("max_iterations"), int) or not 1 <= agent["max_iterations"] <= 1000:
        raise ConfigError("agent.max_iterations must be an integer between 1 and 1000")
    for name, low, high in (("temperature", 0.0, 2.0), ("top_p", 0.0, 1.0)):
        value = agent.get(name)
        if not isinstance(value, (int, float)) or not low <= value <= high:
            raise ConfigError(f"agent.{name} must be between {low} and {high}")
    if not isinstance(agent.get("stream_include_usage"), bool):
        raise ConfigError("agent.stream_include_usage must be a boolean")

    budgets = config["budgets"]
    for name, value in budgets.items():
        if not isinstance(value, int) or value < 1:
            raise ConfigError(f"budgets.{name} must be a positive integer")

    microcompact = config["microcompact"]
    if not isinstance(microcompact.get("keep_recent"), int) or microcompact["keep_recent"] < 0:
        raise ConfigError("microcompact.keep_recent must be a non-negative integer")

    observability = config.get("observability", {})
    if not isinstance(observability, dict):
        raise ConfigError("Config section 'observability' must be a mapping")
    sample_rate = observability.get("sample_rate", 1.0)
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, (int, float))
        or not 0.0 <= sample_rate <= 1.0
    ):
        raise ConfigError("observability.sample_rate must be between 0.0 and 1.0")
    if observability.get("provider", "langfuse") not in {"langfuse"}:
        raise ConfigError("observability.provider must be 'langfuse'")
    langfuse = observability.get("langfuse", {})
    if not isinstance(langfuse, dict):
        raise ConfigError("observability.langfuse must be a mapping")
    for name in ("enabled", "capture_input", "capture_output"):
        if not isinstance(observability.get(name, False), bool):
            raise ConfigError(f"observability.{name} must be a boolean")
    for name in ("provider", "environment", "release"):
        if name in observability and not isinstance(observability[name], str):
            raise ConfigError(f"observability.{name} must be a string")
    for name in ("public_key", "secret_key", "base_url"):
        if name in langfuse and not isinstance(langfuse[name], str):
            raise ConfigError(f"observability.langfuse.{name} must be a string")
    if not isinstance(langfuse.get("flush_at_shutdown", True), bool):
        raise ConfigError("observability.langfuse.flush_at_shutdown must be a boolean")

    retry = config["retry"]
    if not isinstance(retry.get("max_retries"), int) or not 0 <= retry["max_retries"] <= 10:
        raise ConfigError("retry.max_retries must be between 0 and 10")
    for name in ("base_delay", "max_delay"):
        if not isinstance(retry.get(name), (int, float)) or retry[name] < 0:
            raise ConfigError(f"retry.{name} must be non-negative")

    tasks = config.get("tasks", {})
    if not isinstance(tasks, dict):
        raise ConfigError("Config section 'tasks' must be a mapping")
    if not isinstance(tasks.get("max_concurrent"), int) or not 1 <= tasks["max_concurrent"] <= 32:
        raise ConfigError("tasks.max_concurrent must be an integer between 1 and 32")
    if (
        not isinstance(tasks.get("max_output_bytes"), int)
        or not 1 <= tasks["max_output_bytes"] <= 10_000_000
    ):
        raise ConfigError("tasks.max_output_bytes must be between 1 and 10000000")

    web = config.get("web", {})
    if not isinstance(web, dict):
        raise ConfigError("Config section 'web' must be a mapping")
    if not isinstance(web.get("enabled", True), bool):
        raise ConfigError("web.enabled must be a boolean")
    timeout_seconds = web.get("timeout_seconds", 30)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 300
    ):
        raise ConfigError("web.timeout_seconds must be an integer between 1 and 300")

    mcp = config.get("mcp", {})
    if not isinstance(mcp, dict):
        raise ConfigError("Config section 'mcp' must be a mapping")
    servers = mcp.get("servers", {})
    if not isinstance(servers, dict):
        raise ConfigError("mcp.servers must be a mapping")
    for name, server in servers.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("mcp server names must be non-empty strings")
        if not isinstance(server, dict):
            raise ConfigError(f"mcp.servers.{name} must be a mapping")
        server_type = server.get("type", "stdio")
        if server_type not in {"stdio", "http", "sse"}:
            raise ConfigError(f"mcp.servers.{name}.type must be stdio, http, or sse")
        endpoint_key = "command" if server_type == "stdio" else "url"
        if not isinstance(server.get(endpoint_key), str) or not server[endpoint_key]:
            raise ConfigError(f"mcp.servers.{name}.{endpoint_key} must be a non-empty string")
        for key in ("args", "env") if server_type == "stdio" else ("headers",):
            value = server.get(key, [] if key == "args" else {})
            expected = list if key == "args" else dict
            if not isinstance(value, expected):
                raise ConfigError(
                    f"mcp.servers.{name}.{key} must be a {key[:-1] if key == 'args' else 'mapping'}"
                )
        if server_type != "stdio":
            timeout = server.get("timeout", 30.0)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ConfigError(f"mcp.servers.{name}.timeout must be positive")


def _resolve_env_vars(value: Any) -> Any:
    """Resolve ${ENV_VAR} and $ENV_VAR placeholders in config values."""
    if isinstance(value, str):

        def _replace(match: re.Match) -> str:
            var_name = match.group(1) or match.group(2) or ""
            return os.environ.get(var_name) or match.group(0)

        # Handle both ${VAR} and $VAR forms
        return re.sub(r"\$\{(\w+)\}|\$(\w+)", _replace, value)
    return value


def _deep_resolve(config: dict[str, Any]) -> dict[str, Any]:
    """Recursively resolve env vars in config."""
    result: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            result[key] = _deep_resolve(value)  # type: ignore[arg-type]
        else:
            result[key] = _resolve_env_vars(value)
    return result


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge override into base."""
    result = base.copy()
    if (
        "llm" in override
        and isinstance(override["llm"], dict)
        and isinstance(base.get("llm"), dict)
        and "model" in override["llm"]
        and override["llm"]["model"] != base["llm"].get("model")
    ):
        result["llm"] = {**base["llm"], "context_window": 0}
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


_KNOWN_TOP_LEVEL_KEYS: frozenset[str] = frozenset(DEFAULT_CONFIG.keys()) | frozenset({"openrouter"})
# Internal keys set at runtime (not from user config files)
_RUNTIME_KEYS: frozenset[str] = frozenset({"_subagent_depth"})


def _warn_unknown_keys(user_config: dict[str, Any], source: str) -> None:
    """Warn about unrecognised top-level keys in a user-supplied config."""
    logger = logging.getLogger(__name__)
    unknown = set(user_config.keys()) - _KNOWN_TOP_LEVEL_KEYS - _RUNTIME_KEYS
    for key in sorted(unknown):
        logger.warning("Unknown config key '%s' in %s (typo?)", key, source)


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load configuration from YAML files, falling back to defaults.

    Config is loaded in layers (later layers override earlier ones):
    1. DEFAULT_CONFIG (built-in defaults)
    2. ~/.nova/config.yaml (global config, if it exists)
    3. config.yaml in the current directory (local config, if it exists)
    4. Explicit config_path (if provided, overrides local config)
    """
    config = copy.deepcopy(DEFAULT_CONFIG)

    # Layer 2: Global config (~/.nova/config.yaml)
    global_config_path = get_nova_home() / "config.yaml"
    if global_config_path.exists():
        # Check file permissions — warn if world-readable
        file_stat = global_config_path.stat()
        if file_stat.st_mode & (stat.S_IRGRP | stat.S_IROTH):
            logger = logging.getLogger(__name__)
            logger.warning(
                "Config file %s is world-readable. Consider: chmod 600 %s",
                global_config_path,
                global_config_path,
            )
        with open(global_config_path, encoding="utf-8") as f:
            global_config: dict[str, Any] = yaml.safe_load(f) or {}
        _warn_unknown_keys(global_config, str(global_config_path))
        config = _deep_merge(config, global_config)

    # Local config is supported for project preferences, but an untrusted
    # repository must not be able to redirect credentials or tool execution.
    is_automatic_local_config = config_path is None
    resolved_config_path = config_path or (Path.cwd() / "config.yaml")

    if resolved_config_path.exists():
        with open(resolved_config_path, encoding="utf-8") as f:
            user_config: dict[str, Any] = yaml.safe_load(f) or {}
        _warn_unknown_keys(user_config, str(resolved_config_path))
        if is_automatic_local_config:
            user_config = copy.deepcopy(user_config)
            for key in ("permissions", "mcp", "delegation"):
                user_config.pop(key, None)
            if isinstance(user_config.get("observability"), dict):
                observability = user_config["observability"]
                for key in (
                    "enabled",
                    "sample_rate",
                    "capture_input",
                    "capture_output",
                    "flush_at_shutdown",
                ):
                    observability.pop(key, None)
                if isinstance(observability.get("langfuse"), dict):
                    for key in ("public_key", "secret_key", "base_url", "flush_at_shutdown"):
                        observability["langfuse"].pop(key, None)
            if isinstance(user_config.get("llm"), dict):
                user_config["llm"].pop("api_key", None)
                user_config["llm"].pop("base_url", None)
            if isinstance(user_config.get("openrouter"), dict):
                user_config["openrouter"].pop("api_key", None)
                user_config["openrouter"].pop("base_url", None)
            if isinstance(user_config.get("web"), dict):
                user_config["web"].pop("firecrawl_api_key", None)
        config = _deep_merge(config, user_config)

    # Resolve environment variable placeholders
    config = _deep_resolve(config)

    if isinstance(config.get("llm"), dict):
        llm_config = cast(dict[str, Any], config["llm"])
        api_key = llm_config.get("api_key", "")
        if isinstance(api_key, str) and re.fullmatch(r"\$\{?\w+\}?", api_key):
            llm_config["api_key"] = ""

    # Backward compat: migrate old 'openrouter' config key to 'llm'
    if "openrouter" in config:
        old: dict[str, Any] = config.pop("openrouter")  # type: ignore[assignment]
        existing: dict[str, Any] = config.get("llm", {})  # type: ignore[assignment]
        config["llm"] = _deep_merge({"llm": existing}, {"llm": old})["llm"]

    # Ensure API key from env var if not in config
    # Accept LLM_API_KEY (preferred) or OPENROUTER_API_KEY (legacy)
    llm = config.get("llm", {})
    if isinstance(llm, dict) and not llm.get("api_key"):
        config["llm"]["api_key"] = os.environ.get(  # type: ignore[index]
            "LLM_API_KEY", os.environ.get("OPENROUTER_API_KEY", "")
        )

    web = config.get("web")
    if not isinstance(web, dict):
        raise ConfigError("Config section 'web' must be a mapping")
    firecrawl_api_key = web.get("firecrawl_api_key", "")
    if isinstance(firecrawl_api_key, str) and re.fullmatch(r"\$\{?\w+\}?", firecrawl_api_key):
        firecrawl_api_key = ""
    if not firecrawl_api_key:
        web["firecrawl_api_key"] = os.environ.get("FIRECRAWL_API_KEY", "")

    observability = config.get("observability", {})
    if isinstance(observability, dict) and isinstance(observability.get("langfuse"), dict):
        langfuse = observability["langfuse"]
        for key, env_name in (
            ("public_key", "LANGFUSE_PUBLIC_KEY"),
            ("secret_key", "LANGFUSE_SECRET_KEY"),
            ("base_url", "LANGFUSE_BASE_URL"),
        ):
            if not langfuse.get(key) or (
                isinstance(langfuse.get(key), str) and re.fullmatch(r"\$\{?\w+\}?", langfuse[key])
            ):
                default = "https://cloud.langfuse.com" if key == "base_url" else ""
                langfuse[key] = os.environ.get(env_name, default)

    _validate_config(config)
    return config


def get_nova_home() -> Path:
    """Get the Nova data directory (~/.nova)."""
    return Path.home() / ".nova"


def ensure_nova_home() -> Path:
    """Ensure the Nova data directory exists."""
    home = get_nova_home()
    home.mkdir(parents=True, exist_ok=True)
    return home
