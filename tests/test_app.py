"""HTTP surface: routing, auth, degraded answers, drop modes and sfx serving.

These use a stub MA client rather than the fake server: what is under test here
is the shape of the HTTP layer and how it reacts to MA outcomes, and the stub
lets a test say "this command fails with error_code 11" in one line.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musicbox.app import (  # noqa: E402
    DUP_WINDOW_DEFAULT,
    RESULT_KEYS,
    create_app,
    live_position,
    looks_like_uri,
    resolve_sfx,
)
from musicbox.config import Config  # noqa: E402
from musicbox.ma_client import (  # noqa: E402
    SEARCH_MEDIA_TYPES,
    MAError,
    MANotConnected,
    MATimeout,
)

MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00fake audio payload"

# A search result exactly as MA 2.9.13 puts it on the wire, trimmed to the keys
# musicbox reads. The provider instance id keeps its per-install random suffix
# on purpose (spotify--asK7Swun, not spotify), because that suffix is what
# breaks a naive URI detector.
def track_result(
    name: str = "Sun In Your Eyes",
    item_id: str = "57HLbw5C35P2CjpNJ9ALuS",
    *,
    version: str = "",
    artist: str = "Above & Beyond",
    available: bool = True,
    is_playable: bool = True,
) -> dict:
    return {
        "item_id": item_id,
        "provider": "spotify--asK7Swun",
        "name": name,
        "version": version,
        "uri": f"spotify--asK7Swun://track/{item_id}",
        "is_playable": is_playable,
        "media_type": "track",
        "duration": 291.026,
        "artists": [{"name": artist, "media_type": "artist"}],
        "album": {"name": "Group Therapy", "year": 2011},
        "provider_mappings": [
            {"provider_instance": "spotify--asK7Swun", "available": available}
        ],
    }


def queue_item(position: int, title: str = "Sun In Your Eyes", artist: str = "Above & Beyond") -> dict:
    return {
        "queue_id": "snapcast_musicbox",
        "queue_item_id": f"qi{position}",
        # Pre-composed "artists - title", which is what a QueueItem's name
        # really looks like. Using it as the title is the bug this shape exists
        # to catch.
        "name": f"{artist} - {title}",
        "duration": 291,
        # NOT the position, on purpose. The live box handed back ordinals 0 to 7
        # carrying sort_index 0, 1, 2, 6, 4, 14, 18, 22 with shuffle off, so a
        # sort_index is stamped on add or move and never renumbered afterwards.
        # Offsetting by 100 here keeps it obviously wrong: a test that only
        # passes because position equals sort_index is testing a bug.
        "sort_index": position + 100,
        # Always 0 on the wire, on every item, no matter its real position.
        "index": 0,
        "available": True,
        "media_item": {
            "name": title,
            "version": "",
            "uri": f"spotify--asK7Swun://track/t{position}",
            "artists": [{"name": artist}],
            "album": {"name": "Group Therapy"},
        },
    }


class StubMA:
    """Implements exactly the MAClient surface app.py uses."""

    def __init__(self, connected: bool = True, announcement: bool = True) -> None:
        self.connected = connected
        self.authenticated = connected
        self.server_info = {"server_version": "2.9.13", "schema_version": 28}
        self.player_id = "snapcast_musicbox" if connected else None
        self.player_name = "musicbox" if connected else None
        self.player_features = ["play_media", "play_announcement"] if announcement else ["play_media"]
        self.player_error = None
        self.last_error = "" if connected else "connection refused"
        self.connected_since = time.time() if connected else None
        self.connect_attempts = 1
        self.events_seen = 0

        self.calls: list[tuple] = []
        self.queue = {
            "queue_id": "snapcast_musicbox",
            "state": "playing",
            "items": 3,
            "current_index": 0,
            # The audible track and the track the flow generator has already
            # read ahead to are two different numbers on the real box, and MA's
            # move and delete guards are written against this one. Equal here,
            # which is the normal case; a test that cares moves it forward.
            "index_in_buffer": 0,
            "elapsed_time": 10.0,
            "elapsed_time_last_updated": time.time(),
            "playback_speed": 1.0,
            "shuffle_enabled": False,
            "repeat_mode": "off",
            "current_item": {
                "name": "Test Track",
                "duration": 180,
                "uri": "spotify://track/abc",
                "media_item": {"artists": [{"name": "Tester"}], "album": {"name": "Testing"}},
            },
        }
        self.player = {"volume_level": 42, "volume_muted": False, "playback_state": "playing"}
        self.announce_error: Exception | None = None
        self.queue_error: Exception | None = None
        self.play_media_error: Exception | None = None
        self.move_error: Exception | None = None
        self._added = 0
        # Keyed by media type, so a test can say "there are albums but no
        # tracks" in one line.
        self.search_results: dict[str, list] = {"track": [track_result()]}
        self.items = [queue_item(0), queue_item(1, "Alchemy"), queue_item(2, "Thing Called Love")]

    # lifecycle
    async def start(self) -> None:
        self.calls.append(("start",))

    async def stop(self) -> None:
        self.calls.append(("stop",))

    async def reconnect(self) -> None:
        self.calls.append(("reconnect",))

    async def wait_connected(self, timeout: float) -> bool:
        return self.connected

    async def refresh_player(self, force: bool = False) -> None:
        self.calls.append(("refresh_player", force))

    async def probe_info(self, timeout: float = 2.0) -> dict:
        return {}

    def supports_announcement(self) -> bool:
        return "play_announcement" in self.player_features

    # adapters
    def _guard(self) -> None:
        if not self.connected:
            raise MANotConnected(self.last_error)

    async def get_active_queue(self):
        self._guard()
        if self.queue_error:
            raise self.queue_error
        return self.queue

    async def get_player(self):
        self._guard()
        return self.player

    async def search(self, query: str, media_type: str = "track", limit: int = 5) -> dict:
        self._guard()
        self.calls.append(("search", query, media_type, limit))
        key = {"track": "tracks", "album": "albums", "artist": "artists", "playlist": "playlists"}
        # Every key present and every one a list, like the real SearchResults.
        # `radio` is singular there, and is included so nothing here starts
        # depending on a uniform plural.
        empty = {v: [] for v in key.values()} | {"genres": [], "radio": []}
        return empty | {key[media_type]: list(self.search_results.get(media_type, []))[:limit]}

    async def queue_items(self, queue_id: str, limit: int = 20, offset: int = 0) -> list:
        self._guard()
        self.calls.append(("queue_items", queue_id, limit, offset))
        return self.items[offset : offset + limit]

    # ── the queue as a real list ──────────────────────────────────────────────
    # play_media, move_item and delete_item MUTATE self.items the way MA does,
    # rather than only recording the call. Enqueue position and the fair lane
    # are entirely about WHERE an item ends up, and a stub that answers None and
    # changes nothing cannot tell a working implementation from one that
    # computes the wrong index. The guards are copied from the server too,
    # including the two that succeed while doing nothing, because those are the
    # ones that make musicbox lie about where a song went.

    def _new_item(self, media: str) -> dict:
        self._added += 1
        item = queue_item(900 + self._added, title=f"Added {self._added}")
        item["queue_item_id"] = f"added{self._added}"
        item["media_item"]["uri"] = media
        return item

    def _sync(self) -> None:
        self.queue["items"] = len(self.items)

    async def play_media(self, media: str, option: str, queue_id: str | None = None) -> None:
        self._guard()
        self.calls.append(("play_media", media, option))
        if self.play_media_error is not None:
            raise self.play_media_error
        if self.queue is None:
            return
        item = self._new_item(media)
        current = self.queue.get("current_index") or 0
        buffered = self.queue.get("index_in_buffer")
        anchor = buffered if isinstance(buffered, int) else current
        if option == "replace":
            self.items = [item]
            self.queue["current_index"] = 0
        elif option == "add" and not self.queue.get("shuffle_enabled"):
            self.items.append(item)
        elif option == "add":
            # Shuffle on: MA inserts after the current item and reshuffles the
            # tail, so the landing slot is not the end and is not predictable.
            self.items.insert(anchor + 1, item)
        elif option in ("next", "play"):
            self.items.insert(anchor + 1, item)
        else:  # pragma: no cover - musicbox never sends replace_next
            raise AssertionError(f"unexpected option {option!r}")
        self._sync()

    async def move_item(self, queue_id: str, queue_item_id: str, pos_shift: int) -> None:
        self._guard()
        self.calls.append(("move_item", queue_item_id, pos_shift))
        if self.move_error is not None:
            raise self.move_error
        ids = [item["queue_item_id"] for item in self.items]
        if queue_item_id not in ids:
            raise MAError(3, f"Item {queue_item_id} not found in queue")
        index = ids.index(queue_item_id)
        buffered = self.queue.get("index_in_buffer")
        if isinstance(buffered, int) and index <= buffered:
            # A bare IndexError on the server, which reaches a client as 999.
            raise MAError(999, f"{index} is already played/buffered")
        target = index + pos_shift
        current = self.queue.get("current_index") or 0
        if target < current or target > len(self.items):
            # Silently does nothing and reports success. This is the trap.
            return
        self.items.insert(target, self.items.pop(index))

    async def delete_item(self, queue_id: str, item_id: str) -> None:
        self._guard()
        self.calls.append(("delete_item", item_id))
        ids = [item["queue_item_id"] for item in self.items]
        if item_id not in ids:
            raise MAError(3, f"Item {item_id} not found in queue")
        index = ids.index(item_id)
        buffered = self.queue.get("index_in_buffer")
        if isinstance(buffered, int) and index <= buffered:
            # Logs a line and returns null. Nothing is deleted and the caller is
            # told it worked.
            return
        del self.items[index]
        self._sync()

    async def play_announcement(self, url: str, volume_level=None) -> None:
        self._guard()
        self.calls.append(("play_announcement", url))
        if self.announce_error is not None:
            error, self.announce_error = self.announce_error, None
            raise error

    async def pause(self) -> None:
        self._guard()
        self.calls.append(("pause",))

    async def resume(self) -> None:
        self._guard()
        self.calls.append(("resume",))

    async def next_track(self) -> None:
        self._guard()
        self.calls.append(("next_track",))

    async def set_volume(self, level: int) -> None:
        self._guard()
        self.calls.append(("set_volume", level))


def build(tmp_path: Path, *, token: str = "", ma: StubMA | None = None):
    sfx_dir = tmp_path / "sfx"
    sfx_dir.mkdir(exist_ok=True)
    (sfx_dir / "airhorn.mp3").write_bytes(MP3_BYTES)
    (sfx_dir / "notes.txt").write_text("not audio")
    stub = ma or StubMA()
    config = Config(host="0.0.0.0", port=8099, player="musicbox", sfx_dir=sfx_dir, token=token)
    # TestClient is used without its context manager on purpose: that skips the
    # lifespan, so no real connection is attempted and the stub stays in charge.
    return TestClient(create_app(config, stub)), stub, sfx_dir


# ── health ────────────────────────────────────────────────────────────────────


def test_health_is_open_and_truthful_when_connected(tmp_path):
    client, _, _ = build(tmp_path, token="s3cret")
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["ma_connected"] is True
    assert body["player"] == "snapcast_musicbox"
    assert body["version"]
    assert body["auth_required"] is True
    assert body["sfx_count"] == 1


def test_health_answers_200_when_ma_is_down(tmp_path):
    client, _, _ = build(tmp_path, ma=StubMA(connected=False))
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["ma_connected"] is False
    assert body["player"] is None
    assert body["last_error"] == "connection refused"


# ── auth ──────────────────────────────────────────────────────────────────────


def test_no_token_configured_means_no_auth(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.get("/now").status_code == 200


def test_token_required_when_configured(tmp_path):
    client, _, _ = build(tmp_path, token="s3cret")
    assert client.get("/now").status_code == 401
    assert client.get("/now", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/now", headers={"Authorization": "s3cret"}).status_code == 401
    assert client.get("/now", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_health_and_sfx_file_stay_open_when_a_token_is_set(tmp_path):
    client, _, _ = build(tmp_path, token="s3cret")
    assert client.get("/health").status_code == 200
    # MA fetches this URL itself and cannot present our bearer token.
    assert client.get("/sfx/file/airhorn.mp3").status_code == 200


# ── transport and media ───────────────────────────────────────────────────────


def test_now_reports_track_position_volume_and_queue_length(tmp_path):
    client, _, _ = build(tmp_path)
    body = client.get("/now").json()
    assert body["track"]["title"] == "Test Track"
    assert body["track"]["artist"] == "Tester"
    assert body["track"]["album"] == "Testing"
    assert body["volume"] == 42
    assert body["queue_length"] == 3
    assert body["position"] >= 10.0


def test_queue_and_play_map_to_the_right_queue_option(tmp_path):
    client, stub, _ = build(tmp_path)
    assert client.post("/queue", json={"uri": "spotify://track/abc"}).json()["action"] == "queued"
    assert client.post("/play", json={"url": "https://x/y.mp3"}).json()["action"] == "playing"
    assert ("play_media", "spotify://track/abc", "add") in stub.calls
    assert ("play_media", "https://x/y.mp3", "replace") in stub.calls


def test_queue_requires_uri_or_url(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.post("/queue", json={}).status_code == 400
    assert client.post("/queue", json={"uri": "  "}).status_code == 400


def test_transport_endpoints(tmp_path):
    client, stub, _ = build(tmp_path)
    assert client.post("/skip").status_code == 200
    assert client.post("/pause").status_code == 200
    assert client.post("/resume").status_code == 200
    assert ("next_track",) in stub.calls
    assert ("pause",) in stub.calls
    assert ("resume",) in stub.calls


def test_volume_range_is_validated(tmp_path):
    client, stub, _ = build(tmp_path)
    assert client.post("/volume", json={"level": 55}).status_code == 200
    assert ("set_volume", 55) in stub.calls
    assert client.post("/volume", json={"level": 101}).status_code == 400
    assert client.post("/volume", json={"level": -1}).status_code == 400


def test_reconnect_reports_the_outcome(tmp_path):
    client, stub, _ = build(tmp_path)
    body = client.post("/reconnect").json()
    assert body["ok"] is True
    assert body["player"] == "snapcast_musicbox"
    assert ("reconnect",) in stub.calls


# ── degraded answers ──────────────────────────────────────────────────────────


def test_every_endpoint_gives_a_clean_503_when_ma_is_down(tmp_path):
    client, _, _ = build(tmp_path, ma=StubMA(connected=False))
    for method, path, body in [
        ("get", "/now", None),
        ("post", "/queue", {"uri": "spotify://track/abc"}),
        ("post", "/play", {"uri": "spotify://track/abc"}),
        ("post", "/skip", None),
        ("post", "/pause", None),
        ("post", "/resume", None),
        ("post", "/volume", {"level": 10}),
        ("post", "/drop", {"url": "http://x/y.mp3", "mode": "over"}),
    ]:
        response = getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
        assert response.status_code == 503, path
        payload = response.json()
        assert payload["ok"] is False
        assert payload["error"] == "music_assistant_unavailable"
        assert payload["detail"]


def test_ma_error_codes_map_to_useful_statuses(tmp_path):
    for code, expected in [(2, 404), (13, 422), (8, 409), (11, 503), (23, 502), (999, 502)]:
        stub = StubMA()
        stub.queue_error = MAError(code, "nope", command="player_queues/get_active_queue")
        client, _, _ = build(tmp_path, ma=stub)
        response = client.get("/now")
        assert response.status_code == expected, code
        assert response.json()["code"] == code


def test_ma_timeout_maps_to_504(tmp_path):
    stub = StubMA()
    stub.queue_error = MATimeout("took too long")
    client, _, _ = build(tmp_path, ma=stub)
    response = client.get("/now")
    assert response.status_code == 504
    assert response.json()["error"] == "music_assistant_timeout"


# ── drop ──────────────────────────────────────────────────────────────────────


def test_drop_over_uses_the_announcement_path(tmp_path):
    client, stub, _ = build(tmp_path)
    body = client.post("/drop", json={"url": "http://x/y.mp3", "mode": "over"}).json()
    assert body["mode_used"] == "over"
    assert body["fell_back"] is False
    assert body["note"]
    assert ("play_announcement", "http://x/y.mp3") in stub.calls
    assert ("pause",) not in stub.calls


def test_drop_cut_pauses_and_resumes_around_the_sound(tmp_path):
    client, stub, _ = build(tmp_path)
    body = client.post("/drop", json={"url": "http://x/y.mp3", "mode": "cut"}).json()
    assert body["mode_used"] == "cut"
    assert body["resumed"] is True
    names = [call[0] for call in stub.calls]
    assert names.index("pause") < names.index("play_announcement") < names.index("resume")


def test_drop_cut_does_not_resume_music_that_was_not_playing(tmp_path):
    stub = StubMA()
    stub.queue["state"] = "idle"
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/drop", json={"url": "http://x/y.mp3", "mode": "cut"}).json()
    assert body["resumed"] is False
    assert ("pause",) not in stub.calls
    assert ("resume",) not in stub.calls


def test_drop_over_falls_back_to_cut_when_the_player_lacks_the_feature(tmp_path):
    stub = StubMA(announcement=False)
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/drop", json={"url": "http://x/y.mp3", "mode": "over"}).json()
    assert body["mode_requested"] == "over"
    assert body["mode_used"] == "cut"
    assert body["fell_back"] is True
    assert "play_announcement" in body["reason"]
    assert ("pause",) in stub.calls and ("resume",) in stub.calls


def test_drop_over_falls_back_when_ma_rejects_the_announcement(tmp_path):
    stub = StubMA()
    stub.announce_error = MAError(11, "player refused", command="players/cmd/play_announcement")
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/drop", json={"url": "http://x/y.mp3", "mode": "over"}).json()
    assert body["mode_used"] == "cut"
    assert body["fell_back"] is True
    assert "player refused" in body["reason"]
    # The second attempt, inside the cut path, went through.
    assert [c[0] for c in stub.calls].count("play_announcement") == 2


def test_drop_resumes_the_music_even_if_the_sound_fails(tmp_path):
    stub = StubMA()
    stub.announce_error = MAError(2, "file gone", command="players/cmd/play_announcement")
    client, _, _ = build(tmp_path, ma=stub)
    response = client.post("/drop", json={"url": "http://x/y.mp3", "mode": "cut"})
    assert response.status_code == 404
    assert ("resume",) in stub.calls


def test_drop_cut_still_plays_the_sound_when_the_pause_is_refused(tmp_path):
    # No sound at all is the worst outcome. The music was going to keep playing
    # either way, so degrade to what "over" does rather than 4xx.
    stub = StubMA()

    async def refuse_pause():
        raise MAError(11, "player refused pause", command="player_queues/pause")

    stub.pause = refuse_pause
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/drop", json={"url": "http://x/y.mp3", "mode": "cut"}).json()
    assert body["ok"] is True
    assert body["resumed"] is False
    assert "pause failed" in body["reason"]
    assert ("play_announcement", "http://x/y.mp3") in stub.calls
    assert ("resume",) not in stub.calls


def test_now_tolerates_junk_in_the_queue_payload(tmp_path):
    # These fields come off the wire from a server we do not version-lock.
    stub = StubMA()
    stub.queue["elapsed_time_last_updated"] = "2026-08-15T00:00:00Z"
    stub.queue["current_item"]["media_item"]["artists"] = {"name": "not a list"}
    client, _, _ = build(tmp_path, ma=stub)
    response = client.get("/now")
    assert response.status_code == 200
    body = response.json()
    assert body["position"] is None
    assert body["track"]["artist"] is None


def test_drop_validates_mode_and_url_scheme(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.post("/drop", json={"url": "http://x/y.mp3", "mode": "sideways"}).status_code == 400
    assert client.post("/drop", json={"url": "/var/lib/musicbox/sfx/a.mp3"}).status_code == 400
    assert client.post("/drop", json={"url": "file:///tmp/a.mp3"}).status_code == 400


# ── sfx ───────────────────────────────────────────────────────────────────────


def test_sfx_listing_only_shows_audio_and_carries_fetchable_urls(tmp_path):
    client, _, _ = build(tmp_path)
    body = client.get("/sfx", headers={"Host": "pi5:8099"}).json()
    assert body["count"] == 1
    entry = body["sfx"][0]
    assert entry["name"] == "airhorn"
    assert entry["url"] == "http://pi5:8099/sfx/file/airhorn.mp3"


def test_sfx_play_hands_ma_a_url_pointing_back_at_us(tmp_path):
    client, stub, _ = build(tmp_path)
    body = client.post("/sfx/airhorn", headers={"Host": "100.64.0.5:8099"}).json()
    assert body["sfx"] == "airhorn"
    assert ("play_announcement", "http://100.64.0.5:8099/sfx/file/airhorn.mp3") in stub.calls


def test_sfx_base_url_override_wins_over_the_host_header(tmp_path):
    sfx_dir = tmp_path / "sfx2"
    sfx_dir.mkdir()
    (sfx_dir / "airhorn.mp3").write_bytes(MP3_BYTES)
    stub = StubMA()
    config = Config(
        host="0.0.0.0",
        port=8099,
        player="musicbox",
        sfx_dir=sfx_dir,
        sfx_base_url="http://10.88.0.1:8099",
    )
    client = TestClient(create_app(config, stub))
    client.post("/sfx/airhorn", headers={"Host": "127.0.0.1:8099"})
    assert ("play_announcement", "http://10.88.0.1:8099/sfx/file/airhorn.mp3") in stub.calls


def test_sfx_play_accepts_cut_mode(tmp_path):
    client, stub, _ = build(tmp_path)
    body = client.post("/sfx/airhorn?mode=cut").json()
    assert body["mode_used"] == "cut"
    assert ("pause",) in stub.calls


def test_sfx_play_accepts_mode_in_the_body_like_drop_does(tmp_path):
    # The README and examples/sfx-drop.sh send it this way, and a caller who
    # copied the /drop shape would otherwise get a silent "over".
    client, stub, _ = build(tmp_path)
    body = client.post("/sfx/airhorn", json={"mode": "cut"}).json()
    assert body["mode_used"] == "cut"
    assert ("pause",) in stub.calls


def test_sfx_play_with_no_body_at_all_defaults_to_over(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.post("/sfx/airhorn").json()["mode_used"] == "over"


def test_sfx_play_rejects_an_unknown_mode_in_the_body(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.post("/sfx/airhorn", json={"mode": "sideways"}).status_code == 400


def test_sfx_urls_escape_spaces_so_ma_can_fetch_them(tmp_path):
    client, stub, sfx_dir = build(tmp_path)
    (sfx_dir / "air horn 2.mp3").write_bytes(MP3_BYTES)

    listing = client.get("/sfx", headers={"Host": "pi5:8099"}).json()
    urls = {entry["url"] for entry in listing["sfx"]}
    assert "http://pi5:8099/sfx/file/air%20horn%202.mp3" in urls

    client.post("/sfx/air horn 2", headers={"Host": "pi5:8099"})
    assert ("play_announcement", "http://pi5:8099/sfx/file/air%20horn%202.mp3") in stub.calls
    # And the escaped URL is the one that actually serves the file back.
    assert client.get("/sfx/file/air%20horn%202.mp3").status_code == 200


def test_unknown_sfx_is_a_404_not_a_crash(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.post("/sfx/nope").status_code == 404
    assert client.get("/sfx/file/nope.mp3").status_code == 404
    # A non audio file in the directory is not addressable.
    assert client.post("/sfx/notes").status_code == 404


def test_sfx_names_cannot_escape_the_directory(tmp_path):
    client, _, _ = build(tmp_path)
    secret = tmp_path / "secret.mp3"
    secret.write_bytes(b"do not serve me")
    for name in ["../secret.mp3", "..%2Fsecret.mp3", "/etc/passwd"]:
        response = client.get(f"/sfx/file/{name}")
        # 404 from our own lookup, or 405/404 after the client collapses the
        # dot segments and lands on a different route. Either way the file
        # outside the sfx directory is never in the body.
        assert response.status_code in (404, 405), name
        assert b"do not serve me" not in response.content


def test_sfx_file_is_served_with_a_content_length(tmp_path):
    client, _, _ = build(tmp_path)
    response = client.get("/sfx/file/airhorn.mp3")
    assert response.status_code == 200
    assert response.content == MP3_BYTES
    # MA needs a real length to probe the duration, otherwise it raises
    # "Announcement duration could not be determined".
    assert int(response.headers["content-length"]) == len(MP3_BYTES)


def test_sfx_file_answers_head(tmp_path):
    # FastAPI does not answer HEAD on a GET route by itself, and a fetcher that
    # probes before downloading would get a 405.
    client, _, _ = build(tmp_path)
    response = client.head("/sfx/file/airhorn.mp3")
    assert response.status_code == 200
    assert int(response.headers["content-length"]) == len(MP3_BYTES)
    assert response.content == b""
    assert client.head("/sfx/file/nope.mp3").status_code == 404


def test_openapi_generates_without_warnings(tmp_path):
    # A duplicate operation id warns on every startup, and a warning that is
    # always present is a warning nobody reads.
    import warnings

    client, _, _ = build(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client.app.openapi()
    assert [str(w.message) for w in caught] == []


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_live_position_advances_only_while_playing():
    playing = {"state": "playing", "elapsed_time": 10.0, "elapsed_time_last_updated": time.time() - 5}
    assert 14.5 <= live_position(playing) <= 15.5
    paused = {"state": "paused", "elapsed_time": 10.0, "elapsed_time_last_updated": time.time() - 5}
    assert live_position(paused) == 10.0
    assert live_position(None) is None
    assert live_position({"state": "idle"}) is None


def test_resolve_sfx_matches_name_or_filename_case_insensitively(tmp_path):
    sfx_dir = tmp_path / "s"
    sfx_dir.mkdir()
    (sfx_dir / "AirHorn.mp3").write_bytes(MP3_BYTES)
    assert resolve_sfx(sfx_dir, "airhorn").name == "AirHorn.mp3"
    assert resolve_sfx(sfx_dir, "AirHorn.mp3").name == "AirHorn.mp3"
    assert resolve_sfx(sfx_dir, "") is None
    assert resolve_sfx(tmp_path / "missing", "airhorn") is None


def test_missing_sfx_dir_is_not_fatal(tmp_path):
    stub = StubMA()
    config = Config(host="127.0.0.1", port=8099, player="musicbox", sfx_dir=tmp_path / "gone")
    client = TestClient(create_app(config, stub))
    assert client.get("/health").json()["sfx_count"] == 0
    assert client.get("/sfx").json()["count"] == 0


def test_the_request_log_keeps_a_hash_in_the_path(tmp_path, capsys):
    # `big airhorn #2.mp3` is an ordinary thing to drag into a folder, and
    # starlette's request.url.path drops everything from the '#' onward because
    # it re-splits a URL string it built from the scope. The request itself
    # routes fine; only the log line lied, and it lied in the direction of
    # "there is no sfx by that name", which is a bad hour to spend at an event.
    client, _, sfx_dir = build(tmp_path)
    (sfx_dir / "big airhorn #2.mp3").write_bytes(MP3_BYTES)
    capsys.readouterr()
    response = client.post("/sfx/big%20airhorn%20%232", json={"mode": "over"})
    assert response.status_code == 200
    line = [l for l in capsys.readouterr().out.splitlines() if "event=request" in l][-1]
    assert "big airhorn #2" in line


# ── jukebox: search, free text resolution and the queue listing ───────────────


def test_search_returns_the_fields_a_caller_can_act_on(tmp_path):
    client, stub, _ = build(tmp_path)
    body = client.get("/search", params={"q": "Sun in Your Eyes"}).json()
    assert body["ok"] is True
    assert body["query"] == "Sun in Your Eyes"
    assert body["type"] == "track"
    assert body["count"] == 1
    result = body["results"][0]
    assert result["title"] == "Sun In Your Eyes"
    assert result["artist"] == "Above & Beyond"
    assert result["album"] == "Group Therapy"
    assert result["uri"] == "spotify--asK7Swun://track/57HLbw5C35P2CjpNJ9ALuS"
    assert result["media_type"] == "track"
    assert result["playable"] is True
    # A float on the wire (291.026), an int here, so that /search and /queue
    # agree with each other.
    assert result["duration"] == 291
    # Exactly one media type is asked for, never MA's default of all eight.
    assert ("search", "Sun in Your Eyes", "track", 5) in stub.calls


def test_search_with_no_results_is_a_200_and_an_empty_list(tmp_path):
    # Not a 404. "Nobody recorded that" and "the provider is down" arrive here
    # as the same empty list, and neither is an error in the HTTP sense.
    client, stub, _ = build(tmp_path)
    stub.search_results = {"track": []}
    body = client.get("/search", params={"q": "nnnope"}).json()
    assert body["ok"] is True
    assert body["count"] == 0
    assert body["results"] == []


def test_search_rejects_a_media_type_music_assistant_would_500_on(tmp_path):
    client, _, _ = build(tmp_path)
    response = client.get("/search", params={"q": "x", "type": "sandwich"})
    assert response.status_code == 400
    assert "track" in response.json()["detail"]


def test_search_limit_is_capped_rather_than_refused(tmp_path):
    # A caller asking for 50 would become five sequential throttled Spotify
    # calls and blow the command timeout. Capping is friendlier than a 400.
    client, stub, _ = build(tmp_path)
    client.get("/search", params={"q": "x", "limit": 50})
    assert ("search", "x", "track", 10) in stub.calls


def test_title_carries_the_version_so_two_mixes_are_distinguishable(tmp_path):
    client, stub, _ = build(tmp_path)
    stub.search_results = {"track": [track_result(version="Marsh Remix")]}
    body = client.get("/search", params={"q": "sun in your eyes"}).json()
    assert body["results"][0]["title"] == "Sun In Your Eyes (Marsh Remix)"


def test_a_provider_mapping_without_an_available_key_is_playable(tmp_path):
    """Absent means available, because that is the field's declared default.

    ProviderMapping.available is `bool = True` in music_assistant_models.
    Reading it with no default turned a missing key into None, which is falsy,
    which made every result from a provider that did not serialize it
    unplayable. At an event that is the box refusing every song while blaming
    region licensing, which is the last place anyone would look.
    """
    client, stub, _ = build(tmp_path)
    item = track_result()
    for mapping in item["provider_mappings"]:
        mapping.pop("available")
    stub.search_results = {"track": [item]}
    body = client.get("/search", params={"q": "sun"}).json()
    assert body["results"][0]["playable"] is True
    assert client.post("/queue", json={"query": "sun"}).status_code == 200


def test_a_region_blocked_track_is_not_playable_despite_is_playable_true(tmp_path):
    # The top level is_playable is true on every Spotify result including the
    # ones that fail at play time. provider_mappings[].available is the field
    # that carries the real answer.
    client, stub, _ = build(tmp_path)
    stub.search_results = {"track": [track_result(available=False)]}
    body = client.get("/search", params={"q": "sun in your eyes"}).json()
    assert body["results"][0]["playable"] is False


def test_free_text_play_resolves_and_says_what_it_landed_on(tmp_path):
    client, stub, _ = build(tmp_path)
    body = client.post("/play", json={"query": "Above and Beyond Sun in Your Eyes"}).json()
    assert body["action"] == "playing"
    assert body["query"] == "Above and Beyond Sun in Your Eyes"
    assert body["resolved"] == {
        "title": "Sun In Your Eyes",
        "artist": "Above & Beyond",
        "uri": "spotify--asK7Swun://track/57HLbw5C35P2CjpNJ9ALuS",
    }
    assert (
        "play_media",
        "spotify--asK7Swun://track/57HLbw5C35P2CjpNJ9ALuS",
        "replace",
    ) in stub.calls


def test_free_text_queue_appends_and_reports_the_match(tmp_path):
    client, stub, _ = build(tmp_path)
    body = client.post("/queue", json={"query": "baile de favela"}).json()
    assert body["action"] == "queued"
    assert body["resolved"]["title"] == "Sun In Your Eyes"
    assert ("play_media", body["media"], "add") in stub.calls


def test_free_text_in_the_uri_field_is_searched_anyway(tmp_path):
    # Models and humans both put song names in `uri`. Searching it beats a 404
    # that reads like the box is broken.
    client, stub, _ = build(tmp_path)
    body = client.post("/queue", json={"uri": "some song name"}).json()
    assert body["query"] == "some song name"
    assert body["resolved"] is not None


def test_a_uri_is_passed_through_untouched_and_never_searched(tmp_path):
    client, stub, _ = build(tmp_path)
    body = client.post("/play", json={"uri": "spotify--asK7Swun://track/abc"}).json()
    assert body["resolved"] is None
    assert body["query"] is None
    assert body["media"] == "spotify--asK7Swun://track/abc"
    assert not [call for call in stub.calls if call[0] == "search"]


def test_nothing_found_is_a_404_naming_the_query(tmp_path):
    client, stub, _ = build(tmp_path)
    stub.search_results = {"track": []}
    response = client.post("/queue", json={"query": "asdfghjkl"})
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "no_playable_match"
    assert "asdfghjkl" in body["detail"]
    assert not [call for call in stub.calls if call[0] == "play_media"]


def test_results_that_exist_but_are_all_unplayable_are_their_own_404(tmp_path):
    # Deliberately a different sentence from "nothing found": it sends the
    # person asking somewhere completely different.
    client, stub, _ = build(tmp_path)
    stub.search_results = {"track": [track_result(available=False)]}
    response = client.post("/play", json={"query": "sun in your eyes"})
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "none of them can be played" in detail
    assert "sun in your eyes" in detail


def test_the_first_playable_result_wins_not_the_first_result(tmp_path):
    client, stub, _ = build(tmp_path)
    stub.search_results = {
        "track": [
            track_result("Blocked", "aaa", available=False),
            track_result("Playable", "bbb"),
        ]
    }
    body = client.post("/queue", json={"query": "whatever"}).json()
    assert body["resolved"]["title"] == "Playable"


def test_play_and_queue_still_reject_an_empty_body(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.post("/queue", json={}).status_code == 400
    assert client.post("/queue", json={"uri": "  "}).status_code == 400


def test_a_blank_uri_does_not_hide_a_real_query(tmp_path):
    # A caller that fills in every field and blanks the ones it is not using.
    # Taking the first TRUTHY field made a single space beat a perfectly good
    # query, so {"uri": ""} worked and {"uri": " "} was a 400. Nobody can debug
    # that difference from outside the box.
    client, _, _ = build(tmp_path)
    body = client.post("/queue", json={"uri": " ", "query": "baile de favela"})
    assert body.status_code == 200
    assert body.json()["query"] == "baile de favela"
    assert client.post("/play", json={"uri": "", "url": "\t", "query": "sun"}).status_code == 200


def test_a_search_longer_than_any_real_request_is_refused_without_being_echoed(tmp_path):
    # A model looping, or somebody pasting a document. It costs a Spotify round
    # trip and a 600 s cache entry, and echoing it back would put the whole
    # thing in the error body and from there into a context window.
    client, stub, _ = build(tmp_path)
    huge = "x" * 5000
    response = client.post("/play", json={"query": huge})
    assert response.status_code == 400
    assert "5000 characters" in response.json()["detail"]
    assert huge not in response.text
    assert len(response.text) < 400
    assert not [call for call in stub.calls if call[0] == "search"]


def test_the_two_media_type_lists_cannot_drift_apart():
    # app.py validates against RESULT_KEYS because that is the dict it
    # subscripts; ma_client validates against SEARCH_MEDIA_TYPES because that is
    # what goes on the wire. Two lists, and the day they disagree the odd one
    # out is a KeyError and a 500 on a request that looked fine.
    assert tuple(RESULT_KEYS) == tuple(SEARCH_MEDIA_TYPES)


def test_limit_is_coerced_the_way_a_caller_means_it(tmp_path):
    # Every query string value is text, and a model sends whichever of 5, "5"
    # and 5.0 its schema lets through. All three mean the same thing.
    client, stub, _ = build(tmp_path)
    for value in ("3", 3, 3.0, 2.6):
        stub.calls.clear()
        assert client.get("/search", params={"q": "x", "limit": value}).status_code == 200
        assert ("search", "x", "track", 3) in stub.calls


def test_a_limit_that_is_not_a_number_is_one_sentence_not_a_pydantic_dump(tmp_path):
    # Every other bad argument in this app answers 400 with a sentence. A plain
    # `limit: int` annotation made this one a 422 whose detail is a list of
    # pydantic error objects, which is also what the stdio MCP proxy would have
    # relayed to a model.
    client, _, _ = build(tmp_path)
    for path in ("/search", "/queue"):
        params = {"limit": "lots"} | ({"q": "x"} if path == "/search" else {})
        response = client.get(path, params=params)
        assert response.status_code == 400
        assert response.json()["detail"] == "limit must be a number, got 'lots'"


def test_get_queue_lists_upcoming_items_current_first(tmp_path):
    client, stub, _ = build(tmp_path)
    body = client.get("/queue").json()
    assert body["ok"] is True
    assert body["count"] == 3
    assert body["index"] == 0
    assert body["upcoming"] == 3
    assert [item["position"] for item in body["items"]] == [0, 1, 2]
    first = body["items"][0]
    # Title and artist come from media_item. The QueueItem's own `name` is the
    # composed "Above & Beyond - Sun In Your Eyes" and using it would render the
    # artist twice.
    assert first["title"] == "Sun In Your Eyes"
    assert first["artist"] == "Above & Beyond"
    assert first["uri"] == "spotify--asK7Swun://track/t0"
    assert first["duration"] == 291


def test_get_queue_asks_for_the_window_starting_at_the_current_index(tmp_path):
    client, stub, _ = build(tmp_path)
    stub.queue = dict(stub.queue, current_index=2, items=3)
    body = client.get("/queue", params={"limit": 5}).json()
    assert ("queue_items", "snapcast_musicbox", 5, 2) in stub.calls
    assert body["upcoming"] == 1
    assert [item["position"] for item in body["items"]] == [2]


def test_get_queue_counts_position_off_the_offset_and_ignores_sort_index(tmp_path):
    # Live regression. MA returns the window in queue order but each item's
    # sort_index is whatever it was stamped with when it was added or moved:
    # the box returned ordinals 0 to 7 with sort_index 0, 1, 2, 6, 4, 14, 18, 22
    # and shuffle off. Reading position off sort_index told the person standing
    # there that their song was ninetieth when it was twenty fourth.
    client, stub, _ = build(tmp_path)
    stub.items = [queue_item(i) for i in range(10)]
    for scrambled, item in zip((0, 1, 2, 6, 4, 14, 18, 22, 3, 99), stub.items):
        item["sort_index"] = scrambled
    stub.queue = dict(stub.queue, items=10, current_index=5)

    body = client.get("/queue", params={"limit": 5}).json()

    # Counted from the offset that was asked for, not read off the items.
    assert [item["position"] for item in body["items"]] == [5, 6, 7, 8, 9]
    # And the first item really is the one MA considers current, which is what
    # makes counting from current_index the right thing to do.
    assert body["index"] == 5
    assert body["items"][0]["uri"] == "spotify--asK7Swun://track/t5"


def test_get_queue_reports_the_honest_total_when_the_window_is_smaller(tmp_path):
    client, stub, _ = build(tmp_path)
    stub.items = [queue_item(i) for i in range(30)]
    stub.queue = dict(stub.queue, items=30, current_index=0)
    body = client.get("/queue", params={"limit": 2}).json()
    assert body["count"] == 30
    assert body["upcoming"] == 30
    assert len(body["items"]) == 2


def test_get_queue_when_the_queue_is_empty(tmp_path):
    client, stub, _ = build(tmp_path)
    stub.items = []
    # An idle box that has finished everything: items 0 and a null index, which
    # is what MA sends on a queue that has never played or has emptied itself.
    stub.queue = dict(stub.queue, items=0, current_index=None, state="idle")
    body = client.get("/queue").json()
    assert body["ok"] is True
    assert body["count"] == 0
    assert body["upcoming"] == 0
    assert body["items"] == []
    assert body["index"] is None


def test_get_queue_when_there_is_no_active_queue_at_all(tmp_path):
    client, stub, _ = build(tmp_path)
    stub.queue = None
    body = client.get("/queue").json()
    assert body == {"ok": True, "queue_id": None, "count": 0, "index": None, "upcoming": 0, "items": []}


def test_a_queue_item_without_a_media_item_still_renders(tmp_path):
    # A raw stream URL queued through the builtin provider has no media_item.
    client, stub, _ = build(tmp_path)
    stub.items = [
        {
            "queue_item_id": "qi9",
            "name": "Some Radio - The Show",
            "duration": None,
            "sort_index": 0,
            "media_item": None,
            "available": True,
        }
    ]
    stub.queue = dict(stub.queue, items=1, current_index=0)
    item = client.get("/queue").json()["items"][0]
    assert item["title"] == "The Show"
    assert item["artist"] == "Some Radio"
    assert item["duration"] is None
    # No media item means no uri, and `uri` says so instead of being backfilled
    # with the queue_item_id. The two are different kinds of handle: a caller
    # can hand a uri straight back to /play, and handing back a queue_item_id
    # gets it searched as free text and plays whatever "qi9" happens to match.
    assert item["uri"] is None
    # The id is still there, under its own name, for delete_item and move_item.
    assert item["queue_item_id"] == "qi9"


# ── uri detection ─────────────────────────────────────────────────────────────


def test_looks_like_uri_says_yes_only_to_real_uris():
    assert looks_like_uri("http://example.com/a.mp3")
    assert looks_like_uri("https://open.spotify.com/track/57HLbw5C35P2CjpNJ9ALuS")
    # The scheme is a provider INSTANCE id and carries a per-install random
    # suffix after a double hyphen. A detector that assumed [a-z]+:// fails here.
    assert looks_like_uri("spotify--asK7Swun://track/57HLbw5C35P2CjpNJ9ALuS")
    assert looks_like_uri("library://track/123")
    assert looks_like_uri("spotify:track:57HLbw5C35P2CjpNJ9ALuS")


def test_looks_like_uri_says_no_to_anything_a_person_would_say():
    # The bare word is the bug this whole function exists for: "toca spotify"
    # must search for the word, not try to open a scheme.
    assert not looks_like_uri("spotify")
    assert not looks_like_uri("spotify://")
    assert not looks_like_uri("Above and Beyond Sun in Your Eyes")
    # Two colons in ordinary prose is not a Spotify URI.
    assert not looks_like_uri("toca isso: agora: vai")
    assert not looks_like_uri("track:")
    assert not looks_like_uri("")
    assert not looks_like_uri("   ")
    # A song title that happens to contain a colon.
    assert not looks_like_uri("Blade Runner 2049: Mesa")
    # Whitespace is the highest value reject for a PROVIDER uri.
    assert not looks_like_uri("spotify--asK7Swun://track/abc and more")
    # Text that merely mentions a link is still text: it does not START with
    # the scheme, which is the only thing that short circuits the reject.
    assert not looks_like_uri("toca https://example.com/a.mp3 please")
    assert not looks_like_uri("play http://example.com/a.mp3")


def test_a_url_with_a_space_in_it_is_still_a_url(tmp_path):
    """An operator's own file URL, unescaped, must not be searched for.

    `http://box:8099/sfx/file/my song.mp3` is what a file dropped in with
    Finder looks like once it is a URL, and it is the shape POST /play {"url":
    ...} carried untouched before the box learned to search. Classifying it as
    free text answered 200 having played an unrelated song: the caller asked
    for one specific file and got somebody else's track with nothing in the
    response to say so. A malformed URL handed to Music Assistant fails out
    loud instead, which is the direction this has to fail in.
    """
    assert looks_like_uri("http://box:8099/sfx/file/my song.mp3")
    client, stub, _ = build(tmp_path)
    body = client.post("/play", json={"url": "http://box:8099/sfx/file/my song.mp3"}).json()
    assert body["media"] == "http://box:8099/sfx/file/my song.mp3"
    assert body["resolved"] is None
    assert not [call for call in stub.calls if call[0] == "search"]


# ── enqueue position, duplicates and the fair lane ────────────────────────────
# The live failure these exist for: an agent loaded a 213 item Spotify playlist,
# so every request from a person in the room was appended at 214 and never
# played. The box answered 200 every time and silently did nothing.

TRACK_URI = "spotify--asK7Swun://track/57HLbw5C35P2CjpNJ9ALuS"


def loaded(stub: StubMA, total: int = 213, current: int = 5) -> StubMA:
    """A stub carrying a background playlist the size of the real one."""
    stub.items = [queue_item(i) for i in range(total)]
    stub.queue = dict(
        stub.queue, items=total, current_index=current, index_in_buffer=current
    )
    return stub


def ids_of(stub: StubMA) -> list[str]:
    return [item["queue_item_id"] for item in stub.items]


def test_queue_still_appends_when_no_position_is_given(tmp_path):
    # The default is the whole compatibility story: every caller written before
    # positions existed sends no position and must keep getting "add".
    client, stub, _ = build(tmp_path, ma=loaded(StubMA()))
    body = client.post("/queue", json={"uri": TRACK_URI}).json()
    assert body["action"] == "queued"
    assert body["position"] == "end"
    assert ("play_media", TRACK_URI, "add") in stub.calls
    assert ids_of(stub)[-1] == "added1"
    # And it says out loud how far away that is, which is the number nobody had
    # before: 213 items, current 5, so the request is 208 songs away.
    assert body["queue_position"] == 213
    assert body["plays_after"] == 208


def test_position_next_puts_it_after_the_current_track(tmp_path):
    client, stub, _ = build(tmp_path, ma=loaded(StubMA()))
    body = client.post("/queue", json={"uri": TRACK_URI, "position": "next"}).json()
    assert ("play_media", TRACK_URI, "next") in stub.calls
    assert ids_of(stub)[6] == "added1"
    assert body["plays_after"] == 1
    # Never claimed as exact: in flow mode MA anchors on index_in_buffer, so a
    # request made as a track ends lands one slot further on.
    assert body["queue_position_exact"] is False


def test_position_now_interrupts_but_keeps_the_rest_of_the_queue(tmp_path):
    client, stub, _ = build(tmp_path, ma=loaded(StubMA()))
    body = client.post("/queue", json={"uri": TRACK_URI, "position": "now"}).json()
    assert body["action"] == "playing"
    assert body["queue_cleared"] is False
    assert ("play_media", TRACK_URI, "play") in stub.calls
    assert len(stub.items) == 214


def test_position_replace_wipes_the_queue(tmp_path):
    client, stub, _ = build(tmp_path, ma=loaded(StubMA()))
    body = client.post("/queue", json={"uri": TRACK_URI, "position": "replace"}).json()
    assert body["action"] == "playing"
    assert body["queue_cleared"] is True
    assert ("play_media", TRACK_URI, "replace") in stub.calls
    assert len(stub.items) == 1


def test_position_fair_lands_in_front_of_the_filler(tmp_path):
    client, stub, _ = build(tmp_path, ma=loaded(StubMA()))
    body = client.post("/queue", json={"uri": TRACK_URI, "position": "fair"}).json()
    assert body["action"] == "queued"
    # Appended first, then moved up. That direction is deliberate: the append
    # lands far past the buffered region, so the move cannot be refused, and a
    # playhead that advances mid-operation leaves the item at the end rather
    # than suddenly playing it.
    assert ("play_media", TRACK_URI, "add") in stub.calls
    assert ("move_item", "added1", 6 - 213) in stub.calls
    assert ids_of(stub)[6] == "added1"
    assert body["queue_position"] == 6
    assert body["queue_position_exact"] is True
    assert body["plays_after"] == 1


def test_fair_requests_play_in_the_order_they_were_asked_for(tmp_path):
    """The reason "fair" exists at all.

    Straight "next" puts each new request in front of the previous one, so ten
    people are served in reverse and whoever asked first waits longest. These
    three must come out 6, 7, 8.
    """
    client, stub, _ = build(tmp_path, ma=loaded(StubMA()))
    for n in range(3):
        client.post("/queue", json={"uri": f"spotify--asK7Swun://track/req{n}", "position": "fair"})
    assert ids_of(stub)[6:9] == ["added1", "added2", "added3"]
    # And the filler that was at 6 is still right behind them.
    assert stub.items[9]["queue_item_id"] == "qi6"


def test_a_request_that_already_played_does_not_warn_the_next_one(tmp_path):
    """The lane warning has to mean something, so it must not fire every time.

    Ids stay remembered after their song has played, and a played item sits
    BEHIND the playhead where the 50 item window never looks. Counting "ids I
    did not find" as "lane I could not see the end of" therefore warned on every
    request from the second one of the evening onwards, on a queue that is always
    longer than the window. A warning that is always on is noise, and it was
    being read out to people.
    """
    stub = loaded(StubMA())
    client, _, _ = build(tmp_path, ma=stub)
    first = client.post("/queue", json={"uri": TRACK_URI, "position": "fair"}).json()
    assert first["note"] is None

    # The evening moves on and that request has played.
    stub.queue["current_index"] = 30
    stub.queue["index_in_buffer"] = 30
    second = client.post(
        "/queue", json={"uri": "spotify--asK7Swun://track/other", "position": "fair"}
    ).json()
    assert second["note"] is None
    assert second["queue_position"] == 31


def test_a_lane_running_past_the_window_still_warns(tmp_path):
    """And the real case must survive, otherwise the fix is just deleting it.

    The lane can only outrun the 50 item window by stacking 50 requests in front
    of the playhead, and then its tail is the last thing the window can see. That
    is the signal: a lane member sitting at the far edge of the window means the
    lane may continue past it, so this request may jump an earlier one.
    """
    stub = loaded(StubMA())
    client, _, _ = build(tmp_path, ma=stub)

    # Fill the lane so that a remembered item sits on the last slot the window
    # reads: current 5 and a 50 item window means the far edge is index 54, and
    # requests land from index 6 up.
    for n in range(DUP_WINDOW_DEFAULT):
        client.post(
            "/queue", json={"uri": f"spotify--asK7Swun://track/req{n}", "position": "fair"}
        )
    body = client.post(
        "/queue", json={"uri": "spotify--asK7Swun://track/late", "position": "fair"}
    ).json()
    assert body["note"] is not None
    assert "may play before a request that was made earlier" in body["note"]


def test_next_counts_from_the_buffered_track_not_the_audible_one(tmp_path):
    """In flow mode MA anchors an insert on index_in_buffer, so must the answer.

    _enqueue_with_option on the box reads
    `cur_index = index_in_buffer if index_in_buffer is not None else current_index`
    while the queue is playing or paused, and inserts at cur_index + 1. With the
    generator a track ahead of the speaker, saying "plays in 1 song" for
    something that plays in 2 is a promise musicbox cannot keep.
    """
    stub = loaded(StubMA())
    stub.queue["index_in_buffer"] = 6
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": TRACK_URI, "position": "next"}).json()
    assert ids_of(stub)[7] == "added1"
    assert body["queue_position"] == 7
    assert body["plays_after"] == 2


def test_fair_degrades_to_next_when_the_lane_memory_is_gone(tmp_path):
    """A restart loses the in-process lane, and that is stated, not hidden.

    MA offers no way to mark an item as ours (play_media cannot set
    extra_attributes, and every live item has an empty dict there), so the ids
    are remembered in process or not at all. Losing them means a new request
    lands right after the current track instead of behind the earlier ones.
    """
    stub = loaded(StubMA())
    client, _, _ = build(tmp_path, ma=stub)
    client.post("/queue", json={"uri": TRACK_URI, "position": "fair"})
    # A fresh app is a fresh lane, against the same queue.
    client2, _, _ = build(tmp_path, ma=stub)
    body = client2.post("/queue", json={"uri": "spotify--asK7Swun://track/other", "position": "fair"}).json()
    assert body["queue_position"] == 6
    assert stub.items[6]["queue_item_id"] == "added2"


def test_fair_says_so_when_the_move_is_refused_by_the_buffer(tmp_path):
    # MA raises a bare IndexError for anything at or before index_in_buffer,
    # which arrives as error code 999. It is an expected outcome near a track
    # boundary, not a crash, and the song is still queued.
    stub = loaded(StubMA())
    stub.move_error = MAError(999, "212 is already played/buffered")
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": TRACK_URI, "position": "fair"}).json()
    assert body["action"] == "queued"
    assert body["queue_position"] == 213
    assert "buffered" in body["note"]


def test_fair_does_not_claim_a_position_when_the_move_silently_did_nothing(tmp_path):
    # move_item returns null for a destination out of range, having moved
    # nothing. Trusting that reply is how a caller reports a position that is
    # not where the song is.
    stub = loaded(StubMA())
    client, _, _ = build(tmp_path, ma=stub)
    original = stub.move_item

    async def refuse(queue_id, queue_item_id, pos_shift):
        stub.calls.append(("move_item", queue_item_id, pos_shift))

    stub.move_item = refuse
    body = client.post("/queue", json={"uri": TRACK_URI, "position": "fair"}).json()
    stub.move_item = original
    assert body["queue_position"] is None
    assert body["queue_position_exact"] is False
    assert "could not confirm" in body["note"]


def test_fair_on_an_empty_queue_just_adds(tmp_path):
    client, stub, _ = build(tmp_path)
    stub.items = []
    stub.queue = dict(stub.queue, items=0, current_index=None, index_in_buffer=None, state="idle")
    body = client.post("/queue", json={"uri": TRACK_URI, "position": "fair"}).json()
    assert body["queue_position"] == 0
    # Nothing to jump over, so no move at all rather than a pos_shift of 0,
    # which MA reads as "move to the top" and is not a no-op.
    assert not [call for call in stub.calls if call[0] == "move_item"]


def test_an_unknown_position_is_refused_and_nothing_is_queued(tmp_path):
    client, stub, _ = build(tmp_path, ma=loaded(StubMA()))
    response = client.post("/queue", json={"uri": TRACK_URI, "position": "first"})
    assert response.status_code == 400
    assert "position must be one of" in response.json()["detail"]
    assert not [call for call in stub.calls if call[0] == "play_media"]


def test_replace_next_is_not_reachable_by_name(tmp_path):
    # It deletes everything after the current item. On the live queue that is
    # the whole evening, and no phrase anybody says maps to it.
    client, _, _ = build(tmp_path, ma=loaded(StubMA()))
    assert client.post("/queue", json={"uri": TRACK_URI, "position": "replace_next"}).status_code == 400


# ── duplicates ────────────────────────────────────────────────────────────────


def test_a_track_already_waiting_is_not_queued_twice(tmp_path):
    stub = loaded(StubMA())
    stub.items[40]["media_item"]["uri"] = TRACK_URI
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": TRACK_URI}).json()
    assert body["ok"] is True
    assert body["action"] == "already_queued"
    assert body["queue_position"] == 40
    # 35 songs away, counted from the audible track and not from the top of the
    # queue. This is the number somebody standing at the speaker is told.
    assert body["plays_after"] == 35
    assert not [call for call in stub.calls if call[0] == "play_media"]
    assert len(stub.items) == 213


def test_an_already_played_copy_is_not_a_duplicate(tmp_path):
    """Live evidence, exactly this shape.

    The real queue holds the same Imogen Heap track three times at positions 1,
    2 and 4 with current_index 5. All three are behind the playhead. A whole
    queue check would refuse a request for it right now for no reason at all.
    """
    stub = loaded(StubMA())
    for position in (1, 2, 4):
        stub.items[position]["media_item"]["uri"] = TRACK_URI
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": TRACK_URI}).json()
    assert body["action"] == "queued"
    assert ("play_media", TRACK_URI, "add") in stub.calls


def test_the_track_playing_right_now_is_not_reported_as_coming(tmp_path):
    """It is at the current index, so it is not waiting, it is ON.

    Nothing is queued, which is right: telling somebody "that is what you are
    hearing" beats adding a second copy. What was wrong was the number. This
    answered plays_after 0, and 0 renders as "it is next", so the one person who
    asked for the song already playing was promised it was coming when it was
    seconds from over and would never play again.
    """
    stub = loaded(StubMA())
    stub.items[5]["media_item"]["uri"] = TRACK_URI
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": TRACK_URI}).json()
    assert body["action"] == "already_queued"
    assert body["queue_position"] == 5
    assert body["already_playing"] is True
    # Null and not 0: "how many songs before it" has no answer for a track that
    # is already on, and any number here is read out loud as a promise.
    assert body["plays_after"] is None


def test_on_a_stopped_queue_the_current_track_really_is_next(tmp_path):
    # Between sets the live box sits idle with current_index 5 and nothing
    # audible. That item has NOT played, it is what starts when somebody presses
    # play, so "it is next" is the truth and must survive the fix above.
    stub = loaded(StubMA())
    stub.queue["state"] = "idle"
    stub.items[5]["media_item"]["uri"] = TRACK_URI
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": TRACK_URI}).json()
    assert body["action"] == "already_queued"
    assert body["already_playing"] is False
    assert body["plays_after"] == 0


def test_a_queue_that_has_never_played_is_checked_from_the_very_top(tmp_path):
    """current_index is null until something plays, and null is not 0 by luck.

    Nothing has played, so every item including the first is still upcoming and
    the check has to start at 0. Reading the null as "skip the first item" would
    let a duplicate of the very first track through, and treating item 0 as the
    playing track would refuse it.
    """
    stub = loaded(StubMA(), total=10, current=0)
    stub.queue["current_index"] = None
    stub.queue["index_in_buffer"] = None
    stub.queue["state"] = "idle"
    stub.items[0]["media_item"]["uri"] = TRACK_URI
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": TRACK_URI}).json()
    assert body["action"] == "already_queued"
    assert body["queue_position"] == 0
    # Nothing is on, so it is genuinely the next thing that plays.
    assert body["already_playing"] is False
    assert body["plays_after"] == 0
    assert body["duplicate_check"]["exhaustive"] is True


def test_force_queues_a_duplicate_on_purpose(tmp_path):
    # Somebody asking twice at a party is normal, and sometimes they mean it.
    stub = loaded(StubMA())
    stub.items[40]["media_item"]["uri"] = TRACK_URI
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": TRACK_URI, "force": True}).json()
    assert body["action"] == "queued"
    assert ("play_media", TRACK_URI, "add") in stub.calls


def test_a_partial_duplicate_check_says_it_was_partial(tmp_path):
    """The queue is longer than the window, and the answer must admit it.

    Fetching all 213 items costs 637 KB against the live box. The window is 50,
    about three hours of music, and a copy sitting past that is not found. The
    honest report is "checked 50 of 208", not a clean "no".
    """
    stub = loaded(StubMA())
    stub.items[100]["media_item"]["uri"] = TRACK_URI
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": TRACK_URI}).json()
    assert body["action"] == "queued"
    check = body["duplicate_check"]
    assert check["checked"] == 50
    assert check["upcoming"] == 208
    assert check["exhaustive"] is False


def test_a_short_queue_is_checked_exhaustively(tmp_path):
    stub = loaded(StubMA(), total=10, current=0)
    client, _, _ = build(tmp_path, ma=stub)
    check = client.post("/queue", json={"uri": TRACK_URI}).json()["duplicate_check"]
    assert check["checked"] == 10
    assert check["exhaustive"] is True


def test_a_bare_provider_uri_matches_the_instance_form_in_the_queue(tmp_path):
    """spotify://track/x and spotify--asK7Swun://track/x are the same track.

    A URI carries the provider INSTANCE, suffix and all. MA resolves the bare
    domain form perfectly well, so an agent that types one by hand would never
    string match the queue and every request would be enqueued twice.
    """
    stub = loaded(StubMA())
    stub.items[20]["media_item"]["uri"] = "spotify--asK7Swun://track/abc123"
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": "spotify://track/abc123"}).json()
    assert body["action"] == "already_queued"
    assert body["queue_position"] == 20


def test_an_item_without_a_uri_is_never_a_duplicate_of_anything(tmp_path):
    # A raw stream URL queued through the builtin provider has no media_item at
    # all, so it has no uri to compare. Two of them are not the same track.
    stub = loaded(StubMA(), total=10, current=0)
    for item in stub.items:
        item["media_item"] = None
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"url": "https://example.com/stream.mp3"}).json()
    assert body["action"] == "queued"


def test_now_and_replace_skip_the_duplicate_check(tmp_path):
    # "It is already at position 40" is not an answer to "play it now", and a
    # refused interrupt is an interrupt that silently did not happen.
    stub = loaded(StubMA())
    stub.items[40]["media_item"]["uri"] = TRACK_URI
    client, _, _ = build(tmp_path, ma=stub)
    assert client.post("/queue", json={"uri": TRACK_URI, "position": "now"}).json()["action"] == "playing"
    assert client.post("/play", json={"uri": TRACK_URI}).json()["action"] == "playing"


def test_the_duplicate_check_survives_a_queue_that_is_not_there(tmp_path):
    client, stub, _ = build(tmp_path)
    stub.queue = None
    body = client.post("/queue", json={"uri": TRACK_URI}).json()
    assert body["action"] == "queued"
    assert body["queue_position"] is None


def test_shuffle_on_means_the_landing_position_is_not_claimed(tmp_path):
    # With shuffle enabled MA's "add" does not append: it inserts after the
    # current item and reshuffles the tail. Reporting 213 there would be a
    # number invented out of the option name.
    stub = loaded(StubMA())
    stub.queue = dict(stub.queue, shuffle_enabled=True)
    client, _, _ = build(tmp_path, ma=stub)
    body = client.post("/queue", json={"uri": TRACK_URI}).json()
    assert body["queue_position"] is None
    assert "shuffle" in body["note"]


def test_fair_without_an_active_queue_still_adds(tmp_path):
    # Nothing to be fair about, and no queue to compute a target against.
    client, stub, _ = build(tmp_path)
    stub.queue = None
    body = client.post("/queue", json={"uri": TRACK_URI, "position": "fair"}).json()
    assert body["action"] == "queued"
    assert body["queue_position"] is None
    assert ("play_media", TRACK_URI, "add") in stub.calls
