"""T03-A: recorded extraction into app-issued pending candidates with source checks.

Machine checks cover schema, source identity and quote location only. Relation meaning
and direction are left to human review; nothing here approves a candidate.
"""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from .llm import LLMError, parse_events, read_recording, replay_recording
from .schemas import section_bodies, sha256

MAX_SOURCE_BYTES = 1_048_576
MAX_CANDIDATES = 8

Service = Literal["webapi", "answer", "retrieval", "reranker", "suggest"]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Section = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Quote = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
_IDENTIFIER = TypeAdapter(Identifier, config=ConfigDict(strict=True))
CheckKind = Literal[
    "section_missing",
    "section_ambiguous",
    "section_mismatch",
    "quote_missing",
    "self_reference",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ExtractedCandidate(_Strict):
    """One relation as claimed by model output; untrusted until checked."""

    subject: Service
    predicate: Literal["depends_on"]
    object: Service
    section: Section
    quote: Quote
    review_status: Literal["pending"]


class ExtractionPayload(_Strict):
    document_id: str
    candidates: list[ExtractedCandidate] = Field(max_length=MAX_CANDIDATES)


class SourceRecord(_Strict):
    """Source identity assigned by the app; the hash is computed from stored bytes."""

    source_id: Identifier
    source_version: Identifier
    sha256: Sha256
    byte_length: int = Field(ge=1, le=MAX_SOURCE_BYTES)
    blob: str


class SourceCheck(_Strict):
    status: Literal["passed", "failed"]
    kind: CheckKind | None = None
    message: str | None = None


class Candidate(_Strict):
    candidate_id: str
    revision: int = Field(ge=1)
    subject: Service
    predicate: Literal["depends_on"]
    object: Service
    source_id: Identifier
    source_version: Identifier
    source_sha256: Sha256
    section: Section
    quote: Quote
    review_status: Literal["pending"]
    source_check: SourceCheck


class CandidateSet(_Strict):
    schema_version: Literal[1]
    kind: Literal["candidate_set"]
    llm_mode: Literal["recorded_output"]
    provenance: Literal["synthetic_test_double"]
    recording_sha256: Sha256
    source: SourceRecord
    candidates: list[Candidate]
    deployment_status: Literal["unverified"]


def candidate_id(source_sha256: str, claim: ExtractedCandidate) -> str:
    """Content-derived ID: any edit to the claim or its source yields a different ID."""
    identity = {"source_sha256": source_sha256} | claim.model_dump(
        exclude={"review_status"}
    )
    return "c-" + sha256(json.dumps(identity, sort_keys=True).encode())[:16]


def check_source(document: str, claim: ExtractedCandidate) -> SourceCheck:
    if claim.subject == claim.object:
        return SourceCheck(
            status="failed",
            kind="self_reference",
            message="Subject and object are the same service",
        )
    bodies = section_bodies(document, claim.section)
    if not bodies:
        return SourceCheck(
            status="failed",
            kind="section_missing",
            message="Named H2 section does not exist in the source",
        )
    if len(bodies) > 1:
        return SourceCheck(
            status="failed",
            kind="section_ambiguous",
            message="Named H2 section title is not unique in the source",
        )
    if claim.quote in bodies[0]:
        return SourceCheck(status="passed")
    if claim.quote in document:
        return SourceCheck(
            status="failed",
            kind="section_mismatch",
            message="Quote exists in the source but not in the named section",
        )
    return SourceCheck(
        status="failed", kind="quote_missing", message="Quote is absent from the source"
    )


def _payload(raw: object, source_id: str) -> ExtractionPayload:
    try:
        payload = ExtractionPayload.model_validate(raw)
    except ValidationError as exc:
        # Report locations only; model output may echo adversarial document text.
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "<root>"
        raise LLMError(
            "schema_error",
            f"{exc.error_count()} schema violation(s); first at {location}: {first['type']}",
        ) from exc
    if payload.document_id != source_id:
        raise LLMError("schema_error", "Response must match the requested document")
    claims = [c.model_dump_json() for c in payload.candidates]
    if len(set(claims)) != len(claims):
        raise LLMError("schema_error", "Duplicate candidate")
    return payload


def extract(
    source_path: Path,
    source_id: str,
    source_version: str,
    recording: Path,
    workspace: Path,
) -> tuple[dict, int]:
    result = {
        "schema_version": 1,
        "extraction_status": "error",
        "llm_mode": "recorded_output",
        "provenance": "synthetic_test_double",
        "engine_mode": "not_used",
        "workspace": None,
        "source": None,
        "candidates": None,
        "deployment_status": "unverified",
    }
    try:
        if workspace.exists():
            raise LLMError(
                "output_error", "Workspace already exists; choose a new path"
            )
        for value in (source_id, source_version):
            try:
                _IDENTIFIER.validate_python(value)
            except ValidationError as exc:
                raise LLMError(
                    "input_error",
                    "source ID/version must match ^[a-z0-9][a-z0-9._-]{0,63}$",
                ) from exc
        try:
            with source_path.open("rb") as handle:
                document_bytes = handle.read(MAX_SOURCE_BYTES + 1)
            if not document_bytes or len(document_bytes) > MAX_SOURCE_BYTES:
                raise LLMError("input_error", "Source must be 1 byte to 1 MiB")
            document = document_bytes.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise LLMError(
                "input_error", f"Cannot read source: {type(exc).__name__}"
            ) from exc
        source_sha256 = sha256(document_bytes)
        try:
            data = read_recording(recording)
        except OSError as exc:
            raise LLMError(
                "input_error", f"Cannot read recording: {type(exc).__name__}"
            ) from exc
        output = replay_recording(
            data, {"source_id": source_id, "document_sha256": source_sha256}
        )
        raw, events = parse_events(output)
        payload = _payload(raw, source_id)
        source = SourceRecord(
            source_id=source_id,
            source_version=source_version,
            sha256=source_sha256,
            byte_length=len(document_bytes),
            blob=f"sources/{source_sha256}.md",
        )
        candidates = [
            Candidate(
                candidate_id=candidate_id(source_sha256, claim),
                revision=1,
                subject=claim.subject,
                predicate=claim.predicate,
                object=claim.object,
                source_id=source_id,
                source_version=source_version,
                source_sha256=source_sha256,
                section=claim.section,
                quote=claim.quote,
                review_status="pending",
                source_check=check_source(document, claim),
            )
            for claim in payload.candidates
        ]
        candidate_set = CandidateSet(
            schema_version=1,
            kind="candidate_set",
            llm_mode="recorded_output",
            provenance="synthetic_test_double",
            recording_sha256=sha256(data),
            source=source,
            candidates=candidates,
            deployment_status="unverified",
        )
        encoded = (candidate_set.model_dump_json(indent=2) + "\n").encode()
        try:
            workspace.mkdir(parents=True, exist_ok=False)
            (workspace / "sources").mkdir()
            with (workspace / source.blob).open("xb") as blob:
                blob.write(document_bytes)
            with (workspace / "candidates.json").open("xb") as handle:
                handle.write(encoded)
        except OSError as exc:
            raise LLMError(
                "output_error", f"Cannot write workspace: {type(exc).__name__}"
            ) from exc
        failed = sum(c.source_check.status == "failed" for c in candidates)
        result.update(
            extraction_status="success" if candidates else "empty",
            workspace=str(workspace),
            candidate_set_sha256=sha256(encoded),
            source=source.model_dump(),
            candidates=[c.model_dump() for c in candidates],
            summary={
                "total": len(candidates),
                "source_check_passed": len(candidates) - failed,
                "source_check_failed": failed,
            },
            audit=events,
            limitations=[
                "Recorded synthetic test double; no model was called.",
                "Source checks verify quote location only, not relation meaning or direction.",
                "All candidates are pending; approval requires human review.",
            ],
        )
        return result, 0
    except LLMError as exc:
        result["error"] = {"kind": exc.kind, "message": str(exc)}
    return result, {"input_error": 2, "recording_error": 2, "output_error": 4}.get(
        result["error"]["kind"], 3
    )
