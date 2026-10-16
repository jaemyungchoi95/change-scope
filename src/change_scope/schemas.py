"""T01's explicitly reviewed fixture input, not a review/export workflow."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


class InputError(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    snapshot_hash: str
    purpose: str
    services: frozenset[str]
    edges: tuple[tuple[str, str], ...]
    assertions: tuple[dict, ...]
    sources: tuple[dict, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InputError(message)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def load_snapshot(path: Path) -> Snapshot:
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise InputError(f"Cannot read snapshot: {exc}") from exc
    _require(isinstance(data, dict), "Snapshot must be a JSON object")
    _require(
        type(data.get("schema_version")) is int and data["schema_version"] == 1,
        "Unsupported snapshot schema_version",
    )
    _require(
        data.get("fixture") is True, "T01 accepts explicitly reviewed fixtures only"
    )
    _require(_text(data.get("snapshot_id")), "Missing snapshot_id")
    _require(
        data.get("purpose") in ("baseline", "proposed"), "Invalid snapshot purpose"
    )
    _require(
        data.get("deployment_status") == "unverified", "Deployment must be unverified"
    )
    services = data.get("services")
    _require(
        isinstance(services, list)
        and bool(services)
        and all(_text(s) for s in services),
        "Invalid service IDs",
    )
    _require(len(set(services)) == len(services), "Duplicate service IDs")
    sources = data.get("sources")
    _require(isinstance(sources, list), "sources must be a list")
    documents = {}
    for source in sources:
        _require(isinstance(source, dict), "Invalid source")
        _require(
            all(
                _text(source.get(k))
                for k in ("source_id", "source_version", "path", "sha256")
            ),
            "Invalid source metadata",
        )
        key = (source["source_id"], source["source_version"])
        _require(key not in documents, "Duplicate source/version")
        try:
            source_path = (path.parent / source["path"]).resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            raise InputError(f"Cannot resolve source path: {exc}") from exc
        _require(
            not Path(source["path"]).is_absolute()
            and source_path.is_relative_to(path.parent.resolve()),
            "Source escapes fixture directory",
        )
        try:
            content = source_path.read_bytes()
            _require(sha256(content) == source["sha256"], "Source hash mismatch")
            documents[key] = content.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise InputError(f"Cannot read source: {exc}") from exc
    assertions = data.get("assertions")
    _require(isinstance(assertions, list), "assertions must be a list")
    seen_ids = set()
    edges = set()
    for fact in assertions:
        _require(isinstance(fact, dict), "Invalid assertion")
        _require(
            all(
                _text(fact.get(k))
                for k in (
                    "assertion_id",
                    "subject",
                    "predicate",
                    "object",
                    "source_id",
                    "source_version",
                    "section",
                    "quote",
                    "review_status",
                )
            ),
            "Invalid assertion fields",
        )
        _require(
            type(fact.get("revision")) is int and fact["revision"] > 0,
            "Invalid revision",
        )
        identity = (fact["assertion_id"], fact["revision"])
        _require(identity not in seen_ids, "Duplicate assertion revision")
        seen_ids.add(identity)
        _require(
            fact["review_status"] == "approved", "Fixture contains unapproved assertion"
        )
        _require(fact["predicate"] == "depends_on", "Unsupported fixture predicate")
        _require(
            fact["subject"] in services and fact["object"] in services,
            "Assertion references unknown service",
        )
        document = documents.get((fact["source_id"], fact["source_version"]))
        _require(document is not None, "Assertion references unknown source/version")
        # T01 synthetic documents use unique H2 sections. Full candidate review is T03.
        sections = re.split(r"^## (.+)\r?$", document, flags=re.MULTILINE)
        matches = [
            sections[i + 1]
            for i in range(1, len(sections), 2)
            if sections[i] == fact["section"]
        ]
        _require(
            len(matches) == 1 and fact["quote"] in matches[0],
            "Quote missing from unique source section",
        )
        edges.add((fact["subject"], fact["object"]))
    return Snapshot(
        data["snapshot_id"],
        sha256(raw),
        data["purpose"],
        frozenset(services),
        tuple(sorted(edges)),
        tuple(assertions),
        tuple(sources),
    )
