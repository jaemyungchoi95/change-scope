"""T03-C seam: export approved candidates and analyze them with the real engine.

No LLM/network calls; PyreWire runs for real.
"""

import json
import os
from pathlib import Path

import pytest

from change_scope.cli import main
from change_scope.extraction import extract

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/fixtures/t03/topology.md"
RECORDING = ROOT / "data/fixtures/t03/topology-recorded.json"
CORRECT = [
    ("webapi", "answer", "HTTP calls"),
    ("answer", "retrieval", "HTTP calls"),
    ("retrieval", "reranker", "HTTP calls"),
    ("webapi", "suggest", "HTTP calls"),
    ("suggest", "reranker", "HTTP calls"),
]
REVERSED = ("answer", "webapi", "HTTP calls")
FAKE_QUOTE = ("reranker", "webapi", "HTTP calls")
WRONG_SECTION = ("retrieval", "reranker", "Operations")
RERANKER_UPSTREAM = ["answer", "retrieval", "suggest", "webapi"]


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "review"
    result, code = extract(SOURCE, "topology-review", "1", RECORDING, path)
    assert code == 0, result
    return path


def ids(workspace):
    data = json.loads((workspace / "candidates.json").read_text())
    return {
        (c["subject"], c["object"], c["section"]): c["candidate_id"]
        for c in data["candidates"]
    }


def cli(capsys, *args):
    code = main(list(map(str, args)))
    return code, json.loads(capsys.readouterr().out)


def decide(capsys, workspace, key, decision):
    code, out = cli(
        capsys,
        "review",
        "decide",
        "--workspace",
        workspace,
        "--id",
        ids(workspace)[key],
        "--revision",
        1,
        "--decision",
        decision,
        "--reviewer",
        "tester",
        "--reason",
        "checked against source",
    )
    assert code == 0, out


def export(capsys, workspace, output):
    return cli(capsys, "export", "--workspace", workspace, "--output", output)


def analyze(capsys, snapshot, target):
    return cli(capsys, "analyze", "--snapshot", snapshot, "--target", target)


def reviewed(capsys, workspace, *, reject=(REVERSED, FAKE_QUOTE, WRONG_SECTION)):
    for key in CORRECT:
        decide(capsys, workspace, key, "approve")
    for key in reject:
        decide(capsys, workspace, key, "reject")
    return workspace


@pytest.fixture
def snapshot(capsys, workspace, tmp_path):
    reviewed(capsys, workspace)
    code, out = export(capsys, workspace, tmp_path / "snap")
    assert code == 0, out
    return tmp_path / "snap" / "snapshot.json"


def test_full_review_exports_only_approved_and_analyzes(capsys, workspace, tmp_path):
    reviewed(capsys, workspace)
    output = tmp_path / "snap"
    code, out = export(capsys, workspace, output)
    assert code == 0, out
    assert out["export_status"] == "success"
    assert out["summary"] == {
        "total": 8,
        "approved": 5,
        "pending": 0,
        "rejected": 3,
        "approval_blocked": 0,
    }
    assert sorted((f["subject"], f["object"]) for f in out["approved"]) == sorted(
        (s, o) for s, o, _ in CORRECT
    )
    assert all(f["reviewer"] == "tester" for f in out["approved"])
    assert {e["status"] for e in out["excluded"]} == {"rejected"}
    assert out["deployment_status"] == "unverified"
    files = sorted(p.relative_to(output).as_posix() for p in output.rglob("*"))
    source = json.loads((workspace / "candidates.json").read_text())["source"]
    blob = source["blob"]
    assert files == sorted(
        ["candidates.json", "ledger.jsonl", "snapshot.json", "sources", blob]
    )
    for name in ("candidates.json", "ledger.jsonl", blob):
        assert (output / name).read_bytes() == (workspace / name).read_bytes()

    code, result = analyze(capsys, output / "snapshot.json", "reranker")
    assert code == 0, result
    assert result["analysis_status"] == "success"
    assert result["upstream_candidates"] == RERANKER_UPSTREAM
    assert result["engine_mode"] == "live_engine"
    assert result["engine"]["version"]
    assert result["fixture"] is False
    assert result["snapshot_kind"] == "reviewed"
    assert result["snapshot_id"] == out["snapshot_id"]
    assert result["snapshot_hash"] == out["snapshot_sha256"]
    assert result["review"]["ledger_head_sha256"] == out["ledger_head_sha256"]
    assert result["review"]["summary"]["rejected"] == 3
    assert result["unique_relation_count"] == 5
    support = {
        (f["subject"], f["object"]): f["support"][0]["candidate_id"]
        for f in result["facts"]
    }
    assert support == {(s, o): ids(workspace)[(s, o, sec)] for s, o, sec in CORRECT}

    code, result = analyze(capsys, output / "snapshot.json", "webapi")
    assert code == 0, result
    assert result["analysis_status"] == "empty"
    assert result["upstream_candidates"] == []


def test_partial_review_lists_pending_and_blocked(capsys, workspace, tmp_path):
    reviewed(capsys, workspace, reject=())
    code, out = export(capsys, workspace, tmp_path / "snap")
    assert code == 0, out
    statuses = {
        (e["subject"], e["object"], e["source_check"]["kind"]): e["status"]
        for e in out["excluded"]
    }
    assert statuses == {
        ("answer", "webapi", None): "pending",
        ("reranker", "webapi", "quote_missing"): "approval_blocked",
        ("retrieval", "reranker", "section_mismatch"): "approval_blocked",
    }
    assert out["summary"]["pending"] == 1
    assert out["summary"]["approval_blocked"] == 2
    code, result = analyze(capsys, tmp_path / "snap/snapshot.json", "reranker")
    assert code == 0, result
    assert result["upstream_candidates"] == RERANKER_UPSTREAM
    assert result["review"]["summary"]["pending"] == 1


def test_human_approval_of_reversed_relation_reaches_analysis(
    capsys, workspace, tmp_path
):
    # Direction is a human judgement; a wrong approval is analyzed as recorded.
    reviewed(capsys, workspace, reject=())
    decide(capsys, workspace, REVERSED, "approve")
    code, out = export(capsys, workspace, tmp_path / "snap")
    assert code == 0, out
    code, result = analyze(capsys, tmp_path / "snap/snapshot.json", "webapi")
    assert code == 0, result
    assert result["upstream_candidates"] == ["answer"]


@pytest.mark.parametrize("reject", [(), (REVERSED, FAKE_QUOTE)])
def test_export_without_approvals_is_refused(capsys, workspace, tmp_path, reject):
    for key in reject:
        decide(capsys, workspace, key, "reject")
    code, out = export(capsys, workspace, tmp_path / "snap")
    assert code == 2
    assert out["export_status"] == "error"
    assert out["error"]["kind"] == "nothing_approved"
    assert out["approved"] is None
    assert not (tmp_path / "snap").exists()


def test_export_to_existing_path_is_refused(capsys, workspace, tmp_path):
    reviewed(capsys, workspace)
    existing = tmp_path / "snap"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep")
    code, out = export(capsys, workspace, existing)
    assert code == 4
    assert out["error"]["kind"] == "output_error"
    assert [p.name for p in existing.iterdir()] == ["keep.txt"]


def test_export_of_tampered_workspace_creates_nothing(capsys, workspace, tmp_path):
    reviewed(capsys, workspace)
    path = workspace / "candidates.json"
    path.write_text(path.read_text().replace('"answer"', '"suggest"', 1))
    code, out = export(capsys, workspace, tmp_path / "snap")
    assert code == 2
    assert out["error"]["kind"] == "integrity_error"
    assert not (tmp_path / "snap").exists()


def test_export_of_non_workspace_is_input_error(capsys, tmp_path):
    code, out = export(capsys, tmp_path, tmp_path / "snap")
    assert code == 2
    assert out["error"]["kind"] == "input_error"
    assert not (tmp_path / "snap").exists()


def test_export_failure_after_mkdir_removes_output(
    capsys, workspace, tmp_path, monkeypatch
):
    reviewed(capsys, workspace)
    real_open = os.open

    def fail_ledger(path, *args, **kwargs):
        if str(path).endswith("ledger.jsonl"):
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_ledger)
    code, out = export(capsys, workspace, tmp_path / "snap")
    assert code == 2
    assert out["error"]["kind"] == "integrity_error"
    assert not (tmp_path / "snap").exists()


def test_export_is_deterministic(capsys, workspace, tmp_path):
    reviewed(capsys, workspace)
    _, first = export(capsys, workspace, tmp_path / "a")
    _, second = export(capsys, workspace, tmp_path / "b")
    assert first["snapshot_id"] == second["snapshot_id"]
    assert (tmp_path / "a/snapshot.json").read_bytes() == (
        tmp_path / "b/snapshot.json"
    ).read_bytes()


def _edit_snapshot(snapshot, change):
    data = json.loads(snapshot.read_text())
    change(data)
    snapshot.write_text(json.dumps(data, indent=2))


def _inject_reversed(data):
    fact = dict(data["approved"][0])
    fact.update(subject="answer", object="webapi")
    data["approved"].append(fact)


@pytest.mark.parametrize(
    "tamper",
    [
        _inject_reversed,
        lambda d: d["approved"].pop(),
        lambda d: d["excluded"][0].update(status="pending"),
        lambda d: d.update(snapshot_id="rs-0000000000000000"),
        lambda d: d.update(ledger_head_sha256="0" * 64),
        lambda d: d["summary"].update(rejected=0),
        lambda d: d["approved"][0].update(reviewer="someone else"),
        lambda d: d.update(extra=True),
        lambda d: d.update(fixture=True),
        lambda d: d.update(approved=[]),
    ],
    ids=[
        "inject-approval",
        "drop-approval",
        "excluded-status",
        "snapshot-id",
        "ledger-head",
        "summary",
        "reviewer",
        "extra-field",
        "fixture-flag",
        "empty-approved",
    ],
)
def test_edited_snapshot_is_refused(capsys, snapshot, tamper):
    _edit_snapshot(snapshot, tamper)
    code, result = analyze(capsys, snapshot, "reranker")
    assert code == 2
    assert result["analysis_status"] == "error"
    assert result["error"]["kind"] == "input_error"
    assert result["upstream_candidates"] is None


def _source_blob(snapshot):
    return next((snapshot.parent / "sources").iterdir())


@pytest.mark.parametrize(
    "tamper",
    [
        lambda s: (s.parent / "ledger.jsonl").unlink(),
        lambda s: (s.parent / "ledger.jsonl").write_bytes(
            b"".join((s.parent / "ledger.jsonl").read_bytes().splitlines(True)[1:])
        ),
        lambda s: (s.parent / "candidates.json").unlink(),
        lambda s: _source_blob(s).write_bytes(_source_blob(s).read_bytes() + b"x"),
    ],
    ids=[
        "ledger-removed",
        "ledger-line-removed",
        "candidates-removed",
        "source-edited",
    ],
)
def test_edited_evidence_is_refused(capsys, snapshot, tamper):
    tamper(snapshot)
    code, result = analyze(capsys, snapshot, "reranker")
    assert code == 2
    assert result["error"]["kind"] == "input_error"
    assert "verification" in result["error"]["message"]


def test_decision_appended_to_exported_copy_is_refused(capsys, workspace, tmp_path):
    reviewed(capsys, workspace, reject=())
    code, out = export(capsys, workspace, tmp_path / "snap")
    assert code == 0, out
    # The copy is a valid workspace, but its snapshot.json no longer matches it.
    decide(capsys, tmp_path / "snap", REVERSED, "approve")
    code, result = analyze(capsys, tmp_path / "snap/snapshot.json", "webapi")
    assert code == 2
    assert result["error"]["message"] == (
        "Reviewed snapshot does not match its review evidence"
    )


def test_unknown_target_on_reviewed_snapshot_is_input_error(capsys, snapshot):
    code, result = analyze(capsys, snapshot, "billing")
    assert code == 2
    assert result["error"]["kind"] == "input_error"
    assert result["snapshot_id"].startswith("rs-")
