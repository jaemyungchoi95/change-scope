"""T08: local read-only review and impact viewer with one explicit LLM action.

The server binds to 127.0.0.1 only and serves files fixed at startup; clients never
pass paths. Every view re-runs the same verified CLI functions (`review show`,
`analyze`, `compare`), so the screens show exactly what the CLI would. The only
write-like action is generating suggestions, which stay unreviewed proposals.
"""

import argparse
import threading
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from .extraction import Service

HOST = "127.0.0.1"
ACTION_HEADER = "x-changescope-action"
_STATUS = {0: 200, 2: 400, 3: 502, 4: 500}


@dataclass(frozen=True)
class ServerConfig:
    snapshot: Path
    workspace: Path | None = None
    evidence: Path | None = None
    scenario: Path | None = None
    llm: Literal["live", "recorded", "off"] = "off"
    suggestions_recording: Path | None = None
    port: int = 8765


class SuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: Service


def _respond(result: dict, code: int) -> JSONResponse:
    return JSONResponse(result, status_code=_STATUS.get(code, 500))


def _error(kind: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"schema_version": 1, "error": {"kind": kind, "message": message}},
        status_code=status,
    )


def _analysis_args(config: ServerConfig, target: str, **extra) -> argparse.Namespace:
    base = {
        "snapshot": config.snapshot,
        "target": target,
        "timeout": 30.0,
        "evidence": config.evidence,
        "suggestions": None,
        "live_suggestions": False,
        "report": None,
    }
    return argparse.Namespace(**(base | extra))


def create_app(config: ServerConfig) -> FastAPI:
    from . import review
    from .cli import run_analysis, run_compare
    from .reviewed import SERVICES

    app = FastAPI(
        title="ChangeScope local viewer",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    web = files("change_scope").joinpath("web")
    allowed_hosts = {f"{HOST}:{config.port}", f"localhost:{config.port}"}
    llm_lock = threading.Lock()

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Refuse other Host headers so a rebinding page cannot reach the API.
        if request.headers.get("host") not in allowed_hosts:
            return _error("forbidden", "Local viewer accepts localhost only", 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(web.joinpath("index.html")))

    @app.get("/static/{name}")
    def static(name: str):
        if name not in ("app.js", "style.css"):
            return _error("not_found", "Unknown asset", 404)
        return FileResponse(str(web.joinpath(name)))

    @app.get("/api/config")
    def api_config() -> dict:
        return {
            "schema_version": 1,
            "read_only": True,
            "services": SERVICES,
            "snapshot": f"{config.snapshot.parent.name}/{config.snapshot.name}",
            "has_workspace": config.workspace is not None,
            "has_evidence": config.evidence is not None,
            "has_scenario": config.scenario is not None,
            "llm": config.llm,
            "deployment_status": "unverified",
        }

    @app.get("/api/review")
    def api_review(id: str | None = None):
        if config.workspace is None:
            return _error("not_configured", "Start the server with --workspace", 404)
        return _respond(*review.show(config.workspace, id))

    @app.get("/api/analysis")
    def api_analysis(target: str):
        return _respond(*run_analysis(_analysis_args(config, target)))

    @app.get("/api/compare")
    def api_compare(target: str):
        if config.scenario is None:
            return _error("not_configured", "Start the server with --scenario", 404)
        args = argparse.Namespace(
            base=config.snapshot,
            scenario=config.scenario,
            target=target,
            evidence=config.evidence,
            report=None,
            timeout=30.0,
        )
        return _respond(*run_compare(args))

    @app.post("/api/suggestions")
    def api_suggestions(
        body: SuggestionRequest,
        request: Request,
        action: str | None = Header(default=None, alias=ACTION_HEADER),
    ):
        origin = request.headers.get("origin")
        if action != "suggest" or (
            origin is not None and origin.split("://", 1)[-1] not in allowed_hosts
        ):
            return _error("forbidden", "Suggestions need the viewer's own request", 403)
        if config.llm == "off" or config.evidence is None:
            return _error(
                "not_configured", "Suggestions are disabled for this server", 409
            )
        if not llm_lock.acquire(blocking=False):
            return _error("busy", "A suggestion request is already running", 429)
        try:
            extra = (
                {"live_suggestions": True}
                if config.llm == "live"
                else {"suggestions": config.suggestions_recording}
            )
            return _respond(*run_analysis(_analysis_args(config, body.target, **extra)))
        finally:
            llm_lock.release()

    return app


def serve(config: ServerConfig) -> int:
    import uvicorn

    uvicorn.run(create_app(config), host=HOST, port=config.port, log_level="info")
    return 0
