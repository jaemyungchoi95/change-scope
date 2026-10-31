"""T06 seam: explicit tests/documents, claim kinds and recorded suggestions.

No LLM/network calls; PyreWire runs for real.
"""

import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from change_scope import evidence
from change_scope.cli import main
from change_scope.evidence import suggestion_recording
from change_scope.process import ProcessOutput

ROOT = Path(__file__).resolve().parents[1]
S1 = ROOT / "data/fixtures/s1/snapshot.json"
GOLDEN_V1 = ROOT / "data/fixtures/t03/reviewed-snapshot-v1/snapshot.json"
T06 = ROOT / "data/fixtures/t06"
CATALOG = T06 / "catalog.json"
RECORDED = T06 / "s1-reranker-suggestions-recorded.json"


def analyze(capsys, *args, snapshot=S1, target="reranker"):
    code = main(
        ["analyze", "--snapshot", str(snapshot), "--target", target, *map(str, args)]
    )
    return code, json.loads(capsys.readouterr().out)


def test_tests_documents_claims_and_report(capsys, tmp_path):
    report = tmp_path / "report.md"
    code, result = analyze(
        capsys, "--evidence", CATALOG, "--suggestions", RECORDED, "--report", report
    )
    assert code == 0, result
    assert [t["doc_id"] for t in result["target_checks"]["tests"]] == [
        "T-RERANK-CONTRACT"
    ]
    linked = {
        service: (
            [t["doc_id"] for t in found["tests"]],
            [d["doc_id"] for d in found["documents"]],
        )
        for service, found in result["candidate_evidence"].items()
    }
    assert linked == {
        "answer": (["T-SEARCH-E2E"], []),
        "retrieval": (["T-SEARCH-E2E"], []),
        "suggest": ([], ["D-SUGGEST-DESIGN"]),
        "webapi": (["T-SEARCH-E2E", "T-WEBAPI-SMOKE"], []),
    }
    # A candidate without linked tests stays a candidate and is marked unverified.
    assert "suggest" in result["upstream_candidates"]
    claims = result["claims"]
    assert Counter(c["kind"] for c in claims) == {
        "source_fact": 11,
        "rule_conclusion": 5,
        "suggestion": 2,
        "unverified": 7,
    }
    assert {
        "kind": "unverified",
        "text": "suggest has no linked test document in the catalog",
        "refs": [],
    } in claims
    suggestions = [c for c in claims if c["kind"] == "suggestion"]
    assert all(
        (c["review_status"], c["llm_mode"], c["provenance"])
        == ("unreviewed", "recorded_output", "synthetic_test_double")
        for c in suggestions
    )
    assert result["suggestions"]["count"] == 2
    assert result["llm_mode"] == "not_used"  # Extraction LLM; suggestions are labelled.
    rule = next(c for c in claims if c["kind"] == "rule_conclusion")
    assert rule["text"] == (
        "answer is an upstream check candidate for reranker via "
        "answer -> retrieval -> reranker"
    )
    assert rule["refs"] == ["F02", "F03"]
    text = report.read_text()
    assert result["report"] == str(report)
    for heading in (
        "## Target checks: reranker",
        "## Upstream candidates (4)",
        "## 원문 사실 (Source facts)",
        "## 규칙 결론 (Rule conclusions)",
        "## 추가 제안 (Suggestions, unreviewed)",
        "## 미확인 (Unverified)",
    ):
        assert heading in text
    assert (
        '- test T-RERANK-CONTRACT Reranker scoring contract test: "sends scoring'
        in text
    )
    assert "- path via suggest: webapi -> suggest -> reranker" in text

    before = report.read_bytes()
    code, again = analyze(capsys, "--evidence", CATALOG, "--report", report)
    assert code == 4 and again["error"]["kind"] == "output_error"
    assert again["upstream_candidates"] is None
    assert report.read_bytes() == before


def test_target_checks_survive_an_empty_upstream_result(capsys, tmp_path):
    code, result = analyze(
        capsys, "--evidence", CATALOG, "--report", tmp_path / "r.md", target="webapi"
    )
    assert code == 0, result
    assert result["analysis_status"] == "empty"
    assert result["upstream_candidates"] == []
    assert [t["doc_id"] for t in result["target_checks"]["tests"]] == [
        "T-SEARCH-E2E",
        "T-WEBAPI-SMOKE",
    ]
    assert result["suggestions"] == {"llm_mode": "not_used"}
    assert "- (none: valid empty result)" in (tmp_path / "r.md").read_text()


def test_without_evidence_the_t05_result_is_unchanged(capsys):
    code, result = analyze(capsys)
    assert code == 0
    for key in ("target_checks", "candidate_evidence", "claims", "suggestions"):
        assert key not in result


@pytest.mark.parametrize(
    "args", [("--suggestions", RECORDED), ("--report", "unused.md")]
)
def test_suggestions_and_report_require_evidence(capsys, args):
    code, result = analyze(capsys, *args)
    assert code == 2 and result["error"]["kind"] == "input_error"


def test_reviewed_snapshot_claims_cite_candidates(capsys):
    code, result = analyze(capsys, "--evidence", CATALOG, snapshot=GOLDEN_V1)
    assert code == 0, result
    facts = [c for c in result["claims"] if c["kind"] == "source_fact"]
    assert {"c-c3e0780f7d7dbc49"} <= {r for c in facts for r in c["refs"]}


@pytest.fixture
def catalog_copy(tmp_path):
    copy = tmp_path / "t06"
    shutil.copytree(T06, copy)
    return copy


def _edit(copy, change):
    path = copy / "catalog.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data, indent=2))


def _link(data, link_id):
    return next(link for link in data["links"] if link["link_id"] == link_id)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda c: _edit(c, lambda d: _link(d, "L-RERANK").update(quote="not there")),
        lambda c: _edit(c, lambda d: _link(d, "L-RERANK").update(section="Missing")),
        lambda c: _edit(c, lambda d: _link(d, "L-RERANK").update(service="billing")),
        lambda c: _edit(c, lambda d: _link(d, "L-RERANK").update(doc_id="T-NOPE")),
        lambda c: _edit(
            c, lambda d: _link(d, "L-RERANK").update(doc_id="D-SUGGEST-DESIGN")
        ),
        lambda c: _edit(
            c, lambda d: _link(d, "L-RERANK").update(review_status="pending")
        ),
        lambda c: _edit(c, lambda d: d["documents"][0].update(path="../../outside.md")),
        lambda c: _edit(c, lambda d: d["documents"].append(dict(d["documents"][0]))),
        lambda c: _edit(c, lambda d: d.update(fixture=False)),
        lambda c: (c / "docs/reranker-contract.md").write_text("edited\n"),
    ],
    ids=[
        "quote",
        "section",
        "unknown-service",
        "unknown-doc",
        "covers-design-doc",
        "unreviewed-link",
        "path-escape",
        "duplicate-doc",
        "not-fixture",
        "doc-hash",
    ],
)
def test_bad_catalog_is_input_error(capsys, catalog_copy, tmp_path, tamper):
    tamper(catalog_copy)
    report = tmp_path / "r.md"
    code, result = analyze(
        capsys, "--evidence", catalog_copy / "catalog.json", "--report", report
    )
    assert code == 2 and result["error"]["kind"] == "input_error"
    assert result["upstream_candidates"] is None
    assert not report.exists()


def _recording(tmp_path, payload, target="reranker", catalog=CATALOG):
    digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
    path = tmp_path / "suggestions.json"
    path.write_text(json.dumps(suggestion_recording(payload, "S1", target, digest)))
    return path


def _payload(**change):
    item = {"service": "suggest", "text": "Add a test.", "refs": ["F05"]} | change
    return {"target": "reranker", "suggestions": [item]}


@pytest.mark.parametrize(
    "payload,target,kind",
    [
        (_payload(refs=["F99"]), "reranker", "schema_error"),
        (_payload(refs=[]), "reranker", "schema_error"),
        (_payload(text="ok\x1b[31m"), "reranker", "schema_error"),
        (_payload(service="billing"), "reranker", "schema_error"),
        (_payload() | {"target": "webapi"}, "reranker", "schema_error"),
        (
            {"target": "reranker", "suggestions": [_payload()["suggestions"][0]] * 6},
            "reranker",
            "schema_error",
        ),
        (_payload() | {"extra": 1}, "reranker", "schema_error"),
        # Bound to reranker but replayed for webapi.
        (_payload(), "webapi", "recording_error"),
    ],
)
def test_bad_suggestions_fail_the_whole_result(capsys, tmp_path, payload, target, kind):
    recording = _recording(tmp_path, payload)
    report = tmp_path / "r.md"
    code, result = analyze(
        capsys,
        "--evidence",
        CATALOG,
        "--suggestions",
        recording,
        "--report",
        report,
        target=target,
    )
    assert code == 3
    assert result["analysis_status"] == "error"
    assert result["error"]["kind"] == kind
    assert result["upstream_candidates"] is None
    assert "claims" not in result
    assert not report.exists()


def test_suggestion_outside_the_result_is_refused(capsys, tmp_path):
    payload = {"target": "webapi", "suggestions": [_payload()["suggestions"][0]]}
    recording = _recording(tmp_path, payload, target="webapi")
    code, result = analyze(
        capsys, "--evidence", CATALOG, "--suggestions", recording, target="webapi"
    )
    assert code == 3
    assert "outside the result" in result["error"]["message"]


def test_missing_suggestions_file_is_input_error(capsys, tmp_path):
    code, result = analyze(
        capsys, "--evidence", CATALOG, "--suggestions", tmp_path / "missing.json"
    )
    assert code == 2 and result["error"]["kind"] == "input_error"


def test_report_neutralizes_control_characters(capsys, catalog_copy, tmp_path):
    _edit(
        catalog_copy,
        lambda d: d["documents"][0].update(title="Contract\x1b[31m `x`"),
    )
    report = tmp_path / "r.md"
    code, result = analyze(
        capsys, "--evidence", catalog_copy / "catalog.json", "--report", report
    )
    assert code == 0, result
    text = report.read_text()
    assert "\x1b" not in text and "`" not in text


def _fake_live(payload):
    digest = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
    stdout = suggestion_recording(payload, "S1", "reranker", digest)["stdout"].encode()

    def fake(case, document, prompt, timeout, audit, schema=None):
        audit.update(cli_version="codex-cli 0.163.0", workspace_unchanged=True)
        return ProcessOutput(0, stdout, b"")

    return fake, []


def test_live_output_citing_unknown_evidence_is_rejected(monkeypatch, capsys):
    fake, _ = _fake_live(
        {
            "target": "reranker",
            "suggestions": [{"service": "suggest", "text": "x", "refs": ["F99"]}],
        }
    )
    monkeypatch.setattr(evidence, "_live", fake)
    code = main(
        ["analyze", "--snapshot", str(S1), "--target", "reranker", "--evidence",
         str(CATALOG), "--live-suggestions"]
    )  # fmt: skip
    result = json.loads(capsys.readouterr().out)
    assert code == 3 and result["error"]["kind"] == "schema_error"
    assert result["upstream_candidates"] is None


def test_live_and_recorded_suggestions_are_exclusive(capsys):
    code = main(
        ["analyze", "--snapshot", str(S1), "--target", "reranker", "--evidence",
         str(CATALOG), "--live-suggestions", "--suggestions", str(RECORDED)]
    )  # fmt: skip
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == "input_error"
