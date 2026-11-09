import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.local_provider import LocalProvider


@pytest.fixture
def protocol(tmp_path):
    with LocalProvider() as provider:
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "llm": {
                        "api_key": "fixture-token",
                        "base_url": provider.url,
                        "model": "fixture-model",
                    },
                    "session": {"directory": str(tmp_path / "sessions")},
                    "permissions": {"mode": "ask"},
                    "wiki": {"enabled": False},
                    "skills": {"enabled": False},
                    "context_files": [],
                    "observability": {"enabled": False},
                }
            )
        )
        env = dict(os.environ, HOME=str(tmp_path / "home"))
        command = [
            sys.executable,
            "-m",
            "nova",
            "ask",
            "--workspace",
            str(tmp_path),
            "--config",
            str(config),
        ]
        yield provider, command, env, tmp_path


def invoke(protocol, *args, input=None):
    _, command, env, workspace = protocol
    return subprocess.run(
        [*command, *args],
        input=input,
        capture_output=True,
        text=True,
        cwd=workspace,
        env=env,
        timeout=10,
    )


def test_json_success_unicode_stdin_and_resume(protocol):
    result = invoke(
        protocol, "--prompt-file", "-", "--output-format", "json", input="日本語 café task"
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "completed"
    assert "日本語 café" in report["output"]
    assert report["usage"]["input_tokens"] == 10
    resumed = invoke(
        protocol, "continue", "--session", report["session_id"], "--output-format", "json"
    )
    assert json.loads(resumed.stdout)["session_id"] == report["session_id"]
    assert resumed.returncode == 0


def test_jsonl_event_order_and_terminal_result(protocol):
    result = invoke(protocol, "task", "--output-format", "jsonl")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["type"] for event in events] == [
        "run_started",
        "message_delta",
        "message_delta",
        "run_finished",
    ]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert events[-1]["result"]["status"] == "completed"


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_machine_permission_blocks_without_stdin(protocol, format):
    result = invoke(protocol, "write-artifact", "--output-format", format, input="")
    assert result.returncode == 3, result.stderr
    data = (
        json.loads(result.stdout)
        if format == "json"
        else json.loads(result.stdout.splitlines()[-1])["result"]
    )
    assert data["status"] == "blocked"
    assert data["stop_reason"] == "permission_required"
    assert not (protocol[-1] / "artifact.txt").exists()
    assert "Allow?" not in result.stdout


@pytest.mark.parametrize(
    "arguments,code,error",
    [
        (["fail-provider"], 1, "provider_error"),
        (["task", "--max-total-tokens", "1"], 4, None),
        (["task", "--timeout-seconds", "nan"], 2, "invalid_options"),
        (["task", "--session", "missing"], 1, "session_not_found"),
    ],
)
def test_json_failures_and_exit_codes(protocol, arguments, code, error):
    result = invoke(protocol, *arguments, "--output-format", "json")
    assert result.returncode == code, result.stderr
    data = json.loads(result.stdout)
    if error is not None:
        assert data["error"]["code"] == error


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_signal_cancels_with_machine_output(protocol, sig, format):
    provider, command, env, workspace = protocol
    process = subprocess.Popen(
        [*command, "slow", "--output-format", format],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workspace,
        env=env,
    )
    try:
        assert provider.requested.wait(5)
        process.send_signal(sig)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert process.returncode == 130, stderr
    report = (
        json.loads(stdout) if format == "json" else json.loads(stdout.splitlines()[-1])["result"]
    )
    assert report["status"] == "cancelled"
    assert "Interrupted." not in stdout


def test_positional_prompt_and_prompt_file_are_mutually_exclusive(protocol):
    result = invoke(protocol, "task", "--prompt-file", "-", "--output-format", "json", input="task")
    assert result.returncode == 2
    assert result.stdout == ""


def test_closed_jsonl_pipe_exits_without_traceback(protocol):
    _, command, env, workspace = protocol
    process = subprocess.Popen(
        [*command, "slow", "--output-format", "jsonl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workspace,
        env=env,
    )
    try:
        assert json.loads(process.stdout.readline())["type"] == "run_started"
        process.stdout.close()
        assert process.wait(timeout=5) == 1
        assert "Traceback" not in process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.skipif(
    not os.environ.get("NOVA_TEST_WHEEL"),
    reason="Set NOVA_TEST_WHEEL to run packaged-install smoke",
)
def test_packaged_examples_outside_repository(protocol):
    _, command, env, workspace = protocol
    target = workspace / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--target",
            str(target),
            os.environ["NOVA_TEST_WHEEL"],
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    repository = Path(__file__).resolve().parents[1]
    bootstrap = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); import nova; assert nova.__file__.startswith(sys.path[0]); script=sys.argv.pop(1); sys.argv[0]=script; runpy.run_path(script,run_name='__main__')"
    config = command[command.index("--config") + 1]
    package_env = dict(env, PYTHONPATH=str(target))
    sdk = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            bootstrap,
            str(target),
            str(repository / "examples/call_nova.py"),
            "write-artifact",
            "--workspace",
            str(workspace),
            "--config",
            config,
            "--approve-tool",
            "write_file",
        ],
        cwd=workspace,
        env=package_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert sdk.returncode == 0, sdk.stderr
    result = json.loads(sdk.stdout)
    assert (workspace / "artifact.txt").read_text() == "fixture artifact\n"
    task = "continue; $(touch must-not-exist)"
    cli = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            bootstrap,
            str(target),
            str(repository / "examples/call_nova_subprocess.py"),
            task,
            "--workspace",
            str(workspace),
            "--config",
            config,
            "--session",
            result["session_id"],
        ],
        cwd=workspace,
        env=package_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout)["session_id"] == result["session_id"]
    assert task in json.loads(cli.stdout)["output"]
    assert not (workspace / "must-not-exist").exists()
