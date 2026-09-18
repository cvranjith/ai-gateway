"""service_id: "deepsink_articulate"

params:
    transcript       (str, required) - a short, recent excerpt of the
                     session's transcript (on-device recognized on the
                     phone, not the accurate Whisper one - see DeepSink's
                     LiveAssistEngine), typically the last few minutes,
                     not the whole meeting.
    background_notes (str, optional) - free-text context the user typed
                     about the session (who's in the room, the agenda,
                     acronyms/jargon, prior history) - the same field
                     deepsink_notes takes, reused here so an answer given
                     mid-meeting benefits from it too.

result:
    {"bullets": ["...", ...], "speech": "..."}

Meant to be tapped mid-meeting and waited on, so this is deliberately
fast rather than thorough: a much shorter Codex timeout than
deepsink_notes, and a much shorter input. "bullets" is a quick-reference
list; "speech" is the same content phrased as something to actually read
out loud - first person, conversational, not a summary.

Same subprocess pattern as youtube_summarizer/deepsink_notes -
instructions as the CLI arg, the transcript excerpt piped via stdin.
"""

import json
import os
import re
import subprocess
import tempfile

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "deepsink_articulate"
DEFAULT_CODEX_TIMEOUT_SECONDS = 45

INSTRUCTIONS = """You are helping someone who has just been asked a question in a meeting, or has re-tuned into a conversation after not fully following it, using only the transcript excerpt pasted below (the last few minutes, not necessarily the whole meeting - there may be no clear question in it at all).

Output ONLY a single JSON object, no markdown code fences, no commentary, with exactly these keys:
- "bullets": array of 2-5 short strings - the quickest possible reference to what's just been discussed and, if there's an apparent question, the key points of a reasonable answer or opinion
- "speech": a short (2-4 sentence) first-person, conversational response phrased as something to actually say out loud right now - not a summary, an answer, in a natural spoken style

If the excerpt doesn't contain a clear question, treat it as "catch me up" instead: bullets covering what's just been said, and speech as a short spoken recap. If background notes are provided, use them to interpret the excerpt correctly (names, acronyms, context) - they are not part of the conversation itself."""


def _build_background_section(background_notes):
    if not background_notes:
        return ""
    return f"Background provided by the user (not part of the conversation itself):\n{background_notes}\n\n"


def _extract_json(raw_text):
    text = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def _generate_with_codex(transcript, background_notes):
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

    stdin_text = _build_background_section(background_notes) + f"Transcript excerpt:\n{transcript}"

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
                f"articulate failed (codex exit {result.returncode}): {result.stderr[-2000:]}", 502
            )
        with open(output_path, "r") as f:
            raw = f.read().strip()
        if not raw:
            raise ServiceError("articulate produced an empty result", 502)
        try:
            return _extract_json(raw)
        except (json.JSONDecodeError, ValueError):
            raise ServiceError("articulate did not return valid JSON", 502)
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def handle(params):
    transcript = (params.get("transcript") or "").strip()
    if not transcript:
        raise ServiceError("missing 'transcript'", 400)
    background_notes = (params.get("background_notes") or "").strip()
    return _generate_with_codex(transcript, background_notes)
