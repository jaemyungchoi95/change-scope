"""Public T01 CLI. JSON on stdout; nonzero exit on input/engine/output errors."""

import argparse
import json
import math
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .engine import EngineError, rules_hash, upstream_candidates
from .evidence import (
    attach,
    live_suggestions,
    load_catalog,
    load_suggestions,
    render_markdown,
)
from .llm import SERVICES, LLMError
from .schemas import InputError, sha256
from .witness import WitnessError, explain


class OutputError(RuntimeError):
    pass


def _timeout(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="change-scope")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser(
        "analyze", help="Analyze a reviewed fixture or an exported reviewed snapshot"
    )
    analyze.add_argument("--snapshot", type=Path, required=True)
    analyze.add_argument("--target", required=True)
    analyze.add_argument(
        "--output",
        type=Path,
        help="Create a new result file; existing files are preserved",
    )
    analyze.add_argument("--timeout", type=_timeout, default=30.0, metavar="SECONDS")
    analyze.add_argument(
        "--evidence", type=Path, help="Reviewed evidence catalog of covers/about links"
    )
    analyze.add_argument(
        "--suggestions",
        type=Path,
        help="Synthetic recording of suggestion output; never executed",
    )
    analyze.add_argument(
        "--live-suggestions",
        action="store_true",
        help="Explicitly opt in to one subscription CLI turn for suggestions",
    )
    analyze.add_argument(
        "--report",
        type=Path,
        help="Create a new Markdown report; existing files are preserved",
    )
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
    review = commands.add_parser(
        "review", help="Inspect candidates and record human review decisions"
    )
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_show = review_commands.add_parser(
        "show", help="Verify the workspace and show candidates with source text"
    )
    review_show.add_argument("--workspace", type=Path, required=True)
    review_show.add_argument("--id", help="Show one candidate with its section text")
    review_show.add_argument(
        "--text", action="store_true", help="Human-readable output instead of JSON"
    )
    review_decide = review_commands.add_parser(
        "decide", help="Append one approve/reject decision to the review ledger"
    )
    review_decide.add_argument("--workspace", type=Path, required=True)
    review_decide.add_argument("--id", required=True)
    review_decide.add_argument("--revision", type=int, required=True)
    review_decide.add_argument(
        "--decision", choices=("approve", "reject"), required=True
    )
    review_decide.add_argument(
        "--reviewer", required=True, help="Recorded label; not authentication"
    )
    review_decide.add_argument("--reason", required=True)
    review_revise = review_commands.add_parser(
        "revise", help="Append a new pending revision of a candidate"
    )
    review_revise.add_argument("--workspace", type=Path, required=True)
    review_revise.add_argument("--id", required=True)
    review_revise.add_argument("--from-revision", type=int, required=True)
    for name in ("subject", "object", "section", "quote"):
        review_revise.add_argument(f"--{name}", help="Defaults to the latest revision")
    review_revise.add_argument(
        "--source-version", help="Defaults to the current source version"
    )
    review_revise.add_argument("--reviewer", required=True)
    review_revise.add_argument("--reason", required=True)
    review_withdraw = review_commands.add_parser(
        "withdraw", help="End the effect of an approved latest revision"
    )
    review_withdraw.add_argument("--workspace", type=Path, required=True)
    review_withdraw.add_argument("--id", required=True)
    review_withdraw.add_argument("--revision", type=int, required=True)
    review_withdraw.add_argument("--reviewer", required=True)
    review_withdraw.add_argument("--reason", required=True)
    review_source = review_commands.add_parser(
        "add-source", help="Register a new version of the workspace source"
    )
    review_source.add_argument("--workspace", type=Path, required=True)
    review_source.add_argument("--source", type=Path, required=True)
    review_source.add_argument("--source-version", required=True)
    review_source.add_argument("--reviewer", required=True)
    review_source.add_argument("--reason", required=True)
    export = commands.add_parser(
        "export", help="Export ledger-approved candidates as a reviewed snapshot"
    )
    export.add_argument("--workspace", type=Path, required=True)
    export.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New snapshot directory; existing paths are refused",
    )
    proposal = commands.add_parser("proposal", help="Review change proposals")
    proposal_commands = proposal.add_subparsers(dest="proposal_command", required=True)
    proposal_init = proposal_commands.add_parser(
        "init",
        help="Validate pending proposals against their base into a new workspace",
    )
    proposal_init.add_argument("--proposals", type=Path, required=True)
    proposal_init.add_argument("--base", type=Path, required=True)
    proposal_init.add_argument("--workspace", type=Path, required=True)
    proposal_decide = proposal_commands.add_parser(
        "decide", help="Append one approve/reject decision to the proposal ledger"
    )
    proposal_decide.add_argument("--workspace", type=Path, required=True)
    proposal_decide.add_argument("--id", required=True)
    proposal_decide.add_argument("--revision", type=int, required=True)
    proposal_decide.add_argument(
        "--decision", choices=("approve", "reject"), required=True
    )
    proposal_decide.add_argument("--reviewer", required=True)
    proposal_decide.add_argument("--reason", required=True)
    scenario = commands.add_parser("scenario", help="Build scenario snapshots")
    scenario_commands = scenario.add_subparsers(dest="scenario_command", required=True)
    scenario_build = scenario_commands.add_parser(
        "build", help="Apply approved proposals to their base in a new directory"
    )
    scenario_build.add_argument("--workspace", type=Path, required=True)
    scenario_build.add_argument("--base", type=Path, required=True)
    scenario_build.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser(
        "compare", help="Analyze a base and its scenario and report the change"
    )
    compare.add_argument("--base", type=Path, required=True)
    compare.add_argument("--scenario", type=Path, required=True)
    compare.add_argument("--target", required=True)
    compare.add_argument("--evidence", type=Path)
    compare.add_argument("--report", type=Path)
    compare.add_argument("--timeout", type=_timeout, default=30.0)
    serve = commands.add_parser(
        "serve", help="Run the local read-only viewer on 127.0.0.1"
    )
    serve.add_argument("--snapshot", type=Path, required=True)
    serve.add_argument("--workspace", type=Path, help="Review workspace to show")
    serve.add_argument("--evidence", type=Path)
    serve.add_argument("--scenario", type=Path)
    serve.add_argument("--llm", choices=("off", "recorded", "live"), default="off")
    serve.add_argument("--suggestions-recording", type=Path)
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command == "serve":
        return run_serve(args)
    if args.command in ("proposal", "scenario"):
        from . import scenario as scenario_module

        if args.command == "scenario":
            result, exit_code = scenario_module.build(
                args.workspace, args.base, args.output
            )
            return emit_result(
                result, exit_code, None, status_key="build_status", value_key="applied"
            )
        if args.proposal_command == "init":
            result, exit_code = scenario_module.init(
                args.proposals, args.base, args.workspace
            )
            return emit_result(
                result,
                exit_code,
                None,
                status_key="proposal_status",
                value_key="proposals",
            )
        result, exit_code = scenario_module.decide(
            args.workspace,
            args.id,
            args.revision,
            args.decision,
            args.reason,
            args.reviewer,
        )
        return emit_result(
            result, exit_code, None, status_key="decide_status", value_key="proposals"
        )
    if args.command == "compare":
        return emit_result(
            *run_compare(args),
            None,
            status_key="compare_status",
            value_key="comparison",
        )
    if args.command == "export":
        from .reviewed import export as run_export

        result, exit_code = run_export(args.workspace, args.output)
        return emit_result(
            result, exit_code, None, status_key="export_status", value_key="approved"
        )
    if args.command == "review":
        from . import review as review_module

        if args.review_command == "show":
            result, exit_code = review_module.show(args.workspace, args.id)
            if args.text:
                print(review_module.render_text(result))
                return exit_code
            return emit_result(
                result,
                exit_code,
                None,
                status_key="show_status",
                value_key="candidates",
            )
        if args.review_command == "revise":
            result, exit_code = review_module.revise(
                args.workspace,
                args.id,
                args.from_revision,
                args.reason,
                args.reviewer,
                subject=args.subject,
                object_=args.object,
                section=args.section,
                quote=args.quote,
                source_version=args.source_version,
            )
            return emit_result(
                result,
                exit_code,
                None,
                status_key="revise_status",
                value_key="candidate",
            )
        if args.review_command == "withdraw":
            result, exit_code = review_module.withdraw(
                args.workspace, args.id, args.revision, args.reason, args.reviewer
            )
            return emit_result(
                result,
                exit_code,
                None,
                status_key="withdraw_status",
                value_key="candidate",
            )
        if args.review_command == "add-source":
            result, exit_code = review_module.add_source(
                args.workspace,
                args.source,
                args.source_version,
                args.reason,
                args.reviewer,
            )
            return emit_result(
                result, exit_code, None, status_key="source_status", value_key="source"
            )
        result, exit_code = review_module.decide(
            args.workspace,
            args.id,
            args.revision,
            args.decision,
            args.reason,
            args.reviewer,
        )
        return emit_result(
            result, exit_code, None, status_key="decide_status", value_key="candidate"
        )
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
    result, exit_code = run_analysis(args)
    return emit_result(result, exit_code, args.output)


def run_analysis(args: argparse.Namespace) -> tuple[dict, int]:
    """Analyze one snapshot; needs snapshot, target, timeout, evidence, suggestions, report."""
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
        from .reviewed import load_for_analysis

        snapshot, extras = load_for_analysis(args.snapshot)
        result.update(
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_hash,
            purpose=snapshot.purpose,
        )
        if args.target not in snapshot.services:
            raise InputError(f"Unknown target: {args.target}")
        live = getattr(args, "live_suggestions", False)
        if (args.suggestions or args.report or live) and not args.evidence:
            raise InputError(
                "--suggestions, --live-suggestions and --report require --evidence"
            )
        if args.suggestions and live:
            raise InputError("Use either recorded or live suggestions")
        if args.report and os.path.lexists(args.report):
            raise OutputError("Report already exists; choose a new path")
        catalog = (
            load_catalog(args.evidence, snapshot.services) if args.evidence else None
        )
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
        witnesses, witness_check = explain(snapshot, args.target, candidates)
        result.update(
            analysis_status="success" if candidates else "empty",
            upstream_candidates=candidates,
            witnesses=witnesses,
            witness_check=witness_check,
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
                "Witnesses are paths in the reviewed input graph, not engine proof trees.",
                "Test evidence and scenario comparison are not implemented yet.",
            ],
        )
        result.update(extras)
        if catalog is not None:
            if live:
                suggestions, meta = live_suggestions(
                    snapshot, args.target, candidates, witnesses, catalog
                )
            elif args.suggestions:
                suggestions, meta = load_suggestions(
                    args.suggestions, snapshot, args.target, candidates, catalog
                )
            else:
                suggestions, meta = None, None
            result.update(
                attach(
                    snapshot,
                    args.target,
                    candidates,
                    witnesses,
                    catalog,
                    suggestions,
                    meta,
                )
            )
        if args.report:
            try:
                with args.report.open("x", encoding="utf-8") as report:
                    report.write(render_markdown(result))
            except (OSError, ValueError) as exc:
                raise OutputError(f"Cannot write report: {type(exc).__name__}") from exc
            result["report"] = str(args.report)
    except OutputError as exc:
        result.update(analysis_status="error", upstream_candidates=None)
        result["error"] = {"kind": "output_error", "message": str(exc)}
        exit_code = 4
    except LLMError as exc:
        result.update(analysis_status="error", upstream_candidates=None)
        result["error"] = {"kind": exc.kind, "message": str(exc)}
        exit_code = 2 if exc.kind == "input_error" else 3
    except InputError as exc:
        result["error"] = {"kind": "input_error", "message": str(exc)}
        exit_code = 2
    except WitnessError as exc:
        result["error"] = {"kind": "witness_mismatch", "message": str(exc)}
        exit_code = 3
    except (EngineError, OSError) as exc:
        result["error"] = {"kind": "engine_error", "message": str(exc)}
        exit_code = 3
    return result, exit_code


def run_serve(args: argparse.Namespace) -> int:
    from .reviewed import load_for_analysis
    from .server import ServerConfig, serve

    result = {"schema_version": 1, "serve_status": "error"}
    try:
        load_for_analysis(args.snapshot)
        if args.scenario:
            load_for_analysis(args.scenario)
        if args.evidence:
            load_catalog(args.evidence, frozenset(SERVICES))
        if args.llm != "off" and not args.evidence:
            raise InputError("--llm needs --evidence")
        if args.llm == "recorded" and not args.suggestions_recording:
            raise InputError("--llm recorded needs --suggestions-recording")
        if args.workspace and not (args.workspace / "candidates.json").is_file():
            raise InputError("--workspace is not a review workspace")
        if not 1024 <= args.port <= 65535:
            raise InputError("--port must be 1024-65535")
    except InputError as exc:
        result["error"] = {"kind": "input_error", "message": str(exc)}
        return emit_result(
            result, 2, None, status_key="serve_status", value_key="error"
        )
    return serve(
        ServerConfig(
            snapshot=args.snapshot,
            workspace=args.workspace,
            evidence=args.evidence,
            scenario=args.scenario,
            llm=args.llm,
            suggestions_recording=args.suggestions_recording,
            port=args.port,
        )
    )


def run_compare(args: argparse.Namespace) -> tuple[dict, int]:
    from .scenario import compare, render_compare_markdown

    result = {
        "schema_version": 1,
        "compare_status": "error",
        "llm_mode": "not_used",
        "comparison": None,
        "deployment_status": "unverified",
    }
    if args.report and os.path.lexists(args.report):
        result["error"] = {
            "kind": "output_error",
            "message": "Report already exists; choose a new path",
        }
        return result, 4
    runs = {}
    for name, path in (("base", args.base), ("scenario", args.scenario)):
        sub = argparse.Namespace(
            snapshot=path,
            target=args.target,
            timeout=args.timeout,
            evidence=args.evidence,
            suggestions=None,
            report=None,
        )
        runs[name], code = run_analysis(sub)
        if code:
            result[f"{name}_result"] = runs[name]
            result["error"] = {
                "kind": runs[name]["error"]["kind"],
                "message": f"{name} analysis failed: {runs[name]['error']['message']}",
            }
            return result, code
    scenario = runs["scenario"].get("scenario")
    if scenario is None:
        result["error"] = {
            "kind": "input_error",
            "message": "--scenario is not a scenario snapshot",
        }
        return result, 2
    if (scenario["base"]["snapshot_id"], scenario["base"]["snapshot_hash"]) != (
        runs["base"]["snapshot_id"],
        runs["base"]["snapshot_hash"],
    ):
        result["error"] = {
            "kind": "input_error",
            "message": "Scenario was built on another base snapshot ID/hash",
        }
        return result, 2
    result.update(
        compare_status="success",
        comparison=compare(runs["base"], runs["scenario"], scenario),
        base_result=runs["base"],
        scenario_result=runs["scenario"],
    )
    if args.report:
        try:
            with args.report.open("x", encoding="utf-8") as report:
                report.write(render_compare_markdown(result))
        except (OSError, ValueError) as exc:
            result.update(compare_status="error", comparison=None)
            result["error"] = {
                "kind": "output_error",
                "message": f"Cannot write report: {type(exc).__name__}",
            }
            return result, 4
        result["report"] = str(args.report)
    return result, 0


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
