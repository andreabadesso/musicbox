"""Persistent websocket client for the Music Assistant API.

Why hand rolled instead of the official python client: this process has to
survive a whole event unattended, so the two behaviours that matter most are
reconnect and never blocking a request handler on the socket. Both are easier
to own outright than to wrap, and the protocol is one JSON object per text
frame with a caller chosen message_id, which is a small thing to implement.

Everything MA specific lives in this module. Every command name is a constant
in COMMANDS and every call goes through a named adapter method below, so a name
that turns out to be wrong on the running image is a one line fix here and not
a grep across the app.

Protocol facts this depends on, all verified against MA 2.9.13:

  * The server sends one unsolicited ServerInfoMessage right after the
    handshake, before auth. It has no message_id and no event key, so it is
    identified by the presence of server_version.
  * Every command except the auth ones requires a token. auth is a special
    command intercepted before dispatch.
  * A successful auth subscribes you to the FULL event firehose with no way to
    filter or unsubscribe, and the server drops any client whose outgoing queue
    reaches 512 messages. So there is always exactly one task draining the
    socket, and request handlers only ever await a future.
  * Replies echo the message_id. Streaming commands send several messages with
    the same message_id and partial=true before a final partial=false, so a
    future is only resolved on the final one.
  * Errors are {"message_id", "error_code", "details"}. The human text is in
    details. There is no "error" or "message" key.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import random
import time
from typing import Any, Callable

import aiohttp

from . import logs
from .config import Config

# ── MA command names ──────────────────────────────────────────────────────────
# Correct as of server 2.9.13. Confirm against GET /api-docs/commands.json on
# the live box if anything here starts returning "Invalid Command".
COMMANDS = {
    "players_all": "players/all",
    "player_get": "players/get",
    "player_get_by_name": "players/get_by_name",
    "player_volume_set": "players/cmd/volume_set",
    "player_play_announcement": "players/cmd/play_announcement",
    "queue_get_active": "player_queues/get_active_queue",
    "queue_play_media": "player_queues/play_media",
    "queue_pause": "player_queues/pause",
    "queue_resume": "player_queues/resume",
    "queue_next": "player_queues/next",
    "queue_play_index": "player_queues/play_index",
}

# QueueOption values, exactly as the server enum spells them. An unknown value
# does NOT error: QueueOption._missing_ returns UNKNOWN and play_media becomes a
# silent no-op that still answers 200. So the value is validated before it is
# sent, never after.
QUEUE_OPTIONS = frozenset({"play", "replace", "next", "replace_next", "add"})

# Error codes from music_assistant_models.errors that the app reacts to.
ERR_MEDIA_NOT_FOUND = 2
ERR_QUEUE_EMPTY = 8
ERR_PLAYER_UNAVAILABLE = 10
ERR_PLAYER_COMMAND_FAILED = 11
ERR_INVALID_COMMAND = 12
ERR_UNPLAYABLE_MEDIA = 13
ERR_AUTH_REQUIRED = 20
ERR_AUTH_FAILED = 21
ERR_INSUFFICIENT_PERMISSIONS = 22
ERR_INVALID_TOKEN = 23

AUTH_ERRORS = frozenset({ERR_AUTH_REQUIRED, ERR_AUTH_FAILED, ERR_INVALID_TOKEN})

# Feature name the Snapcast provider advertises when it can do announcements.
FEATURE_ANNOUNCEMENT = "play_announcement"


class MAError(Exception):
    """An error the MA server reported for a command we sent."""

    def __init__(self, code: int, details: str, command: str = "") -> None:
        super().__init__(f"{command or 'command'} failed: {code} {details}")
        self.code = code
        self.details = details
        self.command = command


class MANotConnected(Exception):
    """We are not currently authenticated to MA. Callers turn this into a 503."""


class MATimeout(Exception):
    """MA accepted the command and never answered within the timeout."""


def ws_url_for(ma_url: str) -> str:
    """http://host:8095 -> ws://host:8095/ws"""
    base = ma_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base + "/ws"


class MAClient:
    def __init__(
        self,
        config: Config,
        session_factory: Callable[[], aiohttp.ClientSession] | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory or (lambda: aiohttp.ClientSession())
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._supervisor: asyncio.Task | None = None
        self._closing = False
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._partials: dict[str, list] = {}
        # Per-connection, unlike server_info, which is kept across reconnects so
        # /health can still name the MA version while we are down. Without this
        # flag the second connection would see a truthy server_info from the
        # first and skip the wait entirely.
        self._info_seen = False

        self.connected = False
        # Tracked separately from `connected` because the two fail apart: the
        # socket can be perfectly healthy while every command comes back with
        # error_code 20 because the token is missing or expired, and "connected
        # but useless" is the single most confusing state to debug blind.
        self.authenticated = False
        self.server_info: dict[str, Any] = {}
        self.last_error: str = "not started"
        self.connected_since: float | None = None
        self.connect_attempts = 0
        self.events_seen = 0

        self.player_id: str | None = None
        self.player_name: str | None = None
        self.player_features: list[str] = []
        self.player_error: str | None = None
        self._player_lookup_at = 0.0
        self._player_lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._closing = False
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(self._supervise(), name="ma-supervisor")

    async def stop(self) -> None:
        self._closing = True
        task, self._supervisor = self._supervisor, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._teardown("shutdown")
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def reconnect(self) -> None:
        """Tear the connection down and rebuild it, right now.

        The supervisor task is replaced rather than merely nudged. Closing the
        socket alone looks tidier but is wrong in the exact case POST /reconnect
        exists for: when MA has been unreachable for a while the supervisor is
        asleep in a backoff of up to backoff_max seconds, holding no socket at
        all, so closing "the socket" is a no-op and the operator's reconnect
        does nothing for another half minute. Cancelling the task and starting a
        fresh one guarantees an attempt within milliseconds and resets the
        backoff to its initial value.
        """
        self._closing = True
        task, self._supervisor = self._supervisor, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        ws = self._ws
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()
        await self._teardown("reconnect requested")
        self.connect_attempts = 0
        # start() clears _closing and spawns the replacement supervisor.
        await self.start()

    async def wait_connected(self, timeout: float) -> bool:
        """Best effort wait, used by POST /reconnect so it can report the truth."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connected:
                return True
            await asyncio.sleep(0.05)
        return self.connected

    # ── connection supervision ────────────────────────────────────────────────

    async def _supervise(self) -> None:
        backoff = self._config.backoff_initial
        while not self._closing:
            self.connect_attempts += 1
            started = time.monotonic()
            try:
                await self._run_connection()
                self.last_error = "connection closed by server"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the supervisor must not die
                self.last_error = f"{type(exc).__name__}: {exc}"
                logs.warn(
                    "ma_connect_failed",
                    attempt=self.connect_attempts,
                    error=self.last_error,
                )
            finally:
                await self._teardown(self.last_error)

            if self._closing:
                break

            # A connection that survived a while is treated as healthy, so a
            # single blip does not leave us at a 30 second backoff for the rest
            # of the event.
            if time.monotonic() - started > 60:
                backoff = self._config.backoff_initial

            delay = min(backoff, self._config.backoff_max)
            # Jitter so that musicbox and any other client do not stampede MA
            # in lockstep when the container restarts.
            delay += random.uniform(0, delay * 0.25)
            logs.log("ma_reconnect_scheduled", delay_s=delay, error=self.last_error)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, self._config.backoff_max)

    async def _run_connection(self) -> None:
        if self._session is None or self._session.closed:
            self._session = self._session_factory()
        url = ws_url_for(self._config.ma_url)
        # No heartbeat argument on purpose. The server opens the socket with
        # heartbeat=25 and aiohttp answers its pings automatically, so adding a
        # client side heartbeat only doubles the keepalive traffic. No timeout
        # argument either: its accepted type changed across aiohttp releases
        # (float, then ClientWSTimeout) and the default is fine here.
        async with self._session.ws_connect(url) as ws:
            self._ws = ws
            self._info_seen = False
            reader = asyncio.create_task(self._read_loop(ws), name="ma-reader")
            try:
                # last_error is deliberately NOT cleared here. It is the field
                # an operator reads out of /health while the box is failing, and
                # clearing it at the top of every attempt means that during a
                # reconnect loop it is empty for most of the time and only
                # briefly holds the reason. _handshake clears it at the exact
                # point the connection is known good instead.
                await self._handshake()
                self.connected = True
                self.connected_since = time.time()
                logs.log(
                    "ma_connected",
                    url=url,
                    server_version=self.server_info.get("server_version"),
                    schema=self.server_info.get("schema_version"),
                    player=self.player_id,
                    player_name=self.player_name,
                    player_error=self.player_error,
                )
                await reader
            finally:
                self.connected = False
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader

    async def _teardown(self, reason: str) -> None:
        self.connected = False
        self.connected_since = None
        self._ws = None
        pending, self._pending = self._pending, {}
        self._partials.clear()
        for future in pending.values():
            if not future.done():
                future.set_exception(MANotConnected(reason or "connection lost"))

    async def _handshake(self) -> None:
        """Wait for the server info push, authenticate, then resolve the player.

        Player resolution failing is NOT a handshake failure. A misnamed or
        temporarily missing player must leave the socket up so that /health can
        report exactly that, instead of hiding it behind a reconnect loop.
        """
        await self._await_server_info(timeout=5.0)
        if self._config.ma_token:
            result = await self._call(
                "auth",
                {"token": self._config.ma_token},
                timeout=10.0,
                require_connected=False,
            )
            user = (result or {}).get("user", {})
            self.authenticated = True
            # Cleared here and only here: the socket is up and the token was
            # accepted, so whatever went wrong last time is genuinely history.
            # refresh_player below can put a new reason straight back in.
            self.last_error = ""
            logs.log("ma_authenticated", user=user.get("username") or user.get("name"))
        else:
            # Not fatal on its own: an unauthenticated MA (or a future version
            # without auth) still answers commands. If it does require auth, the
            # first real command comes back with error_code 20 and /health shows
            # it, which is a far clearer signal than refusing to connect.
            # Optimistic here, flipped by the first auth error we actually see.
            self.authenticated = True
            self.last_error = ""
            logs.warn("ma_no_token", note="MUSICBOX_MA_TOKEN is unset, commands may be rejected")
        await self.refresh_player(force=True)

    async def _await_server_info(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not self._info_seen:
            ws = self._ws
            if ws is None or ws.closed:
                # The server hung up instead of greeting us. The usual cause is
                # a fresh MA container with no users, which answers
                # {"message_id":"connection","error_code":503,"details":"Setup
                # required"} and closes. Returning immediately lets the auth
                # attempt below fail fast and put the real reason in last_error,
                # instead of burning the whole timeout on every retry.
                return
            if time.monotonic() > deadline:
                # Not fatal. Some proxies swallow the unsolicited frame, and
                # auth works regardless.
                logs.warn("ma_no_server_info", note="continuing without it")
                return
            await asyncio.sleep(0.02)

    # ── message pump ──────────────────────────────────────────────────────────

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except ValueError:
                    logs.warn("ma_bad_frame", data=msg.data[:200])
                    continue
                if isinstance(payload, dict):
                    self._dispatch(payload)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise ws.exception() or ConnectionError("websocket error")

    def _dispatch(self, payload: dict) -> None:
        # Route on key presence. Do not copy the upstream parse_message helper:
        # it tests for sdk_version and therefore never matches the server info
        # frame this server actually sends.
        if "server_version" in payload and "message_id" not in payload:
            self.server_info = payload
            self._info_seen = True
            return

        message_id = payload.get("message_id")
        if message_id is None:
            if "event" in payload:
                self.events_seen += 1
            return
        message_id = str(message_id)

        if "error_code" in payload:
            # message_id "connection" is the server refusing the socket itself,
            # most often 503 "Setup required" on a fresh container.
            details = str(payload.get("details", ""))
            code = int(payload.get("error_code", 999))
            future = self._pending.pop(message_id, None)
            self._partials.pop(message_id, None)
            if future is None or future.done():
                self.last_error = f"MA error {code}: {details}"
                logs.warn("ma_unmatched_error", code=code, details=details, message_id=message_id)
                return
            future.set_exception(MAError(code, details, command=message_id))
            return

        if "event" in payload:
            self.events_seen += 1
            return

        if payload.get("partial"):
            self._partials.setdefault(message_id, []).extend(payload.get("result") or [])
            return

        future = self._pending.pop(message_id, None)
        chunks = self._partials.pop(message_id, None)
        if future is None or future.done():
            return
        result = payload.get("result")
        if chunks is not None:
            chunks.extend(result or [])
            result = chunks
        future.set_result(result)

    # ── command plumbing ──────────────────────────────────────────────────────

    async def _call(
        self,
        command: str,
        args: dict | None = None,
        timeout: float | None = None,
        require_connected: bool = True,
    ) -> Any:
        if require_connected and not self.connected:
            raise MANotConnected(self.last_error or "not connected to Music Assistant")
        ws = self._ws
        if ws is None or ws.closed:
            raise MANotConnected(self.last_error or "websocket is closed")

        message_id = f"{command}#{next(self._ids)}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        frame = {"message_id": message_id, "command": command, "args": args or {}}
        try:
            await ws.send_str(json.dumps(frame))
            return await asyncio.wait_for(future, timeout or self._config.command_timeout)
        except asyncio.TimeoutError as exc:
            raise MATimeout(f"{command} timed out after {timeout or self._config.command_timeout}s") from exc
        except MAError as exc:
            if exc.code in AUTH_ERRORS:
                self.authenticated = False
                self.last_error = f"auth: {exc.details}"
            # Re-stamp the command: _dispatch only knows the message_id.
            raise MAError(exc.code, exc.details, command=command) from None
        except ConnectionResetError as exc:
            raise MANotConnected(str(exc)) from exc
        finally:
            self._pending.pop(message_id, None)
            self._partials.pop(message_id, None)

    async def command(self, command: str, args: dict | None = None, timeout: float | None = None) -> Any:
        """Escape hatch for anything without an adapter method below."""
        return await self._call(command, args, timeout)

    # ── player resolution ─────────────────────────────────────────────────────

    async def refresh_player(self, force: bool = False) -> None:
        """Resolve MUSICBOX_PLAYER, which may be an id or a display name.

        Re-resolved on every reconnect because ids are not guaranteed stable
        across an MA restart, and lazily (rate limited) while it is missing so
        that a speaker which shows up ten minutes late is picked up without an
        operator doing anything.
        """
        async with self._player_lock:
            # Rate limited whether or not we currently have a player. Without
            # the second half of that, a missing player means one extra pair of
            # MA commands on EVERY request, which is exactly when the box is
            # already unhappy.
            if not force and time.monotonic() - self._player_lookup_at < 5.0:
                return
            self._player_lookup_at = time.monotonic()
            wanted = self._config.player
            if not wanted:
                self.player_id = None
                self.player_error = "MUSICBOX_PLAYER is not set"
                return

            player = None
            try:
                player = await self._call(
                    COMMANDS["player_get"],
                    {"player_id": wanted, "raise_unavailable": False},
                    require_connected=False,
                )
            except MAError as exc:
                if exc.code in AUTH_ERRORS:
                    self.player_error = f"auth: {exc.details}"
                    self.last_error = self.player_error
                    return
                player = None
            except (MANotConnected, MATimeout) as exc:
                self.player_error = str(exc)
                return

            if not player:
                try:
                    player = await self._call(
                        COMMANDS["player_get_by_name"],
                        {"name": wanted},
                        require_connected=False,
                    )
                except (MAError, MATimeout) as exc:
                    self.player_error = str(exc)
                    return
                except MANotConnected as exc:
                    self.player_error = str(exc)
                    return

            if not player:
                self.player_id = None
                self.player_name = None
                self.player_features = []
                self.player_error = f"no MA player matches {wanted!r}"
                logs.warn("ma_player_missing", wanted=wanted)
                return

            self.player_id = player.get("player_id")
            self.player_name = player.get("name")
            self.player_features = [str(f) for f in (player.get("supported_features") or [])]
            self.player_error = None if player.get("available", True) else "player is unavailable"

    async def require_player(self) -> str:
        if not self.connected:
            raise MANotConnected(self.last_error or "not connected to Music Assistant")
        if not self.player_id:
            await self.refresh_player()
        if not self.player_id:
            raise MANotConnected(self.player_error or "no player resolved")
        return self.player_id

    def supports_announcement(self) -> bool:
        """True unless MA positively says otherwise.

        When the feature list is empty (an older model version, or a provider
        that does not populate it) the honest answer is "try it and see", so the
        optimistic default is correct: /drop over falls back to cut on the
        actual PlayerCommandFailed rather than on a guess.
        """
        if not self.player_features:
            return True
        return any(FEATURE_ANNOUNCEMENT in f.lower() for f in self.player_features)

    # ── adapters: one method per MA command this app uses ─────────────────────

    async def get_player(self) -> dict:
        player_id = await self.require_player()
        return await self._call(
            COMMANDS["player_get"], {"player_id": player_id, "raise_unavailable": False}
        ) or {}

    async def list_players(self) -> list:
        return await self._call(COMMANDS["players_all"], {"return_unavailable": True}) or []

    async def get_active_queue(self) -> dict | None:
        """Always ask, never cache.

        queue_id equals player_id only while the player is alone. The moment it
        is grouped or synced the active queue belongs to the group, and a cached
        id sends every transport command to a queue nobody is listening to.
        """
        player_id = await self.require_player()
        return await self._call(COMMANDS["queue_get_active"], {"player_id": player_id})

    async def require_queue_id(self) -> str:
        queue = await self.get_active_queue()
        if not queue or not queue.get("queue_id"):
            raise MANotConnected("player has no active queue")
        return str(queue["queue_id"])

    async def play_media(self, media: str, option: str) -> None:
        if option not in QUEUE_OPTIONS:
            raise ValueError(f"invalid queue option {option!r}")
        queue_id = await self.require_queue_id()
        await self._call(
            COMMANDS["queue_play_media"],
            {"queue_id": queue_id, "media": media, "option": option, "radio_mode": False},
        )

    async def play_announcement(self, url: str, volume_level: int | None = None) -> None:
        player_id = await self.require_player()
        await self._call(
            COMMANDS["player_play_announcement"],
            {
                "player_id": player_id,
                "url": url,
                # The arg is pre_announce. use_pre_announce is the pre 2.9 name
                # and is silently dropped, because args are parsed with
                # strict=False. Passing it explicitly also stops MA from
                # deciding on its own: when it is None and the url contains the
                # substring "tts" anywhere, MA may prepend a chime.
                "pre_announce": False,
                "volume_level": volume_level,
            },
            timeout=self._config.announce_timeout,
        )

    async def pause(self) -> None:
        queue_id = await self.require_queue_id()
        # On snapcast this becomes a STOP (the provider declares no PAUSE
        # feature), but MA records queue.resume_pos first, which is what makes
        # resume land in the right place afterwards.
        await self._call(COMMANDS["queue_pause"], {"queue_id": queue_id})

    async def resume(self) -> None:
        queue_id = await self.require_queue_id()
        # resume, not play: play restarts the current item, resume replays from
        # the recorded resume_pos. With a stop based player that difference is
        # the whole feature.
        await self._call(COMMANDS["queue_resume"], {"queue_id": queue_id, "fade_in": False})

    async def next_track(self) -> None:
        queue_id = await self.require_queue_id()
        await self._call(COMMANDS["queue_next"], {"queue_id": queue_id})

    async def play_index(self, index: Any, seek_position: int = 0) -> None:
        queue_id = await self.require_queue_id()
        await self._call(
            COMMANDS["queue_play_index"],
            {
                "queue_id": queue_id,
                "index": index,
                "seek_position": int(seek_position),
                "fade_in": False,
            },
        )

    async def set_volume(self, level: int) -> None:
        player_id = await self.require_player()
        await self._call(
            COMMANDS["player_volume_set"], {"player_id": player_id, "volume_level": int(level)}
        )

    # ── unauthenticated liveness probe ────────────────────────────────────────

    async def probe_info(self, timeout: float = 2.0) -> dict:
        """GET /info on MA. Unauthenticated, so it works even when the token is
        wrong, which is exactly the case where /health needs to say something
        more useful than "not connected"."""
        if self._session is None or self._session.closed:
            self._session = self._session_factory()
        url = self._config.ma_url.rstrip("/") + "/info"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 - a failed probe is just "no info"
            return {}
