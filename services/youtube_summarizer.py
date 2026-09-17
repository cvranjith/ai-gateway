"""service_id: "youtube_summarizer"

params:
    video_id (str, required)
    length   (str, optional, default "paragraph") - one of LENGTH_PROMPTS

result:
    {"video_id": "...", "length": "...", "summary": "..."}

Fetches the video's transcript (youtube_transcript_api — same library
and approach as the original yt-cc.py test script) and summarizes it
via Codex CLI, non-interactively.

`--ignore-user-config` is required, not optional: this machine's
~/.codex/config.toml pins a model that 400s under this account's
ChatGPT-based auth. Ignoring it means codex falls back to its own
built-in default model unless config.properties (or the /ui web UI)
sets youtube_summarizer.model_id to something else.
"""

import os
import subprocess
import tempfile

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "youtube_summarizer"
DEFAULT_CODEX_TIMEOUT_SECONDS = 180

LENGTH_PROMPTS = {
    "short": (
        "Summarize the following YouTube video transcript in 2-3 concise sentences. "
        "Output only the summary itself, nothing else - no preamble, no headings, no quotes around it."
    ),
    "paragraph": (
        "Summarize the following YouTube video transcript in a single well-organized paragraph "
        "(roughly 4-6 sentences) covering the main points. "
        "Output only the summary itself, nothing else - no preamble, no headings, no quotes around it."
    ),
    "detailed": (
        "Write a detailed summary of the following YouTube video transcript, covering all the "
        "main points and key details across a few short paragraphs. "
        "Output only the summary itself, nothing else - no preamble, no headings, no quotes around it."
    ),
}


def _fetch_transcript_text(video_id):
    ytt = YouTubeTranscriptApi()
    transcript = ytt.fetch(video_id, languages=["en", "en-US"])
    return " ".join(snippet.text for snippet in transcript)


def _summarize_with_codex(transcript_text, length):
    prompt = LENGTH_PROMPTS[length]
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
            input=transcript_text,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            raise ServiceError(
                f"summarization failed (codex exit {result.returncode}): {result.stderr[-2000:]}", 502
            )
        with open(output_path, "r") as f:
            summary = f.read().strip()
        if not summary:
            raise ServiceError("summarization produced an empty result", 502)
        return summary
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def handle(params):
    video_id = (params.get("video_id") or "").strip()
    length = (params.get("length") or "paragraph").strip().lower()

    if not video_id:
        raise ServiceError("missing 'video_id'", 400)
    if length not in LENGTH_PROMPTS:
        raise ServiceError(f"invalid 'length' - must be one of {sorted(LENGTH_PROMPTS)}", 400)

    try:
        transcript_text = _fetch_transcript_text(video_id)
    except (TranscriptsDisabled, NoTranscriptFound):
        raise ServiceError("no captions available for this video", 404)
    except VideoUnavailable:
        raise ServiceError("video unavailable", 404)
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError(f"failed to fetch transcript: {e}", 502)

    if not transcript_text.strip():
        raise ServiceError("transcript was empty", 404)

    summary = _summarize_with_codex(transcript_text, length)
    return {"video_id": video_id, "length": length, "summary": summary}
