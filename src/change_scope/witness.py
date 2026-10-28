"""T05: source-backed witness paths for engine impact candidates.

A witness is a path in the same unique, reviewed edge set the engine evaluated; it
is not a native proof tree or an enumeration of every derivation. For each candidate
there is at most one shortest path per first hop, so alternative routes stay
visible. The witnessed set must equal the engine's candidate set.
"""

from collections import deque

from .schemas import Snapshot


class WitnessError(RuntimeError):
    pass


def _shortest(
    successors: dict[str, list[str]], start: str, target: str, avoid: str
) -> list[str] | None:
    """Shortest simple path start -> target that never revisits `avoid`.

    Neighbours are expanded in sorted order, so ties resolve deterministically.
    """
    parents: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == target:
            path = [node]
            while (node := parents[node]) is not None:
                path.append(node)
            return path[::-1]
        for nxt in successors.get(node, []):
            if nxt not in parents and nxt != avoid:
                parents[nxt] = node
                queue.append(nxt)
    return None


def _support(fact: dict, sources: tuple[dict, ...]) -> dict:
    if "source_sha256" in fact:
        source = next(s for s in sources if s["sha256"] == fact["source_sha256"])
    elif "source_id" in fact:
        source = next(
            s
            for s in sources
            if (s["source_id"], s["source_version"])
            == (fact["source_id"], fact["source_version"])
        )
    else:
        # Reviewed snapshot v1 facts carry no source fields; v1 has one source.
        (source,) = sources
    return {
        "fact_id": fact.get("assertion_id") or fact["candidate_id"],
        "revision": fact["revision"],
        "source_id": source["source_id"],
        "source_version": source["source_version"],
        "source_sha256": source["sha256"],
        "section": fact["section"],
        "quote": fact["quote"],
    }


def explain(
    snapshot: Snapshot, target: str, candidates: list[str]
) -> tuple[dict, dict]:
    successors: dict[str, list[str]] = {}
    for src, dst in snapshot.edges:
        successors.setdefault(src, []).append(dst)
    for hops in successors.values():
        hops.sort()
    support: dict[tuple[str, str], list[dict]] = {}
    for fact in snapshot.assertions:
        support.setdefault((fact["subject"], fact["object"]), []).append(
            _support(fact, snapshot.sources)
        )
    witnesses: dict[str, list[dict]] = {}
    for service in sorted(snapshot.services - {target}):
        paths = []
        for first in successors.get(service, []):
            if first == service:
                continue
            rest = _shortest(successors, first, target, avoid=service)
            if rest is None:
                continue
            path = [service, *rest]
            paths.append(
                {
                    "via": first,
                    "length": len(path) - 1,
                    "path": path,
                    "hops": [
                        {"subject": a, "object": b, "support": support[(a, b)]}
                        for a, b in zip(path, path[1:], strict=False)
                    ],
                }
            )
        if paths:
            witnesses[service] = paths
    if sorted(witnesses) != sorted(candidates):
        raise WitnessError(
            f"Witness paths {sorted(witnesses)} do not match engine candidates "
            f"{sorted(candidates)}"
        )
    check = {
        "status": "consistent",
        "engine_upstream": sorted(candidates),
        "witnessed": sorted(witnesses),
        "method": "app BFS over the same unique edges; one shortest path per first hop",
    }
    return witnesses, check
