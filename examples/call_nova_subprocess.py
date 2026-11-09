import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Delegate through Nova's JSON CLI protocol.")
    parser.add_argument("task")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--session")
    args = parser.parse_args()
    command = [
        sys.executable,
        "-m",
        "nova",
        "ask",
        args.task,
        "--output-format",
        "json",
        "--workspace",
        str(args.workspace),
        "--max-total-tokens",
        "100000",
        "--timeout-seconds",
        "60",
    ]
    if args.config is not None:
        command.extend(["--config", str(args.config)])
    if args.session is not None:
        command.extend(["--session", args.session])
    completed = subprocess.run(command, capture_output=True, text=True, timeout=75, check=False)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    result = json.loads(completed.stdout)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
