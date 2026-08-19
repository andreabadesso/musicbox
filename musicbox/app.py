"""The HTTP surface. Every endpoint here is part of the frozen contract.

Two rules shape the whole file:

  1. Nothing crashes the process. MA being down, the player being missing, the
     speaker being asleep and the token being wrong are all normal states at a
     live event. They produce a 503 with a sentence explaining what is wrong.
     /health always answers 200 and tells the truth.
  2. No handler ever touches the websocket directly. It awaits a future that
     the single reader task resolves. See ma_client for why.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import logs
from .board import BOARD_HTML
from .config import Config
# mixer_client only, never mixer: the mixer needs numpy and an ALSA device, and
# this process has to start on a laptop with neither. The client is a unix
# socket and a shlex parser and nothing else.
from .mixer_client import MixerClient, MixerUnavailable
from .prefetch import PrefetchError, Prefetcher
from .ma_client import (
    BUFFERED_DETAIL,
    ERR_INSUFFICIENT_PERMISSIONS,
    ERR_INVALID_COMMAND,
    ERR_MEDIA_NOT_FOUND,
    ERR_PLAYER_COMMAND_FAILED,
    ERR_PLAYER_UNAVAILABLE,
    ERR_QUEUE_EMPTY,
    ERR_UNEXPECTED,
    ERR_UNPLAYABLE_MEDIA,
    AUTH_ERRORS,
    SEARCH_LIMIT_MAX,
    SEARCH_MEDIA_TYPES,
    MAClient,
    MAError,
    MANotConnected,
    MATimeout,
)

from . import __version__

# Extensions we are willing to hand to MA. MA re-encodes whatever we serve, but
# keeping the list short means a stray .txt in the sfx dir cannot become a
# playable name.
SFX_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus")

# What "over" really does on this stack. Music Assistant 2.9 has no ducking
# anywhere (grep the server tree for "duck" and the only hit is a comment in the
# demo provider). On the Snapcast provider an announcement swaps the group's
# audio source to a new stream, plays it alone, then swaps back, so the music is
# silenced rather than lowered and the queue keeps running unheard underneath.
# Saying so in the response is better than pretending the contract was honored.
OVER_NOTE = (
    "Music Assistant has no ducking. The music is silenced for the length of the "
    "drop and rejoins the track wherever it got to, it does not play underneath."
)

# The other half of that sentence, for when musicbox-mixer is running. It is a
# different note rather than a flag on the old one because a caller reading the
# answer has to be able to tell the two behaviours apart without knowing this
# feature exists: they SOUND completely different.
MIXER_NOTE = (
    "Played through musicbox-mixer: the effect is layered on top of the music "
    "and the music ducks under it instead of stopping. Pressing again while it "
    "plays adds another copy rather than waiting in line."
)


class MediaBody(BaseModel):
    uri: str | None = Field(default=None, description="A provider URI, for example spotify://track/xyz")
    url: str | None = Field(default=None, description="An http(s) URL to an audio file")
    query: str | None = Field(
        default=None,
        description="Free text, for example 'Above and Beyond Sun in Your Eyes'. "
        "It is searched and the first playable track wins.",
    )
    position: str | None = Field(
        default=None,
        description="Where it lands in the queue: end (default), fair, next, now "
        "or replace. Only POST /queue reads this; POST /play always replaces.",
    )
    force: bool = Field(
        default=False,
        description="Enqueue even when the same track is already waiting further "
        "down the queue. Without it a duplicate answers ok with action "
        "'already_queued' and does not enqueue.",
    )

    def wanted(self) -> str:
        """The one string the caller wants played, whichever field it arrived in.

        The three fields are collapsed on purpose and the value is classified by
        looks_like_uri afterwards rather than by which key it came in. A caller
        that puts free text in `uri` (models do this constantly, and so do
        humans copying an old example) gets a search instead of a 404, and a
        share URL sent as `query` still resolves to exactly that track, because
        MA's own search short-circuits a share URL into a direct lookup.

        The first NON-BLANK field wins, not the first truthy one. A caller that
        fills in every field and blanks the ones it is not using sends
        {"uri": " ", "query": "baile de favela"}, and picking `uri` because a
        single space is truthy answered 400 "one of 'uri', 'url' or 'query' is
        required" with a perfectly good query sitting right there in the body.
        An empty string worked and a space did not, which is not a distinction
        anybody can debug from the outside.
        """
        for value in (self.uri, self.url, self.query):
            text = (value or "").strip()
            if text:
                return text
        raise HTTPException(
            status_code=400, detail="one of 'uri', 'url' or 'query' is required"
        )


class DropBody(BaseModel):
    url: str
    mode: str = "over"


class SfxBody(BaseModel):
    """Body for POST /sfx/{name}.

    It exists so that `/sfx/{name}` takes `mode` the same way `/drop` does. The
    mode used to be query-string only, which meant a caller copying the /drop
    shape sent {"mode": "cut"} in the body, got a 200 back, and silently got
    "over" instead. Both forms are accepted now; the body wins when both are
    present, because that is the one the caller had to go out of their way to
    send.
    """

    mode: str | None = None


class VolumeBody(BaseModel):
    level: int


# ── auth ──────────────────────────────────────────────────────────────────────


async def require_auth(request: Request) -> None:
    token: str = request.app.state.config.token
    if not token:
        return
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    # compare_digest, not ==, so a token cannot be recovered a byte at a time
    # over the tailnet. Compared as BYTES: the str form of compare_digest raises
    # TypeError on any non-ASCII character, so a token with an accent in it
    # would turn every request into a 500 instead of a 401.
    expected = token.encode("utf-8")
    supplied = presented.strip().encode("utf-8")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


# ── sfx ───────────────────────────────────────────────────────────────────────


def list_sfx(sfx_dir: Path) -> list[dict[str, Any]]:
    try:
        entries = sorted(p for p in sfx_dir.iterdir() if p.is_file())
    except OSError:
        return []
    out = []
    for path in entries:
        if path.suffix.lower() not in SFX_EXTENSIONS:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        out.append({"name": path.stem, "file": path.name, "bytes": size})
    return out


def resolve_sfx(sfx_dir: Path, name: str) -> Path | None:
    """Match a request name against the directory listing.

    The candidate path is never built by joining user input onto the sfx dir, it
    is always an entry we listed ourselves. That makes ../../etc/shadow simply
    not match anything, with no normalising to get wrong.
    """
    wanted = name.strip().lower()
    if not wanted:
        return None
    try:
        # Sorted, not raw iterdir order: with both airhorn.mp3 and airhorn.wav
        # in the directory the stem "airhorn" is ambiguous, and readdir order is
        # filesystem dependent. Sorting at least makes the winner the same file
        # every time, so a test that passes on the Mac passes on the Pi.
        entries = sorted(p for p in sfx_dir.iterdir() if p.is_file())
    except OSError:
        return None
    for path in entries:
        if path.suffix.lower() not in SFX_EXTENSIONS:
            continue
        if wanted in (path.name.lower(), path.stem.lower()):
            return path
    return None


def sfx_base_url_from_config(config: Config) -> str:
    """The sfx base URL derivable without an incoming request.

    Split out of sfx_base_url so the MCP tools can build an sfx URL too. They
    have no Request to read a Host header from, so for them this IS the whole
    resolution: MUSICBOX_SFX_BASE_URL, or our own listen address.
    """
    if config.sfx_base_url:
        return config.sfx_base_url
    host = config.host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    elif ":" in host:
        # An IPv6 literal has to be bracketed or the port separator is
        # ambiguous and MA's fetch fails on a URL that looks fine by eye.
        host = f"[{host}]"
    return f"http://{host}:{config.port}"


def sfx_base_url(request: Request) -> str:
    """The base URL Music Assistant should use to fetch our sfx files.

    This indirection exists because MA refuses anything that is not http(s) for
    an announcement (controllers/players/controller.py: "Only URLs are supported
    for announcements"), so a local path or file:// under MUSICBOX_SFX_DIR
    cannot be handed over. musicbox therefore serves the file itself and passes
    MA a URL.

    The trap is which address goes in that URL. MA runs in a podman container.
    If it does not use host networking, 127.0.0.1 inside that container is MA
    itself, not us, and the announcement fails with a fetch error that looks
    like a broken file. So the order is:

      1. MUSICBOX_SFX_BASE_URL, when the operator knows the reachable address.
         The NixOS module always sets this, to a loopback URL, because with the
         MA container on the host network namespace loopback is the one address
         that is guaranteed to mean the same thing on both sides. Deployments
         should not fall through to 2 or 3.
      2. The Host header of the incoming request. Best effort only, and the
         reason it is not first: the caller might have reached us by a MagicDNS
         name that resolves through the host's resolver, and the container has
         its own /etc/resolv.conf. It is also caller controlled, so a request
         with a forged Host would make MA fetch audio from somewhere else
         entirely. Fine as a fallback on a private tailnet, not fine as the
         primary.
      3. Our own listen address, as a last resort.

    Only MA has to reach this URL. MA fetches the file, re-encodes it and hosts
    it on its own stream server, so the speaker never sees our address.
    """
    config: Config = request.app.state.config
    if config.sfx_base_url:
        return config.sfx_base_url
    host_header = request.headers.get("host", "").strip()
    if host_header:
        return f"{request.url.scheme}://{host_header}"
    return sfx_base_url_from_config(config)


def sfx_file_url(base: str, filename: str) -> str:
    """URL MA should fetch a given sfx file from, given a resolved base.

    quote() is not decoration. `airhorn 2.mp3` is a perfectly ordinary filename
    for something an operator dropped in with Finder, and an unescaped space in
    the URL makes MA's fetch fail with an error that reads like the file is
    missing. safe="" so that a name containing a slash cannot alter the path.
    """
    return f"{base}/sfx/file/{quote(filename, safe='')}"


def sfx_url(request: Request, filename: str) -> str:
    return sfx_file_url(sfx_base_url(request), filename)


# ── helpers ───────────────────────────────────────────────────────────────────


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def live_position(queue: dict | None) -> float | None:
    """elapsed_time on a PlayerQueue is a snapshot, not a clock.

    corrected_elapsed_time is a server side property and is not serialized, so
    the live position has to be reconstructed here.
    """
    if not queue:
        return None
    elapsed = _get(queue, "elapsed_time")
    if elapsed is None:
        return None
    # Every float() here is guarded. These fields come straight off the wire
    # from a server we do not version-lock, and GET /now returning 500 because
    # a future MA sent an ISO timestamp where a unix float used to be would be
    # a silly way to lose the one endpoint that tells you what is going on.
    try:
        elapsed = float(elapsed)
        if str(_get(queue, "state", "")).lower() != "playing":
            return elapsed
        updated = _get(queue, "elapsed_time_last_updated")
        if not updated:
            return elapsed
        speed = float(_get(queue, "playback_speed") or 1.0)
        return elapsed + max(0.0, time.time() - float(updated)) * speed
    except (TypeError, ValueError):
        return None


def describe_track(queue: dict | None) -> dict[str, Any] | None:
    item = _get(queue, "current_item") if queue else None
    if not item:
        return None
    media_item = _get(item, "media_item") or {}
    artists = _get(media_item, "artists") or []
    artist = None
    # isinstance rather than truthiness: if a future MA sends artists as a dict
    # or a bare string, artists[0] is a KeyError or a single character, and
    # this is not a field worth 500ing GET /now over.
    if isinstance(artists, (list, tuple)) and artists:
        artist = _get(artists[0], "name")
    album = _get(media_item, "album")
    return {
        "title": _get(item, "name") or _get(media_item, "name"),
        "artist": artist,
        "album": _get(album, "name") if album else None,
        "uri": _get(item, "uri") or _get(media_item, "uri"),
        "duration": _get(item, "duration") or _get(media_item, "duration"),
    }


def ma_error_status(exc: MAError) -> int:
    if exc.code == ERR_MEDIA_NOT_FOUND:
        return 404
    if exc.code == ERR_UNPLAYABLE_MEDIA:
        return 422
    if exc.code == ERR_QUEUE_EMPTY:
        return 409
    if exc.code == ERR_INVALID_COMMAND:
        return 501
    if exc.code in (ERR_PLAYER_UNAVAILABLE, ERR_PLAYER_COMMAND_FAILED):
        return 503
    if exc.code in AUTH_ERRORS or exc.code == ERR_INSUFFICIENT_PERMISSIONS:
        # Ours to fix, not the caller's: the MA token is missing or expired.
        return 502
    return 502


# ── service layer ─────────────────────────────────────────────────────────────
# These three are the whole of what "now", "reconnect" and "drop" mean, with no
# HTTP in them. GET /now and POST /reconnect are thin wrappers, and the MCP
# tools in mcp_server.py call the same functions rather than looping an HTTP
# request back into our own process.


# ── search, resolution and the queue listing ──────────────────────────────────
# This is the jukebox half of the box. Everything here is shared: GET /search,
# POST /play, POST /queue, GET /queue and the MCP tools all end up in these
# functions, so "toca Baile de Favela" means the same thing however it arrived.

# Which SearchResults key holds which media type. MA does not pluralize
# uniformly: `radio` is singular, and `genres` sits between `albums` and
# `tracks`. Every key is read with .get(key, []) because which ones exist varies
# with the server version, and the live box has no `sound_effects` key even
# though the newer models declare one.
RESULT_KEYS = {
    "track": "tracks",
    "album": "albums",
    "artist": "artists",
    "playlist": "playlists",
}

# Default number of results, for /search and for resolving free text alike.
# Deliberately the same number in both places: MA's 600 s result cache is keyed
# on the limit as well as the query, so a search at 5 warms a play at 5 and one
# person asking for a song twice costs 1.3 s once and 5 ms after that.
SEARCH_LIMIT_DEFAULT = 5

# How many upcoming items GET /queue returns by default, and the ceiling. The
# real queue can be hundreds of items long (there is an 808 item queue on the
# box next to this one); nobody reading "what is coming up" wants 808, and an
# agent pasting them into a context window wants it even less. The honest total
# is always reported separately.
QUEUE_PAGE_DEFAULT = 20
QUEUE_PAGE_MAX = 100

# ── where a request lands in the queue ────────────────────────────────────────
# The name a caller gives, and the MA QueueOption it becomes. "fair" is the one
# musicbox composes itself; every other name is a single MA option.
#
# This mapping is the whole reason the jukebox works. Before it existed every
# enqueue was "add", and on a queue holding a 213 item background playlist a
# request from a person standing in the room landed at position 214 and never
# played. The box looked like it worked and silently did nothing.
#
# "end" MUST stay the default. Every existing caller of POST /queue and of the
# queue tool sends no position at all and means what it has always meant.
QUEUE_POSITIONS = {
    "end": "add",
    "next": "next",
    # add, then move_item into place. See enqueue_fair.
    "fair": "add",
    "now": "play",
    "replace": "replace",
}
QUEUE_POSITION_DEFAULT = "end"

# Positions that interrupt whatever is playing right now. They are listed
# separately because they skip the duplicate check: "already in the queue at 40"
# is not an answer to somebody who asked for a song NOW, and refusing them would
# make the interrupt silently not interrupt.
INTERRUPTING_POSITIONS = frozenset({"now", "replace"})

# MA's remaining QueueOption, replace_next, is deliberately NOT exposed. It inserts
# after the current item and deletes everything after it, so on the live 213 item
# queue its blast radius is the entire evening's music. No phrase a person says
# at a party maps to it, and a model reaching for it by mistake cannot be undone.

# How far ahead of the current track the duplicate check looks. 50 items is
# about three hours of music, costs 152 KB and 17 ms against the live box, and
# covers any queue anybody is actually waiting through. The whole 213 item queue
# is 637 KB, which is not a thing to fetch on every single request. When the
# queue is longer than the window the answer says so rather than claiming the
# check was exhaustive.
DUP_WINDOW_DEFAULT = 50
DUP_WINDOW_MAX = 200

# How many items are read back after an append to find the queue_item_id of what
# was just added. play_media returns null, so this read is the only way to learn
# it. Small because the item is the last one in the list by construction.
TAIL_WINDOW = 5

# How many items are read at the target index to verify where a move actually
# landed. move_item is a silent no-op when the destination is out of range, so
# the landed position is always confirmed by looking rather than by assuming.
LANDING_WINDOW = 5

# Longest the in-process memory of inserted request ids is allowed to get. It is
# only ever scanned against a queue window, so old ids cost nothing but memory,
# and a box left running for a week should not accumulate them forever.
LANE_MEMORY_MAX = 500

# Longest search text we will send to Music Assistant. A song request is a
# handful of words. Twenty thousand characters is a model looping or somebody
# pasting a document, and it is not free: it costs a Spotify round trip, a
# 600 s cache entry keyed on the whole string, and the same text echoed back in
# the not-found sentence and from there into a model's context window. Long
# enough that no real request can hit it, short enough that none of that
# happens.
SEARCH_QUERY_MAX = 250

# A URI scheme here is a Music Assistant PROVIDER INSTANCE ID, not a fixed
# protocol name, and instance ids carry a per-install random suffix after a
# DOUBLE HYPHEN: spotify--asK7Swun://track/57HLbw5C35P2CjpNJ9ALuS. So the scheme
# alphabet has to allow hyphens and digits. The trailing /\S+ is what stops the
# bare string "spotify://" from counting as a URI.
_MA_URI = re.compile(r"^[A-Za-z][A-Za-z0-9_.+-]*://[^/\s]+/\S+$")

# Spotify's colon form, anchored to real media types so that ordinary prose with
# two colons in it ("toca isso: agora: vai") is not mistaken for a URI. MA's own
# rule is merely "exactly two colons", which misfires on speech.
_COLON_URI = re.compile(
    r"^[a-z_]+:(track|album|artist|playlist|radio|audiobook|podcast):[A-Za-z0-9]+$"
)


def coerce_limit(value: Any, default: int, ceiling: int) -> int:
    """A caller supplied limit, or a sentence saying why it is not a number.

    Wide on purpose, because "5" and 5 and 5.0 are the same intention arriving
    by three different routes: over MCP a model picks whichever the schema lets
    through, and in a query string every value is text before anything sees it.
    Refusing "5" would be pedantry. Refusing "lots" with a sentence is the half
    that is worth doing.

    round(float(...)) and not int(...), for the same reason set_volume does it:
    int("5.0") is a ValueError, and turning a perfectly clear request into an
    error message is not a service to anyone. OverflowError is caught because
    float("inf") succeeds and round(inf) then raises, and that has to read as a
    bad argument rather than as a musicbox bug.

    Out of range is CLAMPED rather than refused. Somebody asking for 500 results
    at an event wants as many as they can have, not an error, and the ceiling is
    a latency guard rather than a rule about what may be asked for.
    """
    if value is None:
        return default
    try:
        number = round(float(value))
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(
            status_code=400, detail=f"limit must be a number, got {value!r}"
        ) from None
    return max(1, min(number, ceiling))


class NoPlayableMatch(Exception):
    """Searched, and nothing came back that can actually be played.

    Carries the whole sentence, including the query, because both surfaces show
    it to a person: HTTP turns it into a 404 detail and the MCP tools return it
    as their answer.
    """


def looks_like_uri(text: str) -> bool:
    """True only when the input really is a URI or URL, false for everything else.

    The failure direction is the whole point. Misreading a URI as text is cheap:
    MA's music/search runs parse_uri itself, so a share URL handed to search
    still resolves to exactly that one item (verified live). Misreading text as
    a URI is expensive: play_media either no-ops or fails, and "toca spotify"
    silently plays nothing. So when in doubt this returns False.

    The whitespace reject is the highest value rule in here. Every PROVIDER URI
    form MA accepts is whitespace free and essentially every spoken request has
    a space in it, so that single check kills almost every false positive on its
    own.

    Deliberately NARROWER than MA's parse_uri, which also accepts an existing
    file path on disk. That rule is not implemented here: a stranger's free text
    at an event should never cause a filesystem stat.
    """
    value = (text or "").strip()
    if not value:
        return False
    low = value.lower()
    # An explicit http(s) scheme is decided BEFORE the whitespace reject, and
    # that ordering is load bearing. `http://box:8099/sfx/file/my song.mp3` is
    # exactly what an operator's own file URL looks like, and it is still a URL
    # with a space in it rather than a song request. Sent to search, it answered
    # 200 having silently played an unrelated track, which is the single worst
    # outcome this endpoint has: the caller asked for one specific file and got
    # somebody else's song with no sign anything went wrong. Passed through, MA
    # rejects the malformed URL out loud and the caller can fix it. It also
    # keeps the pre-jukebox behaviour of the `url` field, which handed
    # everything to MA untouched.
    #
    # The cost is that "https://example.com/a.mp3 please" is now read as a URL
    # and fails at MA instead of being searched for. That trade is taken on
    # purpose: a loud failure on a request that was never going to work beats a
    # silent wrong track, and text that merely MENTIONS a link ("toca
    # https://... please") does not start with the scheme and is unaffected.
    if low.startswith(("http://", "https://", "rtsp://", "rtmp://")):
        return True
    if " " in value or "\t" in value:
        return False
    return bool(_MA_URI.match(value) or _COLON_URI.match(value))


def item_is_playable(item: Any) -> bool:
    """Whether a search result can actually be played, which is not `is_playable`.

    The obvious field is the wrong one. Top level `is_playable` defaults to True
    and in MA 2.9.13 is only ever set False by the yandex_music provider and by
    browse folders, so every Spotify result says true including region blocked
    ones. Spotify's real answer is folded into the provider mapping instead
    (providers/spotify/parsers.py: available = not is_local and is_playable), so
    a filter on the top level field hands you a track that fails at play time.

    The `provider_mappings is None` branch is for an ItemMapping stub, which the
    SearchResults type permits in place of a full item and which carries a flat
    `available` field instead. Not observed live, handled rather than KeyErrored.

    Every read here defaults to AVAILABLE when the field is absent, including
    the one inside the mapping, and that default is deliberate in the fail-open
    direction. ProviderMapping.available is declared `bool = True` in
    music_assistant_models, so an absent key means true, not false. Reading it
    with no default made a missing key mean None, which is falsy, which made
    every result from a provider that did not serialize it unplayable. The
    visible symptom would be the box refusing every single song at an event
    while saying "found 5 matches but none of them can be played here, usually
    because the track is not available in this region", pointing whoever is
    debugging it at Spotify licensing instead of at this line.
    """
    if not _get(item, "is_playable", True):
        return False
    mappings = _get(item, "provider_mappings")
    if mappings is None:
        return bool(_get(item, "available", True))
    if not isinstance(mappings, (list, tuple)):
        return True
    return any(_get(mapping, "available", True) for mapping in mappings)


def _title_of(item: Any) -> str | None:
    """name, plus the version in parentheses when there is one.

    Not decoration. This library holds both "Sun In Your Eyes" and "Sun In Your
    Eyes" with version "Marsh Remix", and without the version they are the same
    string, so a caller reading back what it queued cannot tell whether it got
    the one that was asked for. MA composes display names the same way.
    """
    name = _get(item, "name")
    version = (_get(item, "version") or "").strip()
    if name and version:
        return f"{name} ({version})"
    return name


def _artist_of(item: Any) -> str | None:
    artists = _get(item, "artists") or []
    if not isinstance(artists, (list, tuple)):
        return None
    # "/" is the separator MA itself uses in Track.artist_str, so a title that
    # musicbox reads back matches what shows up in Music Assistant's own UI.
    names = [n for n in (_get(a, "name") for a in artists) if n]
    return "/".join(names) or None


def describe_search_item(item: Any) -> dict[str, Any]:
    album = _get(item, "album")
    duration = _get(item, "duration")
    return {
        "title": _title_of(item),
        "artist": _artist_of(item),
        "album": _get(album, "name") if album else None,
        "uri": _get(item, "uri"),
        "media_type": _get(item, "media_type"),
        "playable": item_is_playable(item),
        # Rounded because MA sends a float here (291.026) and an int in queue
        # items (337). Same field, same endpoint family, so it is made to look
        # the same rather than leaving the caller to notice.
        "duration": round(duration) if isinstance(duration, (int, float)) else None,
    }


async def search_media(
    ma: MAClient,
    query: str,
    media_type: str = "track",
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> dict[str, Any]:
    """Search every provider Music Assistant has, and flatten one media type.

    Empty results are a normal answer, not an error. MA swallows a failing
    provider entirely (per provider try/except plus return_exceptions=True on
    the gather), so "the Spotify token expired" and "nobody has recorded that
    song" arrive here as the same empty list. Neither is worth a 500.
    """
    # Validated against RESULT_KEYS, which is the dict this function INDEXES,
    # rather than against ma_client.SEARCH_MEDIA_TYPES, which is the list the
    # wire call validates. Checking one and subscripting the other means the day
    # somebody adds "radio" to only one of them is a KeyError and a 500 on a
    # perfectly reasonable request. A test asserts the two stay equal.
    wanted = (media_type or "track").strip().lower()
    if wanted not in RESULT_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"type must be one of {', '.join(RESULT_KEYS)}, not {media_type!r}",
        )
    capped = coerce_limit(limit, SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MAX)

    text = (query or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="q is required and cannot be empty")
    if len(text) > SEARCH_QUERY_MAX:
        # The length is named and the text is NOT echoed. Echoing it is how a
        # 20 kB argument becomes a 20 kB error body and, over MCP, 20 kB of a
        # model's context window quoting its own mistake back at it.
        raise HTTPException(
            status_code=400,
            detail=f"the search text is {len(text)} characters, longer than the "
            f"{SEARCH_QUERY_MAX} character limit. Send a song and an artist, not a document.",
        )

    raw = await ma.search(text, media_type=wanted, limit=capped)
    items = raw.get(RESULT_KEYS[wanted]) or []
    results = [describe_search_item(item) for item in items]
    return {"ok": True, "query": text, "type": wanted, "count": len(results), "results": results}


async def resolve_media(
    ma: MAClient, wanted: str, media_type: str = "track"
) -> dict[str, Any]:
    """Turn whatever the caller sent into something play_media will accept.

    This is the single implementation behind POST /play, POST /queue and both
    MCP tools. A URI or URL is passed through untouched; anything else is
    searched and the first PLAYABLE result wins.

    First playable, and no cleverness on top. MA has already sorted by
    relevance, already hoisted exact title matches and library copies, and
    already deduped its own library against the providers. Re-ranking that from
    out here would be guessing with less information than MA had.
    """
    text = (wanted or "").strip()
    if not text:
        raise NoPlayableMatch("Nothing to play: the request was empty.")
    if looks_like_uri(text):
        return {"media": text, "query": None, "resolved": None, "searched": False}

    found = await search_media(ma, text, media_type=media_type, limit=SEARCH_LIMIT_DEFAULT)
    for result in found["results"]:
        if result["playable"] and result["uri"]:
            return {
                "media": result["uri"],
                "query": text,
                "resolved": {
                    "title": result["title"],
                    "artist": result["artist"],
                    "uri": result["uri"],
                },
                "searched": True,
            }

    # The two empty-handed cases are told apart on purpose. "Found it but it is
    # not playable here" and "found nothing at all" send the caller looking in
    # completely different places, and at an event the person asking is standing
    # right there waiting for an answer.
    if found["results"]:
        raise NoPlayableMatch(
            f"Music Assistant found {found['count']} match(es) for {text!r} but none of "
            "them can be played here, usually because the track is not available in this "
            "region or on the connected account. Try a different version or another song."
        )
    raise NoPlayableMatch(
        f"Nothing matching {text!r} was found on any connected music provider. "
        "Check the spelling, or try including the artist name, or pass a direct "
        "Spotify or http(s) link instead."
    )


def normalize_position(raw: Any) -> str:
    """A caller supplied position name, or a 400 listing the real ones.

    None and "" mean the default, because "no opinion" is by far the commonest
    case and every caller written before positions existed sends exactly that.
    An unknown name is refused rather than quietly treated as the default: a
    model that asks for "first" and is silently given "end" has been told its
    request was honored when the song went to position 214, which is the exact
    failure this whole feature exists to remove.
    """
    name = str(raw or "").strip().lower()
    if not name:
        return QUEUE_POSITION_DEFAULT
    if name not in QUEUE_POSITIONS:
        raise HTTPException(
            status_code=400,
            detail=f"position must be one of {', '.join(QUEUE_POSITIONS)}, not {raw!r}",
        )
    return name


class RequestLane:
    """Which queue items musicbox itself put in front of the background playlist.

    This exists because MA gives us no way to mark an item. Its own party
    provider marks guest items with extra_attributes and finds the end of the
    guest section by scanning forward for consecutive marked items, but
    play_media has no argument that sets extra_attributes, and every item on the
    live queue carries an empty dict there. So the only handle musicbox has is
    the queue_item_ids it saw come back from its own inserts, remembered here in
    process, in insertion order.

    The consequence is stated out loud in the README rather than hidden: a
    restart loses this memory, and "fair" then degrades to "next" until the lane
    is rebuilt. That is honest and harmless. The alternative, persisting ids that
    may not exist any more, would put requests in front of songs that are
    already playing.
    """

    def __init__(self) -> None:
        self._ids: dict[str, list[str]] = {}

    def remember(self, queue_id: str, item_id: str) -> None:
        if not queue_id or not item_id:
            return
        ids = self._ids.setdefault(queue_id, [])
        if item_id in ids:
            return
        ids.append(item_id)
        if len(ids) > LANE_MEMORY_MAX:
            del ids[: len(ids) - LANE_MEMORY_MAX]

    def ids(self, queue_id: str) -> list[str]:
        return list(self._ids.get(queue_id, ()))

    def forget(self, queue_id: str, item_id: str) -> None:
        ids = self._ids.get(queue_id)
        if ids and item_id in ids:
            ids.remove(item_id)


def uri_key(uri: Any) -> tuple[str, str] | None:
    """A URI reduced to what makes two of them the same track.

    A Music Assistant URI carries the PROVIDER INSTANCE, not the provider:
    spotify--asK7Swun://track/2tej1KSqNuxwywIpY1rDRc. Search results and queue
    items both use that instance form, so comparing a search-resolved uri to a
    queue item's uri is a plain string match and works.

    An agent that types spotify://track/<id> by hand is the case that needs this
    function. MA resolves that form perfectly well, and it will never string
    match the queue. Cutting the instance suffix at the double hyphen makes the
    two comparable without pretending that two different providers holding the
    same id are the same track.

    Returns None for anything without a scheme, which is what a raw stream URL
    queued through the builtin provider looks like. Those can never be matched,
    and returning None keeps them from all matching each other.
    """
    text = str(uri or "").strip().lower()
    scheme, sep, rest = text.partition("://")
    if not sep or not rest:
        return None
    return scheme.split("--", 1)[0], rest


def find_duplicate(items: list, offset: int, uri: str) -> int | None:
    """Position of the first item in `items` that is the same track as `uri`.

    Matched on uri and never on title. The live queue holds three items whose
    names all contain "Hide and Seek" at positions 142 and 147 and elsewhere, and
    they are different remixes with different uris. Matching on the name would
    refuse a song nobody had asked for yet.
    """
    wanted = uri_key(uri)
    if wanted is None:
        return None
    for i, item in enumerate(items):
        media_item = _get(item, "media_item") or {}
        if uri_key(_get(media_item, "uri")) == wanted:
            return offset + i
    return None


async def queue_state(ma: MAClient, window: int = DUP_WINDOW_DEFAULT) -> dict[str, Any]:
    """Everything an enqueue needs to know about the queue, in two calls.

    One get_active_queue for the counters and one items read for the window
    ahead of the playhead. The window pays for both the duplicate check and the
    end of the request lane, so it is fetched once and passed around rather than
    read twice.

    current_index and index_in_buffer are BOTH kept, and they are not the same
    number on this box. The pi5 queue is permanently in flow mode (the snapcast
    provider hardcodes requires_flow_mode), so current_index is the AUDIBLE track
    reconstructed from the stream log, while index_in_buffer is how far the flow
    generator has already read ahead, normally the same and up to one track
    further on for the last half minute of a song. Anything a person is told
    ("plays in 3 songs") counts from current_index. Anything about where an
    insert may legally land counts from index_in_buffer, because MA refuses to
    move or delete at or before it.
    """
    capped = coerce_limit(window, DUP_WINDOW_DEFAULT, DUP_WINDOW_MAX)
    queue = await ma.get_active_queue()
    if not queue or not _get(queue, "queue_id"):
        return {
            "queue_id": None,
            "total": 0,
            "current_index": None,
            "index_in_buffer": None,
            "shuffle": False,
            "state": None,
            "offset": 0,
            "window": [],
            "exhaustive": True,
        }

    queue_id = str(_get(queue, "queue_id"))
    try:
        total = int(_get(queue, "items", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    index = _get(queue, "current_index")
    try:
        offset = max(0, int(index))
    except (TypeError, ValueError):
        offset = 0
    buffered = _get(queue, "index_in_buffer")
    try:
        buffered = int(buffered)
    except (TypeError, ValueError):
        buffered = None

    items = await ma.queue_items(queue_id, limit=capped, offset=offset) if total else []
    return {
        "queue_id": queue_id,
        "total": total,
        "current_index": index,
        "index_in_buffer": buffered,
        "shuffle": bool(_get(queue, "shuffle_enabled", False)),
        "state": _get(queue, "state"),
        "offset": offset,
        "window": items,
        # Whether the window reached the end of the queue. Reported to the
        # caller as-is, because "I checked all of it" and "I checked the next
        # three hours of it" are different claims and only one of them is true
        # on a queue this long.
        "exhaustive": offset + len(items) >= total,
    }


def duplicate_report(state: dict[str, Any], uri: str) -> dict[str, Any]:
    """Is this track already waiting, and how much of the queue was looked at.

    Only from the current index FORWARD. An already played copy is not a
    duplicate, and this is not hypothetical: the live queue holds the same
    Imogen Heap track three times at positions 1, 2 and 4 with current_index 5.
    A whole-queue check would refuse a request for it right now for no reason at
    all.
    """
    window = state["window"]
    found = find_duplicate(window, state["offset"], uri)
    checked = len(window)
    upcoming = max(0, state["total"] - state["offset"])
    return {
        "position": found,
        "checked": checked,
        "upcoming": upcoming,
        # False means: it is not in the part we looked at, and there is more
        # queue past that. Never dressed up as a clean "no".
        "exhaustive": bool(state["exhaustive"]),
    }


def _plays_after(position: int | None, current_index: Any) -> int | None:
    """How many tracks play before `position`, counting from the audible one."""
    if position is None:
        return None
    try:
        current = int(current_index)
    except (TypeError, ValueError):
        current = 0
    return max(0, position - current)


async def _find_appended(ma: MAClient, state: dict[str, Any], uri: str) -> str | None:
    """queue_item_id of the item an "add" just appended, or None.

    play_media answers null, so the id has to be fished back out of the queue.
    The item is the last one in the list by construction, so a five item tail is
    enough, and it is matched on uri so that a concurrent insert by somebody else
    cannot be mistaken for ours. Searched from the END backwards for the same
    reason: if the same track really is in the tail twice, ours is the later one.

    None is a normal answer, not a failure. With shuffle_enabled on, MA's "add"
    inserts after the current item and reshuffles the tail instead of appending,
    so the item is not where this looks. The caller degrades to "it is in the
    queue somewhere" rather than moving a track it has not identified.
    """
    queue_id = state["queue_id"]
    total = state["total"]
    offset = max(0, total - 1)
    tail = await ma.queue_items(queue_id, limit=TAIL_WINDOW, offset=offset)
    wanted = uri_key(uri)
    if wanted is None:
        return None
    for item in reversed(tail):
        media_item = _get(item, "media_item") or {}
        if uri_key(_get(media_item, "uri")) == wanted:
            return _get(item, "queue_item_id")
    return None


def _lane_target(state: dict[str, Any], lane_ids: list[str]) -> tuple[int, bool]:
    """Where the next request should land, and whether the lane was fully seen.

    The lane is the run of musicbox-inserted requests sitting between the
    playhead and the background playlist. A new request goes AFTER the last one
    still waiting, which is what makes ten requests play in the order they were
    asked for. Straight "next" puts each new request in front of the previous
    one, so ten people get served in reverse, and the first person to ask waits
    the longest. That is the unfairness this function exists to remove.

    The floor is one past the playhead, and past index_in_buffer as well when
    that is further on: an item landing at or before the buffered index is in
    audio MA has already generated, so it would be skipped over silently.
    """
    current = state["offset"]
    buffered = state["index_in_buffer"]
    target = current + 1
    if isinstance(buffered, int):
        target = max(target, buffered + 1)

    window = state["window"]
    last_is_lane = False
    for i, item in enumerate(window):
        item_id = _get(item, "queue_item_id")
        if item_id in lane_ids:
            target = max(target, current + i + 1)
            last_is_lane = i == len(window) - 1

    # Whether the whole lane was seen. The test is "does the lane still look like
    # it is running at the far edge of the window", NOT "were all the remembered
    # ids found", and getting that wrong is what the first version did: ids stay
    # remembered after their song has played, a played item sits BEHIND the
    # playhead where the window never looks, so from the second request of the
    # evening onwards every single answer carried the warning. A warning that
    # fires every time tells nobody anything.
    #
    # Not seeing a remembered id means it already played, because the lane cannot
    # start beyond the window: it is built right after the playhead (every fair
    # insert targets one past the previous one, and only "next" and "now" ever
    # insert in front of it, one slot each). So a lane member can only be past
    # the 50 item window when 50 requests are stacked in front of it, and in that
    # case the members before it ARE in the window and the last one we can see is
    # the last item of the window. Which is exactly what this looks at.
    complete = bool(state["exhaustive"]) or not last_is_lane
    return target, complete


async def enqueue_fair(
    ma: MAClient, state: dict[str, Any], uri: str, lane: RequestLane
) -> dict[str, Any]:
    """Put a request at the back of the request lane, in front of the filler.

    Append then move UP, rather than "next" then move down, and the direction is
    deliberate. The append lands far past index_in_buffer, so the buffered-region
    guard on move_item cannot trip, and if the playhead advances between the two
    calls the worst case is that the item is still sitting at the end of the
    queue rather than suddenly being the next thing everyone hears. The "next
    then push down" version leaves a window in which somebody's request cuts the
    line for real.

    Four MA calls in total, two of them reads, and the cost does not grow with
    the length of the queue. Nothing here rewrites the 213 item list from the
    client side.
    """
    queue_id = state["queue_id"]
    if not queue_id:
        # No active queue to be fair about. play_media resolves one itself and
        # fails with the same sentence every other command does, which is a
        # better answer than a lane computed against a queue that is not there.
        await ma.play_media(uri, "add")
        return {"queue_item_id": None, "queue_position": None, "exact": False, "note": None}

    landing = state["total"]  # 0-based index the appended item takes
    target, lane_complete = _lane_target(state, lane.ids(queue_id))

    await ma.play_media(uri, "add", queue_id=queue_id)

    note = None
    if not lane_complete:
        note = (
            "The request lane runs past the part of the queue that was read, so this "
            "may play before a request that was made earlier."
        )

    item_id = await _find_appended(ma, state, uri)
    if item_id is None:
        # It is queued, it is simply not where we can point at it. Saying "at the
        # end, probably" is worth more than a made up number.
        return {
            "queue_item_id": None,
            "queue_position": None,
            "exact": False,
            "note": (
                "It was added, but musicbox could not find it again to move it into "
                "place, so it is at the end of the queue."
                + (" The queue has shuffle on, which moves new items itself." if state["shuffle"] else "")
            ),
        }

    lane.remember(queue_id, item_id)

    if target >= landing:
        # Nothing of anybody else's is in front of it, so the append already put
        # it exactly where the lane ends. Moving would be a no-op at best.
        return {
            "queue_item_id": item_id,
            "queue_position": landing,
            "exact": True,
            "note": note,
        }

    try:
        # Relative, and exact in both directions because MA pops before it
        # inserts. See ma_client.move_item.
        await ma.move_item(queue_id, item_id, target - landing)
    except MAError as exc:
        if exc.code != ERR_UNEXPECTED or BUFFERED_DETAIL not in exc.details:
            raise
        # The playhead reached the target between our two calls. The song is
        # still queued, just at the end, which is exactly today's behaviour and
        # not worth failing a request over.
        return {
            "queue_item_id": item_id,
            "queue_position": landing,
            "exact": True,
            "note": (
                "It could not be moved up because the queue had already buffered that "
                "far, so it is at the end of the queue."
            ),
        }

    landed = await _verify_position(ma, queue_id, item_id, target)
    if landed is None:
        # move_item returns null for a destination out of range, having done
        # nothing at all, so a null reply proves nothing. This is what that looks
        # like from out here.
        return {
            "queue_item_id": item_id,
            "queue_position": None,
            "exact": False,
            "note": (
                "It was added, but musicbox could not confirm where it landed. Read the "
                "queue to see where it is."
            ),
        }
    return {"queue_item_id": item_id, "queue_position": landed, "exact": True, "note": note}


async def _verify_position(
    ma: MAClient, queue_id: str, item_id: str, target: int
) -> int | None:
    """Where an item really is, looked at rather than assumed, or None.

    Only the slots around the target are read, which is where the item is when
    the move worked. None means it is not there, and that is a real outcome
    rather than an error: move_item answers null and does nothing when the
    destination is out of range, leaving the item where the append put it. The
    caller says "queued, position unknown" instead of quoting the target back as
    if it had been confirmed. It does NOT search the rest of the queue for it,
    because the answer to "where exactly" is a queue read the caller can do, and
    a request must not pay for it.
    """
    window = await ma.queue_items(queue_id, limit=LANDING_WINDOW, offset=max(0, target))
    for i, item in enumerate(window):
        if _get(item, "queue_item_id") == item_id:
            return max(0, target) + i
    return None


# O prefetcher e um global de modulo, e isso e deliberado.
#
# perform_media e chamado de quatro lugares (duas rotas HTTP e dois backends
# MCP) e nenhum deles tem o Config em maos. Passar o objeto por todos exigiria
# mudar as quatro assinaturas e a fronteira do MCP na vespera do evento, para
# ganhar pureza que ninguem vai usar: existe exatamente um prefetcher por
# processo, criado no create_app e nunca trocado. Quando isso for refatorado,
# o lugar certo e um objeto de servico carregando ma + config + prefetcher.
_PREFETCHER: Prefetcher | None = None


def set_prefetcher(prefetcher: Prefetcher | None) -> None:
    global _PREFETCHER
    _PREFETCHER = prefetcher


def get_prefetcher() -> Prefetcher | None:
    return _PREFETCHER


# O cliente do mixer segue exatamente a mesma decisao do prefetcher acima, e
# pelo mesmo motivo: perform_sfx e chamado da rota HTTP e do backend MCP, e
# nenhum dos dois carrega o Config. Existe um cliente por processo, criado no
# create_app, e None quando o mixer esta desligado, que e o default.
_MIXER: MixerClient | None = None


def set_mixer(mixer: MixerClient | None) -> None:
    global _MIXER
    _MIXER = mixer


def get_mixer() -> MixerClient | None:
    return _MIXER


async def localize_media(media: str) -> dict[str, Any]:
    """Troca uma URL remota pela copia local, baixando se preciso.

    Devolve sempre um dicionario com `media` (o que deve ir para o MA) e o que
    aconteceu, porque a resposta ao chamador precisa dizer que houve download:
    numa rede ruim isso explica por que o comando demorou trinta segundos, e
    sem essa explicacao a demora parece travamento.

    Falha de download NAO derruba a chamada. Se nao deu para baixar, tocamos a
    URL original e deixamos o MA tentar em streaming, que e o comportamento
    antigo. Pior caso voltamos ao que era antes, nunca a menos que isso.
    """
    prefetcher = _PREFETCHER
    if prefetcher is None or not prefetcher.handles(media):
        return {"media": media, "prefetched": False, "reason": None}
    try:
        result = await prefetcher.ensure_local(media)
    except PrefetchError as exc:
        logs.warn("prefetch_failed", url=media, error=str(exc))
        return {"media": media, "prefetched": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        # Qualquer coisa, e nao so PrefetchError. Um cache_dir que nao da para
        # criar levantava PermissionError daqui ate virar 500, e uma otimizacao
        # de rede derrubando o pedido inteiro e o pior negocio possivel: sem
        # isso, um diretorio mal configurado tira o box do ar em vez de apenas
        # voltar a tocar em streaming. Encontrado pelos testes, e teria
        # aparecido no evento como "nao toca mais nada".
        logs.warn("prefetch_broken", url=media, error=f"{type(exc).__name__}: {exc}")
        return {"media": media, "prefetched": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "media": prefetcher.local_url(result.filename),
        "prefetched": True,
        "cached": result.cached,
        "bytes": result.bytes,
        "source": result.source,
        "reason": None,
    }


async def perform_media(
    ma: MAClient,
    wanted: str,
    position: str = QUEUE_POSITION_DEFAULT,
    force: bool = False,
    lane: RequestLane | None = None,
) -> dict[str, Any]:
    """Resolve, decide whether it is already coming, then play or enqueue.

    The response names the resolved track because free text is a guess, and the
    caller is the only one who can tell whether the guess was right. "Queued
    Baile de Favela by MC Joao" lets a person say no; a bare "ok" does not.

    `action` is the field to read, and it now has three values, not two:
    "playing", "queued", and "already_queued" for a request that was NOT
    enqueued because the same track is still waiting. That last one is ok: true.
    Somebody asking twice at a party is the normal case, and the useful answer is
    "it is coming in three songs", not an error.
    """
    position = normalize_position(position)
    option = QUEUE_POSITIONS[position]
    resolution = await resolve_media(ma, wanted)
    local = await localize_media(resolution["media"])
    media = local["media"]

    answer: dict[str, Any] = {
        "ok": True,
        "position": position,
        "media": media,
        "prefetched": local["prefetched"],
        "query": resolution["query"],
        "resolved": resolution["resolved"],
    }

    if position in INTERRUPTING_POSITIONS:
        # No duplicate check and no queue read: both of these are answers to
        # "play this NOW", and neither is satisfied by a copy sitting at
        # position 40.
        await ma.play_media(media, option)
        return {
            **answer,
            "action": "playing",
            "queue_cleared": position == "replace",
            "queue_position": None,
        }

    state = await queue_state(ma)
    # Whether the box is actually sitting on current_index. Both the duplicate
    # wording and the "next" landing slot turn on it, because MA itself branches
    # on the same thing: index_in_buffer only anchors an insert while the queue
    # is playing or paused, and only then is the current item partly played.
    on_air = str(state["state"] or "").lower() in ("playing", "paused")
    duplicate = duplicate_report(state, media)
    already = duplicate["position"]
    if already is not None and not force:
        # A copy AT the current index is not something waiting: it is the track
        # the box is on. That distinction is not cosmetic. plays_after came out
        # 0 for it, 0 renders as "it is next", and somebody who asked for the
        # song that was playing was told it was coming when it was in fact
        # nearly over and would not play again. Flagged instead, and plays_after
        # is null because "how many tracks play before it" has no answer for a
        # track that is already on.
        #
        # Only while the box is actually on it. On a stopped or idle queue the
        # item at current_index has NOT played yet, it is the one that starts
        # when somebody presses play, and there "it is next" is the truth. The
        # live box sits exactly like that between sets: state idle, current_index
        # 5, index_in_buffer 5.
        playing_now = (
            on_air and isinstance(state["current_index"], int) and already == state["offset"]
        )
        return {
            **answer,
            "action": "already_queued",
            "queue_position": already,
            "already_playing": playing_now,
            "plays_after": None if playing_now else _plays_after(already, state["current_index"]),
            "duplicate_check": duplicate,
        }

    if position == "fair":
        placed = await enqueue_fair(ma, state, media, lane or RequestLane())
        landed = placed["queue_position"]
        return {
            **answer,
            "action": "queued",
            "queue_position": landed,
            "queue_position_exact": placed["exact"],
            "plays_after": _plays_after(landed, state["current_index"]),
            "queue_item_id": placed["queue_item_id"],
            "duplicate_check": duplicate,
            "note": placed["note"],
        }

    await ma.play_media(media, option, queue_id=state["queue_id"])

    # Where it went, derived rather than measured, and flagged as such. Reading
    # the queue back to confirm would double the cost of the commonest call in
    # the app for a number nobody is waiting on.
    landed: int | None
    note = None
    if position == "end":
        landed = state["total"] if state["queue_id"] else None
        if state["shuffle"]:
            # With shuffle on, "add" does not append at all: it inserts after the
            # current item and reshuffles everything after it.
            landed = None
            note = "The queue has shuffle on, so Music Assistant chose the position itself."
    else:
        # "next" anchors on index_in_buffer and NOT on current_index while the
        # queue is playing or paused, which is what _enqueue_with_option does on
        # the box: cur_index = index_in_buffer if it is not None, else
        # current_index. In flow mode those differ for the last half minute or so
        # of a track, so anchoring on current_index alone reported "plays in 1
        # song" for something that plays in 2. The same rule the fair lane
        # already uses, applied to the number a person is told.
        #
        # Still not a promise: the buffer can advance between this read and the
        # insert, which is what the note is for.
        # Clamped to the queue length so an empty or one item queue does not
        # answer "position 1" for the only track in it.
        anchor = state["offset"]
        if on_air and isinstance(state["index_in_buffer"], int):
            anchor = max(anchor, state["index_in_buffer"])
        landed = min(anchor + 1, state["total"]) if state["queue_id"] else None
        note = (
            "It plays after the current song, or after the one following it if the "
            "current song is nearly over."
        )
    return {
        **answer,
        "action": "queued",
        "queue_position": landed,
        "queue_position_exact": False,
        "plays_after": _plays_after(landed, state["current_index"]),
        "duplicate_check": duplicate,
        "note": note,
    }


def describe_queue_item(item: Any, position: int | None = None) -> dict[str, Any]:
    """One QueueItem, in the same shape /search uses for a track.

    Three traps, all live-verified, all easy to write wrong:

      * `index` on a QueueItem is ALWAYS 0. It is declared with a default and
        never assigned.
      * `sort_index` is NOT the position either, so `position` is passed in by
        the caller, counted off the offset it asked MA for. This was written the
        obvious way first and it was wrong on the live box: the queue there
        returned ordinals 0 to 7 carrying sort_index 0, 1, 2, 6, 4, 14, 18, 22.
        Non-contiguous AND non-monotonic, on a queue with shuffle_enabled false,
        because a sort_index is stamped when an item is added or moved and is
        never renumbered afterwards. What IS reliable is that MA returns the
        window in queue order starting at `offset`, so ordinal = offset + i.
        Proof it is the right number: current_index was 5, and the item at
        ordinal 5 is the one now_playing reported, while its sort_index was 14.
        Reporting sort_index tells the ninth person in line they are ninetieth.
      * `name` on a QueueItem is a PRE-COMPOSED display string built as
        "artists - title (version)", unlike a search result's bare `name`. Using
        it as the title renders "Above & Beyond/Marsh - Sun In Your Eyes (Marsh
        Remix)" by "Above & Beyond/Marsh". So title and artist are read out of
        media_item, and the composed name is only split as a fallback.

    There is also no `uri` key on a QueueItem: QueueItem.uri is a Python
    property and properties do not cross the wire.
    """
    media_item = _get(item, "media_item") or None
    composed = _get(item, "name")
    if media_item:
        title = _title_of(media_item)
        artist = _artist_of(media_item)
        album = _get(media_item, "album")
        uri = _get(media_item, "uri")
    else:
        # A raw stream URL queued through the builtin provider has no media
        # item at all. Split the composed name on the separator MA used to
        # build it, and accept that a title containing " - " will split wrong.
        artist, _, title = (composed or "").partition(" - ")
        if not title:
            title, artist = composed, None
        album = None
        uri = None
    duration = _get(item, "duration")
    return {
        "position": position,
        "title": title,
        "artist": artist,
        "album": _get(album, "name") if album else None,
        "duration": round(duration) if isinstance(duration, (int, float)) else None,
        # `uri` is null when the item has no uri, and is NEVER backfilled with
        # the queue_item_id. They are two different kinds of handle and the
        # queue_item_id is already returned under its own name right below, so
        # substituting it here bought nothing and cost the one guarantee this
        # field has: that whatever is in it can be handed straight back to /play
        # or /queue. A queue_item_id cannot. It has no scheme, so looks_like_uri
        # reads it as free text and the box searches for "6dd077e395..." and
        # plays whatever that happens to match. Silently playing the wrong track
        # is the worst thing this API can do, and a field that is a uri most of
        # the time and an opaque id the rest of the time is how you get there.
        "uri": uri,
        # queue_item_id is the handle MA wants for delete_item, move_item and
        # play_index, so it is cheaper to return it now than to make a caller
        # come back for it.
        "queue_item_id": _get(item, "queue_item_id"),
        "available": _get(item, "available", True),
    }


async def queue_snapshot(ma: MAClient, limit: int = QUEUE_PAGE_DEFAULT) -> dict[str, Any]:
    """What is coming up, current item first.

    `count` is the honest total length of the whole queue, `upcoming` is how
    many of those are at or after the current item, and `items` is the window
    actually returned. All three are reported because they routinely differ, and
    a caller that only sees len(items) will tell someone their song is next when
    it is ninetieth.

    The window is asked for by offset rather than fetched whole and sliced.
    offset lines up exactly with current_index, verified live: asking with
    offset = current_index put the track now_playing was reporting first in the
    list. It does NOT line up with each item's sort_index, which is why position
    is counted here instead of read off the item. See describe_queue_item.
    """
    capped = coerce_limit(limit, QUEUE_PAGE_DEFAULT, QUEUE_PAGE_MAX)

    queue = await ma.get_active_queue()
    if not queue or not _get(queue, "queue_id"):
        # No active queue is a normal state, not a failure: it is what an idle
        # box with nothing ever queued looks like.
        return {"ok": True, "queue_id": None, "count": 0, "index": None, "upcoming": 0, "items": []}

    queue_id = str(_get(queue, "queue_id"))
    total = _get(queue, "items", 0) or 0
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = 0
    # current_index is NULL on a queue that has never played. Defaulting it to 0
    # is what makes "upcoming" mean the whole queue in that case.
    index = _get(queue, "current_index")
    try:
        offset = max(0, int(index))
    except (TypeError, ValueError):
        offset = 0

    raw = await ma.queue_items(queue_id, limit=capped, offset=offset) if total else []
    return {
        "ok": True,
        "queue_id": queue_id,
        "state": _get(queue, "state"),
        "count": total,
        "index": index,
        "upcoming": max(0, total - offset),
        "items": [describe_queue_item(item, offset + i) for i, item in enumerate(raw)],
    }


async def now_snapshot(ma: MAClient) -> dict[str, Any]:
    queue = await ma.get_active_queue()
    player = await ma.get_player()
    return {
        "ok": True,
        "player": ma.player_id,
        "player_name": ma.player_name,
        "state": _get(queue, "state") if queue else _get(player, "playback_state"),
        "track": describe_track(queue),
        "position": live_position(queue),
        "volume": _get(player, "volume_level"),
        "muted": _get(player, "volume_muted"),
        "queue_id": _get(queue, "queue_id") if queue else None,
        "queue_length": _get(queue, "items", 0) if queue else 0,
        "queue_index": _get(queue, "current_index") if queue else None,
        "shuffle": _get(queue, "shuffle_enabled") if queue else None,
        "repeat": _get(queue, "repeat_mode") if queue else None,
    }


async def do_reconnect(ma: MAClient) -> dict[str, Any]:
    await ma.reconnect()
    connected = await ma.wait_connected(timeout=10.0)
    if connected:
        with contextlib.suppress(Exception):
            await ma.refresh_player(force=True)
    # The contract asks for the player state to be logged after a reconnect, so
    # that the journal shows what the box thought it had.
    logs.log(
        "reconnected",
        ma_connected=ma.connected,
        player=ma.player_id,
        player_name=ma.player_name,
        player_error=ma.player_error,
        features=",".join(ma.player_features) or None,
        last_error=ma.last_error or None,
    )
    return {
        "ok": connected,
        "ma_connected": ma.connected,
        "player": ma.player_id,
        "player_name": ma.player_name,
        "player_error": ma.player_error,
        "last_error": ma.last_error or None,
    }


# ── drop ──────────────────────────────────────────────────────────────────────


async def perform_drop(ma: MAClient, url: str, mode: str) -> dict[str, Any]:
    """Play a one shot sound.

    mode "over": hand it straight to MA's announcement path.
    mode "cut":  remember the queue, pause it (which records resume_pos), play
                 the announcement, then resume so the track carries on from
                 where it stopped. Doing the pause and resume ourselves is the
                 only way to get the "resume where it stopped" half of the
                 contract, because a native snapcast announcement never stops
                 the queue in the first place.
    """
    requested = mode
    fell_back = False
    reason = None

    if mode == "over" and not ma.supports_announcement():
        mode = "cut"
        fell_back = True
        reason = "the MA player does not advertise play_announcement"

    if mode == "over":
        try:
            await ma.play_announcement(url)
            return {
                "ok": True,
                "mode_requested": requested,
                "mode_used": "over",
                "fell_back": False,
                "note": OVER_NOTE,
                "url": url,
            }
        except MAError as exc:
            if exc.code not in (ERR_PLAYER_COMMAND_FAILED, ERR_INVALID_COMMAND):
                raise
            # The player claimed the feature and then refused the command. Do
            # not fail the drop over it, take the path that does work.
            mode = "cut"
            fell_back = True
            reason = f"announcement rejected by MA: {exc.details}"
            logs.warn("drop_over_failed", code=exc.code, details=exc.details)

    queue = None
    with contextlib.suppress(MAError, MATimeout):
        queue = await ma.get_active_queue()
    was_playing = str(_get(queue, "state", "")).lower() == "playing" if queue else False

    if was_playing:
        try:
            await ma.pause()
        except (MAError, MATimeout) as exc:
            # Play the sound anyway. A drop that refuses to fire because the
            # pause did not take is the worst of both worlds: no sound, and the
            # music was going to keep playing regardless. Degrade to what "over"
            # does and say so, and do not try to resume something we never
            # stopped.
            was_playing = False
            reason = f"pause failed, played without it: {exc}"
            logs.warn("drop_pause_failed", error=str(exc))

    try:
        await ma.play_announcement(url)
    finally:
        # Resume even if the announcement blew up. Leaving the music stopped
        # because a sound effect failed is the worst outcome available.
        if was_playing:
            with contextlib.suppress(MAError, MATimeout, MANotConnected):
                await ma.resume()

    return {
        "ok": True,
        "mode_requested": requested,
        "mode_used": "cut",
        "fell_back": fell_back,
        "reason": reason,
        "resumed": was_playing,
        "url": url,
    }


async def perform_sfx(
    ma: MAClient, path: Path, url: str, mode: str, mixer: MixerClient | None = None
) -> dict[str, Any]:
    """Play a sound already on the box, by the best path available.

    Two paths, and the answer always says which one was taken, because they
    sound different and somebody debugging at an event needs to know which one
    they are listening to:

      "mixer"        musicbox-mixer layers the effect on top of the music and
                     ducks the music underneath it. Retriggers are free, so
                     pressing the same key ten times gives ten sounds.
      "announcement" Music Assistant's announcement path, which is what this
                     has always done. The music is SILENCED for the length of
                     the effect, and MA serialises announcements: four presses
                     0.35s apart measured out as four playbacks about 3 seconds
                     apart on the live box.

    The mixer is used for mode "over" only. "over" is a promise the
    announcement path could never keep, so when the mixer is there it is the
    honest implementation of it; "cut" explicitly means pause, play and resume,
    which is a queue operation and belongs to MA either way.

    Any mixer failure falls straight through to the announcement path and says
    so in `reason`. A sound effect that refuses to fire because the mixer is
    down is strictly worse than one that fires the old way: the old way is what
    the box did yesterday, and it works.
    """
    if mixer is not None and mixer.enabled and mode == "over":
        try:
            answer = await mixer.play(path.stem)
            return {
                "ok": True,
                "mode_requested": mode,
                "mode_used": "over",
                "path": "mixer",
                "fell_back": False,
                "voices": _int_or_none(answer.get("voices")),
                "note": MIXER_NOTE,
                "url": url,
            }
        except MixerUnavailable as exc:
            # warn and not error: the fallback works, the box keeps playing,
            # and this is exactly the line someone greps for when the effects
            # suddenly start cutting the music again.
            logs.warn("sfx_mixer_unavailable", sfx=path.stem, error=str(exc))
            result = await perform_drop(ma, url, mode)
            result["path"] = "announcement"
            result["fell_back"] = True
            result["reason"] = f"the mixer was not usable, played through Music Assistant: {exc}"
            return result

    result = await perform_drop(ma, url, mode)
    result["path"] = "announcement"
    return result


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── app ───────────────────────────────────────────────────────────────────────


def create_app(config: Config | None = None, ma: MAClient | None = None) -> FastAPI:
    # Imported here and not at the top of the file because mcp_server imports
    # the service layer above (perform_drop, now_snapshot, list_sfx) from this
    # module, and a module level import in both directions is a cycle.
    from .mcp_server import MCP_PATH, MountedMCP

    config = config or Config.from_env()
    ma = ma or MAClient(config)

    # ONE lane for the whole process, shared by the HTTP routes and the mounted
    # MCP tools. Two lanes would mean a request queued over MCP is invisible to
    # a request queued over HTTP, and the second one would jump the first.
    lane = RequestLane()

    # Built before the app so the lifespan below can enter its session manager.
    # See mcp_server.MountedMCP for why that entering is not optional.
    mcp_mount = MountedMCP(config, ma, lane)

    # A URL base do prefetcher e a MESMA dos sfx, e nao por economia: e o
    # endereco que o Music Assistant consegue alcancar de dentro do container,
    # que ja foi resolvido e testado uma vez. Reaproveitar significa que se um
    # dia esse calculo mudar, muda para os dois juntos e nao so para metade.
    set_prefetcher(
        Prefetcher(
            config.cache_dir,
            sfx_base_url_from_config(config),
            timeout=config.prefetch_timeout,
            max_bytes=config.prefetch_max_bytes,
        )
        if config.prefetch
        else None
    )

    # None unless MUSICBOX_MIXER is on. That is the whole opt-in: with it off
    # there is no client, perform_sfx never looks at a socket, and every sfx
    # takes the same announcement path it took yesterday.
    set_mixer(MixerClient(config.mixer) if config.mixer.enable else None)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        logs.log(
            "startup",
            version=__version__,
            host=config.host,
            port=config.port,
            ma_url=config.ma_url,
            player=config.player or None,
            sfx_dir=str(config.sfx_dir),
            auth=bool(config.token),
            # Logged on every start so the journal says out loud that there is
            # an unauthenticated control surface on this port, rather than
            # leaving that fact only in the README.
            mcp=MCP_PATH,
            mcp_auth=False,
            # Said out loud at every start, because "why did the music stop for
            # that airhorn" and "why did it not" have the same one word answer
            # and this is the line that gives it.
            mixer=config.mixer.enable,
            mixer_socket=str(config.mixer.socket) if config.mixer.enable else None,
        )
        # start() only spawns the supervisor. It deliberately does not wait for
        # a connection: MA may well come up after us, and a startup that blocks
        # on it is a startup that can fail.
        await ma.start()
        try:
            # The MCP session manager owns a task group. Without this the /mcp
            # route answers every request with "Task group is not initialized",
            # and the fact that mounting a sub-app does NOT run that sub-app's
            # lifespan is the whole reason it has to be entered by hand here.
            async with mcp_mount.lifespan():
                yield
        finally:
            await ma.stop()
            logs.log("shutdown")

    app = FastAPI(title="musicbox", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.ma = ma
    app.state.lane = lane

    def logged_path(request: Request) -> str:
        """The path as routing saw it, which is not always request.url.path.

        Starlette rebuilds a URL string from the scope and re-splits it, so a
        path that already contains a decoded '#' loses everything after it:
        POST /sfx/big%20airhorn%20%232 routes correctly and answers 503, and
        request.url.path logs it as "/sfx/big airhorn ". Verified against
        starlette's URL(scope=...) directly. The journal is the debugging tool
        at the event, and a line that truncates the name is a line that sends
        someone hunting a 404 that never happened. scope["path"] is what the
        router matched on, so it is what gets logged.
        """
        return request.scope.get("path") or request.url.path

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - log it, then let it 500
            logs.error(
                "request",
                method=request.method,
                path=logged_path(request),
                status=500,
                outcome="crash",
                dur_ms=(time.monotonic() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        status = response.status_code
        outcome = "ok" if status < 400 else ("client_error" if status < 500 else "server_error")
        logs.log(
            "request",
            method=request.method,
            path=logged_path(request),
            status=status,
            outcome=outcome,
            dur_ms=(time.monotonic() - started) * 1000,
            client=request.client.host if request.client else None,
        )
        return response

    @app.exception_handler(MANotConnected)
    async def _not_connected(request: Request, exc: MANotConnected):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "music_assistant_unavailable", "detail": str(exc)},
        )

    @app.exception_handler(NoPlayableMatch)
    async def _no_match(request: Request, exc: NoPlayableMatch):
        # 404 and not 422: the request was well formed, the song simply is not
        # there. The detail names the query so a caller can quote it back at the
        # person who asked.
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": "no_playable_match", "detail": str(exc)},
        )

    @app.exception_handler(MATimeout)
    async def _timeout(request: Request, exc: MATimeout):
        return JSONResponse(
            status_code=504,
            content={"ok": False, "error": "music_assistant_timeout", "detail": str(exc)},
        )

    @app.exception_handler(MAError)
    async def _ma_error(request: Request, exc: MAError):
        return JSONResponse(
            status_code=ma_error_status(exc),
            content={
                "ok": False,
                "error": "music_assistant_error",
                "code": exc.code,
                "command": exc.command,
                "detail": exc.details,
            },
        )

    open_router = APIRouter()
    api = APIRouter(dependencies=[Depends(require_auth)])

    # ── health and the sfx file route stay unauthenticated ────────────────────
    # /health: a watchdog or a colleague debugging the box should not need the
    # token to ask whether the box is alive, and it exposes nothing secret.
    # /sfx/file: Music Assistant fetches this URL itself and has no way to
    # present our bearer token. It only serves files an operator deliberately
    # placed in MUSICBOX_SFX_DIR, and it is bound to the tailnet.

    @open_router.get("/health")
    async def health() -> dict[str, Any]:
        info = ma.server_info or await ma.probe_info()
        mixer = get_mixer()
        return {
            "ok": True,
            "ma_connected": ma.connected,
            "player": ma.player_id,
            "version": __version__,
            # A healthy socket with a bad token answers every command with
            # error_code 20, so ma_connected alone is not enough to tell
            # whether the box can actually do anything.
            "ma_authenticated": ma.authenticated,
            "player_name": ma.player_name,
            "player_error": ma.player_error,
            "ma_version": info.get("server_version"),
            # MA gates behaviour on the schema version, and the server image and
            # this client are versioned independently. When something behaves
            # unexpectedly this is the first number to look at.
            "ma_schema_version": info.get("schema_version"),
            "ma_url": config.ma_url,
            "announcement_supported": ma.supports_announcement() if ma.player_id else None,
            "connect_attempts": ma.connect_attempts,
            "connected_since": ma.connected_since,
            "last_error": ma.last_error or None,
            "sfx_count": len(list_sfx(config.sfx_dir)),
            "auth_required": bool(config.token),
            # Reported from the client's last outcome, NOT by pinging here.
            # /health is polled by the soundboard every 5 seconds and a probe
            # per poll would put the mixer's control thread on the critical
            # path of a page refresh for no new information. `null` means
            # nothing has been played since this process started, which is the
            # truth. GET /mixer is the endpoint that actually asks.
            "mixer_enabled": config.mixer.enable,
            "mixer_ok": mixer.last_ok if mixer else None,
            "mixer_error": mixer.last_error if mixer else None,
        }

    @open_router.get("/sfx/file/{name}")
    async def sfx_file(name: str):
        path = resolve_sfx(config.sfx_dir, name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"no sfx named {name!r}")
        # FileResponse and not a streamed body on purpose: MA needs a real
        # Content-Length and a probeable duration, and raises
        # "Announcement duration could not be determined" without one.
        return FileResponse(path)

    # A separate function rather than methods=["GET", "HEAD"] on one route.
    # FastAPI derives the OpenAPI operation id from the function name and the
    # path but not the method, so one route serving two methods warns
    # "Duplicate Operation ID" on every single startup, and a warning that is
    # always there is a warning nobody reads. HEAD itself is worth keeping:
    # FastAPI does not answer HEAD on a GET route (Starlette would, FastAPI's
    # APIRoute does not), and a fetcher that probes with HEAD before
    # downloading would otherwise get a 405.
    @open_router.head("/sfx/file/{name}", include_in_schema=False)
    async def sfx_file_head(name: str):
        return await sfx_file(name)

    # O mesmo par GET/HEAD para o audio baixado. Sem autenticacao pela mesma
    # razao que os sfx: quem busca aqui e o Music Assistant, de dentro do
    # container, e ele nao carrega o nosso bearer. FileResponse de novo porque
    # o MA precisa de Content-Length real para descobrir a duracao.
    # ── Soundboard ────────────────────────────────────────────────────────────
    # Sem autenticacao, como o resto das rotas abertas. E a mesma decisao ja
    # tomada para /mcp: a porta e tailnet-only, e exigir um bearer numa pagina
    # que a pessoa abre no celular no meio de um evento transforma um botao em
    # um problema de suporte.
    #
    # Sem cache: a lista de sons muda quando alguem copia um arquivo novo, e
    # uma pagina cacheada mostraria um teclado desatualizado.
    @open_router.get("/board", response_class=HTMLResponse)
    async def board():
        return HTMLResponse(BOARD_HTML, headers={"cache-control": "no-store"})

    @open_router.get("/cache/file/{name}")
    async def cache_file(name: str):
        prefetcher = get_prefetcher()
        path = prefetcher.cached_path(name) if prefetcher else None
        if path is None:
            raise HTTPException(status_code=404, detail=f"nothing cached under {name!r}")
        return FileResponse(path)

    @open_router.head("/cache/file/{name}", include_in_schema=False)
    async def cache_file_head(name: str):
        return await cache_file(name)

    @api.get("/now")
    async def now() -> dict[str, Any]:
        return await now_snapshot(ma)

    # `limit: int | float | str` on both listings, not `limit: int`. FastAPI
    # validates a query parameter before the handler runs, so a plain `int` made
    # ?limit=abc a 422 whose detail is pydantic's error LIST, while every other
    # bad argument in this app answers 400 with one sentence. It also split the
    # two MCP transports: the mounted tools call coerce_limit and get a
    # sentence, while the stdio proxy goes through this route and got the
    # pydantic dump instead. Widened, coerce_limit is the only thing that
    # decides, and both surfaces say the same thing.
    @api.get("/search")
    async def search(
        q: str,
        type: str = "track",
        limit: int | float | str = SEARCH_LIMIT_DEFAULT,
    ) -> dict[str, Any]:
        # `type` shadows the builtin inside this function only, and it is the
        # query parameter name the contract promises, so it stays.
        return await search_media(ma, q, media_type=type, limit=limit)

    @api.get("/queue")
    async def queue_list(limit: int | float | str = QUEUE_PAGE_DEFAULT) -> dict[str, Any]:
        return await queue_snapshot(ma, limit=limit)

    @api.post("/queue")
    async def queue_add(body: MediaBody) -> dict[str, Any]:
        # The default is "end", which is what this endpoint has always done, so
        # every caller written before positions existed keeps working unchanged.
        return await perform_media(
            ma, body.wanted(), body.position, force=body.force, lane=lane
        )

    @api.post("/play")
    async def play_now(body: MediaBody) -> dict[str, Any]:
        # "replace" and not "play": replace clears what was queued, which is
        # what "play now, replacing the queue" means in the contract. `position`
        # in the body is ignored here on purpose, so that a caller that wants
        # anything else has one obvious place to go: POST /queue.
        return await perform_media(ma, body.wanted(), "replace")

    @api.post("/drop")
    async def drop(body: DropBody) -> dict[str, Any]:
        mode = (body.mode or "over").strip().lower()
        if mode not in ("over", "cut"):
            raise HTTPException(status_code=400, detail="mode must be 'over' or 'cut'")
        url = body.url.strip()
        if not url.startswith(("http://", "https://")):
            # MA rejects anything else outright, so failing here gives a much
            # better message than error_code 11 from the player.
            raise HTTPException(status_code=400, detail="url must be http:// or https://")
        return await perform_drop(ma, url, mode)

    @api.get("/sfx")
    async def sfx_list(request: Request) -> dict[str, Any]:
        items = list_sfx(config.sfx_dir)
        for item in items:
            item["url"] = sfx_url(request, item["file"])
        return {"ok": True, "dir": str(config.sfx_dir), "count": len(items), "sfx": items}

    @api.post("/sfx/{name}")
    async def sfx_play(
        name: str,
        request: Request,
        body: SfxBody | None = None,
        mode: str = "over",
    ) -> dict[str, Any]:
        # Body first, query second, "over" last. See SfxBody for why both.
        chosen = (body.mode if body is not None and body.mode else mode) or "over"
        chosen = chosen.strip().lower()
        if chosen not in ("over", "cut"):
            raise HTTPException(status_code=400, detail="mode must be 'over' or 'cut'")
        path = resolve_sfx(config.sfx_dir, name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"no sfx named {name!r}")
        # The URL is still resolved even when the mixer takes the call. It
        # costs nothing, it is what the fallback needs the moment the mixer
        # answers badly, and the answer carrying it means a caller can always
        # see what MA would have been asked to fetch.
        result = await perform_sfx(
            ma, path, sfx_url(request, path.name), chosen, get_mixer()
        )
        result["sfx"] = path.stem
        return result

    # ── mixer ─────────────────────────────────────────────────────────────────
    # Never raises and never 503s. This is the endpoint someone hits at an
    # event to find out why the effects sound wrong, and an endpoint that
    # answers with an error page in that moment is one more thing to debug.
    # `ok: false` with a sentence is the answer.
    @api.get("/mixer")
    async def mixer_status() -> dict[str, Any]:
        mixer = get_mixer()
        if mixer is None or not mixer.enabled:
            return {
                "ok": True,
                "enabled": False,
                "detail": "the mixer is off, so sfx go through Music Assistant announcements",
            }
        try:
            stats = await mixer.stats()
        except MixerUnavailable as exc:
            return {
                "ok": False,
                "enabled": True,
                "socket": str(mixer.socket_path),
                "error": "mixer_unavailable",
                "detail": str(exc),
            }
        return {"ok": True, "enabled": True, "socket": str(mixer.socket_path), **stats}

    @api.post("/mixer/stop")
    async def mixer_stop() -> dict[str, Any]:
        """Cut every effect voice immediately. The music is untouched.

        The panic button: somebody holds a key down, eight voices of airhorn
        are playing, and the fastest fix has to be one request and not a
        service restart. The duck still releases over its normal fade, so this
        does not put a click through the PA.
        """
        mixer = get_mixer()
        if mixer is None or not mixer.enabled:
            return {"ok": True, "enabled": False, "stopped": False}
        try:
            await mixer.stop()
        except MixerUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "enabled": True, "stopped": True}

    @api.post("/skip")
    async def skip() -> dict[str, Any]:
        await ma.next_track()
        return {"ok": True, "action": "skipped"}

    @api.post("/pause")
    async def pause() -> dict[str, Any]:
        await ma.pause()
        return {"ok": True, "action": "paused"}

    @api.post("/resume")
    async def resume() -> dict[str, Any]:
        await ma.resume()
        return {"ok": True, "action": "resumed"}

    @api.post("/volume")
    async def volume(body: VolumeBody) -> dict[str, Any]:
        if not 0 <= body.level <= 100:
            raise HTTPException(status_code=400, detail="level must be between 0 and 100")
        await ma.set_volume(body.level)
        return {"ok": True, "action": "volume", "level": body.level}

    @api.post("/reconnect")
    async def reconnect() -> dict[str, Any]:
        return await do_reconnect(ma)

    app.include_router(open_router)
    app.include_router(api)

    # ── /mcp is deliberately UNAUTHENTICATED ──────────────────────────────────
    # Auth in this app is a router dependency (`api` above), not middleware, so
    # a route added outside that router simply never runs require_auth. That is
    # the intent here and not an oversight: MUSICBOX_TOKEN can be set and /mcp
    # still takes no credentials. The repo owner made that call because MCP
    # clients handle custom headers badly and the port is bound to the tailnet.
    #
    # Say the consequence out loud: anyone who can reach this port can play
    # audio, skip tracks and set the volume, with no token. The tailnet ACL and
    # `openFirewall`/`firewallInterfaces` in nix/module.nix are the only things
    # limiting that. Do not bind musicbox to a public interface.
    #
    # A plain starlette Route rather than app.mount(): Mount("/mcp") compiles
    # to ^/mcp/(?P<path>.*)$, so a POST to exactly /mcp misses it and only
    # reaches the handler after a 307 to /mcp/. Every client we care about
    # follows that redirect, but it costs a round trip on every single call and
    # it is one more thing to explain when a client does not. A Route matches
    # /mcp exactly, and with methods left as None it matches every verb the
    # transport needs: POST for calls, GET for the SSE stream, DELETE for
    # session teardown.
    #
    # The endpoint must be a callable OBJECT and not a plain function:
    # starlette's Route picks between "request handler" and "raw ASGI app" with
    # inspect.isfunction, and only a non-function is handed the raw ASGI
    # triple. See mcp_server.MountedMCP.
    #
    # add_route, which builds exactly that Route, rather than appending one to
    # app.router.routes by hand: fastapi 0.139 caches each router's matching
    # candidates and keys the cache on a version counter that only its own
    # mutators bump (APIRouter._mark_routes_changed). A bare list append leaves
    # that counter alone, so it works only for as long as nothing has matched a
    # request before the append. True today, and not a property to depend on.
    app.router.add_route(MCP_PATH, mcp_mount, include_in_schema=False)
    return app
