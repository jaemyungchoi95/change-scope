"""T07: reviewed change proposals, scenario snapshots and impact comparison.

Proposals are bound to one base snapshot ID and hash and start pending. Only
`proposal decide` records approval, in a hash-chained ledger. A scenario snapshot
ships a copy of its base and of the proposal evidence; its loader re-verifies both
and re-applies the approved proposals. A remove is a scenario override, not a
withdrawal of the base's sources.
"""

import fcntl
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError

from .extraction import Identifier, Quote, Section, Service, Sha256, StrictModel
from .review import MAX_REASON, MAX_REVIEWER, ReviewError, _read_regular, _text_field
from .schemas import InputError, Snapshot, section_bodies, sha256

PROPOSALS = "proposals.json"
LEDGER = "ledger.jsonl"
SCENARIO = "scenario.json"
ProposalId = Annotated[str, StringConstraints(pattern=r"^P-[0-9]{3}$")]


class ProposalSource(StrictModel):
    source_id: Identifier
    source_version: Identifier
    path: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,100}\.md$")]
    sha256: Sha256


class Proposal(StrictModel):
    proposal_id: ProposalId
    revision: Literal[1]
    operation: Literal["add", "remove"]
    predicate: Literal["depends_on"]
    subject: Service
    object: Service
    section: Section
    quote: Quote
    review_status: Literal["pending"]


class ProposalSet(StrictModel):
    schema_version: Literal[1]
    kind: Literal["proposal_set"]
    base_snapshot_id: str
    base_snapshot_hash: Sha256
    source: ProposalSource
    proposals: list[Proposal] = Field(min_length=1, max_length=20)


class ProposalDecision(StrictModel):
    seq: int = Field(ge=1)
    decided_at: AwareDatetime
    proposal_id: ProposalId
    revision: Literal[1]
    decision: Literal["approve", "reject"]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=MAX_REASON)]
    reviewer: Annotated[str, StringConstraints(min_length=1, max_length=MAX_REVIEWER)]
    proposal_set_sha256: Sha256
    prev_sha256: Sha256 | None


class AppliedProposal(StrictModel):
    proposal_id: ProposalId
    revision: int
    operation: Literal["add", "remove"]
    subject: Service
    object: Service
    section: Section
    quote: Quote
    decision_seq: int
    reviewer: str
    reason: str


class NotApplied(StrictModel):
    proposal_id: ProposalId
    status: Literal["pending", "rejected"]


class ScenarioBase(StrictModel):
    snapshot_id: str
    snapshot_hash: Sha256
    path: Literal["base/snapshot.json"]


class ScenarioSnapshot(StrictModel):
    schema_version: Literal[1]
    kind: Literal["scenario_snapshot"]
    scenario_id: str
    purpose: Literal["proposed"]
    base: ScenarioBase
    proposal_set_sha256: Sha256
    ledger_head_sha256: Sha256
    applied: list[AppliedProposal] = Field(min_length=1)
    not_applied: list[NotApplied]
    overrides: dict[Literal["added", "removed"], list[tuple[Service, Service]]]
    edges: list[tuple[Service, Service]]
    deployment_status: Literal["unverified"]


class ProposalWorkspace:
    def __init__(self, path, model, digest, document, decisions, head):
        self.path = path
        self.model: ProposalSet = model
        self.sha256: str = digest
        self.document: str = document
        self.decisions: list[ProposalDecision] = decisions
        self.head: str | None = head

    def decision(self, pid: str) -> ProposalDecision | None:
        return next((d for d in self.decisions if d.proposal_id == pid), None)


def _check_operations(proposals: list[Proposal], edges: set[tuple[str, str]]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for p in proposals:
        edge = (p.subject, p.object)
        if p.subject == p.object:
            raise InputError(
                f"{p.proposal_id}: subject and object are the same service"
            )
        if edge in seen:
            raise InputError(
                f"{p.proposal_id}: conflicts with {seen[edge]} on the same relation"
            )
        seen[edge] = p.proposal_id
        if p.operation == "remove" and edge not in edges:
            raise InputError(
                f"{p.proposal_id}: cannot remove a relation the base lacks"
            )
        if p.operation == "add" and edge in edges:
            raise InputError(f"{p.proposal_id}: cannot add a relation the base has")


def _source_check(model: ProposalSet, document: str) -> None:
    for p in model.proposals:
        bodies = section_bodies(document, p.section)
        if len(bodies) != 1 or p.quote not in bodies[0]:
            raise InputError(
                f"{p.proposal_id}: quote missing from unique source section"
            )


def _check_base(model: ProposalSet, base: Snapshot) -> None:
    if (model.base_snapshot_id, model.base_snapshot_hash) != (
        base.snapshot_id,
        base.snapshot_hash,
    ):
        raise InputError(
            "Proposals target another base snapshot ID/hash; they are never re-applied "
            "to a different base"
        )
    _check_operations(model.proposals, set(base.edges))


def load_proposals(path: Path) -> ProposalWorkspace:
    """Load a proposal workspace and replay its decision ledger; tampering is an error."""
    try:
        raw = _read_regular(path / PROPOSALS, PROPOSALS)
        model = ProposalSet.model_validate_json(raw)
        blob = _read_regular(path / model.source.path, "Change request")
    except (ReviewError, OSError, ValidationError) as exc:
        raise InputError(f"Invalid proposal workspace: {type(exc).__name__}") from exc
    if sha256(blob) != model.source.sha256:
        raise InputError("Change request does not match its recorded hash")
    try:
        document = blob.decode("utf-8")
    except UnicodeError as exc:
        raise InputError("Change request is not UTF-8") from exc
    ids = [p.proposal_id for p in model.proposals]
    if len(set(ids)) != len(ids):
        raise InputError("Duplicate proposal ID")
    _source_check(model, document)
    try:
        ledger = _read_regular(path / LEDGER, LEDGER, missing_ok=True) or b""
    except (ReviewError, OSError) as exc:
        raise InputError(f"Invalid proposal ledger: {type(exc).__name__}") from exc
    if ledger and not ledger.endswith(b"\n"):
        raise InputError("Proposal ledger has a partial final line")
    digest = sha256(raw)
    decisions: list[ProposalDecision] = []
    head = None
    for number, line in enumerate(ledger.split(b"\n")[:-1], start=1):
        try:
            d = ProposalDecision.model_validate_json(line)
        except ValidationError as exc:
            raise InputError(
                f"Proposal ledger line {number} failed validation"
            ) from exc
        if d.seq != number or d.prev_sha256 != head or d.proposal_set_sha256 != digest:
            raise InputError(f"Proposal ledger line {number} breaks the hash chain")
        if d.proposal_id not in ids or any(
            x.proposal_id == d.proposal_id for x in decisions
        ):
            raise InputError(f"Proposal ledger line {number} breaks review rules")
        decisions.append(d)
        head = sha256(line)
    return ProposalWorkspace(path, model, digest, document, decisions, head)


def _result(status_key: str) -> dict:
    return {
        "schema_version": 1,
        status_key: "error",
        "llm_mode": "not_used",
        "engine_mode": "not_used",
        "deployment_status": "unverified",
    }


def _fail(result: dict, exc: Exception, kind: str | None = None) -> tuple[dict, int]:
    kind = kind or getattr(exc, "kind", "input_error")
    result["error"] = {"kind": kind, "message": str(exc)}
    return result, 4 if kind == "output_error" else 2


def _summary(ws: ProposalWorkspace) -> list[dict]:
    rows = []
    for p in ws.model.proposals:
        d = ws.decision(p.proposal_id)
        status = (
            "pending"
            if d is None
            else ("approved" if d.decision == "approve" else "rejected")
        )
        rows.append(p.model_dump() | {"current_status": status})
    return rows


def init(proposals: Path, base_path: Path, workspace: Path) -> tuple[dict, int]:
    from .reviewed import load_for_analysis

    result = _result("proposal_status")
    try:
        if os.path.lexists(workspace):
            raise ReviewError(
                "output_error", "Workspace already exists; choose a new path"
            )
        if proposals.name != PROPOSALS:
            raise InputError(f"Proposal file must be named {PROPOSALS}")
        staged = load_proposals(proposals.parent)
        base, _ = load_for_analysis(base_path)
        _check_base(staged.model, base)
        try:
            workspace.mkdir(parents=True)
            for name in (PROPOSALS, staged.model.source.path):
                shutil.copyfile(staged.path / name, workspace / name)
        except OSError as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            raise ReviewError(
                "output_error", f"Cannot write workspace: {type(exc).__name__}"
            ) from exc
        ws = load_proposals(workspace)
        result.update(
            proposal_status="success", workspace=str(workspace), proposals=_summary(ws)
        )
        return result, 0
    except (InputError, ReviewError) as exc:
        return _fail(result, exc)


def decide(
    workspace: Path, pid: str, revision: int, decision: str, reason: str, reviewer: str
) -> tuple[dict, int]:
    result = _result("decide_status")

    def check(ws: ProposalWorkspace) -> None:
        if pid not in {p.proposal_id for p in ws.model.proposals}:
            raise ReviewError("input_error", f"Unknown proposal: {pid}")
        if revision != 1:
            raise ReviewError("input_error", f"{pid} has revision 1, not {revision}")
        if ws.decision(pid) is not None:
            raise ReviewError(
                "already_decided", f"{pid} revision 1 already has a decision"
            )

    try:
        _text_field(reason, "reason", MAX_REASON)
        _text_field(reviewer, "reviewer", MAX_REVIEWER)
        if decision not in ("approve", "reject"):
            raise ReviewError("input_error", "decision must be approve or reject")
        check(load_proposals(workspace))
        try:
            fd = os.open(
                workspace / LEDGER,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o644,
            )
        except OSError as exc:
            raise ReviewError(
                "output_error", f"Cannot open ledger: {type(exc).__name__}"
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            ws = load_proposals(workspace)
            check(ws)
            entry = ProposalDecision(
                seq=len(ws.decisions) + 1,
                decided_at=datetime.now(UTC),
                proposal_id=pid,
                revision=1,
                decision=decision,
                reason=reason,
                reviewer=reviewer,
                proposal_set_sha256=ws.sha256,
                prev_sha256=ws.head,
            )
            try:
                os.write(fd, entry.model_dump_json().encode() + b"\n")
                os.fsync(fd)
            except OSError as exc:
                raise ReviewError(
                    "output_error", f"Cannot append ledger: {type(exc).__name__}"
                ) from exc
        finally:
            os.close(fd)
        ws = load_proposals(workspace)
        result.update(
            decide_status="success", proposals=_summary(ws), ledger_head_sha256=ws.head
        )
        return result, 0
    except (InputError, ReviewError) as exc:
        return _fail(result, exc)


def build_manifest(ws: ProposalWorkspace, base: Snapshot) -> ScenarioSnapshot:
    _check_base(ws.model, base)
    applied, not_applied = [], []
    for p in ws.model.proposals:
        d = ws.decision(p.proposal_id)
        if d is not None and d.decision == "approve":
            applied.append(
                AppliedProposal(
                    proposal_id=p.proposal_id,
                    revision=p.revision,
                    operation=p.operation,
                    subject=p.subject,
                    object=p.object,
                    section=p.section,
                    quote=p.quote,
                    decision_seq=d.seq,
                    reviewer=d.reviewer,
                    reason=d.reason,
                )
            )
        else:
            not_applied.append(
                NotApplied(
                    proposal_id=p.proposal_id,
                    status="pending" if d is None else "rejected",
                )
            )
    if not applied:
        raise ReviewError(
            "nothing_approved",
            "No approved proposals; pending proposals never change the base",
        )
    removed = sorted((a.subject, a.object) for a in applied if a.operation == "remove")
    added = sorted((a.subject, a.object) for a in applied if a.operation == "add")
    edges = sorted((set(base.edges) - set(removed)) | set(added))
    identity = {"base": base.snapshot_hash, "proposals": ws.sha256, "ledger": ws.head}
    return ScenarioSnapshot(
        schema_version=1,
        kind="scenario_snapshot",
        scenario_id="sc-" + sha256(json.dumps(identity, sort_keys=True).encode())[:16],
        purpose="proposed",
        base=ScenarioBase(
            snapshot_id=base.snapshot_id,
            snapshot_hash=base.snapshot_hash,
            path="base/snapshot.json",
        ),
        proposal_set_sha256=ws.sha256,
        ledger_head_sha256=ws.head,
        applied=applied,
        not_applied=not_applied,
        overrides={"added": added, "removed": removed},
        edges=edges,
        deployment_status="unverified",
    )


def build(workspace: Path, base_path: Path, output: Path) -> tuple[dict, int]:
    from .reviewed import load_for_analysis

    result = _result("build_status")
    try:
        ws = load_proposals(workspace)
        base, _ = load_for_analysis(base_path)
        build_manifest(ws, base)
        if os.path.lexists(output):
            raise ReviewError(
                "output_error", "Output already exists; choose a new path"
            )
        try:
            output.mkdir(parents=True)
            # The whole base directory travels with the scenario (sources, evidence).
            shutil.copytree(base_path.parent, output / "base", symlinks=False)
            if base_path.name != "snapshot.json":
                os.replace(
                    output / "base" / base_path.name, output / "base/snapshot.json"
                )
            (output / "proposals").mkdir()
            for name in (PROPOSALS, ws.model.source.path, LEDGER):
                shutil.copyfile(workspace / name, output / "proposals" / name)
            copied_base, _ = load_for_analysis(output / "base/snapshot.json")
            manifest = build_manifest(load_proposals(output / "proposals"), copied_base)
            with (output / SCENARIO).open("x", encoding="utf-8") as handle:
                handle.write(manifest.model_dump_json(indent=2) + "\n")
        except BaseException as exc:
            shutil.rmtree(output, ignore_errors=True)
            if isinstance(exc, OSError):
                raise ReviewError(
                    "output_error", f"Cannot write scenario: {type(exc).__name__}"
                ) from exc
            raise
        result.update(
            build_status="success",
            scenario=str(output / SCENARIO),
            scenario_id=manifest.scenario_id,
            base=manifest.base.model_dump(),
            applied=[a.model_dump() for a in manifest.applied],
            not_applied=[n.model_dump() for n in manifest.not_applied],
            overrides=manifest.overrides,
        )
        return result, 0
    except (InputError, ReviewError) as exc:
        return _fail(result, exc)


def load_scenario(path: Path, raw: bytes) -> tuple[Snapshot, dict]:
    from .reviewed import load_for_analysis

    try:
        parsed = ScenarioSnapshot.model_validate_json(raw)
    except ValidationError as exc:
        raise InputError(
            f"Scenario snapshot failed schema validation ({exc.error_count()} error(s))"
        ) from exc
    base, base_extras = load_for_analysis(path.parent / parsed.base.path)
    ws = load_proposals(path.parent / "proposals")
    try:
        rebuilt = build_manifest(ws, base)
    except ReviewError as exc:
        raise InputError(f"Scenario evidence failed verification: {exc}") from exc
    if rebuilt != parsed:
        raise InputError("Scenario snapshot does not match its base and proposals")
    removed = set(map(tuple, parsed.overrides["removed"]))
    source = ws.model.source
    assertions = [
        # Reviewed v1 facts name no source; their snapshot has exactly one.
        a
        if "source_sha256" in a or "source_id" in a
        else a | {"source_sha256": base.sources[0]["sha256"]}
        for a in base.assertions
        if (a["subject"], a["object"]) not in removed
    ]
    assertions += [
        {
            "assertion_id": a.proposal_id,
            "revision": a.revision,
            "subject": a.subject,
            "object": a.object,
            "source_id": source.source_id,
            "source_version": source.source_version,
            "section": a.section,
            "quote": a.quote,
            "evidence_kind": "proposal",
        }
        for a in parsed.applied
        if a.operation == "add"
    ]
    snapshot = Snapshot(
        parsed.scenario_id,
        sha256(raw),
        "proposed",
        base.services,
        tuple(map(tuple, parsed.edges)),
        tuple(assertions),
        tuple(base.sources) + (source.model_dump(),),
    )
    extras = {
        "fixture": False,
        "snapshot_kind": "scenario",
        "scenario": {
            "base": parsed.base.model_dump(),
            "base_kind": base_extras.get("snapshot_kind", "fixture"),
            "applied": [a.model_dump() for a in parsed.applied],
            "not_applied": [n.model_dump() for n in parsed.not_applied],
            "overrides": parsed.overrides,
        },
        "limitations": [
            "Scenario model: approved proposals override the base; base sources are not withdrawn.",
            "Deployment unverified; a scenario is not a deployed change.",
        ],
    }
    return snapshot, extras


def compare(base_result: dict, scenario_result: dict, scenario: dict) -> dict:
    before = set(base_result["upstream_candidates"])
    after = set(scenario_result["upstream_candidates"])

    def paths(result: dict, service: str) -> list[list[str]]:
        return [w["path"] for w in result["witnesses"].get(service, [])]

    kept = sorted(before & after)
    return {
        "target": base_result["target"],
        "base": {
            "snapshot_id": base_result["snapshot_id"],
            "upstream_candidates": base_result["upstream_candidates"],
        },
        "scenario": {
            "snapshot_id": scenario_result["snapshot_id"],
            "upstream_candidates": scenario_result["upstream_candidates"],
        },
        "diff": {
            "removed": sorted(before - after),
            "added": sorted(after - before),
            "kept": kept,
        },
        "path_changes": {
            s: {"base": paths(base_result, s), "scenario": paths(scenario_result, s)}
            for s in kept
            if paths(base_result, s) != paths(scenario_result, s)
        },
        "overrides": scenario["applied"],
        "not_applied": scenario["not_applied"],
    }


def render_compare_markdown(result: dict) -> str:
    from .evidence import _md

    c = result["comparison"]
    lines = [
        f"# ChangeScope scenario comparison: {_md(c['target'])}",
        "",
        f"- base: {_md(c['base']['snapshot_id'])} -> scenario: {_md(c['scenario']['snapshot_id'])}",
        f"- rules_hash: {result['scenario_result']['rules_hash']}",
        f"- deployment_status: {result['deployment_status']}",
        "",
        "## Upstream candidates",
        "",
        f"- base: {', '.join(c['base']['upstream_candidates']) or '(none)'}",
        f"- scenario: {', '.join(c['scenario']['upstream_candidates']) or '(none)'}",
        f"- removed: {', '.join(c['diff']['removed']) or '(none)'}",
        f"- added: {', '.join(c['diff']['added']) or '(none)'}",
        f"- kept: {', '.join(c['diff']['kept']) or '(none)'}",
        "",
        "## Applied proposals (scenario overrides, not source withdrawals)",
        "",
    ]
    for a in c["overrides"]:
        lines.append(
            f"- {a['proposal_id']} {a['operation']} {a['subject']} -> {a['object']} "
            f'(approved by {_md(a["reviewer"])}): "{_md(a["quote"])}"'
        )
    for n in c["not_applied"]:
        lines.append(f"- {n['proposal_id']} not applied ({n['status']})")
    lines += ["", "## Path changes for kept candidates", ""]
    if not c["path_changes"]:
        lines.append("- (none)")
    for service, change in c["path_changes"].items():
        lines.append(f"- {service}")
        lines += [f"  - base: {' -> '.join(p)}" for p in change["base"]]
        lines += [f"  - scenario: {' -> '.join(p)}" for p in change["scenario"]]
    checks = result["scenario_result"].get("candidate_evidence")
    if checks is not None:
        lines += ["", "## Tests to re-check in the scenario", ""]
        target = result["scenario_result"]["target_checks"]
        lines += [
            f"- target {c['target']}: "
            + (", ".join(t["doc_id"] for t in target["tests"]) or "(no linked tests)")
        ]
        for service, found in checks.items():
            lines.append(
                f"- {service}: "
                + (
                    ", ".join(t["doc_id"] for t in found["tests"])
                    or "(no linked tests: unverified)"
                )
            )
    lines += [
        "",
        "## Unverified",
        "",
        "- Production deployment state is unverified for both models.",
    ]
    return "\n".join(lines) + "\n"
