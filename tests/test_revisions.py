"""T04 seam: revisions, new source versions and withdrawals through the public CLI.

No LLM/network calls; PyreWire runs for real.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from change_scope.cli import main
from change_scope.extraction import extract

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/fixtures/t03/topology.md"
SOURCE_V2 = ROOT / "data/fixtures/t04/topology-v2.md"
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
BATCH_QUOTE = "webapi calls answer for the nightly batch export."


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


def review(capsys, command, workspace, *args):
    return cli(
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


def decide(capsys, workspace, cid, decision, revision=1):
    return review(
        capsys,
        "decide",
        workspace,
        "--id",
        cid,
        "--revision",
        revision,
        "--decision",
        decision,
    )


def revise(capsys, workspace, cid, from_revision=1, *fields):
    return review(
        capsys,
        "revise",
        workspace,
        "--id",
        cid,
        "--from-revision",
        from_revision,
        *fields,
    )


def withdraw(capsys, workspace, cid, revision):
    return review(capsys, "withdraw", workspace, "--id", cid, "--revision", revision)


def add_source(capsys, workspace, source=SOURCE_V2, version="2"):
    return review(
        capsys,
        "add-source",
        workspace,
        "--source",
        source,
        "--source-version",
        version,
    )


def export(capsys, workspace, output):
    return cli(capsys, "export", "--workspace", workspace, "--output", output)


def analyze(capsys, snapshot, target):
    return cli(capsys, "analyze", "--snapshot", snapshot, "--target", target)


def ok(result):
    code, out = result
    assert code == 0, out
    return out


def t03_reviewed(capsys, workspace):
    cid = ids(workspace)
    for key in CORRECT:
        ok(decide(capsys, workspace, cid[key], "approve"))
    ok(decide(capsys, workspace, cid[REVERSED], "reject"))
    return cid


def test_new_source_version_flow(capsys, workspace, tmp_path):
    cid = t03_reviewed(capsys, workspace)
    out = ok(export(capsys, workspace, tmp_path / "s_a"))
    assert out["summary"]["approved"] == 5
    old = ok(analyze(capsys, tmp_path / "s_a/snapshot.json", "reranker"))
    assert old["upstream_candidates"] == ["answer", "retrieval", "suggest", "webapi"]

    # A new source version invalidates every approval for new snapshots.
    out = ok(add_source(capsys, workspace))
    assert out["source"]["source_version"] == "2"
    assert out["lifecycle"]["stale_source"] == 8
    code, out = export(capsys, workspace, tmp_path / "stale")
    assert code == 2 and out["error"]["kind"] == "nothing_approved"
    assert not (tmp_path / "stale").exists()

    # Re-review: same claims on v2. suggest -> reranker is gone from v2.
    for key in CORRECT:
        out = ok(revise(capsys, workspace, cid[key]))
        assert out["candidate"]["revision"] == 2
        assert out["candidate"]["current_status"] == "pending"
        assert out["candidate"]["source_version"] == "2"
        assert out["candidate"]["history"][0]["status"] == "approved"
    gone = cid[CORRECT[4]]
    code, out = decide(capsys, workspace, gone, "approve", revision=2)
    assert code == 2 and out["error"]["kind"] == "approval_blocked"
    ok(decide(capsys, workspace, gone, "reject", revision=2))
    for key in CORRECT[:4]:
        ok(decide(capsys, workspace, cid[key], "approve", revision=2))

    # The rejected reversed candidate becomes a second source for webapi -> answer.
    out = ok(
        revise(
            capsys,
            workspace,
            cid[REVERSED],
            1,
            "--subject",
            "webapi",
            "--object",
            "answer",
            "--section",
            "Batch",
            "--quote",
            BATCH_QUOTE,
        )
    )
    assert out["candidate"]["source_check"]["status"] == "passed"
    ok(decide(capsys, workspace, cid[REVERSED], "approve", revision=2))

    out = ok(export(capsys, workspace, tmp_path / "s_b0"))
    assert out["summary"]["approved"] == 5 and out["summary"]["relations"] == 4
    both = ok(analyze(capsys, tmp_path / "s_b0/snapshot.json", "reranker"))
    assert both["upstream_candidates"] == ["answer", "retrieval", "webapi"]
    support = {
        (f["subject"], f["object"]): sorted(s["candidate_id"] for s in f["support"])
        for f in both["facts"]
    }
    assert support[("webapi", "answer")] == sorted([cid[CORRECT[0]], cid[REVERSED]])

    # Withdrawing one of two sources keeps the relation.
    out = ok(withdraw(capsys, workspace, cid[CORRECT[0]], 2))
    assert out["candidate"]["current_status"] == "withdrawn"
    out = ok(export(capsys, workspace, tmp_path / "s_b"))
    statuses = {e["candidate_id"]: e["status"] for e in out["excluded"]}
    assert statuses[cid[CORRECT[0]]] == "withdrawn"
    assert statuses[gone] == "rejected"
    assert statuses[cid[FAKE_QUOTE]] == "stale_source"
    new = ok(analyze(capsys, tmp_path / "s_b/snapshot.json", "reranker"))
    assert new["upstream_candidates"] == ["answer", "retrieval", "webapi"]
    assert new["reviewed_schema_version"] == 2
    assert [s["source_version"] for s in new["sources"]] == ["1", "2"]
    empty = ok(analyze(capsys, tmp_path / "s_b/snapshot.json", "webapi"))
    assert empty["analysis_status"] == "empty"

    # The earlier snapshot still analyzes from its own preserved v1 evidence.
    again = ok(analyze(capsys, tmp_path / "s_a/snapshot.json", "reranker"))
    assert again["upstream_candidates"] == old["upstream_candidates"]
    assert again["snapshot_hash"] == old["snapshot_hash"]

    out = ok(
        cli(capsys, "review", "show", "--workspace", workspace, "--id", cid[CORRECT[0]])
    )
    history = out["candidates"][0]["history"]
    assert [(h["revision"], h["source_version"], h["status"]) for h in history] == [
        (1, "1", "approved"),
        (2, "2", "withdrawn"),
    ]


def test_withdrawing_the_only_source_removes_the_relation(capsys, workspace, tmp_path):
    cid = t03_reviewed(capsys, workspace)
    ok(withdraw(capsys, workspace, cid[CORRECT[4]], 1))
    ok(export(capsys, workspace, tmp_path / "snap"))
    result = ok(analyze(capsys, tmp_path / "snap/snapshot.json", "reranker"))
    # suggest only reached reranker through the withdrawn relation.
    assert result["upstream_candidates"] == ["answer", "retrieval", "webapi"]


@pytest.mark.parametrize(
    "version,source,message",
    [
        ("1", SOURCE, "already exists"),
        ("1", SOURCE_V2, "different content"),
        ("Bad Version", SOURCE_V2, "source version must match"),
        ("2", ROOT / "missing.md", "Cannot read source"),
    ],
)
def test_add_source_refusals_create_nothing(
    capsys, workspace, version, source, message
):
    code, out = add_source(capsys, workspace, source, version)
    assert code == 2 and out["error"]["kind"] == "input_error"
    assert message in out["error"]["message"]
    assert not (workspace / "ledger.jsonl").exists()
    assert len(list((workspace / "sources").iterdir())) == 1


def test_add_source_twice_is_refused(capsys, workspace):
    ok(add_source(capsys, workspace))
    code, out = add_source(capsys, workspace)
    assert code == 2 and "already exists" in out["error"]["message"]


@pytest.mark.parametrize(
    "args,message",
    [
        ((2,), "revise from the latest revision"),
        ((1,), "must change"),
        ((1, "--source-version", "9"), "Unknown source version"),
        ((1, "--subject", "billing"), "Invalid revision subject"),
    ],
)
def test_revise_refusals(capsys, workspace, args, message):
    cid = ids(workspace)[CORRECT[0]]
    code, out = revise(capsys, workspace, cid, *args)
    assert code == 2 and out["error"]["kind"] == "input_error"
    assert message in out["error"]["message"]
    assert not (workspace / "ledger.jsonl").exists()


def test_revision_must_cite_current_source(capsys, workspace):
    cid = ids(workspace)[CORRECT[0]]
    ok(add_source(capsys, workspace))
    code, out = revise(
        capsys,
        workspace,
        cid,
        1,
        "--source-version",
        "1",
        "--quote",
        "webapi calls answer",
    )
    assert code == 2
    assert "current source version 2" in out["error"]["message"]


def test_decisions_follow_the_latest_revision(capsys, workspace):
    cid = ids(workspace)
    ok(add_source(capsys, workspace))
    # Pending v1 revision with a passing check: stale, so approval is blocked.
    code, out = decide(capsys, workspace, cid[REVERSED], "approve")
    assert code == 2 and out["error"]["kind"] == "approval_blocked"
    assert "revise it first" in out["error"]["message"]
    ok(decide(capsys, workspace, cid[REVERSED], "reject"))
    ok(revise(capsys, workspace, cid[CORRECT[0]]))
    code, out = decide(capsys, workspace, cid[CORRECT[0]], "approve", revision=1)
    assert code == 2 and out["error"]["kind"] == "input_error"
    ok(decide(capsys, workspace, cid[CORRECT[0]], "approve", revision=2))
    code, out = decide(capsys, workspace, cid[CORRECT[0]], "reject", revision=2)
    assert code == 2 and out["error"]["kind"] == "already_decided"


def test_withdraw_refusals(capsys, workspace):
    cid = t03_reviewed(capsys, workspace)
    for target, revision in [
        (cid[FAKE_QUOTE], 1),  # pending
        (cid[REVERSED], 1),  # rejected
        (cid[CORRECT[0]], 2),  # no such revision
    ]:
        code, out = withdraw(capsys, workspace, target, revision)
        assert code == 2 and out["error"]["kind"] == "input_error", out
    ok(withdraw(capsys, workspace, cid[CORRECT[0]], 1))
    code, out = withdraw(capsys, workspace, cid[CORRECT[0]], 1)
    assert code == 2 and "already withdrawn" in out["error"]["message"]
    ok(add_source(capsys, workspace))
    ok(revise(capsys, workspace, cid[CORRECT[1]]))
    code, out = withdraw(capsys, workspace, cid[CORRECT[1]], 1)
    assert code == 2 and out["error"]["kind"] == "input_error"


def test_text_output_shows_lifecycle_and_history(capsys, workspace):
    cid = t03_reviewed(capsys, workspace)
    ok(add_source(capsys, workspace))
    ok(revise(capsys, workspace, cid[CORRECT[0]]))
    main(["review", "show", "--workspace", str(workspace), "--text"])
    text = capsys.readouterr().out
    assert "Lifecycle : versions 1, 2; revised 1, withdrawn 0, stale source 7" in text
    assert "(stale source v1)" in text
    main(
        [
            "review",
            "show",
            "--workspace",
            str(workspace),
            "--id",
            cid[CORRECT[0]],
            "--text",
        ]
    )
    text = capsys.readouterr().out
    assert "history  : rev 1 v1 webapi -> answer [approved] approve by tester" in text
    assert "history  : rev 2 v2 webapi -> answer [pending]" in text


def _chain(entries):
    lines, previous = [], None
    for number, entry in enumerate(entries, start=1):
        entry = entry | {"seq": number, "prev_sha256": previous}
        line = json.dumps(entry, separators=(",", ":")).encode()
        lines.append(line)
        previous = hashlib.sha256(line).hexdigest()
    return b"".join(line + b"\n" for line in lines)


def _entries(workspace):
    lines = (workspace / "ledger.jsonl").read_bytes().split(b"\n")[:-1]
    return [json.loads(line) for line in lines]


def _revise_entry(entries):
    return next(e for e in entries if e.get("type") == "revise")


def _forge_passed_check(entries):
    revised = _revise_entry(entries)
    revised["candidate"]["source_check"] = {
        "status": "passed",
        "kind": None,
        "message": None,
    }
    return entries


def _forge_content(entries):
    _revise_entry(entries)["candidate"]["subject"] = "webapi"
    return entries


def _forge_approval(entries):
    decision = dict(next(e for e in entries if "decision" in e))
    revised = _revise_entry(entries)["candidate"]
    decision.update(
        candidate_id=revised["candidate_id"],
        revision=2,
        decision="approve",
        source_sha256=revised["source_sha256"],
    )
    return entries + [decision]


def _forge_withdraw(entries):
    withdrawn = dict(_revise_entry(entries))
    for key in ("candidate", "content_sha256"):
        withdrawn.pop(key)
    revised = _revise_entry(entries)["candidate"]
    withdrawn.update(type="withdraw", candidate_id=revised["candidate_id"], revision=2)
    return entries + [withdrawn]


def _forge_source_reorder(entries):
    # Move the revision before the source event that it cites.
    source = next(e for e in entries if e.get("type") == "source")
    revised = _revise_entry(entries)
    rest = [e for e in entries if e is not source and e is not revised]
    return rest + [revised, source]


@pytest.mark.parametrize(
    "forge",
    [
        _forge_passed_check,
        _forge_content,
        _forge_approval,
        _forge_withdraw,
        _forge_source_reorder,
        lambda entries: entries + [entries[-1] | {"type": "rename"}],
    ],
    ids=[
        "blocked-check-passed",
        "content-hash",
        "approve-blocked-revision",
        "withdraw-pending",
        "revision-before-source",
        "unknown-type",
    ],
)
def test_rechained_t04_rule_violations_are_integrity_errors(capsys, workspace, forge):
    cid = ids(workspace)
    ok(decide(capsys, workspace, cid[CORRECT[0]], "approve"))
    ok(add_source(capsys, workspace))
    # suggest -> reranker fails its source check on v2.
    ok(revise(capsys, workspace, cid[CORRECT[4]]))
    (workspace / "ledger.jsonl").write_bytes(_chain(forge(_entries(workspace))))
    code, out = cli(capsys, "review", "show", "--workspace", workspace)
    assert code == 2 and out["error"]["kind"] == "integrity_error"


def test_missing_new_source_blob_is_integrity_error(capsys, workspace):
    ok(add_source(capsys, workspace))
    blob = hashlib.sha256(SOURCE_V2.read_bytes()).hexdigest()
    (workspace / f"sources/{blob}.md").unlink()
    code, out = cli(capsys, "review", "show", "--workspace", workspace)
    assert code == 2 and out["error"]["kind"] == "integrity_error"


def _v2_snapshot(capsys, workspace, tmp_path):
    cid = t03_reviewed(capsys, workspace)
    ok(add_source(capsys, workspace))
    ok(revise(capsys, workspace, cid[CORRECT[1]]))
    ok(decide(capsys, workspace, cid[CORRECT[1]], "approve", revision=2))
    ok(export(capsys, workspace, tmp_path / "snap"))
    return tmp_path / "snap"


def test_v2_snapshot_ships_every_source_version(capsys, workspace, tmp_path):
    snap = _v2_snapshot(capsys, workspace, tmp_path)
    assert len(list((snap / "sources").iterdir())) == 2
    result = ok(analyze(capsys, snap / "snapshot.json", "reranker"))
    # Only answer -> retrieval was re-approved on v2.
    assert result["upstream_candidates"] == []
    result = ok(analyze(capsys, snap / "snapshot.json", "retrieval"))
    assert result["upstream_candidates"] == ["answer"]


@pytest.mark.parametrize(
    "tamper",
    [
        lambda snap: next(
            p
            for p in (snap / "sources").iterdir()
            if p.read_bytes() == SOURCE.read_bytes()
        ).unlink(),
        lambda snap: _edit(snap, lambda d: d["sources"].pop(0)),
        lambda snap: _edit(snap, lambda d: d["excluded"][0].update(status="pending")),
        lambda snap: _edit(snap, lambda d: d.update(schema_version=3)),
        lambda snap: _edit(snap, lambda d: d.update(schema_version=1)),
    ],
    ids=["old-blob-removed", "sources-list", "excluded", "schema-3", "schema-1"],
)
def test_edited_v2_snapshot_is_refused(capsys, workspace, tmp_path, tamper):
    snap = _v2_snapshot(capsys, workspace, tmp_path)
    tamper(snap)
    code, out = analyze(capsys, snap / "snapshot.json", "retrieval")
    assert code == 2 and out["error"]["kind"] == "input_error"


def _edit(snap, change):
    data = json.loads((snap / "snapshot.json").read_text())
    change(data)
    (snap / "snapshot.json").write_text(json.dumps(data, indent=2))


def test_export_copy_failure_of_new_blob_cleans_up(capsys, workspace, tmp_path):
    snap = _v2_snapshot(capsys, workspace, tmp_path)
    shutil.rmtree(snap)
    blob = hashlib.sha256(SOURCE_V2.read_bytes()).hexdigest()
    (workspace / f"sources/{blob}.md").write_bytes(b"changed")
    code, out = export(capsys, workspace, tmp_path / "snap")
    assert code == 2 and out["error"]["kind"] == "integrity_error"
    assert not (tmp_path / "snap").exists()
