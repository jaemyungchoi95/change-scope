"""T04 seam: revisions, new source versions and withdrawals in the review ledger.

No LLM/network calls.
"""

import hashlib
import json
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
