"""service_id: "deepsink_chat"

params:
    transcript       (str, required) - the session's transcript so far
                     (accurate, Whisper-transcribed text - unlike
                     deepsink_articulate's short on-device excerpt, this
                     is meant to be the whole thing available to date).
    notes_summary    (str, optional) - the generated summary, if any
                     (deepsink_notes' output) - cheap extra context, the
                     model doesn't have to re-derive "what's this about"
                     from the raw transcript alone.
    background_notes (str, optional) - same free-text field
                     deepsink_notes/deepsink_articulate take.
    question         (str, required) - what the user is asking.

result:
    {"answer": "..."}

The web session viewer's "chat" feature: ask a question about a
specific session's content and get a direct answer, grounded only in
what was actually said (plus background_notes for interpretation) -
not a general-purpose assistant. Same subprocess-Codex pattern as
deepsink_notes/deepsink_articulate; no separate LLM integration needed.
"""

import json
import os
import re
import subprocess
import tempfile

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "deepsink_chat"
DEFAULT_CODEX_TIMEOUT_SECONDS = 60

INSTRUCTIONS = """You are answering a question about a specific recorded meeting, using only the transcript (and optional summary/background) pasted below - not general knowledge, not other meetings.

Output ONLY a single JSON object, no markdown code fences, no commentary, with exactly this key:
- "answer": a direct, concise answer (a few sentences, or a short list if that fits the question better) grounded in the transcript

If the transcript doesn't contain enough information to answer, say so plainly in "answer" rather than guessing or inventing detail. Background notes (if provided) are context for interpreting the transcript (names, acronyms, agenda) - not part of the conversation itself, and not something to answer questions about unless the question is directly about them."""


def _build_context(notes_summary, background_notes):
    parts = []
    if background_notes:
        parts.append(f"Background provided by the user (not part of the conversation itself):\n{background_notes}\n")
    if notes_summary:
        parts.append(f"Summary of the meeting so far:\n{notes_summary}\n")
    return "\n".join(parts)


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


def _generate_with_codex(transcript, notes_summary, background_notes, question):
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

    stdin_text = f"{_build_context(notes_summary, background_notes)}Transcript:\n{transcript}\n\nQuestion: {question}"

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
                f"chat failed (codex exit {result.returncode}): {result.stderr[-2000:]}", 502
            )
        with open(output_path, "r") as f:
            raw = f.read().strip()
        if not raw:
            raise ServiceError("chat produced an empty result", 502)
        try:
            return _extract_json(raw)
        except (json.JSONDecodeError, ValueError):
            raise ServiceError("chat did not return valid JSON", 502)
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def handle(params):
    transcript = (params.get("transcript") or "").strip()
    question = (params.get("question") or "").strip()
    if not transcript:
        raise ServiceError("missing 'transcript'", 400)
    if not question:
        raise ServiceError("missing 'question'", 400)
    notes_summary = (params.get("notes_summary") or "").strip()
    background_notes = (params.get("background_notes") or "").strip()
    return _generate_with_codex(transcript, notes_summary, background_notes, question)
