"""REST API for server-persisted DeepSink sessions.

Genuinely different in kind from the rest of this gateway's /invoke
surface: those are stateless function calls (audio in, text out;
transcript in, notes out); this is real CRUD over session state that
now lives permanently on this Mac (see session_store.py) - the source
of truth flipped from the phone's local SwiftData store to here, per
discussion with the user. Mobile (and any future web client) holds no
persistent copy of a session at all - it fetches on demand and sends
explicit write requests for the handful of things that change (a chunk
arriving, a checkbox toggled, notes regenerated).

Auth is a separate, human-facing user_id/password (see user_auth.py),
not ai-gateway's own OAuth client_id/secret (auth.py) - the two guard
different things: a client_id/secret says "this is a legitimate app
calling the gateway at all"; a user_id/password says "this is
<user_id>'s own session data" and scopes every session_store.py call to
that user's folder. Single user today by design, but every session
already lives under a user_id, so a second registered user later needs
no data migration.

Reuses deepsink_transcribe/deepsink_notes/deepsink_diarize's `handle()`
functions directly as in-process function calls (not HTTP) for the
actual Whisper/Codex work - this module is just the persistence and
HTTP-routing layer around them, so there's exactly one place each of
those actually runs.

Reached through ai-router's generic /deepsink/* passthrough (see that
repo's worker.js), which forwards the client's own Authorization header
through unchanged for this prefix (unlike /v1/invoke, which exchanges
the router's own shared token for a fresh ai-gateway OAuth-client
token) - the user_id/password flow is meant to be held by the app
itself, not hidden inside router-side secrets.
"""

import base64
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

import live_preview
import material_extract
import session_store
import speaker_roster
import user_auth
from services import (
    deepsink_background_extract,
    deepsink_diarize,
    deepsink_notes,
    deepsink_prepare,
    deepsink_transcribe,
)
from services.errors import ServiceError


def _now_iso():
    return datetime.now(timezone.utc).isoformat()

bp = Blueprint("deepsink", __name__, url_prefix="/deepsink")

# Guards the background "auto-regenerate notes after a chunk lands"
# trigger below - a plain per-session lock, acquired non-blocking: if a
# regen is already running for this session, a chunk landing mid-way
# through it just skips starting a second one rather than piling up
# overlapping Codex calls that could race on which one's result gets
# written last. Nothing is lost by skipping - the next chunk to land
# (or an explicit /finish) triggers a fresh regen that covers whatever
# transcript exists at that point, including what the skipped attempt
# would have covered.
_regen_locks_guard = threading.Lock()
_regen_locks = {}


def _regen_lock_for(user_id, session_id):
    key = (user_id, session_id)
    with _regen_locks_guard:
        if key not in _regen_locks:
            _regen_locks[key] = threading.Lock()
        return _regen_locks[key]


@bp.route("/auth/token", methods=["POST"])
def issue_token():
    body = request.get_json(silent=True) or {}
    user_id = (body.get("user_id") or "").strip()
    password = body.get("password") or ""
    if not user_id or not password:
        return jsonify({"error": "invalid_request", "error_description": "missing user_id or password"}), 400

    token_response = user_auth.issue_token(user_id, password)
    if token_response is None:
        return jsonify({"error": "invalid_grant", "error_description": "wrong user_id or password"}), 401
    return jsonify(token_response)


def _current_user_id():
    return request.deepsink_user_id


def _session_or_404(session_id):
    return session_store.get_session(_current_user_id(), session_id)


@bp.route("/sessions", methods=["POST"])
@user_auth.require_user
def create_session():
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip() or "Untitled session"
    # Defaults to False - a plain "create a session" call (the web
    # viewer's own "+ New Session," title-only, nothing attached to it
    # yet) should never come back looking like it's already being
    # recorded. DeepSink's mobile app passes true explicitly, since it
    # only ever calls this as part of immediately starting to record.
    is_recording = bool(body.get("is_recording"))
    data = session_store.create_session(
        _current_user_id(), title=title, started_at=body.get("started_at"), is_recording=is_recording
    )
    return jsonify(data), 201


@bp.route("/sessions", methods=["GET"])
@user_auth.require_user
def list_sessions():
    return jsonify({"sessions": session_store.list_sessions(_current_user_id())})


@bp.route("/sessions/<session_id>", methods=["GET"])
@user_auth.require_user
def get_session(session_id):
    data = _session_or_404(session_id)
    if data is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(data)


@bp.route("/sessions/<session_id>", methods=["PATCH"])
@user_auth.require_user
def patch_session(session_id):
    body = request.get_json(silent=True) or {}
    # A small, explicit allowlist rather than a generic merge - these are
    # the only fields a client legitimately sets directly; everything
    # else (notes, transcript, action items, speakers) only ever changes
    # through its own purpose-built endpoint below.
    #
    # "stage" is the one deliberate exception to "the server always
    # decides stage" (append_chunk/save_notes normally own every other
    # transition) - Resume Recording (DeepSink's mobile app) continues
    # an already-"ready" session's recording, and nothing server-side
    # would otherwise know that's happened until its first new chunk
    # actually uploads. Restricted to exactly "recording" so this can't
    # be used to fake any other transition (e.g. "ready" without real
    # notes). Also flips is_recording back to True in the same write -
    # see that field's own comment in session_store.py for why that,
    # not stage, is what live_preview/the web viewer's live-stream
    # actually key off.
    allowed = {"title", "background_notes", "duration_seconds", "recording_incomplete", "stage"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if "stage" in fields:
        if fields["stage"] != "recording":
            return jsonify({"error": "'stage' can only be set to 'recording' via this endpoint"}), 400
        fields["is_recording"] = True
    if "title" in fields:
        # A client explicitly setting the title IS the "human renamed
        # this" signal - see title_is_manual's own comment in
        # session_store.py. From here on, background notes regen never
        # touches title again, no matter how many more chunks land.
        fields["title_is_manual"] = True
    if not fields:
        return jsonify({"error": "no updatable fields in body"}), 400
    data = session_store.update_session(_current_user_id(), session_id, **fields)
    if data is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(data)


@bp.route("/sessions/<session_id>", methods=["DELETE"])
@user_auth.require_user
def delete_session(session_id):
    session_store.delete_session(_current_user_id(), session_id)
    return jsonify({"deleted": session_id})


@bp.route("/sessions/<session_id>/chunks", methods=["POST"])
@user_auth.require_user
def upload_chunk(session_id):
    user_id = _current_user_id()
    if _session_or_404(session_id) is None:
        return jsonify({"error": "not_found"}), 404

    body = request.get_json(silent=True) or {}
    audio_b64 = (body.get("audio_base64") or "").strip()
    if not audio_b64:
        return jsonify({"error": "missing 'audio_base64'"}), 400
    try:
        chunk_index = int(body.get("chunk_index"))
        start_offset_seconds = float(body.get("start_offset_seconds") or 0)
        duration_seconds = float(body.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "'chunk_index'/'start_offset_seconds'/'duration_seconds' must be numbers"}), 400
    fmt = (body.get("format") or "m4a").strip().lstrip(".") or "m4a"

    try:
        result = deepsink_transcribe.handle({
            "audio_base64": audio_b64,
            "start_offset_seconds": start_offset_seconds,
            "format": fmt,
            "chunk_index": chunk_index,
        })
    except ServiceError as e:
        session_store.mark_failed(user_id, session_id, e.message)
        return jsonify({"error": e.message}), e.status_code

    file_name = f"{chunk_index}.{fmt}"
    session_store.save_chunk_audio(user_id, session_id, file_name, base64.b64decode(audio_b64))
    data = session_store.append_chunk(
        user_id, session_id, chunk_index, file_name, start_offset_seconds, duration_seconds,
        result.get("blocks", []),
    )
    live_preview.clear(f"{user_id}:{session_id}")
    # Only when there's actually something to summarize - an empty (or
    # silent) chunk means an empty transcript, and deepsink_notes treats
    # that as a real error (marks the session "failed"), which is right
    # for an explicit /finish with nothing recorded but wrong to trigger
    # invisibly in the background off a single quiet chunk early in an
    # otherwise-normal recording.
    if data["transcript_blocks"]:
        _trigger_background_regen(user_id, session_id)
    return jsonify(data)


def _trigger_background_regen(user_id, session_id):
    # Fires the same notes/action-items regeneration `/finish` and
    # `/notes/regenerate` trigger explicitly, but automatically after
    # every chunk - so Notes/Actions "materialize" progressively during
    # a long recording, not just once at the end. Runs in a background
    # thread so a chunk upload's own response (which the phone is
    # actively waiting on to know the chunk landed) doesn't also have to
    # wait on a Codex call - see the lock above for how overlapping
    # triggers are handled.
    lock = _regen_lock_for(user_id, session_id)
    if not lock.acquire(blocking=False):
        return

    def run():
        try:
            _generate_notes(user_id, session_id)
        finally:
            lock.release()

    threading.Thread(target=run, daemon=True).start()


def _generate_notes(user_id, session_id):
    data = session_store.get_session(user_id, session_id)
    if data is None:
        return None, (jsonify({"error": "not_found"}), 404)

    # Set immediately, before the (possibly slow) Codex call - this is
    # what lets a viewer show a "generating notes" spinner instead of a
    # plain empty tab while it's in flight. Cleared by whichever exit
    # path is actually taken below (mark_failed or save_notes both set
    # it back to False as part of their own single write).
    session_store.update_session(user_id, session_id, is_generating_notes=True)

    transcript = " ".join(
        block["text"] for block in sorted(data["transcript_blocks"], key=lambda b: b["start"])
    )
    marker_hints = [
        {"offset_seconds": m["offset_seconds"], "comment": m.get("comment") or ""}
        for m in sorted(data["markers"], key=lambda m: m["offset_seconds"])
    ]

    try:
        notes_payload = deepsink_notes.handle({
            "transcript": transcript,
            "marker_hints": marker_hints,
            "background_notes": data.get("background_notes") or "",
            # Just the date portion of the ISO started_at timestamp -
            # for resolving relative due dates ("by next Friday")
            # against when the meeting actually happened.
            "meeting_date": (data.get("started_at") or "")[:10],
        })
    except ServiceError as e:
        session_store.mark_failed(user_id, session_id, e.message)
        return None, (jsonify({"error": e.message}), e.status_code)

    # Matched by exact text, since generated action items have no stable
    # ID across a regenerate - the same reconciliation DeepSink's mobile
    # app used to do locally (SessionProcessor.generateNotes), now the
    # one and only place it happens.
    previously_checked = {item["text"] for item in data["action_items"] if item.get("is_checked")}
    action_items = [
        {
            "id": str(uuid.uuid4()),
            "text": item.get("text", ""),
            "owner": item.get("owner"),
            "due": item.get("due"),
            "is_checked": item.get("text") in previously_checked,
            "sort_order": index,
        }
        for index, item in enumerate(notes_payload.get("action_items") or [])
    ]

    updated = session_store.save_notes(user_id, session_id, notes_payload, action_items)
    return updated, None


@bp.route("/sessions/<session_id>/finish", methods=["POST"])
@user_auth.require_user
def finish_session(session_id):
    user_id = _current_user_id()
    # Unconditionally, before notes even run, and regardless of whether
    # they succeed - /finish is the one call that only ever happens
    # because the user actually tapped Stop, so this is the real,
    # canonical "recording has stopped" signal (see is_recording's own
    # comment in session_store.py). Deliberately not folded into
    # _generate_notes itself: that function also runs from the
    # per-chunk background trigger, which must NOT touch this.
    session_store.update_session(user_id, session_id, is_recording=False)
    data, error_response = _generate_notes(user_id, session_id)
    if error_response is not None:
        return error_response
    # Auto-fires once a recording is genuinely finished, not per-chunk
    # like notes above - diarizing is real CPU-minutes, only worth
    # paying once there's a final, complete transcript. Manual "Detect
    # Speakers" (the /diarize route below) still exists for a re-run
    # after an edit, or for a session finished before this existed.
    _trigger_background_diarize(user_id, session_id)
    return jsonify(data)


@bp.route("/sessions/<session_id>/notes/regenerate", methods=["POST"])
@user_auth.require_user
def regenerate_notes(session_id):
    data, error_response = _generate_notes(_current_user_id(), session_id)
    if error_response is not None:
        return error_response
    return jsonify(data)


@bp.route("/sessions/<session_id>/action_items/<item_id>", methods=["PATCH"])
@user_auth.require_user
def patch_action_item(session_id, item_id):
    body = request.get_json(silent=True) or {}
    # is_checked (the checkbox) plus owner/due, now fillable by hand
    # when the model left them null - same allowlist-and-merge shape as
    # patch_session above, just scoped to one action item.
    allowed = {"is_checked", "owner", "due"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if "is_checked" in fields:
        fields["is_checked"] = bool(fields["is_checked"])
    if not fields:
        return jsonify({"error": "no updatable fields in body"}), 400
    data = session_store.update_action_item(_current_user_id(), session_id, item_id, **fields)
    if data is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(data)


@bp.route("/sessions/<session_id>/markers", methods=["POST"])
@user_auth.require_user
def add_marker(session_id):
    body = request.get_json(silent=True) or {}
    try:
        offset_seconds = float(body.get("offset_seconds"))
    except (TypeError, ValueError):
        return jsonify({"error": "'offset_seconds' must be a number"}), 400
    comment = (body.get("comment") or "").strip() or None
    data = session_store.add_marker(_current_user_id(), session_id, offset_seconds, comment)
    if data is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(data)


# "Prepare Me" - a persisted, multi-turn planning conversation for a
# meeting/presentation that hasn't happened yet (see deepsink_prepare.py's
# own docstring for the full framing). One route, not a create+append
# pair: an empty "message" with an empty prep_chat kicks the
# conversation off (the model opens with a clarifying question); any
# other call is a normal turn. `history` sent to the model is always
# the session's existing prep_chat, so this route is the only writer -
# no client-side merge logic, same "server returns the full session"
# contract as everything else here.
@bp.route("/sessions/<session_id>/prepare/chat", methods=["POST"])
@user_auth.require_user
def prepare_chat(session_id):
    user_id = _current_user_id()
    data = _session_or_404(session_id)
    if data is None:
        return jsonify({"error": "not_found"}), 404

    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    history = data.get("prep_chat") or []
    if not message and history:
        return jsonify({"error": "missing 'message'"}), 400

    try:
        result = deepsink_prepare.handle({
            "background_notes": data.get("background_notes") or "",
            "history": history,
            "message": message,
        })
    except ServiceError as e:
        return jsonify({"error": e.message}), e.status_code

    new_turns = list(history)
    if message:
        new_turns.append({"role": "user", "content": message, "created_at": _now_iso()})
    new_turns.append({"role": "assistant", "content": result.get("reply", ""), "created_at": _now_iso()})

    updated = session_store.update_session(user_id, session_id, prep_chat=new_turns)
    return jsonify(updated)


# Background prep materials (PDF/plain-text uploads) - see
# material_extract.py and session_store.py's own comments. The raw
# file is never kept, only its extracted text; MAX_MATERIAL_BYTES is a
# sanity cap on the *decoded* upload (a personal app's own prep
# documents, not a general file host).
MAX_MATERIAL_BYTES = 20 * 1024 * 1024


@bp.route("/sessions/<session_id>/materials", methods=["POST"])
@user_auth.require_user
def upload_material(session_id):
    user_id = _current_user_id()
    if _session_or_404(session_id) is None:
        return jsonify({"error": "not_found"}), 404

    body = request.get_json(silent=True) or {}
    filename = (body.get("filename") or "").strip() or "Untitled"
    fmt = (body.get("format") or "").strip().lower()
    content_b64 = (body.get("content_base64") or "").strip()
    if not content_b64:
        return jsonify({"error": "missing 'content_base64'"}), 400
    try:
        file_bytes = base64.b64decode(content_b64, validate=True)
    except Exception:
        return jsonify({"error": "'content_base64' is not valid base64"}), 400
    if len(file_bytes) > MAX_MATERIAL_BYTES:
        return jsonify({"error": f"file too large - {MAX_MATERIAL_BYTES // (1024 * 1024)}MB max"}), 413

    try:
        text = material_extract.extract_text(file_bytes, fmt)
    except ServiceError as e:
        return jsonify({"error": e.message}), e.status_code

    material_id = str(uuid.uuid4())
    session_store.save_material_text(user_id, session_id, material_id, text)
    data = session_store.add_material(user_id, session_id, material_id, filename, fmt, len(text))
    if data is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(data), 201


@bp.route("/sessions/<session_id>/materials/<material_id>", methods=["DELETE"])
@user_auth.require_user
def delete_material(session_id, material_id):
    user_id = _current_user_id()
    data = session_store.remove_material(user_id, session_id, material_id)
    if data is None:
        return jsonify({"error": "not_found"}), 404
    session_store.delete_material_text(user_id, session_id, material_id)
    return jsonify(data)


# "Process" - the structured extraction pass (deepsink_background_extract)
# over everything prep-related on the session so far: background_notes,
# every uploaded material's text, and the prep_chat conversation.
# Explicit/on-demand (a button in the web viewer), not triggered on
# every background_notes save, since it's a real Codex call each time.
@bp.route("/sessions/<session_id>/background/process", methods=["POST"])
@user_auth.require_user
def process_background(session_id):
    user_id = _current_user_id()
    data = _session_or_404(session_id)
    if data is None:
        return jsonify({"error": "not_found"}), 404

    materials_text = "\n\n".join(
        text for text in (
            session_store.load_material_text(user_id, session_id, m["id"])
            for m in data.get("materials") or []
        ) if text
    )

    try:
        result = deepsink_background_extract.handle({
            "background_notes": data.get("background_notes") or "",
            "materials_text": materials_text,
            "prep_chat": data.get("prep_chat") or [],
        })
    except ServiceError as e:
        return jsonify({"error": e.message}), e.status_code

    updated = session_store.update_session(user_id, session_id, background_summary=result)
    return jsonify(updated)


# --- Live preview (live_preview.py) - see that module's own docstring.
# Not part of session_store/session.json on purpose: this is a
# transient "what's being recognized right now" signal, not durable
# session state.

@bp.route("/sessions/<session_id>/live_preview", methods=["POST"])
@user_auth.require_user
def post_live_preview(session_id):
    if _session_or_404(session_id) is None:
        return jsonify({"error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    live_preview.set_text(f"{_current_user_id()}:{session_id}", text)
    return jsonify({"ok": True})


# Polled by the phone on its own schedule to decide whether it's worth
# pushing live text at all right now - see live_preview.py's docstring.
@bp.route("/sessions/<session_id>/live_preview/viewers", methods=["GET"])
@user_auth.require_user
def get_live_preview_viewers(session_id):
    return jsonify({"viewers": live_preview.viewer_count(f"{_current_user_id()}:{session_id}")})


@bp.route("/sessions/<session_id>/live_preview/stream", methods=["GET"])
def stream_live_preview(session_id):
    # Deliberately NOT @user_auth.require_user - confirmed the hard way
    # (a 401 on every single real browser attempt, silently making the
    # whole live-preview feature look broken end to end): EventSource,
    # the browser API the web viewer uses for this, cannot set custom
    # request headers at all - no Authorization header support, a
    # standing limitation of that API, not something a client-side fix
    # can work around. This route alone also accepts the token as a
    # query param for exactly that reason; every other route stays
    # header-only.
    token = request.args.get("token") or ""
    if not token:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            token = header[len("Bearer "):].strip()
    payload = user_auth.verify_user_token(token)
    if payload is None:
        return jsonify({"error": "invalid_token"}), 401
    user_id = payload["sub"]

    if session_store.get_session(user_id, session_id) is None:
        return jsonify({"error": "not_found"}), 404
    key = f"{user_id}:{session_id}"

    def generate():
        live_preview.add_viewer(key)
        try:
            last_sent = None
            while True:
                text, _ = live_preview.get_text(key)
                if text != last_sent:
                    yield f"data: {json.dumps({'text': text})}\n\n"
                    last_sent = text
                else:
                    # SSE comment line, not a data event - just keeps the
                    # connection alive through any intermediate proxy
                    # (Caddy/Funnel) that might otherwise time out an
                    # idle streaming response.
                    yield ": keep-alive\n\n"
                time.sleep(1)
        except GeneratorExit:
            pass
        finally:
            live_preview.remove_viewer(key)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@bp.route("/sessions/<session_id>/diarize", methods=["POST"])
@user_auth.require_user
def diarize_session(session_id):
    updated, error_response = _run_diarization(_current_user_id(), session_id)
    if error_response is not None:
        return error_response
    return jsonify(updated)


def _run_diarization(user_id, session_id):
    """Returns (updated_session, error_response) - error_response is a
    (jsonify(...), status) tuple on failure, None on success. Shared by
    the manual /diarize route above and the automatic post-/finish
    trigger below, so there's exactly one place this actually runs."""
    data = session_store.get_session(user_id, session_id)
    if data is None:
        return None, (jsonify({"error": "not_found"}), 404)

    chunk_parts = []
    for chunk in data["chunks"]:
        path = session_store.chunk_audio_path(user_id, session_id, chunk["file_name"])
        if not path.exists():
            return None, (jsonify({"error": "audio for this session is no longer available"}), 409)
        chunk_parts.append({
            "audio_base64": base64.b64encode(path.read_bytes()).decode(),
            "start_offset_seconds": chunk["start_offset_seconds"],
        })

    session_store.update_session(user_id, session_id, is_diarizing=True, diarization_error=None)
    try:
        result = deepsink_diarize.handle({"chunks": chunk_parts, "format": "m4a"})
    except ServiceError as e:
        session_store.update_session(user_id, session_id, is_diarizing=False, diarization_error=e.message)
        return None, (jsonify({"error": e.message}), e.status_code)

    embeddings = result.get("embeddings", {})
    session_store.save_speaker_embeddings(user_id, session_id, embeddings)

    updated_blocks, speakers = _apply_diarization(
        data["transcript_blocks"], result.get("segments", []), data.get("speakers") or [], user_id, embeddings
    )
    updated = session_store.set_speakers(user_id, session_id, updated_blocks, speakers)
    return updated, None


def _trigger_background_diarize(user_id, session_id):
    # Quiet no-ops (not an error anywhere a user would see it) if
    # diarization isn't set up, there's no audio, or one's somehow
    # already running - the manual "Detect Speakers" button still gives
    # a real error if genuinely tapped without setup; this is just the
    # automatic convenience path and shouldn't be noisy about declining.
    if not deepsink_diarize.is_configured():
        return
    data = session_store.get_session(user_id, session_id)
    if not data or not data.get("chunks") or data.get("is_diarizing"):
        return
    threading.Thread(target=lambda: _run_diarization(user_id, session_id), daemon=True).start()


@bp.route("/sessions/<session_id>/speakers/<speaker_id>", methods=["PATCH"])
@user_auth.require_user
def rename_speaker(session_id, speaker_id):
    user_id = _current_user_id()
    body = request.get_json(silent=True) or {}
    display_name = (body.get("display_name") or "").strip()
    if not display_name:
        return jsonify({"error": "missing 'display_name'"}), 400

    data = session_store.rename_speaker(user_id, session_id, speaker_id, display_name)
    if data is None:
        return jsonify({"error": "not_found"}), 404

    # Naming a speaker IS the roster enrollment step - see
    # speaker_roster.py's own docstring. This session's own diarization
    # run is the only place that embedding exists; a future session
    # re-diarized after this checks the roster and can auto-apply this
    # same name instead of a fresh "Person N".
    embeddings = session_store.load_speaker_embeddings(user_id, session_id)
    embedding = embeddings.get(speaker_id)
    if embedding:
        speaker_roster.upsert(user_id, display_name, embedding)

    return jsonify(data)


_AUTO_SPEAKER_NAME_RE = re.compile(r"^Person \d+$")


# Same time-overlap assignment DeepSink's mobile app used to do locally
# (SpeakerDiarization.assign/defaultSpeakers) - reimplemented here since
# diarization is now a server-side write, not something the client
# merges into its own copy.
def _apply_diarization(blocks, segments, previous_speakers, user_id, embeddings):
    previous_names = {s["id"]: s["display_name"] for s in previous_speakers}
    updated_blocks = []
    for block in blocks:
        speaker_id = None
        for seg in segments:
            if seg["start"] <= block["start"] < seg["end"]:
                speaker_id = seg["speaker"]
                break
        if speaker_id is None:
            candidates = [seg for seg in segments if seg["start"] <= block["start"]]
            if candidates:
                speaker_id = max(candidates, key=lambda s: s["start"])["speaker"]
        updated = dict(block)
        updated["speaker_id"] = speaker_id
        updated_blocks.append(updated)

    seen_order = []
    for block in sorted(updated_blocks, key=lambda b: b["start"]):
        sid = block.get("speaker_id")
        if sid and sid not in seen_order:
            seen_order.append(sid)

    speakers = []
    for i, sid in enumerate(seen_order):
        previous = previous_names.get(sid)
        if previous and not _AUTO_SPEAKER_NAME_RE.match(previous):
            # A real, user-confirmed name (a previous rename on this
            # exact session, e.g. re-running Detect Speakers after an
            # edit) - keep it, don't let a roster match override a name
            # the user already chose for this speaker in this session.
            display_name = previous
        else:
            # Still just an auto-assigned placeholder (or first time
            # seeing this speaker) - always worth a fresh roster check,
            # since the roster can grow between one Detect Speakers run
            # and the next even for the same session.
            display_name = speaker_roster.match(user_id, embeddings.get(sid)) or f"Person {i + 1}"
        speakers.append({"id": sid, "display_name": display_name})
    return updated_blocks, speakers
