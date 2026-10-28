"""T05 seam: witness paths attached to real PyreWire candidates; no LLM/network."""

import json
from pathlib import Path

import pytest

from change_scope import cli as cli_module
from change_scope.cli import main
from change_scope.extraction import extract

ROOT = Path(__file__).resolve().parents[1]
S1 = ROOT / "data/fixtures/s1/snapshot.json"
CYCLE = ROOT / "data/fixtures/t05/cycle/snapshot.json"
ALTERNATE = ROOT / "data/fixtures/t05/alternate/snapshot.json"
GOLDEN_V1 = ROOT / "data/fixtures/t03/reviewed-snapshot-v1/snapshot.json"
# Pinned: T01a's Wirelog CLI parity receipts were produced with this rule file.
RULES_HASH = "daacbd202f86b6ca140f19a9e0066141f741dc60383f1b40b077bd9c941126f0"


def cli(capsys, *args):
    code = main(list(map(str, args)))
    return code, json.loads(capsys.readouterr().out)


def analyze(capsys, snapshot, target):
    code, result = cli(capsys, "analyze", "--snapshot", snapshot, "--target", target)
    return code, result


def paths(result):
    return {
        service: [(w["via"], w["path"]) for w in ws]
        for service, ws in result["witnesses"].items()
    }


def hop_ids(witness):
    return [[s["fact_id"] for s in hop["support"]] for hop in witness["hops"]]


def test_s1_direct_indirect_and_alternative_paths(capsys):
    code, result = analyze(capsys, S1, "reranker")
    assert code == 0, result
    assert result["rules_hash"] == RULES_HASH
    assert paths(result) == {
        "answer": [("retrieval", ["answer", "retrieval", "reranker"])],
        "retrieval": [("reranker", ["retrieval", "reranker"])],
        "suggest": [("reranker", ["suggest", "reranker"])],
        "webapi": [
            ("answer", ["webapi", "answer", "retrieval", "reranker"]),
            ("suggest", ["webapi", "suggest", "reranker"]),
        ],
    }
    assert result["witness_check"]["status"] == "consistent"
    assert result["witness_check"]["witnessed"] == result["upstream_candidates"]
    (answer,) = result["witnesses"]["answer"]
    # Indirect impact is explained as a path, never stored as a direct fact.
    assert hop_ids(answer) == [["F02"], ["F03"]]
    assert ("answer", "reranker") not in {
        (f["subject"], f["object"]) for f in result["facts"]
    }
    (hop,) = result["witnesses"]["retrieval"][0]["hops"]
    assert hop["support"] == [
        {
            "fact_id": "F03",
            "revision": 1,
            "source_id": "topology",
            "source_version": "1",
            "source_sha256": result["sources"][0]["sha256"],
            "section": "HTTP calls",
            "quote": "retrieval calls reranker over HTTP.",
        }
    ]


def test_empty_result_has_no_witnesses(capsys):
    code, result = analyze(capsys, S1, "webapi")
    assert code == 0
    assert result["analysis_status"] == "empty"
    assert result["witnesses"] == {}
    assert result["witness_check"]["status"] == "consistent"


def test_cycle_terminates_and_excludes_target(capsys):
    code, result = analyze(capsys, CYCLE, "answer")
    assert code == 0, result
    assert result["upstream_candidates"] == ["retrieval", "webapi"]
    assert paths(result) == {
        "retrieval": [("answer", ["retrieval", "answer"])],
        "webapi": [("answer", ["webapi", "answer"])],
    }
    (back,) = result["witnesses"]["retrieval"]
    assert back["hops"][0]["support"][0]["section"] == "Callbacks"


def test_cycle_paths_never_revisit_the_candidate(capsys):
    code, result = analyze(capsys, CYCLE, "reranker")
    assert code == 0, result
    # retrieval -> answer -> retrieval -> reranker would revisit retrieval.
    assert paths(result)["retrieval"] == [("reranker", ["retrieval", "reranker"])]
    for witnesses in result["witnesses"].values():
        for w in witnesses:
            assert len(set(w["path"])) == len(w["path"])
            assert w["path"][-1] == "reranker"
            assert "reranker" not in w["path"][:-1]


def test_alternative_path_survives_a_removed_edge(capsys):
    code, result = analyze(capsys, ALTERNATE, "reranker")
    assert code == 0, result
    assert result["upstream_candidates"] == ["suggest", "webapi"]
    assert paths(result) == {
        "suggest": [("reranker", ["suggest", "reranker"])],
        "webapi": [("suggest", ["webapi", "suggest", "reranker"])],
    }


def test_engine_witness_mismatch_is_an_error(capsys, monkeypatch):
    real = cli_module.upstream_candidates
    monkeypatch.setattr(
        cli_module,
        "upstream_candidates",
        lambda *args: [s for s in real(*args) if s != "suggest"],
    )
    code, result = analyze(capsys, S1, "reranker")
    assert code == 3
    assert result["analysis_status"] == "error"
    assert result["error"]["kind"] == "witness_mismatch"
    assert result["upstream_candidates"] is None
    assert "witnesses" not in result


def test_reviewed_v1_snapshot_witnesses_cite_candidates(capsys):
    code, result = analyze(capsys, GOLDEN_V1, "reranker")
    assert code == 0, result
    (hop,) = result["witnesses"]["suggest"][0]["hops"]
    (support,) = hop["support"]
    assert support["fact_id"] == "c-c3e0780f7d7dbc49"
    assert support["source_id"] == "topology-review"
    assert support["source_sha256"] == result["sources"][0]["sha256"]


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "review"
    result, code = extract(
        ROOT / "data/fixtures/t03/topology.md",
        "topology-review",
        "1",
        ROOT / "data/fixtures/t03/topology-recorded.json",
        path,
    )
    assert code == 0, result
    return path


def _review(capsys, command, workspace, *args):
    code, out = cli(
        capsys,
        "review",
        command,
        "--workspace",
        workspace,
        *args,
        "--reviewer",
        "tester",
        "--reason",
        "checked against source",
    )
    assert code == 0, out
    return out


def test_witness_hops_list_every_effective_source(capsys, workspace, tmp_path):
    data = json.loads((workspace / "candidates.json").read_text())
    cid = {
        (c["subject"], c["object"]): c["candidate_id"]
        for c in data["candidates"]
        if c["section"] == "HTTP calls"
    }
    for pair in [
        ("webapi", "answer"),
        ("answer", "retrieval"),
        ("retrieval", "reranker"),
        ("webapi", "suggest"),
        ("suggest", "reranker"),
    ]:
        _review(capsys, "decide", workspace, "--id", cid[pair], "--revision", 1,
                "--decision", "approve")  # fmt: skip
    # Turn the reversed candidate into a second source for webapi -> answer.
    reversed_id = cid[("answer", "webapi")]
    _review(capsys, "revise", workspace, "--id", reversed_id, "--from-revision", 1,
            "--subject", "webapi", "--object", "answer")  # fmt: skip
    _review(capsys, "decide", workspace, "--id", reversed_id, "--revision", 2,
            "--decision", "approve")  # fmt: skip
    cli(capsys, "export", "--workspace", workspace, "--output", tmp_path / "both")
    code, result = analyze(capsys, tmp_path / "both/snapshot.json", "reranker")
    assert code == 0, result
    via_answer = result["witnesses"]["webapi"][0]
    assert via_answer["via"] == "answer"
    first = via_answer["hops"][0]["support"]
    assert sorted((s["fact_id"], s["revision"]) for s in first) == sorted(
        [(cid[("webapi", "answer")], 1), (reversed_id, 2)]
    )

    _review(capsys, "withdraw", workspace, "--id", cid[("webapi", "answer")],
            "--revision", 1)  # fmt: skip
    cli(capsys, "export", "--workspace", workspace, "--output", tmp_path / "one")
    code, result = analyze(capsys, tmp_path / "one/snapshot.json", "reranker")
    assert code == 0, result
    first = result["witnesses"]["webapi"][0]["hops"][0]["support"]
    assert [(s["fact_id"], s["revision"]) for s in first] == [(reversed_id, 2)]
