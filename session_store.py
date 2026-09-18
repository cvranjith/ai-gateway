"""File-backed session store for DeepSink's server-persisted sessions.

One JSON file per session (sessions_data/<id>/session.json), audio
chunks alongside it (sessions_data/<id>/chunks/<file_name>). This Mac
is the source of truth once a session is created here - mobile (and any
future web client) only ever holds a display copy and sends explicit
write requests for the few things that change (a chunk arriving, a
checkbox toggled, notes regenerated). See deepsink_sessions.py for the
HTTP surface built on top of this.

A per-session threading.Lock (ai-gateway runs Flask with threaded=True,
so two requests for the same session really can race - e.g. two chunk
uploads landing close together) serializes each session's own
read-modify-write cycle; different sessions never block each other.
Plain JSON files rather than a database on purpose: with the server as
the only writer, there's no concurrent-access problem a database would
be solving, and files stay directly inspectable/debuggable.
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


def _lock_for(session_id):
    with _locks_guard:
        if session_id not in _locks:
            _locks[session_id] = threading.Lock()
        return _locks[session_id]


def _session_dir(session_id):
    return SESSIONS_DIR / session_id


def _session_path(session_id):
    return _session_dir(session_id) / "session.json"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read(session_id):
    path = _session_path(session_id)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _write(session_id, data):
    _session_dir(session_id).mkdir(parents=True, exist_ok=True)
    path = _session_path(session_id)
    # Write-then-rename rather than writing the real path directly, so a
    # request that crashes or is killed mid-write can never leave a
    # truncated, unparseable session.json behind for the next reader.
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    tmp_path.replace(path)


def create_session(title, started_at=None):
    session_id = str(uuid.uuid4())
    data = {
        "id": session_id,
        "title": title,
        "started_at": started_at or _now(),
        "duration_seconds": 0,
        "background_notes": "",
        "stage": "recording",
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
    }
    with _lock_for(session_id):
        _write(session_id, data)
    return data


def get_session(session_id):
    with _lock_for(session_id):
        return _read(session_id)


def list_sessions():
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions
    for entry in SESSIONS_DIR.iterdir():
        if not entry.is_dir():
            continue
        data = _read(entry.name)
        if data:
            sessions.append(data)
    sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    return sessions


def update_session(session_id, **fields):
    with _lock_for(session_id):
        data = _read(session_id)
        if data is None:
            return None
        data.update(fields)
        _write(session_id, data)
        return data


def delete_session(session_id):
    with _lock_for(session_id):
        session_dir = _session_dir(session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir)


def chunk_audio_path(session_id, file_name):
    return _session_dir(session_id) / "chunks" / file_name


def save_chunk_audio(session_id, file_name, audio_bytes):
    chunks_dir = _session_dir(session_id) / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    (chunks_dir / file_name).write_bytes(audio_bytes)


def delete_chunk_audio(session_id):
    chunks_dir = _session_dir(session_id) / "chunks"
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir)


def append_chunk(session_id, chunk_index, file_name, start_offset_seconds, duration_seconds, blocks):
    with _lock_for(session_id):
        data = _read(session_id)
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
        _write(session_id, data)
        return data


def save_notes(session_id, notes_payload, action_items):
    with _lock_for(session_id):
        data = _read(session_id)
        if data is None:
            return None
        if notes_payload.get("title"):
            data["title"] = notes_payload["title"]
        data["notes"] = notes_payload
        data["action_items"] = action_items
        data["ready_at"] = _now()
        data["stage"] = "ready"
        data["failure_reason"] = None
        _write(session_id, data)
        return data


def mark_failed(session_id, reason):
    return update_session(session_id, stage="failed", failure_reason=reason)


def toggle_action_item(session_id, item_id, is_checked):
    with _lock_for(session_id):
        data = _read(session_id)
        if data is None:
            return None
        for item in data["action_items"]:
            if item["id"] == item_id:
                item["is_checked"] = is_checked
                break
        _write(session_id, data)
        return data


def add_marker(session_id, offset_seconds, comment):
    with _lock_for(session_id):
        data = _read(session_id)
        if data is None:
            return None
        marker = {
            "id": str(uuid.uuid4()),
            "offset_seconds": offset_seconds,
            "comment": comment,
            "created_at": _now(),
        }
        data["markers"].append(marker)
        _write(session_id, data)
        return data


def set_speakers(session_id, transcript_blocks, speakers):
    with _lock_for(session_id):
        data = _read(session_id)
        if data is None:
            return None
        data["transcript_blocks"] = transcript_blocks
        data["speakers"] = speakers
        data["is_diarizing"] = False
        data["diarization_error"] = None
        _write(session_id, data)
        return data
