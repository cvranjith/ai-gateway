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
```

## Services

- `youtube_summarizer` — `params: { "video_id": "...", "length": "short" | "paragraph" | "detailed" }`.
  Fetches the video's transcript and summarizes it via Codex CLI. See
  `services/youtube_summarizer.py`.

## Adding a new service

1. Create `services/your_service.py` exposing `handle(params: dict) -> dict`.
   Raise `services.errors.ServiceError(message, status_code)` for any
   failure (bad input, upstream failure, etc.) — `gateway.py` turns
   that straight into the HTTP response.
2. Register it in `gateway.py`'s `SERVICES` dict: `"your_service": your_service.handle`.

No endpoint, request/response shape, or error-handling change needed.

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

`/invoke` requires OAuth2 Client Credentials — see `auth.py` for the
full flow. This endpoint is reachable over the public internet once
`local-llm.sh` has run (via Tailscale Funnel), not just the private
tailnet, so every client needs its own registered `client_id`/`client_secret`.

Registering a new client:
```
python3 generate_config.py <client_name>
```
This writes/updates `auth_config.json` (gitignored, mode 600 —
never commit it; `auth_config.json.example` shows the shape) and
prints the new `client_id`/`client_secret` once. Save them immediately;
they aren't stored anywhere else and can't be recovered later.

A client exchanges its credentials for a short-lived (1 hour) signed
JWT via `POST /oauth/token`, then sends it as `Authorization: Bearer
<token>` on every `/invoke` call. Tokens are stateless (verified by
signature + expiry only), so nothing needs to be revoked or looked up
server-side per request — a compromised client is dealt with by
removing its entry from `auth_config.json` and restarting the gateway.
