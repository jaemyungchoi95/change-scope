"""One fresh native-engine process/session per full snapshot evaluation."""

import json
import subprocess
import sys
from importlib.metadata import version
from importlib.resources import files

from .schemas import sha256

ENGINE_VERSION = "1.1.2"


class EngineError(RuntimeError):
    pass


def rules_bytes() -> bytes:
    return files("change_scope").joinpath("rules/impact.dl").read_bytes()


def rules_hash() -> str:
    return sha256(rules_bytes())


def upstream_candidates(
    edges: tuple[tuple[str, str], ...],
    target: str,
    services: frozenset[str],
    timeout: float,
) -> list[str]:
    request = json.dumps({"edges": edges, "target": target})
    try:
        result = subprocess.run(
            [sys.executable, "-m", "change_scope.engine"],
            input=request,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EngineError(f"PyreWire snapshot timed out after {timeout:g}s") from exc
    except (OSError, OverflowError) as exc:
        raise EngineError(f"Cannot execute engine: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:1000]
        raise EngineError(f"Engine exited with code {result.returncode}: {detail}")
    try:
        output = json.loads(result.stdout)
    except ValueError as exc:
        raise EngineError("Engine returned invalid JSON") from exc
    if not isinstance(output, list) or any(
        not isinstance(s, str) or s not in services or s == target for s in output
    ):
        raise EngineError("Engine returned invalid service IDs")
    return sorted(set(output))


def _worker() -> int:
    try:
        if version("pyrewire") != ENGINE_VERSION:
            raise EngineError(f"Expected PyreWire {ENGINE_VERSION}")
        from pyrewire import EasySession

        request = json.load(sys.stdin)
        with EasySession(rules_bytes().decode("utf-8")) as session:
            for src, dst in sorted(set(map(tuple, request["edges"]))):
                session.insert("depends_on", [src, dst])
            session.insert("target", [request["target"]])
            rows = session.snapshot("upstream")
        if any(len(row) != 1 or not isinstance(row[0], str) for row in rows):
            raise EngineError("Invalid upstream relation from PyreWire")
        print(json.dumps(sorted({row[0] for row in rows})))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_worker())
