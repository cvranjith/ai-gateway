"""service_id: "youtube_download"

params:
    video_id (str, required)
    kind     (str, optional, default "video") - "video" or "audio"

result:
    {
        "video_id": "...",
        "kind": "video" | "audio",
        "title": "...",
        "ext": "mp4" | "m4a",
        "url": "https://..." | "/files/<token>.m4a",
        "filesize": 12345 | null,
    }

For "video", `url` is a direct, already-decrypted YouTube CDN URL -
yt-dlp resolves it, but no video bytes pass through this server; the
phone downloads straight from YouTube using that URL, which is what
gives a normal native progress bar for free (an ordinary URLSession
download task against a normal HTTP URL).

For "audio", `url` is instead a *relative* path served by this same
gateway (see gateway.py's `/files/<name>` route) - the caller (ai-router,
or the app talking to this gateway directly) must resolve it against
whatever base URL it already used to reach `/invoke`. This is a
deliberate exception to "no bytes pass through this server", explained
below.

Both kinds are resolved via yt-dlp, replacing the app's old native
(Swift-side) approach, which parsed `ytInitialPlayerResponse` in the
WebView and only worked for videos whose formats had a plain `url` (no
`signatureCipher`).

## Why "audio" isn't just a direct adaptive-audio CDN URL

It used to be - `_best_audio()` below picked whichever audio-only
adaptive format yt-dlp found via YouTube's default (web-ish) API
context, same shape as the video path. In practice this made audio
downloads dramatically *slower* than video, and it isn't a transcoding
cost - confirmed by hand: every audio-only format returned by every
yt-dlp client context this project could reach (default, android,
android_vr, android_creator - ios/web/mweb/tv/web_creator currently
fail outright, likely needing PO tokens) lacks the `ratebypass=yes`
marker that YouTube's CDN URLs carry when *not* throttled. The
`android`/`android_vr` contexts *do* get an unthrottled URL, but only
for the progressive (video+audio combined) format - never audio-only.
So there is currently no way to ask YouTube for an audio-only stream
that isn't deliberately rate-limited server-side.

Instead, "audio" reuses the same unthrottled progressive format
"video" uses, and has ffmpeg (already installed on this Mac, though
previously unused by this gateway) strip out just the audio track - a
stream copy (`-acodec copy`), not a re-encode, so it's fast regardless
of video length: ffmpeg reads over HTTP directly from the progressive
URL as it demuxes and writes only the small resulting audio file to
disk, so this server never holds a full temp copy of the video. The
result is saved under a random, short-lived filename in FILES_DIR and
served once by gateway.py's `/files/<name>` route; the caller gets a
relative path back and fetches the small audio file itself, which is
what still gives a real (and now fast) client-side progress bar rather
than a percentage tracked here.

For "video", only a *progressive* format is used - one URL with
video+audio already combined, so the phone just downloads one file
with no server-side muxing required. Capped at a configurable quality
(youtube_download.max_video_height, default 1080) since progressive
formats top out well below the highest adaptive-only qualities anyway.
If a video genuinely has no progressive format at all (happens
sometimes - not every video offers one), this raises ServiceError for
both kinds, since audio extraction also depends on that same format.
"""

import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import yt_dlp

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "youtube_download"
DEFAULT_MAX_VIDEO_HEIGHT = 1080

# Where extracted audio files wait to be picked up by the phone - see
# `_extract_audio()` below and gateway.py's `/files/<name>` route.
# Swept for anything older than FILE_TTL_SECONDS on every audio
# request, so this never needs a cron job or background thread of its
# own - a leftover from a request whose result was never actually
# fetched just waits for the next audio request to come along and
# clean it up.
FILES_DIR = Path(tempfile.gettempdir()) / "ai-gateway-files"
FILE_TTL_SECONDS = 30 * 60


def _cleanup_old_files():
    FILES_DIR.mkdir(exist_ok=True)
    cutoff = time.time() - FILE_TTL_SECONDS
    for path in FILES_DIR.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _extract_info(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    # The ANDROID client context is the one confirmed (by hand, against
    # a real video) to return an unthrottled (`ratebypass=yes`)
    # progressive format - see the module docstring for the full
    # investigation. Always used now, since both "video" and "audio"
    # need this same format.
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ServiceError(f"couldn't resolve this video: {e}", 502)


def _best_progressive_video(info, max_height):
    candidates = [
        f for f in info.get("formats", [])
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and f.get("url")
        and (f.get("height") or 0) <= max_height
    ]
    if not candidates:
        return None
    # Prefer mp4 (H.264/AAC) over any other container/codec, even if a
    # higher-resolution non-mp4 progressive format exists - confirmed in
    # practice that a resolved WebM/VP9 progressive format is a
    # perfectly valid file (plays fine in other apps) but shows a black
    # screen in an AVPlayer-based preview: AVFoundation doesn't support
    # the WebM container at all, regardless of the codec inside it. A
    # lower-resolution mp4 the client can actually play beats a
    # higher-resolution one it can't.
    mp4_candidates = [f for f in candidates if f.get("ext") == "mp4"]
    pool = mp4_candidates or candidates
    return max(pool, key=lambda f: f.get("height") or 0)


def _extract_audio(source_url):
    """Runs ffmpeg directly against the progressive stream URL, keeping
    only its audio track. 10-minute subprocess timeout as a backstop
    for a genuinely stuck transfer - a stream copy of even a long video
    should finish well within that on any normal connection.
    """
    _cleanup_old_files()
    token = uuid.uuid4().hex
    out_path = FILES_DIR / f"{token}.m4a"
    cmd = ["ffmpeg", "-y", "-i", source_url, "-vn", "-acodec", "copy", str(out_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise ServiceError("audio extraction timed out", 504)
    if result.returncode != 0 or not out_path.exists():
        stderr_tail = result.stderr.decode(errors="replace")[-500:]
        raise ServiceError(f"audio extraction failed: {stderr_tail}", 502)
    return token, out_path.stat().st_size


def handle(params):
    video_id = (params.get("video_id") or "").strip()
    kind = (params.get("kind") or "video").strip().lower()

    if not video_id:
        raise ServiceError("missing 'video_id'", 400)
    if kind not in ("video", "audio"):
        raise ServiceError("invalid 'kind' - must be 'video' or 'audio'", 400)

    info = _extract_info(video_id)
    max_height = int(gateway_config.get_param(SERVICE_ID, "max_video_height", DEFAULT_MAX_VIDEO_HEIGHT))
    fmt = _best_progressive_video(info, max_height)
    if fmt is None:
        raise ServiceError(
            f"no directly downloadable {kind} stream found for this video "
            "(it may not offer a single combined progressive format)",
            404,
        )
    title = info.get("title") or "Video"

    if kind == "video":
        return {
            "video_id": video_id,
            "kind": "video",
            "title": title,
            "ext": fmt.get("ext") or "mp4",
            "url": fmt["url"],
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
        }

    token, size = _extract_audio(fmt["url"])
    return {
        "video_id": video_id,
        "kind": "audio",
        "title": title,
        "ext": "m4a",
        "url": f"/files/{token}.m4a",
        "filesize": size,
    }
