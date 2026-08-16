"""Client behaviour against the fake MA server: handshake, correlation,
partial results, errors, player resolution and reconnect."""

from __future__ import annotations

import asyncio

import pytest

from conftest import async_test, config_for, connected_client, running_ma
from musicbox.config import Config
from musicbox.ma_client import MAClient, MAError, MANotConnected, MATimeout, ws_url_for


def test_ws_url_derivation():
    assert ws_url_for("http://127.0.0.1:8095") == "ws://127.0.0.1:8095/ws"
    assert ws_url_for("http://127.0.0.1:8095/") == "ws://127.0.0.1:8095/ws"
    assert ws_url_for("https://ma.example:443") == "wss://ma.example:443/ws"


@async_test()
async def test_handshake_authenticates_and_resolves_player():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            assert client.server_info["server_version"] == "2.9.13"
            assert client.player_id == "snapcast_musicbox"
            assert client.player_name == "musicbox"
            assert client.player_error is None
            assert client.supports_announcement() is True
            assert ("auth", {"token": "good-token"}) in server.calls


@async_test()
async def test_player_resolved_by_name_when_id_lookup_misses():
    async with running_ma() as server:
        server.resolve_by_id = False
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            assert client.player_id == "snapcast_musicbox"
            assert any(call[0] == "players/get_by_name" for call in server.calls)


@async_test()
async def test_missing_player_does_not_break_the_connection():
    async with running_ma() as server:
        async with connected_client(server, player="nope") as client:
            assert await client.wait_connected(5)
            assert client.connected is True
            assert client.player_id is None
            assert "nope" in (client.player_error or "")
            with pytest.raises(MANotConnected):
                await client.get_active_queue()


@async_test()
async def test_replies_are_correlated_not_ordered():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            slow = asyncio.create_task(client.command("test/slow"))
            await asyncio.sleep(0.01)
            fast = await client.command("test/fast")
            # The fast reply arrives while the slow one is still outstanding.
            assert fast == {"fast": True}
            assert not slow.done()
            assert await slow == {"slow": True}


@async_test()
async def test_partial_results_are_accumulated_into_one_reply():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            assert await client.command("test/stream") == ["a", "b", "c", "d"]


@async_test()
async def test_server_errors_surface_with_code_and_details():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            with pytest.raises(MAError) as caught:
                await client.command("test/boom")
            assert caught.value.code == 2
            assert caught.value.details == "media not found"
            assert caught.value.command == "test/boom"


@async_test()
async def test_unknown_command_is_reported_as_invalid_command():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            with pytest.raises(MAError) as caught:
                await client.command("nope/nope")
            assert caught.value.code == 12


@async_test()
async def test_command_timeout_does_not_wedge_the_client():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            with pytest.raises(MATimeout):
                await client.command("test/never", timeout=0.2)
            # The socket is still usable and nothing is left pending.
            assert await client.command("test/fast") == {"fast": True}
            assert client._pending == {}


@async_test()
async def test_bad_token_leaves_us_disconnected_and_says_why():
    async with running_ma() as server:
        client = MAClient(config_for(server, ma_token="wrong"))
        await client.start()
        try:
            await asyncio.sleep(0.5)
            assert client.connected is False
            assert "Invalid or expired token" in client.last_error
            # It keeps retrying rather than giving up or crashing.
            assert client.connect_attempts >= 2
        finally:
            await client.stop()


@async_test()
async def test_reconnects_after_the_server_drops_the_socket():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            first_connections = server.connections

            await server.drop_connections()
            await asyncio.sleep(0.2)

            assert await client.wait_connected(5)
            assert server.connections > first_connections
            assert client.player_id == "snapcast_musicbox"
            assert await client.command("test/fast") == {"fast": True}


@async_test()
async def test_pending_commands_fail_fast_when_the_socket_dies():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            pending = asyncio.create_task(client.command("test/never", timeout=10))
            await asyncio.sleep(0.05)
            await server.drop_connections()
            with pytest.raises(MANotConnected):
                await pending


@async_test()
async def test_commands_while_disconnected_raise_not_connected():
    async with running_ma() as server:
        url = server.url
    # Server is gone before the client ever starts, which is the "MA is down at
    # boot" case. Nothing may raise out of start().
    client = MAClient(Config(ma_url=url, player="musicbox", backoff_initial=0.05, backoff_max=0.1))
    await client.start()
    try:
        await asyncio.sleep(0.2)
        assert client.connected is False
        with pytest.raises(MANotConnected):
            await client.command("test/fast")
    finally:
        await client.stop()


@async_test()
async def test_reconnect_does_not_wait_out_a_long_backoff():
    # The exact scenario POST /reconnect exists for, from the README: the token
    # was wrong, the operator minted a new one, and now wants the box back. By
    # then the supervisor is asleep in a long backoff holding no socket at all,
    # so a reconnect that only closed "the socket" would do nothing and the
    # operator would conclude the endpoint is broken.
    async with running_ma() as server:
        client = MAClient(config_for(server, ma_token="wrong", backoff_initial=30.0, backoff_max=30.0))
        await client.start()
        try:
            await asyncio.sleep(0.3)
            assert client.connected is False

            client._config.ma_token = "good-token"
            await client.reconnect()
            assert await client.wait_connected(5)
            assert client.player_id == "snapcast_musicbox"
        finally:
            await client.stop()


@async_test()
async def test_last_error_is_never_cleared_by_a_retry_that_also_fails():
    # /health reads last_error while the box is failing. Clearing it at the top
    # of every attempt left it empty for most of a reconnect loop, so whether
    # you saw the reason depended on when you happened to curl.
    async with running_ma() as server:
        client = MAClient(config_for(server, ma_token="wrong"))
        await client.start()
        try:
            seen = False
            for _ in range(60):
                await asyncio.sleep(0.05)
                if "Invalid or expired token" in client.last_error:
                    seen = True
                elif seen:
                    raise AssertionError(f"last_error was cleared mid-loop: {client.last_error!r}")
            assert seen
            assert client.connect_attempts >= 3
        finally:
            await client.stop()


@async_test()
async def test_events_are_drained_and_never_resolve_a_future():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            for _ in range(50):
                await server.push_event("queue_time_updated", {"elapsed_time": 1})
            await asyncio.sleep(0.2)
            assert client.events_seen >= 50
            assert await client.command("test/fast") == {"fast": True}


@async_test()
async def test_adapters_send_the_expected_commands():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            server.calls.clear()

            await client.play_media("spotify://track/abc", "add")
            await client.play_media("https://example.com/a.mp3", "replace")
            await client.pause()
            await client.resume()
            await client.next_track()
            await client.set_volume(30)
            await client.play_announcement("http://host:8099/sfx/file/airhorn.mp3")

            sent = {name: args for name, args in server.calls}
            assert sent["player_queues/play_media"]["option"] == "replace"
            assert sent["player_queues/play_media"]["queue_id"] == "snapcast_musicbox"
            assert sent["player_queues/pause"] == {"queue_id": "snapcast_musicbox"}
            assert sent["player_queues/resume"]["queue_id"] == "snapcast_musicbox"
            assert sent["players/cmd/volume_set"]["volume_level"] == 30
            announce = sent["players/cmd/play_announcement"]
            # pre_announce, not use_pre_announce: the old name is dropped
            # silently by the server and would let a filename containing "tts"
            # trigger a chime.
            assert announce["pre_announce"] is False
            assert "use_pre_announce" not in announce


@async_test()
async def test_invalid_queue_option_is_rejected_before_it_reaches_ma():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            server.calls.clear()
            with pytest.raises(ValueError):
                await client.play_media("spotify://track/abc", "append")
            assert server.calls == []


@async_test()
async def test_auth_failure_on_a_healthy_socket_is_visible():
    # The socket is fine, the server just rejects every command. /health has to
    # be able to tell that apart from "MA is down".
    async with running_ma(token=None) as server:

        def needs_auth(args):
            from fake_ma import FakeError

            raise FakeError(20, "Authentication required. Please send auth command first.")

        server.handlers["players/get"] = needs_auth
        server.handlers["players/get_by_name"] = needs_auth
        async with connected_client(server, ma_token="") as client:
            assert await client.wait_connected(5)
            assert client.connected is True
            assert client.authenticated is False
            assert "auth" in client.last_error


@async_test()
async def test_probe_info_works_without_a_token():
    async with running_ma() as server:
        async with connected_client(server, ma_token="") as client:
            info = await client.probe_info()
            assert info["server_version"] == "2.9.13"


# ── search and queue reading ──────────────────────────────────────────────────


@async_test()
async def test_search_sends_one_media_type_as_a_list():
    # A bare string instead of a list is a plain text 500 from the real server,
    # with no error code to react to, so the shape of this argument matters more
    # than it looks.
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            results = await client.search("sun in your eyes", "track", 3)
            assert results["tracks"][0]["uri"] == (
                "spotify--asK7Swun://track/57HLbw5C35P2CjpNJ9ALuS"
            )
            args = dict(server.calls)["music/search"]
            assert args["media_types"] == ["track"]
            assert args["search_query"] == "sun in your eyes"
            assert args["limit"] == 3


@async_test()
async def test_search_refuses_a_media_type_before_it_reaches_the_server():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            with pytest.raises(ValueError):
                await client.search("x", "sandwich")
            assert "music/search" not in dict(server.calls)


@async_test()
async def test_search_limit_is_clamped_to_what_spotify_pages_in_one_call():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            await client.search("x", "track", 99)
            assert dict(server.calls)["music/search"]["limit"] == 10
            await client.search("x", "track", 0)
            assert dict(server.calls)["music/search"]["limit"] == 1


@async_test()
async def test_search_with_no_match_returns_empty_lists_not_an_error():
    async with running_ma() as server:
        server.tracks = []
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            results = await client.search("asdfghjkl", "track")
            assert results["tracks"] == []
            # radio is singular on the wire. Reading it as "radios" is a silent
            # KeyError waiting to happen.
            assert results["radio"] == []


@async_test()
async def test_queue_items_returns_the_requested_window():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            items = await client.queue_items("snapcast_musicbox", limit=2, offset=2)
            # Identified by queue_item_id, not by sort_index. sort_index is
            # stamped when an item is added and never renumbered, so on the live
            # box it is neither contiguous nor ordered and cannot tell you which
            # slice you were handed.
            assert [item["queue_item_id"] for item in items] == ["qi2", "qi3"]


@async_test()
async def test_queue_items_for_an_unknown_queue_is_empty_not_an_error():
    async with running_ma() as server:
        async with connected_client(server) as client:
            assert await client.wait_connected(5)
            assert await client.queue_items("nope") == []
