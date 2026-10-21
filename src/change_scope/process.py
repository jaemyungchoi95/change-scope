"""Bound subprocess I/O and lifetime, including inherited pipes and child processes."""

import math
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class ProcessError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


def run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    input_bytes: bytes = b"",
    timeout: float = 60,
    max_stdout: int = 262144,
    max_stderr: int = 65536,
    env: dict[str, str] | None = None,
) -> ProcessOutput:
    if os.name != "posix":
        raise ProcessError(
            "unsupported_platform", "POSIX process isolation is required"
        )
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise ProcessError(
            "invalid_limit", "Process timeout must be in (0, 600] seconds"
        )
    if max_stdout < 1 or max_stderr < 1 or len(input_bytes) > 65536:
        raise ProcessError("invalid_limit", "Invalid process input/output limit")
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise ProcessError("start_error", "Cannot start requested process") from exc
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": max_stdout, "stderr": max_stderr}
    deadline = time.monotonic() + timeout
    remaining_input = memoryview(input_bytes)
    try:
        for stream, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if input_bytes:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessError("timeout", "Process deadline exceeded")
            for key, _ in selector.select(min(remaining, 0.05)):
                if key.data == "stdin":
                    try:
                        count = os.write(key.fd, remaining_input[:4096])
                        remaining_input = remaining_input[count:]
                    except BrokenPipeError:
                        remaining_input = remaining_input[:0]
                    if not remaining_input:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                data = os.read(key.fd, 8192)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if len(buffers[key.data]) + len(data) > limits[key.data]:
                    raise ProcessError(
                        "output_limit", f"Process {key.data} exceeded byte limit"
                    )
                buffers[key.data].extend(data)
        return ProcessOutput(
            process.wait(), bytes(buffers["stdout"]), bytes(buffers["stderr"])
        )
    finally:
        # A process can exit while descendants retain its pipes: kill the entire session.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
