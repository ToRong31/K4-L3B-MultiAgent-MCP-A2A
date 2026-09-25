from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .agent_runtime import AgentRuntime
from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .model_worker import LocalModelWorker, ModelConfig
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool_name, specification in (await gateway.describe_tools()).items():
            print(f"{tool_name}: {json.dumps(specification, ensure_ascii=False)}")


async def _run(
    root: Path, *, resume: bool = False, allow_model_fallback: bool = False
) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    metrics_path = root / "traces" / "run-metrics.jsonl"
    summary_path = root / "traces" / "run-summary.json"
    model_worker = LocalModelWorker(
        ModelConfig(
            base_url=settings.model_base_url,
            model=settings.model_id,
            api_key=settings.model_api_key,
        )
    )
    model_ready = await model_worker.ready()
    if model_ready:
        try:
            smoke_test = await model_worker.complete(
                system="Preflight test. Return only JSON.",
                payload={"preflight": "check"},
                schema={
                    "type": "object",
                    "properties": {"status": {"type": "string"}},
                    "required": ["status"],
                },
                max_tokens=64,
            )
            if not isinstance(smoke_test, dict):
                model_ready = False
        except Exception as exc:
            if not allow_model_fallback:
                raise RuntimeError(f"local model preflight completion failed: {exc}") from exc
            model_ready = False
    if not model_ready and not allow_model_fallback:
        raise RuntimeError(
            "local model is unavailable; existing outputs were preserved. "
            "Start the model server or pass --allow-model-fallback explicitly"
        )
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    if not resume:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
        metrics_path.unlink(missing_ok=True)
        summary_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts, metrics_path)
    failures: list[dict[str, str]] = []
    started = time.monotonic()
    agent_runtime = AgentRuntime(model_worker, trace) if model_ready else None
    if not model_ready:
        print("WARN local model unavailable; deterministic fallback active", file=sys.stderr)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        for case_id in case_set.case_ids:
            if resume and (output_root / f"{case_id}.json").is_file():
                print(f"SKIP {case_id} existing output", flush=True)
                continue
            case = case_set.cases[case_id]
            case_started = time.monotonic()
            try:
                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                output = await solve_case(case, gateway, trace, agent_runtime=agent_runtime)
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
                print(f"OK {case_id} {time.monotonic() - case_started:.3f}s", flush=True)
            except Exception as exc:  # Isolate a failed case and preserve the rest of the batch.
                failures.append(
                    {"case_id": case_id, "error_type": type(exc).__name__, "message": str(exc)}
                )
                print(f"FAIL {case_id}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    completed = len(list(output_root.glob("*.json")))
    summary = {
        "case_count": len(case_set.case_ids),
        "success_count": completed,
        "failure_count": len(failures),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "model_ready": model_ready,
        "model_invocations": model_worker.invocations,
        "failures": failures,
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if failures:
        raise RuntimeError(f"{len(failures)} cases failed; see {summary_path}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume", action="store_true", help="keep valid existing outputs and run missing cases"
    )
    run.add_argument(
        "--allow-model-fallback", action="store_true",
        help="permit a deterministic-only run if the local LLM server is unavailable",
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
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
            asyncio.run(
                _run(root, resume=args.resume, allow_model_fallback=args.allow_model_fallback)
            )
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
