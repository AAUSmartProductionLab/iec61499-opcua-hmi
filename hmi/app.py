"""Flask application: JSON API plus the single page HMI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, url_for

from .service import HmiService

LOGGER = logging.getLogger(__name__)

SESSION_HEADER = "X-Session-Id"
MAX_LOG_LIMIT = 200


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def create_app(service: HmiService) -> Flask:
    app = Flask(__name__)
    app.config["service"] = service

    @app.context_processor
    def asset_urls() -> dict[str, Any]:
        """Static file URLs carry the file's timestamp, so an edit is picked up."""

        def asset(filename: str) -> str:
            path = Path(app.static_folder or "") / filename
            try:
                stamp = int(path.stat().st_mtime)
            except OSError:
                stamp = 0
            return url_for("static", filename=filename, v=stamp)

        return {"asset": asset}

    @app.get("/favicon.ico")
    def favicon() -> Any:
        return Response(status=204)

    def session_id() -> str:
        value = request.headers.get(SESSION_HEADER) or request.args.get("session", "")
        value = value.strip()
        if not value:
            raise ApiError(f"missing session id, send it in the {SESSION_HEADER} header")
        if len(value) > 128:
            raise ApiError("session id is too long")
        return value

    def body() -> dict[str, Any]:
        data = request.get_json(silent=True)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ApiError("the request body must be a JSON object")
        return data

    def module_key(data: dict[str, Any] | None = None) -> str:
        key = (data or {}).get("module") or request.args.get("module", "")
        if key not in service.modules:
            raise ApiError(f"unknown module '{key}'", status=404)
        return key

    @app.get("/")
    def index() -> Any:
        return render_template("hmi.html")

    @app.get("/api/config")
    def api_config() -> Any:
        return jsonify(service.client_config())

    @app.get("/api/snapshot")
    def api_snapshot() -> Any:
        limit = min(int(request.args.get("log", 60)), MAX_LOG_LIMIT)
        snapshot = service.snapshot(session_id())
        snapshot["log"] = service.log_entries(limit)
        return jsonify(snapshot)

    @app.post("/api/occupation")
    def api_occupation() -> Any:
        data = body()
        result = service.occupy(session_id(), module_key(data), str(data.get("action", "")))
        return jsonify(result)

    @app.post("/api/module-command")
    def api_module_command() -> Any:
        data = body()
        result = service.module_command(
            session_id(), module_key(data), str(data.get("command", ""))
        )
        return jsonify(result)

    @app.post("/api/skill-command")
    def api_skill_command() -> Any:
        data = body()
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise ApiError("params must be a JSON object")
        result = service.skill_command(
            session_id(),
            module_key(data),
            str(data.get("skill", "")),
            str(data.get("command", "")),
            params,
        )
        return jsonify(result)

    @app.get("/api/log")
    def api_log() -> Any:
        limit = min(int(request.args.get("limit", 60)), MAX_LOG_LIMIT)
        return jsonify({"entries": service.log_entries(limit)})

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError) -> Any:
        return jsonify({"error": error.message}), error.status

    @app.errorhandler(ValueError)
    def handle_value_error(error: ValueError) -> Any:
        return jsonify({"error": str(error)}), 400

    @app.errorhandler(404)
    def handle_not_found(error: Any) -> Any:
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(Exception)
    def handle_unexpected(error: Exception) -> Any:
        LOGGER.exception("unhandled error")
        return jsonify({"error": f"{type(error).__name__}: {error}"}), 500

    return app