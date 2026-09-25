from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    staging_parent = root / "build"
    staging_parent.mkdir(parents=True, exist_ok=True)

    # A failed MCP call must not erase the last valid submission artifacts.
    # Generate the complete run in a temporary directory and publish it only
    # after all 100 cases pass schema validation.
    with tempfile.TemporaryDirectory(prefix="day09-run-", dir=staging_parent) as staging_name:
        staging_root = Path(staging_name)
        staging_outputs = staging_root / "outputs"
        staging_trace = staging_root / "trace.jsonl"
        staging_outputs.mkdir()
        trace = TraceWriter(staging_trace, contracts)

        async with connect_gateway(
            settings.mcp_endpoint, settings.team_api_key, contracts
        ) as gateway:
            discovered_tools = await gateway.list_tools()
            if not discovered_tools:
                raise RuntimeError("MCP Gateway returned no tools")
            for case_id in case_set.case_ids:
                case = case_set.cases[case_id]
                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                output = await solve_case(case, gateway, trace)
                contracts.validate_output(output, f"outputs/{case_id}.json")
                if output.get("case_id") != case_id:
                    raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                target = staging_outputs / f"{case_id}.json"
                target.write_text(
                    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")

        output_root.mkdir(parents=True, exist_ok=True)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        for stale in output_root.glob("*.json"):
            stale.unlink()
        for staged_output in staging_outputs.glob("*.json"):
            staged_output.replace(output_root / staged_output.name)
        staging_trace.replace(trace_path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
