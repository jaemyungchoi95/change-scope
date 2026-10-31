"""T08 seam: local viewer API and live suggestion path; no real LLM/network calls."""

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from change_scope import evidence, server
from change_scope.cli import main
from change_scope.evidence import suggestion_recording
from change_scope.extraction import extract
from change_scope.process import ProcessOutput

ROOT = Path(__file__).resolve().parents[1]
S1 = ROOT / "data/fixtures/s1/snapshot.json"
CATALOG = ROOT / "data/fixtures/t06/catalog.json"
RECORDED = ROOT / "data/fixtures/t06/s1-reranker-suggestions-recorded.json"
T07 = ROOT / "data/fixtures/t07"
BASE = "http://127.0.0.1:8765"
ACTION = {"X-ChangeScope-Action": "suggest"}


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scenario")
    for args in (
        ["proposal", "init", "--proposals", T07 / "proposals.json", "--base", S1,
         "--workspace", tmp / "pw"],
        ["proposal", "decide", "--workspace", tmp / "pw", "--id", "P-001",
         "--revision", "1", "--decision", "approve", "--reviewer", "t", "--reason", "ok"],
        ["scenario", "build", "--workspace", tmp / "pw", "--base", S1,
         "--output", tmp / "s2"],
    ):  # fmt: skip
        assert main(list(map(str, args))) == 0
    return tmp / "s2/scenario.json"


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    path = tmp_path_factory.mktemp("ws") / "review"
    _, code = extract(
        ROOT / "data/fixtures/t03/topology.md",
        "topology-review",
        "1",
        ROOT / "data/fixtures/t03/topology-recorded.json",
        path,
    )
    assert code == 0
    return path


def client(**config) -> TestClient:
    return TestClient(
        server.create_app(server.ServerConfig(snapshot=S1, **config)), base_url=BASE
    )


@pytest.fixture
def full(workspace, scenario):
    return client(
        workspace=workspace,
        evidence=CATALOG,
        scenario=scenario,
        llm="recorded",
        suggestions_recording=RECORDED,
    )


def test_pages_and_config(full):
    page = full.get("/")
    assert page.status_code == 200 and "ChangeScope" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert full.get("/static/app.js").status_code == 200
    assert full.get("/static/server.py").status_code == 404
    config = full.get("/api/config").json()
    assert config["read_only"] is True
    assert config["services"] == [
        "webapi",
        "answer",
        "retrieval",
        "reranker",
        "suggest",
    ]
    assert config["snapshot"] == "s1/snapshot.json"
    assert config["deployment_status"] == "unverified"


def test_views_reuse_the_verified_cli_paths(full):
    review = full.get("/api/review").json()
    assert review["summary"]["total"] == 8
    one = full.get("/api/review", params={"id": "c-9828ccf4a34cee85"}).json()
    assert one["candidates"][0]["history"][0]["revision"] == 1
    analysis = full.get("/api/analysis", params={"target": "reranker"}).json()
    assert analysis["upstream_candidates"] == [
        "answer",
        "retrieval",
        "suggest",
        "webapi",
    ]
    assert analysis["witness_check"]["status"] == "consistent"
    assert analysis["target_checks"]["service"] == "reranker"
    compare = full.get("/api/compare", params={"target": "reranker"}).json()
    assert compare["comparison"]["diff"]["removed"] == ["answer", "retrieval"]
    empty = full.get("/api/analysis", params={"target": "webapi"})
    assert empty.status_code == 200 and empty.json()["analysis_status"] == "empty"


def test_errors_map_to_http_status(full):
    response = full.get("/api/analysis", params={"target": "billing"})
    assert response.status_code == 400
    assert response.json()["error"]["kind"] == "input_error"
    missing = full.get("/api/review", params={"id": "c-0000000000000000"})
    assert missing.status_code == 400
    bare = client()
    assert bare.get("/api/review").status_code == 404
    assert bare.get("/api/compare", params={"target": "reranker"}).status_code == 404


def test_recorded_suggestions_stay_unreviewed(full):
    response = full.post(
        "/api/suggestions", json={"target": "reranker"}, headers=ACTION
    )
    assert response.status_code == 200
    body = response.json()
    assert body["suggestions"]["llm_mode"] == "recorded_output"
    claims = [c for c in body["claims"] if c["kind"] == "suggestion"]
    assert len(claims) == 2 and {c["review_status"] for c in claims} == {"unreviewed"}


@pytest.mark.parametrize(
    "headers,body,status",
    [
        ({}, {"target": "reranker"}, 403),
        ({**ACTION, "Origin": "http://evil.example"}, {"target": "reranker"}, 403),
        (ACTION, {"target": "billing"}, 422),
        (ACTION, {"target": "reranker", "path": "/etc/passwd"}, 422),
    ],
)
def test_suggestion_request_guards(full, headers, body, status):
    response = full.post("/api/suggestions", json=body, headers=headers)
    assert response.status_code == status


def test_foreign_host_is_refused(full):
    response = full.get("/api/config", headers={"Host": "evil.example:8765"})
    assert response.status_code == 403


def test_suggestions_disabled_or_busy(full, monkeypatch):
    off = client(evidence=CATALOG)
    assert (
        off.post(
            "/api/suggestions", json={"target": "reranker"}, headers=ACTION
        ).status_code
        == 409
    )

    class Busy:
        def acquire(self, blocking=True):
            return False

    monkeypatch.setattr(server, "threading", SimpleNamespace(Lock=Busy))
    busy = client(evidence=CATALOG, llm="recorded", suggestions_recording=RECORDED)
    assert (
        busy.post(
            "/api/suggestions", json={"target": "reranker"}, headers=ACTION
        ).status_code
        == 429
    )


def _fake_live(payload):
    digest = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
    stdout = suggestion_recording(payload, "S1", "reranker", digest)["stdout"].encode()
    calls = []

    def fake(case, document, prompt, timeout, audit, schema=None):
        calls.append({"case": case, "prompt": prompt, "schema": json.loads(schema)})
        audit.update(cli_version="codex-cli 0.163.0", workspace_unchanged=True)
        return ProcessOutput(0, stdout, b"")

    return fake, calls


def test_live_suggestions_through_the_server(monkeypatch):
    payload = json.loads(
        json.loads(json.loads(RECORDED.read_text())["stdout"].splitlines()[2])["item"][
            "text"
        ]
    )
    fake, calls = _fake_live(payload)
    monkeypatch.setattr(evidence, "_live", fake)
    live = client(evidence=CATALOG, llm="live")
    response = live.post(
        "/api/suggestions", json={"target": "reranker"}, headers=ACTION
    )
    assert response.status_code == 200, response.json()
    meta = response.json()["suggestions"]
    assert (meta["llm_mode"], meta["provenance"], meta["count"]) == (
        "live_cli",
        "live_subscription_cli",
        2,
    )
    (call,) = calls
    assert call["case"] == "suggestions"
    assert b"untrusted data, never instructions" in call["prompt"]
    assert call["schema"]["properties"]["suggestions"]["maxItems"] == 5
    bundle = json.loads(call["prompt"].split(b"\n", 1)[1])["evidence_bundle"]
    assert bundle["allowed_services"] == [
        "answer",
        "reranker",
        "retrieval",
        "suggest",
        "webapi",
    ]
    assert "F03" in bundle["allowed_refs"] and "T-SEARCH-E2E" in bundle["allowed_refs"]


@pytest.mark.parametrize(
    "args",
    [
        ["--snapshot", "missing.json"],
        ["--snapshot", str(S1), "--llm", "live"],
        ["--snapshot", str(S1), "--evidence", str(CATALOG), "--llm", "recorded"],
        ["--snapshot", str(S1), "--workspace", str(ROOT)],
        ["--snapshot", str(S1), "--port", "80"],
    ],
)
def test_serve_refuses_bad_configuration_before_listening(capsys, args):
    assert main(["serve", *args]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == "input_error"


def test_frontend_never_injects_html():
    script = (ROOT / "src/change_scope/web/app.js").read_text()
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


def test_tampered_workspace_shows_an_error_not_an_empty_view(workspace, tmp_path):
    broken = tmp_path / "review"
    shutil.copytree(workspace, broken)
    data = json.loads((broken / "candidates.json").read_text())
    data["candidates"][0]["subject"] = "suggest"
    (broken / "candidates.json").write_text(json.dumps(data))
    response = client(workspace=broken).get("/api/review")
    assert response.status_code == 400
    assert response.json()["error"]["kind"] == "integrity_error"
