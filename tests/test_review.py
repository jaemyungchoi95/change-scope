"""T03-B seam: human review CLI and ledger integrity; no LLM/network calls."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from change_scope import review
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
    out = capsys.readouterr().out
    return code, out


def show(capsys, workspace, *extra):
    code, out = cli(capsys, "review", "show", "--workspace", workspace, *extra)
    return code, json.loads(out)


def decide(
    capsys,
    workspace,
    cid,
    decision,
    reason="checked against source",
    revision=1,
    reviewer="tester",
):
    code, out = cli(
        capsys,
        "review",
        "decide",
        "--workspace",
        workspace,
        "--id",
        cid,
        "--revision",
        revision,
        "--decision",
        decision,
        "--reviewer",
        reviewer,
        "--reason",
        reason,
    )
    return code, json.loads(out)


def test_show_lists_pending_candidates_with_source_text(capsys, workspace):
    code, result = show(capsys, workspace)
    assert code == 0
    assert result["show_status"] == "success"
    assert result["summary"] == {
        "total": 8,
        "pending": 8,
        "approved": 0,
        "rejected": 0,
        "source_check_failed": 2,
    }
    assert result["ledger_head_sha256"] is None
    views = {(c["subject"], c["object"], c["section"]): c for c in result["candidates"]}
    assert views[REVERSED]["approvable"] is True
    assert "webapi calls answer over HTTP." in views[REVERSED]["section_text"]
    assert views[FAKE_QUOTE]["approvable"] is False
    assert views[WRONG_SECTION]["approvable"] is False
    assert views[WRONG_SECTION]["section_text"].startswith("Operators restart")
    assert not (workspace / "ledger.jsonl").exists()


def test_public_cli_review_flow(tmp_path, workspace):
    def run(*args):
        p = subprocess.run(
            [
                sys.executable,
                "-c",
                "from change_scope.cli import main; raise SystemExit(main())",
                *map(str, args),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return p.returncode, p.stdout

    candidate = ids(workspace)
    for key in CORRECT:
        code, out = run(
            "review",
            "decide",
            "--workspace",
            workspace,
            "--id",
            candidate[key],
            "--revision",
            1,
            "--decision",
            "approve",
            "--reviewer",
            "tester",
            "--reason",
            "Quote states this direct call",
        )
        assert code == 0, out
    code, out = run(
        "review",
        "decide",
        "--workspace",
        workspace,
        "--id",
        candidate[REVERSED],
        "--revision",
        1,
        "--decision",
        "reject",
        "--reviewer",
        "tester",
        "--reason",
        "Source says webapi calls answer; direction reversed",
    )
    assert code == 0
    result = json.loads(out)
    assert result["candidate"]["current_status"] == "rejected"
    assert result["summary"]["approved"] == 5 and result["summary"]["rejected"] == 1

    code, out = run("review", "show", "--workspace", workspace, "--text")
    assert code == 0
    assert "approved 5, rejected 1" in out
    assert "(approval blocked)" in out

    lines = (workspace / "ledger.jsonl").read_bytes().split(b"\n")[:-1]
    assert len(lines) == 6
    previous = None
    for number, line in enumerate(lines, start=1):
        entry = json.loads(line)
        assert entry["seq"] == number and entry["prev_sha256"] == previous
        previous = hashlib.sha256(line).hexdigest()
    assert result["ledger_head_sha256"] == previous


@pytest.mark.parametrize(
    "key,kind", [(FAKE_QUOTE, "quote_missing"), (WRONG_SECTION, "section_mismatch")]
)
def test_failed_source_check_blocks_approval_but_allows_reject(
    capsys, workspace, key, kind
):
    cid = ids(workspace)[key]
    code, result = decide(capsys, workspace, cid, "approve")
    assert code == 2
    assert result["decide_status"] == "error"
    assert result["error"]["kind"] == "approval_blocked"
    assert kind in result["error"]["message"]
    assert not (workspace / "ledger.jsonl").exists()
    code, result = decide(
        capsys, workspace, cid, "reject", reason="Quote not in named section"
    )
    assert code == 0
    assert result["candidate"]["current_status"] == "rejected"


def test_second_decision_on_same_revision_is_refused(capsys, workspace):
    cid = ids(workspace)[CORRECT[0]]
    assert decide(capsys, workspace, cid, "approve")[0] == 0
    before = (workspace / "ledger.jsonl").read_bytes()
    code, result = decide(capsys, workspace, cid, "reject")
    assert code == 2
    assert result["error"]["kind"] == "already_decided"
    assert (workspace / "ledger.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    "cid,revision,reason,reviewer",
    [
        ("c-0000000000000000", 1, "ok", "tester"),
        (None, 2, "ok", "tester"),
        (None, 1, "   ", "tester"),
        (None, 1, "x" * 1001, "tester"),
        (None, 1, "line\nbreak", "tester"),
        (None, 1, "\x1b[31mred", "tester"),
        (None, 1, "ok", ""),
        (None, 1, "ok", "a\tb"),
    ],
)
def test_invalid_decide_requests_are_input_errors(
    capsys, workspace, cid, revision, reason, reviewer
):
    cid = cid or ids(workspace)[CORRECT[0]]
    code, result = decide(
        capsys,
        workspace,
        cid,
        "approve",
        reason=reason,
        revision=revision,
        reviewer=reviewer,
    )
    assert code == 2
    assert result["error"]["kind"] == "input_error"
    assert not (workspace / "ledger.jsonl").exists()


def test_show_single_candidate_and_unknown_id(capsys, workspace):
    cid = ids(workspace)[REVERSED]
    code, result = show(capsys, workspace, "--id", cid)
    assert code == 0 and [c["candidate_id"] for c in result["candidates"]] == [cid]
    code, result = show(capsys, workspace, "--id", "c-missing")
    assert code == 2 and result["error"]["kind"] == "input_error"
    assert result["candidates"] is None


def test_not_a_workspace_is_input_error(capsys, tmp_path):
    code, result = show(capsys, tmp_path / "nothing")
    assert code == 2 and result["error"]["kind"] == "input_error"
    code, result = decide(capsys, tmp_path / "nothing", "c-x", "approve")
    assert code == 2 and result["error"]["kind"] == "input_error"
    assert not (tmp_path / "nothing").exists()


def _edit_candidates(workspace, change):
    path = workspace / "candidates.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def _candidate(data, key):
    return next(
        c
        for c in data["candidates"]
        if (c["subject"], c["object"], c["section"]) == key
    )


@pytest.mark.parametrize(
    "tamper",
    [
        # Flip the reversed candidate into the correct direction without a new ID.
        lambda d: _candidate(d, REVERSED).update(subject="webapi", object="answer"),
        lambda d: _candidate(d, REVERSED).update(review_status="approved"),
        lambda d: _candidate(d, FAKE_QUOTE).update(
            source_check={"status": "passed", "kind": None, "message": None}
        ),
        lambda d: _candidate(d, CORRECT[0]).update(source_version="2"),
        lambda d: _candidate(d, CORRECT[0]).update(revision=2),
        lambda d: d["source"].update(blob="../outside.md"),
        lambda d: d["candidates"].append(dict(d["candidates"][0])),
        lambda d: d.update(llm_mode="live_cli"),
    ],
)
def test_candidate_tampering_is_integrity_error(capsys, workspace, tamper):
    _edit_candidates(workspace, tamper)
    code, result = show(capsys, workspace)
    assert code == 2
    assert result["error"]["kind"] == "integrity_error"
    assert result["candidates"] is None
    code, result = decide(capsys, workspace, "c-anything", "approve")
    assert code == 2 and result["error"]["kind"] == "integrity_error"
    assert not (workspace / "ledger.jsonl").exists()


def test_source_blob_tampering_is_integrity_error(capsys, workspace):
    blob = next((workspace / "sources").iterdir())
    blob.write_bytes(
        blob.read_bytes().replace(b"webapi calls answer", b"answer calls webapi")
    )
    code, result = show(capsys, workspace)
    assert code == 2 and result["error"]["kind"] == "integrity_error"


def _decided(capsys, workspace, count=3):
    candidate = ids(workspace)
    for key in CORRECT[:count]:
        assert decide(capsys, workspace, candidate[key], "approve")[0] == 0
    return (workspace / "ledger.jsonl").read_bytes().split(b"\n")[:-1]


def _chain(entries):
    lines, previous = [], None
    for number, entry in enumerate(entries, start=1):
        entry = entry | {"seq": number, "prev_sha256": previous}
        line = json.dumps(entry, separators=(",", ":")).encode()
        lines.append(line)
        previous = hashlib.sha256(line).hexdigest()
    return b"".join(line + b"\n" for line in lines)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda lines: lines[:1] + lines[2:],
        lambda lines: [lines[0].replace(b"checked against source", b"rubber stamp")]
        + lines[1:],
        lambda lines: [lines[1], lines[0], lines[2]],
        lambda lines: lines + [b"not json"],
    ],
)
def test_ledger_edits_before_head_are_integrity_errors(capsys, workspace, tamper):
    lines = _decided(capsys, workspace)
    (workspace / "ledger.jsonl").write_bytes(
        b"".join(line + b"\n" for line in tamper(lines))
    )
    code, result = show(capsys, workspace)
    assert code == 2 and result["error"]["kind"] == "integrity_error"


def test_partial_final_ledger_line_is_integrity_error(capsys, workspace):
    lines = _decided(capsys, workspace, 1)
    (workspace / "ledger.jsonl").write_bytes(lines[0])
    code, result = show(capsys, workspace)
    assert code == 2 and result["error"]["kind"] == "integrity_error"


@pytest.mark.parametrize(
    "forge",
    [
        # Rule violations survive a correctly recomputed chain.
        lambda entries, cid: entries
        + [entries[0] | {"candidate_id": cid["fake"], "decision": "approve"}],
        lambda entries, cid: entries + [entries[0]],
        lambda entries, cid: entries
        + [entries[0] | {"candidate_id": "c-0000000000000000"}],
        lambda entries, cid: entries
        + [
            entries[0]
            | {"candidate_id": cid["reversed"], "candidate_set_sha256": "0" * 64}
        ],
        lambda entries, cid: entries
        + [entries[0] | {"candidate_id": cid["reversed"], "revision": 2}],
    ],
)
def test_rechained_rule_violations_are_integrity_errors(capsys, workspace, forge):
    lines = _decided(capsys, workspace, 1)
    entries = [json.loads(line) for line in lines]
    candidate = ids(workspace)
    forged = forge(
        entries, {"fake": candidate[FAKE_QUOTE], "reversed": candidate[REVERSED]}
    )
    (workspace / "ledger.jsonl").write_bytes(_chain(forged))
    code, result = show(capsys, workspace)
    assert code == 2 and result["error"]["kind"] == "integrity_error"


def test_symlinked_ledger_is_integrity_error(capsys, workspace, tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    (workspace / "ledger.jsonl").symlink_to(target)
    code, result = show(capsys, workspace)
    assert code == 2 and result["error"]["kind"] == "integrity_error"
    code, result = decide(capsys, workspace, ids(workspace)[CORRECT[0]], "approve")
    assert code == 2 and result["error"]["kind"] == "integrity_error"
    assert target.read_text() == ""


def test_text_output_neutralizes_terminal_control_sequences(capsys, tmp_path):
    source = tmp_path / "doc.md"
    source.write_text("## HTTP calls\n\nwebapi calls answer \x1b[2J over HTTP.\n")
    payload = {
        "document_id": "doc",
        "candidates": [
            {
                "subject": "webapi",
                "predicate": "depends_on",
                "object": "answer",
                "section": "HTTP calls",
                "quote": "webapi calls answer \x1b[2J over HTTP.",
                "review_status": "pending",
            }
        ],
    }
    events = [
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(payload)},
        },
        {"type": "turn.completed"},
    ]
    recording = tmp_path / "r.json"
    recording.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provenance": "synthetic_test_double",
                "source_id": "doc",
                "document_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "returncode": 0,
                "stdout": "".join(json.dumps(e) + "\n" for e in events),
                "stderr": "",
            }
        )
    )
    workspace = tmp_path / "review"
    assert extract(source, "doc", "1", recording, workspace)[1] == 0
    cid = json.loads((workspace / "candidates.json").read_text())["candidates"][0][
        "candidate_id"
    ]
    code, out = cli(
        capsys, "review", "show", "--workspace", workspace, "--id", cid, "--text"
    )
    assert code == 0
    assert "\x1b" not in out
    assert "\\x1b[2J" in out


def test_text_output_reports_errors(capsys, tmp_path):
    code, out = cli(
        capsys, "review", "show", "--workspace", tmp_path / "none", "--text"
    )
    assert code == 2
    assert out.startswith("ERROR [input_error]")


def test_decided_at_is_timezone_aware_utc(capsys, workspace):
    code, result = decide(capsys, workspace, ids(workspace)[CORRECT[0]], "approve")
    assert code == 0
    assert result["candidate"]["decision"]["decided_at"].endswith("Z")
    assert (
        review.load_workspace(workspace)
        .decisions[0]
        .decided_at.utcoffset()
        .total_seconds()
        == 0
    )
