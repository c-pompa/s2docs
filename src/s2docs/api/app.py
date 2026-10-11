"""FastAPI application factory: JSON API + static frontend + MCP over HTTP."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from s2docs.api.routes import router
from s2docs.build.validate_script import ToolchainMissing
from s2docs.config import get_settings
from s2docs.db.connection import Database, DatabaseNotInitialized
from s2docs.db.repository import NotFound
from s2docs.prompts import PromptError


def create_app(db_path: Path | None = None, *, mount_mcp: bool = True) -> FastAPI:
    db = Database(db_path or get_settings().db_path)

    mcp_server = None
    mcp_asgi = None
    if mount_mcp:
        from s2docs.mcp_server import create_mcp_server

        mcp_server = create_mcp_server(db)
        # Build the MCP ASGI app now so the session manager exists for the lifespan.
        mcp_asgi = mcp_server.streamable_http_app(streamable_http_path="/", stateless_http=True)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if mcp_server is not None:
            async with mcp_server.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(
        title="Source 2 Docs",
        version="0.1.0",
        description="Console variables, Hammer entities, schema, game events and script APIs for Source 2 games.",
        lifespan=lifespan,
    )
    app.state.db = db

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(DatabaseNotInitialized)
    async def _no_db(_: Request, exc: DatabaseNotInitialized) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(ToolchainMissing)
    async def _no_toolchain(_: Request, exc: ToolchainMissing) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(PromptError)
    @app.exception_handler(ValueError)
    async def _bad_request(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    app.include_router(router)
    if mcp_asgi is not None:
        app.mount("/mcp", mcp_asgi)
        app.add_middleware(_McpPathAlias)
    web_dir = resources.files("s2docs") / "web"
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app


class _McpPathAlias:
    """Serve MCP at both `/mcp` and `/mcp/` (clients are configured either way).

    Pure ASGI (not BaseHTTPMiddleware) so streamed MCP responses pass through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)
