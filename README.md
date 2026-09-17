# ai-gateway

One stable, personal REST endpoint for small AI-backed services,
dispatched by `service_id` so the endpoint itself never has to change
as new services get added.

## Contract

```
POST /oauth/token
{ "client_id": "<id>", "client_secret": "<secret>" }

200 -> { "access_token": "<jwt>", "token_type": "Bearer", "expires_in": 3600 }
401 -> { "error": "invalid_client" }

POST /invoke
Authorization: Bearer <jwt>
{ "service_id": "<id>", "params": { ...service-specific... } }

200 -> { "service_id": "<id>", "result": {...} }
401 -> { "error": "invalid_token", "error_description": "..." }
4xx/5xx -> { "error": "..." }

GET /health -> { "status": "ok", "services": [...] }

GET  /api/config -> { "services": [...], "config": { "<key>": "<value>", ... } }
POST /api/config
{ "config": { "<key>": "<value>", ... } }   # full replace - omit a key to delete it
200 -> { "services": [...], "config": {...} }

GET    /api/clients -> { "clients": ["<client_id>", ...] }   # never secrets
POST   /api/clients
{ "name": "<client_name>" }
200 -> { "client_id": "<id>", "client_secret": "<secret>" }   # shown once, not recoverable
DELETE /api/clients/<client_id> -> { "deleted": "<client_id>" }
400 -> refuses to delete the last remaining client

GET /ui -> the web dashboard (config editor, /invoke tester, client management)
```

`/invoke`, `/api/config`, and `/api/clients` all require the same
`Authorization: Bearer <token>` from `/oauth/token`.

## Services

- `youtube_summarizer` — `params: { "video_id": "...", "length": "short" | "paragraph" | "detailed" }`.
  Fetches the video's transcript and summarizes it via Codex CLI. See
  `services/youtube_summarizer.py`. Configurable: `youtube_summarizer.model_id`
  (passed to Codex as `-m`; leave blank to use Codex's own default),
  `youtube_summarizer.codex_timeout_seconds` (default `180`).

## Adding a new service

1. Create `services/your_service.py` exposing `handle(params: dict) -> dict`.
   Raise `services.errors.ServiceError(message, status_code)` for any
   failure (bad input, upstream failure, etc.) — `gateway.py` turns
   that straight into the HTTP response.
2. Register it in `gateway.py`'s `SERVICES` dict: `"your_service": your_service.handle`.
3. If it needs configurable parameters, read them with
   `config.get_param("your_service", "param_name", default)` — see
   `config.py` and how `youtube_summarizer.py` uses it. They then show
   up in `/api/config` and the `/ui` config editor automatically, no
   further wiring needed.

No endpoint, request/response shape, or error-handling change needed.

## Config

Per-service and gateway-wide parameters live in `config.properties`
(plain `key=value` lines, committed — nothing in it is secret). Keys
are namespaced `<service_id>.<param>` or `gateway.<param>` (a
service-specific key wins over a `gateway.` fallback of the same
`param` name). Edit the file directly, or through the `/ui` config
editor — both take effect immediately, no restart needed, since
`config.py` reads the file fresh on every request.

## Running

Standalone (local testing):
```
pip install -r requirements.txt
python3 generate_config.py <client_name>   # once, to create auth_config.json (gitignored)
/opt/homebrew/opt/python@3.10/bin/python3.10 gateway.py

TOKEN=$(curl -s -X POST localhost:8788/oauth/token -H 'Content-Type: application/json' \
  -d '{"client_id":"<id>","client_secret":"<secret>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -X POST localhost:8788/invoke -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"service_id":"youtube_summarizer","params":{"video_id":"jNQXAC9IVRw","length":"short"}}'
```

Normally, though, this is started/stopped as part of
`/Users/ranjithcv/Documents/code/claude/ollama/local-llm.sh --start`
(alongside Ollama, Caddy, and the Tailscale Funnel that exposes it
publicly at `/gateway/*` on the same URL Ollama is funneled through —
see that script for the full flow, `--status` to check it's running,
and `--log`/`--follow` for its logs).

## Auth

`/invoke`, `/api/config`, and `/api/clients` all require OAuth2 Client
Credentials — see `auth.py` for the full flow. This endpoint is
reachable over the public internet once `local-llm.sh` has run (via
Tailscale Funnel), not just the private tailnet, so every client needs
its own registered `client_id`/`client_secret`.

**First run**: if no clients are registered at all yet (a brand new
`auth_config.json`, or none exists), the gateway auto-creates one
`bootstrap` client on startup and prints its `client_id`/`client_secret`
to its own log — that's how you get in to `/ui` the very first time,
with no manual setup step. `local-llm.sh --log` (or `--follow`) shows
it. It only happens once; once any client exists, startup leaves them
alone.

**Registering more clients**, either:
- Through `/ui`, once signed in — the Clients panel adds one and shows
  its secret once, or
- From the command line: `python3 generate_config.py <client_name>`

Either path writes to `auth_config.json` (gitignored, mode 600 — never
commit it; `auth_config.json.example` shows the shape) and shows the
new `client_id`/`client_secret` exactly once. Save it immediately; it
isn't stored anywhere else and can't be recovered later. `/ui`'s
Clients panel also lists existing client IDs (never secrets) and can
delete one (refused if it's the only one left, to avoid locking
yourself out of `/ui`).

A client exchanges its credentials for a short-lived (1 hour) signed
JWT via `POST /oauth/token`, then sends it as `Authorization: Bearer
<token>` on every call above. Tokens are stateless (verified by
signature + expiry only), so nothing needs to be revoked or looked up
server-side per request — a compromised client is dealt with by
deleting it (via `/ui` or by editing `auth_config.json` directly) and
restarting the gateway.

## Web UI

`GET /ui` serves a small dashboard (`static/index.html`, no build
step) with three panels once signed in with a client's ID/secret:
- **Config** — view/edit every `config.properties` key, add new ones,
  save (full replace — removing a row and saving deletes that key).
- **Test /invoke** — pick a service, edit its params as JSON, invoke
  it, see the raw response. Uses the same bearer token as everything else.
- **Clients** — list, add, and delete OAuth clients (see Auth above).

The token lives only in the browser tab's `sessionStorage` — closing
the tab signs you out; there's no server-side session.
