"""Talking to musicbox-mixer over its unix socket.

This module is deliberately tiny and deliberately dependency free: it must not
import numpy, alsaaudio or anything else the mixer needs, because app.py
imports it unconditionally and the API process has to start on a machine with
no sound card and no audio libraries at all.

The protocol is one line in, one line out, shlex quoted both ways:

    play "big airhorn" gain=-6
    ok play name="big airhorn" seconds=1.90 gain_db=-6.000 voices=2

    play nope
    err not_found name=nope known=airhorn,tambores

shlex on both ends is not decoration. `big airhorn #2.mp3` is an ordinary thing
for someone to drop into the sfx directory from Finder, and a protocol that
splits on spaces turns that name into two arguments and a 404 for a file that
is right there. The same class of bug already bit the MCP proxy once, over URL
quoting, and cost an event's worth of confusion.

EVERY failure here is a MixerUnavailable, and the caller's job is to fall back
to the Music Assistant announcement path rather than to fail the request. The
mixer being down must never mean the button does nothing.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any

from . import logs
from .config import MixerConfig


class MixerUnavailable(Exception):
    """The mixer could not be reached or refused the command.

    One exception type for connection refused, no such file, timeout and an
    `err` response on purpose: the caller does exactly the same thing for all
    of them, which is to take the other path and say so in the answer.
    """


def parse(line: str) -> dict[str, Any]:
    """A response line into a dict, with `ok` and `command` always present."""
    try:
        parts = shlex.split(line.strip())
    except ValueError:
        parts = line.strip().split()
    result: dict[str, Any] = {"ok": bool(parts) and parts[0] == "ok", "command": None}
    fields = []
    for index, part in enumerate(parts[1:]):
        key, sep, value = part.partition("=")
        if sep and key:
            result[key] = value
        else:
            fields.append(part)
    if fields:
        result["command"] = fields[0]
        if not result["ok"]:
            # For an error line the second bare word is the code: "err
            # not_found name=x". Kept separate from `command` so a caller can
            # branch on the code without string matching the whole line.
            result["error"] = fields[0]
            result["message"] = " ".join(fields[1:]) or result.get("message")
    return result


class MixerClient:
    """A connection per command, opened and closed.

    A persistent connection would be one more thing to reconnect, and the
    measurable cost of not having one is a unix socket connect, which is tens
    of microseconds against an effect whose trigger-to-audible is around 300 ms
    of buffers. Not worth any state.
    """

    def __init__(self, settings: MixerConfig):
        self.settings = settings
        self.socket_path: Path | None = settings.socket
        self.timeout = settings.timeout
        # Cheap health signal for GET /health, updated by every real command,
        # so the health endpoint does not have to make a call of its own to say
        # something true about the last time this mattered.
        self.last_ok: bool | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enable and self.socket_path)

    async def command(self, line: str) -> dict[str, Any]:
        if not self.enabled:
            raise MixerUnavailable("the mixer is not enabled")
        try:
            reply = await asyncio.wait_for(self._roundtrip(line), self.timeout)
        except asyncio.TimeoutError as exc:
            self._record(False, f"timed out after {self.timeout}s")
            raise MixerUnavailable(
                f"the mixer did not answer within {self.timeout}s on {self.socket_path}"
            ) from exc
        except (OSError, ConnectionError) as exc:
            # FileNotFoundError means the unit is not running (or the socket
            # path disagrees); ConnectionRefusedError means it died holding the
            # file; PermissionError means the socket mode or the group is
            # wrong, which is the one that looks like a bug and is not.
            self._record(False, f"{type(exc).__name__}: {exc}")
            raise MixerUnavailable(f"could not reach the mixer at {self.socket_path}: {exc}") from exc
        except MixerUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - the promise in the docstring
            # Everything else, including the ones nobody predicted: a socket
            # path too long for sun_path, a garbled line that upsets the
            # parser, a bug in here. The caller's only job is to fall back to
            # the announcement path, and it can only do that if the promise
            # this module makes ("every failure is a MixerUnavailable") has no
            # holes in it. A 500 from POST /sfx because the mixer misbehaved
            # would mean the button does nothing, which is strictly worse than
            # the behaviour the box had yesterday.
            #
            # CancelledError is a BaseException and is deliberately not caught:
            # a cancelled request is not a mixer fault.
            self._record(False, f"{type(exc).__name__}: {exc}")
            logs.error("mixer_client_failed", socket=str(self.socket_path), error=f"{type(exc).__name__}: {exc}")
            raise MixerUnavailable(f"the mixer call failed unexpectedly: {exc}") from exc

        result = parse(reply)
        if not result["ok"]:
            self._record(False, reply.strip())
            raise MixerUnavailable(result.get("message") or reply.strip() or "the mixer said no")
        self._record(True, None)
        return result

    async def _roundtrip(self, line: str) -> str:
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write((line.rstrip("\n") + "\n").encode("utf-8"))
            await writer.drain()
            raw = await reader.readline()
            if not raw:
                raise MixerUnavailable("the mixer closed the connection without answering")
            return raw.decode("utf-8", "replace")
        finally:
            writer.close()
            # wait_closed can raise if the peer already went away, and a
            # failure to close politely is not a failure of the command that
            # already succeeded.
            with_suppressed = (OSError, ConnectionError, asyncio.TimeoutError)
            try:
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except with_suppressed:
                pass

    def _record(self, ok: bool, error: str | None) -> None:
        self.last_ok = ok
        self.last_error = error

    # ── the commands app.py actually uses ─────────────────────────────────────

    async def play(self, name: str, *, gain_db: float | None = None) -> dict[str, Any]:
        line = f"play {shlex.quote(name)}"
        if gain_db is not None:
            line += f" gain={gain_db}"
        return await self.command(line)

    async def stop(self) -> dict[str, Any]:
        return await self.command("stop")

    async def stats(self) -> dict[str, Any]:
        return await self.command("stats")

    async def ping(self) -> bool:
        try:
            await self.command("ping")
            return True
        except MixerUnavailable as exc:
            logs.warn("mixer_ping_failed", socket=str(self.socket_path), error=str(exc))
            return False

    async def reload(self) -> dict[str, Any]:
        return await self.command("reload")

    async def gain(self, db: float | None = None) -> dict[str, Any]:
        """Le (sem argumento) ou ajusta o ganho base dos efeitos, em dB."""
        return await self.command("gain" if db is None else f"gain {db}")
