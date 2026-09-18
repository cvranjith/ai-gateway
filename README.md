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

- `youtube_download` — `params: { "video_id": "...", "kind": "video" | "audio" }`.
  Resolves a direct, ready-to-download URL via yt-dlp (no video bytes
  proxied through this server — the caller downloads straight from
  YouTube's own CDN, which is what gives a normal client-side download
  progress bar for free). Returns `{ "video_id", "kind", "title", "ext",
  "url", "filesize" }`. `"video"` only ever returns a *progressive*
  format (video+audio already combined in one URL, no muxing needed on
  either end) capped at `youtube_download.max_video_height` (default
  `1080`); if a video has no progressive format at all, this 404s
  rather than falling back to a server-side merge (not implemented —
  would need its own streaming route outside the `/invoke` JSON
  contract). See `services/youtube_download.py`.

- `deepsink_transcribe` — `params: { "audio_base64": "...", "format": "m4a",
  "chunk_index": 0, "start_offset_seconds": 0.0 }`. Transcribes one audio
  chunk locally via OpenAI Whisper (`openai-whisper`, CPU by default — no
  audio ever leaves this Mac). Returns
  `{ "blocks": [{ "start", "end", "text" }, ...] }`, with `start`/`end`
  already offset by `start_offset_seconds` so the caller does no timestamp
  math. Configurable: `deepsink_transcribe.model_id` (default `small.en`),
  `deepsink_transcribe.device` (default CPU; `mps` is untested here). See
  `services/deepsink_transcribe.py`.

- `mac_deploy` — `params: { "action": "wifi_status" | "start_deploy" | "deploy_status", "project": "ytrun" | "deepsink" }`.
  Triggers a real rebuild+reinstall of the given app onto whichever
  device is currently paired with this Mac (`project` defaults to
  `"ytrun"` if omitted, for older callers). See `services/mac_deploy.py`.

- `deepsink_notes` — `params: { "transcript": "...", "marker_hints": [...], "background_notes": "..." }`.
  Turns a full meeting transcript into structured notes via Codex CLI —
  same subprocess pattern as `youtube_summarizer`, JSON-out instead of
  plain text. `background_notes` is free-text context the user typed
  about the session (attendees, agenda, acronyms) — used to interpret
  the transcript, never treated as meeting content itself. Returns
  `{ "title", "summary", "key_points", "decisions", "action_items",
  "open_questions" }`. Configurable: `deepsink_notes.model_id`,
  `deepsink_notes.codex_timeout_seconds` (default `180`). See
  `services/deepsink_notes.py`.

- `deepsink_articulate` — `params: { "transcript": "...", "background_notes": "..." }`.
  Same idea as `deepsink_notes` but for a short, recent excerpt rather
  than a full transcript, and tuned to be fast (tapped mid-meeting,
  waited on) rather than thorough; `background_notes` is the same field
  `deepsink_notes` takes. Returns `{ "bullets": [...], "speech": "..." }`
  — quick reference points, plus the same content phrased as something
  to read out loud. Configurable: `deepsink_articulate.model_id`,
  `deepsink_articulate.codex_timeout_seconds` (default `45`). See
  `services/deepsink_articulate.py`.

- `deepsink_diarize` — `params: { "chunks": [{ "audio_base64", "start_offset_seconds" }, ...], "format": "m4a" }`.
  Diarizes a whole session's audio in one pass via `pyannote.audio`,
  running in its own isolated venv (`.venv-diarize`) and invoked as a
  subprocess, not imported — see "deepsink_diarize setup" below for why,
  and `services/deepsink_diarize.py`'s own module docstring for the full
  story. Returns
  `{ "segments": [{ "start", "end", "speaker": "SPEAKER_00" }, ...] }` in
  session-absolute seconds — raw diarization output, not merged with any
  transcript text. Configurable: `deepsink_diarize.timeout_seconds`
  (default `1800` — a long meeting genuinely takes a while on CPU).

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

## DeepSink session store (REST API)

Genuinely different in kind from everything above: `/invoke`'s services
are stateless function calls (audio in, text out; forgotten immediately
after). `/deepsink/sessions/*` is real, permanent CRUD — this Mac is now
the source of truth for DeepSink sessions, not the phone's local store.
See `session_store.py` (one JSON file per session under `sessions_data/`,
gitignored — real meeting content never belongs in git) and
`deepsink_sessions.py` (the Flask Blueprint below, registered in
`gateway.py`). Reuses `deepsink_transcribe`/`deepsink_notes`/
`deepsink_diarize`'s `handle()` functions as plain in-process calls for
the actual Whisper/Codex work, so each only runs from one place.

Same `Authorization: Bearer <token>` as `/invoke`. Reached through
ai-router's `/deepsink/*` passthrough in normal use, so the phone
(or a future web client) never needs a second credential.

```
POST   /deepsink/sessions                          { "title": "..." }              -> 201 <session>
GET    /deepsink/sessions                                                          -> { "sessions": [<session>, ...] }
GET    /deepsink/sessions/<id>                                                     -> <session>
PATCH  /deepsink/sessions/<id>                      { "title" | "background_notes" | "duration_seconds" | "recording_incomplete": ... } -> <session>
DELETE /deepsink/sessions/<id>                                                     -> { "deleted": "<id>" }

POST   /deepsink/sessions/<id>/chunks               { "audio_base64", "chunk_index", "start_offset_seconds", "duration_seconds", "format" }
                                                     -> transcribes via deepsink_transcribe, persists the chunk + blocks -> <session>
POST   /deepsink/sessions/<id>/finish                                              -> generates notes via deepsink_notes, persists -> <session>
POST   /deepsink/sessions/<id>/notes/regenerate                                    -> same as /finish, for re-running after a transcript edit
PATCH  /deepsink/sessions/<id>/action_items/<item_id>  { "is_checked": true|false } -> <session>
POST   /deepsink/sessions/<id>/markers              { "offset_seconds", "comment" } -> <session>
POST   /deepsink/sessions/<id>/diarize                                             -> runs deepsink_diarize against all stored chunks, persists speaker-tagged blocks -> <session>
```

`<session>` is the full JSON object `session_store.py` maintains — id,
title, timestamps, `stage` (`recording`|`uploading`|`summarising`|`ready`|`failed`),
`chunks`, `transcript_blocks`, `notes`, `action_items`, `markers`,
`speakers`, `background_notes`, plus diarization progress fields. A
client never merges partial updates locally — every write endpoint
returns the full, current session, so re-rendering is just "replace
what I'm showing with this response."

A per-session `threading.Lock` (`session_store.py`) serializes each
session's own read-modify-write cycle, since Flask runs `threaded=True`
and two requests for the same session (e.g. two chunk uploads landing
close together) can genuinely race. Different sessions never block each
other.

## Config

Per-service and gateway-wide parameters live in `config.properties`
(plain `key=value` lines, committed — nothing in it is secret). Keys
are namespaced `<service_id>.<param>` or `gateway.<param>` (a
service-specific key wins over a `gateway.` fallback of the same
`param` name). Edit the file directly, or through the `/ui` config
editor — both take effect immediately, no restart needed, since
`config.py` reads the file fresh on every request.

## `deepsink_diarize` setup

One-time, and only needed if you actually want speaker detection in
DeepSink — every other service works without any of this.

1. **Create (or sign into) a free HuggingFace account** at
   [huggingface.co](https://huggingface.co).
2. **Accept the model licenses** — visit all three of these while signed
   in and accept the terms on each page (separate gates even though the
   `speaker-diarization-3.1` pipeline pulls in all of them; the third one
   is a sub-dependency pulled in by this repo's installed
   `pyannote.audio` version specifically — confirmed the hard way, a
   403 naming it is exactly what a missing accept here looks like):
   - [huggingface.co/pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
   - [huggingface.co/pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
   - [huggingface.co/pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
3. **Generate an access token** at
   [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
   — "New token", type **Read** is enough, no need for Write.
4. **Save it** to `hf_token.txt` at this repo's root (gitignored, mode
   600 like `auth_config.json` — `hf_token.txt.example` shows the shape):
   ```
   echo "<your token>" > hf_token.txt
   chmod 600 hf_token.txt
   ```
5. **Create the isolated venv** this service runs in (see
   `services/deepsink_diarize.py`'s module docstring for why it's
   isolated from the rest of this repo's dependencies):
   ```
   python3.10 -m venv .venv-diarize
   .venv-diarize/bin/pip install -r requirements-diarize.txt
   ```
6. Restart the gateway (`local-llm.sh --restart`). No code change needed
   — `deepsink_diarize` reads the token fresh from `hf_token.txt` and
   checks for `.venv-diarize` on every call, not just at startup.

Until all of this is done, `deepsink_diarize` returns a clean, specific
error (503, "Diarization isn't configured yet...") rather than failing
oddly or crashing anything else.

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
