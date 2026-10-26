"""Public T01 CLI. JSON on stdout; nonzero exit on input/engine/output errors."""

import argparse
import json
import math
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .engine import EngineError, rules_hash, upstream_candidates
from .schemas import InputError, load_snapshot, sha256


def _timeout(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="change-scope")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser(
        "analyze", help="Analyze an explicitly reviewed fixture"
    )
    analyze.add_argument("--snapshot", type=Path, required=True)
    analyze.add_argument("--target", required=True)
    analyze.add_argument(
        "--output",
        type=Path,
        help="Create a new result file; existing files are preserved",
    )
    analyze.add_argument("--timeout", type=_timeout, default=30.0, metavar="SECONDS")
    parity = commands.add_parser(
        "verify-wirelog", help="Compare an upstream Wirelog CLI with PyreWire"
    )
    parity.add_argument("--snapshot", type=Path, required=True)
    parity.add_argument("--target", required=True)
    parity.add_argument("--binary", type=Path, required=True)
    parity.add_argument("--output", type=Path)
    parity.add_argument("--timeout", type=_timeout, default=30.0)
    llm_smoke = commands.add_parser(
        "llm-smoke", help="Run a fixed synthetic LLM smoke; recorded by default"
    )
    llm_smoke.add_argument(
        "--case", choices=("normal", "adversarial"), default="normal"
    )
    llm_smoke.add_argument(
        "--live", action="store_true", help="Explicitly opt in to subscription CLI use"
    )
    llm_smoke.add_argument(
        "--recording",
        type=Path,
        help="Replay a synthetic process recording; never executes it",
    )
    llm_smoke.add_argument("--timeout", type=_timeout, default=120.0)
    llm_smoke.add_argument("--output", type=Path)
    extract = commands.add_parser(
        "extract",
        help="Turn a recorded extraction into pending candidates in a new workspace",
    )
    extract.add_argument("--source", type=Path, required=True)
    extract.add_argument("--source-id", required=True)
    extract.add_argument("--source-version", required=True)
    extract.add_argument(
        "--recording",
        type=Path,
        required=True,
        help="Synthetic process recording; never executed",
    )
    extract.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="New review workspace directory; existing paths are refused",
    )
    args = parser.parse_args(argv)
    if args.command == "extract":
        from .extraction import extract as run_extract

        result, exit_code = run_extract(
            args.source,
            args.source_id,
            args.source_version,
            args.recording,
            args.workspace,
        )
        return emit_result(
            result,
            exit_code,
            None,
            status_key="extraction_status",
            value_key="candidates",
        )
    if args.command == "llm-smoke":
        if args.output and args.output.exists():
            return emit_result(
                {
                    "schema_version": 1,
                    "extraction_status": "error",
                    "candidates": None,
                    "llm_mode": "not_used",
                },
                4,
                args.output,
                status_key="extraction_status",
                value_key="candidates",
            )
        from .llm import smoke

        result, exit_code = smoke(
            args.case, live=args.live, recording=args.recording, timeout=args.timeout
        )
        return emit_result(
            result,
            exit_code,
            args.output,
            status_key="extraction_status",
            value_key="candidates",
        )
    if args.command == "verify-wirelog":
        from .wirelog_cli import verify

        result, exit_code = verify(
            args.snapshot, args.target, args.binary, args.timeout
        )
        return emit_result(result, exit_code, args.output)
    result = {
        "schema_version": 1,
        "analysis_status": "error",
        "engine_mode": "unavailable",
        "llm_mode": "not_used",
        "engine": {"name": "pyrewire", "version": None},
        "snapshot_id": None,
        "snapshot_hash": None,
        "input_hash": None,
        "rules_hash": None,
        "target": args.target,
        "upstream_candidates": None,
        "deployment_status": "unverified",
    }
    exit_code = 0
    try:
        snapshot = load_snapshot(args.snapshot)
        result.update(
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_hash,
            purpose=snapshot.purpose,
        )
        if args.target not in snapshot.services:
            raise InputError(f"Unknown target: {args.target}")
        result["rules_hash"] = rules_hash()
        result["input_hash"] = sha256(
            json.dumps(
                {
                    "snapshot_hash": snapshot.snapshot_hash,
                    "target": args.target,
                    "rules_hash": result["rules_hash"],
                },
                sort_keys=True,
            ).encode()
        )
        try:
            result["engine"]["version"] = version("pyrewire")
        except PackageNotFoundError as exc:
            raise EngineError("PyreWire is not installed") from exc
        result["engine_mode"] = "live_engine"
        candidates = upstream_candidates(
            snapshot.edges, args.target, snapshot.services, args.timeout
        )
        result.update(
            analysis_status="success" if candidates else "empty",
            upstream_candidates=candidates,
            unique_relation_count=len(snapshot.edges),
            fixture=True,
            sources=list(snapshot.sources),
            facts=[
                {
                    "subject": src,
                    "predicate": "depends_on",
                    "object": dst,
                    "support": [
                        a
                        for a in snapshot.assertions
                        if (a["subject"], a["object"]) == (src, dst)
                    ],
                }
                for src, dst in snapshot.edges
            ],
            limitations=[
                "Document model only; deployment unverified.",
                "Witnesses, test evidence, review workflow and scenario comparison are not implemented in T01.",
            ],
        )
    except InputError as exc:
        result["error"] = {"kind": "input_error", "message": str(exc)}
        exit_code = 2
    except (EngineError, OSError) as exc:
        result["error"] = {"kind": "engine_error", "message": str(exc)}
        exit_code = 3
    return emit_result(result, exit_code, args.output)


def emit_result(
    result: dict,
    exit_code: int,
    output_path: Path | None,
    *,
    status_key: str = "analysis_status",
    value_key: str = "upstream_candidates",
) -> int:
    if output_path:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("x", encoding="utf-8") as output:
                output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        except (OSError, ValueError) as exc:
            result.update(
                {status_key: "error", value_key: None},
                error={"kind": "output_error", "message": str(exc)},
            )
            exit_code = 4
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code
