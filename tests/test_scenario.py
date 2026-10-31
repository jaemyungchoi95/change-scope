"""T07 seam: reviewed change proposals, scenario snapshots and comparison.

No LLM/network calls; PyreWire runs for real.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from change_scope.cli import main

ROOT = Path(__file__).resolve().parents[1]
S1 = ROOT / "data/fixtures/s1/snapshot.json"
ALTERNATE = ROOT / "data/fixtures/t05/alternate/snapshot.json"
GOLDEN_V1 = ROOT / "data/fixtures/t03/reviewed-snapshot-v1/snapshot.json"
T07 = ROOT / "data/fixtures/t07"
CATALOG = ROOT / "data/fixtures/t06/catalog.json"


def cli(capsys, *args):
    code = main(list(map(str, args)))
    return code, json.loads(capsys.readouterr().out)


def ok(result):
    code, out = result
    assert code == 0, out
    return out


def init(capsys, workspace, proposals=T07 / "proposals.json", base=S1):
    return cli(
        capsys, "proposal", "init", "--proposals", proposals, "--base", base,
        "--workspace", workspace,
    )  # fmt: skip


def decide(capsys, workspace, pid, decision):
    return cli(
        capsys, "proposal", "decide", "--workspace", workspace, "--id", pid,
        "--revision", 1, "--decision", decision, "--reviewer", "tester",
        "--reason", "checked against the change request",
    )  # fmt: skip


def build(capsys, workspace, output, base=S1):
    return cli(
        capsys, "scenario", "build", "--workspace", workspace, "--base", base,
        "--output", output,
    )  # fmt: skip


def analyze(capsys, snapshot, target="reranker"):
    return cli(capsys, "analyze", "--snapshot", snapshot, "--target", target)


@pytest.fixture
def workspace(capsys, tmp_path):
    path = tmp_path / "proposals"
    ok(init(capsys, path))
    return path


@pytest.fixture
def s2(capsys, workspace, tmp_path):
    ok(decide(capsys, workspace, "P-001", "approve"))
    ok(decide(capsys, workspace, "P-002", "reject"))
    ok(build(capsys, workspace, tmp_path / "s2"))
    return tmp_path / "s2/scenario.json"


def test_pending_proposals_never_change_the_base(capsys, workspace, tmp_path):
    before = ok(analyze(capsys, S1))
    code, out = build(capsys, workspace, tmp_path / "s2")
    assert code == 2 and out["error"]["kind"] == "nothing_approved"
    assert not (tmp_path / "s2").exists()
    after = ok(analyze(capsys, S1))
    assert after["upstream_candidates"] == before["upstream_candidates"]
    assert after["snapshot_hash"] == before["snapshot_hash"]


def test_s2_compare_story(capsys, s2, tmp_path):
    result = ok(analyze(capsys, s2))
    assert result["upstream_candidates"] == ["suggest", "webapi"]
    assert result["snapshot_kind"] == "scenario"
    assert result["purpose"] == "proposed"
    assert result["scenario"]["overrides"] == {
        "added": [],
        "removed": [["retrieval", "reranker"]],
    }
    assert result["scenario"]["not_applied"] == [
        {"proposal_id": "P-002", "status": "rejected"}
    ]
    report = tmp_path / "compare.md"
    out = ok(
        cli(
            capsys,
            "compare",
            "--base",
            S1,
            "--scenario",
            s2,
            "--target",
            "reranker",
            "--evidence",
            CATALOG,
            "--report",
            report,
        )  # fmt: skip
    )
    c = out["comparison"]
    assert c["base"]["upstream_candidates"] == [
        "answer",
        "retrieval",
        "suggest",
        "webapi",
    ]
    assert c["scenario"]["upstream_candidates"] == ["suggest", "webapi"]
    assert c["diff"] == {
        "removed": ["answer", "retrieval"],
        "added": [],
        "kept": ["suggest", "webapi"],
    }
    # webapi keeps its alternative path through suggest.
    assert c["path_changes"] == {
        "webapi": {
            "base": [
                ["webapi", "answer", "retrieval", "reranker"],
                ["webapi", "suggest", "reranker"],
            ],
            "scenario": [["webapi", "suggest", "reranker"]],
        }
    }
    assert [a["proposal_id"] for a in c["overrides"]] == ["P-001"]
    assert out["deployment_status"] == "unverified"
    text = report.read_text()
    assert "- removed: answer, retrieval" in text
    assert (
        '- P-001 remove retrieval -> reranker (approved by tester): "Remove the call'
        in text
    )
    assert "- suggest: (no linked tests: unverified)" in text
    code, again = cli(
        capsys, "compare", "--base", S1, "--scenario", s2, "--target", "reranker",
        "--report", report,
    )  # fmt: skip
    assert code == 4 and again["error"]["kind"] == "output_error"


def test_added_relation_cites_the_proposal(capsys, workspace, tmp_path):
    ok(decide(capsys, workspace, "P-001", "approve"))
    ok(decide(capsys, workspace, "P-002", "approve"))
    ok(build(capsys, workspace, tmp_path / "s2"))
    result = ok(analyze(capsys, tmp_path / "s2/scenario.json"))
    # answer reaches reranker again, now only through the proposed answer -> suggest.
    assert result["upstream_candidates"] == ["answer", "suggest", "webapi"]
    (path,) = result["witnesses"]["answer"]
    assert path["path"] == ["answer", "suggest", "reranker"]
    (support,) = path["hops"][0]["support"]
    assert (support["fact_id"], support["evidence_kind"], support["source_id"]) == (
        "P-002",
        "proposal",
        "change-request",
    )
    assert "evidence_kind" not in path["hops"][1]["support"][0]


def _proposals_copy(tmp_path, change):
    copy = tmp_path / "input"
    shutil.copytree(T07, copy)
    data = json.loads((copy / "proposals.json").read_text())
    change(data)
    (copy / "proposals.json").write_text(json.dumps(data, indent=2))
    return copy / "proposals.json"


def _p(data, pid):
    return next(p for p in data["proposals"] if p["proposal_id"] == pid)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda d: d.update(base_snapshot_hash="0" * 64), "another base"),
        (lambda d: _p(d, "P-001").update(subject="webapi"), "cannot remove"),
        (lambda d: _p(d, "P-002").update(object="retrieval"), "cannot add"),
        (
            lambda d: _p(d, "P-002").update(
                operation="remove", subject="retrieval", object="reranker"
            ),
            "conflicts with P-001",
        ),
        (lambda d: _p(d, "P-001").update(quote="Delete everything"), "quote missing"),
        (lambda d: _p(d, "P-001").update(review_status="approved"), "Invalid proposal"),
        (lambda d: d["proposals"].append(dict(d["proposals"][0])), "Duplicate"),
    ],
)
def test_invalid_proposals_are_refused(capsys, tmp_path, change, message):
    proposals = _proposals_copy(tmp_path, change)
    code, out = init(capsys, tmp_path / "ws", proposals)
    assert code == 2 and out["error"]["kind"] == "input_error"
    assert message in out["error"]["message"]
    assert not (tmp_path / "ws").exists()


def test_proposals_are_not_reapplied_to_another_base(capsys, workspace, tmp_path):
    code, out = init(capsys, tmp_path / "other", base=ALTERNATE)
    assert code == 2 and "another base" in out["error"]["message"]
    ok(decide(capsys, workspace, "P-001", "approve"))
    code, out = build(capsys, workspace, tmp_path / "s2", base=ALTERNATE)
    assert code == 2 and "another base" in out["error"]["message"]


def test_proposal_decision_rules(capsys, workspace):
    ok(decide(capsys, workspace, "P-001", "approve"))
    code, out = decide(capsys, workspace, "P-001", "reject")
    assert code == 2 and out["error"]["kind"] == "already_decided"
    code, out = decide(capsys, workspace, "P-999", "approve")
    assert code == 2 and out["error"]["kind"] == "input_error"
    code, out = init(capsys, workspace)
    assert code == 4 and out["error"]["kind"] == "output_error"


def _edit_json(path, change):
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data, indent=2))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda s: _edit_json(s, lambda d: d["edges"].append(["answer", "webapi"])),
        lambda s: _edit_json(s, lambda d: d["overrides"].update(removed=[])),
        lambda s: _edit_json(s, lambda d: d.update(not_applied=[])),
        lambda s: _edit_json(
            s.parent / "base/snapshot.json", lambda d: d.update(snapshot_id="S9")
        ),
        lambda s: (s.parent / "proposals/ledger.jsonl").write_bytes(
            (s.parent / "proposals/ledger.jsonl").read_bytes().split(b"\n", 1)[1]
        ),
        lambda s: (s.parent / "proposals/change-request.md").write_text("edited\n"),
    ],
    ids=[
        "edges",
        "overrides",
        "not-applied",
        "base-copy",
        "ledger-line",
        "change-request",
    ],
)
def test_edited_scenario_is_refused(capsys, s2, tamper):
    tamper(s2)
    code, out = analyze(capsys, s2)
    assert code == 2 and out["error"]["kind"] == "input_error"
    assert out["upstream_candidates"] is None


def test_decision_appended_to_scenario_copy_is_refused(capsys, s2):
    # The copied proposal workspace accepts a decision, but the manifest no longer matches.
    proposals = s2.parent / "proposals"
    (proposals / "ledger.jsonl").write_bytes(
        (proposals / "ledger.jsonl").read_bytes().split(b"\n")[0] + b"\n"
    )
    ok(decide(capsys, proposals, "P-002", "approve"))
    code, out = analyze(capsys, s2)
    assert code == 2
    assert "does not match" in out["error"]["message"]


def test_scenario_survives_removal_of_the_original_base(capsys, tmp_path):
    base_dir = tmp_path / "s1"
    shutil.copytree(S1.parent, base_dir)
    ws = tmp_path / "ws"
    ok(init(capsys, ws, base=base_dir / "snapshot.json"))
    ok(decide(capsys, ws, "P-001", "approve"))
    ok(build(capsys, ws, tmp_path / "s2", base=base_dir / "snapshot.json"))
    shutil.rmtree(base_dir)
    assert ok(analyze(capsys, tmp_path / "s2/scenario.json"))[
        "upstream_candidates"
    ] == [
        "suggest",
        "webapi",
    ]


def test_compare_refuses_a_scenario_of_another_base(capsys, s2):
    code, out = cli(
        capsys, "compare", "--base", ALTERNATE, "--scenario", s2, "--target", "reranker"
    )
    assert code == 2 and "another base" in out["error"]["message"]
    code, out = cli(
        capsys, "compare", "--base", S1, "--scenario", S1, "--target", "reranker"
    )
    assert code == 2 and "not a scenario" in out["error"]["message"]


def test_reviewed_snapshot_can_be_a_base(capsys, tmp_path):
    digest = hashlib.sha256(GOLDEN_V1.read_bytes()).hexdigest()
    snapshot_id = json.loads(GOLDEN_V1.read_text())["snapshot_id"]
    proposals = _proposals_copy(
        tmp_path,
        lambda d: d.update(base_snapshot_id=snapshot_id, base_snapshot_hash=digest),
    )
    ws = tmp_path / "ws"
    ok(init(capsys, ws, proposals, base=GOLDEN_V1))
    ok(decide(capsys, ws, "P-001", "approve"))
    ok(build(capsys, ws, tmp_path / "s2", base=GOLDEN_V1))
    result = ok(analyze(capsys, tmp_path / "s2/scenario.json"))
    assert result["upstream_candidates"] == ["suggest", "webapi"]
    assert result["scenario"]["base_kind"] == "reviewed"
