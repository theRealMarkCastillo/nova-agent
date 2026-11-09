import argparse
import json
from pathlib import Path

from nova import NovaOptions, run_task


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delegate a bounded task through Nova's Python SDK."
    )
    parser.add_argument("task")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--session")
    parser.add_argument("--approve-tool", action="append", default=[])
    args = parser.parse_args()
    result = run_task(
        args.task,
        options=NovaOptions(
            workspace=args.workspace,
            config_path=args.config,
            session_id=args.session,
            max_total_tokens=100000,
            timeout_seconds=60,
        ),
        confirm=(lambda name, arguments: name in args.approve_tool) if args.approve_tool else None,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    raise SystemExit(
        {"completed": 0, "failed": 1, "blocked": 3, "limit_reached": 4, "cancelled": 130}[
            result.status
        ]
    )


if __name__ == "__main__":
    main()
