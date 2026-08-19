"""MCP surface: the same box, as tools instead of curl.

Two ways in, one set of tools:

  * In process. `create_app` mounts this on the running FastAPI app at /mcp
    (streamable HTTP, same port 8099). The tools call the service layer in
    app.py directly. They do NOT make HTTP requests back into their own
    process: that would go out through the socket, through the logging
    middleware and through auth, to reach a function that is already imported.
  * Over stdio, via the `musicbox-mcp` console script, for a client that will
    only speak stdio. That one is a thin proxy: it talks HTTP to a musicbox at
    MUSICBOX_URL and presents the identical tool set.

The two share everything above the transport. `register_tools` is the single
definition of what a tool is called, what its arguments are and what sentence
it answers with, and it is written against a small Backend interface that
LocalBackend and HttpBackend both implement. A tool that behaves differently
depending on how the agent connected would be a trap nobody would find until
the event.

THE RULE HERE: a tool never raises. Music Assistant being down, the speaker
being asleep, a typo in an sfx name and musicbox itself being unreachable are
all normal states at a live event. Every one of them comes back as a sentence
saying what happened. An exception out of a tool is an error the model has to
interpret mid-event with the music stopped, which is the exact moment it has
the least attention to spare.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp
from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .app import (
    MIXER_NOTE,
    OVER_NOTE,
    QUEUE_PAGE_DEFAULT,
    QUEUE_POSITION_DEFAULT,
    QUEUE_POSITIONS,
    SEARCH_LIMIT_DEFAULT,
    NoPlayableMatch,
    RequestLane,
    do_reconnect,
    get_mixer,
    list_sfx,
    looks_like_uri,
    now_snapshot,
    perform_drop,
    perform_media,
    perform_sfx,
    queue_snapshot,
    resolve_sfx,
    search_media,
    sfx_base_url_from_config,
    sfx_file_url,
)
from .config import Config
from .ma_client import MAClient, MAError, MANotConnected, MATimeout

# Where the streamable HTTP endpoint lives on the FastAPI app. Exactly this
# path and no trailing slash: it is what goes in `claude mcp add`, and the
# README states it, so it is a constant rather than a string in two files.
MCP_PATH = "/mcp"

MODES = ("over", "cut")

INSTRUCTIONS = """\
Controls the music box: one Bluetooth speaker driven by Music Assistant. It is
a jukebox. People ask for songs by name, so the normal path is to pass what they
said straight to queue.

play and queue take plain text ("Above and Beyond Sun in Your Eyes"), a provider
URI (spotify://track/xyz), or an http(s) URL to an audio file. Plain text is
searched and the first playable match is used, and the answer names what it
found so you can read it back and be corrected. Use search first only when you
want to choose between versions; otherwise queue the text directly and save a
round trip.

queue is the answer to a request. play is the exception: it stops the current
song and throws the rest of the queue away, so it is for starting a set, not for
honoring "toca essa". The box usually has a long background playlist loaded, so
a plain queue can put a request hours away. Pass position "fair" for a person's
request: it lands after the other requests and in front of the filler, and
several people asking are played in the order they asked. position "next" jumps
the whole line, so use it only when somebody really has to be served first.

Asking twice is normal at a party and is not an error. If the track is already
waiting, queue adds nothing and tells you where it is, so you can say how many
songs away it is. Pass force true to queue it a second time on purpose.

get_queue shows what is coming up, current track first.

drop and sfx fire a one shot sound. mode "over" plays it on top of what is
playing; mode "cut" pauses the music, plays the sound, and resumes where it
stopped. What "over" actually sounds like depends on how the box is set up, and
the answer says which one happened: with the mixer running the music ducks
under the effect and repeated presses layer, and without it Music Assistant
silences the music for the length of the sound and plays repeats one after
another. Read the answer rather than promising either behaviour up front.

Every tool answers with a sentence, including when it fails. Nothing here
raises, so read the answer rather than assuming a call succeeded.\
"""


class ToolFailure(Exception):
    """A failure a backend can already describe in one sentence.

    Raised inside a backend, caught by `explain`, and returned to the model as
    its own message with nothing added. Anything a backend cannot phrase itself
    should be an MAError, an MANotConnected or an MATimeout, which `explain`
    knows how to turn into a sentence.
    """


def explain(exc: Exception, tail: str = "Nothing was played.") -> str:
    """Every failure a tool can produce, as something a model can act on.

    Each sentence says what did NOT happen and what to try, because the model
    reading it cannot see the journal and is usually mid-event.

    `tail` is that "what did not happen" clause, and it is a parameter because
    two of these tools do not play anything. A failed search answering "Nothing
    was played" reads as a failed PLAY, and a model that believes a play failed
    goes and retries the play, which is how somebody's typo turns into an
    interruption of whatever was on.
    """
    if isinstance(exc, ToolFailure):
        return str(exc)
    if isinstance(exc, NoPlayableMatch):
        # Already a full sentence naming the query, written for exactly this
        # reader. Adding anything would only bury it.
        return str(exc)
    if isinstance(exc, HTTPException):
        # The service layer validates arguments with HTTPException because HTTP
        # is its other caller. Over MCP there is no status code to show, so only
        # the detail is worth saying.
        #
        # The period is added here because those details are written as HTTP
        # `detail` values and do not carry one ("limit must be a number, got
        # 'lots'"). Gluing the tail straight on ran the two together into
        # "...got 'lots' Nothing was searched.", which reads as one broken
        # sentence to the only reader this string has.
        detail = str(exc.detail).strip()
        if detail and detail[-1] not in ".!?":
            detail += "."
        return f"{detail} {tail}"
    if isinstance(exc, MANotConnected):
        return (
            "Music Assistant is not reachable, so nothing happened. "
            f"The last error was: {exc}. Try the reconnect tool; if that does not "
            "help, the Music Assistant server is down and a human has to look at it."
        )
    if isinstance(exc, MATimeout):
        return (
            "Music Assistant accepted the command and never answered, so it is "
            f"unclear whether it took effect ({exc}). Check now_playing before "
            "sending it again."
        )
    if isinstance(exc, MAError):
        return (
            f"Music Assistant refused the command: {exc.details} "
            f"(error code {exc.code}). {tail}"
        )
    # Deliberately last and deliberately loud about the type. If this arm is
    # ever hit it is a musicbox bug, and the model naming the exception class
    # in its report is what makes it findable afterwards.
    return f"musicbox hit an unexpected {type(exc).__name__}: {exc}. {tail}"


def _mode(raw: str | None) -> str:
    mode = (raw or "over").strip().lower()
    if mode not in MODES:
        raise ToolFailure(f"mode must be 'over' or 'cut', not {raw!r}. Nothing was played.")
    return mode


def _media(raw: str) -> str:
    media = (raw or "").strip()
    if not media:
        raise ToolFailure(
            "Give the name of a song, a provider URI, or an http(s) URL. Nothing was played."
        )
    return media


def _position(raw: str | None) -> str:
    """The position name, checked here so a wrong one is a sentence.

    The service layer validates it too, and raises an HTTPException that explain
    turns into the same words. This exists so that the check happens before the
    track is resolved: a search costs a Spotify round trip, and spending it to
    then refuse the argument is a second of an event wasted for nothing.
    """
    name = (raw or "").strip().lower()
    if not name:
        return QUEUE_POSITION_DEFAULT
    if name not in QUEUE_POSITIONS:
        raise ToolFailure(
            f"position must be one of {', '.join(QUEUE_POSITIONS)}, not {raw!r}. "
            "Nothing was queued."
        )
    return name


def _http_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url.startswith(("http://", "https://")):
        # Music Assistant rejects anything else outright (a literal
        # `if not url.startswith("http")` in its player controller), so saying
        # so here beats relaying error code 11 from the player.
        raise ToolFailure(
            f"The url must start with http:// or https://, got {raw!r}. Music "
            "Assistant cannot fetch anything else. For a file already on the box, "
            "use the sfx tool instead."
        )
    return url


# ── backends ──────────────────────────────────────────────────────────────────


class LocalBackend:
    """Calls the service layer in app.py in this same process."""

    def __init__(self, config: Config, ma: MAClient, lane: RequestLane | None = None) -> None:
        self._config = config
        self._ma = ma
        # Passed in by create_app so the HTTP routes and these tools share one
        # request lane. The fallback keeps a standalone LocalBackend (tests, and
        # any future embedding) working; it just means its "fair" ordering only
        # knows about what it queued itself.
        self._lane = lane or RequestLane()

    async def play(self, media: str) -> dict[str, Any]:
        # "replace", not "play": replace clears the queue, which is what "play
        # now, replacing the queue" means. Same call POST /play makes, resolver
        # included, so free text means the same thing on both surfaces.
        return await perform_media(self._ma, media, "replace")

    async def enqueue(
        self, media: str, position: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        return await perform_media(
            self._ma, media, position or QUEUE_POSITION_DEFAULT, force=force, lane=self._lane
        )

    async def search(self, query: str, media_type: str, limit: int) -> dict[str, Any]:
        return await search_media(self._ma, query, media_type=media_type, limit=limit)

    async def get_queue(self, limit: int) -> dict[str, Any]:
        return await queue_snapshot(self._ma, limit=limit)

    async def drop(self, url: str, mode: str) -> dict[str, Any]:
        return await perform_drop(self._ma, url, mode)

    async def sfx(self, name: str, mode: str) -> dict[str, Any]:
        path = resolve_sfx(self._config.sfx_dir, name)
        if path is None:
            known = ", ".join(item["name"] for item in list_sfx(self._config.sfx_dir))
            raise ToolFailure(
                f"There is no sound effect called {name!r}. "
                + (f"Available: {known}." if known else "There are none loaded at all.")
            )
        # The URL is built from config alone. There is no incoming request to
        # read a Host header from here, so MUSICBOX_SFX_BASE_URL (which the
        # NixOS module always sets) is what makes this reachable from inside
        # the Music Assistant container.
        url = sfx_file_url(sfx_base_url_from_config(self._config), path.name)
        # perform_sfx, not perform_drop: it takes the musicbox-mixer path when
        # the mixer is enabled and MA's announcement path when it is not, so
        # this tool and POST /sfx cannot end up disagreeing about which one a
        # given box uses. get_mixer() is None unless create_app set one, which
        # is the same opt-in the HTTP route reads.
        result = await perform_sfx(self._ma, path, url, mode, get_mixer())
        result["sfx"] = path.stem
        return result

    async def sfx_list(self) -> list[dict[str, Any]]:
        return list_sfx(self._config.sfx_dir)

    async def now(self) -> dict[str, Any]:
        return await now_snapshot(self._ma)

    async def skip(self) -> None:
        await self._ma.next_track()

    async def pause(self) -> None:
        await self._ma.pause()

    async def resume(self) -> None:
        await self._ma.resume()

    async def volume(self, level: int) -> None:
        await self._ma.set_volume(level)

    async def reconnect(self) -> dict[str, Any]:
        return await do_reconnect(self._ma)

    async def close(self) -> None:
        return None


class HttpBackend:
    """Calls a musicbox over HTTP. Used only by the stdio entrypoint.

    Every non-2xx is turned into a ToolFailure carrying musicbox's own `detail`
    string, so the sentence the model sees is the one the server wrote rather
    than a status code.
    """

    def __init__(self, base_url: str, token: str = "") -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    async def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        # Checked here rather than by catching aiohttp's exception for it. On
        # aiohttp 3.14 `MUSICBOX_URL=pi5:8099` raises NonHttpUrlClientError,
        # older releases raise InvalidURL, and both stringify to nothing but
        # the URL itself, so the generic arm below produced "Could not reach
        # musicbox at pi5:8099: pi5:8099/skip" and sent the reader off
        # debugging a network that is fine. Per call and not in __init__ so a
        # bad value is a sentence from a tool, not a stdio server that dies
        # before the client sees it.
        if not self._base.startswith(("http://", "https://")):
            raise ToolFailure(
                f"{self._base!r} is not a usable musicbox URL. MUSICBOX_URL has to "
                "include the scheme, for example http://pi5:8099."
            )
        # Created lazily rather than in __init__: aiohttp binds a session to
        # the running loop, and __init__ runs before the stdio server starts
        # one.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        headers = {"authorization": f"Bearer {self._token}"} if self._token else {}
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method,
                url,
                json=body,
                headers=headers,
                # Longer than musicbox's own command timeout and longer than a
                # long sfx, because /drop blocks for the whole length of the
                # sound plus the snapserver buffer.
                timeout=aiohttp.ClientTimeout(total=320),
            ) as resp:
                text = await resp.text()
                try:
                    payload = json.loads(text) if text else {}
                except ValueError:
                    payload = {}
                if resp.status >= 400:
                    detail = payload.get("detail") or payload.get("error") or text.strip()
                    if resp.status == 401:
                        raise ToolFailure(
                            "musicbox refused the request: bad or missing bearer token. "
                            "Set MUSICBOX_TOKEN for this MCP server to the same value "
                            "the musicbox service is using."
                        )
                    raise ToolFailure(
                        f"musicbox answered {resp.status}: {detail or 'no detail given'}. "
                        "Nothing was played."
                    )
                return payload
        except aiohttp.ClientError as exc:
            raise ToolFailure(
                f"Could not reach musicbox at {self._base}: {exc}. Check that the "
                "service is running and that MUSICBOX_URL points at it."
            ) from exc
        except TimeoutError as exc:
            raise ToolFailure(
                f"musicbox at {self._base} did not answer in time. It may still be "
                "playing the sound; check now_playing before retrying."
            ) from exc

    def _media_body(self, media: str) -> dict[str, str]:
        # Classified here as well as on the server, and not because the server
        # needs the help: it re-classifies whatever arrives in whichever field.
        # This is so the request in the musicbox journal says which of the two
        # things the caller meant, which is the difference between debugging a
        # bad search and debugging a bad URI.
        return {"uri": media} if looks_like_uri(media) else {"query": media}

    async def play(self, media: str) -> dict[str, Any]:
        return await self._request("POST", "/play", self._media_body(media))

    async def enqueue(
        self, media: str, position: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        body = self._media_body(media)
        # Sent only when they are not the defaults, so this proxy keeps working
        # against a musicbox older than enqueue positions: an unknown key in the
        # body is ignored by FastAPI, but an older server would silently drop a
        # position it does not understand, and sending nothing is the same
        # request that server has always answered correctly.
        if position:
            body["position"] = position
        if force:
            body["force"] = True
        return await self._request("POST", "/queue", body)

    async def search(self, query: str, media_type: str, limit: int) -> dict[str, Any]:
        params = urlencode({"q": query, "type": media_type, "limit": limit})
        return await self._request("GET", f"/search?{params}")

    async def get_queue(self, limit: int) -> dict[str, Any]:
        return await self._request("GET", f"/queue?{urlencode({'limit': limit})}")

    async def drop(self, url: str, mode: str) -> dict[str, Any]:
        return await self._request("POST", "/drop", {"url": url, "mode": mode})

    async def sfx(self, name: str, mode: str) -> dict[str, Any]:
        # quote(safe="") and not an f-string of the raw name. sfx names come
        # from list_sfx, which reports the file stem, and an operator dropping
        # `big airhorn #2.mp3` in with Finder is ordinary. Unquoted, the `#`
        # became a URL fragment and this posted to /sfx/big airhorn, which
        # answered 404 "no sfx named 'big airhorn '" while the SAME name played
        # fine through the mounted endpoint. app.sfx_file_url quotes for the
        # same reason; this is the other half of it.
        return await self._request("POST", f"/sfx/{quote(name, safe='')}", {"mode": mode})

    async def sfx_list(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/sfx")).get("sfx") or []

    async def now(self) -> dict[str, Any]:
        return await self._request("GET", "/now")

    async def skip(self) -> None:
        await self._request("POST", "/skip")

    async def pause(self) -> None:
        await self._request("POST", "/pause")

    async def resume(self) -> None:
        await self._request("POST", "/resume")

    async def volume(self, level: int) -> None:
        await self._request("POST", "/volume", {"level": level})

    async def reconnect(self) -> dict[str, Any]:
        return await self._request("POST", "/reconnect")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


# ── phrasing ──────────────────────────────────────────────────────────────────


def _describe_drop(result: dict[str, Any], what: str) -> str:
    used = result.get("mode_used", "over")
    if used == "over":
        # Which path was taken is not a detail: the two SOUND different, and a
        # model that tells someone "the music will duck" when it is about to
        # stop dead has misled them. `path` is absent on older answers and on
        # the /drop route, which is why the default is the announcement one.
        if result.get("path") == "mixer":
            line = f"Played {what} on top of the music. {MIXER_NOTE}"
            if result.get("voices"):
                line += f" {result['voices']} effect voices are playing right now."
            return line
        line = f"Played {what} over the music. {OVER_NOTE}"
        if result.get("fell_back") and result.get("reason"):
            # The mixer was supposed to handle this one and could not. Say so,
            # because the person listening just heard the music stop and will
            # ask why.
            line += f" {result['reason']}."
        return line
    line = f"Cut to {what} and "
    line += "resumed the music where it stopped." if result.get("resumed") else "the music was not playing."
    if result.get("fell_back"):
        line += f" You asked for 'over' and got 'cut' instead: {result.get('reason')}."
    return line


def _label(entry: dict[str, Any]) -> str:
    title = entry.get("title") or "an untitled track"
    artist = entry.get("artist")
    return f"{title} by {artist}" if artist else title


def _short(text: Any, limit: int = 120) -> Any:
    """A caller's own words, bounded, for echoing back in a failure.

    Only ever used on the ERROR path. A tool that quotes a rejected argument
    back verbatim turns a 20 kB argument into 20 kB of the context window that
    produced it, which is the one context a model can least afford to spend
    while it is being told it got something wrong.
    """
    if isinstance(text, str) and len(text) > limit:
        return text[:limit] + f"... ({len(text)} characters)"
    return text


def _wait_phrase(result: dict[str, Any]) -> str:
    """"plays in N songs", when N is actually known.

    plays_after is the number of tracks in front of it counted from the AUDIBLE
    one, which is what somebody standing next to the speaker is asking about. It
    is None whenever the landing slot could not be confirmed, and in that case
    this says nothing rather than guessing a number that a person will then be
    told out loud.
    """
    waiting = result.get("plays_after")
    if not isinstance(waiting, int):
        return ""
    if waiting <= 0:
        return " It is next."
    if waiting == 1:
        return " It plays after the current song."
    return f" It plays in {waiting} songs."


def _describe_media(result: dict[str, Any]) -> str:
    """What play and queue answer with.

    When free text was resolved the sentence names the match AND the words that
    were searched for. Both halves matter: the person who asked can only correct
    a wrong guess if they hear what the guess was, and the model can only
    apologize usefully if it knows which of its words led there.
    """
    action = result.get("action")
    resolved = result.get("resolved")
    query = result.get("query")
    what = _label(resolved) if resolved else (result.get("media") or "it")
    matched = f", matched from {query!r}" if resolved and query else ""

    if action == "already_queued":
        # ok: true, nothing was enqueued, and the sentence has to make both of
        # those obvious in one read. A model that reads this as a failure will
        # retry with force and put the song in twice, which is the thing the
        # check exists to stop.
        if result.get("already_playing"):
            # The copy is the track the box is ON, not one waiting. This used to
            # come out as "already in the queue at position 5. It is next.",
            # which told the room a song was coming that was in fact seconds
            # from over. Different fact, different sentence.
            return (
                f"{what} is playing right now, at queue position "
                f"{result.get('queue_position')}. Nothing was added: it is not waiting "
                "in the queue, it is already on. Tell the person it is the song they "
                "are hearing. If they want it AGAIN after this one, call queue again "
                "with force true."
            )
        line = f"{what} is already in the queue at position {result.get('queue_position')}."
        line += _wait_phrase(result)
        check = result.get("duplicate_check") or {}
        line += (
            " Nothing was added. Tell the person it is already coming. If they want it "
            "again anyway, call queue again with force true."
        )
        if check.get("exhaustive") is False:
            line += (
                f" (Only the next {check.get('checked')} of {check.get('upcoming')} "
                "upcoming tracks were checked.)"
            )
        return line

    if action == "playing":
        tail = (
            " The previous queue was cleared."
            if result.get("queue_cleared")
            else " The rest of the queue is untouched."
        )
        line = f"Playing {what} now{matched}.{tail}"
        if resolved:
            line += " If that is the wrong track, say so and search for another."
        return line

    position = result.get("position") or "end"
    if position == "end":
        placed = " It plays after everything already in the queue."
    elif position == "fair":
        placed = " It plays after the other requests and before the background playlist."
    else:
        placed = ""
    line = f"Queued {what}{matched}.{placed}{_wait_phrase(result)}"
    note = result.get("note")
    if note:
        line += f" {note}"
    if resolved:
        line += " If that is the wrong track, say so and search for another."
    return line


def _describe_search(found: dict[str, Any]) -> dict[str, Any]:
    """A search result list small enough to sit in a context window.

    Album art, provider mapping ids, external ids and per-provider audio formats
    are all dropped. The uri is kept because it is the one field the model has
    to hand back to play or queue to pick a specific version.
    """
    return {
        "query": found.get("query"),
        "type": found.get("type"),
        "count": found.get("count", 0),
        "results": [
            {
                "title": item.get("title"),
                "artist": item.get("artist"),
                "album": item.get("album"),
                "uri": item.get("uri"),
                "duration_seconds": item.get("duration"),
                "playable": item.get("playable"),
            }
            for item in found.get("results") or []
        ],
    }


def _describe_queue(snapshot: dict[str, Any]) -> dict[str, Any]:
    items = snapshot.get("items") or []
    return {
        "state": snapshot.get("state") or "unknown",
        # total is the whole queue including what has already played, upcoming
        # is what is left, and showing is how many are in this answer. They
        # differ constantly and conflating them is how somebody gets told their
        # song is next when it is ninetieth.
        "total": snapshot.get("count", 0),
        "upcoming": snapshot.get("upcoming", 0),
        "showing": len(items),
        "now_playing_index": snapshot.get("index"),
        "items": [
            {
                "position": item.get("position"),
                "title": item.get("title"),
                "artist": item.get("artist"),
                "duration_seconds": item.get("duration"),
                "uri": item.get("uri"),
            }
            for item in items
        ],
    }


def _describe_now(snapshot: dict[str, Any]) -> dict[str, Any]:
    track = snapshot.get("track") or {}
    position = snapshot.get("position")
    return {
        "state": snapshot.get("state") or "unknown",
        "title": track.get("title"),
        "artist": track.get("artist"),
        "position_seconds": round(position) if isinstance(position, (int, float)) else None,
        "duration_seconds": track.get("duration"),
        "volume": snapshot.get("volume"),
        "queue_length": snapshot.get("queue_length", 0),
        "player": snapshot.get("player_name") or snapshot.get("player"),
    }


def _describe_sfx(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        # Names only. The model calls sfx(name), so the file sizes and URLs the
        # HTTP listing carries are noise in a context window.
        "names": [item["name"] for item in items],
    }


# ── tool registration ─────────────────────────────────────────────────────────


def register_tools(mcp: FastMCP, backend: Any) -> None:
    """Define every tool, once, against either backend.

    The docstrings are the tool descriptions the model actually reads, so they
    are written for someone who has never seen this repo.
    """

    # play and queue keep the single `uri_or_url` parameter they have always
    # had, widened rather than joined by a second one. A model choosing between
    # `uri_or_url` and a `query` sibling has to guess which of the two a string
    # belongs in, and it will guess wrong on a share URL. One parameter that
    # accepts everything cannot be filled in wrong.
    @mcp.tool()
    async def play(uri_or_url: str) -> str:
        """Start playing something right now, clearing whatever was queued.

        Takes any of three things:
          * plain text, for example "Above and Beyond Sun in Your Eyes", or
            whatever the person actually said. It is searched across every music
            provider on the box and the first playable match is played.
          * a provider URI, for example spotify://track/xyz, or one copied from
            a search result.
          * an http(s) URL to an audio file.

        Anything without a scheme is treated as text to search for, so you can
        pass a request through verbatim. The answer names the track it landed on
        and the words it searched for. Read it back to the person who asked:
        search picks one match out of several and it can pick the wrong one, and
        they are the only one who knows.

        play is the EXCEPTION, not the normal path. It interrupts: the current
        song stops and everything else that was waiting is thrown away. Use
        queue for a song somebody asked for, with position "fair" so it plays
        soon without wiping the evening's music. Reach for play only when the
        whole queue is meant to go, for example starting a set from scratch.
        """
        try:
            return _describe_media(await backend.play(_media(uri_or_url)))
        except Exception as exc:  # noqa: BLE001 - a tool never raises
            return explain(exc)

    @mcp.tool()
    async def queue(
        uri_or_url: str,
        position: str | None = None,
        force: bool | None = False,
    ) -> str:
        """Add a song to the queue. This is the normal way to honor a request.

        Someone asks for a song, you call queue. Use play only when something has
        to start this second, because play cuts off whatever is on and clears
        everything else that was waiting. At an event that is almost never what
        anybody wants.

        uri_or_url takes exactly what play takes: plain text such as "toca Baile
        de Favela" or just "Baile de Favela", a provider URI such as
        spotify://track/xyz, or an http(s) URL to an audio file. Text is searched
        and the first playable match is queued. The answer names the track and
        the words it was matched from, so read it back to whoever asked.

        position decides where it lands, and it matters more than it sounds. The
        queue often holds a long background playlist, so the default sends a
        request behind all of it and it may not play for hours:

          "fair" (use this for a person's request) puts it after the other
              requests already waiting and in front of the background playlist.
              Ten people who ask get played in the order they asked.
          "next" plays it immediately after the current song, jumping ahead of
              every request already waiting. Use it when somebody has to be
              served right now, and know that it pushes back everyone who asked
              earlier. If the current song is nearly over it lands after the
              following one instead.
          "end" (the default) adds it after everything already queued. Right for
              filling a quiet box, wrong for a request when a long playlist is
              loaded: read `plays in N songs` in the answer before promising
              anybody anything.
          "now" starts it immediately, cutting off the current song, and leaves
              the rest of the queue alone.
          "replace" throws the whole queue away and starts this instead.

        If the same track is already waiting further down the queue, nothing is
        added: the answer says it is already queued and how long the wait is, so
        you can tell the person "essa ja esta na fila, toca em 3 musicas". If it
        is the song playing at that moment the answer says that instead. Only
        what is still to play counts, so a song that already played is not a
        duplicate. To queue it a second time on purpose, call again with
        force true.

        That check reads the next 50 upcoming tracks and not the whole queue, so
        on a long queue a copy sitting further down than that is not found and
        the song is queued again. "Queued" therefore means "not already coming in
        the next 50", not "definitely not in the queue". When a duplicate IS
        found the answer says how much was checked.
        """
        try:
            wanted = _media(uri_or_url)
            where = _position(position)
            return _describe_media(
                await backend.enqueue(wanted, where, bool(force))
            )
        except Exception as exc:  # noqa: BLE001
            return explain(exc)

    # `limit: int | float | str | None` on this tool and on get_queue, for the
    # same reason set_volume takes a union: pydantic validates the arguments
    # BEFORE the body runs, so a plain `int` never reached the careful coercion
    # in the service layer. limit=5.5 and limit="all" both came back as "1
    # validation error for searchArguments", which is the pydantic dump this
    # whole module exists to keep away from the model, and it is the one shape
    # of failure a tool here is not allowed to have. Widened, both reach
    # coerce_limit: 5.5 rounds, "all" gets a sentence.
    @mcp.tool()
    async def search(
        query: str,
        type: str | None = "track",
        limit: int | float | str | None = None,
    ) -> dict[str, Any]:
        """Find something to play, by name, across every provider on the box.

        query is free text: a song title, an artist, or both. Formatting it as
        "Artist - Title" measurably improves the ordering, because Music
        Assistant scores an exact title match and hoists it to the top.

        type is one of track, album, artist or playlist. Default track.
        limit defaults to 5 and is capped at 10. Asking for more is genuinely
        slower: results come from Spotify ten at a time behind a rate limiter,
        so 5 takes about a second and 11 takes four.

        Returns a list with a uri on each entry. Hand that uri to play or queue
        to pick one exactly. You do NOT have to search first: play and queue
        take plain text themselves and do this same search. Search when you want
        to choose between versions, show someone the options, or check whether a
        song exists at all.

        `playable` false means Music Assistant knows the track but cannot play
        it here, usually a region or account restriction. Do not queue those.
        An empty list means nothing matched, or that a provider is down; the two
        are indistinguishable from here.
        """
        try:
            wanted = (query or "").strip()
            if not wanted:
                raise ToolFailure("Give something to search for. Nothing was searched.")
            found = await backend.search(
                wanted,
                (type or "track").strip().lower(),
                SEARCH_LIMIT_DEFAULT if limit is None else limit,
            )
            return _describe_search(found)
        except Exception as exc:  # noqa: BLE001
            return {
                "query": _short(query),
                "count": 0,
                "results": [],
                # Not the default tail. A search that fails has not played
                # anything and has not failed to play anything either, and
                # telling a model "Nothing was played" here sends it to fix the
                # wrong thing.
                "error": explain(exc, "Nothing was searched."),
            }

    @mcp.tool()
    async def get_queue(limit: int | float | str | None = None) -> dict[str, Any]:
        """What is coming up, current track first.

        `total` is the whole queue length, `upcoming` is how many are still to
        play, and `showing` is how many are in this answer. They are three
        different numbers: read `upcoming` before telling someone how long the
        wait is, not the length of the list.

        `position` on an item is its real index in the whole queue, counting
        from 0. The first item returned is the current one, so its position
        equals `now_playing_index`, and how long someone waits is their position
        minus that. Do not expect this number anywhere else: Music Assistant's
        own per item sort index is stamped when a track is added and never
        renumbered, so it disagrees with the real order.

        `uri` is null on an item that has none, which is what a raw stream
        queued by URL looks like. A null there means you cannot re-queue that
        item by uri, not that something is broken. Search for it by name.
        """
        try:
            return _describe_queue(
                await backend.get_queue(QUEUE_PAGE_DEFAULT if limit is None else limit)
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "total": 0,
                "upcoming": 0,
                "showing": 0,
                "items": [],
                "error": explain(exc, "The queue could not be read."),
            }

    # `mode: str | None` and not `mode: str`, here and on sfx. The argument
    # schema is enforced by pydantic BEFORE the function body runs, so with a
    # plain `str` a client that spells the default as an explicit null (which
    # models do routinely) got "1 validation error for dropArguments" instead
    # of a mode of "over". _mode() has always handled None; the annotation was
    # what stopped it ever seeing one.
    @mcp.tool()
    async def drop(url: str, mode: str | None = "over") -> str:
        """Play a one shot sound from an http(s) URL, on top of the music.

        mode "over" hands it to Music Assistant's announcement path. mode "cut"
        pauses the music, plays the sound, then resumes where it stopped.
        Music Assistant has no ducking: "over" silences the music for the
        length of the sound rather than lowering it under the sound. That is
        true for drop even on a box running musicbox-mixer, because the mixer
        plays files that are already on the machine and this tool takes a URL.
        For a real ducked overlay, put the file in the sfx directory and use
        sfx.

        This is for short sounds, not for music. To play a track or a whole
        file, use play. For a sound already loaded on the box, use sfx.
        """
        try:
            chosen = _mode(mode)
            target = _http_url(url)
            return _describe_drop(await backend.drop(target, chosen), target)
        except Exception as exc:  # noqa: BLE001
            return explain(exc)

    @mcp.tool()
    async def sfx(name: str, mode: str | None = "over") -> str:
        """Play one of the sound effects preloaded on the box.

        Call list_sfx for the names. mode works exactly as it does for drop:
        "over" plays it on top of the music, "cut" pauses the music, plays it,
        and resumes where it stopped.

        On a box running musicbox-mixer, "over" really is on top: the music
        ducks under the effect and comes back, and firing the same effect again
        while it is still playing adds another copy instead of queueing, so
        rapid presses stutter rather than waiting in line. Without the mixer,
        "over" goes through Music Assistant's announcement path, which silences
        the music for the length of the effect and plays repeats strictly one
        after another. The answer says which one happened.

        This only plays names that are already on the box. For a sound at an
        http(s) URL, use drop.
        """
        try:
            chosen = _mode(mode)
            wanted = (name or "").strip()
            if not wanted:
                raise ToolFailure("Give the name of a sound effect. Call list_sfx for the names.")
            result = await backend.sfx(wanted, chosen)
            return _describe_drop(result, f"the {result.get('sfx', wanted)!r} sound effect")
        except Exception as exc:  # noqa: BLE001
            return explain(exc)

    @mcp.tool()
    async def list_sfx() -> dict[str, Any]:
        """List the sound effects preloaded on the box, by name.

        These are the names the sfx tool takes. They are files an operator put
        on the machine, so the list changes without warning: read it rather
        than remembering it.
        """
        try:
            return _describe_sfx(await backend.sfx_list())
        except Exception as exc:  # noqa: BLE001
            return {"count": 0, "names": [], "error": explain(exc)}

    @mcp.tool()
    async def now_playing() -> dict[str, Any]:
        """What is playing right now: track, position, volume and queue length.

        Read this before assuming a previous call took effect. position_seconds
        and duration_seconds are seconds, volume is 0 to 100.
        """
        try:
            return _describe_now(await backend.now())
        except Exception as exc:  # noqa: BLE001
            return {"state": "unknown", "error": explain(exc)}

    @mcp.tool()
    async def skip() -> str:
        """Skip to the next track in the queue."""
        try:
            await backend.skip()
            return "Skipped to the next track."
        except Exception as exc:  # noqa: BLE001
            return explain(exc)

    @mcp.tool()
    async def pause() -> str:
        """Pause the music. Use resume to carry on from the same place."""
        try:
            await backend.pause()
            return "Paused. Call resume to carry on from the same place."
        except Exception as exc:  # noqa: BLE001
            return explain(exc)

    @mcp.tool()
    async def resume() -> str:
        """Carry on playing from wherever pause stopped."""
        try:
            await backend.resume()
            return "Resumed."
        except Exception as exc:  # noqa: BLE001
            return explain(exc)

    # The union is not decoration. Pydantic validates arguments before the body
    # runs, so with a plain `level: int` the coercion below was unreachable and
    # set_volume("loud") or set_volume(55.7) came back as "1 validation error
    # for set_volumeArguments", which is the pydantic dump this whole module
    # exists to keep away from the model. Widened, every one of those reaches
    # the body and gets a sentence. int stays first so the schema still leads
    # with the type a client should send.
    @mcp.tool()
    async def set_volume(level: int | float | str) -> str:
        """Set the speaker volume, 0 to 100. Send a whole number."""
        try:
            try:
                # round(float(...)) and not int(...): int("55.7") is a
                # ValueError, and refusing a perfectly clear request for 55.7
                # would be pedantry rather than safety.
                value = round(float(level))
            # OverflowError is in the list for "inf": float() takes it happily
            # and round() then refuses it, and that must read as a bad argument
            # rather than as the "unexpected" arm of explain(), which tells the
            # model it found a musicbox bug.
            except (TypeError, ValueError, OverflowError):
                raise ToolFailure(
                    f"level must be a number from 0 to 100, got {level!r}. "
                    "The volume was not changed."
                ) from None
            if not 0 <= value <= 100:
                raise ToolFailure(f"level must be between 0 and 100, got {value}. The volume was not changed.")
            await backend.volume(value)
            return f"Volume set to {value}."
        except Exception as exc:  # noqa: BLE001
            return explain(exc)

    @mcp.tool()
    async def reconnect() -> str:
        """Re-establish the connection to Music Assistant.

        Use this when other tools report that Music Assistant is unreachable.
        It tears the websocket down and rebuilds it immediately instead of
        waiting out the reconnect backoff, and it does not interrupt anything
        that is already playing on the speaker.
        """
        try:
            result = await backend.reconnect()
            if result.get("ok"):
                player = result.get("player_name") or result.get("player") or "no player"
                return f"Reconnected to Music Assistant. Player: {player}."
            return (
                "Tried to reconnect and Music Assistant is still not answering. "
                f"Last error: {result.get('last_error') or 'none reported'}."
            )
        except Exception as exc:  # noqa: BLE001
            return explain(exc)

    # The same listing as a resource, so a client can see what sounds exist
    # without spending a tool call on it. Text and not JSON: a resource is
    # dropped into a context window as is, and a comma separated line reads
    # better there than a serialized object.
    @mcp.resource(
        "musicbox://sfx",
        name="sound effects",
        description="Names of the sound effects preloaded on the music box.",
        mime_type="text/plain",
    )
    async def sfx_resource() -> str:
        try:
            names = _describe_sfx(await backend.sfx_list())["names"]
        except Exception as exc:  # noqa: BLE001
            return f"The sound effect list could not be read. {explain(exc)}"
        if not names:
            return "No sound effects are loaded on the box."
        return "Sound effects available to the sfx tool: " + ", ".join(names)


def build_mcp(backend: Any, **settings: Any) -> FastMCP:
    mcp = FastMCP("musicbox", instructions=INSTRUCTIONS, **settings)
    # FastMCP has no `version` argument (checked against the signature of
    # FastMCP.__init__ in mcp 1.29.0), and the low level server falls back to
    # `pkg_version("mcp")` when its own version is unset. The result is that the
    # initialize handshake answered
    #   "serverInfo": {"name": "musicbox", "version": "1.29.0"}
    # which is the SDK's version reported as ours. Anyone debugging a mismatch
    # between the box and a client would chase the wrong number. Setting it on
    # the wrapped server is the only way in, so it is guarded: if a future SDK
    # renames the attribute we go back to the cosmetic wart rather than
    # crash-looping the unit over a version string.
    server = getattr(mcp, "_mcp_server", None)
    if server is not None:
        server.version = __version__
    register_tools(mcp, backend)
    return mcp


# ── mounted on the FastAPI app ────────────────────────────────────────────────


class MountedMCP:
    """The /mcp endpoint, as a raw ASGI app that app.py hangs off one route.

    It is an object with __call__ rather than a function on purpose: starlette
    decides between "request handler" and "raw ASGI app" with
    inspect.isfunction, so a plain async function here would be wrapped and
    handed a Request, and the streamable HTTP protocol needs the raw triple.
    """

    def __init__(self, config: Config, ma: MAClient, lane: RequestLane | None = None) -> None:
        self.mcp = build_mcp(
            LocalBackend(config, ma, lane),
            # Stateless: no session id to track, nothing to expire, and a
            # client that drops off the tailnet and comes back does not have to
            # be told its session is gone. We send no server initiated
            # notifications, which is the only thing statelessness costs.
            stateless_http=True,
            # Answer with a single application/json body rather than an SSE
            # stream. Every MCP client accepts both, and this one can be poked
            # with curl when something is wrong at an event, which the SSE form
            # cannot.
            json_response=True,
            # THE trap in this file, and it is silent. The SDK's default is
            # TransportSecuritySettings(enable_dns_rebinding_protection=True,
            # allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]), so out
            # of the box a request whose Host header is anything else is
            # answered "421 Misdirected Request" before any tool is reached.
            # Every way we expect this to be used has a non-loopback Host:
            # http://pi5:8099/mcp, the MagicDNS name, the raw 100.x tailnet
            # address. Verified by a test in tests/test_mcp.py, which failed
            # with a 421 on Host "testserver" before this was set.
            #
            # The protection it turns off guards a BROWSER on a machine that
            # can reach this port, tricked by DNS rebinding into POSTing here
            # from an attacker's page. Nothing here is browser-facing, the port
            # is tailnet-only, and the allowlist would have to enumerate every
            # name a tailnet client might dial. Disabling it is a far smaller
            # hole than the one this endpoint already has on purpose: /mcp
            # takes no credentials at all. See the comment on the route in
            # app.create_app.
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
        # streamable_http_app() is called for its side effect only: it is what
        # constructs the session manager, and `mcp.session_manager` raises
        # before it has been called. The Starlette app it returns is discarded
        # because its route path moved between SDK versions (a Mount in 1.15, a
        # Route in 1.26), and where /mcp lives is our decision, not the SDK's.
        self.mcp.streamable_http_app()
        self.session_manager = self.mcp.session_manager

    def lifespan(self):
        """Enter this for the life of the app. The session manager owns a task
        group, and every request before it is entered fails with "Task group is
        not initialized"."""
        return self.session_manager.run()

    async def __call__(self, scope, receive, send) -> None:
        await self.session_manager.handle_request(scope, receive, send)


# ── stdio entrypoint ──────────────────────────────────────────────────────────


def closing_backend(backend: Any):
    """A FastMCP lifespan that closes the backend when the server stops.

    Closing the aiohttp session on the way out is not tidiness. Without it the
    stdio process exits with the session still open and aiohttp writes

        Unclosed client session
        Unclosed connector

    to stderr from a garbage collector callback. Stderr on a stdio server is
    the client's log file, so those two lines land in the MCP client's log and
    read like musicbox crashed on exit. Seen on every run of the console script
    before this existed.

    It has to be a lifespan rather than a try/finally around mcp.run(): run()
    owns the event loop and closes it before returning, and an aiohttp session
    bound to a loop that is already closed cannot be closed at all.
    """

    @contextlib.asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            # Suppressed because this runs while the process is already on its
            # way out. A failure to close a socket must not be the last thing
            # the client sees.
            with contextlib.suppress(Exception):
                await backend.close()

    return lifespan


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `musicbox-mcp` console script.

    A stdio MCP server that proxies to a musicbox over HTTP. For a client on
    the tailnet the mounted /mcp endpoint is better (one process, one state, no
    per-client subprocess); this exists for clients that only speak stdio.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="musicbox-mcp",
        description="MCP server for musicbox, over stdio. Proxies to a musicbox over HTTP.",
    )
    parser.add_argument("--version", action="version", version=f"musicbox {__version__}")
    parser.add_argument(
        "--url",
        default=None,
        help="base URL of the musicbox to control, overrides MUSICBOX_URL "
        "(default http://127.0.0.1:8099)",
    )
    args = parser.parse_args(argv)

    url = args.url or os.environ.get("MUSICBOX_URL", "").strip() or "http://127.0.0.1:8099"
    # The same MUSICBOX_TOKEN the server reads, and the same file fallback, so
    # a client on the box itself can point at the token file rather than
    # putting the secret in an MCP client config. Only the HTTP proxy needs it:
    # the mounted /mcp endpoint takes no credentials at all.
    token = os.environ.get("MUSICBOX_TOKEN", "").strip()
    if not token:
        token_file = os.environ.get("MUSICBOX_TOKEN_FILE", "").strip()
        if token_file:
            with contextlib.suppress(OSError):
                token = Path(token_file).read_text(encoding="utf-8").strip()

    backend = HttpBackend(url, token)
    mcp = build_mcp(backend, lifespan=closing_backend(backend))
    # run() is blocking and owns the loop. Nothing is logged to stdout here:
    # stdout IS the protocol channel, and one stray print corrupts the stream.
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
