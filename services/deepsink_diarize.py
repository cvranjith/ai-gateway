"""service_id: "deepsink_diarize"

params:
    chunks (list, required) - [{"audio_base64": "...", "start_offset_seconds": <float>}, ...],
             one entry per recorded audio chunk for the session, in any
             order (sorted here by start_offset_seconds before use).
    format (str, optional, default "m4a") - ffmpeg-readable container/codec
             of each chunk's audio.

result:
    {"segments": [{"start": <float>, "end": <float>, "speaker": "SPEAKER_00"}, ...]}
    Raw diarization output, in session-absolute seconds - NOT merged with
    transcript text. DeepSink already has its own accurately-timestamped
    transcript from Whisper; this only answers "who was probably talking
    when," and the app aligns that against its own transcript blocks by
    time overlap. Keeping this service's output separate from the
    transcript avoids any risk of it subtly duplicating or drifting from
    text that's already correct.

Diarizes a whole session's audio in one pass (not per-chunk) via
pyannote.audio - a session-relative speaker label like "SPEAKER_00" only
means anything within the one diarization run that produced it, so
per-chunk diarization would give inconsistent numbering across a session
(chunk 1's "SPEAKER_00" isn't necessarily chunk 4's). Meant to be
triggered on demand against a finished session, not automatically - a
full meeting is real CPU time even in the isolated venv described below.

Requires a HuggingFace access token (own account, free) with the
`pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0` model
licenses accepted at huggingface.co - see this repo's README,
"deepsink_diarize setup", for exact steps. Read from hf_token.txt
(gitignored) at the repo root; a missing/empty file raises a clean
ServiceError rather than crashing.

## Why a separate venv + subprocess, not an in-process import

pyannote.audio's own dependency requirements (torch>=2.8.0,
numpy>=2.2.2) are incompatible with the shared environment's pinned
versions - specifically, openai-whisper's own `numba` dependency
requires numpy<=2.1.x. Confirmed the hard way: installing pyannote.audio
into the shared environment broke deepsink_transcribe outright until
reverted. `.venv-diarize` (gitignored; create once with
`python3.10 -m venv .venv-diarize && .venv-diarize/bin/pip install -r
requirements-diarize.txt`) keeps pyannote's dependency tree fully
separate. This module shells out to `diarize_worker.py` running under
that venv's own interpreter - same "external process, not an imported
library" shape services/deepsink_notes.py already uses for Codex, just
for a dependency-conflict reason here rather than a language one.
Because of this, pyannote.audio deliberately does NOT appear in this
repo's main requirements.txt - see requirements-diarize.txt instead.

## Why audio is concatenated, not diarized per chunk, and why each chunk
   is re-encoded to WAV first rather than stream-copying them together

Chunks are separately-recorded AAC-in-M4A files; naively stream-copy-
concatenating compressed MP4-family containers is fragile (that
container format isn't really designed for it, unlike e.g. MPEG-TS).
Each chunk is decoded to its own WAV first (cheap, and it's what
pyannote wants as input anyway), then the WAV files are concatenated via
ffmpeg's concat demuxer - safe once every input is identical,
uncompressed PCM. A session interrupted mid-recording (see
AudioRecorder) can leave a real time gap between two chunks that
concatenation itself removes, so this also tracks, per chunk, the
offset needed to map a position in the concatenated file back to real
session-absolute time - see `_offset_ranges`/`_delta_for`.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "deepsink_diarize"
HF_TOKEN_PATH = Path(__file__).resolve().parent.parent / "hf_token.txt"
VENV_PYTHON = Path(__file__).resolve().parent.parent / ".venv-diarize" / "bin" / "python"
WORKER_SCRIPT = Path(__file__).resolve().parent.parent / "diarize_worker.py"
# A 2-hour meeting can genuinely take a long time to diarize on CPU -
# generous by design; a stuck subprocess still can't hang the gateway
# forever (see gateway.py's threaded=True), just this one request.
DEFAULT_TIMEOUT_SECONDS = 1800


def _read_hf_token():
    if not HF_TOKEN_PATH.exists():
        return None
    token = HF_TOKEN_PATH.read_text().strip()
    return token or None


def _parse_chunks(chunks):
    if not isinstance(chunks, list) or not chunks:
        raise ServiceError("missing or empty 'chunks'", 400)
    parsed = []
    for i, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise ServiceError(f"chunk {i} must be an object", 400)
        audio_b64 = (chunk.get("audio_base64") or "").strip()
        if not audio_b64:
            raise ServiceError(f"chunk {i} missing 'audio_base64'", 400)
        try:
            start_offset = float(chunk.get("start_offset_seconds") or 0)
        except (TypeError, ValueError):
            raise ServiceError(f"chunk {i} 'start_offset_seconds' must be a number", 400)
        try:
            audio_bytes = base64.b64decode(audio_b64, validate=True)
        except Exception:
            raise ServiceError(f"chunk {i} 'audio_base64' is not valid base64", 400)
        if not audio_bytes:
            raise ServiceError(f"chunk {i} decoded audio was empty", 400)
        parsed.append({"start_offset": start_offset, "bytes": audio_bytes})
    parsed.sort(key=lambda c: c["start_offset"])
    return parsed


def _probe_duration_seconds(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise ServiceError("could not determine an audio chunk's duration", 502)


def _convert_to_wav(src_path, dst_path):
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src_path), "-ar", "16000", "-ac", "1", str(dst_path)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise ServiceError(f"audio conversion failed: {result.stderr[-500:]}", 502)


def _concatenate_wavs(wav_paths, concat_list_path, output_path):
    with open(concat_list_path, "w") as f:
        for path in wav_paths:
            f.write(f"file '{path}'\n")
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list_path),
         "-c", "copy", str(output_path)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise ServiceError(f"audio concatenation failed: {result.stderr[-500:]}", 502)


def _offset_ranges(parsed_chunks):
    cursor = 0.0
    ranges = []
    for chunk in parsed_chunks:
        duration = chunk["duration"]
        ranges.append({
            "concat_start": cursor,
            "concat_end": cursor + duration,
            "delta": chunk["start_offset"] - cursor,
        })
        cursor += duration
    return ranges


def _delta_for(concat_time, ranges):
    for r in ranges:
        if r["concat_start"] <= concat_time < r["concat_end"]:
            return r["delta"]
    # Past the last range's end - a floating-point edge right at the tail
    # of the last chunk, not a real out-of-bounds segment. Use the last
    # chunk's delta rather than dropping the segment.
    return ranges[-1]["delta"] if ranges else 0.0


def _run_worker(wav_path, token):
    timeout_seconds = int(gateway_config.get_param(SERVICE_ID, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    env = dict(os.environ)
    env["HF_TOKEN"] = token
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), str(WORKER_SCRIPT), str(wav_path)],
            capture_output=True, text=True, timeout=timeout_seconds, env=env,
        )
    except subprocess.TimeoutExpired:
        raise ServiceError(
            f"diarization timed out after {timeout_seconds}s - a very long meeting "
            "may need a larger deepsink_diarize.timeout_seconds", 504
        )
    if result.returncode != 0:
        raise ServiceError(f"diarization failed: {result.stderr.strip()[-2000:]}", 502)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ServiceError("diarization worker returned malformed output", 502)


def handle(params):
    parsed_chunks = _parse_chunks(params.get("chunks"))
    fmt = (params.get("format") or "m4a").strip().lstrip(".") or "m4a"

    token = _read_hf_token()
    if not token:
        raise ServiceError(
            "Diarization isn't configured yet - add a HuggingFace access token to "
            "hf_token.txt (see this repo's README, deepsink_diarize section)",
            503,
        )
    if not VENV_PYTHON.exists():
        raise ServiceError(
            "Diarization isn't set up yet - create .venv-diarize and install "
            "requirements-diarize.txt into it (see this repo's README)",
            503,
        )

    work_dir = Path(tempfile.mkdtemp(prefix="deepsink-diarize-"))
    try:
        wav_paths = []
        for i, chunk in enumerate(parsed_chunks):
            src_path = work_dir / f"chunk_{i}.{fmt}"
            src_path.write_bytes(chunk["bytes"])
            wav_path = work_dir / f"chunk_{i}.wav"
            _convert_to_wav(src_path, wav_path)
            chunk["duration"] = _probe_duration_seconds(wav_path)
            wav_paths.append(wav_path)

        ranges = _offset_ranges(parsed_chunks)

        concat_list_path = work_dir / "concat_list.txt"
        concatenated_path = work_dir / "concatenated.wav"
        _concatenate_wavs(wav_paths, concat_list_path, concatenated_path)

        worker_result = _run_worker(concatenated_path, token)
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError(f"diarization failed: {e}", 502)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    segments = []
    for segment in worker_result.get("segments", []):
        delta = _delta_for(segment["start"], ranges)
        segments.append({
            "start": round(segment["start"] + delta, 2),
            "end": round(segment["end"] + delta, 2),
            "speaker": segment["speaker"],
        })
    return {"segments": segments}
