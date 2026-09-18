"""service_id: "deepsink_transcribe"

params:
    audio_base64          (str, required) - base64-encoded audio chunk
    format                (str, optional, default "m4a") - ffmpeg-readable
                           container/codec name (whisper shells out to
                           ffmpeg to decode, so anything ffmpeg reads works)
    chunk_index            (int, optional) - passed through for logging only
    start_offset_seconds   (float, optional, default 0) - added to every
                            segment's start/end before returning, so blocks
                            come back already expressed in session-absolute
                            seconds - matches what DeepSink's RouterClient
                            expects, no client-side offset math needed.

result:
    {"blocks": [{"start": <float>, "end": <float>, "text": "..."}, ...]}

Runs OpenAI Whisper locally (openai-whisper, already installed in this
gateway's python@3.10 environment) against the decoded chunk - no audio
ever leaves this Mac. CPU by default (no CUDA on a Mac mini, and whisper's
MPS support is inconsistent enough across ops to not risk as the default);
configurable via deepsink_transcribe.device if you want to try "mps".

The model is loaded once per process and cached in _MODELS, not reloaded
per request - loading is the slow part (several seconds), and this gateway
is long-running (see local-llm.sh), so there's no reason to pay that cost
on every chunk.
"""

import base64
import os
import tempfile

import whisper

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "deepsink_transcribe"
# ".en" (English-only) rather than the multilingual variant - faster and
# more accurate for a known-English-meetings use case, and skips language
# auto-detection entirely. Override via deepsink_transcribe.model_id if
# your meetings aren't English.
DEFAULT_MODEL_ID = "small.en"

_MODELS = {}


def _load_model(model_id, device):
    key = (model_id, device)
    if key not in _MODELS:
        _MODELS[key] = whisper.load_model(model_id, device=device)
    return _MODELS[key]


def handle(params):
    audio_b64 = (params.get("audio_base64") or "").strip()
    if not audio_b64:
        raise ServiceError("missing 'audio_base64'", 400)

    fmt = (params.get("format") or "m4a").strip().lstrip(".") or "m4a"

    try:
        start_offset = float(params.get("start_offset_seconds") or 0)
    except (TypeError, ValueError):
        raise ServiceError("'start_offset_seconds' must be a number", 400)

    try:
        audio_bytes = base64.b64decode(audio_b64, validate=True)
    except Exception:
        raise ServiceError("'audio_base64' is not valid base64", 400)
    if not audio_bytes:
        raise ServiceError("decoded audio was empty", 400)

    model_id = (gateway_config.get_param(SERVICE_ID, "model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID).strip()
    device = (gateway_config.get_param(SERVICE_ID, "device", "") or "").strip() or None

    fd, path = tempfile.mkstemp(suffix=f".{fmt}")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(audio_bytes)

        model = _load_model(model_id, device)
        # fp16=False: this runs on CPU by default (no CUDA here); asking
        # for fp32 up front skips whisper's noisy "FP16 is not supported
        # on CPU" warning rather than triggering then ignoring it.
        result = model.transcribe(path, fp16=False)
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError(f"transcription failed: {e}", 502)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    blocks = []
    for segment in result.get("segments", []):
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        blocks.append({
            "start": round(segment["start"] + start_offset, 2),
            "end": round(segment["end"] + start_offset, 2),
            "text": text,
        })

    # An empty result is valid, not an error - e.g. a chunk that's mostly
    # silence before anyone starts talking. The client just sees "no
    # transcript for this stretch" rather than a failure.
    return {"blocks": blocks}
