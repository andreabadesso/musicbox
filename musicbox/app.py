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
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import logs
from .config import Config
from .ma_client import (
    ERR_INSUFFICIENT_PERMISSIONS,
    ERR_INVALID_COMMAND,
    ERR_MEDIA_NOT_FOUND,
    ERR_PLAYER_COMMAND_FAILED,
    ERR_PLAYER_UNAVAILABLE,
    ERR_QUEUE_EMPTY,
    ERR_UNPLAYABLE_MEDIA,
    AUTH_ERRORS,
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


class MediaBody(BaseModel):
    uri: str | None = Field(default=None, description="A provider URI, for example spotify://track/xyz")
    url: str | None = Field(default=None, description="An http(s) URL to an audio file")

    def media(self) -> str:
        value = (self.uri or self.url or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail="one of 'uri' or 'url' is required")
        return value


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


# ── app ───────────────────────────────────────────────────────────────────────


def create_app(config: Config | None = None, ma: MAClient | None = None) -> FastAPI:
    # Imported here and not at the top of the file because mcp_server imports
    # the service layer above (perform_drop, now_snapshot, list_sfx) from this
    # module, and a module level import in both directions is a cycle.
    from .mcp_server import MCP_PATH, MountedMCP

    config = config or Config.from_env()
    ma = ma or MAClient(config)

    # Built before the app so the lifespan below can enter its session manager.
    # See mcp_server.MountedMCP for why that entering is not optional.
    mcp_mount = MountedMCP(config, ma)

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

    @api.get("/now")
    async def now() -> dict[str, Any]:
        return await now_snapshot(ma)

    @api.post("/queue")
    async def queue_add(body: MediaBody) -> dict[str, Any]:
        media = body.media()
        await ma.play_media(media, "add")
        return {"ok": True, "action": "queued", "media": media}

    @api.post("/play")
    async def play_now(body: MediaBody) -> dict[str, Any]:
        media = body.media()
        # "replace" and not "play": replace clears what was queued, which is
        # what "play now, replacing the queue" means in the contract.
        await ma.play_media(media, "replace")
        return {"ok": True, "action": "playing", "media": media}

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
        result = await perform_drop(ma, sfx_url(request, path.name), chosen)
        result["sfx"] = path.stem
        return result

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
