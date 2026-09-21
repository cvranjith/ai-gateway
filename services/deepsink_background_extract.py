"""service_id: "deepsink_background_extract"

params:
    background_notes (str, optional) - the free text already on the
                     session's Background tab.
    materials_text    (str, optional) - concatenated extracted text from
                     any uploaded PDF/text materials (see
                     deepsink_sessions.py's material upload route).
    prep_chat         (list, optional) - the session's own "ask me more
                     questions" planning conversation (deepsink_prepare's
                     persisted history, same shape) - folded in since an
                     answer given there ("it's for the leadership team")
                     often establishes exactly the field this is trying
                     to detect, even though it's not part of the raw
                     background_notes text itself.

result:
    {
      "summary": "...",
      "key_points": ["...", ...],
      "intent": "meeting" | "presentation" | "self_note" | "other" | null,
      "audience": "..." | null,
      "main_speaker": "..." | null
    }

Runs *before* any recording exists - this is a compact, structured read
of whatever prep material/notes/conversation exist so far, not a
meeting summary (that's deepsink_notes, generated from a real
transcript after recording). Triggered on demand ("Process" in the web
viewer's Background tab), not automatically on every keystroke, since
it's a real Codex call each time.

Same subprocess-Codex pattern as deepsink_notes/deepsink_chat/
deepsink_prepare; no separate LLM integration needed.
"""

import json
import os
import re
import subprocess
import tempfile

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "deepsink_background_extract"
DEFAULT_CODEX_TIMEOUT_SECONDS = 60

INSTRUCTIONS = """You are extracting a compact, structured summary from a user's preparatory notes for something they haven't presented or recorded yet - a meeting, a presentation, or just a personal note. Combine everything provided below (free-text notes, any uploaded material, any planning conversation) into one coherent picture.

Output ONLY a single JSON object, no markdown code fences, no commentary, with exactly these keys:
- "summary": a short (2-4 sentence) plain-text summary of what this is about
- "key_points": array of 3-6 short strings - the main points or topics
- "intent": one of "meeting", "presentation", "self_note", "other" - your best read of what kind of thing this is being prepared for, or null if genuinely unclear
- "audience": a short string naming who the audience/participants are, or null if that's not established anywhere in what's provided
- "main_speaker": a short string naming who the main speaker/presenter is, or null if not established

Only fill in "intent"/"audience"/"main_speaker" when there's real evidence for them somewhere in the provided text - use null rather than guessing, since this is shown as a small set of labeled fields, and a wrong guess there is worse than an absent one. Keep "summary" and "key_points" concise - this is a compact overview, not a full document."""


def _build_sections(background_notes, materials_text, prep_chat):
    parts = []
    if background_notes:
        parts.append(f"Free-text notes:\n{background_notes}\n")
    if materials_text:
        parts.append(f"Uploaded material (extracted text):\n{materials_text}\n")
    if prep_chat:
        lines = ["Planning conversation so far:"]
        for turn in prep_chat:
            role = "User" if turn.get("role") == "user" else "Assistant"
            content = (turn.get("content") or "").strip()
            if content:
                lines.append(f"{role}: {content}")
        parts.append("\n".join(lines) + "\n")
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


def _generate_with_codex(background_notes, materials_text, prep_chat):
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

    stdin_text = _build_sections(background_notes, materials_text, prep_chat)

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
                f"background_extract failed (codex exit {result.returncode}): {result.stderr[-2000:]}", 502
            )
        with open(output_path, "r") as f:
            raw = f.read().strip()
        if not raw:
            raise ServiceError("background_extract produced an empty result", 502)
        try:
            return _extract_json(raw)
        except (json.JSONDecodeError, ValueError):
            raise ServiceError("background_extract did not return valid JSON", 502)
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def handle(params):
    background_notes = (params.get("background_notes") or "").strip()
    materials_text = (params.get("materials_text") or "").strip()
    prep_chat = params.get("prep_chat") or []
    if not isinstance(prep_chat, list):
        raise ServiceError("'prep_chat' must be an array", 400)
    if not background_notes and not materials_text and not prep_chat:
        raise ServiceError(
            "nothing to process yet - add background notes, upload a file, or start a planning conversation first", 400
        )
    return _generate_with_codex(background_notes, materials_text, prep_chat)
