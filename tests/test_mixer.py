"""The mixer, tested with no sound card anywhere.

Everything the audio loop touches is injectable: the source is a fifo, a file
or plain silence, and the sink is a buffer. So the suite runs on a laptop, in
CI, and on the Pi, and none of those runs can make a noise or open the speaker
that a room is listening to.

The engine is also stepped one period at a time with run_once() rather than
started as a thread wherever that is possible. A test that sleeps and hopes is
a test that fails on a loaded machine at the worst possible time, and the
things worth asserting here (arithmetic, envelope shape, voice bookkeeping) are
all exact.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from musicbox.config import MixerConfig
from musicbox.mixer import (
    FRAME_BYTES,
    PRODUCER_CHUNK_BYTES,
    RATE,
    AlsaSink,
    BufferSink,
    Effect,
    EffectCache,
    FifoSource,
    FileSink,
    Mixer,
    MixerEngine,
    MixerService,
    PcmFileSource,
    SilenceSource,
    SinkBroken,
    db_to_gain,
    frames_of,
    silence,
)

PERIOD = 960  # 20 ms, the production period size


def tone(frames: int, amplitude: int = 8000, freq: float = 440.0) -> np.ndarray:
    t = np.arange(frames, dtype=np.float32) / RATE
    wave = (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.int16)
    return np.stack([wave, wave], axis=1)


def flat(frames: int, value: int) -> np.ndarray:
    return np.full((frames, 2), value, dtype=np.int16)


def effect_of(pcm: np.ndarray, name: str = "airhorn") -> Effect:
    return Effect(name, pcm, Path(f"/nowhere/{name}.mp3"), "test")


# ── mixing arithmetic ─────────────────────────────────────────────────────────


def test_music_alone_passes_through_untouched():
    """No voices means no duck and no gain: the bytes must survive the trip.

    This is the one that matters for the deployed box. The mixer sits in the
    music path all evening with nothing playing over it, and any accidental
    rounding, scaling or dtype slip would be permanently audible.
    """
    mixer = Mixer()
    music = tone(PERIOD)
    out = mixer.mix(music)
    assert out.dtype == np.int16
    assert np.array_equal(out, music)


def test_voice_is_added_to_the_ducked_music():
    mixer = Mixer(duck_db=-6.0, fade_in_ms=0.0001)  # effectively instant duck
    music = flat(PERIOD, 1000)
    mixer.trigger(effect_of(flat(PERIOD, 2000)))
    out = mixer.mix(music)
    # The first sample is still on the ramp; by the end the duck is settled.
    expected = 1000 * db_to_gain(-6.0) + 2000
    assert abs(int(out[-1, 0]) - expected) <= 2


def test_summing_clips_instead_of_wrapping():
    """int16 addition wraps at full scale, and wrapping sounds like tearing.

    Two effects at full scale on top of music at full scale is 3x the range.
    Done in int16 the result is a negative number, which is the loudest and
    ugliest failure this file can produce.
    """
    mixer = Mixer(duck_db=0.0)  # no duck, so the sum really is 3x full scale
    music = flat(PERIOD, 32000)
    mixer.trigger(effect_of(flat(PERIOD, 32000)))
    mixer.trigger(effect_of(flat(PERIOD, 32000), "second"))
    out = mixer.mix(music)
    assert out.max() == 32767
    assert out.min() >= 0, "a wrapped sum shows up as a large negative sample"

    # And the same on the negative rail, where the wrap would go the other way.
    mixer2 = Mixer(duck_db=0.0)
    mixer2.trigger(effect_of(flat(PERIOD, -32000)))
    mixer2.trigger(effect_of(flat(PERIOD, -32000), "second"))
    out2 = mixer2.mix(flat(PERIOD, -32000))
    assert out2.min() == -32768
    assert out2.max() <= 0


def test_effect_gain_scales_the_voice_only():
    mixer = Mixer(duck_db=0.0, effect_db=-6.0)
    mixer.trigger(effect_of(flat(PERIOD, 10000)))
    out = mixer.mix(silence(PERIOD))
    assert abs(int(out[0, 0]) - int(10000 * db_to_gain(-6.0))) <= 1

    # And a per-trigger gain multiplies on top of the configured one.
    mixer.stop_all()
    mixer.trigger(effect_of(flat(PERIOD, 10000)), gain_db=-6.0)
    out = mixer.mix(silence(PERIOD))
    assert abs(int(out[0, 0]) - int(10000 * db_to_gain(-12.0))) <= 1


# ── the duck envelope ─────────────────────────────────────────────────────────


def test_duck_fades_down_and_back_without_a_step():
    """A duck that jumps is a click, and a click through a PA is loud.

    Asserting on the SHAPE and not just the endpoints: the failure worth
    catching is a one sample transition, which passes any "did it duck" check
    and is exactly what you hear.
    """
    mixer = Mixer(duck_db=-12.0, fade_in_ms=60.0, fade_out_ms=250.0)
    music = flat(PERIOD * 20, 10000)
    effect_frames = int(RATE * 0.1)  # 100 ms
    mixer.trigger(effect_of(flat(effect_frames, 0)))  # silent effect: only the duck shows

    blocks = [mixer.mix(music[i * PERIOD:(i + 1) * PERIOD]) for i in range(20)]
    out = np.concatenate(blocks, axis=0)[:, 0].astype(np.float32) / 10000.0

    floor = db_to_gain(-12.0)
    assert out[0] > 0.99, "the duck must start from where the music was"
    assert min(out) == pytest.approx(floor, abs=0.01), "it has to reach the configured floor"
    assert out[-1] == pytest.approx(1.0, abs=0.01), "and come all the way back"

    # No step bigger than one fade step. 60 ms down over a travel of
    # (1 - floor) is the fastest legal move, with a frame of slack.
    fastest = (1.0 - floor) / (0.060 * RATE) * 1.5
    assert np.abs(np.diff(out)).max() <= fastest

    # The release is slower than the attack, which is what an ear expects.
    down = int(np.argmin(out))
    up = int(np.argmax(out[down:] > 0.99)) + down
    assert (up - down) > down, "coming back must take longer than going down"


def test_duck_holds_while_voices_overlap():
    """Two overlapping effects must not let the music up between them."""
    mixer = Mixer(duck_db=-12.0, fade_in_ms=1.0, fade_out_ms=250.0)
    mixer.trigger(effect_of(flat(PERIOD * 2, 100)))
    mixer.mix(flat(PERIOD, 10000))
    mixer.mix(flat(PERIOD, 10000))
    # The first voice is done, a second one starts in the same period.
    assert mixer.voices == 0
    mixer.trigger(effect_of(flat(PERIOD * 2, 100), "second"))
    out = mixer.mix(flat(PERIOD, 10000))
    assert out[0, 0] < 10000 * 0.4, "the music must still be held down"


def test_stop_all_releases_the_duck_gently():
    """stop is a panic button, and it must not put a click through the PA."""
    mixer = Mixer(duck_db=-12.0, fade_in_ms=1.0, fade_out_ms=250.0)
    mixer.trigger(effect_of(flat(RATE, 20000)))
    mixer.mix(flat(PERIOD, 10000))
    assert mixer.duck == pytest.approx(db_to_gain(-12.0), abs=0.01)
    assert mixer.stop_all() == 1
    out = mixer.mix(flat(PERIOD, 10000))
    # Still ducked at the top of the very next period, releasing over the fade
    # rather than snapping back to full volume in one sample.
    assert out[0, 0] < 10000 * 0.35
    assert out[-1, 0] > out[0, 0]


# ── retrigger and polyphony ───────────────────────────────────────────────────


def test_retrigger_adds_a_voice_rather_than_restarting_one():
    """The documented policy, asserted rather than assumed.

    "Does pressing repeatedly give a stutter effect" was the actual question,
    and layering is what answers yes. If someone ever switches this to
    restart-in-place, this test is the thing that tells them they changed the
    product and not just the implementation.
    """
    mixer = Mixer(duck_db=0.0)
    effect = effect_of(flat(RATE, 1000))
    mixer.trigger(effect)
    mixer.mix(silence(PERIOD))
    assert mixer.voices == 1
    mixer.trigger(effect)
    out = mixer.mix(silence(PERIOD))
    assert mixer.voices == 2
    # Two copies of the same effect, so twice the amplitude. A restart policy
    # would leave this at 1000.
    assert out[0, 0] == 2000


def test_polyphony_limit_steals_the_oldest_voice():
    mixer = Mixer(polyphony=3, duck_db=0.0)
    effects = [effect_of(flat(RATE, 1000), f"e{i}") for i in range(4)]
    for effect in effects:
        mixer.trigger(effect)
    assert mixer.voices == 3
    assert mixer.stolen == 1
    playing = {voice.effect.name for voice in mixer._voices}
    assert playing == {"e1", "e2", "e3"}, "the newest press must always be heard"


def test_a_voice_shorter_than_a_period_ends_cleanly():
    """The off-by-one that would tick at the end of every short effect."""
    mixer = Mixer(duck_db=0.0)
    mixer.trigger(effect_of(flat(100, 5000)))
    out = mixer.mix(silence(PERIOD))
    assert out[99, 0] == 5000
    assert out[100, 0] == 0
    assert mixer.voices == 0


# ── no music at all ───────────────────────────────────────────────────────────


def test_effects_play_with_no_music_present():
    """Silence is a valid music input, not an error.

    Two ways this happens in production and both must sound the same: nothing
    is queued in Music Assistant (snapclient's file player writes actual zeros
    at full rate, it does not stall), or snapclient is not running at all.
    """
    engine = MixerEngine(
        SilenceSource(), BufferSink, Mixer(duck_db=-12.0), period_frames=PERIOD
    )
    engine.mixer.trigger(effect_of(flat(PERIOD * 2, 6000)))
    engine.run_once()
    engine.run_once()
    audio = engine.sink.audio()
    assert len(audio) == PERIOD * 2
    assert audio.max() == 6000, "the effect must come out at its own level"
    assert engine.stats.periods == 2


# ── the fifo ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def fifo_path(tmp_path: Path) -> Path:
    return tmp_path / "snapfifo"


@pytest.fixture()
def socket_path():
    """A unix socket path short enough to bind.

    sun_path is 104 bytes on macOS and 108 on Linux, and pytest's tmp_path is
    already most of that before a filename is added: binding there fails with
    "AF_UNIX path too long". Worth knowing outside the suite too, which is why
    the production default is /run/musicbox/mixer.sock and not somewhere under
    a StateDirectory with a long name.
    """
    import shutil as shutil_
    import tempfile

    directory = tempfile.mkdtemp(prefix="mbx", dir="/tmp")
    try:
        yield Path(directory) / "m.sock"
    finally:
        shutil_.rmtree(directory, ignore_errors=True)


def test_fifo_reads_music_and_pads_a_short_period(fifo_path: Path):
    source = FifoSource(fifo_path, poll_ms=50)
    try:
        writer = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            half = PERIOD // 2
            os.write(writer, flat(half, 1234).tobytes())
            block = source.read(PERIOD)
        finally:
            os.close(writer)
        assert len(block) == PERIOD
        assert block[0, 0] == 1234
        # Padded with zeros, not truncated. A short ALSA write is accepted and
        # then simply does not play out until a full period arrives, which is a
        # dropout that no counter anywhere would report.
        assert block[-1, 0] == 0
        assert source.stats.short_periods == 1
    finally:
        source.close()


def test_fifo_that_disappears_and_comes_back(fifo_path: Path):
    """snapclient restarting must be a gap of silence and nothing else.

    The trap this guards: on an O_RDONLY fd, once a writer has attached and
    exited, poll() returns POLLHUP immediately and forever and read() returns
    b''. The loop would spin at 100% CPU, silently, from the moment snapclient
    restarted. Holding the write end ourselves (O_RDWR) is the fix, and this
    test is what proves it did not get reverted: the timing assertion below
    fails loudly if a dry read stops blocking.
    """
    source = FifoSource(fifo_path, poll_ms=50)
    try:
        writer = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        os.write(writer, flat(PERIOD, 999).tobytes())
        assert source.read(PERIOD)[0, 0] == 999

        # The writer goes away, exactly as a snapclient restart does.
        os.close(writer)
        started = time.monotonic()
        dry = source.read(PERIOD)
        elapsed = time.monotonic() - started
        assert not dry.any(), "no music means silence, not an error and not stale audio"
        assert source.stats.dry_periods == 1
        assert elapsed >= 0.045, "a dry read must BLOCK on the poll, not spin on POLLHUP"

        # And it comes back, with no reopen logic anywhere in the mixer.
        writer = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(writer, flat(PERIOD, 777).tobytes())
            assert source.read(PERIOD)[0, 0] == 777
        finally:
            os.close(writer)
    finally:
        source.close()


def test_fifo_drops_a_period_when_the_backlog_grows(fifo_path: Path):
    """Two clocks drift over an evening, and the correction is to DROP.

    The wrong fix, which has already been made twice on this box, is to let the
    backlog stand and grow a buffer around it. That turns drift into permanent
    invisible latency: the sound lands after the button, forever, with nothing
    reporting a fault.

    A small period so the whole excursion fits in a pipe on any kernel, but the
    threshold itself is measured rather than assumed: it has to sit above one
    snapclient chunk plus a period, or it fires on a healthy chain.
    """
    small = 240
    source = FifoSource(fifo_path, poll_ms=50)
    period_bytes = small * FRAME_BYTES
    need = source.limit_bytes(period_bytes) + 2 * period_bytes
    try:
        writer = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        written = 0
        value = 0
        try:
            while written < need:
                value += 1
                try:
                    # 960 bytes is under PIPE_BUF, so a pipe write is atomic:
                    # it either lands whole or fails, never half a frame.
                    written += os.write(writer, flat(small, value * 1000).tobytes())
                except BlockingIOError:
                    break
            block = source.read(small)
        finally:
            os.close(writer)
        if source.stats.backlog_bytes == 0:
            pytest.skip("this kernel does not report FIONREAD on a fifo")
        if written < need:
            pytest.skip("this kernel's pipe is too small to stage the excursion")
        assert source.stats.dropped_periods >= 1
        assert block[0, 0] > 1000, "the freshest audio must win, not the oldest"
    finally:
        source.close()


def test_a_healthy_backlog_is_never_dropped(fifo_path: Path):
    """The bug this one exists to keep dead.

    snapclient does not trickle: it writes one 9600 byte chunk every 50 ms, so
    2.5 periods land at once at the production period size, and the reader
    needs about another period of cushion to survive the gap until the next
    chunk. A healthy chain therefore swings up to roughly 3.5 periods of
    backlog every single cycle. With the threshold set below that (it defaulted
    to 2.0 once), the correction fires on a chain with nothing wrong with it,
    throws away a period of music, starves on the next read, and does it again
    20 times a second. It sounds like a broken speaker and every counter says
    the mixer is working.

    So: however small the configured ratio, the threshold is floored at one
    producer chunk plus one period, and a backlog that deep produces no drops
    and hands back the OLDEST audio, in order.
    """
    small = 240
    period_bytes = small * FRAME_BYTES
    source = FifoSource(fifo_path, poll_ms=20, max_backlog_periods=1.0)
    try:
        assert source.limit_bytes(period_bytes) == PRODUCER_CHUNK_BYTES + period_bytes
        writer = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            for value in (1, 2, 3, 4):
                os.write(writer, flat(small, value * 1000).tobytes())
            source.backlog = lambda: PRODUCER_CHUNK_BYTES + period_bytes
            block = source.read(small)
        finally:
            os.close(writer)
        assert source.stats.dropped_periods == 0
        assert block[0, 0] == 1000, "nothing was dropped, so the first period is first"
    finally:
        source.close()


def test_drift_correction_drops_music_and_never_grows_a_buffer(fifo_path: Path):
    """The drop decision, with the kernel taken out of the picture.

    FIONREAD on an O_RDWR fifo fd returns 0 on macOS however much is queued
    (measured), so the test above skips on a dev laptop and the one piece of
    logic with an actual decision in it would go uncovered on the machine where
    it gets edited. Stubbing the measurement covers the decision everywhere.
    """
    small = 240
    source = FifoSource(fifo_path, poll_ms=20)
    period_bytes = small * FRAME_BYTES
    try:
        writer = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            for value in (1, 2, 3, 4):
                os.write(writer, flat(small, value * 1000).tobytes())
            # Two periods past the threshold, reported honestly once, and then
            # the real reads take over: exactly two periods of music get thrown
            # away and the third is what comes out.
            over = source.limit_bytes(period_bytes) + 2 * period_bytes
            source.backlog = lambda: over
            block = source.read(small)
        finally:
            os.close(writer)
        assert source.stats.dropped_periods == 2
        assert block[0, 0] == 3000, "the stale audio is thrown away, not queued"
    finally:
        source.close()


def test_music_already_in_the_pipe_is_never_skipped_for_lack_of_time(fifo_path: Path):
    """A zero millisecond budget must still take what is already waiting.

    The order inside the read is the whole content of this test. Polling first
    means computing `int((deadline - now) * 1000)`, which rounds down: with a
    small poll budget, or with the thread preempted between taking the deadline
    and using it, that comes out 0 and the read returns empty WITH A FULL
    PERIOD SITTING IN THE PIPE. The mixer then splices in silence and counts it
    as the music having stopped, which sends whoever is debugging it at snapclient.
    """
    source = FifoSource(fifo_path, poll_ms=0)
    try:
        writer = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(writer, flat(PERIOD, 321).tobytes())
            block = source.read(PERIOD)
        finally:
            os.close(writer)
        assert block[0, 0] == 321
        assert source.stats.dry_periods == 0
    finally:
        source.close()


def test_a_dry_fifo_is_loud_once_and_says_when_the_music_returns(fifo_path: Path, capsys):
    """Silence with every unit green is this box's worst failure mode.

    So the music not arriving has to produce a line at the moment it happens,
    not a counter in a heartbeat 30 seconds later. Once, though: a warn per
    period is 50 lines a second from the audio thread into a pipe journald
    owns, and a blocked write between two ALSA writes is an underrun.
    """
    source = FifoSource(fifo_path, poll_ms=1)
    try:
        for _ in range(60):  # 60 periods of 20 ms, past the one second mark
            source.read(PERIOD)
        out = capsys.readouterr().out
        assert out.count("event=mixer_music_dry") == 1

        writer = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(writer, flat(PERIOD, 4242).tobytes())
            assert source.read(PERIOD)[0, 0] == 4242
        finally:
            os.close(writer)
        assert "event=mixer_music_back" in capsys.readouterr().out
    finally:
        source.close()


def test_a_steady_snapclient_never_makes_the_correction_fire(fifo_path: Path):
    """Two seconds of the real thing, at the real numbers, on any kernel.

    This is the system level version of the test above, and it is the one that
    would have caught the drift threshold being set below the producer's own
    quantum. The shape it recreates is the whole reason the FIFO is awkward:

      - snapclient writes 9600 bytes (50 ms) at a time and nothing in between.
      - the mixer reads 3840 bytes (20 ms) at a time, paced by the ALSA write.

    So the backlog sawtooths up to about three periods on a chain where
    absolutely nothing is wrong. A correction that fires at two periods throws
    away a period of music every cycle and then starves, forever, and every
    counter in the mixer says it is doing its job. Here that shows up as
    dropped periods and as gaps in the sequence of chunk numbers.

    backlog() is stubbed from the test's own bookkeeping rather than from
    FIONREAD, because FIONREAD on an O_RDWR fifo fd returns 0 on macOS: without
    this the assertion would pass on a laptop no matter what the threshold was.
    """
    source = FifoSource(fifo_path, poll_ms=5)
    pending = {"bytes": 0}
    real_read = source._read_exactly

    def counting_read(want: int) -> bytearray:
        got = real_read(want)
        pending["bytes"] -= len(got)
        return got

    source._read_exactly = counting_read
    source.backlog = lambda: pending["bytes"]

    writer = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
    seen: list[int] = []
    outbox = bytearray()

    def pump() -> None:
        """Push what the pipe will take. A partial write is not a problem here.

        There is one writer, so a short write just leaves the tail for the next
        call and the byte stream stays in order. macOS starts a fifo's buffer at
        512 bytes and grows it, so a 9600 byte write really does come back
        short on a laptop.
        """
        while outbox:
            try:
                written = os.write(writer, bytes(outbox[:4096]))
            except BlockingIOError:
                return
            if written <= 0:
                return
            del outbox[:written]
            pending["bytes"] += written

    try:
        chunk = 0
        next_chunk_ms = 0.0
        for index in range(100):  # 100 periods of 20 ms is 2 seconds
            while next_chunk_ms <= index * 20.0:
                chunk += 1
                outbox += flat(2400, chunk).tobytes()  # 50 ms, snapclient's quantum
                next_chunk_ms += 50.0
            pump()
            block = source.read(PERIOD)
            seen.extend(int(v) for v in np.unique(block[:, 0]))
    finally:
        os.close(writer)
        source.close()

    assert source.stats.dropped_periods == 0, "a healthy chain must never lose music"
    # Every chunk that was consumed came out, in order and with no gaps. A drop
    # would show up here as a missing number even if the counter above lied.
    heard = [value for value in dict.fromkeys(seen) if value]
    assert heard == list(range(1, len(heard) + 1)), f"chunks arrived out of order: {heard}"
    assert len(heard) >= 30, "two seconds should be about 40 chunks of music"
    # The padding is the cushion building itself once, at the start, not an
    # every-cycle event. More than a handful means the reader is starving.
    assert source.stats.short_periods <= 3
    assert source.stats.dry_periods == 0


def test_fifo_is_created_with_a_mode_snapclient_can_open(fifo_path: Path):
    """snapclient runs as another user, and umask 0077 would lock it out.

    That failure is invisible from this side: snapclient blocks inside
    fopen(path, "wb") in its constructor, the unit stays green, and the room is
    silent.
    """
    import stat

    old = os.umask(0o077)
    try:
        source = FifoSource(fifo_path)
    finally:
        os.umask(old)
    try:
        mode = os.stat(fifo_path).st_mode
        assert stat.S_ISFIFO(mode)
        assert mode & 0o060 == 0o060, "the group must be able to read and write"
    finally:
        source.close()


# ── the engine ────────────────────────────────────────────────────────────────


class BreakingSink(BufferSink):
    """A sink that dies once, the way bluealsa does when it restarts."""

    kind = "breaking"

    def __init__(self, fail_on: int = 2):
        super().__init__()
        self.fail_on = fail_on
        self.calls = 0

    def write(self, block):
        self.calls += 1
        if self.calls == self.fail_on:
            raise SinkBroken("bluealsa went away")
        return super().write(block)


def test_engine_survives_the_output_dying_and_keeps_draining(fifo_path: Path):
    """The single most important behaviour in this file.

    If the mixer stops draining the fifo, snapclient blocks in fwrite; its
    file player does not run on its own thread, so its io_context stalls and it
    stops reading the network too. A broken speaker would take the whole stream
    down with it. So: the source is read on every iteration whatever the output
    is doing, the failure is LOUD, and the sink is reopened.
    """
    sink = BreakingSink(fail_on=2)
    source = FifoSource(fifo_path, poll_ms=20)
    engine = MixerEngine(
        source, lambda: sink, Mixer(), period_frames=PERIOD, reopen_backoff=0.0
    )
    try:
        writer = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            for _ in range(4):
                os.write(writer, flat(PERIOD, 500).tobytes())
                engine.run_once()
        finally:
            os.close(writer)
        assert engine.stats.sink_failures == 1
        assert engine.last_error is None, "it must have reopened, not stayed broken"
        assert source.stats.periods == 4, "every period was drained regardless"
        assert engine.stats.periods == 4
        # Three writes landed: the broken one lost its period, which is a
        # dropout and not a stall.
        assert len(sink.blocks) == 3
    finally:
        engine.stop()


def test_engine_keeps_running_when_the_sink_cannot_be_opened_at_all():
    """No speaker at boot is normal here, and must not be fatal."""
    def refuse():
        raise SinkBroken("no such device")

    engine = MixerEngine(
        SilenceSource(), refuse, Mixer(), period_frames=PERIOD, reopen_backoff=10.0
    )
    for _ in range(3):
        engine.run_once()
    assert engine.stats.periods == 3
    assert engine.stats.periods_without_sink == 3
    assert engine.last_error is not None, "and it must say so, every time it retries"


def test_the_watchdog_notices_a_wedged_loop_and_notices_it_recovering(capsys):
    """The one fault the loop cannot report is the loop being stuck.

    A write that blocks forever takes the reporting thread with it: the room
    goes quiet, the unit stays active, and the journal simply stops. "The
    journal stopped" is not something anybody spots during an event, so a
    second thread watches the period counter and says it out loud.
    """
    from musicbox.mixer import LoopWatchdog

    engine = MixerEngine(SilenceSource(), BufferSink, Mixer(), period_frames=PERIOD)
    watchdog = LoopWatchdog(engine, timeout=0.05, interval=0.01)
    watchdog.start()
    try:
        time.sleep(0.2)  # the loop never runs, so the counter never moves
        assert watchdog.stalls >= 1
        assert "event=mixer_loop_stalled" in capsys.readouterr().out

        # And it takes the recovery back: a stall that is over has to stop
        # looking like an outage.
        for _ in range(3):
            engine.run_once()
            time.sleep(0.02)
        assert "event=mixer_loop_moving_again" in capsys.readouterr().out
    finally:
        watchdog.stop()


def test_engine_runs_as_a_thread_and_stops():
    engine = MixerEngine(
        SilenceSource(pace=True), BufferSink, Mixer(), period_frames=PERIOD
    )
    engine.start()
    try:
        deadline = time.monotonic() + 2.0
        while engine.stats.periods < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert engine.stats.periods >= 3
    finally:
        engine.stop()
    assert not threading.current_thread().name.startswith("musicbox-mixer")


# ── ALSA write semantics, without an ALSA device ──────────────────────────────


class FakePCM:
    """Stands in for a pyalsaaudio PCM. Only write() is exercised."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.writes = []

    def write(self, data):
        # len() of the (frames, 2) array, so these are FRAMES offered, which is
        # also what the real write() returns. Bytes are 4x this.
        self.writes.append(len(data))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def alsa_sink_with(pcm) -> AlsaSink:
    # __init__ opens a device, which is the one thing this suite must never do.
    sink = object.__new__(AlsaSink)
    sink.device = "bluealsa:DEV=00:00:00:00:00:00,PROFILE=a2dp"
    sink.periodsize = PERIOD
    sink.xruns = 0
    sink._pcm = pcm
    return sink


def test_an_underrun_is_a_return_value_and_not_an_exception():
    """-EPIPE comes back as -32, AFTER the library has already recovered.

    Getting this backwards is a crash on a completely normal xrun. The stream
    is already prepared again by the time we see the number, so the right
    response is to count it and move on to the next period.
    """
    pcm = FakePCM([-32])
    sink = alsa_sink_with(pcm)
    assert sink.write(flat(PERIOD, 1)) == 0
    assert sink.xruns == 1


def test_anything_other_than_an_underrun_reopens_the_device():
    """A dead bluealsa transport surfaces as an exception, not as a number."""
    pcm = FakePCM([RuntimeError("No such device [bluealsa:DEV=...]")])
    sink = alsa_sink_with(pcm)
    with pytest.raises(SinkBroken):
        sink.write(flat(PERIOD, 1))


def test_a_short_write_finishes_the_period():
    """write() returns FRAMES, and a dropped tail would be an audible buzz."""
    pcm = FakePCM([400, 560])
    sink = alsa_sink_with(pcm)
    assert sink.write(flat(PERIOD, 1)) == PERIOD
    assert pcm.writes == [PERIOD, PERIOD - 400], "the remainder must be offered again"


# ── the effect cache ──────────────────────────────────────────────────────────


def write_raw(path: Path, pcm: np.ndarray) -> Path:
    path.write_bytes(pcm.tobytes())
    return path


def test_cache_loads_raw_effects_by_stem_and_filename(tmp_path: Path):
    sfx = tmp_path / "sfx"
    sfx.mkdir()
    write_raw(sfx / "airhorn.raw", flat(4800, 1000))
    cache = EffectCache(sfx, tmp_path / "pcm")
    cache.load()
    assert cache.names() == ["airhorn"]
    assert cache.get("airhorn") is not None
    assert cache.get("AIRHORN") is not None, "names are matched case insensitively"
    assert cache.get("airhorn.raw") is not None
    assert cache.get("nope") is None
    assert cache.get("airhorn").seconds == pytest.approx(0.1)


def test_cache_rejects_a_decode_that_is_not_whole_frames(tmp_path: Path):
    """mpg123 emits ZERO bytes on a non-mp3 and says nothing about it.

    Without this check a .wav renamed to .mp3 becomes a silent pad with no line
    in any log, and a truncated decode becomes a burst of noise. Both are
    rejected before anything is cached.
    """
    sfx = tmp_path / "sfx"
    sfx.mkdir()
    (sfx / "empty.raw").write_bytes(b"")
    (sfx / "ragged.raw").write_bytes(b"\x01\x02\x03")  # not a multiple of 4
    cache = EffectCache(sfx, tmp_path / "pcm")
    cache.load()
    assert cache.names() == []


def test_cache_writes_atomically_and_reuses_the_file(tmp_path: Path):
    """A half written .raw surviving a crash is full scale garbage on playback.

    So the decode goes to a temp file and is renamed. The assertion that no
    .tmp survives is the cheap proxy for that, and the second load proves the
    cache is actually read back rather than silently re-decoding every start.
    """
    sfx = tmp_path / "sfx"
    sfx.mkdir()
    pcm_dir = tmp_path / "pcm"
    source = sfx / "beep.wav"
    _write_wav(source, tone(RATE // 10))

    cache = EffectCache(sfx, pcm_dir, decoders=_fake_decoders())
    cache.load()
    assert cache.get("beep") is not None
    cached = list(pcm_dir.glob("*.raw"))
    assert len(cached) == 1
    assert not list(pcm_dir.glob("*.tmp"))

    # Second load must come off disk. A decoder that would fail proves it.
    again = EffectCache(sfx, pcm_dir, decoders=(("boom", ("false", "{input}")),))
    again.load()
    assert again.get("beep") is not None
    assert again.get("beep").decoder == "cache"


def test_cache_redecodes_when_the_source_file_changes(tmp_path: Path):
    sfx = tmp_path / "sfx"
    sfx.mkdir()
    pcm_dir = tmp_path / "pcm"
    source = sfx / "beep.wav"
    _write_wav(source, flat(4800, 1000))
    cache = EffectCache(sfx, pcm_dir, decoders=_fake_decoders())
    cache.load()
    first = cache.get("beep").frames

    _write_wav(source, flat(9600, 1000))
    os.utime(source, (time.time() + 10, time.time() + 10))
    cache.load()
    assert cache.get("beep").frames != first


def test_cache_prunes_the_decode_it_no_longer_needs(tmp_path: Path):
    """The cache key is (name, size, mtime), so every edit orphans a file.

    A megabyte of decoded PCM per corrected sound effect, kept forever, in a
    CacheDirectory nothing else prunes, on a box that boots off an SD card. Not
    an evening's problem, but "grows without bound and nobody looks" is how an
    SD card dies.
    """
    sfx = tmp_path / "sfx"
    sfx.mkdir()
    pcm_dir = tmp_path / "pcm"
    source = sfx / "beep.wav"
    _write_wav(source, flat(4800, 1000))
    cache = EffectCache(sfx, pcm_dir, decoders=_fake_decoders())
    cache.load()
    first = {p.name for p in pcm_dir.glob("*.raw")}
    assert len(first) == 1

    _write_wav(source, flat(9600, 1000))
    os.utime(source, (time.time() + 10, time.time() + 10))
    cache.load()
    now = {p.name for p in pcm_dir.glob("*.raw")}
    assert len(now) == 1, "the superseded decode is gone, not stacked up"
    assert now != first

    # And the effect really was reloaded, so the prune did not eat the live one.
    assert cache.get("beep").frames == 9600


def test_an_unreadable_sfx_dir_does_not_wipe_the_cache(tmp_path: Path):
    """Prune only on a listing that worked.

    A transient permission error would otherwise empty the entire decode cache,
    which turns a five second blip into a re-decode of everything at the worst
    possible moment.
    """
    sfx = tmp_path / "sfx"
    sfx.mkdir()
    pcm_dir = tmp_path / "pcm"
    _write_wav(sfx / "beep.wav", flat(4800, 1000))
    cache = EffectCache(sfx, pcm_dir, decoders=_fake_decoders())
    cache.load()
    assert len(list(pcm_dir.glob("*.raw"))) == 1

    gone = EffectCache(tmp_path / "not-here", pcm_dir, decoders=_fake_decoders())
    gone.load()
    assert len(list(pcm_dir.glob("*.raw"))) == 1, "a missing sfx dir must not delete decodes"


def _write_wav(path: Path, pcm: np.ndarray) -> None:
    sink = FileSink(path, wav=True)
    sink.write(pcm)
    sink.close()


def _fake_decoders():
    """A decoder that is really just "strip the wav header".

    ffmpeg is not guaranteed to exist on the machine running this suite, and a
    test that skips itself when it is missing is a test that does not run. This
    exercises the whole decode, validate, cache-atomically path with a command
    that is always available.
    """
    script = (
        "import sys,pathlib;"
        "d=pathlib.Path(sys.argv[1]).read_bytes();"
        "i=d.find(b'data');"
        "sys.stdout.buffer.write(d[i+8:] if i>=0 else d)"
    )
    return (("python-wav", (sys.executable, "-c", script, "{input}")),)


# ── the control surface ───────────────────────────────────────────────────────


def service_for(tmp_path: Path, socket_path: Path | None = None) -> MixerService:
    """A loaded service over a real sfx directory.

    The files are .wav and not .raw because app.resolve_sfx only accepts the
    extensions musicbox has always accepted, and these same directories are
    handed to create_app further down. Two surfaces disagreeing about which
    files count is precisely the bug that would show up as "the button 404s but
    the sound is right there".
    """
    sfx = tmp_path / "sfx"
    sfx.mkdir(exist_ok=True)
    _write_wav(sfx / "airhorn.wav", flat(4800, 6000))
    _write_wav(sfx / "big airhorn.wav", flat(4800, 3000))
    cache = EffectCache(sfx, tmp_path / "pcm", decoders=_fake_decoders())
    cache.load()
    engine = MixerEngine(SilenceSource(), BufferSink, Mixer(), period_frames=PERIOD)
    return MixerService(cache, engine, socket_path=socket_path)


def test_control_commands(tmp_path: Path):
    service = service_for(tmp_path)
    assert service.handle("ping").startswith("ok pong")
    assert "count=2" in service.handle("list")

    played = service.handle("play airhorn")
    assert played.startswith("ok play")
    assert "name=airhorn" in played

    assert service.handle("play nope").startswith("err not_found")
    assert service.handle("wat").startswith("err unknown_command")
    assert service.handle("play").startswith("err no_name")
    assert service.handle("play airhorn gain=loud").startswith("err bad_gain")
    # float() accepts these two and nothing downstream would complain: a NaN
    # gain makes the whole float32 accumulator NaN, np.clip passes NaN through
    # untouched, and .astype(int16) of NaN is a full scale garbage sample at
    # whatever volume the room is set to.
    assert service.handle("play airhorn gain=nan").startswith("err bad_gain")
    assert service.handle("play airhorn gain=inf").startswith("err bad_gain")
    assert service.handle("play airhorn gain=-400").startswith("err bad_gain")
    assert service.handle("").startswith("err empty")
    assert service.handle("stop").startswith("ok stop")
    assert "effects=2" in service.handle("stats")


def test_control_handles_a_name_with_spaces(tmp_path: Path):
    """`big airhorn #2.mp3` is an ordinary thing to drop in from Finder.

    A protocol that splits on spaces turns that into a 404 for a file that is
    sitting right there, which is precisely the bug that already bit the MCP
    proxy once over URL quoting.
    """
    service = service_for(tmp_path)
    assert service.handle('play "big airhorn"').startswith("ok play")
    # Unquoted too, because a human typing into nc will not quote anything.
    assert service.handle("play big airhorn").startswith("ok play")


def test_trigger_reaches_the_audio_thread_through_the_queue(tmp_path: Path):
    """The control thread never touches the mixer state directly.

    It only enqueues, and the audio thread drains at the top of its period. So
    the voice count moves on the next run_once and not before, which is exactly
    the one period of latency the ledger accounts for.
    """
    service = service_for(tmp_path)
    service.handle("play airhorn")
    assert service.engine.mixer.voices == 0
    service.engine.run_once()
    assert service.engine.mixer.voices == 1
    assert service.engine.sink.audio().max() == 6000


def test_control_socket_round_trip(tmp_path: Path, socket_path: Path):
    import socket as socketlib

    service = service_for(tmp_path, socket_path)
    service.serve_forever()
    try:
        client = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(socket_path))
        # Two commands down ONE connection, because a person with `nc -U`
        # leaves it open and musicbox may reuse it.
        client.sendall(b"ping\nplay airhorn\n")
        data = b""
        while data.count(b"\n") < 2:
            data += client.recv(4096)
        client.close()
        lines = data.decode().splitlines()
        assert lines[0].startswith("ok pong")
        assert lines[1].startswith("ok play")
    finally:
        service.close()
    assert not socket_path.exists(), "a stale socket makes the next start fail to bind"


# ── the client musicbox uses ──────────────────────────────────────────────────


def test_client_parses_and_falls_back(tmp_path: Path, socket_path: Path):
    import asyncio

    from musicbox.mixer_client import MixerClient, MixerUnavailable, parse

    parsed = parse('ok play name="big airhorn" voices=2')
    assert parsed["ok"] and parsed["name"] == "big airhorn" and parsed["voices"] == "2"
    bad = parse("err not_found name=nope")
    assert not bad["ok"] and bad["error"] == "not_found"

    settings = MixerConfig(enable=True, socket=socket_path, timeout=1.0)
    client = MixerClient(settings)

    # Nothing listening: this must be a MixerUnavailable and not a crash, since
    # the caller's job is to take the announcement path instead.
    with pytest.raises(MixerUnavailable):
        asyncio.run(client.play("airhorn"))
    assert client.last_ok is False

    service = service_for(tmp_path, socket_path)
    service.serve_forever()
    try:
        answer = asyncio.run(client.play("big airhorn"))
        assert answer["name"] == "big airhorn"
        assert client.last_ok is True
        with pytest.raises(MixerUnavailable):
            asyncio.run(client.play("nope"))
    finally:
        service.close()


def test_client_is_inert_when_the_mixer_is_disabled():
    import asyncio

    from musicbox.mixer_client import MixerClient, MixerUnavailable

    client = MixerClient(MixerConfig())  # enable defaults to False
    assert client.enabled is False
    with pytest.raises(MixerUnavailable):
        asyncio.run(client.play("airhorn"))
    # ping is the one that must NOT raise: /health calls it and a health check
    # that 500s because the optional feature is off is worse than useless.
    assert asyncio.run(client.ping()) is False


def test_every_client_failure_is_a_mixer_unavailable(socket_path: Path):
    """Including the ones nobody predicted.

    perform_sfx only falls back to the announcement path for MixerUnavailable.
    Anything else escaping this module comes out of POST /sfx as a 500, which
    means the button does nothing: strictly worse than the behaviour the box
    had before the mixer existed.
    """
    import asyncio

    from musicbox.mixer_client import MixerClient, MixerUnavailable

    client = MixerClient(MixerConfig(enable=True, socket=socket_path))

    async def explode(line: str) -> str:
        raise RuntimeError("something nobody thought of")

    client._roundtrip = explode
    with pytest.raises(MixerUnavailable):
        asyncio.run(client.play("airhorn"))
    assert client.last_ok is False


# ── settings ──────────────────────────────────────────────────────────────────


def test_mixer_is_off_by_default():
    """The rule that overrides everything: opt in, default off."""
    settings = MixerConfig.from_env({})
    assert settings.enable is False
    assert settings.device == ""
    # And the buffer defaults are the ones measured to work on this box.
    assert settings.periodsize * settings.periods / 48.0 == 200.0
    assert settings.describe()["buffer_ms"] == 200.0


def test_mixer_settings_from_env():
    settings = MixerConfig.from_env({
        "MUSICBOX_MIXER": "yes",
        "MUSICBOX_MIXER_SOCKET_MODE": "660",
        "MUSICBOX_MIXER_DUCK_DB": "-9",
        "MUSICBOX_MIXER_FIFO": "",
        "MUSICBOX_MIXER_VOICES": "4",
    })
    assert settings.enable is True
    # Octal, not decimal. "660" read as decimal is 0o1224, a mode with the
    # setuid bit set and no complaint from chmod.
    assert settings.socket_mode == 0o660
    assert settings.duck_db == -9.0
    assert settings.fifo is None, "an empty value means no fifo, for file renders"
    assert settings.polyphony == 4


# ── offline rendering, which is how a human checks this without the speaker ───


def test_standalone_renders_a_wav_with_the_effect_in_it(tmp_path: Path):
    """`musicbox-mixer --out` must produce something you can actually listen to.

    This is the path the verify agent uses and the path a human uses to hear
    the duck without going anywhere near a speaker a room is listening to.
    """
    from musicbox.mixer import main

    sfx = tmp_path / "sfx"
    sfx.mkdir()
    write_raw(sfx / "airhorn.raw", flat(RATE // 2, 8000))
    music = tmp_path / "music.wav"
    _write_wav(music, flat(RATE * 2, 10000))
    out = tmp_path / "mix.wav"

    rc = main([
        "--music", str(music),
        "--out", str(out),
        "--sfx-dir", str(sfx),
        "--cache-dir", str(tmp_path / "pcm"),
        "--socket", "",
        "--fire", "airhorn@0.5",
        "--duration", "1.5",
    ])
    assert rc == 0
    rendered = frames_of(out.read_bytes()[44:])
    assert len(rendered) > RATE
    # Before the effect: untouched music. During it: ducked music plus effect.
    assert rendered[100, 0] == 10000
    at_effect = rendered[int(RATE * 0.7), 0]
    assert at_effect > 10000, "the effect has to be audible on top"
    # And after it, the music comes back to where it was.
    assert rendered[int(RATE * 1.4), 0] == pytest.approx(10000, abs=200)


def test_check_prints_settings_and_touches_nothing(tmp_path: Path, capsys):
    from musicbox.mixer import main

    assert main(["--check", "--socket", "", "--sfx-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "event=mixer_config" in out
    assert "buffer_ms=200.0" in out


# ── how musicbox routes an sfx ────────────────────────────────────────────────


# The whole MAClient surface app.py uses already exists in test_app.StubMA, and
# these tests need /health and the "cut" path as well as play_announcement.
# Importing it keeps one definition of what Music Assistant looks like: a second
# stub would drift, and the drift would show up as a routing test that passes
# against a fake nothing else agrees with.
from test_app import StubMA  # noqa: E402


def _announcements(ma) -> int:
    return sum(1 for call in ma.calls if call[0] == "play_announcement")


def app_client(tmp_path: Path, settings: MixerConfig):
    from fastapi.testclient import TestClient

    from musicbox.app import create_app
    from musicbox.config import Config

    sfx_dir = tmp_path / "sfx"
    ma = StubMA()
    config = Config(
        host="127.0.0.1", port=8099, player="musicbox", sfx_dir=sfx_dir, mixer=settings
    )
    # No context manager, so the lifespan never runs and nothing tries to open
    # a real websocket. Same trick the rest of the suite uses.
    return TestClient(create_app(config, ma)), ma


def test_sfx_goes_to_music_assistant_when_the_mixer_is_off(tmp_path: Path):
    """THE rule: off by default, and off means byte for byte the old path.

    If this test ever fails, a box that was playing music to a room has just
    had its audio chain changed by an upgrade nobody opted into.
    """
    service = service_for(tmp_path)  # creates the sfx files
    service.close()
    client, ma = app_client(tmp_path, MixerConfig())

    body = client.post("/sfx/airhorn", json={"mode": "over"}).json()
    assert body["ok"] is True
    assert body["path"] == "announcement"
    assert _announcements(ma) == 1
    assert client.get("/health").json()["mixer_enabled"] is False
    assert client.get("/mixer").json() == {
        "ok": True,
        "enabled": False,
        "detail": "the mixer is off, so sfx go through Music Assistant announcements",
    }


def test_sfx_goes_to_the_mixer_when_it_is_on(tmp_path: Path, socket_path: Path):
    service = service_for(tmp_path, socket_path)
    service.serve_forever()
    try:
        client, ma = app_client(
            tmp_path, MixerConfig(enable=True, socket=socket_path, timeout=2.0)
        )
        body = client.post("/sfx/airhorn", json={"mode": "over"}).json()
        assert body["ok"] is True
        assert body["path"] == "mixer"
        assert body["mode_used"] == "over"
        assert _announcements(ma) == 0, "Music Assistant must not have been involved at all"

        # And the effect really is queued for the audio thread, not just acked.
        service.engine.run_once()
        assert service.engine.mixer.voices == 1

        assert client.get("/health").json()["mixer_ok"] is True
        assert client.get("/mixer").json()["enabled"] is True
        assert client.post("/mixer/stop").json()["stopped"] is True
    finally:
        service.close()


def test_sfx_falls_back_when_the_mixer_is_enabled_but_dead(tmp_path: Path, socket_path: Path):
    """A dead mixer must never mean a button that does nothing.

    The announcement path is what the box did yesterday and it works. Falling
    back to it and SAYING SO in the answer is strictly better than failing the
    request, which is how a silent room gets blamed on the person pressing.
    """
    service_for(tmp_path).close()  # sfx files exist, but nothing is listening
    client, ma = app_client(
        tmp_path, MixerConfig(enable=True, socket=socket_path, timeout=0.5)
    )
    body = client.post("/sfx/airhorn", json={"mode": "over"}).json()
    assert body["ok"] is True
    assert body["path"] == "announcement"
    assert body["fell_back"] is True
    assert "mixer" in body["reason"]
    assert _announcements(ma) == 1
    assert client.get("/health").json()["mixer_ok"] is False
    assert client.get("/mixer").json()["ok"] is False


def test_cut_mode_never_uses_the_mixer(tmp_path: Path, socket_path: Path):
    """"cut" means pause, play, resume, which is a queue operation.

    The mixer cannot pause Music Assistant's queue and should not pretend to.
    Silently turning a requested "cut" into a ducked overlay would be the
    opposite of what the caller asked for.
    """
    service = service_for(tmp_path, socket_path)
    service.serve_forever()
    try:
        client, ma = app_client(
            tmp_path, MixerConfig(enable=True, socket=socket_path, timeout=2.0)
        )
        body = client.post("/sfx/airhorn", json={"mode": "cut"}).json()
        assert body["path"] == "announcement"
        assert body["mode_used"] == "cut"
        assert _announcements(ma) == 1
    finally:
        service.close()
