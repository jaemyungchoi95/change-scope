"""T03-C seam: export approved candidates as a reviewed snapshot; no LLM/network calls."""

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


def reviewed(capsys, workspace, *, reject=(REVERSED, FAKE_QUOTE, WRONG_SECTION)):
    for key in CORRECT:
        decide(capsys, workspace, key, "approve")
    for key in reject:
        decide(capsys, workspace, key, "reject")
    return workspace


def test_full_review_exports_only_approved(capsys, workspace, tmp_path):
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
