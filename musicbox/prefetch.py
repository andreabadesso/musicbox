"""Download audio before playing it, instead of streaming it live.

Why this exists, with the numbers that motivated it.

At the hackathon the box ran on venue WiFi that measured 811 kB/s, then
10 kB/s, then 2.5 kB/s, from the same spot within an hour. Music Assistant
streams a remote URL in real time and gives up with

    ERROR player_queues: Timeout waiting for audio data

when the source cannot keep ahead of playback. A 192 kbps mp3 needs 24 kB/s
sustained. At 49 kB/s it died; the file itself was fine, verified by hand: a
valid 4.4 MB mp3 that simply arrived too slowly.

The fix is to stop asking a bad link to behave like a good one. Download the
file once, at whatever speed the link manages, then hand Music Assistant a
localhost URL. A download that takes ninety seconds is an inconvenience; a
stream that stalls mid-song is a failure in front of a room. And once a track
is on disk it plays perfectly for the rest of the event, even if the network
disappears entirely.

This reuses the path that already works: musicbox serves files over HTTP and MA
fetches them from 127.0.0.1, which is the same mechanism sfx have used since
day one. The container shares the host network namespace, so loopback means the
same thing on both sides.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from . import logs

# Content types a music file plausibly arrives as. octet-stream is on the list
# because signed Google Storage URLs (which is what the hackathon's generated
# tracks redirect to) serve exactly that, and refusing it would reject the very
# case this module was written for. The extension check below is the second
# opinion.
AUDIO_CONTENT_TYPES = ("audio/", "application/octet-stream", "video/mp4", "binary/octet-stream")

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".wma")

# Generous, because the whole point is surviving a slow link. A 10 MB file at
# 10 kB/s is 17 minutes, which is longer than anybody will wait, so the cap is
# a compromise: long enough to beat a bad afternoon, short enough that a caller
# is not left hanging forever.
DEFAULT_TIMEOUT = 300.0

# 100 MB. A song is single digit megabytes; anything past this is either a DJ
# set somebody pasted by accident or a mistake, and filling an SD card at an
# event is a failure that outlives the song.
DEFAULT_MAX_BYTES = 100 * 1024 * 1024


class PrefetchError(Exception):
    """Downloading failed in a way the caller should hear about in words."""


def _safe_name(url: str, content_type: str = "") -> str:
    """A cache filename that is stable per URL and cannot escape the directory.

    Keyed on a hash of the whole URL rather than its basename. Two different
    generated tracks routinely share a basename, and signed URLs carry query
    strings that change on every request while pointing at the same audio, so
    the basename is both ambiguous and unstable. The readable suffix is
    cosmetic, for whoever lists the directory later.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    tail = os.path.basename(urlparse(url).path)
    ext = ""
    for candidate in AUDIO_EXTENSIONS:
        if tail.lower().endswith(candidate):
            ext = candidate
            break
    if not ext:
        if "mpeg" in content_type or "mp3" in content_type:
            ext = ".mp3"
        elif "ogg" in content_type:
            ext = ".ogg"
        elif "flac" in content_type:
            ext = ".flac"
        else:
            ext = ".audio"
    slug = re.sub(r"[^a-zA-Z0-9._-]", "", tail)[:40].strip("._-")
    return f"{digest}-{slug}{ext}" if slug else f"{digest}{ext}"


@dataclass
class PrefetchResult:
    filename: str
    bytes: int
    cached: bool
    source: str


class Prefetcher:
    def __init__(
        self,
        cache_dir: Path,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        session_factory=None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._session_factory = session_factory or (lambda: aiohttp.ClientSession())
        # One lock per URL, so two callers asking for the same track at once
        # download it once instead of racing to write the same file. At an event
        # this is the normal case, not the exotic one: two people request the
        # same song within a minute.
        self._locks: dict[str, asyncio.Lock] = {}

    def handles(self, media: str) -> bool:
        """True when this looks like a remote http(s) URL worth downloading.

        Deliberately not our own base URL: an sfx or an already cached file is
        already local, and fetching ourselves would be a loop that looks like a
        hang.
        """
        if not media or not media.lower().startswith(("http://", "https://")):
            return False
        if self.base_url and media.startswith(self.base_url):
            return False
        host = (urlparse(media).hostname or "").lower()
        return host not in ("127.0.0.1", "localhost", "::1")

    def local_url(self, filename: str) -> str:
        return f"{self.base_url}/cache/file/{filename}"

    def cached_path(self, filename: str) -> Path | None:
        path = self.cache_dir / filename
        # The name comes from _safe_name, but a caller reaching the route can
        # send anything, so the containment check is not redundant there.
        try:
            path.resolve().relative_to(self.cache_dir.resolve())
        except (ValueError, OSError):
            return None
        return path if path.is_file() else None

    async def ensure_local(self, url: str) -> PrefetchResult:
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            return await self._ensure(url)

    async def _ensure(self, url: str) -> PrefetchResult:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        provisional = _safe_name(url)
        existing = self.cached_path(provisional)
        if existing is not None:
            logs.log("prefetch_hit", file=provisional, bytes=existing.stat().st_size)
            return PrefetchResult(provisional, existing.stat().st_size, True, url)

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        written = 0
        started = asyncio.get_event_loop().time()
        session = self._session_factory()
        try:
            async with session:
                async with session.get(url, timeout=timeout, allow_redirects=True) as response:
                    if response.status >= 400:
                        raise PrefetchError(
                            f"The source answered {response.status} when asked for the audio."
                        )
                    content_type = (response.headers.get("content-type") or "").lower()
                    filename = _safe_name(url, content_type)
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise PrefetchError(
                            f"That file is {int(declared) // (1024 * 1024)} MB, over the "
                            f"{self.max_bytes // (1024 * 1024)} MB limit, so it was not downloaded."
                        )

                    target = self.cache_dir / filename
                    # Written to a temporary name and renamed at the end. A
                    # half downloaded file under the real name is worse than no
                    # file: the cache would serve a truncated song forever, and
                    # the failure would look like a corrupt source.
                    tmp = target.with_suffix(target.suffix + ".part")
                    try:
                        with open(tmp, "wb") as handle:
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                written += len(chunk)
                                # Enforced DURING the download, not after. A
                                # source that lies in content-length, or sends
                                # none at all, would otherwise fill the disk
                                # before anybody could object.
                                if written > self.max_bytes:
                                    raise PrefetchError(
                                        "The download passed the size limit and was stopped."
                                    )
                                handle.write(chunk)
                        os.replace(tmp, target)
                    finally:
                        if tmp.exists():
                            tmp.unlink(missing_ok=True)
        except PrefetchError:
            raise
        except asyncio.TimeoutError as exc:
            raise PrefetchError(
                f"Downloading the audio took longer than {self.timeout:.0f}s and was given up on. "
                "The network here is probably too slow right now."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - the caller wants a sentence
            raise PrefetchError(f"Could not download the audio: {type(exc).__name__}: {exc}") from exc

        if written == 0:
            raise PrefetchError("The source returned an empty file.")

        elapsed = max(asyncio.get_event_loop().time() - started, 0.001)
        logs.log(
            "prefetch_done",
            file=filename,
            bytes=written,
            seconds=round(elapsed, 1),
            rate_kbs=round(written / elapsed / 1024, 1),
        )
        return PrefetchResult(filename, written, False, url)
