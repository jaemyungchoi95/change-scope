"""T09: holdout questions and extraction metrics against a hand-transcribed gold model.

The holdout corpus and its answers were fixed before development and never used in
tests. Question runs use the real engine on any analyzable snapshot; `remove` edges
are dropped in memory for that question only. Extraction metrics compare a review
workspace's latest candidates with the gold relations; they do not approve anything.
"""

import json
from importlib.metadata import version
from pathlib import Path

from .engine import EngineError, rules_hash, upstream_candidates
from .schemas import InputError, sha256
from .witness import WitnessError, explain


def _questions(path: Path) -> dict:
    try:
        data = json.loads(path.read_bytes())
        source = data["source"]
        corpus = (path.parent / source["path"]).read_bytes()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InputError(
            f"Cannot read holdout questions: {type(exc).__name__}"
        ) from exc
    if sha256(corpus) != source["sha256"]:
        raise InputError(
            "Holdout corpus does not match the hash fixed with its questions"
        )
    if not isinstance(data.get("questions"), list) or not data["questions"]:
        raise InputError("Holdout questions are missing")
    return data


def run_questions(questions: dict, snapshot, timeout: float) -> list[dict]:
    rows = []
    for q in questions["questions"]:
        remove = {tuple(edge) for edge in q.get("remove", [])}
        if not remove <= set(snapshot.edges):
            raise InputError(f"{q['id']}: remove names a relation the snapshot lacks")
        edges = tuple(e for e in snapshot.edges if e not in remove)
        view = snapshot.__class__(
            snapshot.snapshot_id,
            snapshot.snapshot_hash,
            snapshot.purpose,
            snapshot.services,
            edges,
            tuple(
                a
                for a in snapshot.assertions
                if (a["subject"], a["object"]) not in remove
            ),
            snapshot.sources,
        )
        actual = upstream_candidates(edges, q["target"], snapshot.services, timeout)
        _, check = explain(view, q["target"], actual)
        expected = sorted(q["expected_upstream_candidates"])
        rows.append(
            {
                "id": q["id"],
                "question": q["question"],
                "target": q["target"],
                "remove": [list(e) for e in sorted(remove)],
                "expected": expected,
                "actual": actual,
                "passed": actual == expected,
                "witness_check": check["status"],
            }
        )
    return rows


def extraction_metrics(workspace: Path, gold) -> dict:
    from .review import ReviewError, load_workspace

    try:
        ws = load_workspace(workspace)
    except ReviewError as exc:
        raise InputError(f"Workspace failed verification ({exc.kind}): {exc}") from exc
    candidates = ws.latest_candidates()
    gold_edges = set(gold.edges)

    def score(items) -> dict:
        found = {(c.subject, c.object) for c in items}
        tp = len(found & gold_edges)
        return {
            "candidates": len(items),
            "unique_relations": len(found),
            "true_positive": tp,
            "false_positive": sorted(map(list, found - gold_edges)),
            "missed": sorted(map(list, gold_edges - found)),
            "precision": round(tp / len(found), 3) if found else None,
            "recall": round(tp / len(gold_edges), 3) if gold_edges else None,
        }

    return {
        "workspace": str(workspace),
        "llm_mode": ws.candidate_set.llm_mode,
        "provenance": ws.candidate_set.provenance,
        "candidate_set_sha256": ws.candidate_set_sha256,
        "all_candidates": score(candidates),
        "source_check_passed": score(
            [c for c in candidates if c.source_check.status == "passed"]
        ),
        "gold_relations": len(gold_edges),
    }


def evaluate(
    questions_path: Path,
    snapshot_path: Path,
    gold_path: Path | None,
    workspace: Path | None,
    timeout: float,
) -> tuple[dict, int]:
    from .reviewed import load_for_analysis

    result = {
        "schema_version": 1,
        "evaluation_status": "error",
        "llm_mode": "not_used",
        "engine_mode": "live_engine",
        "questions": None,
        "deployment_status": "unverified",
    }
    try:
        if (gold_path is None) != (workspace is None):
            raise InputError("--gold and --workspace go together")
        questions = _questions(questions_path)
        snapshot, extras = load_for_analysis(snapshot_path)
        rows = run_questions(questions, snapshot, timeout)
        result.update(
            evaluation_status="success",
            evaluation_id=questions.get("evaluation_id"),
            questions_sha256=sha256(questions_path.read_bytes()),
            corpus_sha256=questions["source"]["sha256"],
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_hash,
            snapshot_kind=extras.get("snapshot_kind", "fixture"),
            rules_hash=rules_hash(),
            engine={"name": "pyrewire", "version": version("pyrewire")},
            questions=rows,
            summary={
                "passed": sum(r["passed"] for r in rows),
                "total": len(rows),
            },
        )
        if gold_path is not None:
            gold, _ = load_for_analysis(gold_path)
            result["extraction"] = extraction_metrics(workspace, gold)
        result["limitations"] = [
            "Gold relations were transcribed by hand from the holdout corpus; "
            "they are not independent annotations.",
            "Retrieval-only comparison and alias handling (evaluation types 9-10) "
            "were not run.",
        ]
        return result, 0
    except InputError as exc:
        result["error"] = {"kind": "input_error", "message": str(exc)}
        return result, 2
    except (EngineError, WitnessError, OSError) as exc:
        result["error"] = {"kind": "engine_error", "message": str(exc)}
        return result, 3
