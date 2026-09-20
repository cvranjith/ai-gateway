"""service_id: "deepsink_prepare"

params:
    background_notes (str, optional) - free-text context already on the
                     session (attendees, agenda, whatever's been typed
                     so far) - same field deepsink_notes/deepsink_chat
                     take.
    history          (list, optional) - prior turns in this prep
                     conversation, oldest first:
                     [{"role": "user"|"assistant", "content": "..."}, ...]
    message          (str, optional) - the user's newest message. Empty
                     (with empty history too) kicks off a fresh
                     conversation - the model opens with a clarifying
                     question instead of waiting for one.

result:
    {"reply": "..."}

A collaborative planning assistant for an upcoming meeting or
presentation - genuinely different from deepsink_chat (which answers
one-shot questions about a transcript that already exists). This runs
*before* any recording happens: helps figure out what's actually being
presented and to whom, helps structure it into bullets/an outline, and
can draft an intro or per-section spoken narration once there's enough
established to work with. Multi-turn by design (the whole point is a
back-and-forth, not a single answer) - `history` is the session's own
persisted `prep_chat` (see deepsink_sessions.py's prepare_chat route),
not scoped/windowed the way deepsink_articulate's live excerpt is,
since a prep conversation is expected to stay a reasonable length, not
run for hours the way a live meeting transcript does.

Same subprocess-Codex pattern as deepsink_notes/deepsink_chat; no
separate LLM integration needed.
"""

import json
import os
import re
import subprocess
import tempfile

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "deepsink_prepare"
DEFAULT_CODEX_TIMEOUT_SECONDS = 60

INSTRUCTIONS = """You are a collaborative meeting/presentation prep assistant. The user is planning something they'll present or discuss in an upcoming meeting or presentation - your job is to help them figure out what to say and how to structure it, through a back-and-forth conversation, not a one-shot answer.

Guidelines:
- If there's no conversation so far and no new message from the user, this is the very start - open with 1-2 clarifying questions: what are they presenting, to whom, and what outcome/goal they want from it. Don't assume.
- Otherwise, respond naturally to their latest message. Ask a follow-up question when something important is still unclear (audience, time available, the one thing they most need the audience to take away); once there's enough established, help turn it into concrete structure - an outline or bullet list - rather than staying abstract.
- If asked to draft something (an intro, a spoken narration for a section/slide), write it in a natural, spoken style meant to be said out loud, not read as a document - first person, conversational.
- Keep responses conversational and reasonably concise - this is a back-and-forth, not an essay.

Output ONLY a single JSON object, no markdown code fences, no commentary, with exactly this key:
- "reply": your next message in the conversation, as plain text (not JSON, not markdown headers - just what you'd actually say next)"""


def _build_background_section(background_notes):
    if not background_notes:
        return ""
    return f"Background provided by the user (not part of the conversation itself):\n{background_notes}\n\n"


def _build_history_section(history):
    if not history:
        return ""
    lines = ["Conversation so far:"]
    for turn in history:
        role = "User" if turn.get("role") == "user" else "Assistant"
        content = (turn.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) + "\n\n"


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


def _generate_with_codex(background_notes, history, message):
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

    new_message_section = f"New message from the user: {message}" if message else (
        "(No new message - this is the very start of the conversation. Open with a clarifying question.)"
    )
    stdin_text = f"{_build_background_section(background_notes)}{_build_history_section(history)}{new_message_section}"

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
                f"prepare failed (codex exit {result.returncode}): {result.stderr[-2000:]}", 502
            )
        with open(output_path, "r") as f:
            raw = f.read().strip()
        if not raw:
            raise ServiceError("prepare produced an empty result", 502)
        try:
            return _extract_json(raw)
        except (json.JSONDecodeError, ValueError):
            raise ServiceError("prepare did not return valid JSON", 502)
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def handle(params):
    background_notes = (params.get("background_notes") or "").strip()
    history = params.get("history") or []
    message = (params.get("message") or "").strip()
    if not isinstance(history, list):
        raise ServiceError("'history' must be an array", 400)
    return _generate_with_codex(background_notes, history, message)
