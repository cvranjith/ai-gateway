"""Cross-session speaker recognition — a small, per-user roster of
{"name", "embedding"} entries (pyannote voice-embedding vectors),
matched by cosine similarity against a newly diarized session's own
per-speaker embeddings. Lets a recognized regular get their real name
applied automatically ("Person 1" -> "Alex") instead of only ever
getting a session-local placeholder.

Populated the only way a real name enters the system at all: renaming
a speaker (see deepsink_sessions.py's rename_speaker route) saves/
updates that name's roster entry using the embedding pyannote computed
for that speaker in that session (session_store.py's own, session-local
live_speaker_roster) — no separate enrollment flow needed; naming
someone once IS the enrollment. Diarizing again later (this person, or
anyone else in the same conversation) checks the roster and auto-applies
a matching name instead of "Person N" — see deepsink_sessions.py's
_build_speaker_display_list.

This module also holds match_live_roster()/upsert_live_roster() below,
the same cosine-similarity technique applied WITHIN one session instead
of across them - see their own comments for why that's a separate thing
from the cross-session roster above.

Small scale by design (a personal app's own regulars — tens of people,
not thousands), so brute-force cosine similarity against every roster
entry is instant; no vector index/database needed. Matching is
deliberately conservative (SIMILARITY_THRESHOLD) — a wrong auto-name is
worse than just falling back to "Person N", which leaves the existing,
already-correct manual-rename flow as the fallback either way.
"""

import json
import threading

import numpy as np

import config as gateway_config
from session_store import SESSIONS_DIR

# A starting point, not a calibrated value - checked by hand against one
# real pair of short test recordings of the same person: same-speaker
# cosine similarity came out to ~0.61, comfortably-different-speaker
# pairs would be expected well below that. Exposed as
# "speaker_roster.similarity_threshold" (config.properties / the /ui
# config editor) rather than hardcoded, since the right value depends on
# real usage this repo doesn't have data for yet - raise it if wrong
# names start getting applied, lower it if genuine matches are being
# missed.
DEFAULT_SIMILARITY_THRESHOLD = 0.5


def live_similarity_threshold():
    """Config-driven threshold for match_live_roster() below - a small
    accessor so callers (deepsink_sessions.py) don't need their own
    `import config` just for this one value."""
    return float(gateway_config.get_param("speaker_roster", "live_similarity_threshold", DEFAULT_LIVE_SIMILARITY_THRESHOLD))


_locks_guard = threading.Lock()
_locks = {}


def _lock_for(user_id):
    with _locks_guard:
        if user_id not in _locks:
            _locks[user_id] = threading.Lock()
        return _locks[user_id]


def _roster_path(user_id):
    # Same path-traversal guard as session_store._user_dir - user_id
    # always comes from a validated JWT's `sub` claim, but this is
    # cheap insurance against ever taking one raw from elsewhere.
    if not user_id or "/" in user_id or user_id in (".", ".."):
        raise ValueError(f"invalid user_id: {user_id!r}")
    return SESSIONS_DIR / user_id / "speaker_roster.json"


def _load(user_id):
    path = _roster_path(user_id)
    if not path.exists():
        return {"speakers": []}
    with open(path) as f:
        return json.load(f)


def _save(user_id, config):
    path = _roster_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(config, f, indent=2)
    tmp_path.replace(path)


def _cosine_similarity(a, b):
    a, b = np.array(a, dtype=float), np.array(b, dtype=float)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def match(user_id, embedding):
    """Returns the best-matching roster name for `embedding`, or None if
    nothing clears the configured similarity threshold (including an
    empty roster)."""
    if not embedding:
        return None
    threshold = float(gateway_config.get_param("speaker_roster", "similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD))
    with _lock_for(user_id):
        speakers = _load(user_id).get("speakers", [])
    best_name, best_score = None, threshold
    for entry in speakers:
        score = _cosine_similarity(embedding, entry["embedding"])
        if score > best_score:
            best_name, best_score = entry["name"], score
    return best_name


# A starting point for the SAME matching technique applied within one
# session instead of across them (see deepsink_sessions.py's incremental
# diarization pipeline: fast per-chunk pass, periodic consolidation,
# final catchup - each a separate pyannote call whose own "SPEAKER_00"-
# style labels mean nothing across calls, stitched back into one stable
# identity per session via this same cosine-similarity match). Kept as
# its own config key rather than reusing similarity_threshold above:
# these embeddings are being compared within the same recording
# environment/mic/day, where voices should read as more consistent with
# each other than across entirely different sessions, so the right
# value likely isn't identical - exposed separately so each can be tuned
# against its own real usage once there's data for it.
DEFAULT_LIVE_SIMILARITY_THRESHOLD = 0.5


def match_live_roster(roster, embedding, threshold):
    """Same idea as match() above, but against an arbitrary in-memory
    roster (a session's own, still-growing list of {"id", "embedding"}
    entries) rather than the persistent per-user named one. Returns
    (matched_id, score) - matched_id is None if nothing clears
    `threshold` (including an empty roster)."""
    if not embedding:
        return None, 0.0
    best_id, best_score = None, threshold
    for entry in roster:
        score = _cosine_similarity(embedding, entry["embedding"])
        if score > best_score:
            best_id, best_score = entry["id"], score
    return best_id, best_score


def upsert_live_roster(roster, speaker_id, embedding):
    """Adds/updates an entry in an in-memory live-roster list (mutates
    it in place and also returns it for convenience) - same running-mean
    embedding idea as upsert() above, keyed by a session-local stable id
    instead of a real name."""
    if not embedding:
        return roster
    for entry in roster:
        if entry["id"] == speaker_id:
            n = entry.get("sample_count", 1)
            old = np.array(entry["embedding"], dtype=float)
            new = np.array(embedding, dtype=float)
            entry["embedding"] = (((old * n) + new) / (n + 1)).tolist()
            entry["sample_count"] = n + 1
            return roster
    roster.append({"id": speaker_id, "embedding": embedding, "sample_count": 1})
    return roster


def upsert(user_id, name, embedding):
    """Adds a new roster entry, or averages into an existing same-named
    one — a running mean across every session that name has been
    confirmed in, more robust than trusting any single session's
    embedding alone. Case-insensitive match on name so "Alex" and
    "alex" don't split into two entries."""
    name = (name or "").strip()
    if not name or not embedding:
        return
    with _lock_for(user_id):
        config = _load(user_id)
        speakers = config.setdefault("speakers", [])
        for entry in speakers:
            if entry["name"].strip().lower() == name.lower():
                n = entry.get("sample_count", 1)
                old = np.array(entry["embedding"], dtype=float)
                new = np.array(embedding, dtype=float)
                entry["embedding"] = (((old * n) + new) / (n + 1)).tolist()
                entry["sample_count"] = n + 1
                _save(user_id, config)
                return
        speakers.append({"name": name, "embedding": embedding, "sample_count": 1})
        _save(user_id, config)
