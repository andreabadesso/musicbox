"""Speech, tested with no piper on the machine and no sound card anywhere.

Two kinds of fake, and the split is deliberate:

  A fake PIPER, a tiny script that writes a real wav, used wherever the thing
  under test is the render itself. It exercises the actual subprocess path,
  stdin, the temp file and the atomic rename, so the caching and the failure
  handling are tested against a process that really runs and really exits
  non-zero. Mocking that away would leave the only part with a fork in it
  untested.

  A fake MIXER and a fake MA, used wherever the thing under test is which PATH
  a sentence took. Those tests must not depend on rendering at all, so TTS is
  swapped for one that hands back a file that already exists.

Nothing here can make a noise. The mixer stub never opens a device and the MA
stub only records the calls it was asked to make.
"""

from __future__ import annotations

import os
import struct
import sys
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from musicbox.app import (
    SAY_ANNOUNCEMENT_NOTE,
    SAY_MIXER_NOTE,
    create_app,
    list_sfx,
    perform_say,
)
from musicbox.config import Config
from musicbox.mixer_client import MixerUnavailable
from musicbox.tts import SAY_TEXT_MAX, TTS, Rendered, TTSError

from conftest import async_test
from test_app import StubMA

# ── the fake piper ────────────────────────────────────────────────────────────

# argv is exactly the shape tts.py builds: -m <model> -f <out>, text on stdin.
# It asserts that shape rather than tolerating it, because the argv is the one
# part of this that a real piper would reject silently by rendering the wrong
# thing (a `-m` that arrived as positional text becomes a spoken "dash m").
FAKE_PIPER = """#!{python}
import sys, wave
argv = sys.argv[1:]
assert argv[0] == "-m", argv
assert argv[2] == "-f", argv
text = sys.stdin.read()
assert text and "\\n" not in text.strip(), repr(text)
{body}
"""

# 22050 mono, which is what a piper medium voice really emits. The box wants
# 48000:16:2 and nothing here converts: the mixer's EffectCache runs ffmpeg on
# whatever it finds. A test that wrote 48k stereo would quietly assert that
# conversion is not needed.
_OK_BODY = """
with wave.open(argv[3], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
    w.writeframes(b"\\x00\\x00" * int(22050 * 0.4 * len(text) / 20 + 1))
"""

_FAIL_BODY = """
sys.stderr.write("Traceback (most recent call last):\\n")
sys.stderr.write("RuntimeError: could not load the voice model\\n")
sys.exit(1)
"""

_SILENT_BODY = """
open(argv[3], "wb").close()
"""


def fake_piper(tmp_path: Path, body: str = _OK_BODY, name: str = "piper") -> Path:
    path = tmp_path / name
    path.write_text(FAKE_PIPER.format(python=sys.executable, body=body))
    path.chmod(0o755)
    return path


def voice_file(tmp_path: Path) -> Path:
    """A stand-in for the .onnx, plus the .json piper insists sits beside it."""
    voice = tmp_path / "pt_BR-faber-medium.onnx"
    voice.write_bytes(b"not really a model")
    voice.with_suffix(".onnx.json").write_text("{}")
    return voice


def tts_for(tmp_path: Path, **overrides) -> TTS:
    sfx = tmp_path / "sfx"
    sfx.mkdir(exist_ok=True)
    values = dict(
        player="musicbox",
        sfx_dir=sfx,
        piper_bin=str(fake_piper(tmp_path)),
        piper_voice=str(voice_file(tmp_path)),
        prefetch=False,
    )
    values.update(overrides)
    return TTS(Config(**values))


# ── rendering and the cache ───────────────────────────────────────────────────


@async_test()
async def test_render_writes_a_playable_wav_into_the_sfx_dir(tmp_path):
    tts = tts_for(tmp_path)
    out = await tts.render("faltam trinta minutos")
    assert out.cached is False
    # In the sfx dir and NOT somewhere private, because that is the only
    # directory the mixer scans and the only one /sfx/file serves.
    assert out.path.parent == tmp_path / "sfx"
    assert out.path.name.startswith("say-") and out.path.suffix == ".wav"
    with wave.open(str(out.path), "rb") as handle:
        assert handle.getnframes() > 0
    assert out.duration and out.duration > 0


@async_test()
async def test_the_rendered_clip_is_readable_by_the_mixer_user(tmp_path):
    """0600 here means speech is silent with every check green.

    musicbox renders under DynamicUser; the mixer runs as its own user and
    opens the wav as that other uid. mkstemp makes 0600 and os.replace keeps
    the source mode, so the clip would arrive unreadable to the only process
    that can play it — and nothing would report an error: piper succeeds,
    /health stays green, POST /say answers 200, and the mixer's scan just
    skips the file. Measured on the pi5: cache count 27 at 0600, 28 at 0644.
    """
    tts = tts_for(tmp_path)
    # The unit sets UMask=0077, so reproduce it — under the 0022 a developer
    # shell has, piper's own file already lands 0644 and this test proves
    # nothing at all. Verified: with the chmod removed and this umask in
    # place, the assertion below fails.
    previous = os.umask(0o077)
    try:
        out = await tts.render("faltam trinta minutos")
    finally:
        os.umask(previous)
    mode = out.path.stat().st_mode & 0o777
    assert mode & 0o044 == 0o044, (
        f"clip is {mode:#o}; the mixer runs as another user and cannot open it"
    )


@async_test()
async def test_the_same_sentence_twice_is_a_cache_hit(tmp_path):
    tts = tts_for(tmp_path)
    first = await tts.render("faltam trinta minutos")
    stamp = first.path.stat().st_mtime_ns
    second = await tts.render("  faltam   trinta minutos  ")
    # Same file, untouched: the whitespace collapse in clean() has to land on
    # the same cache key, or a countdown re-renders every single time.
    assert second.cached is True
    assert second.path == first.path
    assert second.path.stat().st_mtime_ns == stamp


@async_test()
async def test_a_different_voice_renders_a_different_file(tmp_path):
    tts = tts_for(tmp_path)
    first = await tts.render("bom dia")
    other = tmp_path / "pt_BR-cadu-medium.onnx"
    other.write_bytes(b"another model")
    # The companion json too, or the box correctly refuses to speak with a
    # voice piper cannot load. See the .onnx.json test below.
    other.with_name(other.name + ".json").write_text("{}")
    tts._config.piper_voice = str(other)
    second = await tts.render("bom dia")
    assert second.path != first.path


@async_test()
async def test_concurrent_identical_renders_run_piper_once(tmp_path):
    import asyncio

    tts = tts_for(tmp_path)
    calls = []
    original = tts._run_piper

    async def counting(text, voice, target):
        calls.append(text)
        await original(text, voice, target)

    tts._run_piper = counting  # type: ignore[assignment]
    results = await asyncio.gather(*(tts.render("uepa") for _ in range(4)))
    assert len(calls) == 1
    assert len({r.path for r in results}) == 1


# ── the states that must not raise ────────────────────────────────────────────


@async_test()
async def test_unconfigured_tts_explains_itself_instead_of_raising(tmp_path):
    tts = tts_for(tmp_path, piper_bin="", piper_voice="")
    assert tts.available is False
    reason = tts.unavailable_reason() or ""
    assert "MUSICBOX_PIPER_BIN" in reason
    status = tts.status()
    assert status["tts_available"] is False
    assert status["tts_voice"] is None
    with pytest.raises(TTSError) as caught:
        await tts.render("bom dia")
    assert "Nothing was spoken" in str(caught.value)


def test_a_missing_binary_is_reported_by_path(tmp_path):
    tts = tts_for(tmp_path, piper_bin=str(tmp_path / "nope"))
    assert "does not exist" in (tts.unavailable_reason() or "")


def test_a_missing_voice_is_reported_by_path(tmp_path):
    tts = tts_for(tmp_path, piper_voice=str(tmp_path / "gone.onnx"))
    assert "voice model" in (tts.unavailable_reason() or "")


@async_test()
async def test_empty_text_is_refused_with_a_sentence(tmp_path):
    tts = tts_for(tmp_path)
    for value in ("", "   ", "\n\t "):
        with pytest.raises(TTSError) as caught:
            await tts.render(value)
        assert "no text to say" in str(caught.value)
    assert list(tts.cache_dir.glob("say-*.wav")) == []


@async_test()
async def test_enormous_text_is_refused_and_names_the_limit(tmp_path):
    tts = tts_for(tmp_path)
    with pytest.raises(TTSError) as caught:
        await tts.render("a" * (SAY_TEXT_MAX + 1))
    message = str(caught.value)
    # Refused, not truncated: half a sentence spoken to a room with no sign
    # that it was cut is worse than being told to split it.
    assert str(SAY_TEXT_MAX) in message
    assert "shorter" in message
    assert list(tts.cache_dir.glob("say-*.wav")) == []
    # And the boundary itself is allowed.
    assert (await tts.render("a" * SAY_TEXT_MAX)).cached is False


@async_test()
async def test_a_failing_piper_says_the_music_was_not_interrupted(tmp_path):
    tts = tts_for(tmp_path, piper_bin=str(fake_piper(tmp_path, _FAIL_BODY, "piper_bad")))
    with pytest.raises(TTSError) as caught:
        await tts.render("bom dia")
    message = str(caught.value)
    # The LAST stderr line, not "Traceback (most recent call last):".
    assert "could not load the voice model" in message
    assert "music was not interrupted" in message
    # No half written file left behind to become a silent cache hit forever.
    assert list(tts.cache_dir.glob("say-*")) == []
    assert tts.status()["tts_last_error"]


@async_test()
async def test_a_zero_byte_render_is_not_cached_as_success(tmp_path):
    tts = tts_for(tmp_path, piper_bin=str(fake_piper(tmp_path, _SILENT_BODY, "piper_mute")))
    with pytest.raises(TTSError) as caught:
        await tts.render("bom dia")
    assert "produced no audio" in str(caught.value)
    assert list(tts.cache_dir.glob("say-*")) == []


# ── sharing the sfx directory ─────────────────────────────────────────────────


@async_test()
async def test_speech_clips_are_hidden_from_the_sfx_listing(tmp_path):
    tts = tts_for(tmp_path)
    (tts.cache_dir / "airhorn.mp3").write_bytes(b"ID3fake")
    await tts.render("faltam trinta minutos")
    names = [item["name"] for item in list_sfx(tts.cache_dir)]
    # The soundboard shows what a human put on the box, not forty hashes.
    assert names == ["airhorn"]


def test_pruning_never_touches_a_real_sound_effect(tmp_path, monkeypatch):
    from musicbox import tts as tts_module

    tts = tts_for(tmp_path)
    keeper = tts.cache_dir / "airhorn.wav"
    keeper.write_bytes(b"RIFFfake")
    monkeypatch.setattr(tts_module, "CACHE_MAX_FILES", 1)
    for index in range(5):
        (tts.cache_dir / f"say-{index:032x}.wav").write_bytes(b"RIFFfake")
    tts_module._prune(tts.cache_dir)
    # This is the bug worth a test: a prune that globbed *.wav in the shared
    # directory would have deleted the operator's airhorn.
    assert keeper.exists()
    assert len(list(tts.cache_dir.glob("say-*.wav"))) == 1


@async_test()
async def test_the_cache_is_bounded_while_the_files_are_still_new(tmp_path, monkeypatch):
    """The bound has to hold at the moment the clips are written.

    The version of _prune this replaces exempted anything younger than ten
    minutes, so eighty sentences rendered back to back left eighty files: none
    of them were old enough to prune. That is not a disk problem. The mixer
    decodes every clip in this directory into RAM on its next reload, and a
    model calling say() in a loop reaches several hundred inside that window.
    """
    from musicbox import tts as tts_module

    monkeypatch.setattr(tts_module, "CACHE_MAX_FILES", 8)
    tts = tts_for(tmp_path)
    for index in range(20):
        await tts.render(f"frase numero {index}")
    clips = list(tts.cache_dir.glob("say-*.wav"))
    assert len(clips) <= 8, f"{len(clips)} clips kept against a limit of 8"


@async_test()
async def test_the_clip_just_rendered_survives_its_own_prune(tmp_path, monkeypatch):
    """MA fetches the URL a moment after say() returns, so it has to be there."""
    from musicbox import tts as tts_module

    monkeypatch.setattr(tts_module, "CACHE_MAX_FILES", 1)
    tts = tts_for(tmp_path)
    for index in range(4):
        rendered = await tts.render(f"frase {index}")
        assert rendered.path.exists(), "the freshly rendered clip was pruned"


def test_renders_do_not_pile_up_on_the_box(tmp_path):
    """A model calling say() in a loop must not spawn a piper per sentence.

    The Pi has four cores and is mixing audio in real time on one of them at
    ordinary priority. Measured before this cap: twenty four concurrent calls
    for twenty four different sentences ran twenty four pipers at once. The
    per-key lock does not help, because every sentence is a different key.
    """
    import asyncio as _asyncio

    from musicbox import tts as tts_module

    tts = tts_for(tmp_path)
    live = 0
    peak = 0
    real = tts_module.TTS._run_piper

    async def counting(self, text, voice, target):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await _asyncio.sleep(0.05)
            await real(self, text, voice, target)
        finally:
            live -= 1

    async def go():
        await _asyncio.gather(*[tts.render(f"frase {i}") for i in range(12)])

    tts_module.TTS._run_piper = counting
    try:
        _asyncio.run(go())
    finally:
        tts_module.TTS._run_piper = real
    assert peak <= tts_module.RENDER_CONCURRENCY, f"{peak} renders ran at once"


@async_test()
async def test_a_box_too_busy_to_render_says_so_instead_of_waiting_forever(tmp_path, monkeypatch):
    from musicbox import tts as tts_module

    monkeypatch.setattr(tts_module, "RENDER_QUEUE_WAIT", 0.05)
    tts = tts_for(tmp_path)
    # Both slots taken and never given back, which is what a wedged piper looks
    # like from here.
    for _ in range(tts_module.RENDER_CONCURRENCY):
        await tts._slots.acquire()
    with pytest.raises(TTSError) as caught:
        await tts.render("faltam trinta minutos")
    assert "nothing was spoken" in str(caught.value)
    assert "already rendering" in str(caught.value)


@async_test()
async def test_a_render_that_fails_does_not_leak_its_lock(tmp_path):
    tts = tts_for(tmp_path, piper_bin=str(fake_piper(tmp_path, _FAIL_BODY, "piper_bad")))
    for index in range(10):
        with pytest.raises(TTSError):
            await tts.render(f"falha {index}")
    assert tts._locks == {}


# ── the silence that reads as success ─────────────────────────────────────────

# A wav with a full header and zero frames. piper writes exactly this, and exits
# 0 doing it, for text that phonemises to nothing: punctuation on its own, an
# emoji, a symbol espeak-ng has no pt-br pronunciation for.
_HEADER_ONLY_BODY = """
with wave.open(argv[3], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
    w.writeframes(b"")
"""

# The same thing, but too big to be caught by the size check: a bare wav header
# is 44 bytes, and a trailing metadata chunk (which plenty of encoders write)
# pushes it past that while the data chunk still holds zero frames. This is the
# one that needs the frame count to be read, and the reason the size check is
# not the whole answer.
_PADDED_EMPTY_BODY = """
with wave.open(argv[3], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
    w.writeframes(b"")
with open(argv[3], "ab") as f:
    f.write(b"LIST" + (32).to_bytes(4, "little") + bytes(32))
"""


@async_test()
async def test_a_wav_with_a_header_and_no_audio_is_refused(tmp_path):
    """The worst failure this box has: green everywhere and a silent room.

    Before this check the 44 byte file passed the size test, was cached, and
    every later call was a cache HIT on silence. say() answered ok true,
    spoken true, the mixer reported a voice playing, /health was green, and
    the room heard nothing for as long as the box ran.
    """
    tts = tts_for(tmp_path, piper_bin=str(fake_piper(tmp_path, _HEADER_ONLY_BODY, "piper_hollow")))
    with pytest.raises(TTSError) as caught:
        await tts.render("...")
    assert "nothing was spoken" in str(caught.value)
    # And nothing was left behind to be served as a cache hit next time.
    assert list(tts.cache_dir.glob("say-*")) == []


@async_test()
async def test_an_empty_wav_too_big_for_the_size_check_is_still_refused(tmp_path):
    """Size alone is not the answer, only the cheap half of it.

    44 bytes catches the bare header. A trailing metadata chunk carries the
    same zero frames of audio past that threshold, and only the frame count
    tells the two apart from a clip that would actually be heard.
    """
    tts = tts_for(tmp_path, piper_bin=str(fake_piper(tmp_path, _PADDED_EMPTY_BODY, "piper_padded")))
    with pytest.raises(TTSError) as caught:
        await tts.render("...")
    assert "no audio in it" in str(caught.value)
    assert "Send words" in str(caught.value)
    assert list(tts.cache_dir.glob("say-*")) == []


@async_test()
async def test_a_silent_clip_left_by_an_older_build_is_not_a_cache_hit(tmp_path):
    """The fix has to cover the files already on the live box's disk."""
    tts = tts_for(tmp_path)
    stale = tts.cache_path("faltam trinta minutos", tts.voice)
    with wave.open(str(stale), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(b"")
    assert stale.stat().st_size <= 44
    rendered = await tts.render("faltam trinta minutos")
    assert rendered.cached is False, "a silent clip was served from the cache"
    assert rendered.duration and rendered.duration > 0


@async_test()
async def test_a_nul_byte_cannot_truncate_a_sentence(tmp_path):
    """The C++ piper reads the line into a C string, so a NUL cuts it there.

    Not an injection: the text goes in on stdin and never touches argv, which
    the fake piper asserts. This is about the room hearing the whole sentence.
    """
    tts = tts_for(tmp_path)
    assert tts.clean("faltam\x00 trinta minutos") == "faltam trinta minutos"
    assert tts.clean("bom\x07 dia") == "bom dia"
    # And a string that is nothing but control characters is refused, not sent
    # to piper to come back as an empty clip.
    with pytest.raises(TTSError):
        tts.clean("\x00\x01\x02")


@async_test()
async def test_a_voice_with_no_companion_json_is_reported_before_anyone_speaks(tmp_path):
    """piper needs the .onnx.json and names a file the caller never typed.

    nix/module.nix pins the pair together and explains this trap at length; the
    check has to exist on the side that has to explain it to a person, so it
    shows up in /health rather than as a piper traceback.
    """
    tts = tts_for(tmp_path)
    Path(str(tts.voice) + ".json").unlink()
    reason = tts.unavailable_reason()
    assert reason and "companion json" in reason
    assert tts.available is False
    status = tts.status()
    assert status["tts_available"] is False


# ── which path a sentence took ────────────────────────────────────────────────


class StubMixer:
    """A MixerClient shaped just enough for perform_say, with no socket."""

    def __init__(self, *, enabled: bool = True, fail: Exception | None = None,
                 not_found_until_reload: bool = False):
        self.enabled = enabled
        self.calls: list[tuple] = []
        self.fail = fail
        self.not_found_until_reload = not_found_until_reload
        self.reloaded = 0

    async def reload(self):
        self.calls.append(("reload",))
        self.reloaded += 1
        self.not_found_until_reload = False
        return {"ok": True, "count": 1}

    async def play(self, name, *, gain_db=None):
        self.calls.append(("play", name, gain_db))
        if self.fail is not None:
            raise self.fail
        if self.not_found_until_reload:
            raise MixerUnavailable(f"err not_found name={name} known=airhorn,fogo")
        return {"ok": True, "voices": 2}


class StubTTS:
    """Hands back a file that already exists. Renders nothing, spawns nothing."""

    def __init__(self, path: Path, *, cached: bool = False, error: str | None = None):
        self.path = path
        self.cached = cached
        self.error = error
        self.voice_name = "pt_BR-faber-medium"

    def clean(self, text):
        return " ".join(text.split())

    async def render(self, text):
        if self.error:
            raise TTSError(self.error)
        return Rendered(path=self.path, cached=self.cached, duration=1.5)


def clip(tmp_path: Path, name: str = "say-deadbeef.wav") -> Path:
    sfx = tmp_path / "sfx"
    sfx.mkdir(exist_ok=True)
    path = sfx / name
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(struct.pack("<h", 0) * 2205)
    return path


@async_test()
async def test_say_takes_the_mixer_path_when_the_mixer_is_up(tmp_path):
    ma, mixer = StubMA(), StubMixer()
    result = await perform_say(
        ma, StubTTS(clip(tmp_path)), "faltam trinta minutos",
        "http://127.0.0.1:8099", mixer, -2.0,
    )
    assert result["ok"] is True and result["path"] == "mixer"
    assert result["fell_back"] is False
    assert result["note"] == SAY_MIXER_NOTE
    assert result["voice"] == "pt_BR-faber-medium"
    assert ("play", "say-deadbeef", -2.0) in mixer.calls
    # The music was never touched.
    assert ma.calls == []


@async_test()
async def test_a_fresh_clip_reloads_first_and_a_cached_one_does_not(tmp_path):
    path = clip(tmp_path)
    fresh = StubMixer()
    await perform_say(StubMA(), StubTTS(path, cached=False), "oi", "http://x", fresh)
    assert fresh.calls == [("reload",), ("play", "say-deadbeef", None)]

    warm = StubMixer()
    await perform_say(StubMA(), StubTTS(path, cached=True), "oi", "http://x", warm)
    # The case that happens all evening: one socket round trip, no reload.
    assert warm.calls == [("play", "say-deadbeef", None)]


@async_test()
async def test_a_cached_clip_the_mixer_forgot_reloads_and_retries(tmp_path):
    # What a mixer restart mid-event looks like from here.
    mixer = StubMixer(not_found_until_reload=True)
    result = await perform_say(
        StubMA(), StubTTS(clip(tmp_path), cached=True), "oi", "http://x", mixer
    )
    assert result["path"] == "mixer"
    assert mixer.reloaded == 1
    assert [c[0] for c in mixer.calls] == ["play", "reload", "play"]


@async_test()
async def test_an_unreachable_mixer_falls_back_to_the_announcement(tmp_path):
    ma = StubMA()
    mixer = StubMixer(fail=MixerUnavailable("could not reach the mixer at /run/x.sock"))
    result = await perform_say(
        ma, StubTTS(clip(tmp_path), cached=True), "faltam trinta minutos",
        "http://127.0.0.1:8099", mixer,
    )
    assert result["ok"] is True
    assert result["path"] == "announcement"
    assert result["fell_back"] is True
    assert result["note"] == SAY_ANNOUNCEMENT_NOTE
    assert "mixer was not usable" in result["reason"]
    assert (
        "play_announcement",
        "http://127.0.0.1:8099/sfx/file/say-deadbeef.wav",
    ) in ma.calls


@async_test()
async def test_a_dead_mixer_is_not_retried_twice(tmp_path):
    # A socket that is down must cost ONE timeout, not two. The retry is for
    # not_found only, and this is the assertion that keeps it that way.
    mixer = StubMixer(fail=MixerUnavailable("could not reach the mixer at /run/x.sock"))
    await perform_say(StubMA(), StubTTS(clip(tmp_path), cached=True), "oi", "http://x", mixer)
    assert mixer.reloaded == 0
    assert len(mixer.calls) == 1


@async_test()
async def test_with_no_mixer_at_all_it_announces_without_calling_it_a_fallback(tmp_path):
    ma = StubMA()
    result = await perform_say(
        ma, StubTTS(clip(tmp_path)), "oi", "http://127.0.0.1:8099", None
    )
    assert result["path"] == "announcement"
    assert result.get("fell_back") is False
    assert result["note"] == SAY_ANNOUNCEMENT_NOTE


@async_test()
async def test_perform_say_never_raises_when_tts_is_missing_or_broken(tmp_path):
    absent = await perform_say(StubMA(), None, "oi", "http://x", None)
    assert absent["ok"] is False and absent["spoken"] is False
    assert "not set up on this box" in absent["detail"]

    broken = await perform_say(
        StubMA(),
        StubTTS(clip(tmp_path), error="piper failed to render the speech, so nothing was spoken."),
        "oi", "http://x", None,
    )
    assert broken["ok"] is False
    assert "nothing was spoken" in broken["detail"]


@async_test()
async def test_an_out_of_range_gain_is_refused_rather_than_silencing_the_music(tmp_path):
    mixer = StubMixer()
    result = await perform_say(
        StubMA(), StubTTS(clip(tmp_path)), "oi", "http://x", mixer, 99.0
    )
    assert result["ok"] is False and result["error"] == "bad_gain"
    # The point of catching it here: the mixer would have answered err
    # bad_gain, which is indistinguishable from a dead socket by the time it
    # reaches perform_say, and the fallback would have STOPPED THE MUSIC.
    assert mixer.calls == []



# ── the HTTP surface ──────────────────────────────────────────────────────────


def app_for(tmp_path, **overrides):
    sfx = tmp_path / "sfx"
    sfx.mkdir(exist_ok=True)
    values = dict(
        host="0.0.0.0", port=8099, player="musicbox", sfx_dir=sfx, prefetch=False
    )
    values.update(overrides)
    stub = StubMA()
    return TestClient(create_app(Config(**values), stub)), stub, sfx


def test_health_says_when_there_is_no_voice(tmp_path):
    client, _, _ = app_for(tmp_path)
    body = client.get("/health").json()
    # Green everywhere else and still honest about this. Silence with every
    # service healthy is the worst thing this box does.
    assert body["ok"] is True
    assert body["tts_available"] is False
    assert "MUSICBOX_PIPER_BIN" in body["tts_reason"]
    assert body["tts_voice"] is None


def test_health_names_the_voice_when_there_is_one(tmp_path):
    client, _, _ = app_for(
        tmp_path,
        piper_bin=str(fake_piper(tmp_path)),
        piper_voice=str(voice_file(tmp_path)),
    )
    body = client.get("/health").json()
    assert body["tts_available"] is True
    assert body["tts_voice"] == "pt_BR-faber-medium"
    assert body["tts_reason"] is None


def test_say_route_answers_200_with_a_sentence_when_tts_is_off(tmp_path):
    client, stub, _ = app_for(tmp_path)
    response = client.post("/say", json={"text": "faltam trinta minutos"})
    # Not a 500 and not a 503. Someone with a finger on a phone gets a line of
    # text, and the music is untouched.
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["spoken"] is False
    assert body["detail"]
    assert stub.calls == []


def test_say_route_refuses_empty_text_without_touching_the_music(tmp_path):
    client, stub, _ = app_for(
        tmp_path,
        piper_bin=str(fake_piper(tmp_path)),
        piper_voice=str(voice_file(tmp_path)),
    )
    body = client.post("/say", json={"text": "   "}).json()
    assert body["ok"] is False
    assert "no text to say" in body["detail"]
    assert stub.calls == []


def test_say_route_renders_and_announces_end_to_end(tmp_path):
    client, stub, sfx = app_for(
        tmp_path,
        piper_bin=str(fake_piper(tmp_path)),
        piper_voice=str(voice_file(tmp_path)),
        sfx_base_url="http://10.88.0.1:8099",
    )
    body = client.post("/say", json={"text": "o almoco chegou"}).json()
    assert body["ok"] is True
    assert body["path"] == "announcement"
    assert body["text"] == "o almoco chegou"
    assert body["cached"] is False
    clips = list(sfx.glob("say-*.wav"))
    assert len(clips) == 1
    assert ("play_announcement", f"http://10.88.0.1:8099/sfx/file/{clips[0].name}") in stub.calls
    # And the file it just wrote is fetchable, which is the whole reason it
    # lives in the sfx directory: MA refuses a source that is not http(s).
    assert client.get(f"/sfx/file/{clips[0].name}").status_code == 200
    # Second time round it is a cache hit and no new file appears.
    again = client.post("/say", json={"text": "o almoco chegou"}).json()
    assert again["cached"] is True
    assert len(list(sfx.glob("say-*.wav"))) == 1
    # And it never shows up as a sound effect.
    assert client.get("/sfx").json()["sfx"] == []


def test_the_board_has_a_field_that_posts_to_say(tmp_path):
    from musicbox.board import BOARD_HTML

    assert "id=\"saytext\"" in BOARD_HTML
    assert "'/say'" in BOARD_HTML
    # speak() and not say(): say() is already the status line on that page.
    assert "async function speak()" in BOARD_HTML


# ── the MCP tool ──────────────────────────────────────────────────────────────


@async_test()
async def test_the_mcp_say_tool_describes_the_path_it_took(tmp_path):
    from musicbox.mcp_server import build_mcp

    class Backend:
        def __init__(self):
            self.said = []

        async def say(self, text, gain_db):
            self.said.append((text, gain_db))
            return {"ok": True, "text": text, "path": "mixer", "note": SAY_MIXER_NOTE}

    backend = Backend()
    mcp = build_mcp(backend)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "say" in tools
    description = tools["say"].description or ""
    # The description is what tells a model when NOT to reach for this.
    assert "sfx" in description and "drop" in description
    assert "Portuguese" in description

    answer = await mcp.call_tool("say", {"text": "  faltam trinta minutos  "})
    text = str(answer)
    assert "faltam trinta minutos" in text
    assert "ducks" in text
    assert backend.said == [("faltam trinta minutos", None)]


@async_test()
async def test_the_mcp_say_tool_returns_the_reason_instead_of_raising(tmp_path):
    from musicbox.mcp_server import build_mcp

    class Backend:
        async def say(self, text, gain_db):
            return {"ok": False, "detail": "Text to speech is not configured on this box."}

    mcp = build_mcp(Backend())
    blank = str(await mcp.call_tool("say", {"text": "   "}))
    assert "Give something to say" in blank
    off = str(await mcp.call_tool("say", {"text": "oi"}))
    assert "not configured" in off
