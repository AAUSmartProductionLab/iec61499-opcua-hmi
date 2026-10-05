"""FastAPI application: JSON API and the single page HMI.

The service runs on the application's event loop; the lifespan starts and
stops it unless the caller manages it (``manage=False``).
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

from .service import HmiService

LOGGER = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
SESSION_HEADER = "X-Session-Id"
MAX_LOG_LIMIT = 200


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def asset(filename: str) -> str:
    """Static file URLs carry the file's timestamp, so an edit is picked up."""
    try:
        stamp = int((HERE / "static" / filename).stat().st_mtime)
    except OSError:
        stamp = 0
    return f"/static/{filename}?v={stamp}"


def check_session(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        raise ApiError(f"missing session id, send it in the {SESSION_HEADER} header")
    if len(value) > 128:
        raise ApiError("session id is too long")
    return value


def create_app(service: HmiService, manage: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        if manage:
            await service.start()
        try:
            yield
        finally:
            if manage:
                await service.stop()

    app = FastAPI(title="OPC UA HMI", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.service = service
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["asset"] = asset

    def session_id(request: Request) -> str:
        return check_session(request.headers.get(SESSION_HEADER) or request.query_params.get("session"))

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            return {}
        if not isinstance(data, dict):
            raise ApiError("the request body must be a JSON object")
        return data

    def module_key(request: Request, data: dict[str, Any] | None = None) -> str:
        key = (data or {}).get("module") or request.query_params.get("module", "")
        if key not in service.modules:
            raise ApiError(f"unknown module '{key}'", status=404)
        return key

    def limit(request: Request, name: str) -> int:
        try:
            return max(0, min(int(request.query_params.get(name, 60)), MAX_LOG_LIMIT))
        except ValueError:
            raise ApiError(f"{name} must be a number") from None

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        return templates.TemplateResponse(request, "hmi.html")

    @app.get("/api/config")
    async def api_config() -> dict[str, Any]:
        return service.client_config()

    @app.get("/api/snapshot")
    async def api_snapshot(request: Request) -> dict[str, Any]:
        snapshot = service.snapshot(session_id(request))
        snapshot["log"] = service.log_entries(limit(request, "log"))
        return snapshot

    @app.post("/api/occupation")
    async def api_occupation(request: Request) -> dict[str, Any]:
        data = await body(request)
        return await service.occupy(session_id(request), module_key(request, data), str(data.get("action", "")))

    @app.post("/api/module-command")
    async def api_module_command(request: Request) -> dict[str, Any]:
        data = await body(request)
        return await service.module_command(session_id(request), module_key(request, data),
                                            str(data.get("command", "")))

    @app.post("/api/skill-command")
    async def api_skill_command(request: Request) -> dict[str, Any]:
        data = await body(request)
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise ApiError("params must be a JSON object")
        return await service.skill_command(session_id(request), module_key(request, data),
                                           str(data.get("skill", "")), str(data.get("command", "")), params)

    @app.get("/api/log")
    async def api_log(request: Request) -> dict[str, Any]:
        return {"entries": service.log_entries(limit(request, "limit"))}

    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, error: ApiError) -> JSONResponse:
        return JSONResponse({"error": error.message}, status_code=error.status)

    @app.exception_handler(ValueError)
    async def handle_value_error(_: Request, error: ValueError) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=400)

    @app.exception_handler(HTTPException)
    async def handle_http_error(_: Request, error: HTTPException) -> JSONResponse:
        text = "not found" if error.status_code == 404 else str(error.detail)
        return JSONResponse({"error": text}, status_code=error.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, error: Exception) -> JSONResponse:
        LOGGER.exception("unhandled error")
        return JSONResponse({"error": f"{type(error).__name__}: {error}"}, status_code=500)

    return app
