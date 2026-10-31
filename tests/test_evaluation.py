"""T09 seam: holdout-style questions and extraction metrics; no LLM/network calls.

Synthetic S1 questions stand in for the holdout so tests never tune on it.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from change_scope.cli import main
from change_scope.extraction import extract

ROOT = Path(__file__).resolve().parents[1]
S1 = ROOT / "data/fixtures/s1/snapshot.json"


def questions(tmp_path, items, corpus=ROOT / "data/fixtures/s1/topology.md"):
    shutil.copy(corpus, tmp_path / "topology.md")
    path = tmp_path / "questions.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evaluation_id": "synthetic",
                "source": {
                    "path": "topology.md",
                    "sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(),
                },
                "questions": items,
            }
        )
    )
    return path


def q(qid, target, expected, remove=()):
    return {
        "id": qid,
        "question": qid,
        "target": target,
        "remove": [list(e) for e in remove],
        "expected_upstream_candidates": expected,
    }


def evaluate(capsys, *args):
    code = main(["evaluate", *map(str, args)])
    return code, json.loads(capsys.readouterr().out)


def test_questions_pass_fail_and_removal(capsys, tmp_path):
    path = questions(
        tmp_path,
        [
            q("Q1", "reranker", ["answer", "retrieval", "suggest", "webapi"]),
            q("Q2", "reranker", ["suggest", "webapi"], [("retrieval", "reranker")]),
            q("Q3", "webapi", []),
            q("Q4", "reranker", ["webapi"]),  # deliberately wrong expectation
        ],
    )
    code, result = evaluate(capsys, "--questions", path, "--snapshot", S1)
    assert code == 0, result
    assert [(r["id"], r["passed"]) for r in result["questions"]] == [
        ("Q1", True),
        ("Q2", True),
        ("Q3", True),
        ("Q4", False),
    ]
    assert result["summary"] == {"passed": 3, "total": 4}
    assert {r["witness_check"] for r in result["questions"]} == {"consistent"}
    assert result["rules_hash"] and result["engine"]["version"]


def test_corpus_hash_and_bad_removal_are_input_errors(capsys, tmp_path):
    path = questions(tmp_path, [q("Q1", "reranker", [], [("webapi", "reranker")])])
    code, result = evaluate(capsys, "--questions", path, "--snapshot", S1)
    assert code == 2 and "lacks" in result["error"]["message"]
    (tmp_path / "topology.md").write_text("edited\n")
    code, result = evaluate(capsys, "--questions", path, "--snapshot", S1)
    assert code == 2 and "hash" in result["error"]["message"]


def test_extraction_metrics_count_false_positives(capsys, tmp_path):
    workspace = tmp_path / "ws"
    _, code = extract(
        ROOT / "data/fixtures/t03/topology.md",
        "topology-review",
        "1",
        ROOT / "data/fixtures/t03/topology-recorded.json",
        workspace,
    )
    assert code == 0
    path = questions(tmp_path, [q("Q1", "webapi", [])])
    code, result = evaluate(
        capsys,
        "--questions",
        path,
        "--snapshot",
        S1,
        "--gold",
        S1,
        "--workspace",
        workspace,
    )
    assert code == 0, result
    metrics = result["extraction"]
    assert metrics["llm_mode"] == "recorded_output"
    assert metrics["all_candidates"] | {} == {
        "candidates": 8,
        "unique_relations": 7,
        "true_positive": 5,
        "false_positive": [["answer", "webapi"], ["reranker", "webapi"]],
        "missed": [],
        "precision": 0.714,
        "recall": 1.0,
    }
    assert metrics["source_check_passed"]["false_positive"] == [["answer", "webapi"]]
    assert metrics["source_check_passed"]["precision"] == 0.833


@pytest.mark.parametrize("args", [["--gold", str(S1)], ["--workspace", "x"]])
def test_gold_and_workspace_go_together(capsys, tmp_path, args):
    path = questions(tmp_path, [q("Q1", "webapi", [])])
    code, result = evaluate(capsys, "--questions", path, "--snapshot", S1, *args)
    assert code == 2 and result["error"]["kind"] == "input_error"
