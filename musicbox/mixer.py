"""musicbox-mixer: the effects play OVER the music, in software.

WHY THIS EXISTS
Music Assistant has no ducking anywhere (grep its tree for "duck" and the only
hit is a comment in the demo provider), so `POST /sfx` has always gone through
MA's ANNOUNCEMENT path: the music is silenced, the effect plays alone, the
music resumes. Worse, MA serialises announcements and waits for each one to
finish through the snapserver and the A2DP buffers. Measured on the live box:
pressing the same effect four times 0.35s apart produced four playbacks spaced
about 3 seconds. That is not a sound effect, that is a queue.

There is nowhere else for a second sound to go either. The output is a single
A2DP stream and bluez-alsa will not mix two writers into it. So the only way to
get an effect ON TOP of the music is to own the mix ourselves:

    MA -> snapserver -> snapclient --player file:filename=<fifo>
                                      |
                                 musicbox-mixer   <- effects triggered here
                                      |
                                 ALSA (bluealsa:DEV=<mac>,PROFILE=a2dp) -> speaker

snapclient can write raw PCM to a file instead of opening ALSA itself
(`--player file:filename=<path>,mode=w`), and the stream on this box is
48000:16:2. So the music arrives here as plain bytes, we add the effects, and
we are the ones who write to the speaker.

THE RULE THIS FILE OBEYS: silence with every service green is the worst failure
mode on this box, worse than a stutter, because nothing looks broken. So every
fault here is LOUD in the journal, the FIFO is drained even when the output is
dead, and no code path can decide to stop writing and stay quiet about it.

LATENCY THIS ADDS, over and above the chain that already exists today:
    FIFO transit            25 ms average, 50 ms worst (snapclient emits one
                            50 ms chunk at a time, hard-coded, not an option)
    FIFO backlog cap        up to 85 ms in a drift excursion, ~0 steady state
    block granularity       20 ms (one period)
    mix compute             0.03 to 0.06 ms measured on the Pi 5, 4 voices
    ------------------------------------------------------------------
    ~45 ms typical, ~70 ms worst, ~105 ms in a catch-up excursion.
The 200 ms ALSA buffer and the opaque A2DP buffer are NOT added by the mixer,
they are already in the path today. Trigger to audible therefore lands around
220 to 420 ms, dominated by buffers that already exist. The win is not the
absolute number: it is that a retrigger costs one memory copy instead of a
serialised announcement, so ten presses in a second are ten sounds.

DO NOT paper over jitter by growing a buffer. That mistake has been made twice
on this box: 80 ms of ALSA buffer stutters (484 XRUNs in 5 min), 500 ms stops
playing entirely and reports healthy while doing it. 200 ms is the one depth
proven to work, and it is what the defaults here reproduce.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import math
import os
import queue
import select
import shlex
import shutil
import socketserver
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from . import logs
from .config import MixerConfig

# ── the format, stated once ───────────────────────────────────────────────────
# 48000:16:2 is what the live snapserver stream is (its own log says
# "(Player) Sampleformat: 48000:16:2, stream: 48000:16:2"). It is asserted here
# rather than negotiated because there is nothing to negotiate WITH: the FIFO
# carries headerless PCM. If MA ever changes the stream format the bytes would
# silently reinterpret at the wrong rate and everything would play at the wrong
# pitch with no error anywhere, which is why snapclient should also be pinned
# with --sampleformat 48000:16:2 so the change fails at the resampler instead.
RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 2
FRAME_BYTES = CHANNELS * SAMPLE_BYTES  # 4
STREAM_FORMAT = f"{RATE}:16:{CHANNELS}"

# bytes / 192 = milliseconds, at this format. Handy when reading a FIFO backlog.
BYTES_PER_MS = RATE * FRAME_BYTES // 1000

# What snapclient hands us in one go. From snapcast's client/player/file_player.cpp:
#
#     static constexpr auto kDefaultBuffer = 50ms;
#     requestAudio(): numFrames = msRate * 50; fwrite(buffer, 1, needed, file_);
#
# 50 ms is a compile-time constant with no option for it, so the FIFO does not
# trickle: it jumps by 9600 bytes every 50 ms and is drained a period at a
# time. That granularity is the reason the drift correction below has a floor.
# Getting this number wrong does not fail, it just makes the music worse in a
# way that looks like a stutter, so it is stated once and used everywhere.
PRODUCER_CHUNK_BYTES = 9600

# Linux fcntl commands for pipe capacity. Not in the fcntl module, and absent
# entirely on macOS, where the calls below fail with EINVAL and are caught.
F_SETPIPE_SZ = 1031
F_GETPIPE_SZ = 1032


def db_to_gain(db: float) -> float:
    """dB to a linear multiplier. -6 dB is half the amplitude.

    Non-finite input is refused here rather than propagated, because float()
    accepts "nan" and "inf" happily and both arrive from places a person can
    type into: an environment variable in a unit file, or `play airhorn
    gain=nan` on the control socket. One NaN gain turns the whole float32
    accumulator into NaN, np.clip passes NaN straight through, and
    .astype(int16) of NaN is undefined: in practice a full scale garbage
    sample, at whatever volume the room is set to. 0 dB is the inert answer.

    The clamp on the finite path is for the same class of reason at the other
    end: 10 ** (400 / 20) is inf, and inf * 0 is NaN again.
    """
    if not math.isfinite(db):
        logs.warn("mixer_bad_db", value=str(db))
        return 1.0
    return float(10.0 ** (min(max(db, -120.0), 60.0) / 20.0))


def silence(frames: int) -> np.ndarray:
    return np.zeros((frames, CHANNELS), dtype=np.int16)


def frames_of(data: bytes) -> np.ndarray:
    """Raw interleaved s16le bytes to an (frames, 2) int16 array."""
    usable = len(data) - (len(data) % FRAME_BYTES)
    return np.frombuffer(bytes(data[:usable]), dtype="<i2").reshape(-1, CHANNELS)


# ── sinks ─────────────────────────────────────────────────────────────────────


class SinkBroken(RuntimeError):
    """The output device went away and has to be reopened.

    Separate from every other exception on purpose: this one is recoverable by
    reopening, and the engine treats it as "log loudly, back off, retry" rather
    than as a bug.
    """


class BufferSink:
    """Collects what was written. The test suite's output device.

    The whole point of the sink being injectable is that the suite must run
    with no sound card at all, on a laptop, in CI, anywhere.
    """

    kind = "buffer"

    def __init__(self) -> None:
        self.blocks: list[np.ndarray] = []

    def write(self, block: np.ndarray) -> int:
        # copy(): the engine is free to reuse its own buffers, and a test that
        # asserts on a block mutated after the fact is a test that lies.
        self.blocks.append(np.array(block, dtype=np.int16, copy=True))
        return len(block)

    def audio(self) -> np.ndarray:
        if not self.blocks:
            return silence(0)
        return np.concatenate(self.blocks, axis=0)

    def close(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {"sink": "buffer", "frames": sum(len(b) for b in self.blocks)}


class FileSink:
    """Writes the mix to a file so a human can hear it without the speaker.

    This is how you check the duck, the polyphony and the clipping without
    touching a box that is playing to a room. `--out mix.wav` gives a file that
    any player opens; raw is there because that is what the rest of the chain
    speaks.

    pace=True sleeps to real time. The ALSA write is normally the clock; a file
    write returns instantly, so without pacing a "10 second" render finishes in
    a few milliseconds. That is what you want for a test and not what you want
    when you are triggering effects by hand over the control socket.
    """

    kind = "file"

    def __init__(self, path: str | os.PathLike[str], *, wav: bool = False, pace: bool = False):
        self.path = Path(path)
        self.wav = wav or self.path.suffix.lower() == ".wav"
        self.pace = pace
        self._fh = open(self.path, "wb")
        self._frames = 0
        self._started = time.monotonic()
        if self.wav:
            # Sizes patched on close. Written up front so the file is openable
            # by something else the moment it exists.
            self._fh.write(self._wav_header(0))

    @staticmethod
    def _wav_header(data_bytes: int) -> bytes:
        byte_rate = RATE * FRAME_BYTES
        return b"".join([
            b"RIFF",
            struct.pack("<I", 36 + data_bytes),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, CHANNELS, RATE, byte_rate, FRAME_BYTES, 16),
            b"data",
            struct.pack("<I", data_bytes),
        ])

    def write(self, block: np.ndarray) -> int:
        self._fh.write(np.ascontiguousarray(block, dtype=np.int16).tobytes())
        self._frames += len(block)
        if self.pace:
            ahead = self._frames / RATE - (time.monotonic() - self._started)
            if ahead > 0:
                time.sleep(ahead)
        return len(block)

    def close(self) -> None:
        if self._fh.closed:
            return
        if self.wav:
            self._fh.flush()
            self._fh.seek(0)
            self._fh.write(self._wav_header(self._frames * FRAME_BYTES))
        self._fh.close()

    def describe(self) -> dict[str, Any]:
        return {"sink": "file", "path": str(self.path), "wav": self.wav}


class AlsaSink:
    """The real output: an ALSA PCM, in production the bluealsa one.

    Two things about this device are not discoverable and must be configuration:

      1. The device string. `alsaaudio.pcms()` on this box lists NO entry
         containing "bluealsa", with or without ALSA_PLUGIN_DIR, because
         bluez-alsa ships no ALSA namehints. The MAC goes in the string exactly
         as it does in snapclient's --soundcard.
      2. ALSA_PLUGIN_DIR must be set in the ENVIRONMENT of this process, the
         same as for snapclient, or alsa-lib cannot dlopen
         libasound_module_pcm_bluealsa.so. Measured: without it the open fails
         with "No such device or address", which reads exactly like "the
         speaker is not connected" and sends you to bluetoothctl instead of to
         the unit file. Checked in open() so that lie never gets told.

    The process must also be in group `audio`. bluez-alsa's D-Bus policy grants
    send_destination=org.bluealsa to root and to group audio and to nobody
    else; without it the open fails with "Permission denied" plus a "Rejected
    send message" line, which is a DIFFERENT failure from the plugin dir one
    and has to be diagnosed separately.
    """

    kind = "alsa"

    def __init__(self, device: str, *, periodsize: int, periods: int, prefill_periods: int = 3):
        self.device = device
        self.periodsize = periodsize
        self.periods = periods
        self.prefill_periods = prefill_periods
        self.xruns = 0
        self._aa = None
        self._pcm = None
        self.info: dict[str, Any] = {}
        self.open()

    def open(self) -> None:
        try:
            import alsaaudio  # noqa: PLC0415 - optional dep, absent on dev laptops
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise SinkBroken(
                "pyalsaaudio is not installed, so there is no ALSA output. "
                "Install python3Packages.pyalsaaudio or run with --out to render a file."
            ) from exc
        self._aa = alsaaudio
        if not os.environ.get("ALSA_PLUGIN_DIR") and self.device.startswith("bluealsa"):
            # Refuse rather than fail with the misleading error. The unit sets
            # this; a hand-run process usually does not, and the error you get
            # instead costs an hour.
            raise SinkBroken(
                f"ALSA_PLUGIN_DIR is unset and the device is {self.device!r}. alsa-lib "
                "cannot load libasound_module_pcm_bluealsa.so without it and would fail "
                "with 'No such device or address', which looks like a disconnected "
                "speaker. Set ALSA_PLUGIN_DIR=<bluez-alsa>/lib/alsa-lib."
            )
        try:
            self._pcm = alsaaudio.PCM(
                alsaaudio.PCM_PLAYBACK,
                alsaaudio.PCM_NORMAL,
                device=self.device,
                rate=RATE,
                channels=CHANNELS,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=self.periodsize,
                periods=self.periods,
            )
        except Exception as exc:  # noqa: BLE001 - ALSAAudioError and friends
            raise SinkBroken(f"could not open {self.device!r}: {exc}") from exc

        # alsapcm_setup() IGNORES individual parameter failures: it calls
        # set_rate_near, set_channels and the rest, then reads back whatever it
        # actually got. Asking for 48000 and silently getting 44100 is a real
        # possible outcome, and it would play everything at the wrong pitch
        # with nothing in any log. So read it back, print it, and refuse.
        #
        # .get() on every key, never []: info() silently OMITS any key whose
        # snd_pcm_hw_params_get_* call errored. The first probe written against
        # this API died with KeyError: 'get_periods' on a device that was
        # otherwise working perfectly.
        raw = {}
        with contextlib.suppress(Exception):
            raw = dict(self._pcm.info())
        period_size = raw.get("period_size")
        buffer_size = raw.get("buffer_size")
        self.info = {
            "device": self.device,
            "rate": raw.get("rate"),
            "channels": raw.get("channels"),
            "format": raw.get("format_name"),
            "period_size": period_size,
            "period_time_us": raw.get("period_time"),
            "buffer_size": buffer_size,
            # Derived, not read: see the get_periods note above.
            "periods": (buffer_size // period_size) if (period_size and buffer_size) else None,
            "buffer_ms": round(buffer_size / RATE * 1000, 1) if buffer_size else None,
        }
        logs.log("mixer_alsa_open", **self.info)
        rate = raw.get("rate")
        channels = raw.get("channels")
        if (rate and int(rate) != RATE) or (channels and int(channels) != CHANNELS):
            self.close()
            raise SinkBroken(
                f"{self.device!r} negotiated {rate}:{channels} and the stream is "
                f"{STREAM_FORMAT}. Playing it anyway would come out at the wrong pitch, "
                "which is a failure nothing else in the chain would report."
            )

        # pyalsaaudio never calls snd_pcm_sw_params, so start_threshold and
        # avail_min are whatever alsa-lib defaulted them to and cannot be tuned
        # from Python at all. Prefilling means playback does not start against
        # a nearly empty buffer, which is the cheap half of the same fix.
        for _ in range(self.prefill_periods):
            self.write(silence(self.periodsize))

    def write(self, block: np.ndarray) -> int:
        """Write a whole period, or count the underrun that stopped it.

        The loop is for the partial write. In PCM_NORMAL, snd_pcm_writei blocks
        until every frame is accepted, so a short return should not happen and
        this is belt and braces; the reason it is belt and braces rather than
        an assert is that the ioplug under bluealsa is not the kernel driver
        the blocking guarantee is usually described against, and a silently
        dropped tail of every period is the kind of fault you hear as a buzz
        and cannot find.
        """
        offset = 0
        total = len(block)
        while offset < total:
            written = self._write_once(block[offset:])
            if written <= 0:
                # Either an underrun (already counted and already recovered by
                # the library) or nothing accepted. Give up on this period
                # rather than spin: the next one is 20 ms away and fresher.
                break
            offset += written
        return offset

    def _write_once(self, block: np.ndarray) -> int:
        if self._pcm is None:  # pragma: no cover - guarded by the engine
            raise SinkBroken("the PCM is closed")
        try:
            # The array goes in directly. pyalsaaudio takes any C-contiguous
            # buffer, and .tobytes() first measured 0.78 us/call against 0.52
            # us/call for the array. ascontiguousarray because a strided slice
            # raises ValueError("ndarray is not C-contiguous").
            written = self._pcm.write(np.ascontiguousarray(block, dtype=np.int16))
        except Exception as exc:  # noqa: BLE001 - alsaaudio.ALSAAudioError
            # Everything EXCEPT an underrun raises. This is where a dead
            # bluealsa transport surfaces, so it must reopen, not crash.
            raise SinkBroken(f"write to {self.device!r} failed: {exc}") from exc
        if written < 0:
            # An underrun is a RETURN VALUE, not an exception: on -EPIPE the
            # library has already called snd_pcm_prepare() for us and hands
            # back -32. The stream is recovered and the next write works. This
            # period's audio was dropped, hence the counter rather than a retry.
            self.xruns += 1
            return 0
        # write() returns FRAMES, not bytes. Comparing it against len(data) in
        # bytes is a bug that reads as a permanent short write.
        return int(written)

    def close(self) -> None:
        pcm, self._pcm = self._pcm, None
        if pcm is not None:
            with contextlib.suppress(Exception):
                pcm.close()

    def describe(self) -> dict[str, Any]:
        return {"sink": "alsa", **self.info, "xruns": self.xruns}


# ── sources ───────────────────────────────────────────────────────────────────


@dataclass
class SourceStats:
    periods: int = 0
    dry_periods: int = 0       # nothing at all in the FIFO for a whole period
    short_periods: int = 0     # some music, padded with zeros to fill the period
    dropped_periods: int = 0   # backlog too deep, one period of music discarded
    bytes_read: int = 0
    backlog_bytes: int = 0


class SilenceSource:
    """No music at all, at the right speed.

    Used for `--no-fifo` renders and as the honest answer to "what does the
    mixer do with no snapclient": effects still play. Silence is a valid music
    input, not an error, and that is not a special case anywhere in this file.
    """

    kind = "silence"

    def __init__(self, *, pace: bool = False):
        self.stats = SourceStats()
        self.pace = pace
        self._next = time.monotonic()

    def read(self, frames: int) -> np.ndarray:
        self.stats.periods += 1
        self.stats.dry_periods += 1
        if self.pace:
            self._next += frames / RATE
            delay = self._next - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return silence(frames)

    def close(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {"source": "silence"}


class PcmFileSource:
    """A raw or wav file standing in for the music. Offline listening only."""

    kind = "file"

    def __init__(self, path: str | os.PathLike[str], *, loop: bool = False, pace: bool = False):
        self.path = Path(path)
        self.loop = loop
        self.pace = pace
        self.stats = SourceStats()
        data = self.path.read_bytes()
        if data[:4] == b"RIFF":
            # Smallest correct thing that works: find the data chunk rather
            # than assuming the 44 byte canonical header, which plenty of
            # encoders do not write.
            offset = data.find(b"data")
            data = data[offset + 8:] if offset >= 0 else data[44:]
        self._pcm = frames_of(data)
        self._pos = 0
        self._next = time.monotonic()

    def read(self, frames: int) -> np.ndarray:
        self.stats.periods += 1
        out = silence(frames)
        take = min(frames, len(self._pcm) - self._pos)
        if take > 0:
            out[:take] = self._pcm[self._pos:self._pos + take]
            self._pos += take
        if take < frames:
            self.stats.short_periods += 1
            if self.loop:
                self._pos = 0
        self.stats.bytes_read += take * FRAME_BYTES
        if self.pace:
            self._next += frames / RATE
            delay = self._next - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return out

    def close(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {"source": "file", "path": str(self.path), "frames": len(self._pcm)}


class FifoSource:
    """The music, as snapclient's file player writes it.

    THE PRODUCER, from snapcast's client/player/file_player.cpp: every 50 ms,
    off a steady_clock timer, it writes exactly 2400 frames (9600 bytes) with
    fwrite + fflush and no error handling. `getPlayerChunkOrSilence` means that
    when there is no music it writes ZEROS rather than stalling, so this FIFO
    is a continuous real time source that never goes quiet by absence.

    Four things about this are traps, and all four are handled here:

    1. O_RDWR, NOT O_RDONLY. On an O_RDONLY fd, once a writer has attached and
       exited, poll() returns POLLHUP immediately and forever and read()
       returns b''. Measured: poll(500) came back in 0 ms with revents 16. A
       plain poll/read loop would therefore spin at 100% CPU, silently, from
       the moment snapclient restarts. Holding the write end ourselves means
       the pipe never drops to zero writers, so there is no HUP and no EOF, and
       a snapclient restart is a non-event: a gap of silence and then music
       again, with no reopen logic anywhere. The cost is that we must never
       write to this fd.

    2. BACKPRESSURE RUNS BACKWARDS. fwrite on a full pipe blocks, and
       FilePlayer::needsThread() is false, so it blocks snapclient's single
       io_context and snapclient stops reading the network entirely. This
       source is therefore drained on EVERY iteration of the engine loop, even
       when the output is broken.

    3. THE DEFAULT PIPE IS 262144 BYTES = 1365 ms of audio. If it ever fills,
       that is 1.4 seconds of invisible latency that never drains back down.
       F_SETPIPE_SZ caps it. Granularity is coarse (the Pi 5 kernel here uses
       16384 byte pages, so 4096 and 16384 both give 16384, and 38400 and
       65536 both give 65536); 16384 is 85.3 ms and is the default.

    4. TWO CLOCKS THAT DRIFT. snapclient paces off CLOCK_MONOTONIC, our ALSA
       write is paced by the bluealsa/A2DP clock, and over an evening they
       diverge. So the backlog is measured every period and corrected by
       DROPPING a period of music when it gets deep. Never by growing a buffer.

       THE FLOOR ON THAT THRESHOLD IS NOT DECORATION, and getting it wrong was
       the one bug in here that would have been audible from the first second.
       The producer is not continuous: it writes 9600 bytes at a time (2.5
       periods at the default size) and nothing in between, and the consumer
       needs about one period of cushion on top of that to not run dry in the
       gaps. So the NORMAL backlog of a perfectly healthy chain oscillates
       between roughly zero and 3.5 periods. Any threshold below that fires
       every single cycle, throwing away a period of music each time and then
       starving on the next read, which comes out as a permanent stutter that
       looks like a drift correction working hard. The threshold is therefore
       floored at one producer chunk plus one period, whatever the config says,
       and the default sits just above that. What is left is a correction that
       only fires when the mixer really did fall behind.
    """

    kind = "fifo"

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        pipe_bytes: int = 16384,
        poll_ms: int = 10,
        max_backlog_periods: float = 2.0,
    ):
        self.path = Path(path)
        self.poll_ms = poll_ms
        self.max_backlog_periods = max_backlog_periods
        self.stats = SourceStats()
        self.pipe_bytes = pipe_bytes
        # Consecutive fully dry periods, and whether we have already said so.
        # The music stopping arriving is the failure this box is most afraid of
        # (silence with every unit green), and a counter in a heartbeat 30
        # seconds away is not the same as a line at the moment it happens.
        self._dry_streak = 0
        self._dry_said = False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            os.mkfifo(self.path, 0o660)
        elif not self._is_fifo():
            raise RuntimeError(f"{self.path} exists and is not a fifo")
        # 0o660 explicitly after the fact too: os.mkfifo respects the umask, and
        # the unit runs with UMask=0077, which would leave snapclient (a
        # different user) unable to open it. That failure looks like snapclient
        # hanging on startup with no message at all.
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o660)

        self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        try:
            fcntl.fcntl(self._fd, F_SETPIPE_SZ, pipe_bytes)
            # Believe the read-back only if it is plausible. 1031 and 1032 are
            # Linux command numbers; on macOS they are not F_*PIPE_SZ at all and
            # the call comes back 0 without raising, which would then be logged
            # and reported as "the pipe holds 0 bytes".
            got = fcntl.fcntl(self._fd, F_GETPIPE_SZ)
            if got > 0:
                self.pipe_bytes = got
        except OSError as exc:
            # Not fatal. F_SETPIPE_SZ does not exist on macOS, where the suite
            # runs, and a kernel that refuses the size just means a deeper
            # backlog than we would like, which the drop-on-drift correction
            # below still bounds.
            logs.warn("mixer_pipe_size_failed", path=str(self.path), error=str(exc))
        self._poller = select.poll()
        self._poller.register(self._fd, select.POLLIN)
        logs.log(
            "mixer_fifo_open",
            path=str(self.path),
            pipe_bytes=self.pipe_bytes,
            pipe_ms=round(self.pipe_bytes / BYTES_PER_MS, 1),
        )

    def _is_fifo(self) -> bool:
        import stat  # noqa: PLC0415 - one call, not worth a module level import

        return stat.S_ISFIFO(os.stat(self.path).st_mode)

    def backlog(self) -> int:
        """Bytes sitting in the pipe right now, or 0 if the kernel will not say.

        Advisory, and 0 is a legal answer meaning "no idea". Measured on macOS:
        FIONREAD on an O_RDWR fifo fd returns 0 even with 5000 bytes queued, so
        on a dev laptop the drift correction simply never fires. On Linux, the
        only platform this ships to, it reports the real figure. The drop
        decision itself is tested separately by stubbing this method, so the
        logic is covered everywhere and only the syscall is not.
        """
        try:
            import termios  # noqa: PLC0415

            return struct.unpack("I", fcntl.ioctl(self._fd, termios.FIONREAD, b"\0\0\0\0"))[0]
        except Exception:  # noqa: BLE001 - purely advisory
            return 0

    def _read_exactly(self, want: int) -> bytearray:
        """Up to `want` bytes, waiting at most poll_ms for the rest of them.

        READ FIRST, POLL SECOND, and that order is a bug fix rather than a
        micro-optimisation. Polling first means computing a timeout, and
        `int((deadline - now) * 1000)` rounds down: with poll_ms small, or with
        the thread preempted anywhere between taking the deadline and using it,
        the budget comes out as 0 ms and the function returns EMPTY WITH DATA
        SITTING IN THE PIPE. Upstairs that is indistinguishable from snapclient
        having stopped: a period of silence spliced into the music, counted as
        dry, blamed on the wrong process. Trying the read first cannot get that
        wrong, and it also removes a syscall from the case that always happens.

        An O_RDWR fd never reports EOF (we are our own writer), so an empty
        result here means EAGAIN and nothing more.
        """
        buf = bytearray()
        deadline = time.monotonic() + self.poll_ms / 1000.0
        while len(buf) < want:
            try:
                chunk = os.read(self._fd, want - len(buf))
            except BlockingIOError:
                chunk = b""
            except OSError as exc:  # pragma: no cover - fd taken away under us
                logs.error("mixer_fifo_read_failed", path=str(self.path), error=str(exc))
                break
            if chunk:
                buf += chunk
                continue
            left_ms = int((deadline - time.monotonic()) * 1000)
            if left_ms <= 0 or not self._poller.poll(left_ms):
                break  # timeout. The caller pads with zeros; music is optional.
        return buf

    def limit_bytes(self, want: int) -> int:
        """How deep the backlog may get before a period of music is thrown away.

        Floored at one producer chunk plus one period. See trap 4 in the class
        docstring: below that floor the correction fires on a healthy chain and
        the cure is far worse than the disease.
        """
        return max(int(want * self.max_backlog_periods), PRODUCER_CHUNK_BYTES + want)

    def read(self, frames: int) -> np.ndarray:
        want = frames * FRAME_BYTES
        self.stats.periods += 1

        # Drift correction FIRST, so the period we are about to hand upstairs
        # is the freshest one available rather than the one at the back of a
        # queue we are about to throw away anyway.
        limit = self.limit_bytes(want)
        pending = self.backlog()
        self.stats.backlog_bytes = pending
        while pending > limit:
            dropped = self._read_exactly(want)
            if not dropped:
                break
            self.stats.dropped_periods += 1
            pending -= len(dropped)

        data = self._read_exactly(want)
        self.stats.bytes_read += len(data)
        if not data:
            self.stats.dry_periods += 1
            self._dry_streak += 1
            # One line when it starts, one when it ends, and nothing in
            # between: a warn per period would be 50 lines a second, and a
            # journal writing 50 lines a second from the audio thread is a
            # blocked pipe waiting to happen.
            if not self._dry_said and self._dry_streak * frames >= RATE:
                self._dry_said = True
                logs.warn(
                    "mixer_music_dry",
                    path=str(self.path),
                    seconds=round(self._dry_streak * frames / RATE, 1),
                    note="nothing in the fifo. snapclient is not writing; effects still play",
                )
            return silence(frames)
        if self._dry_said:
            logs.log(
                "mixer_music_back",
                path=str(self.path),
                silent_s=round(self._dry_streak * frames / RATE, 1),
            )
            self._dry_said = False
        self._dry_streak = 0
        out = silence(frames)
        got = frames_of(data)
        out[:len(got)] = got
        if len(got) < frames:
            self.stats.short_periods += 1
        return out

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            with contextlib.suppress(OSError):
                self._poller.unregister(fd)
            with contextlib.suppress(OSError):
                os.close(fd)

    def describe(self) -> dict[str, Any]:
        return {"source": "fifo", "path": str(self.path), "pipe_bytes": self.pipe_bytes}


# ── effects ───────────────────────────────────────────────────────────────────

# Everything decodes to headerless s16le 48000:2. Order matters and is measured
# against the real sfx directory plus a torture set, expecting 384000 bytes for
# a 2 second clip:
#
#     input            ffmpeg     mpg123     sox
#     mono 44.1k mp3   384000     384000     386196
#     mono 22k wav     384000          0     384000
#     stereo 48k ogg   384000          0     384000
#
# ffmpeg is exact on all of them, which is why it is first. mpg123 is exact on
# mp3 and USELESS on anything else, and it fails SILENTLY: with -q it printed
# nothing at all and emitted zero bytes. sox reads everything but does not trim
# the MP3 decoder delay, hence the 2196 extra bytes, which is harmless for a
# sound effect and wrong if anything ever needs sample accuracy. That silent
# zero-byte mode is exactly why _validate below is not optional: without it a
# .wav renamed to .mp3 becomes a silent pad with no line in any log.
#
# -nostdin is not decoration in a service: without it ffmpeg grabs stdin and
# can eat bytes meant for something else. -v error keeps stderr for real
# failures so a decode error is loud.
DECODERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ffmpeg", ("ffmpeg", "-v", "error", "-nostdin", "-i", "{input}",
                "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-")),
    ("mpg123", ("mpg123", "-q", "--rate", str(RATE), "--stereo", "--encoding", "s16", "-s", "{input}")),
    ("sox", ("sox", "{input}", "-t", "raw", "-r", str(RATE), "-c", str(CHANNELS),
             "-b", "16", "-e", "signed-integer", "-L", "-")),
)

# Same list app.py uses, plus .raw for anything pre-decoded by hand.
EFFECT_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".raw")


# eq=False, so an Effect is compared and hashed BY IDENTITY. The default
# dataclass __eq__ compares fields, and one of the fields is a numpy array,
# whose == is elementwise: `effect_a == effect_b` would return an array and
# `set(effects)` would raise "unhashable type". Identity is also the semantics
# that is actually wanted here, since two entries in the cache pointing at the
# same decoded audio ARE the same effect.
@dataclass(eq=False)
class Effect:
    name: str
    pcm: np.ndarray          # (frames, 2) int16, resident
    source: Path
    decoder: str

    @property
    def frames(self) -> int:
        return len(self.pcm)

    @property
    def seconds(self) -> float:
        return self.frames / RATE


class EffectCache:
    """Every effect decoded once, at startup, and kept in memory.

    Triggering has to cost a memory copy and not a decoder spawn. The numbers
    that make this obvious, measured on the Pi: the whole current sfx directory
    is 12 files, 862 KB of mp3, about 11 MB of PCM, and decoding all of it
    costs well under a second of CPU. Eleven megabytes resident is nothing, and
    a subprocess spawn on the trigger path would be both slower than the sound
    and a source of jitter on the audio thread.

    The disk cache on top of that is for RESTARTS, so a mixer that comes back
    mid-event is playing again in milliseconds instead of re-decoding.
    """

    def __init__(
        self,
        sfx_dir: str | os.PathLike[str],
        cache_dir: str | os.PathLike[str],
        *,
        decoders: Sequence[tuple[str, Sequence[str]]] = DECODERS,
        extensions: Sequence[str] = EFFECT_EXTENSIONS,
        timeout: float = 60.0,
    ):
        self.sfx_dir = Path(sfx_dir)
        self.cache_dir = Path(cache_dir)
        self.decoders = tuple((name, tuple(argv)) for name, argv in decoders)
        self.extensions = tuple(ext.lower() for ext in extensions)
        self.timeout = timeout
        self._effects: dict[str, Effect] = {}

    # The lookup is case insensitive and accepts the stem or the filename, the
    # same two spellings app.resolve_sfx accepts, so a name that works over
    # HTTP works here. Anything else and the two surfaces disagree about what
    # exists, which is a support call in the middle of an event.
    def get(self, name: str) -> Effect | None:
        return self._effects.get((name or "").strip().lower())

    def names(self) -> list[str]:
        # set() over the VALUES, because the dict deliberately holds two keys
        # per effect (the stem and the filename) so that both spellings
        # resolve. Listing the values directly reports every sound twice, and
        # that list is what an operator reads to find out what is loaded.
        return sorted(effect.name for effect in set(self._effects.values()))

    def __len__(self) -> int:
        # The dict holds two keys per effect (stem and filename), so counting
        # it directly would report twice as many sounds as exist.
        return len(set(self._effects.values()))

    def load(self) -> list[Effect]:
        """Decode everything in the sfx dir. Safe to call again; that is reload."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        loaded: dict[str, Effect] = {}
        listed = True
        try:
            entries = sorted(p for p in self.sfx_dir.iterdir() if p.is_file())
        except OSError as exc:
            logs.warn("mixer_sfx_dir_unreadable", dir=str(self.sfx_dir), error=str(exc))
            entries = []
            listed = False
        keep: set[str] = set()
        for path in entries:
            if path.suffix.lower() not in self.extensions:
                continue
            with contextlib.suppress(OSError):
                keep.add(self._cache_path(path).name)
            effect = self._load_one(path)
            if effect is None:
                continue
            loaded[effect.name.lower()] = effect
            loaded.setdefault(path.name.lower(), effect)
        self._effects = loaded
        if listed:
            self._prune(keep)
        logs.log(
            "mixer_effects_loaded",
            dir=str(self.sfx_dir),
            count=len(self),
            seconds=round(sum(e.seconds for e in set(self._effects.values())), 1),
        )
        return sorted(set(self._effects.values()), key=lambda e: e.name)

    def _cache_path(self, path: Path) -> Path:
        stat = path.stat()
        # (size, mtime_ns) and the format string, not a content hash: a content
        # hash means reading every file on every start for no benefit, and this
        # key changes for every edit a human can make with cp or Finder.
        key = f"{path.name}|{stat.st_size}|{stat.st_mtime_ns}|{STREAM_FORMAT}"
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.raw"

    @staticmethod
    def _validate(raw: bytes) -> bool:
        # Zero bytes is mpg123 refusing a non-mp3 without saying so. A length
        # that is not a whole number of frames is a truncated or misdecoded
        # file. Either one becomes a burst of garbage or a silent pad, so both
        # are rejected here and fall through to the next decoder.
        return len(raw) > 0 and len(raw) % FRAME_BYTES == 0

    def _load_one(self, path: Path) -> Effect | None:
        cached = self._cache_path(path)
        try:
            if cached.is_file():
                raw = cached.read_bytes()
                if self._validate(raw):
                    return Effect(path.stem, frames_of(raw), path, "cache")
                logs.warn("mixer_cache_invalid", file=str(cached), bytes=len(raw))
                cached.unlink(missing_ok=True)
        except OSError as exc:
            logs.warn("mixer_cache_unreadable", file=str(cached), error=str(exc))

        if path.suffix.lower() == ".raw":
            raw = path.read_bytes()
            if not self._validate(raw):
                logs.error("mixer_decode_rejected", file=str(path), decoder="raw", bytes=len(raw))
                return None
            return Effect(path.stem, frames_of(raw), path, "raw")

        for name, argv in self.decoders:
            if shutil.which(argv[0]) is None:
                continue
            started = time.monotonic()
            command = [arg.format(input=str(path)) for arg in argv]
            try:
                done = subprocess.run(
                    command, capture_output=True, timeout=self.timeout, check=False
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logs.warn("mixer_decode_failed", file=str(path), decoder=name, error=str(exc))
                continue
            if done.returncode != 0 or not self._validate(done.stdout):
                logs.warn(
                    "mixer_decode_rejected",
                    file=str(path),
                    decoder=name,
                    rc=done.returncode,
                    bytes=len(done.stdout),
                    stderr=(done.stderr or b"").decode("utf-8", "replace").strip()[:200] or None,
                )
                continue
            self._write_cache(cached, done.stdout)
            logs.log(
                "mixer_decoded",
                file=path.name,
                decoder=name,
                seconds=round(len(done.stdout) / FRAME_BYTES / RATE, 2),
                ms=round((time.monotonic() - started) * 1000, 1),
            )
            return Effect(path.stem, frames_of(done.stdout), path, name)

        logs.error("mixer_no_decoder", file=str(path), tried=",".join(n for n, _ in self.decoders))
        return None

    def _prune(self, keep: set[str]) -> None:
        """Delete decoded PCM that no source file claims any more.

        The cache key is (name, size, mtime), so every time somebody drops a
        corrected version of a sound in over the old one, the previous decode
        is orphaned and stays. That is roughly a megabyte per edit of an mp3,
        forever, in a CacheDirectory nothing else prunes. It is not going to
        fill an SD card in one evening, but "grows without bound and nobody
        looks" is how an SD card dies, and this box boots from one.

        Only ever called when the sfx directory really was readable: an
        iterdir() that failed would produce an empty keep set, and deleting the
        entire decode cache because of a transient permission error is a
        slower start for no reason. Deleting is always safe otherwise, since
        the worst case is one re-decode of well under a second.
        """
        removed = 0
        with contextlib.suppress(OSError):
            for entry in self.cache_dir.iterdir():
                if entry.name in keep or not entry.is_file():
                    continue
                if entry.suffix not in (".raw", ".tmp"):
                    continue
                with contextlib.suppress(OSError):
                    entry.unlink()
                    removed += 1
        if removed:
            logs.log("mixer_cache_pruned", dir=str(self.cache_dir), removed=removed)

    def _write_cache(self, target: Path, raw: bytes) -> None:
        # Temp file plus os.replace, always. A half written .raw that survives
        # a crash and then gets loaded as a voice is full scale garbage into a
        # room full of people, and it would happen exactly once, at the worst
        # possible moment, and look like a hardware fault.
        tmp = target.with_suffix(".raw.tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            logs.warn("mixer_cache_write_failed", file=str(target), error=str(exc))
            with contextlib.suppress(OSError):
                tmp.unlink()


# ── the mix itself ────────────────────────────────────────────────────────────


class Voice:
    """One playing copy of one effect."""

    __slots__ = ("effect", "pos", "gain", "seq")

    def __init__(self, effect: Effect, gain: float, seq: int):
        self.effect = effect
        self.pos = 0
        self.gain = gain
        self.seq = seq

    @property
    def done(self) -> bool:
        return self.pos >= self.effect.frames


class Mixer:
    """Sums the music and zero or more effect voices into one period.

    RETRIGGER POLICY: ADD ANOTHER VOICE. Pressing the same key again starts a
    second copy alongside the first; it does not restart the one that is
    playing. That is a decision, and this is why:

      - It is the behaviour that was actually asked for. The question was
        literally "does pressing repeatedly give a stutter effect", and adding
        voices is what produces that machine-gun texture. Restarting one voice
        would give a single sound that keeps jumping back to its own start,
        which sounds like a skipping CD, not like a drum roll.
      - Restart-in-place cannot express polyphony at all. Two different effects
        at once would still need voice stacking, so the restart policy would be
        a special case that only exists for same-name presses.
      - It costs nothing. Four voices at a 20 ms period measured 51 us on the
        Pi 5, which is 0.26% of one core.

    At the polyphony limit the OLDEST voice is stolen rather than the newest
    press being refused. At an event the press that matters is the one that
    just happened: a button that visibly does nothing reads as broken, while
    one more layer replacing the layer that is already fading out is inaudible.

    CLIPPING: the accumulator is float32 and the clip happens exactly once, at
    the end. Summing int16 into int16 wraps around at full scale and sounds
    like tearing, which is a very loud way to fail. float32 is also FASTER than
    an int32 accumulator on this chip (27.5 us vs 51.3 us for 4 voices at 960
    frames), so there is no reason to do it the dangerous way.
    """

    def __init__(
        self,
        *,
        polyphony: int = 8,
        duck_db: float = -12.0,
        fade_in_ms: float = 60.0,
        fade_out_ms: float = 250.0,
        effect_db: float = 0.0,
    ):
        self.polyphony = max(1, int(polyphony))
        self.duck_db = float(duck_db)
        self.duck_floor = db_to_gain(duck_db)
        self.effect_gain = db_to_gain(effect_db)
        self.fade_in_ms = float(fade_in_ms)
        self.fade_out_ms = float(fade_out_ms)
        # Per-frame steps for the duck envelope. The full travel is
        # 1.0 -> duck_floor, so the step is that distance over the fade length,
        # which keeps the fade time honest whatever the duck depth is.
        travel = max(1e-6, 1.0 - self.duck_floor)
        self._step_down = travel / max(1.0, self.fade_in_ms * RATE / 1000.0)
        self._step_up = travel / max(1.0, self.fade_out_ms * RATE / 1000.0)
        self._duck = 1.0
        self._voices: list[Voice] = []
        self._seq = 0
        self.stolen = 0
        self.triggered = 0
        self._last_steal_log = 0.0

    @property
    def voices(self) -> int:
        return len(self._voices)

    @property
    def duck(self) -> float:
        return self._duck

    def trigger(self, effect: Effect, *, gain_db: float = 0.0) -> Voice:
        self._seq += 1
        voice = Voice(effect, self.effect_gain * db_to_gain(gain_db), self._seq)
        if len(self._voices) >= self.polyphony:
            oldest = min(self._voices, key=lambda v: v.seq)
            self._voices.remove(oldest)
            self.stolen += 1
            # Rate limited, because this runs on the AUDIO thread and the
            # condition that produces it is somebody holding a key down. Every
            # log line is a write to a pipe journald owns; fill that pipe and
            # the write blocks, and a blocked write between two ALSA writes is
            # an underrun. Once a second is enough to see it happening, and
            # `stolen` in the heartbeat carries the real count.
            now = time.monotonic()
            if now - self._last_steal_log >= 1.0:
                self._last_steal_log = now
                logs.warn(
                    "mixer_voice_stolen",
                    stole=oldest.effect.name,
                    replacing_with=effect.name,
                    polyphony=self.polyphony,
                    stolen_total=self.stolen,
                )
        self._voices.append(voice)
        self.triggered += 1
        return voice

    def stop_all(self) -> int:
        # The voices go, the duck does NOT snap back: leaving _duck alone means
        # the envelope releases over fade_out_ms like it always does. Snapping
        # the music back to full volume in one sample is a click, and a click
        # through a PA is louder than the effect was.
        count = len(self._voices)
        self._voices.clear()
        return count

    def _duck_ramp(self, frames: int, target: float) -> np.ndarray:
        current = self._duck
        if current == target:
            return np.full(frames, target, dtype=np.float32)
        going_down = target < current
        step = self._step_down if going_down else self._step_up
        delta = -step if going_down else step
        ramp = current + np.arange(1, frames + 1, dtype=np.float32) * delta
        low, high = (target, current) if going_down else (current, target)
        np.clip(ramp, low, high, out=ramp)
        self._duck = float(ramp[-1])
        return ramp

    def mix(self, music: np.ndarray) -> np.ndarray:
        """One period in, one period out. The only hot path in this file."""
        frames = len(music)
        if frames == 0:
            return silence(0)

        # The duck target is decided by what is playing at the START of the
        # period, before any voice is advanced or retired, so a voice that ends
        # inside this period still holds the music down for the whole of it and
        # the release starts cleanly on the next one.
        target = self.duck_floor if self._voices else 1.0
        acc = music.astype(np.float32)
        ramp = self._duck_ramp(frames, target)
        acc *= ramp[:, None]

        finished: list[Voice] = []
        for voice in self._voices:
            take = min(frames, voice.effect.frames - voice.pos)
            if take > 0:
                segment = voice.effect.pcm[voice.pos:voice.pos + take]
                acc[:take] += segment.astype(np.float32) * voice.gain
                voice.pos += take
            if voice.done:
                finished.append(voice)
        for voice in finished:
            self._voices.remove(voice)

        # int16 is -32768..32767 and np.clip's upper bound is inclusive, so
        # 32767 is the ceiling. Clipping to 32768 would wrap that one sample to
        # -32768, which is the single worst sample you can emit.
        np.clip(acc, -32768.0, 32767.0, out=acc)
        return acc.astype(np.int16)


# ── the engine ────────────────────────────────────────────────────────────────


@dataclass
class EngineStats:
    periods: int = 0
    frames: int = 0
    xruns: int = 0
    sink_failures: int = 0
    sink_reopens: int = 0
    periods_without_sink: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started


class MixerEngine:
    """The audio thread: read a period, mix it, write it. Nothing else.

    It is a plain thread and not asyncio, and that is not a preference. The
    blocking ALSA write IS the clock and there is no awaitable form of it, so
    running this on the event loop would stall every HTTP request for 20 ms at
    a time and put the HTTP server's jitter into the audio. Triggers arrive
    through a queue that this thread drains at the top of each period; no file
    IO, no decoding and no subprocess ever happens here.

    The sink is created through a FACTORY rather than passed in, because the
    one thing this loop must survive is the output going away: bluealsa
    restarting takes the PCM with it. On SinkBroken it logs loudly, keeps
    draining the source (see FifoSource for why that is not optional), and
    retries with bounded backoff. It never goes quiet without saying so.
    """

    def __init__(
        self,
        source: Any,
        sink_factory: Callable[[], Any],
        mixer: Mixer,
        *,
        period_frames: int = 960,
        reopen_backoff: float = 1.0,
        reopen_backoff_max: float = 10.0,
        log_every: float = 30.0,
    ):
        self.source = source
        self.sink_factory = sink_factory
        self.mixer = mixer
        self.period_frames = period_frames
        self.reopen_backoff = reopen_backoff
        self.reopen_backoff_max = reopen_backoff_max
        self.log_every = log_every
        self.stats = EngineStats()
        self.triggers: queue.SimpleQueue = queue.SimpleQueue()
        self.sink: Any = None
        self._backoff = reopen_backoff
        self._retry_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_log = time.monotonic()
        self.last_error: str | None = None
        # Incremented by the CONTROL thread on submit, while `triggered` on the
        # Mixer is incremented by the audio thread when the voice actually
        # starts. Both are reported, because a stats line read immediately
        # after a play shows queued=1 triggered=0 for up to one period, and an
        # operator seeing triggered=0 alone would reasonably conclude nothing
        # happened and press again.
        self.queued = 0

    # ── trigger intake ────────────────────────────────────────────────────────

    def submit(self, effect: Effect, gain_db: float = 0.0) -> None:
        """Called from the control thread. Never from the audio thread."""
        self.queued += 1
        self.triggers.put((effect, gain_db))

    def submit_stop(self) -> None:
        self.triggers.put(("stop", 0.0))

    def _drain_triggers(self) -> None:
        while True:
            try:
                effect, gain_db = self.triggers.get_nowait()
            except queue.Empty:
                return
            if effect == "stop":
                self.mixer.stop_all()
                continue
            self.mixer.trigger(effect, gain_db=gain_db)

    # ── the loop ──────────────────────────────────────────────────────────────

    def _ensure_sink(self) -> bool:
        if self.sink is not None:
            return True
        now = time.monotonic()
        if now < self._retry_at:
            return False
        try:
            self.sink = self.sink_factory()
        except SinkBroken as exc:
            self.last_error = str(exc)
            self._retry_at = now + self._backoff
            logs.error(
                "mixer_sink_unavailable",
                error=str(exc),
                retry_in_s=round(self._backoff, 1),
                attempt=self.stats.sink_reopens + 1,
            )
            self._backoff = min(self._backoff * 2, self.reopen_backoff_max)
            return False
        self.stats.sink_reopens += 1
        self._backoff = self.reopen_backoff
        self.last_error = None
        logs.log("mixer_sink_open", **self.sink.describe())
        return True

    def run_once(self) -> np.ndarray:
        """One period. Public so tests can step the loop without a thread."""
        self._drain_triggers()
        # The source is read FIRST and unconditionally. If the mixer stops
        # draining, snapclient blocks in fwrite, its io_context stalls, and it
        # stops reading the network: a broken speaker would take the whole
        # stream down with it.
        music = self.source.read(self.period_frames)
        block = self.mixer.mix(music)
        self.stats.periods += 1
        self.stats.frames += len(block)

        if self._ensure_sink():
            try:
                self.sink.write(block)
            except SinkBroken as exc:
                self.stats.sink_failures += 1
                self.last_error = str(exc)
                logs.error("mixer_sink_lost", error=str(exc), periods=self.stats.periods)
                with contextlib.suppress(Exception):
                    self.sink.close()
                self.sink = None
                self._retry_at = time.monotonic() + self._backoff
        else:
            self.stats.periods_without_sink += 1
        self.stats.xruns = getattr(self.sink, "xruns", self.stats.xruns)
        self._maybe_log()
        return block

    def _maybe_log(self) -> None:
        now = time.monotonic()
        if self.log_every <= 0 or now - self._last_log < self.log_every:
            return
        self._last_log = now
        source = getattr(self.source, "stats", SourceStats())
        # A heartbeat, every 30 s by default, with the counters that
        # distinguish the failure modes from each other: xruns mean the output
        # is starving, dry means the music stopped arriving, dropped means the
        # two clocks are drifting apart. Silence with everything green is the
        # thing this line exists to make impossible.
        logs.log(
            "mixer_stats",
            periods=self.stats.periods,
            audio_s=round(self.stats.frames / RATE, 1),
            voices=self.mixer.voices,
            queued=self.queued,
            triggered=self.mixer.triggered,
            xruns=self.stats.xruns,
            dry=source.dry_periods,
            short=source.short_periods,
            dropped=source.dropped_periods,
            backlog_ms=round(source.backlog_bytes / BYTES_PER_MS, 1),
            sink_failures=self.stats.sink_failures,
            no_sink_periods=self.stats.periods_without_sink,
        )

    def run(self) -> None:
        logs.log(
            "mixer_loop_start",
            period_frames=self.period_frames,
            period_ms=round(self.period_frames / RATE * 1000, 1),
            polyphony=self.mixer.polyphony,
            duck_db=self.mixer.duck_db,
            **self.source.describe(),
        )
        try:
            while not self._stop.is_set():
                self.run_once()
        except Exception as exc:  # noqa: BLE001 - the loop dying quietly is the nightmare
            logs.error("mixer_loop_crashed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            logs.log("mixer_loop_stop", periods=self.stats.periods)
            if self.sink is not None:
                with contextlib.suppress(Exception):
                    self.sink.close()
            with contextlib.suppress(Exception):
                self.source.close()

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="musicbox-mixer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


class LoopWatchdog:
    """Notices when the audio loop stops advancing, and says so.

    Every fault the loop KNOWS about is already loud. This is for the one it
    cannot report, because reporting happens on the same thread that is stuck:
    a write that blocks forever, a source that never returns, a deadlock. The
    room goes quiet, the unit stays active, the journal simply stops, and
    "the journal stopped" is not something anybody notices during an event.

    It is a separate thread that reads one integer and logs. It never touches
    the audio path, never holds a lock the loop wants, and cannot fail in a way
    that stops sound coming out, which is the whole reason it is a log and not
    a restart. pyalsaaudio releases the GIL around snd_pcm_writei
    (Py_BEGIN_ALLOW_THREADS in alsaaudio.c), so this thread really does get to
    run while the audio thread is blocked in the kernel; that is the property
    the entire idea depends on.

    Escalating to systemd's WatchdogSec (sd_notify WATCHDOG=1 from the loop, so
    a wedged mixer is killed and restarted rather than merely reported) is the
    obvious next step and is deliberately NOT taken here: it puts a process
    that is playing music to a room one bug away from being killed every
    WatchdogSec, and this box's rule is that the safe direction is loud, not
    clever.
    """

    def __init__(self, engine: MixerEngine, *, timeout: float = 5.0, interval: float = 1.0):
        self.engine = engine
        self.timeout = timeout
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stalls = 0

    def _watch(self) -> None:
        last_periods = -1
        moved_at = time.monotonic()
        complained_at = 0.0
        while not self._stop.wait(self.interval):
            periods = self.engine.stats.periods
            now = time.monotonic()
            if periods != last_periods:
                if complained_at:
                    logs.log(
                        "mixer_loop_moving_again",
                        periods=periods,
                        stalled_s=round(now - moved_at, 1),
                    )
                    complained_at = 0.0
                last_periods = periods
                moved_at = now
                continue
            stuck = now - moved_at
            # Repeat while it lasts, not once: a single line an hour ago is
            # not what somebody staring at a silent speaker needs to see.
            if stuck >= self.timeout and now - complained_at >= self.timeout:
                self.stalls += 1
                complained_at = now
                logs.error(
                    "mixer_loop_stalled",
                    seconds=round(stuck, 1),
                    periods=periods,
                    sink=(self.engine.sink.kind if self.engine.sink is not None else "none"),
                    note="the audio loop has not advanced. The speaker is silent right now.",
                )

    def start(self) -> None:
        if self.timeout <= 0:
            return
        self._thread = threading.Thread(target=self._watch, name="musicbox-mixer-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)


# ── control surface ───────────────────────────────────────────────────────────
#
# A unix socket with one line in and one line out. Not a second HTTP port: the
# box already has one control surface on the network and a second one would be
# a second thing to authenticate, firewall and explain. Filesystem permissions
# are the whole access control story here, which is the right size for a
# process whose only client is musicbox on the same machine.
#
# Request:   play "big airhorn" gain=-6
# Response:  ok play name="big airhorn" voices=2
#            err not_found "there is no effect called ..."
#
# Both sides use shlex, so a name with spaces in it survives. That trap is not
# hypothetical: `big airhorn #2.mp3` is an ordinary thing for someone to drop
# in with Finder, and the same class of bug already bit the MCP proxy once.

OK = "ok"
ERR = "err"


def encode(*parts: Any, **fields: Any) -> str:
    """One response line. Values are quoted when they need to be."""
    out = [str(p) for p in parts]
    for key, value in fields.items():
        if value is None:
            text = "-"
        elif isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, float):
            text = f"{value:.3f}"
        else:
            text = str(value)
        # shlex.quote only when it is needed. Quoting everything would make the
        # common line (ok stats periods=1500 xruns=0) noisier to read at a
        # glance, and this is a surface people read with their eyes over ssh.
        needs_quote = text == "" or any(ch in text for ch in ' "\'\\')
        out.append(f"{key}={shlex.quote(text) if needs_quote else text}")
    return " ".join(out)


class MixerService:
    """Everything wired together: cache, mixer, engine, control socket.

    Also the thing the standalone entry point and the systemd unit both build,
    so there is exactly one description of how the pieces fit.
    """

    def __init__(
        self,
        cache: EffectCache,
        engine: MixerEngine,
        *,
        socket_path: str | os.PathLike[str] | None = None,
        socket_mode: int = 0o660,
    ):
        self.cache = cache
        self.engine = engine
        self.socket_path = Path(socket_path) if socket_path else None
        self.socket_mode = socket_mode
        self._server: socketserver.UnixStreamServer | None = None
        self._server_thread: threading.Thread | None = None

    # ── commands ──────────────────────────────────────────────────────────────

    def handle(self, line: str) -> str:
        try:
            parts = shlex.split(line.strip())
        except ValueError as exc:
            return encode(ERR, "bad_syntax", message=str(exc))
        if not parts:
            return encode(ERR, "empty", message="say one of: ping play stop list stats reload")
        command, args = parts[0].lower(), parts[1:]
        handler = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            return encode(ERR, "unknown_command", command=command)
        try:
            return handler(args)
        except Exception as exc:  # noqa: BLE001 - a control error must not kill the audio
            logs.error("mixer_control_failed", command=command, error=f"{type(exc).__name__}: {exc}")
            return encode(ERR, "internal", message=str(exc))

    def _cmd_ping(self, args: list[str]) -> str:
        return encode(OK, "pong", uptime_s=round(self.engine.stats.uptime, 1))

    def _cmd_play(self, args: list[str]) -> str:
        gain_db = 0.0
        names = []
        for arg in args:
            if arg.startswith("gain="):
                try:
                    gain_db = float(arg.split("=", 1)[1])
                except ValueError:
                    return encode(ERR, "bad_gain", value=arg)
                # float() takes "nan" and "inf" without complaint, and a NaN
                # gain does not fail anywhere downstream: it turns the whole
                # accumulator into NaN, survives np.clip, and comes out of
                # .astype(int16) as full scale noise. db_to_gain refuses it too,
                # but the caller deserves an error rather than a silent 0 dB.
                if not math.isfinite(gain_db) or not -60.0 <= gain_db <= 20.0:
                    return encode(
                        ERR, "bad_gain", value=arg,
                        message="gain must be a finite number of dB between -60 and 20",
                    )
            else:
                names.append(arg)
        if not names:
            return encode(ERR, "no_name", message="play <name> [gain=<db>]")
        name = " ".join(names)
        effect = self.cache.get(name)
        if effect is None:
            return encode(ERR, "not_found", name=name, known=",".join(self.cache.names()) or "-")
        # Handed to the audio thread through a queue and answered immediately.
        # The alternative, waiting for the audio thread to confirm, would make
        # a trigger cost a period of latency in the CALLER, and the caller is
        # an HTTP handler with a person's finger on the other end of it.
        self.engine.submit(effect, gain_db)
        return encode(
            OK, "play",
            name=effect.name,
            seconds=round(effect.seconds, 2),
            gain_db=gain_db,
            voices=self.engine.mixer.voices,
        )

    def _cmd_stop(self, args: list[str]) -> str:
        self.engine.submit_stop()
        return encode(OK, "stop")

    def _cmd_list(self, args: list[str]) -> str:
        names = self.cache.names()
        return encode(OK, "list", count=len(names), names=",".join(names) or "-")

    def _cmd_reload(self, args: list[str]) -> str:
        # Decoding happens on the CONTROL thread, never on the audio thread. It
        # spawns ffmpeg and reads files, and either one on the audio thread is
        # a dropout. The audio thread only ever sees the finished arrays, and
        # a voice already playing keeps its own reference to the old array, so
        # a reload mid-effect cannot pull the memory out from under it.
        self.cache.load()
        return encode(OK, "reload", count=len(self.cache))

    def _cmd_stats(self, args: list[str]) -> str:
        source = getattr(self.engine.source, "stats", SourceStats())
        sink = self.engine.sink
        return encode(
            OK, "stats",
            uptime_s=round(self.engine.stats.uptime, 1),
            periods=self.engine.stats.periods,
            audio_s=round(self.engine.stats.frames / RATE, 1),
            voices=self.engine.mixer.voices,
            polyphony=self.engine.mixer.polyphony,
            queued=self.engine.queued,
            triggered=self.engine.mixer.triggered,
            stolen=self.engine.mixer.stolen,
            duck=round(self.engine.mixer.duck, 3),
            effects=len(self.cache),
            xruns=self.engine.stats.xruns,
            dry=source.dry_periods,
            short=source.short_periods,
            dropped=source.dropped_periods,
            backlog_ms=round(source.backlog_bytes / BYTES_PER_MS, 1),
            sink=(sink.kind if sink is not None else "none"),
            sink_failures=self.engine.stats.sink_failures,
            error=self.engine.last_error,
        )

    # ── socket plumbing ───────────────────────────────────────────────────────

    def serve_forever(self) -> None:
        if self.socket_path is None:
            return
        service = self

        class Handler(socketserver.StreamRequestHandler):
            # A person with `nc -U` leaves the connection open, so one request
            # per connection would be irritating to use by hand. Reading lines
            # until EOF also means musicbox can hold one connection open across
            # many triggers if it ever wants to.
            def handle(self) -> None:
                for raw in self.rfile:
                    try:
                        line = raw.decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001 # pragma: no cover
                        continue
                    if not line.strip():
                        continue
                    self.wfile.write((service.handle(line) + "\n").encode("utf-8"))
                    self.wfile.flush()

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            # Otherwise a client that hangs up mid-write kills the handler with
            # an unhandled BrokenPipeError traceback in the journal, which is
            # noise that looks like a fault.
            allow_reuse_address = True

            def handle_error(self, request, client_address):  # noqa: D102
                logs.warn("mixer_control_client_error", error=str(sys.exc_info()[1]))

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # A stale socket file from an unclean stop makes bind fail with
        # EADDRINUSE and the unit restart-loops on something that is not there.
        with contextlib.suppress(FileNotFoundError):
            if self.socket_path.is_socket():
                self.socket_path.unlink()
        self._server = Server(str(self.socket_path), Handler)
        with contextlib.suppress(OSError):
            # The client is musicbox, which runs as a different user. This mode
            # plus a shared group is the access control; 0600 would mean the
            # HTTP surface silently falls back to the announcement path with a
            # permission error nobody reads.
            os.chmod(self.socket_path, self.socket_mode)
        logs.log("mixer_control_listening", socket=str(self.socket_path), mode=oct(self.socket_mode))
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, name="musicbox-mixer-control", daemon=True
        )
        self._server_thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self.socket_path is not None:
            with contextlib.suppress(OSError):
                self.socket_path.unlink()


# ── construction and the standalone entry point ───────────────────────────────


def build_service(
    settings: MixerConfig,
    *,
    source: Any | None = None,
    sink_factory: Callable[[], Any] | None = None,
) -> MixerService:
    """One place where a MixerConfig becomes a running set of objects.

    source and sink_factory are injectable so the suite and `--out` can supply
    a buffer or a file instead of a sound card, and so a human can hear the
    result without going anywhere near the speaker.
    """
    cache = EffectCache(settings.sfx_dir, settings.cache_dir)
    cache.load()

    if source is None and settings.fifo:
        try:
            source = FifoSource(
                settings.fifo,
                pipe_bytes=settings.pipe_bytes,
                poll_ms=settings.poll_ms,
                max_backlog_periods=settings.max_backlog_periods,
            )
        except OSError as exc:
            # Deliberately fatal, and deliberately not degraded to silence.
            # Without a reader on that fifo snapclient blocks forever inside
            # fopen() with its unit still reporting active, so there is no
            # music either way; the difference is whether systemd shows a
            # failed unit or a green one in a silent room. Exit, loudly, and
            # let the restart loop keep trying.
            logs.error(
                "mixer_fifo_open_failed",
                path=str(settings.fifo),
                error=f"{type(exc).__name__}: {exc}",
                note="snapclient will block in fopen until this fifo has a reader",
            )
            raise
    if source is None:
        source = SilenceSource(pace=True)

    if sink_factory is None:
        if not settings.device:
            raise SystemExit(
                "no output configured: set MUSICBOX_MIXER_DEVICE (or --device) for ALSA, "
                "or use --out FILE to render to disk."
            )
        def sink_factory() -> Any:  # noqa: E306
            return AlsaSink(
                settings.device,
                periodsize=settings.periodsize,
                periods=settings.periods,
            )

    mixer = Mixer(
        polyphony=settings.polyphony,
        duck_db=settings.duck_db,
        fade_in_ms=settings.fade_in_ms,
        fade_out_ms=settings.fade_out_ms,
        effect_db=settings.gain_db,
    )
    engine = MixerEngine(
        source,
        sink_factory,
        mixer,
        period_frames=settings.periodsize,
        log_every=settings.log_every,
    )
    return MixerService(
        cache, engine, socket_path=settings.socket, socket_mode=settings.socket_mode
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="musicbox-mixer",
        description="Mix musicbox sound effects over the snapclient music stream.",
        epilog=(
            "Render to a file instead of the speaker: "
            "musicbox-mixer --no-fifo --out /tmp/mix.wav --music song.wav "
            "--fire airhorn@1 --fire airhorn@1.2 --duration 6"
        ),
    )
    settings = MixerConfig.from_env()
    parser.add_argument("--fifo", default=str(settings.fifo), help="fifo snapclient writes into")
    parser.add_argument("--no-fifo", action="store_true", help="no music source at all, effects only")
    parser.add_argument("--device", default=settings.device, help="ALSA device, for example bluealsa:DEV=..,PROFILE=a2dp")
    parser.add_argument("--out", default=None, help="write the mix to this file instead of ALSA (.wav gets a header)")
    parser.add_argument("--music", default=None, help="use this audio file as the music instead of the fifo")
    parser.add_argument("--sfx-dir", default=str(settings.sfx_dir))
    parser.add_argument("--cache-dir", default=str(settings.cache_dir))
    parser.add_argument("--socket", default=str(settings.socket), help="control socket path ('' to disable)")
    parser.add_argument("--periodsize", type=int, default=settings.periodsize)
    parser.add_argument("--periods", type=int, default=settings.periods)
    parser.add_argument("--pipe-bytes", type=int, default=settings.pipe_bytes)
    parser.add_argument("--duck-db", type=float, default=settings.duck_db)
    parser.add_argument("--fade-in-ms", type=float, default=settings.fade_in_ms)
    parser.add_argument("--fade-out-ms", type=float, default=settings.fade_out_ms)
    parser.add_argument("--gain-db", type=float, default=settings.gain_db)
    parser.add_argument("--voices", type=int, default=settings.polyphony)
    parser.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds")
    parser.add_argument(
        "--fire",
        action="append",
        default=[],
        metavar="NAME[@SECONDS]",
        help="trigger an effect at a time offset. Repeatable, and the way to render "
             "a deterministic file to listen to.",
    )
    parser.add_argument("--pace", action="store_true", help="run a file render at real time")
    parser.add_argument("--check", action="store_true", help="print the resolved settings and exit")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    settings.fifo = Path(args.fifo) if args.fifo else None
    settings.device = args.device
    settings.sfx_dir = Path(args.sfx_dir)
    settings.cache_dir = Path(args.cache_dir)
    settings.socket = Path(args.socket) if args.socket else None
    settings.periodsize = args.periodsize
    settings.periods = args.periods
    settings.pipe_bytes = args.pipe_bytes
    settings.duck_db = args.duck_db
    settings.fade_in_ms = args.fade_in_ms
    settings.fade_out_ms = args.fade_out_ms
    settings.gain_db = args.gain_db
    settings.polyphony = args.voices

    if args.check:
        logs.log("mixer_config", **settings.describe())
        return 0

    source = None
    if args.music:
        source = PcmFileSource(args.music, pace=args.pace)
    elif args.no_fifo:
        source = SilenceSource(pace=args.pace or not args.out)

    sink_factory = None
    if args.out:
        # pace only when a human is listening live over the socket. A plain
        # render should finish as fast as the CPU allows.
        pace = args.pace
        sink_factory = lambda: FileSink(args.out, pace=pace)  # noqa: E731

    service = build_service(settings, source=source, sink_factory=sink_factory)
    # The REAL source, not settings.fifo. --music and --no-fifo override it, and
    # a start line claiming a fifo that nothing is reading is exactly the kind
    # of small lie that costs an hour later.
    logs.log(
        "mixer_start",
        **{**settings.describe(), **service.engine.source.describe()},
        out=args.out or None,
        effects=len(service.cache),
    )

    fires: list[tuple[float, str]] = []
    for spec in args.fire:
        name, _, when = spec.partition("@")
        fires.append((float(when or 0.0), name))
    fires.sort()

    service.serve_forever()
    engine = service.engine
    # Only for the real, open-ended run. A render with --duration is driven by
    # the loop below at whatever speed the CPU manages and has no speaker to go
    # quiet on, so a stall watchdog there would only ever be noise.
    watchdog = LoopWatchdog(engine) if not (fires or args.duration > 0) else None
    deadline = args.duration if args.duration > 0 else None
    if watchdog is not None:
        watchdog.start()
    try:
        if fires or deadline is not None:
            # Driven period by period here rather than on the engine thread, so
            # scheduled triggers land at an exact audio offset in a render and
            # do not depend on wall clock scheduling.
            elapsed = 0.0
            period = settings.periodsize / RATE
            while deadline is None or elapsed < deadline:
                while fires and fires[0][0] <= elapsed:
                    _, name = fires.pop(0)
                    effect = service.cache.get(name)
                    if effect is None:
                        logs.error("mixer_fire_unknown", name=name, known=",".join(service.cache.names()))
                    else:
                        engine.mixer.trigger(effect)
                        logs.log("mixer_fire", name=effect.name, at_s=round(elapsed, 2))
                engine.run_once()
                elapsed += period
        else:
            engine.run()
    except KeyboardInterrupt:
        logs.log("mixer_interrupted")
    finally:
        if watchdog is not None:
            watchdog.stop()
        engine.stop()
        if engine.sink is not None:
            with contextlib.suppress(Exception):
                engine.sink.close()
        with contextlib.suppress(Exception):
            engine.source.close()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
