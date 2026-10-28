"""T03-C: export approved candidates as a reviewed snapshot and load it for analysis.

A reviewed snapshot directory carries its own review evidence (candidates, ledger and
source blob). The loader re-verifies that evidence and rebuilds the snapshot from it,
so hand-edited approvals are refused instead of analyzed. T01's fixture loader is
unchanged and still handles every other snapshot.
"""

import fcntl
import json
import os
import shutil
from pathlib import Path
from typing import Literal, get_args

from pydantic import Field, ValidationError

from .extraction import (
    CandidateSet,
    Quote,
    Section,
    Service,
    Sha256,
    SourceCheck,
    SourceRecord,
    StrictModel,
)
from .review import (
    CANDIDATES,
    LEDGER,
    ReviewError,
    Workspace,
    _read_regular,
    load_workspace,
)
from .schemas import InputError, Snapshot, load_snapshot, sha256

SNAPSHOT = "snapshot.json"
SERVICES = list(get_args(Service))


class ReviewedFact(StrictModel):
    candidate_id: str
    revision: int = Field(ge=1)
    subject: Service
    predicate: Literal["depends_on"]
    object: Service
    section: Section
    quote: Quote
    decision_seq: int = Field(ge=1)
    reviewer: str
    reason: str


class ExcludedCandidate(StrictModel):
    candidate_id: str
    revision: int = Field(ge=1)
    subject: Service
    predicate: Literal["depends_on"]
    object: Service
    status: Literal["pending", "rejected", "approval_blocked"]
    source_check: SourceCheck


class ReviewSummary(StrictModel):
    total: int
    approved: int
    pending: int
    rejected: int
    approval_blocked: int


class ReviewedSnapshot(StrictModel):
    schema_version: Literal[1]
    kind: Literal["reviewed_snapshot"]
    snapshot_id: str
    purpose: Literal["baseline"]
    fixture: Literal[False]
    services: list[Service]
    source: SourceRecord
    candidate_set_sha256: Sha256
    ledger_head_sha256: Sha256
    summary: ReviewSummary
    approved: list[ReviewedFact] = Field(min_length=1)
    excluded: list[ExcludedCandidate]
    deployment_status: Literal["unverified"]


def build_snapshot(workspace: Workspace) -> ReviewedSnapshot:
    """Derive the snapshot from verified review evidence; approvals come from the ledger only."""
    approved: list[ReviewedFact] = []
    excluded: list[ExcludedCandidate] = []
    for c in workspace.candidate_set.candidates:
        decision = workspace.decision_for(c)
        if decision is not None and decision.decision == "approve":
            approved.append(
                ReviewedFact(
                    candidate_id=c.candidate_id,
                    revision=c.revision,
                    subject=c.subject,
                    predicate=c.predicate,
                    object=c.object,
                    section=c.section,
                    quote=c.quote,
                    decision_seq=decision.seq,
                    reviewer=decision.reviewer,
                    reason=decision.reason,
                )
            )
            continue
        if decision is not None:
            status = "rejected"
        elif c.source_check.status == "passed":
            status = "pending"
        else:
            status = "approval_blocked"
        excluded.append(
            ExcludedCandidate(
                candidate_id=c.candidate_id,
                revision=c.revision,
                subject=c.subject,
                predicate=c.predicate,
                object=c.object,
                status=status,
                source_check=c.source_check,
            )
        )
    if not approved:
        # An empty model would analyze as a normal empty result; refuse it instead.
        raise ReviewError(
            "nothing_approved", "No approved candidates; export needs at least one"
        )
    source = workspace.candidate_set.source
    identity = {
        "source_sha256": source.sha256,
        "candidate_set_sha256": workspace.candidate_set_sha256,
        "ledger_head_sha256": workspace.ledger_head_sha256,
    }
    statuses = [e.status for e in excluded]
    return ReviewedSnapshot(
        schema_version=1,
        kind="reviewed_snapshot",
        snapshot_id="rs-" + sha256(json.dumps(identity, sort_keys=True).encode())[:16],
        purpose="baseline",
        fixture=False,
        services=SERVICES,
        source=source,
        candidate_set_sha256=workspace.candidate_set_sha256,
        ledger_head_sha256=workspace.ledger_head_sha256,
        summary=ReviewSummary(
            total=len(workspace.candidate_set.candidates),
            approved=len(approved),
            pending=statuses.count("pending"),
            rejected=statuses.count("rejected"),
            approval_blocked=statuses.count("approval_blocked"),
        ),
        approved=approved,
        excluded=excluded,
        deployment_status="unverified",
    )


def _copy_evidence(workspace_path: Path, output: Path) -> ReviewedSnapshot:
    try:
        fd = os.open(workspace_path / LEDGER, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ReviewError(
            "integrity_error", f"Cannot open ledger: {type(exc).__name__}"
        ) from exc
    try:
        # Shared lock keeps `review decide` out while the evidence files are read.
        fcntl.flock(fd, fcntl.LOCK_SH)
        candidates = _read_regular(workspace_path / CANDIDATES, CANDIDATES)
        ledger = _read_regular(workspace_path / LEDGER, LEDGER)
        try:
            source = CandidateSet.model_validate_json(candidates).source
        except ValidationError as exc:
            raise ReviewError(
                "integrity_error", f"{CANDIDATES} changed during export"
            ) from exc
        blob_path = f"sources/{source.sha256}.md"
        blob = _read_regular(workspace_path / blob_path, "Source blob")
    except OSError as exc:
        raise ReviewError(
            "integrity_error", f"Cannot read workspace: {type(exc).__name__}"
        ) from exc
    finally:
        os.close(fd)
    try:
        (output / "sources").mkdir()
        for name, data in (
            (CANDIDATES, candidates),
            (LEDGER, ledger),
            (blob_path, blob),
        ):
            with (output / name).open("xb") as handle:
                handle.write(data)
    except OSError as exc:
        raise ReviewError(
            "output_error", f"Cannot write snapshot: {type(exc).__name__}"
        ) from exc
    # Build from the copy, so the snapshot describes exactly the evidence it ships.
    snapshot = build_snapshot(load_workspace(output))
    try:
        with (output / SNAPSHOT).open("xb") as handle:
            handle.write((snapshot.model_dump_json(indent=2) + "\n").encode())
    except OSError as exc:
        raise ReviewError(
            "output_error", f"Cannot write snapshot: {type(exc).__name__}"
        ) from exc
    return snapshot


def export(workspace_path: Path, output: Path) -> tuple[dict, int]:
    result = {
        "schema_version": 1,
        "export_status": "error",
        "workspace": str(workspace_path),
        "output": str(output),
        "llm_mode": "not_used",
        "engine_mode": "not_used",
        "approved": None,
        "deployment_status": "unverified",
    }
    try:
        # Refuse invalid or unapproved workspaces before creating anything.
        build_snapshot(load_workspace(workspace_path))
        if os.path.lexists(output):
            raise ReviewError(
                "output_error", "Output already exists; choose a new path"
            )
        try:
            output.mkdir(parents=True)
        except OSError as exc:
            raise ReviewError(
                "output_error", f"Cannot create output: {type(exc).__name__}"
            ) from exc
        try:
            snapshot = _copy_evidence(workspace_path, output)
        except BaseException:
            shutil.rmtree(output, ignore_errors=True)
            raise
        result.update(
            export_status="success",
            snapshot=str(output / SNAPSHOT),
            snapshot_id=snapshot.snapshot_id,
            snapshot_sha256=sha256((output / SNAPSHOT).read_bytes()),
            candidate_set_sha256=snapshot.candidate_set_sha256,
            ledger_head_sha256=snapshot.ledger_head_sha256,
            summary=snapshot.summary.model_dump(),
            approved=[f.model_dump() for f in snapshot.approved],
            excluded=[e.model_dump() for e in snapshot.excluded],
            limitations=[
                "Only ledger approvals enter the snapshot; excluded candidates are listed.",
                "approved is a document review status, not deployment state.",
                "Keep ledger_head_sha256 externally to detect removed trailing decisions.",
            ],
        )
        return result, 0
    except ReviewError as exc:
        result["error"] = {"kind": exc.kind, "message": str(exc)}
        return result, 4 if exc.kind == "output_error" else 2


def load_reviewed_snapshot(path: Path, raw: bytes) -> tuple[Snapshot, dict]:
    try:
        parsed = ReviewedSnapshot.model_validate_json(raw)
    except ValidationError as exc:
        raise InputError(
            f"Reviewed snapshot failed schema validation ({exc.error_count()} error(s))"
        ) from exc
    try:
        rebuilt = build_snapshot(load_workspace(path.parent))
    except ReviewError as exc:
        raise InputError(
            f"Review evidence failed verification ({exc.kind}): {exc}"
        ) from exc
    if rebuilt != parsed:
        raise InputError("Reviewed snapshot does not match its review evidence")
    snapshot = Snapshot(
        parsed.snapshot_id,
        sha256(raw),
        parsed.purpose,
        frozenset(parsed.services),
        tuple(sorted({(f.subject, f.object) for f in parsed.approved})),
        tuple(f.model_dump() for f in parsed.approved),
        (parsed.source.model_dump(),),
    )
    extras = {
        "fixture": False,
        "snapshot_kind": "reviewed",
        "review": {
            "candidate_set_sha256": parsed.candidate_set_sha256,
            "ledger_head_sha256": parsed.ledger_head_sha256,
            "summary": parsed.summary.model_dump(),
            "excluded": [e.model_dump() for e in parsed.excluded],
        },
        "limitations": [
            "Document model of human-approved candidates only; deployment unverified.",
            "Relation meaning and direction were judged by the recorded reviewer.",
            "Witnesses, test evidence, revisions and scenario comparison are not implemented in T03.",
        ],
    }
    return snapshot, extras


def load_for_analysis(path: Path) -> tuple[Snapshot, dict]:
    """Route reviewed snapshots to their own loader; all else stays T01's fixture loader."""
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise InputError(f"Cannot read snapshot: {exc}") from exc
    if isinstance(data, dict) and data.get("kind") == "reviewed_snapshot":
        return load_reviewed_snapshot(path, raw)
    return load_snapshot(path), {}
