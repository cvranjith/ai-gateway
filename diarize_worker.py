#!/usr/bin/env python3
"""
Standalone diarization worker — run via this repo's own `.venv-diarize`
Python, never imported into the main gateway process.

pyannote.audio's own dependency requirements (torch>=2.8.0,
numpy>=2.2.2) are incompatible with the shared environment's pinned
versions (torch 2.6.0, numpy<=2.1.x — required by openai-whisper's own
`numba` dependency). Confirmed the hard way: installing pyannote.audio
into the shared environment broke deepsink_transcribe outright, working
transcription and all, until reverted. Isolated in its own venv and
invoked as a subprocess instead — same pattern services/deepsink_notes.py
already uses for Codex (an external process, not an imported library),
just applied here for a dependency-conflict reason rather than a
language one. See services/deepsink_diarize.py, the only thing that
calls this.

Usage:
    .venv-diarize/bin/python diarize_worker.py <wav_path>
    HF_TOKEN=<token> must be set in the environment (not passed as an
    argument, to keep it out of `ps`/process-list output).

stdout on success: {"segments": [{"start": <float>, "end": <float>, "speaker": "SPEAKER_00"}, ...]}
    (times are relative to <wav_path>'s own timeline — the caller maps
    them back to session-absolute time, since only it knows how the
    chunks it concatenated into that one file line up.)

Exit 0 + that JSON on stdout on success. Exit 1 + a plain-text message on
stderr on failure — same convention services/*.py's own subprocess calls
to external tools already use.
"""

import json
import os
import sys


def main():
    if len(sys.argv) != 2:
        print("usage: diarize_worker.py <wav_path>", file=sys.stderr)
        return 1

    wav_path = sys.argv[1]
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        print("HF_TOKEN environment variable is required", file=sys.stderr)
        return 1

    try:
        from pyannote.audio import Pipeline
    except ImportError as e:
        print(f"pyannote.audio is not installed in this venv: {e}", file=sys.stderr)
        return 1

    try:
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=token)
        diarization = pipeline(wav_path)
    except Exception as e:
        print(f"diarization failed: {e}", file=sys.stderr)
        return 1

    segments = [
        {"start": round(turn.start, 2), "end": round(turn.end, 2), "speaker": speaker}
        for turn, _, speaker in diarization.itertracks(yield_label=True)
    ]
    print(json.dumps({"segments": segments}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
