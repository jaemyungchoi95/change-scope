"""T02 seam: fixed process recordings and real bounded test doubles, no LLM network calls."""

import json
import subprocess
import sys
from copy import deepcopy

import pytest

from change_scope import llm
from change_scope.process import ProcessError, ProcessOutput


@pytest.mark.parametrize("case", ["normal", "adversarial"])
def test_public_cli_defaults_to_recorded(case, tmp_path):
    output = tmp_path / "result.json"
    p = subprocess.run(
        [
            sys.executable,
            "-c",
            "from change_scope.cli import main; raise SystemExit(main())",
            "llm-smoke",
            "--case",
            case,
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    result = json.loads(p.stdout)
    assert p.returncode == 0, p.stderr
    assert result["extraction_status"] == "success"
    assert result["llm_mode"] == "recorded_output"
    assert result["provenance"] == "synthetic_test_double"
    assert result["live_guard_passed"] is False
    assert result["candidates"][0]["review_status"] == "pending"
    assert result["audit"]["tool_events"] == 0
    assert json.loads(output.read_text()) == result


def recording():
    return json.loads(llm._fixture("normal-recorded.json"))


def test_recorded_auth_failure_is_error(tmp_path):
    path = tmp_path / "error.json"
    path.write_bytes(llm._fixture("auth-error-recorded.json"))
    result, code = llm.smoke("normal", recording=path)
    assert code == 3
    assert result["error"]["kind"] == "cli_error"
    assert result["candidates"] is None
    assert result["llm_mode"] == "recorded_output"


@pytest.mark.parametrize(
    "mutation,kind",
    [
        (lambda d: d.update(document_sha256="wrong"), "recording_error"),
        (lambda d: d.update(provenance="actual_model"), "recording_error"),
        (lambda d: d.update(schema_version=True), "recording_error"),
        (lambda d: d.update(stdout="not JSON"), "invalid_json"),
        (lambda d: d.update(stdout=""), "protocol_error"),
        (lambda d: d.update(stdout='{"type":"turn.failed"}\n'), "cli_error"),
        (
            lambda d: d.update(
                stdout='{"type":"item.completed","item":{"type":"error","message":"unexpected"}}\n'
            ),
            "cli_error",
        ),
        (
            lambda d: d.update(
                stdout='{"type":"item.started","item":{"type":"command_execution"}}\n'
            ),
            "tool_activity",
        ),
    ],
)
def test_bad_recordings_fail_closed(tmp_path, mutation, kind):
    data = recording()
    mutation(data)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    result, code = llm.smoke("normal", recording=path)
    assert code != 0
    assert result["extraction_status"] == "error"
    assert result["error"]["kind"] == kind
    assert result["candidates"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(extra="bad"),
        lambda p: p.update(document_id="other"),
        lambda p: p["candidates"][0].update(review_status="approved"),
        lambda p: p["candidates"][0].update(quote="fabricated"),
        lambda p: p["candidates"][0].update(subject="unknown"),
        lambda p: p["candidates"].append(deepcopy(p["candidates"][0])),
    ],
)
def test_payload_validation(mutation):
    data = recording()
    payload = json.loads(json.loads(data["stdout"].splitlines()[2])["item"]["text"])
    mutation(payload)
    with pytest.raises(llm.LLMError):
        llm.validate_payload(payload, "normal", llm._fixture("normal.md").decode())


def test_command_and_environment_restrict_inheritance(monkeypatch, tmp_path):
    for name in [
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_SANDBOX",
        "NODE_OPTIONS",
        "LD_PRELOAD",
    ]:
        monkeypatch.setenv(name, "must-not-inherit")
    assert all(
        name not in llm.safe_environment()
        for name in [
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "CODEX_SANDBOX",
            "NODE_OPTIONS",
            "LD_PRELOAD",
        ]
    )
    command = llm.cli_command("/trusted/codex", tmp_path / "schema.json")
    assert "--ignore-user-config" in command and "--strict-config" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'approval_policy="never"' in command
    assert 'web_search="disabled"' in command
    assert "mcp_servers={}" in command
    assert "shell_tool" in command and "apps" in command and "hooks" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


def test_supported_cli_versions_are_pinned():
    # Each entry was added only after the live sandbox probe and both smokes passed.
    assert llm.SUPPORTED_CLIS == ("codex-cli 0.162.1", "codex-cli 0.163.0")


@pytest.mark.parametrize("version", [b"codex-cli 999.0", b"codex-cli 0.164.0"])
def test_unknown_cli_blocks_before_model_call(monkeypatch, version):
    calls = []
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/fake/codex")

    def runner(argv, **kwargs):
        calls.append(argv)
        return ProcessOutput(0, version + b"\n", b"")

    monkeypatch.setattr(llm, "run_bounded", runner)
    result, code = llm.smoke("normal", live=True)
    assert code == 3 and result["error"]["kind"] == "unsupported_cli"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "fault",
    ["timeout", "output_limit", "workspace_change", "tool_activity", "auth", "sandbox"],
)
def test_live_failure_is_never_success(monkeypatch, fault):
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/fake/codex")

    def runner(argv, **kwargs):
        if "--version" in argv:
            return ProcessOutput(0, (llm.SUPPORTED_CLI + "\n").encode(), b"")
        if "login" in argv:
            return ProcessOutput(
                0,
                b"",
                b"Logged in using API key"
                if fault == "auth"
                else b"Logged in using ChatGPT",
            )
        if "sandbox" in argv:
            return ProcessOutput(
                0,
                json.dumps(
                    {"write_blocked": True, "network_blocked": fault != "sandbox"}
                ).encode(),
                b"",
            )
        if fault in ["timeout", "output_limit"]:
            raise ProcessError(fault, "injected failure")
        if fault == "workspace_change":
            (kwargs["cwd"] / "FORBIDDEN.txt").write_text("PWNED")
        data = recording()
        stdout = data["stdout"].encode()
        if fault == "tool_activity":
            stdout = b'{"type":"item.started","item":{"type":"mcp_tool_call"}}\n'
        return ProcessOutput(0, stdout, b"")

    monkeypatch.setattr(llm, "run_bounded", runner)
    result, code = llm.smoke("normal", live=True)
    assert code == 3
    assert result["extraction_status"] == "error" and result["candidates"] is None
    assert result["live_guard_passed"] is False
    assert result["error"]["kind"] == {
        "workspace_change": "workspace_changed",
        "auth": "authentication_error",
        "sandbox": "sandbox_unverified",
    }.get(fault, fault)


def test_exact_disabled_code_mode_notice_is_not_a_tool_call():
    data = recording()
    notice = {
        "type": "item.completed",
        "item": {"type": "error", "message": llm.DISABLED_CODE_MODE_NOTICE},
    }
    output = ProcessOutput(
        0, (json.dumps(notice) + "\n" + data["stdout"]).encode(), b""
    )
    payload, audit = llm.parse_events(output)
    assert payload["document_id"] == "normal"
    assert audit["tool_events"] == 0
    assert audit["disabled_tool_notices"] == ["code_mode_unavailable"]


def test_existing_output_prevents_live_invocation(monkeypatch, tmp_path, capsys):
    from change_scope.cli import main

    output = tmp_path / "existing.json"
    output.write_text("prior result")
    monkeypatch.setattr(
        llm, "smoke", lambda *a, **k: pytest.fail("Must not consume a live call")
    )
    assert main(["llm-smoke", "--live", "--output", str(output)]) == 4
    assert output.read_text() == "prior result"
    result = json.loads(capsys.readouterr().out)
    assert result["llm_mode"] == "not_used"
    assert result["extraction_status"] == "error"
