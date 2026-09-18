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
import uuid

from flask import Blueprint, jsonify, request

import session_store
import user_auth
from services import deepsink_diarize, deepsink_notes, deepsink_transcribe
from services.errors import ServiceError

bp = Blueprint("deepsink", __name__, url_prefix="/deepsink")


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
    data = session_store.create_session(_current_user_id(), title=title, started_at=body.get("started_at"))
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
    allowed = {"title", "background_notes", "duration_seconds", "recording_incomplete"}
    fields = {k: v for k, v in body.items() if k in allowed}
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
    return jsonify(data)


def _generate_notes(session_id):
    user_id = _current_user_id()
    data = _session_or_404(session_id)
    if data is None:
        return None, (jsonify({"error": "not_found"}), 404)

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
    data, error_response = _generate_notes(session_id)
    if error_response is not None:
        return error_response
    return jsonify(data)


@bp.route("/sessions/<session_id>/notes/regenerate", methods=["POST"])
@user_auth.require_user
def regenerate_notes(session_id):
    data, error_response = _generate_notes(session_id)
    if error_response is not None:
        return error_response
    return jsonify(data)


@bp.route("/sessions/<session_id>/action_items/<item_id>", methods=["PATCH"])
@user_auth.require_user
def toggle_action_item(session_id, item_id):
    body = request.get_json(silent=True) or {}
    if "is_checked" not in body:
        return jsonify({"error": "missing 'is_checked'"}), 400
    data = session_store.toggle_action_item(_current_user_id(), session_id, item_id, bool(body["is_checked"]))
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


@bp.route("/sessions/<session_id>/diarize", methods=["POST"])
@user_auth.require_user
def diarize_session(session_id):
    user_id = _current_user_id()
    data = _session_or_404(session_id)
    if data is None:
        return jsonify({"error": "not_found"}), 404

    chunk_parts = []
    for chunk in data["chunks"]:
        path = session_store.chunk_audio_path(user_id, session_id, chunk["file_name"])
        if not path.exists():
            return jsonify({"error": "audio for this session is no longer available"}), 409
        chunk_parts.append({
            "audio_base64": base64.b64encode(path.read_bytes()).decode(),
            "start_offset_seconds": chunk["start_offset_seconds"],
        })

    session_store.update_session(user_id, session_id, is_diarizing=True, diarization_error=None)
    try:
        result = deepsink_diarize.handle({"chunks": chunk_parts, "format": "m4a"})
    except ServiceError as e:
        session_store.update_session(user_id, session_id, is_diarizing=False, diarization_error=e.message)
        return jsonify({"error": e.message}), e.status_code

    updated_blocks, speakers = _apply_diarization(
        data["transcript_blocks"], result.get("segments", []), data.get("speakers") or []
    )
    updated = session_store.set_speakers(user_id, session_id, updated_blocks, speakers)
    return jsonify(updated)


# Same time-overlap assignment DeepSink's mobile app used to do locally
# (SpeakerDiarization.assign/defaultSpeakers) - reimplemented here since
# diarization is now a server-side write, not something the client
# merges into its own copy.
def _apply_diarization(blocks, segments, previous_speakers):
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
    speakers = [
        {"id": sid, "display_name": previous_names.get(sid, f"Person {i + 1}")}
        for i, sid in enumerate(seen_order)
    ]
    return updated_blocks, speakers
