"""T01a parity: real upstream CLI is opt-in, never replaced by a Python calculation."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data/fixtures/s1/snapshot.json"


def invoke(binary, target="reranker"):
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from change_scope.cli import main; raise SystemExit(main())",
            "verify-wirelog",
            "--snapshot",
            str(SNAPSHOT),
            "--target",
            target,
            "--binary",
            str(binary),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return process, json.loads(process.stdout)


@pytest.mark.parametrize(
    "target,expected",
    [
        ("reranker", ["answer", "retrieval", "suggest", "webapi"]),
        ("webapi", []),
    ],
)
def test_real_wirelog_parity(target, expected):
    binary = os.environ.get("WIRELOG_CLI")
    if not binary:
        pytest.skip("Opt in with WIRELOG_CLI=/path/to/upstream/wirelog_cli")
    process, result = invoke(binary, target)
    assert process.returncode == 0, process.stderr
    assert result["engine_mode"] == "live_engine"
    assert result["parity"] is True
    assert result["upstream_candidates"] == result["pyrewire_candidates"] == expected
    assert result["analysis_status"] == ("success" if expected else "empty")
    assert result["cli_version"].startswith("wirelog_cli ")
    for key in ("binary_sha256", "snapshot_hash", "rules_hash", "program_sha256"):
        assert len(result[key]) == 64


def test_missing_binary_is_error(tmp_path):
    process, result = invoke(tmp_path / "missing")
    assert process.returncode == 3
    assert result["analysis_status"] == "error"
    assert result["upstream_candidates"] is None
    assert result["parity"] is None


def test_invalid_target_does_not_start_cli(tmp_path):
    process, result = invoke(tmp_path / "missing", "unknown")
    assert process.returncode == 2
    assert result["error"]["kind"] == "input_error"


@pytest.mark.parametrize("mode", ["failure", "malformed", "mismatch"])
def test_cli_failure_is_not_empty(tmp_path, mode):
    binary = tmp_path / "fake_wirelog"
    # Explicit process double, not upstream execution evidence.
    code = {
        "failure": "raise SystemExit(7)",
        "malformed": 'print("garbage")',
        "mismatch": "print('upstream(\"answer\")')",
    }[mode]
    binary.write_text(
        "#!"
        + sys.executable
        + '\nimport sys\nif "--version" in sys.argv:\n print("wirelog_cli 0.70.0")\nelse:\n '
        + code
        + "\n"
    )
    binary.chmod(0o700)
    process, result = invoke(binary)
    assert process.returncode == 3
    assert result["analysis_status"] == "error"
    assert result["upstream_candidates"] is None
    assert result["parity"] is None
