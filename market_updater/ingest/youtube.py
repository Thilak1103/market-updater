"""YouTube: read a video's words so it can be used like a podcast episode.

Order of attempts, cheapest first:
1. The video's captions (youtube-transcript-api). Free, but YouTube sometimes refuses cloud
   machines such as GitHub's.
2. The audio (yt-dlp), transcribed with Groq Whisper like a podcast. Uses the audio allowance.
3. Nothing worked: the caller leaves the Inbox row unprocessed, and pasting the transcript into
   the row's page (YouTube: "...more" under the video, then "Show transcript") still works.

Channels can also be sources: a channel URL in the Sources table is turned into its RSS feed
(youtube.com/feeds/videos.xml?channel_id=...), and each new video is read the same way.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..util import get_with_retry, log

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_AUDIO_MIN = 150


def is_youtube(url: str | None) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host in ("youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com")


def video_id(url: str | None) -> str | None:
    """The 11-character id from watch, youtu.be, shorts, live and embed links."""
    if not is_youtube(url):
        return None
    u = urlparse(url)
    if (u.hostname or "").lower() == "youtu.be":
        cand = u.path.strip("/").split("/")[0]
    elif u.path == "/watch":
        cand = (parse_qs(u.query).get("v") or [""])[0]
    else:
        parts = [p for p in u.path.split("/") if p]
        cand = parts[1] if len(parts) > 1 and parts[0] in ("shorts", "live", "embed", "v") else ""
    return cand if _ID.match(cand or "") else None


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def metadata(vid: str) -> dict:
    """Title and channel from YouTube's oEmbed endpoint (no API key needed)."""
    try:
        r = get_with_retry("https://www.youtube.com/oembed",
                           params={"url": watch_url(vid), "format": "json"}, tries=2)
        if r.status_code == 200:
            d = r.json()
            return {"title": d.get("title") or "", "channel": d.get("author_name") or ""}
    except Exception as e:
        log.debug("oEmbed failed for %s: %s", vid, e)
    return {"title": "", "channel": ""}


def captions(vid: str) -> str:
    """English captions as plain text, or "" if YouTube won't give them to us."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return ""
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(vid, languages=["en", "en-GB", "en-US"])
        text = " ".join(getattr(s, "text", "") for s in fetched)
    except Exception as e:
        log.info("No captions for %s (%s)", vid, type(e).__name__)
        return ""
    return re.sub(r"\s+", " ", text).strip()


def audio_transcript(vid: str) -> tuple[str, float]:
    """Download the audio with yt-dlp and transcribe it. Returns (text, seconds)."""
    from .transcribe import TranscriptionError, transcribe_file

    try:
        import yt_dlp
    except ImportError as e:
        raise TranscriptionError("yt-dlp is not installed") from e
    if not shutil.which("ffmpeg"):
        raise TranscriptionError("ffmpeg is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "format": "bestaudio[filesize<200M]/bestaudio/worst",
            "outtmpl": str(Path(tmp) / "audio.%(ext)s"),
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "match_filter": yt_dlp.utils.match_filter_func(f"duration < {MAX_AUDIO_MIN * 60}"),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([watch_url(vid)])
        except Exception as e:
            raise TranscriptionError(f"YouTube refused the audio download: {str(e)[:200]}") from e
        files = list(Path(tmp).glob("audio.*"))
        if not files:
            raise TranscriptionError("no audio downloaded (too long, or blocked)")
        return transcribe_file(files[0])


def read_video(url: str, allow_audio: bool = True) -> dict:
    """Everything we can get for one video: title, channel, text and how we got it.

    text is "" when neither captions nor audio could be read."""
    vid = video_id(url)
    if not vid:
        return {"title": "", "channel": "", "text": "", "how": "not a video link", "seconds": 0.0}
    meta = metadata(vid)
    text, how, secs = captions(vid), "captions", 0.0
    if not text and allow_audio:
        try:
            text, secs = audio_transcript(vid)
            how = "audio"
        except Exception as e:
            log.warning("YouTube audio for %s could not be read: %s", vid, e)
            text, how = "", "failed"
    elif not text:
        how = "failed"
    return {**meta, "text": text, "how": how, "seconds": secs, "url": watch_url(vid)}


def channel_feed(url: str) -> str | None:
    """Turn a channel link (@handle, /channel/UC..., /c/name) into its RSS feed."""
    if "feeds/videos.xml" in url:
        return url
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", url)
    if m:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"
    try:
        r = get_with_retry(url, tries=2)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    m = (re.search(r'"(?:channelId|externalId)":"(UC[A-Za-z0-9_-]{22})"', r.text)
         or re.search(r'feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{22})', r.text))
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}" if m else None
