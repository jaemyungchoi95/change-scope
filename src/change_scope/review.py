"""T03-B/T04: human review history in an append-only, hash-chained ledger.

Only `review decide` writes approvals. Candidate files stay immutable; T04 revisions,
new source versions and withdrawals are further ledger events. Every read replays the
ledger with the same rules the commands enforce, so edited candidates, sources or
ledger entries are reported as integrity errors. This detects workflow tampering; it
does not authenticate users.
"""

import copy
import fcntl
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError

from .extraction import (
    MAX_SOURCE_BYTES,
    Candidate,
    CandidateSet,
    ExtractedCandidate,
    Sha256,
    SourceRecord,
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


class _Event(StrictModel):
    """Common envelope of T04 ledger events; T03 decision lines carry no `type`."""

    seq: int = Field(ge=1)
    recorded_at: AwareDatetime
    reason: Annotated[str, StringConstraints(min_length=1, max_length=MAX_REASON)]
    reviewer: Annotated[str, StringConstraints(min_length=1, max_length=MAX_REVIEWER)]
    candidate_set_sha256: Sha256
    prev_sha256: Sha256 | None


class SourceAdded(_Event):
    type: Literal["source"]
    source: SourceRecord


class Revised(_Event):
    type: Literal["revise"]
    candidate: Candidate
    content_sha256: Sha256


class Withdrawn(_Event):
    type: Literal["withdraw"]
    candidate_id: str
    revision: int = Field(ge=1)


LedgerEvent = Decision | SourceAdded | Revised | Withdrawn
_EVENT_TYPES = {"source": SourceAdded, "revise": Revised, "withdraw": Withdrawn}


def parse_event(line: bytes) -> LedgerEvent:
    try:
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("ledger entry must be an object")
        model = _EVENT_TYPES.get(raw["type"]) if "type" in raw else Decision
        if model is None:
            raise ValueError("unknown ledger event type")
        return model.model_validate_json(line)
    except (ValueError, ValidationError) as exc:
        raise _integrity("Ledger entry failed validation") from exc


def content_sha256(candidate: Candidate) -> str:
    """Hash of what a revision claims, including the exact source bytes it cites."""
    content = candidate.model_dump(
        include={"source_sha256", "subject", "predicate", "object", "section", "quote"}
    )
    return sha256(json.dumps(content, sort_keys=True).encode())


def _claim(candidate: Candidate) -> ExtractedCandidate:
    return ExtractedCandidate(
        subject=candidate.subject,
        predicate=candidate.predicate,
        object=candidate.object,
        section=candidate.section,
        quote=candidate.quote,
        review_status="pending",
    )


@dataclass
class _History:
    """Review state replayed from the ledger; `apply` is the single rule set."""

    source_id: str
    sources: list[SourceRecord]
    documents: dict[str, str]
    revisions: dict[str, list[Candidate]]
    decisions: list[Decision] = field(default_factory=list)
    withdrawn: set[tuple[str, int]] = field(default_factory=set)

    def latest(self, cid: str) -> Candidate:
        if cid not in self.revisions:
            raise ReviewError("input_error", f"Unknown candidate: {cid}")
        return self.revisions[cid][-1]

    def decision(self, cid: str, revision: int) -> Decision | None:
        for d in self.decisions:
            if (d.candidate_id, d.revision) == (cid, revision):
                return d
        return None

    def apply(self, event: LedgerEvent, read: Callable[[SourceRecord], str]) -> None:
        current = self.sources[-1]
        if isinstance(event, Decision):
            latest = self.latest(event.candidate_id)
            if event.revision != latest.revision:
                raise ReviewError(
                    "input_error",
                    f"{event.candidate_id} has revision {latest.revision}, "
                    f"not {event.revision}",
                )
            if self.decision(event.candidate_id, event.revision) is not None:
                raise ReviewError(
                    "already_decided",
                    f"{event.candidate_id} revision {event.revision} already has a "
                    "decision; changes require a new revision",
                )
            if event.source_sha256 != latest.source_sha256:
                raise ReviewError("input_error", "Decision cites another source")
            if event.decision == "approve":
                if latest.source_check.status != "passed":
                    raise ReviewError(
                        "approval_blocked",
                        f"{event.candidate_id} failed source check "
                        f"({latest.source_check.kind}); it can only be rejected",
                    )
                if latest.source_version != current.source_version:
                    raise ReviewError(
                        "approval_blocked",
                        f"{event.candidate_id} cites source version "
                        f"{latest.source_version}, not current "
                        f"{current.source_version}; revise it first",
                    )
            self.decisions.append(event)
        elif isinstance(event, SourceAdded):
            record = event.source
            if record.source_id != self.source_id:
                raise ReviewError("input_error", "New version must keep the source ID")
            for known in self.sources:
                if known.source_version == record.source_version:
                    raise ReviewError(
                        "input_error",
                        f"Source version {record.source_version} already exists"
                        + (
                            ""
                            if known.sha256 == record.sha256
                            else " with different content; register a new version"
                        ),
                    )
            if record.blob != f"sources/{record.sha256}.md":
                raise ReviewError(
                    "input_error", "Source blob path is not content-addressed"
                )
            self.documents[record.sha256] = read(record)
            self.sources.append(record)
        elif isinstance(event, Revised):
            new = event.candidate
            latest = self.latest(new.candidate_id)
            if new.revision != latest.revision + 1:
                raise ReviewError(
                    "input_error",
                    f"{new.candidate_id} is at revision {latest.revision}; "
                    "revise from the latest revision",
                )
            if (new.source_id, new.source_version, new.source_sha256) != (
                current.source_id,
                current.source_version,
                current.sha256,
            ):
                raise ReviewError(
                    "input_error",
                    f"Revisions must cite the current source version "
                    f"{current.source_version}",
                )
            if new.source_check != check_source(
                self.documents[new.source_sha256], _claim(new)
            ):
                raise ReviewError("input_error", "Revision source check is stale")
            if event.content_sha256 != content_sha256(new):
                raise ReviewError("input_error", "Revision content hash mismatch")
            if content_sha256(new) == content_sha256(latest):
                raise ReviewError(
                    "input_error",
                    "Revision must change the claim or its source version",
                )
            self.revisions[new.candidate_id].append(new)
        else:
            latest = self.latest(event.candidate_id)
            decision = self.decision(event.candidate_id, event.revision)
            if (
                event.revision != latest.revision
                or decision is None
                or decision.decision != "approve"
            ):
                raise ReviewError(
                    "input_error",
                    f"Only the approved latest revision of {event.candidate_id} "
                    "can be withdrawn",
                )
            if (event.candidate_id, event.revision) in self.withdrawn:
                raise ReviewError(
                    "input_error",
                    f"{event.candidate_id} revision {event.revision} is already withdrawn",
                )
            self.withdrawn.add((event.candidate_id, event.revision))


@dataclass(frozen=True)
class Workspace:
    path: Path
    candidate_set: CandidateSet
    candidate_set_sha256: str
    history: _History
    event_count: int
    ledger_head_sha256: str | None

    @property
    def decisions(self) -> tuple[Decision, ...]:
        return tuple(self.history.decisions)

    @property
    def sources(self) -> tuple[SourceRecord, ...]:
        return tuple(self.history.sources)

    @property
    def current_source(self) -> SourceRecord:
        return self.history.sources[-1]

    def document(self, source_sha256: str) -> str:
        return self.history.documents[source_sha256]

    def candidate(self, cid: str) -> Candidate:
        """Latest revision of a candidate."""
        return self.history.latest(cid)

    def revisions(self, cid: str) -> tuple[Candidate, ...]:
        self.history.latest(cid)
        return tuple(self.history.revisions[cid])

    def decision_for(self, candidate: Candidate) -> Decision | None:
        return self.history.decision(candidate.candidate_id, candidate.revision)

    def is_withdrawn(self, candidate: Candidate) -> bool:
        return (candidate.candidate_id, candidate.revision) in self.history.withdrawn

    def latest_candidates(self) -> list[Candidate]:
        return [self.candidate(c.candidate_id) for c in self.candidate_set.candidates]


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
    history = _History(
        source_id=source.source_id,
        sources=[source],
        documents={source.sha256: document},
        revisions={c.candidate_id: [c] for c in candidate_set.candidates},
    )
    head = None
    if ledger and not ledger.endswith(b"\n"):
        raise _integrity("Ledger has a partial final line")
    lines = ledger.split(b"\n")[:-1]
    for number, line in enumerate(lines, start=1):
        try:
            event = parse_event(line)
        except ReviewError as exc:
            raise _integrity(f"Ledger line {number} failed validation") from exc
        if event.seq != number or event.prev_sha256 != head:
            raise _integrity(f"Ledger line {number} breaks the hash chain")
        if event.candidate_set_sha256 != set_sha:
            raise _integrity(f"Ledger line {number} refers to other candidates")
        try:
            history.apply(event, lambda record: _read_blob(path, record))
        except ReviewError as exc:
            raise _integrity(
                f"Ledger line {number} breaks review rules: {exc}"
            ) from exc
        head = sha256(line)
    return Workspace(path, candidate_set, set_sha, history, len(lines), head)


def _read_blob(path: Path, record: SourceRecord) -> str:
    try:
        blob = _read_regular(path / record.blob, "Source blob")
    except OSError as exc:
        raise _integrity(f"Cannot read source blob: {type(exc).__name__}") from exc
    if sha256(blob) != record.sha256 or len(blob) != record.byte_length:
        raise _integrity("Source blob does not match recorded hash")
    try:
        return blob.decode("utf-8")
    except UnicodeError as exc:
        raise _integrity("Source blob is not UTF-8") from exc


def _status(workspace: Workspace, candidate: Candidate) -> str:
    decision = workspace.decision_for(candidate)
    if decision is None:
        return "pending"
    if workspace.is_withdrawn(candidate):
        return "withdrawn"
    return "approved" if decision.decision == "approve" else "rejected"


def _source_current(workspace: Workspace, candidate: Candidate) -> bool:
    return candidate.source_version == workspace.current_source.source_version


def _view(workspace: Workspace, candidate: Candidate, *, history: bool = False) -> dict:
    decision = workspace.decision_for(candidate)
    bodies = section_bodies(
        workspace.document(candidate.source_sha256), candidate.section
    )
    view = candidate.model_dump() | {
        "current_status": _status(workspace, candidate),
        "approvable": decision is None
        and candidate.source_check.status == "passed"
        and _source_current(workspace, candidate),
        "source_current": _source_current(workspace, candidate),
        "decision": decision.model_dump(mode="json") if decision else None,
        "section_text": bodies[0].strip() if len(bodies) == 1 else None,
    }
    if history:
        view["history"] = [
            {
                "revision": r.revision,
                "subject": r.subject,
                "object": r.object,
                "section": r.section,
                "quote": r.quote,
                "source_version": r.source_version,
                "source_check": r.source_check.model_dump(),
                "status": _status(workspace, r),
                "decision": (
                    d.model_dump(mode="json")
                    if (d := workspace.decision_for(r))
                    else None
                ),
            }
            for r in workspace.revisions(candidate.candidate_id)
        ]
    return view


def _summary(workspace: Workspace) -> dict:
    latest = workspace.latest_candidates()
    statuses = [_status(workspace, c) for c in latest]
    return {
        "total": len(statuses),
        "pending": statuses.count("pending"),
        "approved": statuses.count("approved"),
        "rejected": statuses.count("rejected"),
        "source_check_failed": sum(c.source_check.status == "failed" for c in latest),
    }


def _lifecycle(workspace: Workspace) -> dict:
    latest = workspace.latest_candidates()
    return {
        "current_source_version": workspace.current_source.source_version,
        "source_versions": [s.source_version for s in workspace.sources],
        "revised": sum(c.revision > 1 for c in latest),
        "withdrawn": sum(workspace.is_withdrawn(c) for c in latest),
        "stale_source": sum(not _source_current(workspace, c) for c in latest),
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
        selected = [workspace.candidate(cid)] if cid else workspace.latest_candidates()
        source = workspace.candidate_set.source
        result.update(
            show_status="success" if selected else "empty",
            source=source.model_dump(),
            current_source=workspace.current_source.model_dump(),
            candidate_set_sha256=workspace.candidate_set_sha256,
            ledger_head_sha256=workspace.ledger_head_sha256,
            summary=_summary(workspace),
            lifecycle=_lifecycle(workspace),
            candidates=[_view(workspace, c, history=bool(cid)) for c in selected],
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


def _envelope(workspace: Workspace, reason: str, reviewer: str) -> dict:
    return {
        "seq": workspace.event_count + 1,
        "recorded_at": datetime.now(UTC),
        "reason": reason,
        "reviewer": reviewer,
        "candidate_set_sha256": workspace.candidate_set_sha256,
        "prev_sha256": workspace.ledger_head_sha256,
    }


def _checked(
    workspace: Workspace,
    build: Callable[[Workspace], LedgerEvent],
    document: str | None,
) -> LedgerEvent:
    """Build the next event and run it through the replay rules on a scratch copy."""
    event = build(workspace)
    history = copy.deepcopy(workspace.history)
    history.apply(event, lambda record: document)
    return event


def _append(
    workspace_path: Path,
    build: Callable[[Workspace], LedgerEvent],
    blob: bytes | None = None,
) -> tuple[Workspace, bytes]:
    """Append one event under the ledger lock; refused requests create no files."""
    document = blob.decode("utf-8") if blob is not None else None
    # Validate before touching the ledger so a refused request creates no files.
    _checked(load_workspace(workspace_path), build, document)
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
        # Re-check under the lock: another command may have appended meanwhile.
        event = _checked(load_workspace(workspace_path), build, document)
        line = event.model_dump_json().encode()
        try:
            if isinstance(event, SourceAdded):
                _write_blob(workspace_path / event.source.blob, blob)
            os.write(fd, line + b"\n")
            os.fsync(fd)
        except OSError as exc:
            raise ReviewError(
                "output_error", f"Cannot append ledger: {type(exc).__name__}"
            ) from exc
    finally:
        os.close(fd)
    return load_workspace(workspace_path), line


def _write_blob(path: Path, blob: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(blob)
    except FileExistsError:
        # Content-addressed: an existing blob must already hold these bytes.
        if _read_regular(path, "Source blob") != blob:
            raise _integrity("Existing source blob does not match its hash") from None


def _run(
    workspace_path: Path,
    status_key: str,
    validate: Callable[[], None],
    build: Callable[[Workspace], LedgerEvent],
    cid: Callable[[LedgerEvent], str | None],
    blob: bytes | None = None,
) -> tuple[dict, int]:
    result = _base(workspace_path, status_key)
    result.pop("candidates")
    result["candidate"] = None
    try:
        validate()
        updated, line = _append(workspace_path, build, blob)
        event = parse_event(line)
        target = cid(event)
        result.update(
            {status_key: "success"},
            candidate=(
                _view(updated, updated.candidate(target), history=True)
                if target
                else None
            ),
            entry_sha256=sha256(line),
            ledger_head_sha256=updated.ledger_head_sha256,
            summary=_summary(updated),
            lifecycle=_lifecycle(updated),
        )
        return result, 0
    except ReviewError as exc:
        result["error"] = {"kind": exc.kind, "message": str(exc)}
        return result, _exit_code(exc.kind)


def _texts(reason: str, reviewer: str) -> None:
    _text_field(reason, "reason", MAX_REASON)
    _text_field(reviewer, "reviewer", MAX_REVIEWER)


def decide(
    workspace_path: Path,
    cid: str,
    revision: int,
    decision: str,
    reason: str,
    reviewer: str,
) -> tuple[dict, int]:
    def validate() -> None:
        _texts(reason, reviewer)
        if decision not in ("approve", "reject"):
            raise ReviewError("input_error", "decision must be approve or reject")

    def build(workspace: Workspace) -> Decision:
        latest = workspace.candidate(cid)
        envelope = _envelope(workspace, reason, reviewer)
        return Decision(
            seq=envelope["seq"],
            decided_at=envelope["recorded_at"],
            candidate_id=cid,
            revision=revision,
            decision=decision,
            reason=reason,
            reviewer=reviewer,
            candidate_set_sha256=envelope["candidate_set_sha256"],
            source_sha256=latest.source_sha256,
            prev_sha256=envelope["prev_sha256"],
        )

    result, code = _run(
        workspace_path, "decide_status", validate, build, lambda e: e.candidate_id
    )
    if code == 0:
        # T03 decide output showed only the decided revision.
        result["candidate"].pop("history")
    return result, code


def revise(
    workspace_path: Path,
    cid: str,
    from_revision: int,
    reason: str,
    reviewer: str,
    *,
    subject: str | None = None,
    object_: str | None = None,
    section: str | None = None,
    quote: str | None = None,
    source_version: str | None = None,
) -> tuple[dict, int]:
    def build(workspace: Workspace) -> Revised:
        latest = workspace.candidate(cid)
        if from_revision != latest.revision:
            raise ReviewError(
                "input_error",
                f"{cid} is at revision {latest.revision}; revise from the latest revision",
            )
        version = source_version or workspace.current_source.source_version
        record = next(
            (s for s in workspace.sources if s.source_version == version), None
        )
        if record is None:
            raise ReviewError("input_error", f"Unknown source version: {version}")
        try:
            claim = ExtractedCandidate(
                subject=subject or latest.subject,
                predicate=latest.predicate,
                object=object_ or latest.object,
                section=section or latest.section,
                quote=quote or latest.quote,
                review_status="pending",
            )
        except ValidationError as exc:
            first = exc.errors()[0]
            raise ReviewError(
                "input_error",
                f"Invalid revision {'.'.join(map(str, first['loc']))}: {first['type']}",
            ) from exc
        candidate = Candidate(
            candidate_id=cid,
            revision=latest.revision + 1,
            subject=claim.subject,
            predicate=claim.predicate,
            object=claim.object,
            source_id=record.source_id,
            source_version=record.source_version,
            source_sha256=record.sha256,
            section=claim.section,
            quote=claim.quote,
            review_status="pending",
            source_check=check_source(workspace.document(record.sha256), claim),
        )
        return Revised(
            type="revise",
            candidate=candidate,
            content_sha256=content_sha256(candidate),
            **_envelope(workspace, reason, reviewer),
        )

    return _run(
        workspace_path,
        "revise_status",
        lambda: _texts(reason, reviewer),
        build,
        lambda e: e.candidate.candidate_id,
    )


def withdraw(
    workspace_path: Path, cid: str, revision: int, reason: str, reviewer: str
) -> tuple[dict, int]:
    def build(workspace: Workspace) -> Withdrawn:
        workspace.candidate(cid)
        return Withdrawn(
            type="withdraw",
            candidate_id=cid,
            revision=revision,
            **_envelope(workspace, reason, reviewer),
        )

    return _run(
        workspace_path,
        "withdraw_status",
        lambda: _texts(reason, reviewer),
        build,
        lambda e: e.candidate_id,
    )


def add_source(
    workspace_path: Path,
    source_path: Path,
    source_version: str,
    reason: str,
    reviewer: str,
) -> tuple[dict, int]:
    blob = b""

    def validate() -> None:
        nonlocal blob
        _texts(reason, reviewer)
        try:
            with source_path.open("rb") as handle:
                blob = handle.read(MAX_SOURCE_BYTES + 1)
            if not blob or len(blob) > MAX_SOURCE_BYTES:
                raise ReviewError("input_error", "Source must be 1 byte to 1 MiB")
            blob.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ReviewError(
                "input_error", f"Cannot read source: {type(exc).__name__}"
            ) from exc

    def build(workspace: Workspace) -> SourceAdded:
        digest = sha256(blob)
        try:
            record = SourceRecord(
                source_id=workspace.current_source.source_id,
                source_version=source_version,
                sha256=digest,
                byte_length=len(blob),
                blob=f"sources/{digest}.md",
            )
        except ValidationError as exc:
            raise ReviewError(
                "input_error", "source version must match ^[a-z0-9][a-z0-9._-]{0,63}$"
            ) from exc
        return SourceAdded(
            type="source", source=record, **_envelope(workspace, reason, reviewer)
        )

    result = _base(workspace_path, "source_status")
    result.pop("candidates")
    try:
        validate()
    except ReviewError as exc:
        result["error"] = {"kind": exc.kind, "message": str(exc)}
        result["candidate"] = None
        return result, _exit_code(exc.kind)
    result, code = _run(
        workspace_path, "source_status", lambda: None, build, lambda e: None, blob
    )
    if code == 0:
        result.pop("candidate")
        updated = load_workspace(workspace_path)
        result["source"] = updated.current_source.model_dump()
    return result, code


def _safe(value: object) -> str:
    """Neutralize terminal control sequences from untrusted document text."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(value))


def render_text(result: dict) -> str:
    if "error" in result:
        return f"ERROR [{result['error']['kind']}]: {_safe(result['error']['message'])}"
    source = result["current_source"]
    summary = result["summary"]
    lifecycle = result["lifecycle"]
    lines = [
        f"Workspace : {_safe(result['workspace'])}",
        f"Source    : {source['source_id']} v{source['source_version']} "
        f"sha256={source['sha256'][:16]}… (deployment unverified)",
        f"Candidates: {summary['total']} — pending {summary['pending']}, "
        f"approved {summary['approved']}, rejected {summary['rejected']}; "
        f"source check failed {summary['source_check_failed']}",
        f"Lifecycle : versions {', '.join(lifecycle['source_versions'])}; "
        f"revised {lifecycle['revised']}, withdrawn {lifecycle['withdrawn']}, "
        f"stale source {lifecycle['stale_source']}",
        f"Ledger    : head={result['ledger_head_sha256'] or '(empty)'}",
    ]
    detail = len(result["candidates"]) == 1
    for number, c in enumerate(result["candidates"], start=1):
        check = c["source_check"]
        lines += [
            "",
            f"[{number}] {c['candidate_id']} rev {c['revision']}  "
            f"{c['current_status'].upper()}"
            + ("" if c["approvable"] or c["decision"] else "  (approval blocked)")
            + (
                ""
                if c["source_current"]
                else f"  (stale source v{c['source_version']})"
            ),
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
        for h in c.get("history", []) if detail else []:
            d = h["decision"]
            lines.append(
                f"    history  : rev {h['revision']} v{h['source_version']} "
                f"{h['subject']} -> {h['object']} [{h['status']}]"
                + (f" {d['decision']} by {_safe(d['reviewer'])}" if d else "")
            )
    return "\n".join(lines)
