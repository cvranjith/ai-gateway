#!/usr/bin/env python3
"""
ai-gateway — one stable, personal REST endpoint for multiple small
AI-backed services, dispatched by service_id so the endpoint itself
never has to change as new services are added.

Run standalone (for local testing):
    /opt/homebrew/opt/python@3.10/bin/python3.10 gateway.py

Normally started/stopped as part of
/Users/ranjithcv/Documents/code/claude/ollama/local-llm.sh, which also
brings up Caddy (reverse-proxying /gateway/* here) and Tailscale Funnel
(which exposes it publicly — see that script's own comments).

Contract:
    POST /invoke
    { "service_id": "<id>", "params": { ...service-specific... } }

    200 -> { "service_id": "<id>", "result": {...} }
    4xx/5xx -> { "error": "..." }

    GET /health -> { "status": "ok", "services": [...] }

Auth: /invoke requires OAuth2 Client Credentials — see auth.py and
README.md. Get a token from POST /oauth/token, then send it as
`Authorization: Bearer <token>` on every /invoke call.

Adding a new service: write a new module under services/ exposing
handle(params: dict) -> dict (raising services.errors.ServiceError for
any failure), then add one line to the SERVICES registry below. No
other change is needed — the endpoint, request/response shape, and
error handling all stay exactly the same.
"""

from flask import Flask, jsonify, request

from auth import issue_token, require_auth
from services.errors import ServiceError
from services import youtube_summarizer

SERVICES = {
    "youtube_summarizer": youtube_summarizer.handle,
}

app = Flask(__name__)


@app.route("/oauth/token", methods=["POST"])
def oauth_token():
    body = request.get_json(silent=True) or {}
    client_id = body.get("client_id")
    client_secret = body.get("client_secret")

    if not client_id or not client_secret:
        return jsonify({
            "error": "invalid_request",
            "error_description": "missing client_id or client_secret",
        }), 400

    token_response = issue_token(client_id, client_secret)
    if token_response is None:
        return jsonify({"error": "invalid_client"}), 401

    return jsonify(token_response)


@app.route("/invoke", methods=["POST"])
@require_auth
def invoke():
    body = request.get_json(silent=True) or {}
    service_id = body.get("service_id")
    params = body.get("params") or {}

    if not service_id:
        return jsonify({"error": "missing 'service_id'"}), 400

    handler = SERVICES.get(service_id)
    if handler is None:
        return jsonify({"error": f"unknown service_id '{service_id}' - known: {sorted(SERVICES)}"}), 400

    if not isinstance(params, dict):
        return jsonify({"error": "'params' must be an object"}), 400

    try:
        result = handler(params)
    except ServiceError as e:
        return jsonify({"error": e.message}), e.status_code
    except Exception as e:
        return jsonify({"error": f"internal error: {e}"}), 500

    return jsonify({"service_id": service_id, "result": result})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "services": sorted(SERVICES)})


if __name__ == "__main__":
    # 0.0.0.0 so this is reachable from Caddy/other local processes, not
    # just localhost-from-this-same-process.
    app.run(host="0.0.0.0", port=8788)
