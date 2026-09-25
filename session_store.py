"""File-backed session store for DeepSink's server-persisted sessions.

One JSON file per session (sessions_data/<user_id>/<session_id>/session.json),
audio chunks alongside it (.../<session_id>/chunks/<file_name>). This Mac
is the source of truth once a session is created here - mobile (and any
future web client) only ever holds a display copy and sends explicit
write requests for the few things that change (a chunk arriving, a
checkbox toggled, notes regenerated). See deepsink_sessions.py for the
HTTP surface built on top of this.

Scoped per user_id (see user_auth.py) from the start, even though this
is a single-user app today - every path below already takes it as the
first argument, so registering a second user later needs no data
migration, just another entry in users.json.

A per-(user_id, session_id) threading.Lock (ai-gateway runs Flask with
threaded=True, so two requests for the same session really can race -
e.g. two chunk uploads landing close together) serializes each
session's own read-modify-write cycle; different sessions never block
each other. Plain JSON files rather than a database on purpose: with
the server as the only writer, there's no concurrent-access problem a
database would be solving, and files stay directly inspectable/debuggable.
"""

import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path(__file__).parent / "sessions_data"
SESSIONS_DIR.mkdir(exist_ok=True)

_locks_guard = threading.Lock()
_locks = {}


def _lock_for(user_id, session_id):
    key = (user_id, session_id)
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _user_dir(user_id):
    # user_id always comes from a validated JWT's `sub` claim (see
    # user_auth.require_user), itself only ever set by create_user() -
    # never taken raw from a URL/body - but a plain name-component check
    # here costs nothing and matches the same defensive pattern
    # gateway.py's serve_file already uses for untrusted path segments.
    if not user_id or "/" in user_id or user_id in (".", ".."):
        raise ValueError(f"invalid user_id: {user_id!r}")
    return SESSIONS_DIR / user_id


def _session_dir(user_id, session_id):
    return _user_dir(user_id) / session_id


def _session_path(user_id, session_id):
    return _session_dir(user_id, session_id) / "session.json"


def _now():
    return datetime.now(timezone.utc).isoformat()


# Every field create_session has ever added, besides the handful always
# present from day one (id/title/started_at/...). A session written
# before a given field existed simply doesn't have that key in its JSON
# - harmless for this Python code (dict.get(...) everywhere already
# tolerates that), but DeepSink's mobile app decodes the whole session
# as a non-optional Swift struct, so a single old session missing a
# newer key breaks decoding of the ENTIRE sessions list response (one
# bad element fails the whole array decode) - confirmed as a real,
# reported bug: older sessions disappeared from the phone (still
# visible on the web viewer, which is far more tolerant of missing
# JSON keys) the moment live_notes_enabled/notes_generation_cancelled
# were added, while a session created after that kept working. Backfilling
# on every read, rather than a one-off migration script, means this
# can never happen again regardless of which client reads an old file
# next or how old it is.
_SCHEMA_DEFAULTS = {
    "recording_incomplete": False,
    "audio_deleted": False,
    "chunks": list,
    "transcript_blocks": list,
    "notes": None,
    "action_items": list,
    "markers": list,
    "speakers": list,
    "is_diarizing": False,
    "diarization_error": None,
    "is_generating_notes": False,
    "is_recording": False,
    "prep_chat": list,
    "materials": list,
    "background_summary": None,
    "title_is_manual": False,
    "live_notes_enabled": True,
    "notes_generation_cancelled": False,
}


def _backfill_defaults(data):
    for key, default in _SCHEMA_DEFAULTS.items():
        if key not in data:
            data[key] = default() if callable(default) else default
    return data


def _read(user_id, session_id):
    path = _session_path(user_id, session_id)
    if not path.exists():
        return None
    with open(path) as f:
        data = json.load(f)
    return _backfill_defaults(data)


def _write(user_id, session_id, data):
    _session_dir(user_id, session_id).mkdir(parents=True, exist_ok=True)
    path = _session_path(user_id, session_id)
    # Write-then-rename rather than writing the real path directly, so a
    # request that crashes or is killed mid-write can never leave a
    # truncated, unparseable session.json behind for the next reader.
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    tmp_path.replace(path)


def create_session(user_id, title, started_at=None, is_recording=False, live_notes_enabled=True):
    session_id = str(uuid.uuid4())
    data = {
        "id": session_id,
        "title": title,
        "started_at": started_at or _now(),
        "duration_seconds": 0,
        "background_notes": "",
        # "ready" (an empty, nothing-pending session - a true, if
        # slightly vacuous, description) rather than "recording" for a
        # session nobody's actually recording yet - e.g. one prepared
        # ahead of time from the web viewer, title-only, no phone
        # attached to it at all. DeepSink's own mobile app is the one
        # caller that passes is_recording=True (see below), since it
        # only ever creates a session as part of actually starting to
        # record into it immediately.
        "stage": "recording" if is_recording else "ready",
        "chunks_done": 0,
        "chunks_total": 0,
        "failure_reason": None,
        "recording_incomplete": False,
        "audio_deleted": False,
        "ready_at": None,
        "created_at": _now(),
        "chunks": [],
        "transcript_blocks": [],
        "notes": None,
        "action_items": [],
        "markers": [],
        "speakers": [],
        "is_diarizing": False,
        "diarization_error": None,
        "is_generating_notes": False,
        # Deliberately separate from `stage`: stage can now cycle
        # through uploading/ready multiple times *during* one ongoing
        # recording (progressive notes regen after every chunk - see
        # _trigger_background_regen), so "stage == ready" stopped
        # reliably meaning "recording has stopped" the moment that
        # feature shipped. This is the real signal for that - true from
        # creation (or a Resume Recording PATCH) until /finish actually
        # runs, full stop, regardless of how many times stage flips in
        # between. live_preview's viewer-facing gating and the web
        # viewer's live-stream subscription both key off this now, not
        # stage.
        "is_recording": is_recording,
        # The "Prepare Me" conversation (deepsink_prepare), surfaced
        # inside the web viewer's Background tab as an "ask me more
        # questions" enrichment loop rather than its own tab - a
        # persisted, multi-turn thread, unlike the Engage/Chat tab's
        # one-shot Q&A (which never gets saved at all).
        # [{"role": "user"|"assistant", "content": "...", "created_at": "..."}, ...],
        # oldest first. Runs *before* a recording exists, but isn't
        # cleared if one starts - background_notes is still what feeds
        # actual notes generation; this is scratch space for getting
        # there.
        "prep_chat": [],
        # Uploaded PDF/text materials for background prep - metadata
        # only here ([{"id", "filename", "format", "uploaded_at",
        # "char_count"}, ...]); each one's actual extracted text lives
        # in its own sibling file (see save/load_material_text below),
        # same "not embedded in session.json" reasoning as speaker
        # embeddings - a client reading an ordinary session has no
        # reason to receive potentially-large extracted document text
        # on every fetch.
        "materials": [],
        # Regenerated by POST /background/process (deepsink_background_extract),
        # combining background_notes + materials' text + prep_chat into
        # one compact, structured summary - {"summary", "key_points",
        # "intent", "audience", "main_speaker"} - or None before the
        # first Process. Deliberately separate from `notes` (which is
        # the *meeting's* summary, generated from a real transcript
        # after recording) - this is the *prep* summary, generated
        # before anything's been recorded at all.
        "background_summary": None,
        # False until the user actually renames the session themselves
        # (see deepsink_sessions.py's patch_session, the only place this
        # ever flips to True) - save_notes checks this before touching
        # `title` at all, so an auto-generated title keeps updating
        # freely (from Codex's own guess, refined as more transcript
        # comes in) right up until a human deliberately overrides it,
        # and never again after that.
        "title_is_manual": False,
        # When False, a landing chunk's transcript still gets stored as
        # usual, but _trigger_background_regen (deepsink_sessions.py) skips
        # firing notes/action-items generation for it - "record now,
        # polish once at the end" instead of the default progressive
        # regen-after-every-chunk behavior. Settable at creation and
        # PATCH-able mid-recording (see patch_session), so it can be
        # flipped either way without stopping the recording.
        "live_notes_enabled": bool(live_notes_enabled),
        # Set by POST .../notes/cancel while a generation is in flight;
        # checked by _generate_notes right before it would persist a
        # result, so that result is discarded instead of saved - a
        # "soft cancel" (the Codex subprocess itself still runs to
        # completion server-side) rather than actually killing a
        # process, which is real process-management work not justified
        # for a personal, single-user server. Reset to False at the
        # start of every new generation.
        "notes_generation_cancelled": False,
    }
    with _lock_for(user_id, session_id):
        _write(user_id, session_id, data)
    return data


def get_session(user_id, session_id):
    with _lock_for(user_id, session_id):
        return _read(user_id, session_id)


def list_sessions(user_id):
    sessions = []
    user_dir = _user_dir(user_id)
    if not user_dir.exists():
        return sessions
    for entry in user_dir.iterdir():
        if not entry.is_dir():
            continue
        data = _read(user_id, entry.name)
        if data:
            sessions.append(data)
    sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    return sessions


def update_session(user_id, session_id, **fields):
    with _lock_for(user_id, session_id):
        data = _read(user_id, session_id)
        if data is None:
            return None
        data.update(fields)
        _write(user_id, session_id, data)
        return data


def delete_session(user_id, session_id):
    with _lock_for(user_id, session_id):
        session_dir = _session_dir(user_id, session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir)


def chunk_audio_path(user_id, session_id, file_name):
    return _session_dir(user_id, session_id) / "chunks" / file_name


def save_chunk_audio(user_id, session_id, file_name, audio_bytes):
    chunks_dir = _session_dir(user_id, session_id) / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    (chunks_dir / file_name).write_bytes(audio_bytes)


def delete_chunk_audio(user_id, session_id):
    chunks_dir = _session_dir(user_id, session_id) / "chunks"
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir)


def append_chunk(user_id, session_id, chunk_index, file_name, start_offset_seconds, duration_seconds, blocks):
    with _lock_for(user_id, session_id):
        data = _read(user_id, session_id)
        if data is None:
            return None
        data["chunks"].append({
            "index": chunk_index,
            "file_name": file_name,
            "start_offset_seconds": start_offset_seconds,
            "duration_seconds": duration_seconds,
            "is_transcribed": True,
        })
        data["transcript_blocks"].extend(blocks)
        data["chunks_done"] = len(data["chunks"])
        data["chunks_total"] = len(data["chunks"])
        data["stage"] = "uploading"
        _write(user_id, session_id, data)
        return data


def save_notes(user_id, session_id, notes_payload, action_items):
    with _lock_for(user_id, session_id):
        data = _read(user_id, session_id)
        if data is None:
            return None
        # Never overwrites a title the user has deliberately set - see
        # title_is_manual's own comment in create_session. Without this,
        # a manual rename would survive only until the *next* chunk's
        # background notes regen (which now fires automatically after
        # every chunk, not just once at the end), silently reverting it
        # back to whatever Codex generates - confirmed as a real bug the
        # user actually hit, not a hypothetical.
        if notes_payload.get("title") and not data.get("title_is_manual"):
            data["title"] = notes_payload["title"]
        data["notes"] = notes_payload
        data["action_items"] = action_items
        data["ready_at"] = _now()
        data["stage"] = "ready"
        data["failure_reason"] = None
        data["is_generating_notes"] = False
        _write(user_id, session_id, data)
        return data


def mark_failed(user_id, session_id, reason):
    return update_session(user_id, session_id, stage="failed", failure_reason=reason, is_generating_notes=False)


def update_action_item(user_id, session_id, item_id, **fields):
    # Generalized from the original toggle_action_item (is_checked
    # only) to also cover owner/due - the model already leaves either
    # null when it's genuinely not inferable from the transcript, and
    # that's meant to be a fillable empty field, not a dead end.
    with _lock_for(user_id, session_id):
        data = _read(user_id, session_id)
        if data is None:
            return None
        found = False
        for item in data["action_items"]:
            if item["id"] == item_id:
                item.update(fields)
                found = True
                break
        if not found:
            return None
        _write(user_id, session_id, data)
        return data


def add_marker(user_id, session_id, offset_seconds, comment):
    with _lock_for(user_id, session_id):
        data = _read(user_id, session_id)
        if data is None:
            return None
        marker = {
            "id": str(uuid.uuid4()),
            "offset_seconds": offset_seconds,
            "comment": comment,
            "created_at": _now(),
        }
        data["markers"].append(marker)
        _write(user_id, session_id, data)
        return data


def set_speakers(user_id, session_id, transcript_blocks, speakers):
    with _lock_for(user_id, session_id):
        data = _read(user_id, session_id)
        if data is None:
            return None
        data["transcript_blocks"] = transcript_blocks
        data["speakers"] = speakers
        data["is_diarizing"] = False
        data["diarization_error"] = None
        _write(user_id, session_id, data)
        return data


def rename_speaker(user_id, session_id, speaker_id, display_name):
    with _lock_for(user_id, session_id):
        data = _read(user_id, session_id)
        if data is None:
            return None
        found = False
        for speaker in data["speakers"]:
            if speaker["id"] == speaker_id:
                speaker["display_name"] = display_name
                found = True
                break
        if not found:
            return None
        _write(user_id, session_id, data)
        return data


# Per-speaker voice-embedding vectors from the session's last diarization
# run (deepsink_diarize's "embeddings" result) - kept in their own file
# alongside session.json rather than as a field on it, since these are
# only ever needed once, at rename time (to feed speaker_roster.upsert),
# not something any client (mobile/web) has a reason to fetch on every
# ordinary session read. Session-local by nature - "SPEAKER_00" only
# means something within the one diarization run that produced it, so
# this is overwritten wholesale on every re-diarize, never merged.

def _speaker_embeddings_path(user_id, session_id):
    return _session_dir(user_id, session_id) / "speaker_embeddings.json"


def save_speaker_embeddings(user_id, session_id, embeddings):
    path = _speaker_embeddings_path(user_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(embeddings, f)
    tmp_path.replace(path)


def load_speaker_embeddings(user_id, session_id):
    path = _speaker_embeddings_path(user_id, session_id)
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


# Background prep materials (uploaded PDF/text files) - metadata lives
# on the session itself (data["materials"]), extracted text in its own
# sibling file per material, same reasoning as speaker embeddings above:
# only ever needed once, when (re-)running background/process, not
# something an ordinary session fetch should have to carry. The raw
# uploaded file itself is deliberately never kept - only its extracted
# text, which is the only thing actually used anywhere.

def _material_text_path(user_id, session_id, material_id):
    return _session_dir(user_id, session_id) / "materials" / f"{material_id}.txt"


def save_material_text(user_id, session_id, material_id, text):
    path = _material_text_path(user_id, session_id, material_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def load_material_text(user_id, session_id, material_id):
    path = _material_text_path(user_id, session_id, material_id)
    if not path.exists():
        return ""
    return path.read_text()


def delete_material_text(user_id, session_id, material_id):
    path = _material_text_path(user_id, session_id, material_id)
    if path.exists():
        path.unlink()


def add_material(user_id, session_id, material_id, filename, fmt, char_count):
    with _lock_for(user_id, session_id):
        data = _read(user_id, session_id)
        if data is None:
            return None
        data.setdefault("materials", []).append({
            "id": material_id,
            "filename": filename,
            "format": fmt,
            "uploaded_at": _now(),
            "char_count": char_count,
        })
        _write(user_id, session_id, data)
        return data


def remove_material(user_id, session_id, material_id):
    with _lock_for(user_id, session_id):
        data = _read(user_id, session_id)
        if data is None:
            return None
        before = len(data.get("materials") or [])
        data["materials"] = [m for m in (data.get("materials") or []) if m["id"] != material_id]
        if len(data["materials"]) == before:
            return None
        _write(user_id, session_id, data)
        return data
