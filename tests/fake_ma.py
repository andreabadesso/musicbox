"""A fake Music Assistant server.

Small on purpose, but it reproduces the four behaviours of the real server that
the client has to get right: the unsolicited server info frame, auth before any
other command, replies correlated only by message_id (handled concurrently, so
they can come back out of order), and partial result streams.

No test in this suite needs a real Music Assistant.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from aiohttp import WSMsgType, web

SERVER_INFO = {
    "server_id": "fake-server",
    "server_version": "2.9.13",
    "schema_version": 28,
    "min_supported_schema_version": 25,
    "base_url": "http://127.0.0.1:8095",
    "homeassistant_addon": False,
    "onboard_done": True,
    "name": "fake",
    "status": "running",
}

PLAYER = {
    "player_id": "snapcast_musicbox",
    "provider": "snapcast",
    "name": "musicbox",
    "available": True,
    "enabled": True,
    "playback_state": "playing",
    "volume_level": 42,
    "volume_muted": False,
    "supported_features": ["play_media", "volume_set", "play_announcement"],
}

QUEUE = {
    "queue_id": "snapcast_musicbox",
    "active": True,
    "display_name": "musicbox",
    "available": True,
    "items": 3,
    "current_index": 0,
    "state": "playing",
    "elapsed_time": 12.0,
    "elapsed_time_last_updated": 0,
    "playback_speed": 1.0,
    "shuffle_enabled": False,
    "repeat_mode": "off",
    "current_item": {
        "queue_item_id": "qi1",
        "name": "Test Track",
        "duration": 180,
        "uri": "spotify://track/abc",
        "media_item": {"name": "Test Track", "artists": [{"name": "Tester"}]},
    },
}


class FakeError(Exception):
    def __init__(self, code: int, details: str) -> None:
        super().__init__(details)
        self.code = code
        self.details = details


class FakeMA:
    def __init__(self, token: str | None = "good-token") -> None:
        self.token = token
        self.url = ""
        self.calls: list[tuple[str, dict]] = []
        self.handlers: dict[str, Callable[[dict], Any]] = {}
        self.partial_commands: set[str] = set()
        self.close_after: int | None = None
        self.connections = 0
        self._runner: web.AppRunner | None = None
        self._sockets: list[web.WebSocketResponse] = []
        self._install_defaults()

    # ── defaults ──────────────────────────────────────────────────────────────

    def _install_defaults(self) -> None:
        self.player = dict(PLAYER)
        self.queue = dict(QUEUE)
        self.resolve_by_id = True

        def player_get(args: dict) -> Any:
            if not self.resolve_by_id:
                return None
            if args.get("player_id") in (self.player["player_id"], self.player["name"]):
                return self.player
            return None

        def player_get_by_name(args: dict) -> Any:
            if str(args.get("name", "")).lower() == str(self.player["name"]).lower():
                return self.player
            return None

        async def slow(args: dict) -> Any:
            await asyncio.sleep(0.2)
            return {"slow": True}

        async def never(args: dict) -> Any:
            await asyncio.sleep(3600)

        self.handlers.update(
            {
                "players/all": lambda args: [self.player],
                "players/get": player_get,
                "players/get_by_name": player_get_by_name,
                "player_queues/get_active_queue": lambda args: self.queue,
                "player_queues/play_media": lambda args: None,
                "player_queues/pause": lambda args: None,
                "player_queues/resume": lambda args: None,
                "player_queues/next": lambda args: None,
                "players/cmd/volume_set": lambda args: None,
                "players/cmd/play_announcement": lambda args: None,
                "test/slow": slow,
                "test/fast": lambda args: {"fast": True},
                "test/never": never,
                "test/boom": self._boom,
                "test/stream": lambda args: ["a", "b", "c", "d"],
            }
        )
        self.partial_commands.add("test/stream")

    def _boom(self, args: dict) -> Any:
        raise FakeError(2, "media not found")

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/info", self._handle_info)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        await self.drop_connections()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def drop_connections(self) -> None:
        sockets, self._sockets = self._sockets, []
        for ws in sockets:
            if not ws.closed:
                await ws.close()

    # ── handlers ──────────────────────────────────────────────────────────────

    async def _handle_info(self, request: web.Request) -> web.Response:
        return web.json_response(SERVER_INFO)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        self.connections += 1
        self._sockets.append(ws)
        authed = self.token is None
        handled = 0

        await ws.send_str(json.dumps(SERVER_INFO))

        tasks: list[asyncio.Task] = []
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            payload = json.loads(msg.data)
            message_id = payload.get("message_id")
            command = payload.get("command")
            args = payload.get("args") or {}
            self.calls.append((command, args))

            if command == "auth":
                if self.token is not None and args.get("token") != self.token:
                    await ws.send_str(
                        json.dumps(
                            {
                                "message_id": message_id,
                                "error_code": 23,
                                "details": "Invalid or expired token",
                            }
                        )
                    )
                    continue
                authed = True
                await ws.send_str(
                    json.dumps(
                        {
                            "message_id": message_id,
                            "result": {"authenticated": True, "user": {"username": "musicbox"}},
                            "partial": False,
                        }
                    )
                )
                continue

            if not authed:
                await ws.send_str(
                    json.dumps(
                        {
                            "message_id": message_id,
                            "error_code": 20,
                            "details": "Authentication required. Please send auth command first.",
                        }
                    )
                )
                continue

            handled += 1
            if self.close_after is not None and handled > self.close_after:
                await ws.close()
                break

            # One task per command, like the real server: replies are correlated
            # by message_id, not by arrival order.
            tasks.append(asyncio.create_task(self._respond(ws, message_id, command, args)))

        for task in tasks:
            task.cancel()
        if ws in self._sockets:
            self._sockets.remove(ws)
        return ws

    async def _respond(self, ws: web.WebSocketResponse, message_id: str, command: str, args: dict) -> None:
        handler = self.handlers.get(command)
        try:
            if handler is None:
                raise FakeError(12, f"Invalid Command: {command}")
            result = handler(args)
            if asyncio.iscoroutine(result):
                result = await result
        except FakeError as exc:
            if not ws.closed:
                await ws.send_str(
                    json.dumps(
                        {"message_id": message_id, "error_code": exc.code, "details": exc.details}
                    )
                )
            return
        except asyncio.CancelledError:
            return

        if ws.closed:
            return
        if command in self.partial_commands and isinstance(result, list):
            for chunk in result[:-1]:
                await ws.send_str(
                    json.dumps({"message_id": message_id, "result": [chunk], "partial": True})
                )
            await ws.send_str(
                json.dumps({"message_id": message_id, "result": [result[-1]], "partial": False})
            )
            return
        await ws.send_str(json.dumps({"message_id": message_id, "result": result, "partial": False}))

    async def push_event(self, event: str, data: Any = None) -> None:
        for ws in list(self._sockets):
            if not ws.closed:
                await ws.send_str(json.dumps({"event": event, "data": data}))
