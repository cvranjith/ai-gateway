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

    GET /files/<name> -> the file itself (unauthenticated - see below)

Auth: /invoke and /api/config require OAuth2 Client Credentials — see
auth.py and README.md. Get a token from POST /oauth/token, then send
it as `Authorization: Bearer <token>` on every call.

Config: per-service and gateway-wide parameters (e.g. which model a
service's Codex call uses) live in config.properties, read fresh on
every request by config.py — see that module and services/*.py for how
a service reads its own settings. GET/POST /api/config exposes it for
the /ui web dashboard, which also has a "test the endpoint" panel that
just calls /invoke with the same token, and a client-management panel
(GET/POST /api/clients, DELETE /api/clients/<id>) backed by auth.py.
On first-ever startup with no clients registered, one is auto-created
and printed to this process's own log — see auth.ensure_bootstrap_client().

Adding a new service: write a new module under services/ exposing
handle(params: dict) -> dict (raising services.errors.ServiceError for
any failure), then add one line to the SERVICES registry below. No
other change is needed — the endpoint, request/response shape, and
error handling all stay exactly the same.
"""

import re
from functools import wraps
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

import config as gateway_config
from auth import (
    create_client,
    delete_client,
    ensure_bootstrap_client,
    issue_token,
    list_clients,
    require_auth,
    verify_client_token,
)
from services.errors import ServiceError
from services import youtube_summarizer
from services import youtube_download
from services import mac_deploy
from services import deepsink_transcribe
from services import deepsink_notes
from services import deepsink_articulate
from services import deepsink_diarize
from services.youtube_download import FILES_DIR
from deepsink_sessions import bp as deepsink_sessions_bp
from user_auth import ensure_bootstrap_user, verify_user_token

SERVICES = {
    "youtube_summarizer": youtube_summarizer.handle,
    "youtube_download": youtube_download.handle,
    "mac_deploy": mac_deploy.handle,
    "deepsink_transcribe": deepsink_transcribe.handle,
    "deepsink_notes": deepsink_notes.handle,
    "deepsink_articulate": deepsink_articulate.handle,
    "deepsink_diarize": deepsink_diarize.handle,
}

CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

app = Flask(__name__)
app.register_blueprint(deepsink_sessions_bp)
ensure_bootstrap_client()
ensure_bootstrap_user()


@app.route("/ui")
def ui():
    return app.send_static_file("index.html")


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


def require_client_or_user(view):
    """Route decorator for /invoke: accepts either an ai-gateway OAuth2
    Client Credentials token (auth.py - other apps, e.g. yt-run's own
    registered client) or a DeepSink user token (user_auth.py). Added so
    DeepSink itself no longer needs a separate registered client just to
    call mac_deploy/deepsink_articulate, on top of the user login it
    already needs for its session data - one password, one JWT, for
    everything DeepSink calls here. Sets request.deepsink_user_id to the
    user_id for a user token, None for a client-credentials token, in
    case a handler ever wants to know which kind of caller this was."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "invalid_token", "error_description": "missing bearer token"}), 401

        token = header[len("Bearer "):].strip()

        if verify_client_token(token) is not None:
            request.deepsink_user_id = None
            return view(*args, **kwargs)

        user_payload = verify_user_token(token)
        if user_payload is not None:
            request.deepsink_user_id = user_payload["sub"]
            return view(*args, **kwargs)

        return jsonify({"error": "invalid_token", "error_description": "token is invalid or expired"}), 401

    return wrapped


@app.route("/invoke", methods=["POST"])
@require_client_or_user
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


@app.route("/api/config", methods=["GET"])
@require_auth
def get_config():
    return jsonify({"services": sorted(SERVICES), "config": gateway_config.load_config()})


@app.route("/api/config", methods=["POST"])
@require_auth
def update_config():
    # Full-replace, not merge: the caller (the /ui table, or a script)
    # sends the complete desired config, so omitting a key actually
    # removes it rather than leaving it stranded forever.
    body = request.get_json(silent=True) or {}
    new_config = body.get("config")

    if not isinstance(new_config, dict):
        return jsonify({"error": "'config' must be an object of {\"<key>\": \"<value>\"}"}), 400

    # No restriction to known service prefixes: config.properties is a
    # free-form store, and get_param()'s "<service_id>.<param>" /
    # "gateway.<param>" convention is just that - a convention for keys
    # a service actually looks up, not a schema this endpoint enforces.
    # Only rule is what the file format itself can't survive.
    cleaned = {}
    for key, value in new_config.items():
        if not key or "\n" in key or "=" in key:
            return jsonify({"error": f"invalid key '{key}' - must be non-empty and contain no '=' or newline"}), 400
        cleaned[key] = "" if value is None else str(value)

    gateway_config.save_config(cleaned)
    return jsonify({"services": sorted(SERVICES), "config": cleaned})


@app.route("/api/clients", methods=["GET"])
@require_auth
def get_clients():
    return jsonify({"clients": list_clients()})


@app.route("/api/clients", methods=["POST"])
@require_auth
def add_client():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()

    if not name or not CLIENT_NAME_RE.match(name):
        return jsonify({
            "error": "invalid_request",
            "error_description": "missing/invalid 'name' - letters, digits, '_', '-' only",
        }), 400

    client_id, client_secret = create_client(name)
    return jsonify({"client_id": client_id, "client_secret": client_secret})


@app.route("/api/clients/<client_id>", methods=["DELETE"])
@require_auth
def remove_client(client_id):
    if list_clients() == [client_id]:
        return jsonify({
            "error": "invalid_request",
            "error_description": "refusing to delete the only remaining client - it would lock out /ui",
        }), 400
    if not delete_client(client_id):
        return jsonify({"error": "not_found"}), 404
    return jsonify({"deleted": client_id})


@app.route("/files/<path:filename>", methods=["GET"])
def serve_file(filename):
    # Unauthenticated by design - same trust model as the signed
    # YouTube CDN URLs `youtube_download` normally hands back directly
    # for "video": an unguessable (UUID) filename that expires on its
    # own (see that service's cleanup sweep) rather than living behind
    # the OAuth bearer token /invoke needs. `.name` strips any
    # directory components, so this can't escape FILES_DIR regardless
    # of what the URL contains.
    safe_name = Path(filename).name
    path = FILES_DIR / safe_name
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="audio/mp4")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "services": sorted(SERVICES)})


if __name__ == "__main__":
    # 0.0.0.0 so this is reachable from Caddy/other local processes, not
    # just localhost-from-this-same-process. threaded=True matters now
    # that mac_deploy's "deploy_status" needs to be pollable *while*
    # "start_deploy"'s background thread is running - the single-
    # threaded default would otherwise queue every other request
    # (including unrelated ones like youtube_summarizer) behind
    # whichever one is currently being handled.
    app.run(host="0.0.0.0", port=8788, threaded=True)
