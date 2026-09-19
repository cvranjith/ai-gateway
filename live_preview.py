"""In-memory, per-session "live preview" text - the rough, on-device
transcript a phone is currently recognizing while a chunk is still in
progress, distinct from the accurate Whisper transcript_blocks that
land once that chunk actually finishes uploading (session_store.py).

Deliberately NOT persisted to session.json: this is a transient
"what's being said right now" signal, gone the moment recording stops
or the gateway restarts, never something a client should expect to
read back later. A plain in-memory dict is enough - single process
(Flask threaded=True, no multi-worker deployment here), and losing it
on restart is the correct behavior, not a bug to work around.

Viewer counting is what makes this demand-driven rather than always-on:
a phone has no reason to keep pushing live text (battery, a network
call every ~1.5s) when nobody's actually watching the web viewer's
Transcript tab for this session. `add_viewer`/`remove_viewer` track
that; the phone polls `viewer_count` on its own schedule (see
deepsink_sessions.py's /live_preview/viewers) and only starts pushing
once it sees a real number there.
"""

import threading
import time

_lock = threading.Lock()
_state = {}  # key -> {"text": str, "updated_at": float, "viewers": int}


def _entry(key):
    return _state.setdefault(key, {"text": "", "updated_at": 0.0, "viewers": 0})


def set_text(key, text):
    with _lock:
        entry = _entry(key)
        entry["text"] = text
        entry["updated_at"] = time.time()


def get_text(key):
    with _lock:
        entry = _state.get(key)
        return (entry["text"], entry["updated_at"]) if entry else ("", 0.0)


def add_viewer(key):
    with _lock:
        _entry(key)["viewers"] += 1


def remove_viewer(key):
    with _lock:
        entry = _state.get(key)
        if entry:
            entry["viewers"] = max(0, entry["viewers"] - 1)


def viewer_count(key):
    with _lock:
        entry = _state.get(key)
        return entry["viewers"] if entry else 0


def clear(key):
    with _lock:
        _state.pop(key, None)
