"""User accounts for DeepSink's server-persisted session store.

Deliberately parallel to auth.py's OAuth2 Client Credentials flow (a
user_id + password exchanged at POST /deepsink/auth/token for a
short-lived signed JWT, sent as `Authorization: Bearer <token>` on every
/deepsink/sessions/* call afterward) rather than reusing that module
directly - a human-chosen, possibly-reused password needs real hashing
(werkzeug's generate_password_hash/check_password_hash, already a Flask
dependency - no new package needed), where a client_secret is just a
random opaque token that's safe to store as plaintext.

Single user today by design (see the user's own framing: "for now I
plan to be a single user app only"), but every session already lives
under sessions_data/<user_id>/ (see session_store.py), so registering a
second user later needs no data migration - just another entry in
users.json and someone else's own user_id/password on their client.
"""

import json
import secrets
import time
from functools import wraps
from pathlib import Path

import jwt
from flask import jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

CONFIG_PATH = Path(__file__).parent / "users.json"
# A week, not auth.py's 1 hour: this is a human signing into their own
# phone or laptop, not a machine client re-authenticating on a schedule
# - re-prompting for a password every hour would just be friction, with
# no real security benefit for a single-user personal server.
TOKEN_TTL_SECONDS = 3600 * 24 * 7
JWT_ALGORITHM = "HS256"


def _load_config():
    if not CONFIG_PATH.exists():
        return {"jwt_secret": secrets.token_urlsafe(48), "users": {}}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    CONFIG_PATH.chmod(0o600)


def list_users():
    """Returns the registered user_ids (never password hashes)."""
    return sorted(_load_config().get("users", {}))


def create_user(user_id, password):
    """Registers (or re-registers, overwriting the password of) a
    user_id. Raises ValueError for an empty user_id or password."""
    user_id = user_id.strip()
    if not user_id:
        raise ValueError("user_id must not be empty")
    if not password:
        raise ValueError("password must not be empty")
    config = _load_config()
    config.setdefault("users", {})[user_id] = generate_password_hash(password)
    _save_config(config)


def delete_user(user_id):
    """Removes a user. Returns True if it existed."""
    config = _load_config()
    existed = config.get("users", {}).pop(user_id, None) is not None
    if existed:
        _save_config(config)
    return existed


def ensure_bootstrap_user():
    """Called once at gateway startup, mirroring
    auth.ensure_bootstrap_client(): if no user is registered yet (fresh
    install, or users.json missing entirely), creates one with a random
    password and prints it once to the gateway's own log - the only
    place it's ever shown in full. Does nothing once any user exists."""
    config = _load_config()
    if config.get("users"):
        return

    user_id = "ranjith"
    password = secrets.token_urlsafe(16)
    create_user(user_id, password)
    print("=" * 64)
    print("ai-gateway: no DeepSink user was registered - created one:")
    print(f"  user_id:  {user_id}")
    print(f"  password: {password}")
    print(f"Enter these in DeepSink's Settings. Stored (hashed) in {CONFIG_PATH.name}; "
          "this password will not be printed again.")
    print("=" * 64)


def issue_token(user_id, password):
    """Returns the OAuth2-shaped token response dict, or None if the
    credentials don't match a registered user."""
    config = _load_config()
    stored_hash = config.get("users", {}).get(user_id)
    if stored_hash is None or not check_password_hash(stored_hash, password):
        return None

    now = int(time.time())
    payload = {"sub": user_id, "iat": now, "exp": now + TOKEN_TTL_SECONDS}
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


def verify_user_token(token):
    """Public wrapper around _verify_token, for callers outside this
    module - see gateway.py's combined /invoke auth (accepts this
    alongside auth.py's own client-credentials token, so DeepSink can
    call mac_deploy/deepsink_articulate with just its user login,
    without also needing a separate registered OAuth2 client)."""
    return _verify_token(token)


def require_user(view):
    """Route decorator for /deepsink/sessions/* - like auth.require_auth,
    but also stashes the authenticated user_id on the request
    (`request.deepsink_user_id`) so the view can scope every
    session_store.py call to that user's own folder."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "invalid_token", "error_description": "missing bearer token"}), 401

        token = header[len("Bearer "):].strip()
        payload = _verify_token(token)
        if payload is None:
            return jsonify({"error": "invalid_token", "error_description": "token is invalid or expired"}), 401

        request.deepsink_user_id = payload["sub"]
        return view(*args, **kwargs)

    return wrapped
