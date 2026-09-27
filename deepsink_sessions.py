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


# Guards ALL diarization work for one session - fast (per-chunk),
# consolidated (periodic ~10-min window), and final (at /finish) passes
# all take this same lock, never run concurrently against each other.
# They'd otherwise race on the same session-local live speaker roster
# (session_store's live_speaker_roster.json) and on which pass's
# transcript_blocks retagging lands last. Fast/consolidated triggers
# acquire non-blocking and just skip when busy (the next chunk, or the
# next consolidation check, naturally catches up); the final catchup at
# /finish blocks until whatever's in flight finishes, since that's the
# one moment that actually needs to wait rather than skip.
_diarize_locks_guard = threading.Lock()
_diarize_locks = {}


def _diarize_lock_for(user_id, session_id):
    key = (user_id, session_id)
    with _diarize_locks_guard:
        if key not in _diarize_locks:
            _diarize_locks[key] = threading.Lock()
        return _diarize_locks[key]


# How much session-absolute audio the "consolidated" tier processes per
# pass - see _maybe_run_consolidation_pass. Deliberately a fixed WINDOW
# (each pass only re-diarizes its own fresh 10 minutes, not everything
# since the session started) rather than a cumulative one: diarizing the
# same earlier audio again on every pass would make total work grow
# quadratically with session length: 10 + 20 + 30 + ... minutes of audio
# processed instead of just 10 + 10 + 10. Speaker identity still carries
# across windows via the live roster's own cosine-similarity matching,
# not by re-processing old audio again.
CONSOLIDATION_WINDOW_SECONDS = 600


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
    # Defaults to True - matches the existing progressive-regen behavior
    # for any client that doesn't send this (the web viewer, older app
    # builds). DeepSink's mobile app can pass false explicitly to start a
    # "record now, polish once at the end" session instead.
    live_notes_enabled = bool(body.get("live_notes_enabled", True))
    category = (body.get("category") or "meeting").strip()
    diarization_enabled = bool(body.get("diarization_enabled", True))
    data = session_store.create_session(
        _current_user_id(), title=title, started_at=body.get("started_at"), is_recording=is_recording,
        live_notes_enabled=live_notes_enabled, category=category, diarization_enabled=diarization_enabled,
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
    allowed = {
        "title", "background_notes", "duration_seconds", "recording_incomplete", "stage",
        "live_notes_enabled", "category", "diarization_enabled",
    }
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
    if "live_notes_enabled" in fields:
        fields["live_notes_enabled"] = bool(fields["live_notes_enabled"])
    if "diarization_enabled" in fields:
        fields["diarization_enabled"] = bool(fields["diarization_enabled"])
    if "category" in fields:
        fields["category"] = (fields["category"] or "meeting").strip()
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
    # Diarization is the only reason this server needs to hold onto raw
    # audio past transcription at all - with it off for this session,
    # there's nothing else that will ever read this chunk's audio file
    # again, so delete it now rather than letting it sit on disk for the
    # rest of the recording (and the whole session-lifetime after that).
    # Safe to just clear the whole chunks dir here rather than only this
    # one file: with diarization off from the start, every earlier chunk
    # already got deleted the same way when IT landed, so this is the
    # only file left in it.
    if not data.get("diarization_enabled", True):
        session_store.delete_chunk_audio(user_id, session_id)
        data = session_store.update_session(user_id, session_id, audio_deleted=True)
    else:
        # Incremental diarization pipeline's fast tier - see
        # _run_fast_diarize_pass and this module's own top-of-file notes
        # on the three-tier design. Best-effort and non-blocking: if a
        # pass is already running for this session, this chunk is simply
        # left uncovered by the fast tier and picked up by the next
        # consolidation window instead, rather than queuing up.
        _trigger_incremental_diarize(user_id, session_id)
    # Only when there's actually something to summarize - an empty (or
    # silent) chunk means an empty transcript, and deepsink_notes treats
    # that as a real error (marks the session "failed"), which is right
    # for an explicit /finish with nothing recorded but wrong to trigger
    # invisibly in the background off a single quiet chunk early in an
    # otherwise-normal recording.
    if data["transcript_blocks"] and data.get("live_notes_enabled", True):
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
    # path is actually taken below (mark_failed, save_notes, and the two
    # cancellation checks below all set it back to False as part of
    # their own write). notes_generation_cancelled is reset here too, in
    # case a previous cancel arrived after its own generation had
    # already finished - it shouldn't discard THIS one.
    session_store.update_session(user_id, session_id, is_generating_notes=True, notes_generation_cancelled=False)

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
            "category": data.get("category") or "meeting",
        })
    except ServiceError as e:
        # A cancel that arrived while Codex was running isn't a real
        # failure from the user's own perspective (they walked away, it
        # didn't break) - don't mark the session failed over it, just
        # clear the spinner and the flag.
        if (session_store.get_session(user_id, session_id) or {}).get("notes_generation_cancelled"):
            cleared = session_store.update_session(
                user_id, session_id, is_generating_notes=False, notes_generation_cancelled=False
            )
            return cleared, None
        session_store.mark_failed(user_id, session_id, e.message)
        return None, (jsonify({"error": e.message}), e.status_code)

    # Soft-cancel: POST .../notes/cancel sets this flag while this call
    # was in flight above. Rather than killing the Codex subprocess (real
    # process-management work not justified for a personal, single-user
    # server), the result is simply discarded here instead of saved -
    # same effect as if this generation had never run. There's never more
    # than one generation in flight per session at once (see the lock in
    # _trigger_background_regen and regenerate_notes's own equivalent),
    # so a single flag is enough to know this result is the cancelled one.
    if (session_store.get_session(user_id, session_id) or {}).get("notes_generation_cancelled"):
        cleared = session_store.update_session(
            user_id, session_id, is_generating_notes=False, notes_generation_cancelled=False
        )
        return cleared, None

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
    # The incremental pipeline's fast/consolidated tiers have already
    # been covering this session as chunks landed (see
    # _trigger_incremental_diarize in upload_chunk) - this is just the
    # final catchup for whatever's left since the last consolidation
    # window, normally well under CONSOLIDATION_WINDOW_SECONDS since the
    # periodic passes were keeping up throughout. Manual "Detect
    # Speakers" (the /diarize route below) still exists for a full
    # from-scratch re-run after an edit, or for a session finished before
    # this pipeline existed. Skips entirely when diarization_enabled is
    # False - and in that case there's no audio left to diarize anyway,
    # since upload_chunk already deleted each chunk's audio as it landed.
    if (data or {}).get("diarization_enabled", True):
        _trigger_final_diarize_catchup(user_id, session_id)
    return jsonify(data)


@bp.route("/sessions/<session_id>/notes/regenerate", methods=["POST"])
@user_auth.require_user
def regenerate_notes(session_id):
    data, error_response = _generate_notes(_current_user_id(), session_id)
    if error_response is not None:
        return error_response
    return jsonify(data)


@bp.route("/sessions/<session_id>/notes/cancel", methods=["POST"])
@user_auth.require_user
def cancel_notes_generation(session_id):
    # Runs on its own request/thread (the gateway's Flask server is
    # threaded) while a POST .../notes/regenerate for the same session is
    # still blocked in another one - clears is_generating_notes right
    # away so the UI's spinner disappears immediately, and sets the flag
    # _generate_notes checks right before it would persist a result, so
    # that result gets discarded instead of saved once Codex actually
    # finishes. See notes_generation_cancelled's own comment in
    # session_store.py for why this is a soft cancel, not a real kill of
    # the underlying Codex subprocess.
    data = session_store.update_session(
        _current_user_id(), session_id, is_generating_notes=False, notes_generation_cancelled=True
    )
    if data is None:
        return jsonify({"error": "not_found"}), 404
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


@bp.route("/action_items", methods=["GET"])
@user_auth.require_user
def list_all_action_items():
    # Cross-session rollup for the "outstanding action items" dashboard
    # - flattens every session's action_items into one list, each item
    # tagged with which session it came from so the client can both
    # display and (via the existing per-item PATCH route above) toggle
    # it without a second round trip. Done items are left out by default
    # since the whole point is "what's still outstanding," but a client
    # can ask for everything to show a completed history too.
    include_done = request.args.get("include_done") == "1"
    items = []
    for session in session_store.list_sessions(_current_user_id()):
        for item in session.get("action_items") or []:
            if not include_done and item.get("is_checked"):
                continue
            items.append({
                **item,
                "session_id": session["id"],
                "session_title": session.get("title") or "Untitled session",
                "session_started_at": session.get("started_at"),
            })
    # Soonest-due first; undated items sink to the bottom (grouped by
    # newest session first within each group) rather than sorting as if
    # an empty due date were "earliest."
    items.sort(key=lambda it: (0 if it.get("due") else 1, it.get("due") or "", it.get("session_started_at") or ""))
    return jsonify({"action_items": items})


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
    """Full, from-scratch, whole-session diarization - the manual
    "Detect Speakers"/"Re-detect Speakers" path (e.g. after editing the
    transcript), distinct from the incremental fast/consolidated/final
    pipeline below that runs automatically during recording. Returns
    (updated_session, error_response) - error_response is a
    (jsonify(...), status) tuple on failure, None on success."""
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

    # A full manual re-run is authoritative over the WHOLE session - reset
    # the incremental pipeline's own state first (its live roster,
    # progress counters, AND the current speakers display list) so this
    # fresh, full-context result becomes the new baseline, rather than
    # getting reconciled against - and diluted by - stale per-chunk
    # guesses the fast/consolidated tiers made earlier in the same
    # recording. Clearing `speakers` doesn't lose a real name the user
    # already gave someone: naming a speaker also enrolls their embedding
    # in the CROSS-session roster (speaker_roster.py), so
    # _build_speaker_display_list's own roster-match fallback re-applies
    # that same name to whichever fresh stable id turns out to be them
    # this time, the same way it would for any other recognized regular.
    session_end = max(
        (c["start_offset_seconds"] + c["duration_seconds"] for c in data["chunks"]), default=0.0
    )
    session_store.save_live_speaker_roster(user_id, session_id, [])
    session_store.update_session(user_id, session_id, speakers=[])
    _reconcile_and_apply(
        user_id, session_id, result, threshold=speaker_roster.live_similarity_threshold(),
        range_start=0.0, range_end=session_end,
    )
    updated = session_store.update_session(
        user_id, session_id,
        diarization_fast_covered_seconds=session_end, diarization_consolidated_seconds=session_end,
    )
    return updated, None


# --- Incremental diarization pipeline (fast / consolidated / final) ---
#
# Runs automatically while a session with diarization_enabled is being
# recorded, so speakers get labelled progressively instead of only once
# at the end. Three tiers, all going through the same _reconcile_and_apply
# so they all stitch identity together via the session's own live speaker
# roster (session_store's live_speaker_roster.json, matched by voice-
# embedding cosine similarity - see speaker_roster.py's
# match_live_roster/upsert_live_roster): pyannote's own "SPEAKER_00"-style
# labels only mean anything within the ONE call that produced them, so
# nothing here relies on them being stable across separate calls.
#
#   fast         - triggered per chunk (upload_chunk), diarizes just
#                  what's landed since the fast tier last ran. Lowest
#                  latency, lowest context (a short pass on its own is
#                  noisier), and the only tier that can be silently
#                  skipped when busy - always covered redundantly by the
#                  next tier anyway.
#   consolidated - triggered once ~CONSOLIDATION_WINDOW_SECONDS of fresh
#                  audio has been fast-covered, re-diarizes that one
#                  window (not the whole session so far - see
#                  CONSOLIDATION_WINDOW_SECONDS's own comment) with more
#                  context, self-correcting whatever the fast tier
#                  guessed for it.
#   final        - triggered at /finish: one last consolidation pass over
#                  whatever's left since the last window, normally well
#                  under CONSOLIDATION_WINDOW_SECONDS since consolidation
#                  keeps pace with fast-tier progress throughout.
#
# All three share _diarize_lock_for - fast/consolidated acquire non-
# blocking and skip when busy, final blocks until the lock is free.

def _trigger_incremental_diarize(user_id, session_id):
    if not deepsink_diarize.is_configured():
        return
    lock = _diarize_lock_for(user_id, session_id)
    if not lock.acquire(blocking=False):
        return
    def run():
        try:
            _run_fast_diarize_pass(user_id, session_id)
            _maybe_run_consolidation_pass(user_id, session_id)
        finally:
            lock.release()
    threading.Thread(target=run, daemon=True).start()


def _run_fast_diarize_pass(user_id, session_id):
    data = session_store.get_session(user_id, session_id)
    if data is None:
        return
    covered = data.get("diarization_fast_covered_seconds", 0.0)
    new_chunks = sorted(
        (c for c in data["chunks"] if c["start_offset_seconds"] >= covered),
        key=lambda c: c["start_offset_seconds"],
    )
    if not new_chunks:
        return

    chunk_parts = []
    for chunk in new_chunks:
        path = session_store.chunk_audio_path(user_id, session_id, chunk["file_name"])
        if not path.exists():
            # Audio for at least one new chunk is already gone (e.g.
            # diarization was toggled off and back on mid-recording) -
            # best-effort tier, so just stop here rather than error;
            # whatever's left keeps waiting for a later pass that may
            # never fully cover this gap, which is an acceptable
            # degradation for having turned diarization off in between.
            return
        chunk_parts.append({
            "audio_base64": base64.b64encode(path.read_bytes()).decode(),
            "start_offset_seconds": chunk["start_offset_seconds"],
        })

    session_store.update_session(user_id, session_id, is_diarizing=True, diarization_error=None)
    try:
        result = deepsink_diarize.handle({"chunks": chunk_parts, "format": "m4a"})
    except ServiceError as e:
        # Best-effort - leave covered-so-far where it was and let the
        # next chunk's fast pass (or the next consolidation window) try
        # again over the combined span, rather than surfacing this as a
        # session-level failure the way a real transcript/notes error
        # would be.
        session_store.update_session(user_id, session_id, is_diarizing=False, diarization_error=e.message)
        return

    new_covered = max(c["start_offset_seconds"] + c["duration_seconds"] for c in new_chunks)
    _reconcile_and_apply(
        user_id, session_id, result, threshold=speaker_roster.live_similarity_threshold(),
        range_start=covered, range_end=new_covered,
    )
    session_store.update_session(user_id, session_id, diarization_fast_covered_seconds=new_covered)


def _maybe_run_consolidation_pass(user_id, session_id):
    data = session_store.get_session(user_id, session_id)
    if data is None:
        return
    fast_covered = data.get("diarization_fast_covered_seconds", 0.0)
    consolidated = data.get("diarization_consolidated_seconds", 0.0)
    if fast_covered - consolidated < CONSOLIDATION_WINDOW_SECONDS:
        return
    _run_consolidation_pass(user_id, session_id, until=consolidated + CONSOLIDATION_WINDOW_SECONDS)


def _run_consolidation_pass(user_id, session_id, until):
    """Diarizes just [diarization_consolidated_seconds, until) as one
    pass, then advances the consolidated marker to `until` regardless of
    whether that window actually had any chunks in it (an empty window -
    e.g. a long pause - still counts as covered, so this doesn't get
    stuck retrying it forever)."""
    data = session_store.get_session(user_id, session_id)
    if data is None:
        return
    start = data.get("diarization_consolidated_seconds", 0.0)
    window_chunks = sorted(
        (c for c in data["chunks"] if start <= c["start_offset_seconds"] < until),
        key=lambda c: c["start_offset_seconds"],
    )
    if not window_chunks:
        session_store.update_session(user_id, session_id, diarization_consolidated_seconds=until)
        return

    chunk_parts = []
    for chunk in window_chunks:
        path = session_store.chunk_audio_path(user_id, session_id, chunk["file_name"])
        if not path.exists():
            return  # leave consolidated where it was; see _run_fast_diarize_pass's own comment
        chunk_parts.append({
            "audio_base64": base64.b64encode(path.read_bytes()).decode(),
            "start_offset_seconds": chunk["start_offset_seconds"],
        })

    session_store.update_session(user_id, session_id, is_diarizing=True, diarization_error=None)
    try:
        result = deepsink_diarize.handle({"chunks": chunk_parts, "format": "m4a"})
    except ServiceError as e:
        session_store.update_session(user_id, session_id, is_diarizing=False, diarization_error=e.message)
        return

    _reconcile_and_apply(
        user_id, session_id, result, threshold=speaker_roster.live_similarity_threshold(),
        range_start=start, range_end=until,
    )
    session_store.update_session(user_id, session_id, diarization_consolidated_seconds=until)


def _trigger_final_diarize_catchup(user_id, session_id):
    if not deepsink_diarize.is_configured():
        return
    def run():
        lock = _diarize_lock_for(user_id, session_id)
        # Blocks (unlike the fast/consolidated triggers) - /finish is the
        # one moment that actually needs to wait for whatever's in flight
        # rather than skip, so the final pass genuinely covers everything.
        lock.acquire()
        try:
            data = session_store.get_session(user_id, session_id)
            if not data or not data.get("chunks"):
                return
            session_end = max(c["start_offset_seconds"] + c["duration_seconds"] for c in data["chunks"])
            if session_end > data.get("diarization_consolidated_seconds", 0.0):
                _run_consolidation_pass(user_id, session_id, until=session_end)
        finally:
            lock.release()
    threading.Thread(target=run, daemon=True).start()


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
    # speaker_roster.py's own docstring. This session's own live speaker
    # roster (not the raw output of any one diarization call - see the
    # incremental pipeline above) is what holds a stable id's best-known
    # embedding; a future session diarized after this checks the
    # cross-session roster and can auto-apply this same name instead of
    # a fresh "Person N".
    roster = session_store.load_live_speaker_roster(user_id, session_id)
    embedding = next((e["embedding"] for e in roster if e["id"] == speaker_id), None)
    if embedding:
        speaker_roster.upsert(user_id, display_name, embedding)

    return jsonify(data)


_AUTO_SPEAKER_NAME_RE = re.compile(r"^Person \d+$")


def _reconcile_and_apply(user_id, session_id, diarize_result, threshold, range_start, range_end):
    """Takes one diarize() call's raw result (its own call-local
    "SPEAKER_00"-style labels, meaningless outside that one call) and:
    1. matches each of its speakers against this session's live roster
       by voice-embedding cosine similarity, reusing a stable id
       ("SPEAKER_1", ...) when it's the same voice as an earlier call
       (fast, consolidated, or a previous call within this same pass),
       minting a new one otherwise;
    2. re-tags every transcript block whose time falls within
       [range_start, range_end) with that stable id - this is what lets
       a later, higher-context pass self-correct an earlier tier's
       guess, since it simply overwrites the same range rather than
       only ever appending;
    3. rebuilds the session's speakers display list from the full,
       now-updated block set.
    Never touches blocks outside that range.

    `range_start`/`range_end` are the audio range the CALLER actually
    asked deepsink_diarize to process (each chunk's own start_offset/
    duration), not inferred from the result's own segments - pyannote's
    reported segments don't necessarily span the exact input audio (it
    trims leading/trailing silence internally), so a block sitting right
    at the edge of what was sent could fall just outside the segments'
    own min/max and never get tagged. Confirmed as a real bug this way:
    a session's very first block (start=0.0) was left with no speaker_id
    at all because the first detected segment started a fraction of a
    second later than 0.0."""
    data = session_store.get_session(user_id, session_id)
    if data is None:
        return
    segments = diarize_result.get("segments", [])
    call_embeddings = diarize_result.get("embeddings", {})
    if not segments:
        session_store.update_session(user_id, session_id, is_diarizing=False)
        return

    roster = session_store.load_live_speaker_roster(user_id, session_id)
    existing_numbers = [
        int(e["id"].split("_")[1]) for e in roster
        if e["id"].startswith("SPEAKER_") and e["id"].split("_")[1].isdigit()
    ]
    next_stable_number = (max(existing_numbers) + 1) if existing_numbers else 1

    call_label_to_stable = {}
    for call_label, embedding in call_embeddings.items():
        matched_id, _ = speaker_roster.match_live_roster(roster, embedding, threshold)
        if matched_id:
            stable_id = matched_id
        else:
            stable_id = f"SPEAKER_{next_stable_number}"
            next_stable_number += 1
        call_label_to_stable[call_label] = stable_id
        speaker_roster.upsert_live_roster(roster, stable_id, embedding)
    session_store.save_live_speaker_roster(user_id, session_id, roster)

    stable_segments = [
        {"start": s["start"], "end": s["end"], "speaker": call_label_to_stable.get(s["speaker"], s["speaker"])}
        for s in segments
    ]

    blocks = data["transcript_blocks"]
    in_range = [b for b in blocks if range_start <= b["start"] < range_end]
    outside_range = [b for b in blocks if not (range_start <= b["start"] < range_end)]
    retagged = _apply_diarization_to_blocks(in_range, stable_segments)
    updated_blocks = sorted(outside_range + retagged, key=lambda b: b["start"])

    speakers = _build_speaker_display_list(updated_blocks, data.get("speakers") or [], user_id, roster)
    session_store.set_speakers(user_id, session_id, updated_blocks, speakers)


# Same time-overlap assignment DeepSink's mobile app used to do locally
# (SpeakerDiarization.assign/defaultSpeakers) before diarization became a
# server-side write - given a set of blocks and (already stable-id)
# segments, tags each block with whichever segment covers its start time.
def _apply_diarization_to_blocks(blocks, segments):
    updated_blocks = []
    for block in blocks:
        speaker_id = None
        for seg in segments:
            if seg["start"] <= block["start"] < seg["end"]:
                speaker_id = seg["speaker"]
                break
        if speaker_id is None:
            earlier = [seg for seg in segments if seg["start"] <= block["start"]]
            if earlier:
                speaker_id = max(earlier, key=lambda s: s["start"])["speaker"]
            elif segments:
                # Nothing starts at or before this block - pyannote
                # trimmed a bit of leading silence internally, so the
                # very first block(s) can start slightly earlier than
                # its first detected segment. Falling forward to that
                # first segment (rather than leaving speaker_id
                # unset) is what actually fixed a real, observed bug:
                # a session's first block was silently left untagged,
                # and with it the whole speakers list came back empty.
                speaker_id = min(segments, key=lambda s: s["start"])["speaker"]
        updated = dict(block)
        updated["speaker_id"] = speaker_id
        updated_blocks.append(updated)
    return updated_blocks


def _build_speaker_display_list(blocks, previous_speakers, user_id, roster):
    """Rebuilds the {"id", "display_name"} list shown to the user from
    the full current set of (stable-id-tagged) blocks. Once a stable id
    has any display name - auto or a real, user-chosen one - it keeps it
    on every later reconciliation; only a stable id that's brand new to
    this session gets a fresh number (or a cross-session roster-matched
    real name). Without this, the SAME speaker's "Person N" could
    renumber every time a later tier's pass reconciles the session,
    which would look like random relabeling rather than the intended
    "self-correct silently, keep names stable" behavior."""
    previous_by_id = {s["id"]: s["display_name"] for s in previous_speakers}
    roster_by_id = {e["id"]: e["embedding"] for e in roster}

    seen_order = []
    for block in sorted(blocks, key=lambda b: b["start"]):
        sid = block.get("speaker_id")
        if sid and sid not in seen_order:
            seen_order.append(sid)

    used_numbers = set()
    for name in previous_by_id.values():
        m = _AUTO_SPEAKER_NAME_RE.match(name or "")
        if m:
            used_numbers.add(int(name.split(" ")[1]))
    next_person_number = (max(used_numbers) + 1) if used_numbers else 1

    speakers = []
    for sid in seen_order:
        previous = previous_by_id.get(sid)
        if previous:
            speakers.append({"id": sid, "display_name": previous})
            continue
        embedding = roster_by_id.get(sid)
        matched_name = speaker_roster.match(user_id, embedding) if embedding else None
        if matched_name:
            display_name = matched_name
        else:
            display_name = f"Person {next_person_number}"
            next_person_number += 1
        speakers.append({"id": sid, "display_name": display_name})
    return speakers
