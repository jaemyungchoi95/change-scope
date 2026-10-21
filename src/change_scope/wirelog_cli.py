"""T01a: explicit upstream Wirelog CLI evaluation, compared with PyreWire."""

import json
import re
import subprocess
import tempfile
from pathlib import Path

from .engine import EngineError, rules_bytes, rules_hash, upstream_candidates
from .schemas import InputError, load_snapshot, sha256


def program_bytes(edges: tuple[tuple[str, str], ...], target: str) -> bytes:
    # This narrow exporter is for the synthetic service IDs, not arbitrary Datalog.
    if any(
        not re.fullmatch(r"[a-z][a-z0-9_-]*", value)
        for value in [target, *(v for edge in edges for v in edge)]
    ):
        raise InputError("Wirelog CLI fixture IDs must be lowercase ASCII identifiers")
    facts = "".join(
        f"depends_on({json.dumps(src)}, {json.dumps(dst)}).\n"
        for src, dst in sorted(set(edges))
    )
    return (
        rules_bytes()
        + (facts + f"target({json.dumps(target)}).\n.output upstream\n").encode()
    )


def _execute(argv: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, ValueError, OverflowError, subprocess.TimeoutExpired) as exc:
        raise EngineError(
            f"Wirelog CLI execution failed: {type(exc).__name__}"
        ) from exc
    if result.returncode:
        raise EngineError(
            f"Wirelog CLI exited with code {result.returncode}: {result.stderr[:1000]}"
        )
    return result


def verify(
    snapshot_path: Path, target: str, binary: Path, timeout: float
) -> tuple[dict, int]:
    result = {
        "schema_version": 1,
        "analysis_status": "error",
        "engine_mode": "unavailable",
        "llm_mode": "not_used",
        "deployment_status": "unverified",
        "target": target,
        "upstream_candidates": None,
        "parity": None,
    }
    try:
        snapshot = load_snapshot(snapshot_path)
        if target not in snapshot.services:
            raise InputError(f"Unknown target: {target}")
        program = program_bytes(snapshot.edges, target)
        result.update(
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_hash,
            rules_hash=rules_hash(),
            program_sha256=sha256(program),
            unique_relation_count=len(snapshot.edges),
        )
        binary = binary.resolve(strict=True)
        result["binary_sha256"] = sha256(binary.read_bytes())
        with tempfile.TemporaryDirectory(prefix="change-scope-wirelog-") as directory:
            cwd = Path(directory)
            version_result = _execute([str(binary), "--version"], cwd, timeout)
            if not re.fullmatch(
                r"wirelog_cli \d+\.\d+\.\d+\S*\n?", version_result.stdout
            ):
                raise EngineError("Unrecognized Wirelog CLI version output")
            result.update(
                engine_mode="live_engine", cli_version=version_result.stdout.strip()
            )
            program_path = cwd / "snapshot.dl"
            program_path.write_bytes(program)
            command = [str(binary), str(program_path)]
            evaluated = _execute(command, cwd, timeout)
            candidates = set()
            for line in evaluated.stdout.splitlines():
                match = re.fullmatch(r'upstream\("([a-z][a-z0-9_-]*)"\)', line)
                if not match or match[1] not in snapshot.services or match[1] == target:
                    raise EngineError("Invalid Wirelog CLI upstream output")
                candidates.add(match[1])
            expected = upstream_candidates(
                snapshot.edges, target, snapshot.services, timeout
            )
            if sorted(candidates) != expected:
                raise EngineError(
                    "Wirelog CLI and PyreWire returned different candidate sets"
                )
            result.update(
                analysis_status="success" if candidates else "empty",
                upstream_candidates=sorted(candidates),
                parity=True,
                pyrewire_candidates=expected,
                command=command,
                program=program.decode(),
                stdout=evaluated.stdout,
                stderr=evaluated.stderr,
                returncode=evaluated.returncode,
            )
        return result, 0
    except InputError as exc:
        result["error"] = {"kind": "input_error", "message": str(exc)}
        return result, 2
    except (EngineError, OSError, ValueError, RuntimeError) as exc:
        result["error"] = {"kind": "engine_error", "message": str(exc)}
        return result, 3
