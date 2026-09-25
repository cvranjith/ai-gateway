"""service_id: "deepsink_notes"

params:
    transcript       (str, required) - full session transcript text
    marker_hints     (list, optional) - [{"offset_seconds": <float>, "comment": "<str>"}, ...]
                     moments the user tapped "mark this moment" on while
                     recording - passed to the model as a hint about what
                     mattered, per requirement-deepsink-mobile.md FR-4.
    background_notes (str, optional) - free-text context the user typed
                     about the session (who's in the room, the agenda,
                     acronyms/jargon, prior history) - not part of the
                     transcript, just background for interpreting it.
    meeting_date      (str, optional) - the session's own started_at
                     date (e.g. "2026-09-24"), so relative references
                     in the transcript ("by next Friday", "end of the
                     month") can resolve to a real date instead of
                     staying vague.

result: the deepsink.notes JSON shape from requirement-deepsink-mobile.md
FR-4, returned as a real JSON object (not a string) - DeepSink decodes
this straight into SessionNotesPayload:
    {
      "title": "...", "summary": "...",
      "key_points": ["..."], "decisions": ["..."],
      "action_items": [{"text": "...", "owner": "You"|"<name>"|null, "due": "<date>"|null}],
      "open_questions": ["..."]
    }
`owner`/`due` are null (not a placeholder string like "unknown") when
the transcript doesn't clearly support a guess - a client shows that
as an empty, fillable field, not a word cluttering the UI. "You" is
the literal convention for whoever's speaking/recording (first-person
commitments - "I'll send that over"), so a later "show only mine" view
has something concrete to filter on.

Summarizes via Codex CLI, non-interactively - same subprocess pattern as
youtube_summarizer._summarize_with_codex (instructions as the CLI arg,
the actual text to work on piped via stdin, since a 2-hour meeting
transcript is far too long for a command-line argument), just a JSON-out
prompt instead of a plain-text one.
"""

import json
import os
import re
import subprocess
import tempfile

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "deepsink_notes"
DEFAULT_CODEX_TIMEOUT_SECONDS = 180

# Same output JSON schema across all three - the client (SessionNotesPayload)
# never needs to know which category produced a given session's notes, only
# the emphasis Codex is told to give each field shifts. "meeting" is the
# original, unchanged instructions; the other two are for the same session
# categories DeepSink's own recording-start screen lets you pick, per the
# 2026-09-25 conversation ("sometimes I want to use this as a self to-do
# creator... or just keep it in my memory").
_SCHEMA_KEYS = """Output ONLY a single JSON object, no markdown code fences, no commentary before or after it, with exactly these keys:
- "title": a short (under 8 words) descriptive title
- "summary": a short summary of what was said
- "key_points": array of strings, the main points
- "decisions": array of strings, decisions that were made (empty array if none)
- "action_items": array of objects {"text": str, "owner": "You" | "<name>" | null, "due": "<date>" | null}
- "open_questions": array of strings, questions raised but not resolved (empty array if none)

For each action item's "owner": use the literal string "You" when it's clearly a first-person commitment by whoever is speaking/recording ("I'll send that over", "let me follow up on X"); use a real name when the transcript names who it's for; use null (not a placeholder word) when genuinely unclear - leave it for a human to fill in rather than guessing. For "due": if the transcript gives a date, or a relative one ("by next Friday", "end of the month") that you can resolve against the date provided below, use that real date; otherwise null."""

INSTRUCTIONS_BY_CATEGORY = {
    "meeting": f"""You are producing structured meeting notes from a raw speech-to-text transcript pasted below. The transcript may contain transcription errors, filler words, and missing punctuation - do your best to infer intent rather than transcribing literally.

{_SCHEMA_KEYS}

"summary" should be 2-4 sentences covering what the meeting covered. If the transcript doesn't clearly support a field, use an empty array (or empty string for "summary") rather than inventing content. If the user marked specific moments as important (see below, if present), weight those moments more heavily when deciding what counts as a key point, decision, or action item. If background notes are provided, use them to interpret the transcript correctly (names, acronyms, context) - they are not meeting content themselves and should not be echoed back into the summary or key points.""",

    "todo": f"""You are extracting a personal to-do list from a raw speech-to-text transcript pasted below - this is someone thinking out loud or dictating tasks to themselves, NOT a meeting. The transcript may contain transcription errors, filler words, and missing punctuation - do your best to infer intent rather than transcribing literally.

{_SCHEMA_KEYS}

Treat "action_items" as the whole point of this - capture every actionable thing said, even something said only in passing, as its own action item. Default an action item's "owner" to "You" unless the transcript clearly assigns it to someone else. "summary" should be a single short sentence at most (can be empty). "key_points" should usually just restate the action items in short form, or be empty if action_items already covers everything. "decisions" and "open_questions" will normally be empty - only fill them if something genuinely fits that isn't better captured as an action item.""",

    "voice_note": f"""You are producing a light note from a raw speech-to-text transcript pasted below - this is a personal voice memo / note-to-self, NOT a meeting. The transcript may contain transcription errors, filler words, and missing punctuation - do your best to infer intent rather than transcribing literally.

{_SCHEMA_KEYS}

Produce a free-form "summary" (2-5 sentences) that captures what was said and why it might matter later - don't force meeting-style structure onto it. "key_points" should only be used if there are genuinely distinct points worth calling out separately; often this can be empty with everything captured in the summary instead. "action_items" should stay empty unless something is an unmistakable concrete commitment ("I need to call the dentist tomorrow") - most voice notes have none. "decisions" and "open_questions" will almost always be empty.""",
}


def _format_offset(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _build_marker_section(marker_hints):
    if not marker_hints:
        return ""
    lines = ["Moments the user flagged as important while recording (not part of the transcript itself):"]
    for hint in marker_hints:
        offset = hint.get("offset_seconds")
        comment = (hint.get("comment") or "").strip()
        ts = _format_offset(offset) if isinstance(offset, (int, float)) else "?"
        lines.append(f"- [{ts}]" + (f" {comment}" if comment else " (no comment)"))
    return "\n".join(lines) + "\n\n"


def _build_background_section(background_notes):
    if not background_notes:
        return ""
    return f"Background provided by the user (not part of the transcript itself):\n{background_notes}\n\n"


def _build_meeting_date_section(meeting_date):
    if not meeting_date:
        return ""
    return f"This meeting's own date, for resolving relative due dates (\"next Friday\", etc.): {meeting_date}\n\n"


def _extract_json(raw_text):
    text = raw_text.strip()
    # Codex sometimes wraps output in a ```json ... ``` fence despite being
    # asked not to - strip that before trying to parse.
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        # Fall back to the first '{' through the last '}' - covers the case
        # where codex adds a stray sentence before/after the object.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def _generate_with_codex(transcript, marker_hints, background_notes, meeting_date, category):
    model_id = (gateway_config.get_param(SERVICE_ID, "model_id", "") or "").strip()
    timeout_seconds = int(gateway_config.get_param(
        SERVICE_ID, "codex_timeout_seconds", DEFAULT_CODEX_TIMEOUT_SECONDS
    ))
    instructions = INSTRUCTIONS_BY_CATEGORY.get(category, INSTRUCTIONS_BY_CATEGORY["meeting"])

    cmd = [
        "codex", "exec",
        "--ignore-user-config",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
    ]
    if model_id:
        cmd += ["-m", model_id]

    stdin_text = (
        _build_background_section(background_notes)
        + _build_meeting_date_section(meeting_date)
        + _build_marker_section(marker_hints)
        + "Transcript:\n" + transcript
    )

    output_fd, output_path = tempfile.mkstemp(suffix=".txt")
    os.close(output_fd)
    try:
        result = subprocess.run(
            cmd + ["-o", output_path, instructions],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            raise ServiceError(
                f"note generation failed (codex exit {result.returncode}): {result.stderr[-2000:]}", 502
            )
        with open(output_path, "r") as f:
            raw = f.read().strip()
        if not raw:
            raise ServiceError("note generation produced an empty result", 502)
        try:
            return _extract_json(raw)
        except (json.JSONDecodeError, ValueError):
            raise ServiceError("note generation did not return valid JSON", 502)
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def handle(params):
    transcript = (params.get("transcript") or "").strip()
    if not transcript:
        raise ServiceError("missing 'transcript'", 400)

    marker_hints = params.get("marker_hints") or []
    if not isinstance(marker_hints, list):
        raise ServiceError("'marker_hints' must be an array", 400)

    background_notes = (params.get("background_notes") or "").strip()
    meeting_date = (params.get("meeting_date") or "").strip()
    category = (params.get("category") or "meeting").strip()

    return _generate_with_codex(transcript, marker_hints, background_notes, meeting_date, category)
