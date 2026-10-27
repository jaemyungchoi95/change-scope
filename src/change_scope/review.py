"""T03-B: human review decisions in an append-only, hash-chained ledger.

Only `review decide` writes approvals. Candidate files stay immutable, and every read
re-verifies the workspace so edited candidates, sources or ledger entries are reported
as integrity errors. This detects workflow tampering; it does not authenticate users.
"""

import fcntl
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError

from .extraction import (
    Candidate,
    CandidateSet,
    ExtractedCandidate,
    Sha256,
    StrictModel,
    candidate_id,
    check_source,
)
from .schemas import section_bodies, sha256

CANDIDATES = "candidates.json"
LEDGER = "ledger.jsonl"
MAX_FILE_BYTES = 4_194_304
MAX_REASON = 1000
MAX_REVIEWER = 100
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_FIELD_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class ReviewError(ValueError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class Decision(StrictModel):
    seq: int = Field(ge=1)
    decided_at: AwareDatetime
    candidate_id: str
    revision: int = Field(ge=1)
    decision: Literal["approve", "reject"]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=MAX_REASON)]
    reviewer: Annotated[str, StringConstraints(min_length=1, max_length=MAX_REVIEWER)]
    candidate_set_sha256: Sha256
    source_sha256: Sha256
    prev_sha256: Sha256 | None


@dataclass(frozen=True)
class Workspace:
    path: Path
    candidate_set: CandidateSet
    candidate_set_sha256: str
    document: str
    decisions: tuple[Decision, ...]
    ledger_head_sha256: str | None

    def candidate(self, cid: str) -> Candidate:
        for candidate in self.candidate_set.candidates:
            if candidate.candidate_id == cid:
                return candidate
        raise ReviewError("input_error", f"Unknown candidate: {cid}")

    def decision_for(self, candidate: Candidate) -> Decision | None:
        for decision in self.decisions:
            if (decision.candidate_id, decision.revision) == (
                candidate.candidate_id,
                candidate.revision,
            ):
                return decision
        return None


def _integrity(message: str) -> ReviewError:
    return ReviewError("integrity_error", message)


def _read_regular(path: Path, label: str, *, missing_ok: bool = False) -> bytes | None:
    if missing_ok and not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise _integrity(f"{label} must be a regular file")
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise _integrity(f"{label} exceeds size limit")
    return data


def load_workspace(path: Path) -> Workspace:
    """Load and re-verify every candidate, source and ledger entry."""
    if not (path / CANDIDATES).exists():
        raise ReviewError(
            "input_error", "Not a review workspace: candidates.json missing"
        )
    try:
        raw = _read_regular(path / CANDIDATES, CANDIDATES)
        try:
            candidate_set = CandidateSet.model_validate_json(raw)
        except ValidationError as exc:
            raise _integrity(
                f"{CANDIDATES} failed schema validation ({exc.error_count()} error(s))"
            ) from exc
        set_sha = sha256(raw)
        source = candidate_set.source
        expected_blob = f"sources/{source.sha256}.md"
        if source.blob != expected_blob:
            raise _integrity("Source blob path is not content-addressed")
        blob = _read_regular(path / expected_blob, "Source blob")
        if sha256(blob) != source.sha256 or len(blob) != source.byte_length:
            raise _integrity("Source blob does not match recorded hash")
        try:
            document = blob.decode("utf-8")
        except UnicodeError as exc:
            raise _integrity("Source blob is not UTF-8") from exc
        seen = set()
        for c in candidate_set.candidates:
            if (c.source_id, c.source_version, c.source_sha256) != (
                source.source_id,
                source.source_version,
                source.sha256,
            ):
                raise _integrity(f"{c.candidate_id}: source identity mismatch")
            if c.revision != 1:
                raise _integrity(f"{c.candidate_id}: T03 issues revision 1 only")
            claim = ExtractedCandidate(
                subject=c.subject,
                predicate=c.predicate,
                object=c.object,
                section=c.section,
                quote=c.quote,
                review_status="pending",
            )
            if candidate_id(source.sha256, claim) != c.candidate_id:
                raise _integrity(f"{c.candidate_id}: content does not match its ID")
            if check_source(document, claim) != c.source_check:
                raise _integrity(f"{c.candidate_id}: stored source check is stale")
            if c.candidate_id in seen:
                raise _integrity(f"{c.candidate_id}: duplicate candidate")
            seen.add(c.candidate_id)
        ledger = _read_regular(path / LEDGER, LEDGER, missing_ok=True) or b""
    except OSError as exc:
        raise _integrity(f"Cannot read workspace: {type(exc).__name__}") from exc
    workspace = Workspace(path, candidate_set, set_sha, document, (), None)
    decisions: list[Decision] = []
    head = None
    if ledger and not ledger.endswith(b"\n"):
        raise _integrity("Ledger has a partial final line")
    for number, line in enumerate(ledger.split(b"\n")[:-1], start=1):
        try:
            decision = Decision.model_validate_json(line)
        except ValidationError as exc:
            raise _integrity(f"Ledger line {number} failed validation") from exc
        if decision.seq != number or decision.prev_sha256 != head:
            raise _integrity(f"Ledger line {number} breaks the hash chain")
        if (decision.candidate_set_sha256, decision.source_sha256) != (
            set_sha,
            source.sha256,
        ):
            raise _integrity(f"Ledger line {number} refers to other candidates/source")
        try:
            candidate = workspace.candidate(decision.candidate_id)
        except ReviewError as exc:
            raise _integrity(
                f"Ledger line {number} refers to an unknown candidate"
            ) from exc
        if decision.revision != candidate.revision:
            raise _integrity(f"Ledger line {number} refers to an unknown revision")
        if any(
            (d.candidate_id, d.revision) == (decision.candidate_id, decision.revision)
            for d in decisions
        ):
            raise _integrity(f"Ledger line {number} decides a candidate twice")
        if decision.decision == "approve" and candidate.source_check.status != "passed":
            raise _integrity(f"Ledger line {number} approves a blocked candidate")
        decisions.append(decision)
        head = sha256(line)
    return Workspace(path, candidate_set, set_sha, document, tuple(decisions), head)


def _status(workspace: Workspace, candidate: Candidate) -> str:
    decision = workspace.decision_for(candidate)
    if decision is None:
        return "pending"
    return "approved" if decision.decision == "approve" else "rejected"


def _view(workspace: Workspace, candidate: Candidate) -> dict:
    decision = workspace.decision_for(candidate)
    bodies = section_bodies(workspace.document, candidate.section)
    return candidate.model_dump() | {
        "current_status": _status(workspace, candidate),
        "approvable": decision is None and candidate.source_check.status == "passed",
        "decision": decision.model_dump(mode="json") if decision else None,
        "section_text": bodies[0].strip() if len(bodies) == 1 else None,
    }


def _summary(workspace: Workspace) -> dict:
    statuses = [_status(workspace, c) for c in workspace.candidate_set.candidates]
    return {
        "total": len(statuses),
        "pending": statuses.count("pending"),
        "approved": statuses.count("approved"),
        "rejected": statuses.count("rejected"),
        "source_check_failed": sum(
            c.source_check.status == "failed"
            for c in workspace.candidate_set.candidates
        ),
    }


def _base(workspace_path: Path, status_key: str) -> dict:
    return {
        "schema_version": 1,
        status_key: "error",
        "workspace": str(workspace_path),
        "llm_mode": "not_used",
        "engine_mode": "not_used",
        "candidates": None,
        "deployment_status": "unverified",
    }


def _exit_code(kind: str) -> int:
    return 4 if kind == "output_error" else 2


def show(workspace_path: Path, cid: str | None = None) -> tuple[dict, int]:
    result = _base(workspace_path, "show_status")
    try:
        workspace = load_workspace(workspace_path)
        selected = (
            [workspace.candidate(cid)] if cid else workspace.candidate_set.candidates
        )
        source = workspace.candidate_set.source
        result.update(
            show_status="success" if selected else "empty",
            source=source.model_dump(),
            candidate_set_sha256=workspace.candidate_set_sha256,
            ledger_head_sha256=workspace.ledger_head_sha256,
            summary=_summary(workspace),
            candidates=[_view(workspace, c) for c in selected],
            limitations=[
                "Source checks verify quote location only; relation meaning is a human decision.",
                "approved is a document review status, not deployment state.",
                "The ledger detects edits and removals before its last entry; "
                "removing trailing entries needs an external copy of the head hash.",
            ],
        )
        return result, 0
    except ReviewError as exc:
        result["error"] = {"kind": exc.kind, "message": str(exc)}
        return result, _exit_code(exc.kind)


def _text_field(value: str, label: str, limit: int) -> str:
    if not value.strip() or len(value) > limit or _FIELD_CONTROL.search(value):
        raise ReviewError(
            "input_error",
            f"{label} must be 1-{limit} printable characters",
        )
    return value


def _decidable(
    workspace: Workspace, cid: str, revision: int, decision: str
) -> Candidate:
    candidate = workspace.candidate(cid)
    if revision != candidate.revision:
        raise ReviewError(
            "input_error", f"{cid} has revision {candidate.revision}, not {revision}"
        )
    if workspace.decision_for(candidate) is not None:
        raise ReviewError(
            "already_decided",
            f"{cid} revision {revision} already has a decision; "
            "changes require a new revision",
        )
    if decision == "approve" and candidate.source_check.status != "passed":
        raise ReviewError(
            "approval_blocked",
            f"{cid} failed source check ({candidate.source_check.kind}); "
            "it can only be rejected",
        )
    return candidate


def decide(
    workspace_path: Path,
    cid: str,
    revision: int,
    decision: str,
    reason: str,
    reviewer: str,
) -> tuple[dict, int]:
    result = _base(workspace_path, "decide_status")
    result.pop("candidates")
    result["candidate"] = None
    try:
        _text_field(reason, "reason", MAX_REASON)
        _text_field(reviewer, "reviewer", MAX_REVIEWER)
        if decision not in ("approve", "reject"):
            raise ReviewError("input_error", "decision must be approve or reject")
        # Validate before touching the ledger so a refused request creates no files.
        _decidable(load_workspace(workspace_path), cid, revision, decision)
        try:
            fd = os.open(
                workspace_path / LEDGER,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o644,
            )
        except OSError as exc:
            raise ReviewError(
                "output_error", f"Cannot open ledger: {type(exc).__name__}"
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # Re-check under the lock: another decide may have appended meanwhile.
            workspace = load_workspace(workspace_path)
            _decidable(workspace, cid, revision, decision)
            entry = Decision(
                seq=len(workspace.decisions) + 1,
                decided_at=datetime.now(UTC),
                candidate_id=cid,
                revision=revision,
                decision=decision,
                reason=reason,
                reviewer=reviewer,
                candidate_set_sha256=workspace.candidate_set_sha256,
                source_sha256=workspace.candidate_set.source.sha256,
                prev_sha256=workspace.ledger_head_sha256,
            )
            line = entry.model_dump_json().encode()
            try:
                os.write(fd, line + b"\n")
                os.fsync(fd)
            except OSError as exc:
                raise ReviewError(
                    "output_error", f"Cannot append ledger: {type(exc).__name__}"
                ) from exc
        finally:
            os.close(fd)
        updated = load_workspace(workspace_path)
        result.update(
            decide_status="success",
            candidate=_view(updated, updated.candidate(cid)),
            entry_sha256=sha256(line),
            ledger_head_sha256=updated.ledger_head_sha256,
            summary=_summary(updated),
        )
        return result, 0
    except ReviewError as exc:
        result["error"] = {"kind": exc.kind, "message": str(exc)}
        return result, _exit_code(exc.kind)


def _safe(value: object) -> str:
    """Neutralize terminal control sequences from untrusted document text."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(value))


def render_text(result: dict) -> str:
    if "error" in result:
        return f"ERROR [{result['error']['kind']}]: {_safe(result['error']['message'])}"
    source = result["source"]
    summary = result["summary"]
    lines = [
        f"Workspace : {_safe(result['workspace'])}",
        f"Source    : {source['source_id']} v{source['source_version']} "
        f"sha256={source['sha256'][:16]}… (deployment unverified)",
        f"Candidates: {summary['total']} — pending {summary['pending']}, "
        f"approved {summary['approved']}, rejected {summary['rejected']}; "
        f"source check failed {summary['source_check_failed']}",
        f"Ledger    : head={result['ledger_head_sha256'] or '(empty)'}",
    ]
    detail = len(result["candidates"]) == 1
    for number, c in enumerate(result["candidates"], start=1):
        check = c["source_check"]
        lines += [
            "",
            f"[{number}] {c['candidate_id']} rev {c['revision']}  "
            f"{c['current_status'].upper()}"
            + ("" if c["approvable"] or c["decision"] else "  (approval blocked)"),
            f"    relation : {c['subject']} -> {c['object']} ({c['predicate']})",
            f"    section  : {_safe(c['section'])}",
            f'    quote    : "{_safe(c["quote"])}"',
            "    check    : "
            + (
                "passed (quote location only)"
                if check["status"] == "passed"
                else f"FAILED {check['kind']} — {_safe(check['message'])}"
            ),
        ]
        if c["decision"]:
            d = c["decision"]
            lines.append(
                f"    decision : {d['decision']} by {_safe(d['reviewer'])} "
                f"at {d['decided_at']} — {_safe(d['reason'])}"
            )
        if detail and c["section_text"] is not None:
            lines.append("    section text:")
            lines += [f"      | {_safe(t)}" for t in c["section_text"].splitlines()]
    return "\n".join(lines)
