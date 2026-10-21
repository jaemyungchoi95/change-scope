"""Exercise real subprocess boundaries without model/network calls."""

import sys
import time

import pytest

from change_scope.process import ProcessError, run_bounded


def call(tmp_path, code, **options):
    return run_bounded([sys.executable, "-c", code], cwd=tmp_path, **options)


def test_bounded_stdin_stdout_stderr(tmp_path):
    result = call(
        tmp_path,
        'import sys; print(sys.stdin.read()); print("diag",file=sys.stderr)',
        input_bytes=b"payload",
    )
    assert result.returncode == 0
    assert result.stdout == b"payload\n"
    assert result.stderr == b"diag\n"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_flood_is_bounded(tmp_path, stream):
    with pytest.raises(ProcessError) as error:
        call(
            tmp_path,
            f'import sys; sys.{stream}.write("x"*1000000)',
            max_stdout=64,
            max_stderr=64,
        )
    assert error.value.kind == "output_limit"


def test_timeout_kills_children_holding_pipes(tmp_path):
    start = time.monotonic()
    with pytest.raises(ProcessError) as error:
        call(
            tmp_path,
            'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])',
            timeout=0.2,
        )
    assert error.value.kind == "timeout"
    assert time.monotonic() - start < 5


def test_early_exit_with_unread_stdin_does_not_hang(tmp_path):
    result = call(tmp_path, "raise SystemExit(4)", input_bytes=b"x" * 65536, timeout=2)
    assert result.returncode == 4


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 1e308])
def test_invalid_limit(tmp_path, timeout):
    with pytest.raises(ProcessError) as error:
        call(tmp_path, "pass", timeout=timeout)
    assert error.value.kind == "invalid_limit"
