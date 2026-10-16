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
    args = parser.parse_args(argv)
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
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        except OSError as exc:
            result.update(
                analysis_status="error",
                upstream_candidates=None,
                error={"kind": "output_error", "message": str(exc)},
            )
            exit_code = 4
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code
