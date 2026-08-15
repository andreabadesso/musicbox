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

from musicbox.app import create_app, live_position, resolve_sfx  # noqa: E402
from musicbox.config import Config  # noqa: E402
from musicbox.ma_client import MAError, MANotConnected, MATimeout  # noqa: E402

MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00fake audio payload"


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

    async def play_media(self, media: str, option: str) -> None:
        self._guard()
        self.calls.append(("play_media", media, option))

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
