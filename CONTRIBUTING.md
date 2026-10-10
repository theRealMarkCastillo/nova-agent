# Contributing to Nova Agent

**Status:** ✅ Active  
**Last Updated:** May 2026  
**Audience:** Open-source contributors, Eidolon Labs team

> Thank you for your interest in contributing! Nova Agent is developed by [Eidolon Labs LLC](https://github.com/eidolonlabs-ai).

---

## Quick Reference

```bash
# Full CI check (must pass before submitting PR)
ruff check . && ruff format --check . && mypy nova/ && pytest

# Run a single test file
pytest tests/test_agent.py -v

# Format and fix lint issues
ruff check --fix .
```

## Development Setup

```bash
# Clone the repo
git clone https://github.com/eidolonlabs-ai/nova-agent.git
cd nova-agent

# Install dependencies and sync virtual environment
uv sync --all-extras

# Verify everything works
uv run ruff check . && uv run mypy nova/ && uv run pytest
```

*(Alternatively, traditional venv: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && pytest`)*

## Code Quality Standards

All contributions must pass:

- **Linting**: `ruff check .` — no errors
- **Formatting**: `ruff format --check .` — no diffs
- **Type checking**: `mypy nova/` — 0 errors in all files
- **Tests**: `pytest` — all tests must pass, coverage ≥ 70%

Run the full CI check before submitting:

```bash
ruff check . && ruff format --check . && mypy nova/ && pytest
```

## Testing

### Writing Tests

- New features should include tests
- Use dependency injection for isolated testing — pass mock `openai_client`, `session_store`, and `wiki_memory_store` to `NovaAgent`
- Place tests in `tests/` with the naming convention `test_<module>.py`

### Test Structure

```python
def test_something():
    config = _minimal_config()  # Use minimal config for speed
    session_store = _mock_session_store()
    mock_client = MagicMock(spec=httpx.Client)

    agent = NovaAgent(
        config=config,
        openai_client=mock_client,
        session_store=session_store,
    )

    # ... assertions
```

### Running Tests

```bash
# All tests
pytest

# Verbose output
pytest -v

# Specific test file
pytest tests/test_agent.py

# With coverage (install pytest-cov first)
pytest --cov=nova --cov-report=term-missing
```

## Pull Requests

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Add tests for new functionality
5. Ensure all checks pass (`ruff check . && mypy nova/ && pytest`)
6. Commit with a clear message
7. Push and open a PR

### Commit Messages

Use conventional commit format:

- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation changes
- `test:` — test additions or changes
- `refactor:` — code refactoring
- `chore:` — maintenance tasks

## Architecture Guidelines

- **Explicit token budgets** at every layer
- **Dependency injection** for testability (openai_client, session_store, wiki_memory_store)
- **Two-tier tool descriptions** — compact list in prompt + JSON schemas to API
- **Head/tail truncation** (70/20 ratio) for long content
- **Prompt mode gating** — full/minimal/none for different agent types

## Adding Tools

1. Create `nova/tools/my_tool.py`
2. Define schema and handler function
3. Call `registry.register()` at module level
4. Add tests in `tests/test_tools.py`

## Adding Skills

Skills are markdown files with YAML frontmatter. See [docs/GUIDE-002-CREATING_SKILLS.md](docs/GUIDE-002-CREATING_SKILLS.md) for the format.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

---

## Related Documentation

| Document | Purpose |
|----------|---------|
| [README](README.md) | Project overview, features, installation |
| [Creating Tools](docs/GUIDE-001-CREATING_TOOLS.md) | Full guide for adding new tools |
| [Creating Skills](docs/GUIDE-002-CREATING_SKILLS.md) | Full guide for adding new skills |
| [Permissions](docs/GUIDE-008-PERMISSIONS.md) | Permission system reference |
| [SECURITY.md](SECURITY.md) | Reporting security vulnerabilities |
