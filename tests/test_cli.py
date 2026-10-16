"""Public CLI seam with the real PyreWire engine; no LLM/network calls."""

import hashlib
import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from change_scope import engine
from change_scope.cli import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/fixtures/s1/snapshot.json"


def run_cli(snapshot=FIXTURE, target="reranker", *options):
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from change_scope.cli import main; raise SystemExit(main())",
            "analyze",
            "--snapshot",
            str(snapshot),
            "--target",
            target,
            *options,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return process, json.loads(process.stdout)


@pytest.fixture
def snapshot_file(tmp_path):
    for path in FIXTURE.parent.iterdir():
        shutil.copy(path, tmp_path / path.name)
    return tmp_path / "snapshot.json"


def mutate(path, change):
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def test_real_s1_and_saved_result(tmp_path):
    output = tmp_path / "result.json"
    process, result = run_cli(FIXTURE, "reranker", "--output", str(output))
    assert process.returncode == 0, process.stderr
    assert result["analysis_status"] == "success"
    assert result["upstream_candidates"] == ["answer", "retrieval", "suggest", "webapi"]
    assert result["unique_relation_count"] == 5
    assert result["engine"] == {"name": "pyrewire", "version": "1.1.2"}
    assert result["engine_mode"] == "live_engine"
    assert result["llm_mode"] == "not_used"
    assert result["deployment_status"] == "unverified"
    assert result["snapshot_id"] == "S1"
    for key in ("snapshot_hash", "input_hash", "rules_hash"):
        assert len(result[key]) == 64
    assert json.loads(output.read_text()) == result


def test_valid_empty_is_successful_query():
    process, result = run_cli(FIXTURE, "webapi")
    assert process.returncode == 0
    assert result["analysis_status"] == "empty"
    assert result["upstream_candidates"] == []
    assert result["engine_mode"] == "live_engine"
    assert "error" not in result


@pytest.mark.parametrize("target", ["missing", 'reranker"). reach("x","y'])
def test_unknown_target_is_input_error(target):
    process, result = run_cli(FIXTURE, target)
    assert process.returncode == 2
    assert result["analysis_status"] == "error"
    assert result["error"]["kind"] == "input_error"
    assert result["upstream_candidates"] is None
    assert result["engine_mode"] == "unavailable"


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema_version=True),
        lambda d: d.update(fixture=False),
        lambda d: d.update(services=["webapi", "webapi"]),
        lambda d: d["assertions"][0].update(review_status="pending"),
        lambda d: d["assertions"][0].update(subject="missing"),
        lambda d: d["assertions"][0].update(quote="fabricated"),
        lambda d: d["assertions"][0].update(source_version="2"),
        lambda d: d["assertions"][0].update(section="Missing section"),
        lambda d: d["sources"][0].update(path="../outside.md"),
        lambda d: d["sources"][0].update(path="bad\x00path"),
        lambda d: d["sources"][0].update(sha256="0" * 64),
    ],
)
def test_invalid_snapshot_rejected(snapshot_file, change):
    mutate(snapshot_file, change)
    process, result = run_cli(snapshot_file)
    assert process.returncode == 2
    assert result["analysis_status"] == "error"
    assert result["error"]["kind"] == "input_error"
    assert result["upstream_candidates"] is None


@pytest.mark.parametrize("content", ["{", "[]", "null"])
def test_malformed_snapshot(snapshot_file, content):
    snapshot_file.write_text(content)
    process, result = run_cli(snapshot_file)
    assert process.returncode == 2
    assert result["error"]["kind"] == "input_error"


def test_missing_snapshot(tmp_path):
    process, result = run_cli(tmp_path / "missing.json")
    assert process.returncode == 2
    assert result["analysis_status"] == "error"


def test_deduplicated_edges_keep_support(snapshot_file):
    def duplicate(data):
        fact = deepcopy(data["assertions"][0])
        fact["assertion_id"] = "F06"
        data["assertions"].append(fact)

    mutate(snapshot_file, duplicate)
    process, result = run_cli(snapshot_file)
    assert process.returncode == 0
    assert result["unique_relation_count"] == 5
    assert result["upstream_candidates"] == ["answer", "retrieval", "suggest", "webapi"]
    supports = next(f["support"] for f in result["facts"] if f["object"] == "answer")
    assert {a["assertion_id"] for a in supports} == {"F01", "F06"}


def test_cycle_terminates_and_excludes_target(snapshot_file):
    def cycle(data):
        source = snapshot_file.parent / "topology.md"
        content = source.read_bytes() + b"reranker calls answer over HTTP.\n"
        source.write_bytes(content)
        data["sources"][0]["sha256"] = hashlib.sha256(content).hexdigest()
        fact = deepcopy(data["assertions"][2])
        fact.update(
            assertion_id="F06",
            subject="reranker",
            object="answer",
            quote="reranker calls answer over HTTP.",
        )
        data["assertions"].append(fact)

    mutate(snapshot_file, cycle)
    process, result = run_cli(snapshot_file, "answer")
    assert process.returncode == 0
    assert result["upstream_candidates"] == [
        "reranker",
        "retrieval",
        "suggest",
        "webapi",
    ]


def test_unsupported_timeout_returns_engine_error():
    process, result = run_cli(FIXTURE, "reranker", "--timeout", "1e308")
    assert process.returncode == 3
    assert result["analysis_status"] == "error"
    assert result["error"]["kind"] == "engine_error"
    assert result["upstream_candidates"] is None


def test_existing_output_is_preserved(tmp_path):
    output = tmp_path / "result.json"
    output.write_text("previous run")
    process, result = run_cli(FIXTURE, "reranker", "--output", str(output))
    assert process.returncode == 4
    assert result["analysis_status"] == "error"
    assert result["error"]["kind"] == "output_error"
    assert output.read_text() == "previous run"


@pytest.mark.parametrize(
    "fault", ["timeout", "native_crash", "exception", "invalid_json", "bad_ids"]
)
def test_engine_fault_is_never_empty(monkeypatch, capsys, fault):
    # Fault injection only at the native-process boundary; normal tests use PyreWire.
    def failed_process(*args, **kwargs):
        if fault == "timeout":
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        return subprocess.CompletedProcess(
            args[0],
            {
                "native_crash": -11,
                "exception": 1,
            }.get(fault, 0),
            stdout={"invalid_json": "not JSON", "bad_ids": "[42]"}.get(fault, ""),
            stderr="injected engine failure"
            if fault in ("exception", "native_crash")
            else "",
        )

    monkeypatch.setattr(engine.subprocess, "run", failed_process)
    assert main(["analyze", "--snapshot", str(FIXTURE), "--target", "reranker"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["analysis_status"] == "error"
    assert result["error"]["kind"] == "engine_error"
    assert result["upstream_candidates"] is None


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "invalid"])
def test_invalid_timeout(timeout):
    with pytest.raises(SystemExit) as error:
        main(
            [
                "analyze",
                "--snapshot",
                str(FIXTURE),
                "--target",
                "reranker",
                "--timeout",
                timeout,
            ]
        )
    assert error.value.code == 2
