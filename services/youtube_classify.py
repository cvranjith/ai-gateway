"""service_id: "youtube_classify"

params:
    channel          (str, required)
    title            (str, optional)
    known_categories (list[str], optional) - vocabulary hint; the model
                      may reuse one of these or invent a short new one

result:
    {"category": "..."}

Classifies a YouTube channel/video into a single short topic word (e.g.
"AI", "Tech", "News", "Comedy") via Codex CLI — no transcript involved,
just the channel name and (if known) the video title, so this is a
much smaller/faster call than youtube_summarizer. Exists specifically
so per-channel category tagging in the YTRun app works against this
gateway (already paid for, no separate API key) rather than requiring
a direct OpenAI/Gemini/Claude key just for this one feature.
"""

import os
import subprocess
import tempfile

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "youtube_classify"
DEFAULT_CODEX_TIMEOUT_SECONDS = 30


def _classify_with_codex(channel, title, known_categories):
    vocabulary = ", ".join(known_categories) if known_categories else "AI, Tech, News, Comedy, Entertainment, Music, Other"
    prompt = (
        "You are tagging a YouTube channel/video with a single short topic category, "
        "for a personal watch-history report. "
        f"Known categories so far: {vocabulary}. "
        "Reply with just the category name - reuse one of the known ones if it fits, invent a "
        "short new one (1-2 words) if none fit well, or reply \"Other\" if you genuinely can't tell. "
        "No punctuation, no explanation.\n\n"
        f"Channel: {channel}\nTitle: {title or '(unknown)'}"
    )
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

    output_fd, output_path = tempfile.mkstemp(suffix=".txt")
    os.close(output_fd)
    try:
        result = subprocess.run(
            cmd + ["-o", output_path, prompt],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            raise ServiceError(
                f"classification failed (codex exit {result.returncode}): {result.stderr[-2000:]}", 502
            )
        with open(output_path, "r") as f:
            category = f.read().strip()
        if not category:
            raise ServiceError("classification produced an empty result", 502)
        # Codex sometimes wraps a short answer in a sentence despite the
        # prompt - a single line with no spaces is almost certainly just
        # the category word; anything longer, take the first line only
        # as a best-effort trim rather than failing outright.
        return category.splitlines()[0].strip().strip('."\'')
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def handle(params):
    channel = (params.get("channel") or "").strip()
    title = (params.get("title") or "").strip()
    known_categories = params.get("known_categories") or []

    if not channel:
        raise ServiceError("missing 'channel'", 400)
    if not isinstance(known_categories, list):
        raise ServiceError("'known_categories' must be a list of strings", 400)

    category = _classify_with_codex(channel, title, [str(c) for c in known_categories])
    return {"category": category}
