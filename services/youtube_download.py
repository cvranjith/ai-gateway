"""service_id: "youtube_download"

params:
    video_id (str, required)
    kind     (str, optional, default "video") - "video" or "audio"

result:
    {
        "video_id": "...",
        "kind": "video" | "audio",
        "title": "...",
        "ext": "mp4" | "m4a" | "webm" | ...,
        "url": "https://...",       # direct, ready-to-download URL
        "filesize": 12345 | null,   # best-effort, yt-dlp doesn't always know it upfront
    }

Resolves a direct, already-decrypted download URL via yt-dlp rather
than proxying any video bytes through this server. The phone downloads
straight from YouTube's own CDN using that URL - which is what gives a
normal native progress bar for free (an ordinary URLSession download
task against a normal HTTP URL) with no custom streaming protocol
needed here, and starts "immediately" in the sense that there's no
server-side download/transcode step in between at all.

This replaces the app's old native (Swift-side) approach, which parsed
`ytInitialPlayerResponse` in the WebView and only worked for videos
whose formats had a plain `url` (no `signatureCipher`) - yt-dlp handles
designation/decryption properly, so this should work for effectively
any video, not just the subset the old approach could reach.

For "video", only a *progressive* format is used - one URL with
video+audio already combined, so the phone just downloads one file
with no server-side muxing required. Capped at a configurable quality
(youtube_download.max_video_height, default 1080) since progressive
formats top out well below the highest adaptive-only qualities anyway.
If a video genuinely has no progressive format at all (happens
sometimes - not every video offers one), this raises ServiceError:
merging separate video+audio streams server-side would need its own
streaming HTTP route rather than fitting this JSON-in/JSON-out
contract, and hasn't been built - not expected to come up often, but
worth revisiting if it does.
"""

import yt_dlp

import config as gateway_config
from .errors import ServiceError

SERVICE_ID = "youtube_download"
DEFAULT_MAX_VIDEO_HEIGHT = 1080


def _extract_info(video_id, kind):
    url = f"https://www.youtube.com/watch?v={video_id}"
    # Which client context yt-dlp asks YouTube's internal API as
    # matters a lot for what shows up in `formats` - confirmed by hand
    # against a real video. The default (web-ish) context had plenty of
    # audio-only and video-only adaptive formats but zero progressive
    # (video+audio combined) ones; the ANDROID context had exactly one
    # progressive format and nothing else (no separate audio-only
    # formats at all). Passing both client names to the same request
    # did NOT merge their format lists the way you'd hope - so instead,
    # ask for whichever single context actually has what this `kind`
    # needs: ANDROID for a progressive video+audio format, the default
    # context for an audio-only adaptive stream. (Same ANDROID-context
    # finding as this project's own transcript-fetching code - see the
    # yt-run app's DownloadManager.swift.)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if kind == "video":
        opts["extractor_args"] = {"youtube": {"player_client": ["android"]}}
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
    return max(candidates, key=lambda f: f.get("height") or 0)


def _best_audio(info):
    candidates = [
        f for f in info.get("formats", [])
        if f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
        and f.get("url")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.get("abr") or 0)


def handle(params):
    video_id = (params.get("video_id") or "").strip()
    kind = (params.get("kind") or "video").strip().lower()

    if not video_id:
        raise ServiceError("missing 'video_id'", 400)
    if kind not in ("video", "audio"):
        raise ServiceError("invalid 'kind' - must be 'video' or 'audio'", 400)

    info = _extract_info(video_id, kind)

    if kind == "video":
        max_height = int(gateway_config.get_param(SERVICE_ID, "max_video_height", DEFAULT_MAX_VIDEO_HEIGHT))
        fmt = _best_progressive_video(info, max_height)
    else:
        fmt = _best_audio(info)

    if fmt is None:
        raise ServiceError(
            f"no directly downloadable {kind} stream found for this video "
            "(it may not offer a single combined progressive format)",
            404,
        )

    return {
        "video_id": video_id,
        "kind": kind,
        "title": info.get("title") or "Video",
        "ext": fmt.get("ext") or ("mp4" if kind == "video" else "m4a"),
        "url": fmt["url"],
        "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
    }
