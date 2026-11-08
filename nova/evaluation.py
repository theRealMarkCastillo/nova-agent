from __future__ import annotations

import argparse
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nova.skills import build_skills_prompt, discover_skills
from nova.tokens import estimate_tokens, estimate_total_request_tokens
from nova.tool_catalog import ToolCatalog
from nova.tools.skills_tool import _skills_list

RETRIEVAL_DATASET_VERSION = 1
_RETRIEVAL_TOOLS = (
    ("mcp__issues__search", "Search repository issues and bug reports by title and status."),
    ("mcp__issues__comment", "Post a comment on a repository issue."),
    ("mcp__docs__search", "Search documentation pages for deployment instructions."),
    ("mcp__warehouse__query", "Query warehouse sales revenue using SQL."),
    ("mcp__weather__forecast", "Retrieve weather forecast precipitation and temperature."),
    ("mcp__mailbox__search", "Search mailbox email messages for invoices and receipts."),
    ("mcp__calendar__create", "Create calendar events and meeting invitations."),
    ("mcp__archive__delete", "Delete archived records permanently."),
)


@dataclass(frozen=True)
class RetrievalCase:
    case_id: str
    split: str
    steps: tuple[tuple[str, tuple[str, ...]], ...]


_RETRIEVAL_CASES = (
    RetrievalCase(
        "tuning-exact", "tuning", (("mcp__warehouse__query", ("mcp__warehouse__query",)),)
    ),
    RetrievalCase(
        "tuning-weather",
        "tuning",
        (("weather forecast precipitation", ("mcp__weather__forecast",)),),
    ),
    RetrievalCase(
        "tuning-ambiguous-search", "tuning", (("search email invoices", ("mcp__mailbox__search",)),)
    ),
    RetrievalCase(
        "tuning-workflow",
        "tuning",
        (
            ("search repository bug reports", ("mcp__issues__search",)),
            ("post comment on issue", ("mcp__issues__comment",)),
        ),
    ),
    RetrievalCase(
        "heldout-docs",
        "heldout",
        (("find documentation deployment instructions", ("mcp__docs__search",)),),
    ),
    RetrievalCase(
        "heldout-sales", "heldout", (("warehouse sales revenue SQL", ("mcp__warehouse__query",)),)
    ),
    RetrievalCase(
        "heldout-paraphrase",
        "heldout",
        (("Will I need an umbrella tomorrow?", ("mcp__weather__forecast",)),),
    ),
    RetrievalCase("heldout-absent", "heldout", (("transcribe audio", ()),)),
)


def evaluate_retrieval(size: int, *, split: str = "all", max_tokens: int = 4096) -> dict[str, Any]:
    if type(size) is not int or not 10 <= size <= 10000:
        raise ValueError("retrieval size must be between 10 and 10000")
    if split not in {"all", "tuning", "heldout"}:
        raise ValueError("split must be all, tuning, or heldout")
    tools = [
        (
            f"archive_search_{index:05}",
            "Search archived records by title, status, date and category. Returns historical records only.",
        )
        for index in range(size - len(_RETRIEVAL_TOOLS))
    ] + list(_RETRIEVAL_TOOLS)
    definitions = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": {"input": {"type": "string"}}},
            },
        }
        for name, description in tools
    ]
    full_names = {name for name, _ in tools}
    full_tokens = estimate_total_request_tokens([], tools=definitions)
    cases: list[dict[str, Any]] = []
    for case in _RETRIEVAL_CASES:
        if split != "all" and case.split != split:
            continue
        catalog = ToolCatalog(definitions, max_tokens=max_tokens, core_names=[])
        snapshots = [estimate_total_request_tokens([], tools=catalog.definitions())]
        required: set[str] = set()
        steps: list[dict[str, Any]] = []
        for query, expected_names in case.steps:
            expected = set(expected_names)
            required.update(expected)
            result = catalog.discover(query, limit=1)
            retrieved = {match["name"] for match in result["matches"]}
            selected = {definition["function"]["name"] for definition in catalog.definitions()}
            snapshots.append(estimate_total_request_tokens([], tools=catalog.definitions()))
            steps.append(
                {
                    "query": query,
                    "expected_tools": sorted(expected),
                    "retrieved_tools": sorted(retrieved),
                    "selected_tools": sorted(selected),
                    "missing_tools": sorted(required - selected),
                    "retrieval_recall": len(expected & retrieved) / len(expected)
                    if expected
                    else float(not retrieved),
                    "retrieval_precision": len(expected & retrieved) / len(retrieved)
                    if retrieved
                    else float(not expected),
                    "availability_recall": len(required & selected) / len(required)
                    if required
                    else 1.0,
                    "full_availability_recall": len(required & full_names) / len(required)
                    if required
                    else 1.0,
                    "schema_tokens": snapshots[-1],
                    "budget_blocked_tools": [
                        match["name"] for match in result["matches"] if not match["active"]
                    ],
                }
            )
        cases.append(
            {
                "case_id": case.case_id,
                "split": case.split,
                "steps": steps,
                "selected": {
                    "retrieval_recall": sum(step["retrieval_recall"] for step in steps)
                    / len(steps),
                    "retrieval_precision": sum(step["retrieval_precision"] for step in steps)
                    / len(steps),
                    "availability_recall": sum(step["availability_recall"] for step in steps)
                    / len(steps),
                    "schema_snapshot_tokens": sum(snapshots),
                    "peak_schema_tokens": max(snapshots),
                    "budget_violations": sum(tokens > max_tokens for tokens in snapshots),
                    "discovery_calls": len(steps),
                },
                "full": {
                    "availability_recall": sum(step["full_availability_recall"] for step in steps)
                    / len(steps),
                    "schema_snapshot_tokens": full_tokens * len(snapshots),
                    "peak_schema_tokens": full_tokens,
                    "discovery_calls": 0,
                },
            }
        )
    return {
        "dataset_version": RETRIEVAL_DATASET_VERSION,
        "catalog_size": size,
        "schema_budget": max_tokens,
        "split": split,
        "retrieval_limit": 1,
        "kind": "offline_scripted_retrieval",
        "cases": cases,
    }


def evaluate_catalog(size: int) -> dict[str, Any]:
    if type(size) is not int or not 1 <= size <= 10000:
        raise ValueError("size must be between 1 and 10000")
    with tempfile.TemporaryDirectory(prefix="nova-eval-") as directory:
        root = Path(directory)
        for index in range(size):
            name = f"skill-{index:05}"
            skill = root / name
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                f"---\nname: {name}\ncategory: fixture\ndescription: Perform task {index}\n---\nFixture body.",
                encoding="utf-8",
            )
        started = time.perf_counter()
        skills = discover_skills(root, max_count=None)
        discovery_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        warm_skills = discover_skills(root, max_count=None)
        warm_discovery_ms = (time.perf_counter() - started) * 1000
        prompt = build_skills_prompt(skills, max_chars=15000)
        target = f"skill-{size - 1:05}"
        started = time.perf_counter()
        result = json.loads(
            _skills_list({"query": target}, config={"skills": {"directory": directory}})
        )
        lookup_ms = (time.perf_counter() - started) * 1000
    schemas = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": f"Perform task {i}",
                "parameters": {"type": "object", "properties": {"input": {"type": "string"}}},
            },
        }
        for i in range(size)
    ]
    catalog = ToolCatalog(
        schemas, max_tokens=4096, core_names=[f"tool_{i}" for i in range(min(3, size))]
    )
    initial_tokens = estimate_total_request_tokens([], tools=catalog.definitions())
    selection = catalog.discover(f"tool_{size - 1}", limit=1)
    return {
        "catalog_size": size,
        "discovered_skills": len(skills),
        "last_skill_found": result["total"] == 1 and target in json.dumps(result["skills"]),
        "prompt_within_budget": len(prompt) <= 15000,
        "skill_prompt_tokens": estimate_tokens(prompt),
        "selected_schema_tokens": selection["schema_tokens"],
        "initial_schema_tokens": initial_tokens,
        "tail_tool_activated": catalog.is_active(f"tool_{size - 1}"),
        "full_tool_schema_tokens": estimate_total_request_tokens([], tools=schemas),
        "discovery_ms": discovery_ms,
        "warm_discovery_ms": warm_discovery_ms,
        "warm_metadata_equal": warm_skills == skills,
        "exact_lookup_ms": lookup_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic Nova catalog and retrieval evaluation; no LLM calls."
    )
    parser.add_argument("--suite", choices=["baseline", "retrieval", "all"], default="baseline")
    parser.add_argument("--split", choices=["all", "tuning", "heldout"], default="all")
    parser.add_argument("--sizes", nargs="+", type=int, default=[10, 100, 1000])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tracking-uri", help="Optionally record metrics in local MLflow.")
    args = parser.parse_args()
    results = [evaluate_catalog(size) for size in args.sizes] if args.suite != "retrieval" else []
    report = {"suite_version": 3, "kind": "synthetic_catalog_baseline", "results": results}
    retrieval_results = []
    if args.suite != "baseline":
        retrieval_results = [evaluate_retrieval(size, split=args.split) for size in args.sizes]
        report.update(
            {
                "suite_version": 4,
                "kind": "synthetic_catalog_evaluation",
                "retrieval_results": retrieval_results,
            }
        )
    if args.tracking_uri:
        import mlflow

        mlflow.set_tracking_uri(args.tracking_uri)
        mlflow.set_experiment("nova-catalog-evals")
        with mlflow.start_run(run_name=f"catalog-{args.suite}"):
            mlflow.log_param("suite_version", report["suite_version"])
            mlflow.log_param("fixture", "synthetic-catalog-v1")
            for result in results:
                size = result["catalog_size"]
                mlflow.log_metrics(
                    {f"catalog_{size}.{key}": float(value) for key, value in result.items()}
                )
            if retrieval_results:
                mlflow.log_param("retrieval_dataset_version", RETRIEVAL_DATASET_VERSION)
                mlflow.log_param("retrieval_split", args.split)
                mlflow.log_param("retrieval_limit", 1)
            for retrieval in retrieval_results:
                size = retrieval["catalog_size"]
                metrics = {}
                for split in ("tuning", "heldout"):
                    cases = [case for case in retrieval["cases"] if case["split"] == split]
                    if not cases:
                        continue
                    for variant in ("selected", "full"):
                        for key in cases[0][variant]:
                            metrics[f"retrieval_{size}.{split}.{variant}.{key}"] = sum(
                                case[variant][key] for case in cases
                            ) / len(cases)
                mlflow.log_metrics(metrics)
            mlflow.log_dict(report, "catalog-results.json")
    serialized = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    if any(
        not row["last_skill_found"]
        or not row["prompt_within_budget"]
        or not row["tail_tool_activated"]
        or not row["warm_metadata_equal"]
        for row in results
    ) or any(
        case["selected"]["budget_violations"]
        for retrieval in retrieval_results
        for case in retrieval["cases"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
