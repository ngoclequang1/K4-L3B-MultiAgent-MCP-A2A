from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .business import investigate_case
from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .entity import resolve_entity
from .entity_mcp import MCPEntitySource
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path, *, details: bool = False) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        if details:
            print(json.dumps(await gateway.describe_tools(), ensure_ascii=False, indent=2))
            return
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path, *, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    completed: set[str] = set()
    if resume:
        existing = {path.stem: path for path in output_root.glob("*.json")}
        expected_prefix = set(case_set.case_ids[:len(existing)])
        if set(existing) != expected_prefix:
            raise ValueError("resume requires a contiguous prefix of validated outputs")
        for case_id, path in existing.items():
            output = json.loads(path.read_text(encoding="utf-8"))
            contracts.validate_output(output, str(path))
            if output.get("case_id") != case_id:
                raise ValueError(f"resume output has mismatched case_id: {case_id}")
        if existing and not trace_path.is_file():
            raise ValueError("resume requires the existing trace")
        if trace_path.is_file():
            retained = []
            finalized = set()
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                contracts.validate_trace(event, "existing trace")
                if event["case_id"] in existing:
                    retained.append(line)
                    if event["event_type"] == "case_finalized":
                        finalized.add(event["case_id"])
            if finalized != set(existing):
                raise ValueError("resume outputs and finalized trace cases disagree")
            trace_path.write_text("\n".join(retained) + ("\n" if retained else ""),
                                  encoding="utf-8")
        completed = set(existing)
    else:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        for case_id in case_set.case_ids:
            if case_id in completed:
                continue
            case = case_set.cases[case_id]
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")


async def _resolve_entity(root: Path, case_id: str) -> None:
    from dataclasses import asdict
    from uuid import uuid4

    case_set = load_case_set(root)
    if case_id not in case_set.cases:
        raise ValueError("case_id not found in input set")
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    destination = root / "diagnostics" / f"entity-{case_id}-{uuid4().hex}"
    trace = TraceWriter(destination / "trace.jsonl", contracts)
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        source = await MCPEntitySource.create(gateway, case_id)
        result = await resolve_entity(case_set.cases[case_id], source, trace)
        value = asdict(result)
        (destination / "entity.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)
        print(
            f"Entity diagnostic only; MCP calls={source.calls}; saved to {destination}", flush=True,
        )


async def _investigate_case(root: Path, case_id: str) -> None:
    from dataclasses import asdict
    from uuid import uuid4

    case_set = load_case_set(root)
    if case_id not in case_set.cases:
        raise ValueError("case_id not found in input set")
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    destination = root / "diagnostics" / f"business-{case_id}-{uuid4().hex}"
    trace = TraceWriter(destination / "trace.jsonl", contracts)
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        report = await investigate_case(case_set.cases[case_id], gateway, trace)
        trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        value = asdict(report)
        (destination / "report.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)
        print(f"Business diagnostic only; saved to {destination}", flush=True)


async def _investigate_all(root: Path) -> None:
    from dataclasses import asdict
    from uuid import uuid4

    case_set = load_case_set(root)
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    destination = root / "diagnostics" / f"business-all-{uuid4().hex}"
    trace = TraceWriter(destination / "trace.jsonl", contracts)
    failures: dict[str, str] = {}
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for case_id in case_set.case_ids:
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            try:
                report = await investigate_case(case_set.cases[case_id], gateway, trace)
            except (RuntimeError, ValueError) as exc:
                failures[case_id] = f"{type(exc).__name__}: {str(exc)[:200]}"
                print(f"{case_id}: failed; see summary", flush=True)
                continue
            (destination / f"{case_id}.json").write_text(
                json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            print(f"{case_id}: {report.assessment['primary_issue']} "
                  f"({report.mcp_calls} MCP calls)", flush=True)
    (destination / "failures.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"Business diagnostics: {destination}; failed cases: {len(failures)}", flush=True)


async def _check_case(root: Path, case_id: str) -> None:
    from uuid import uuid4

    case_set = load_case_set(root)
    if case_id not in case_set.cases:
        raise ValueError("case_id not found in input set")
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    destination = root / "diagnostics" / f"check-{case_id}-{uuid4().hex}"
    trace = TraceWriter(destination / "trace.jsonl", contracts)
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output = await solve_case(case_set.cases[case_id], gateway, trace)
        contracts.validate_output(output, f"check/{case_id}.json")
        (destination / f"{case_id}.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        print(f"Validated one case; saved to {destination}", flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    tool_parser = commands.add_parser(
        "mcp-tools", help="authenticate and list discovered MCP tools",
    )
    tool_parser.add_argument("--details", action="store_true", help="include MCP tool schemas")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument("--resume", action="store_true", help="keep validated completed cases")
    entity_parser = commands.add_parser("resolve-entity", help="diagnose entity resolution only")
    entity_parser.add_argument("--case-id", required=True)
    investigation = commands.add_parser("investigate-case", help="diagnose one business case")
    investigation.add_argument("--case-id", required=True)
    commands.add_parser("investigate-all", help="diagnose every case; audited MCP calls")
    check_case = commands.add_parser("check-case", help="solve one case without clearing outputs")
    check_case.add_argument("--case-id", required=True)
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    package.add_argument("--refine-claims", action="store_true",
                         help="reassess claims from existing audited evidence")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root, details=args.details))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume))
        elif args.command == "resolve-entity":
            asyncio.run(_resolve_entity(root, args.case_id))
        elif args.command == "investigate-case":
            asyncio.run(_investigate_case(root, args.case_id))
        elif args.command == "investigate-all":
            asyncio.run(_investigate_all(root))
        elif args.command == "check-case":
            asyncio.run(_check_case(root, args.case_id))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output,
                                             refine_claims=args.refine_claims)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
