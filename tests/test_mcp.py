"""The MCP surface: the tools, their degraded answers, and the /mcp endpoint.

Two levels, because they can fail independently:

  * The tools, driven through a real FastMCP instance with the stub MA client
    behind it. Going through FastMCP.call_tool rather than calling the closures
    means the argument schemas and the registration are under test too, which
    is where a rename would otherwise slip through.
  * The mounted endpoint, driven over HTTP with real JSON-RPC frames. That is
    the only way to prove the thing an agent actually depends on: that /mcp
    answers with no bearer token while the rest of the API refuses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

import aiohttp
import uvicorn
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musicbox import __version__  # noqa: E402
from musicbox.app import create_app  # noqa: E402
from musicbox.config import Config  # noqa: E402
from musicbox.ma_client import MAError  # noqa: E402
from musicbox.mcp_server import (  # noqa: E402
    HttpBackend,
    LocalBackend,
    MCP_PATH,
    build_mcp,
    closing_backend,
)

from conftest import async_test  # noqa: E402
from test_app import MP3_BYTES, StubMA  # noqa: E402

# Everything an MCP client has to send on a streamable HTTP POST. The Accept
# header is not optional and not a formality: the transport rejects a request
# that does not offer both types with a 406 before any tool is reached, and
# that failure looks exactly like a broken endpoint.
MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def rpc(method: str, params: dict | None = None, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def build_mcp_for(tmp_path: Path, ma: StubMA | None = None, **overrides):
    sfx_dir = tmp_path / "sfx"
    sfx_dir.mkdir(exist_ok=True)
    (sfx_dir / "airhorn.mp3").write_bytes(MP3_BYTES)
    stub = ma or StubMA()
    config = Config(
        host="0.0.0.0",
        port=8099,
        player="musicbox",
        sfx_dir=sfx_dir,
        sfx_base_url="http://10.88.0.1:8099",
        **overrides,
    )
    return build_mcp(LocalBackend(config, stub)), stub


async def call(mcp, tool: str, /, **arguments):
    """Call a tool and return its text, tolerating the SDK's return shape.

    call_tool returned a bare content sequence in older SDK releases and a
    (content, structured) pair in newer ones. The nixpkgs version is whatever
    the Pi's channel has, so the test pins neither.

    Positional-only for mcp and tool, because `sfx` takes an argument called
    `name` and a keyword collision here is a confusing TypeError in a test that
    looks correct.
    """
    result = await mcp.call_tool(tool, arguments)
    content = result[0] if isinstance(result, tuple) else result
    return "".join(block.text for block in content if getattr(block, "type", None) == "text")


async def call_json(mcp, tool: str, /, **arguments):
    return json.loads(await call(mcp, tool, **arguments))


# ── registration ──────────────────────────────────────────────────────────────


@async_test()
async def test_every_promised_tool_is_registered(tmp_path):
    mcp, _ = build_mcp_for(tmp_path)
    names = {tool.name for tool in await mcp.list_tools()}
    assert names == {
        "play",
        "queue",
        "drop",
        "sfx",
        "list_sfx",
        "now_playing",
        "skip",
        "pause",
        "resume",
        "set_volume",
        "reconnect",
    }


@async_test()
async def test_sfx_is_a_resource_as_well_as_a_tool(tmp_path):
    # So a client can see what sounds exist without spending a tool call.
    mcp, _ = build_mcp_for(tmp_path)
    resources = await mcp.list_resources()
    assert [str(r.uri) for r in resources] == ["musicbox://sfx"]
    text = await mcp.read_resource("musicbox://sfx")
    body = "".join(item.content for item in text)
    assert "airhorn" in body


# ── happy paths ───────────────────────────────────────────────────────────────


@async_test()
async def test_play_replaces_the_queue_and_queue_appends(tmp_path):
    mcp, stub = build_mcp_for(tmp_path)
    said = await call(mcp, "play", uri_or_url="spotify://track/abc")
    assert "spotify://track/abc" in said
    said = await call(mcp, "queue", uri_or_url="https://x/y.mp3")
    assert "https://x/y.mp3" in said
    assert ("play_media", "spotify://track/abc", "replace") in stub.calls
    assert ("play_media", "https://x/y.mp3", "add") in stub.calls


@async_test()
async def test_now_playing_reports_the_track_position_volume_and_queue_length(tmp_path):
    mcp, _ = build_mcp_for(tmp_path)
    body = await call_json(mcp, "now_playing")
    assert body["title"] == "Test Track"
    assert body["artist"] == "Tester"
    assert body["volume"] == 42
    assert body["queue_length"] == 3
    assert body["position_seconds"] >= 10


@async_test()
async def test_transport_tools_reach_the_client(tmp_path):
    mcp, stub = build_mcp_for(tmp_path)
    assert "Skipped" in await call(mcp, "skip")
    assert "Paused" in await call(mcp, "pause")
    assert "Resumed" in await call(mcp, "resume")
    assert "55" in await call(mcp, "set_volume", level=55)
    assert ("next_track",) in stub.calls
    assert ("pause",) in stub.calls
    assert ("resume",) in stub.calls
    assert ("set_volume", 55) in stub.calls


@async_test()
async def test_reconnect_reports_what_it_found(tmp_path):
    mcp, stub = build_mcp_for(tmp_path)
    said = await call(mcp, "reconnect")
    assert "Reconnected" in said
    assert "musicbox" in said
    assert ("reconnect",) in stub.calls


@async_test()
async def test_drop_over_says_the_music_is_silenced_not_lowered(tmp_path):
    # The model has to be told, or it will happily narrate over a drop it
    # thinks is ducking under the music.
    mcp, stub = build_mcp_for(tmp_path)
    said = await call(mcp, "drop", url="http://x/y.mp3", mode="over")
    assert "no ducking" in said
    assert ("play_announcement", "http://x/y.mp3") in stub.calls


@async_test()
async def test_drop_cut_pauses_and_resumes(tmp_path):
    mcp, stub = build_mcp_for(tmp_path)
    said = await call(mcp, "drop", url="http://x/y.mp3", mode="cut")
    assert "resumed the music" in said
    names = [c[0] for c in stub.calls]
    assert names.index("pause") < names.index("play_announcement") < names.index("resume")


@async_test()
async def test_sfx_hands_ma_a_url_it_can_fetch(tmp_path):
    # No incoming request here, so the URL comes from MUSICBOX_SFX_BASE_URL.
    # That is the whole reason the NixOS module always sets it.
    mcp, stub = build_mcp_for(tmp_path)
    said = await call(mcp, "sfx", name="airhorn")
    assert "airhorn" in said
    assert ("play_announcement", "http://10.88.0.1:8099/sfx/file/airhorn.mp3") in stub.calls


@async_test()
async def test_list_sfx_returns_names_only(tmp_path):
    mcp, _ = build_mcp_for(tmp_path)
    body = await call_json(mcp, "list_sfx")
    assert body == {"count": 1, "names": ["airhorn"]}


# ── bad input, answered rather than raised ────────────────────────────────────


@async_test()
async def test_bad_arguments_come_back_as_sentences(tmp_path):
    mcp, stub = build_mcp_for(tmp_path)
    assert "http://" in await call(mcp, "drop", url="/var/lib/musicbox/sfx/a.mp3")
    assert "'over' or 'cut'" in await call(mcp, "drop", url="http://x/y.mp3", mode="sideways")
    assert "between 0 and 100" in await call(mcp, "set_volume", level=101)
    assert "provider URI" in await call(mcp, "play", uri_or_url="   ")
    # Nothing reached Music Assistant on any of them.
    assert [c for c in stub.calls if c[0] in ("play_announcement", "play_media", "set_volume")] == []


@async_test()
async def test_an_explicit_null_mode_is_the_default_and_not_a_schema_error(tmp_path):
    # Models spell an omitted optional argument as an explicit null all the
    # time. Arguments are validated by pydantic BEFORE the tool body runs, so
    # with `mode: str` that null never reached _mode() and the model got "1
    # validation error for dropArguments" instead of a drop.
    mcp, stub = build_mcp_for(tmp_path)
    assert "no ducking" in await call(mcp, "drop", url="http://x/y.mp3", mode=None)
    assert "airhorn" in await call(mcp, "sfx", name="airhorn", mode=None)
    assert len([c for c in stub.calls if c[0] == "play_announcement"]) == 2


@async_test()
async def test_a_volume_that_is_not_a_whole_number_is_answered_not_rejected(tmp_path):
    # Same trap as the null mode above: with `level: int` these were pydantic
    # validation errors, and the int() guard in the tool body was unreachable.
    mcp, stub = build_mcp_for(tmp_path)
    assert "Volume set to 56" in await call(mcp, "set_volume", level=55.7)
    assert "Volume set to 40" in await call(mcp, "set_volume", level="40")
    said = await call(mcp, "set_volume", level="loud")
    assert "level must be a number from 0 to 100" in said
    assert "'loud'" in said
    # float() takes "inf" and round() then refuses it, which is a bad argument
    # and not the "musicbox hit an unexpected" arm.
    assert "level must be a number from 0 to 100" in await call(mcp, "set_volume", level="inf")
    assert [c for c in stub.calls if c[0] == "set_volume"] == [("set_volume", 56), ("set_volume", 40)]


@async_test()
async def test_unknown_sfx_names_what_does_exist(tmp_path):
    mcp, _ = build_mcp_for(tmp_path)
    said = await call(mcp, "sfx", name="vuvuzela")
    assert "no sound effect called 'vuvuzela'" in said
    assert "airhorn" in said


# ── degraded: Music Assistant is down ─────────────────────────────────────────


@async_test()
async def test_every_tool_answers_in_words_when_ma_is_down(tmp_path):
    mcp, _ = build_mcp_for(tmp_path, ma=StubMA(connected=False))
    for name, arguments in [
        ("play", {"uri_or_url": "spotify://track/abc"}),
        ("queue", {"uri_or_url": "spotify://track/abc"}),
        ("drop", {"url": "http://x/y.mp3", "mode": "over"}),
        ("sfx", {"name": "airhorn"}),
        ("skip", {}),
        ("pause", {}),
        ("resume", {}),
        ("set_volume", {"level": 10}),
    ]:
        said = await call(mcp, name, **arguments)
        assert "Music Assistant is not reachable" in said, name
        assert "reconnect tool" in said, name


@async_test()
async def test_now_playing_and_list_sfx_stay_readable_when_ma_is_down(tmp_path):
    mcp, _ = build_mcp_for(tmp_path, ma=StubMA(connected=False))
    now = await call_json(mcp, "now_playing")
    assert now["state"] == "unknown"
    assert "Music Assistant is not reachable" in now["error"]
    # The sfx directory is on our own disk, so this one still works with MA
    # down. Worth asserting: it is what an agent falls back to.
    assert await call_json(mcp, "list_sfx") == {"count": 1, "names": ["airhorn"]}


@async_test()
async def test_ma_refusing_a_command_is_reported_with_its_own_words(tmp_path):
    stub = StubMA()
    stub.queue_error = MAError(13, "unplayable media", command="player_queues/play_media")
    mcp, _ = build_mcp_for(tmp_path, ma=stub)
    said = await call(mcp, "now_playing")
    assert "unplayable media" in json.dumps(said)


@async_test()
async def test_reconnect_that_does_not_help_says_so(tmp_path):
    mcp, _ = build_mcp_for(tmp_path, ma=StubMA(connected=False))
    said = await call(mcp, "reconnect")
    assert "still not answering" in said
    assert "connection refused" in said


# ── the mounted endpoint ──────────────────────────────────────────────────────


def app_client(tmp_path, token: str = "", ma: StubMA | None = None) -> TestClient:
    sfx_dir = tmp_path / "sfx"
    sfx_dir.mkdir(exist_ok=True)
    (sfx_dir / "airhorn.mp3").write_bytes(MP3_BYTES)
    config = Config(
        host="0.0.0.0",
        port=8099,
        player="musicbox",
        sfx_dir=sfx_dir,
        token=token,
        sfx_base_url="http://10.88.0.1:8099",
    )
    # WITH the context manager, unlike tests/test_app.py: the lifespan is what
    # starts the MCP session manager, and without it every /mcp request fails
    # with "Task group is not initialized". The MA client is the stub, so the
    # lifespan still opens no real connection.
    return TestClient(create_app(config, ma or StubMA()))


def mcp_post(client: TestClient, body: dict):
    return client.post(MCP_PATH, json=body, headers=MCP_HEADERS)


def test_mcp_is_open_while_the_rest_of_the_api_demands_a_token(tmp_path):
    with app_client(tmp_path, token="s3cret") as client:
        # The deliberate hole. Documented in app.py, in the README, and here:
        # anyone who can reach this port can play audio with no credentials.
        response = mcp_post(client, rpc("tools/list"))
        assert response.status_code == 200
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert "play" in names and "now_playing" in names

        # And the rest of the API is still shut.
        assert client.get("/now").status_code == 401
        assert client.get("/now", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_mcp_ignores_a_wrong_token_rather_than_rejecting_it(tmp_path):
    # An MCP client that guesses at a header must not be locked out of an
    # endpoint that takes no credentials in the first place.
    with app_client(tmp_path, token="s3cret") as client:
        response = client.post(
            MCP_PATH,
            json=rpc("tools/list"),
            headers={**MCP_HEADERS, "authorization": "Bearer nonsense"},
        )
        assert response.status_code == 200


def test_a_tool_call_over_http_reaches_music_assistant(tmp_path):
    stub = StubMA()
    with app_client(tmp_path, token="s3cret", ma=stub) as client:
        response = mcp_post(
            client,
            rpc("tools/call", {"name": "play", "arguments": {"uri_or_url": "spotify://track/abc"}}),
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result.get("isError") is not True
        assert "spotify://track/abc" in result["content"][0]["text"]
    assert ("play_media", "spotify://track/abc", "replace") in stub.calls


def test_a_failing_tool_over_http_is_still_a_successful_call(tmp_path):
    # The point of the whole "a tool never raises" rule: MA being down must
    # reach the model as words, not as a protocol level error it has to guess
    # the meaning of.
    with app_client(tmp_path, ma=StubMA(connected=False)) as client:
        response = mcp_post(
            client,
            rpc("tools/call", {"name": "skip", "arguments": {}}),
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result.get("isError") is not True
        assert "Music Assistant is not reachable" in result["content"][0]["text"]


def test_mcp_answers_at_exactly_slash_mcp_with_no_redirect(tmp_path):
    # The URL in the README and in `claude mcp add` is /mcp with no trailing
    # slash. A starlette Mount would only match /mcp/ and answer this with a
    # 307, which is a round trip on every call and a client incompatibility
    # waiting to happen.
    with app_client(tmp_path) as client:
        response = client.post(
            MCP_PATH, json=rpc("tools/list"), headers=MCP_HEADERS, follow_redirects=False
        )
        assert response.status_code == 200


def test_the_handshake_reports_musicbox_own_version(tmp_path):
    # Not cosmetic enough to skip: FastMCP takes no version argument and the
    # low level server defaults to the version of the `mcp` package itself, so
    # without the fix in build_mcp the handshake told every client that
    # musicbox was version 1.29.0, which is the SDK's number.
    with app_client(tmp_path) as client:
        response = mcp_post(
            client,
            rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            ),
        )
        assert response.status_code == 200
        info = response.json()["result"]["serverInfo"]
        assert info["name"] == "musicbox"
        assert info["version"] == __version__


def test_mcp_does_not_appear_in_the_openapi_schema(tmp_path):
    # It is JSON-RPC over one path, not a REST endpoint, and an entry for it in
    # the schema would only mislead whoever reads it.
    with app_client(tmp_path) as client:
        assert MCP_PATH not in client.app.openapi()["paths"]


# ── the stdio entrypoint's HTTP backend ───────────────────────────────────────
# musicbox-mcp presents the same tools but reaches the box over HTTP. Worth
# testing end to end rather than with a mocked session: the whole point of that
# entrypoint is the wire between it and a musicbox, and the tools have to give
# the same answers there as they do in process.


@contextlib.asynccontextmanager
async def serving(app):
    """Run a real musicbox on an ephemeral port for the length of the block."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def app_for(tmp_path: Path, token: str = "", ma: StubMA | None = None):
    sfx_dir = tmp_path / "sfx"
    sfx_dir.mkdir(exist_ok=True)
    (sfx_dir / "airhorn.mp3").write_bytes(MP3_BYTES)
    stub = ma or StubMA()
    config = Config(
        host="127.0.0.1",
        port=8099,
        player="musicbox",
        sfx_dir=sfx_dir,
        token=token,
        sfx_base_url="http://10.88.0.1:8099",
    )
    return create_app(config, stub), stub


@async_test()
async def test_the_stdio_proxy_drives_a_real_musicbox(tmp_path):
    app, stub = app_for(tmp_path)
    async with serving(app) as url:
        backend = HttpBackend(url)
        mcp = build_mcp(backend)
        try:
            assert "spotify://track/abc" in await call(mcp, "play", uri_or_url="spotify://track/abc")
            assert (await call_json(mcp, "now_playing"))["title"] == "Test Track"
            assert await call_json(mcp, "list_sfx") == {"count": 1, "names": ["airhorn"]}
            assert "airhorn" in await call(mcp, "sfx", name="airhorn")
            assert "Volume set to 55" in await call(mcp, "set_volume", level=55)
        finally:
            await backend.close()
    assert ("play_media", "spotify://track/abc", "replace") in stub.calls
    assert ("set_volume", 55) in stub.calls


@async_test()
async def test_the_stdio_proxy_plays_an_sfx_whose_name_needs_escaping(tmp_path):
    # The two backends have to agree on every name list_sfx hands out, and a
    # file an operator dropped in with Finder is where they stopped agreeing:
    # unquoted, the '#' became a URL fragment, the POST landed on
    # /sfx/big airhorn and musicbox answered 404 "no sfx named 'big airhorn '",
    # while the identical call through the mounted endpoint played it.
    app, stub = app_for(tmp_path)
    (tmp_path / "sfx" / "big airhorn #2.mp3").write_bytes(MP3_BYTES)
    async with serving(app) as url:
        backend = HttpBackend(url)
        mcp = build_mcp(backend)
        try:
            names = (await call_json(mcp, "list_sfx"))["names"]
            assert "big airhorn #2" in names
            said = await call(mcp, "sfx", name="big airhorn #2")
            assert "404" not in said
            assert "big airhorn #2" in said
        finally:
            await backend.close()
    played = [c for c in stub.calls if c[0] == "play_announcement"]
    assert played and played[0][1].endswith("/sfx/file/big%20airhorn%20%232.mp3")


@async_test()
async def test_a_musicbox_url_with_no_scheme_is_named_as_the_problem(tmp_path):
    # MUSICBOX_URL=pi5:8099 is the config typo people actually make. aiohttp
    # raises InvalidURL whose str() is only the URL, so the generic arm read
    # "Could not reach musicbox at pi5:8099: pi5:8099/skip", which sends the
    # reader off debugging the tailnet.
    backend = HttpBackend("pi5:8099")
    try:
        said = await call(build_mcp(backend), "skip")
        assert "not a usable musicbox URL" in said
        assert "http://pi5:8099" in said
    finally:
        await backend.close()


@async_test()
async def test_the_stdio_proxy_carries_the_bearer_token(tmp_path):
    # Unlike the mounted /mcp endpoint, this one goes through the front door
    # and does need the token when one is configured.
    app, _ = app_for(tmp_path, token="s3cret")
    async with serving(app) as url:
        good, bad = HttpBackend(url, "s3cret"), HttpBackend(url, "wrong")
        try:
            assert "Skipped" in await call(build_mcp(good), "skip")
            assert "bearer token" in await call(build_mcp(bad), "skip")
        finally:
            await good.close()
            await bad.close()


@async_test()
async def test_the_stdio_proxy_says_so_when_the_box_is_not_there(tmp_path):
    # The failure a user will actually hit: MUSICBOX_URL pointing at a box that
    # is off, or a tailnet that is down. It has to read as a sentence, not as a
    # connection error traceback in the client's log.
    backend = HttpBackend("http://127.0.0.1:1")
    try:
        said = await call(build_mcp(backend), "play", uri_or_url="spotify://track/abc")
        assert "Could not reach musicbox at http://127.0.0.1:1" in said
        assert "MUSICBOX_URL" in said
    finally:
        await backend.close()


@async_test()
async def test_the_stdio_server_closes_its_http_session_on_the_way_out(tmp_path):
    # Without this lifespan aiohttp prints "Unclosed client session" to stderr
    # as the process dies, and stderr on a stdio server is the MCP client's log
    # file, so it reads like a crash on every exit.
    app, _ = app_for(tmp_path)
    async with serving(app) as url:
        backend = HttpBackend(url)
        lifespan = closing_backend(backend)
        # build_mcp is called the way main() calls it, so a lifespan argument
        # the SDK stopped accepting fails here rather than in the field.
        mcp = build_mcp(backend, lifespan=lifespan)
        assert "Skipped" in await call(mcp, "skip")
        assert backend._session is not None and not backend._session.closed
        async with lifespan(mcp):
            pass
        assert backend._session.closed


@async_test()
async def test_a_non_loopback_host_header_is_not_a_421(tmp_path):
    # Pinning the trap in mcp_server.MountedMCP. The SDK's default DNS
    # rebinding protection allows only 127.0.0.1, localhost and [::1], so
    # every remote client would get "421 Misdirected Request" from an endpoint
    # that is otherwise working perfectly.
    app, _ = app_for(tmp_path)
    async with serving(app) as url:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}{MCP_PATH}",
                json=rpc("tools/list"),
                headers={**MCP_HEADERS, "host": "pi5:8099", "origin": "http://pi5:8099"},
            ) as resp:
                assert resp.status == 200
                body = await resp.json()
    assert {tool["name"] for tool in body["result"]["tools"]} >= {"play", "sfx"}
