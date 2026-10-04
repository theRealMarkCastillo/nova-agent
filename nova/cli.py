"""CLI entry point for Nova Agent."""

import argparse
import asyncio
import getpass
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from nova.agent import NovaAgent
from nova.config import ensure_nova_home, load_config, set_model
from nova.model_metadata import get_model_context_window
from nova.tokens import estimate_total_request_tokens


def _confirm_tool(name: str, arguments: dict[str, Any], app: Any = None) -> bool:
    """Ask for explicit approval before executing a mutating tool."""
    preview = repr(arguments)
    if len(preview) > 1000:
        preview = preview[:1000] + "..."
    print(f"\nTool '{name}' requests execution with arguments: {preview}")
    prompt = "Allow? [y/N] "

    def _ask() -> str:
        if app is None:
            return input(prompt)
        # The agent runs in a worker thread while prompt_toolkit owns stdin.
        # Schedule the terminal prompt on the application loop instead of
        # calling input() from the worker thread.
        import asyncio

        from prompt_toolkit.application.run_in_terminal import run_in_terminal

        loop = getattr(app, "loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("interactive confirmation is unavailable while the TUI is active")

        async def ask_in_terminal() -> str:
            return await run_in_terminal(lambda: input(prompt), render_cli_done=True)

        future = asyncio.run_coroutine_threadsafe(ask_in_terminal(), loop)
        try:
            return future.result(timeout=120)
        except Exception as exc:
            future.cancel()
            raise RuntimeError("interactive confirmation timed out") from exc

    try:
        return _ask().strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt, RuntimeError) as error:
        if isinstance(error, RuntimeError):
            print(f"Confirmation unavailable: {error}", file=sys.stderr)
        return False


def _chat_loop(agent):
    """Run an interactive chat loop with prompt_toolkit TUI."""
    from rich.console import Console

    from nova.command_handlers import dispatch_command
    from nova.display import (
        NovaTUI,
        StreamingReasoningBox,
        print_banner,
        print_message_history,
        print_tool_calls,
    )

    console = Console()
    print_banner(console, agent.config)
    if agent.messages:
        print_message_history(agent.messages)

    model = agent.config["llm"]["model"]
    context_window = get_model_context_window(
        model,
        override=agent.config.get("llm", {}).get("context_window") or None,
    )
    tui = NovaTUI(model=model, context_window=context_window, config=agent.config)
    agent._reasoning_callback = None
    agent._confirmation_callback = lambda name, arguments: _confirm_tool(
        name,
        arguments,
        app=getattr(tui, "_app", None),
    )

    def on_input(user_input: str) -> None:
        from nova.display import _DIM, _RST, _cprint

        # Slash command dispatch via handler registry
        if user_input.startswith("/"):
            parts = user_input[1:].split(None, 1)
            cmd_name = parts[0].lower()
            cmd_args = parts[1] if len(parts) > 1 else ""

            if dispatch_command(cmd_name, agent, cmd_args):
                current_model = agent.config["llm"]["model"]
                tui.model_short = current_model.split("/")[-1]
                tui.context_window = get_model_context_window(
                    current_model, override=agent.config["llm"].get("context_window") or None
                )
                tui.update_context(
                    estimate_total_request_tokens(
                        agent.messages, system_prompt=agent._system_prompt or ""
                    )
                )
                return

            # Unknown/unhandled slash command — send as message to agent
            _cprint(f"{_DIM}(sending /{cmd_name} to agent){_RST}")

        # Regular message → agent
        display = StreamingReasoningBox(
            show_reasoning=agent.config.get("ui", {}).get("show_reasoning", True)
        )
        display.reset()
        tool_names: list[str] = []

        def stream_callback(chunk: str, d: StreamingReasoningBox = display) -> None:
            d.show_reasoning = agent.config.get("ui", {}).get("show_reasoning", True)
            d.feed(chunk)

        def reasoning_callback(chunk: str, d: StreamingReasoningBox = display) -> None:
            d.show_reasoning = agent.config.get("ui", {}).get("show_reasoning", True)
            d.feed_reasoning(chunk)

        # Print tool calls immediately as they execute (before response)
        _tl = tool_names

        def tool_callback(
            name: str, tl: list[str] = _tl, d: StreamingReasoningBox = display
        ) -> None:
            tl.append(name)
            # Flush any pending response content first, then show tool block
            d.flush()
            d.reset()
            print_tool_calls(tl[:])
            tl.clear()

        agent._reasoning_callback = reasoning_callback
        agent._stream_callback = stream_callback
        agent._tool_callback = tool_callback
        # Wire Ctrl+C interrupt into the agent loop
        agent._interrupt_check = tui._interrupt_requested.is_set
        try:
            agent.run(user_input, stream=True, stream_callback=stream_callback)
        finally:
            agent._interrupt_check = None
            display.flush()

        ctx = estimate_total_request_tokens(
            agent.messages,
            system_prompt=agent._system_prompt or "",
        )
        tui.update_context(ctx)

    tui.run(on_input)
    agent.close()


def cmd_chat(args):
    """Start an interactive chat session."""
    config = load_config()
    session_id = args.session
    if getattr(args, "continue_session", False) is True:
        from nova.session import SessionStore

        session_dir = Path(config["session"]["directory"]).expanduser()
        sessions = SessionStore(session_dir / "sessions.db").list_sessions(limit=1)
        session_id = sessions[0]["session_id"] if sessions else None
    agent = NovaAgent(config=config, session_id=session_id)
    _chat_loop(agent)


def cmd_ask(args):
    """Ask a one-shot question."""
    config = load_config()
    agent = NovaAgent(config=config, confirmation_callback=_confirm_tool)
    try:
        response = agent.run(args.question, stream=False)
        print(response)
    finally:
        agent.close()


def cmd_acp(args):
    """Run Nova as an ACP server over standard input and output."""
    from nova.acp_server import run_acp_server

    asyncio.run(run_acp_server(load_config()))


def cmd_sessions(args):
    """List recent sessions, or prune old ones."""
    config = load_config()
    from nova.session import SessionStore

    session_dir = Path(config["session"]["directory"]).expanduser()
    store = SessionStore(session_dir / "sessions.db")

    if args.prune is not None:
        count = store.prune_sessions(older_than_days=args.prune)
        print(f"Pruned {count} session(s) older than {args.prune} day(s).")
        return

    sessions = store.list_sessions(limit=args.limit)
    if not sessions:
        print("No sessions found.")
        return

    print(f"{'Session ID':<30} {'Title':<25} {'Messages':<10} {'Updated':<25}")
    print("-" * 90)
    for s in sessions:
        title = (s.get("title") or "(untitled)")[:23]
        updated = (s.get("updated_at") or "")[:19]
        print(f"{s['session_id']:<30} {title:<25} {s['message_count']:<10} {updated:<25}")


def cmd_reset(args):
    """Delete a session by ID."""
    from nova.session import SessionStore

    config = load_config()
    session_dir = Path(config["session"]["directory"]).expanduser()
    store = SessionStore(session_dir / "sessions.db")

    if not args.session_id:
        print("Specify a session to delete: nova reset --session <id>")
        print("Use 'nova sessions' to list session IDs.")
        return

    deleted = store.delete_session(args.session_id)
    if deleted:
        print(f"Deleted session {args.session_id}")
    else:
        print(f"Session not found: {args.session_id}")
        sys.exit(1)


def cmd_setup(args):
    """Interactive setup wizard to configure Nova Agent."""
    import yaml

    nova_home = ensure_nova_home()
    config_path = nova_home / "config.yaml"

    print("╔══════════════════════════════════════════════════════╗")
    print("║           ✦ Nova Agent Setup Wizard                  ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    # Load existing config if present
    existing_config: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            existing_config = yaml.safe_load(f) or {}

    # Get API key (prefer llm section, fall back to legacy openrouter section)
    current_key = existing_config.get("llm", existing_config.get("openrouter", {})).get(
        "api_key", ""
    )
    env_key = os.environ.get("LLM_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""))

    if env_key:
        print("✓ API key found in environment")
        api_key = env_key
    elif current_key:
        masked = current_key[:8] + "..." if len(current_key) > 8 else current_key
        print(f"✓ API key already configured: {masked}")
        api_key = current_key
    else:
        print("LLM API key is required.")
        print()
        try:
            api_key = getpass.getpass("Enter your API key: ").strip()
        except (EOFError, OSError):
            # Permit redirected stdin in non-interactive setup environments.
            api_key = input("Enter your API key: ").strip()
        if not api_key:
            print("✗ API key is required. Setup cancelled.")
            sys.exit(1)

    # Get model preference
    current_model = existing_config.get("llm", existing_config.get("openrouter", {})).get(
        "model", ""
    )
    if current_model:
        print(f"\nCurrent model: {current_model}")
        change = input("Change model? [y/N]: ").strip().lower()
        if change in ("y", "yes"):
            model = input("Enter model (e.g. anthropic/claude-sonnet-4-20250514): ").strip()
        else:
            model = current_model
    else:
        print("\nSelect a model:")
        print("  1. anthropic/claude-sonnet-4-20250514 (recommended)")
        print("  2. anthropic/claude-opus-4-20250514")
        print("  3. google/gemini-2.5-pro")
        print("  4. openai/gpt-4.1")
        print("  5. Custom model")
        choice = input("\nChoice [1]: ").strip() or "1"
        models = {
            "1": "anthropic/claude-sonnet-4-20250514",
            "2": "anthropic/claude-opus-4-20250514",
            "3": "google/gemini-2.5-pro",
            "4": "openai/gpt-4.1",
        }
        if choice == "5":
            model = input("Enter model name: ").strip()
        else:
            model = models.get(choice, models["1"])

    # Build config
    config = existing_config
    config.setdefault("llm", {})
    config["llm"]["api_key"] = api_key
    set_model(config, model)

    # Write config atomically (temp file + rename to prevent corruption)
    import tempfile

    nova_home = config_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=nova_home, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, config_path)
    except Exception:
        os.unlink(tmp_path)
        raise

    print(f"\n✓ Configuration saved to {config_path}")
    print()
    print("Setup complete! Run 'nova chat' to start.")


def cmd_update(args):
    """Update Nova Agent to the latest version."""
    # Find the install directory (parent of the nova package)
    nova_package_dir = Path(__file__).parent.parent
    install_dir = nova_package_dir

    if not (install_dir / ".git").exists():
        print("✗ Could not find git repository. Nova may have been installed via pip.")
        print("  To update, run: pip install --upgrade nova-agent")
        sys.exit(1)

    print("Updating Nova Agent...")
    print(f"  Repository: {install_dir}")

    # Check for uncommitted changes
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=install_dir,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        print(" Uncommitted changes detected. Stashing before update...")
        subprocess.run(["git", "stash", "push", "--include-untracked"], cwd=install_dir, check=True)

    # Fetch and pull
    subprocess.run(["git", "fetch", "origin"], cwd=install_dir, check=True)
    result = subprocess.run(
        ["git", "pull", "--ff-only", "origin", "main"],
        cwd=install_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"✗ Pull failed: {result.stderr}")
        print("  You may have local changes. Run 'git pull' manually.")
        sys.exit(1)

    # Reinstall dependencies
    venv_python = next(
        (
            candidate
            for candidate in (
                install_dir / ".venv" / "bin" / "python",
                install_dir / "venv" / "bin" / "python",
            )
            if candidate.exists()
        ),
        None,
    )
    if venv_python is not None:
        print("Reinstalling dependencies...")
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-e", ".[dev]"],
            cwd=install_dir,
            check=True,
        )
    else:
        print("✗ No virtual environment found. Run 'python3 -m venv .venv' first.")
        print("  Then run: .venv/bin/pip install -e .")
        sys.exit(1)

    print("\n✓ Nova Agent updated successfully!")
    print("  Run 'nova chat' to start.")


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid int value: '{value}'") from e
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def main():
    parser = argparse.ArgumentParser(
        prog="nova",
        description="Nova Agent — A lightweight personal AI assistant",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # chat
    chat_parser = subparsers.add_parser("chat", help="Start an interactive chat session")
    chat_sessions = chat_parser.add_mutually_exclusive_group()
    chat_sessions.add_argument("--session", help="Resume a specific session ID")
    chat_sessions.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue the most recent session",
    )
    chat_parser.set_defaults(func=cmd_chat)

    # ask
    ask_parser = subparsers.add_parser("ask", help="Ask a one-shot question")
    ask_parser.add_argument("question", help="The question to ask")
    ask_parser.set_defaults(func=cmd_ask)

    # acp
    acp_parser = subparsers.add_parser("acp", help="Run an ACP server over stdio")
    acp_parser.set_defaults(func=cmd_acp)

    # sessions
    sessions_parser = subparsers.add_parser("sessions", help="List recent sessions")
    sessions_parser.add_argument("--limit", type=int, default=20, help="Max sessions to show")
    sessions_parser.add_argument(
        "--prune",
        type=_non_negative_int,
        metavar="DAYS",
        help="Delete sessions older than DAYS days (e.g. --prune 30)",
    )
    sessions_parser.set_defaults(func=cmd_sessions)

    # reset
    reset_parser = subparsers.add_parser("reset", help="Reset session")
    reset_parser.add_argument("--session", dest="session_id", help="Session ID to reset")
    reset_parser.set_defaults(func=cmd_reset)

    # setup
    setup_parser = subparsers.add_parser("setup", help="Interactive setup wizard")
    setup_parser.set_defaults(func=cmd_setup)

    # update
    update_parser = subparsers.add_parser("update", help="Update Nova Agent to latest version")
    update_parser.set_defaults(func=cmd_update)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    from nova.config import ConfigError

    try:
        args.func(args)
    except KeyboardInterrupt:
        stream = sys.stderr if args.command == "acp" else sys.stdout
        print("\nInterrupted.", file=stream)
        sys.exit(130)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
