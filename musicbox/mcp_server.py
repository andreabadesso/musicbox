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
from urllib.parse import quote

import aiohttp
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .app import (
    OVER_NOTE,
    do_reconnect,
    list_sfx,
    now_snapshot,
    perform_drop,
    resolve_sfx,
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
Controls the music box: one Bluetooth speaker driven by Music Assistant.

play and queue take a provider URI (spotify://track/xyz) or a plain http(s) URL
to an audio file. play replaces whatever is queued and starts now; queue adds
to the end.

drop and sfx fire a one shot sound. mode "over" hands it to Music Assistant's
announcement path; mode "cut" pauses the music, plays the sound, and resumes
where it stopped. Music Assistant has no ducking, so "over" silences the music
for the length of the sound rather than lowering it.

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


def explain(exc: Exception) -> str:
    """Every failure a tool can produce, as something a model can act on.

    Each sentence says what did NOT happen and what to try, because the model
    reading it cannot see the journal and is usually mid-event.
    """
    if isinstance(exc, ToolFailure):
        return str(exc)
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
            f"(error code {exc.code}). Nothing was played."
        )
    # Deliberately last and deliberately loud about the type. If this arm is
    # ever hit it is a musicbox bug, and the model naming the exception class
    # in its report is what makes it findable afterwards.
    return f"musicbox hit an unexpected {type(exc).__name__}: {exc}. Nothing was played."


def _mode(raw: str | None) -> str:
    mode = (raw or "over").strip().lower()
    if mode not in MODES:
        raise ToolFailure(f"mode must be 'over' or 'cut', not {raw!r}. Nothing was played.")
    return mode


def _media(raw: str) -> str:
    media = (raw or "").strip()
    if not media:
        raise ToolFailure("Give a provider URI or an http(s) URL. Nothing was played.")
    return media


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

    def __init__(self, config: Config, ma: MAClient) -> None:
        self._config = config
        self._ma = ma

    async def play(self, media: str) -> None:
        # "replace", not "play": replace clears the queue, which is what "play
        # now, replacing the queue" means. Same call POST /play makes.
        await self._ma.play_media(media, "replace")

    async def enqueue(self, media: str) -> None:
        await self._ma.play_media(media, "add")

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
        result = await perform_drop(self._ma, url, mode)
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

    async def play(self, media: str) -> None:
        await self._request("POST", "/play", {"uri": media})

    async def enqueue(self, media: str) -> None:
        await self._request("POST", "/queue", {"uri": media})

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
        return f"Played {what} over the music. {OVER_NOTE}"
    line = f"Cut to {what} and "
    line += "resumed the music where it stopped." if result.get("resumed") else "the music was not playing."
    if result.get("fell_back"):
        line += f" You asked for 'over' and got 'cut' instead: {result.get('reason')}."
    return line


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

    @mcp.tool()
    async def play(uri_or_url: str) -> str:
        """Start playing something right now, clearing whatever was queued.

        Takes a provider URI such as spotify://track/xyz, or a plain http(s)
        URL to an audio file. Use queue instead to add without interrupting.
        """
        try:
            media = _media(uri_or_url)
            await backend.play(media)
            return f"Playing {media} now. The previous queue was cleared."
        except Exception as exc:  # noqa: BLE001 - a tool never raises
            return explain(exc)

    @mcp.tool()
    async def queue(uri_or_url: str) -> str:
        """Add something to the end of the queue without interrupting the music.

        Takes a provider URI such as spotify://track/xyz, or a plain http(s)
        URL to an audio file. Use play instead to start it immediately.
        """
        try:
            media = _media(uri_or_url)
            await backend.enqueue(media)
            return f"Queued {media}. It plays after what is already in the queue."
        except Exception as exc:  # noqa: BLE001
            return explain(exc)

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
        length of the sound rather than lowering it under the sound.

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
        "over" plays it through the announcement path, "cut" pauses the music,
        plays it, and resumes where it stopped.

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

    def __init__(self, config: Config, ma: MAClient) -> None:
        self.mcp = build_mcp(
            LocalBackend(config, ma),
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
