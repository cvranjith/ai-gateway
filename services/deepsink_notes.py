"""service_id: "deepsink_notes"

params:
    transcript     (str, required) - full session transcript text
    marker_hints   (list, optional) - [{"offset_seconds": <float>, "comment": "<str>"}, ...]
                   moments the user tapped "mark this moment" on while
                   recording - passed to the model as a hint about what
                   mattered, per requirement-deepsink-mobile.md FR-4.

result: the deepsink.notes JSON shape from requirement-deepsink-mobile.md
FR-4, returned as a real JSON object (not a string) - DeepSink decodes
this straight into SessionNotesPayload:
    {
      "title": "...", "summary": "...",
      "key_points": ["..."], "decisions": ["..."],
      "action_items": [{"text": "...", "owner": "me|<name>|unknown", "due": "<date>|null"}],
      "open_questions": ["..."]
    }

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

INSTRUCTIONS = """You are producing structured meeting notes from a raw speech-to-text transcript pasted below. The transcript may contain transcription errors, filler words, and missing punctuation - do your best to infer intent rather than transcribing literally.

Output ONLY a single JSON object, no markdown code fences, no commentary before or after it, with exactly these keys:
- "title": a short (under 8 words) descriptive title for the meeting
- "summary": a 2-4 sentence summary of what the meeting covered
- "key_points": array of strings, the main points discussed
- "decisions": array of strings, decisions that were made (empty array if none)
- "action_items": array of objects {"text": str, "owner": "me" | "<name>" | "unknown", "due": "<date>" | null}
- "open_questions": array of strings, questions raised but not resolved (empty array if none)

If the transcript doesn't clearly support a field, use an empty array (or empty string for "summary") rather than inventing content. If the user marked specific moments as important (see below, if present), weight those moments more heavily when deciding what counts as a key point, decision, or action item."""


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


def _generate_with_codex(transcript, marker_hints):
    model_id = (gateway_config.get_param(SERVICE_ID, "model_id", "") or "").strip()
    timeout_seconds = int(gateway_config.get_param(
        SERVICE_ID, "codex_timeout_seconds", DEFAULT_CODEX_TIMEOUT_SECONDS
    ))

    cmd = [
        "codex", "exec",
        "--ignore-user-config",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
    ]
    if model_id:
        cmd += ["-m", model_id]

    stdin_text = _build_marker_section(marker_hints) + "Transcript:\n" + transcript

    output_fd, output_path = tempfile.mkstemp(suffix=".txt")
    os.close(output_fd)
    try:
        result = subprocess.run(
            cmd + ["-o", output_path, INSTRUCTIONS],
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

    return _generate_with_codex(transcript, marker_hints)
