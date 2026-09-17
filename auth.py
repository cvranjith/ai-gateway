"""OAuth 2.0 Client Credentials Grant for ai-gateway.

This is machine-to-machine auth (a personal app talking to your own
server), not a human login — Client Credentials is the standard OAuth2
grant for exactly that, so this issues real, short-lived, signed
bearer tokens rather than just checking a static shared secret on
every request.

Flow:
    1. Client POSTs its client_id + client_secret to /oauth/token.
    2. If valid, gets back a signed JWT access_token (expires in 1 hour).
    3. Client sends that token as `Authorization: Bearer <token>` on
       every /invoke call. require_auth() verifies the signature and
       expiry — nothing is looked up server-side per request, so an
       expired/forged token is rejected purely from the token itself.

Registered clients + the signing secret live in auth_config.json
(gitignored — see auth_config.json.example for the shape). Regenerate
that file (see generate_config.py) rather than committing real
credentials.
"""

import json
import time
from functools import wraps
from pathlib import Path

import jwt
from flask import jsonify, request

CONFIG_PATH = Path(__file__).parent / "auth_config.json"
TOKEN_TTL_SECONDS = 3600
JWT_ALGORITHM = "HS256"


def _load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"{CONFIG_PATH} not found — run generate_config.py once to create it "
            "(see README.md)."
        )
    with open(CONFIG_PATH) as f:
        return json.load(f)


def issue_token(client_id, client_secret):
    """Returns the OAuth2 token response dict, or None if the client
    credentials don't match a registered client."""
    config = _load_config()
    clients = config.get("clients", {})
    expected_secret = clients.get(client_id)
    if expected_secret is None or expected_secret != client_secret:
        return None

    now = int(time.time())
    payload = {"sub": client_id, "iat": now, "exp": now + TOKEN_TTL_SECONDS}
    token = jwt.encode(payload, config["jwt_secret"], algorithm=JWT_ALGORITHM)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
    }


def _verify_token(token):
    """Returns the decoded payload if valid, else None."""
    config = _load_config()
    try:
        return jwt.decode(token, config["jwt_secret"], algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


def require_auth(view):
    """Route decorator — rejects the request with a standard OAuth2-shaped
    401 unless a valid, unexpired Bearer token is present."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "invalid_token", "error_description": "missing bearer token"}), 401

        token = header[len("Bearer "):].strip()
        payload = _verify_token(token)
        if payload is None:
            return jsonify({"error": "invalid_token", "error_description": "token is invalid or expired"}), 401

        return view(*args, **kwargs)

    return wrapped
