"""Speech: piper renders text to a wav, and the box speaks it OVER the music.

Two paths out of here, exactly the two perform_sfx already has, and for the
same reasons:

  mixer        musicbox-mixer layers the clip on top of the music and ducks the
               music under it. This is the path that makes say() usable at all
               during an event: the room hears the sentence without the song
               stopping.
  announcement Music Assistant's announcement path, the fallback. MA SILENCES
               the music for the length of the clip and rejoins the track
               wherever it got to. It works on a box with no mixer, and it is
               what happens whenever the mixer is off or unreachable, because a
               sentence that does not get spoken is worse than one that
               interrupts a song.

WHERE THE RENDERED WAV LIVES, and this is not a detail. The mixer only knows
about effects its EffectCache decoded, and EffectCache scans exactly one flat
directory: MixerConfig.sfx_dir, which is the same MUSICBOX_SFX_DIR the app
serves from. There is no "play this file" command in the control protocol, only
`play <name>` against that cache. So a rendered clip has to land in the sfx
directory to be playable at all, and putting it there buys the other half for
free: GET /sfx/file/<name> already serves it, which is what the announcement
fallback needs, because MA refuses any announcement source that is not http(s)
(a literal `startswith("http")` in its player controller) and a wav on local
disk can never be handed over directly.

The cost of sharing that directory is that speech clips would otherwise show up
as sound effects on the soundboard and in list_sfx. Hence SAY_PREFIX: every
rendered clip is named `say-<hash>.wav`, app.list_sfx hides that prefix, and
_prune here refuses to touch anything without it. That last one is the trap
worth stating out loud: a prune that globbed *.wav in this directory would
delete the operator's airhorns.

Three more properties this module is shaped around:

  1. Rendering must never block the event loop. piper takes a few hundred
     milliseconds to a couple of seconds on a Pi 5, and the box is answering
     other requests the whole time, so it runs as an asyncio subprocess with a
     timeout and a kill.
  2. The same sentence gets said many times in an evening ("faltam 30 minutos",
     said at every checkpoint), so the output is cached on disk by hash of the
     text and the voice. A cache hit is a stat call.
  3. Nothing here raises for a configuration problem. TTS being unconfigured is
     a normal state of this box, and the caller is a model that needs a sentence
     it can read out, not a traceback.

Nothing here converts sample rates either. piper writes whatever the voice
model is (22.05 kHz mono for the medium pt_BR voices) and the box runs at
48000:16:2 everywhere, but the mixer's EffectCache already shells out to ffmpeg
to bring any input to the stream format, and MA re-encodes for the announcement
path. A second converter in here would be a second thing to get wrong.

WHY THE TEXT IS BOUNDED IN LENGTH, and it is not taste. MA wraps
players/cmd/play_announcement in a player lock acquired with a 30 second
deadline, and on timeout it LOGS A WARNING AND RUNS THE BODY ANYWAY
("previous holder appears stuck; proceeding without lock",
controllers/players/controller.py). Two announcements then run concurrently on
the same player, both restore the previous stream in their own finally, and the
last writer wins. Under 30 seconds a second announcement simply waits its turn.
The mixer path has no such limit, but the fallback can fire at any moment
without warning, so the cap is applied to every render rather than only to the
path that needs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import logs
from .config import Config

# Longest text piper will be handed in one call. Portuguese narration runs at
# roughly 14 to 16 characters a second, so 400 characters is about 26 seconds,
# which stays inside MA's 30 second announcement lock with room to spare. Longer
# input is REFUSED with a sentence telling the caller to split it, rather than
# truncated: half a sentence spoken to a room with no sign that it was cut is
# worse than being told to say it in two goes.
SAY_TEXT_MAX = 400

# Every rendered clip is named `say-<hash>.wav`. See the module docstring: the
# clips share the sfx directory with the operator's sound effects, and this
# prefix is the only thing that tells the two apart. app.list_sfx hides it from
# the soundboard and _prune below refuses to delete anything else.
SAY_PREFIX = "say-"

# How long piper is given before it is killed. A cold first run on a Pi 5 loads
# the model and can take several seconds; anything past this is a hang, and a
# hung render must not hold a request open for the whole announce timeout.
RENDER_TIMEOUT = 60.0

# How many renders may run at once on this box, and this is a limit on the
# HARDWARE and not on the feature. piper is onnxruntime: every concurrent call
# is another process that maps the voice model and spins up an inference thread
# pool sized to the core count. The Pi 5 has four cores and is already running
# Music Assistant, snapclient and musicbox-mixer, and the mixer's audio thread
# runs at ordinary priority (there is no Nice or RT setting on its unit, only a
# comment in nix/module.nix saying to try one if it ever drops out).
#
# Measured here with a fake piper that sleeps 600 ms: twenty four say() calls
# for twenty four different sentences spawned twenty four processes at once and
# finished in 1.13 s wall. On a laptop that is merely rude. On the Pi, in a room
# with a speaker, it is the audio thread missing its 20 ms deadline, and that
# comes out of the speaker as a dropout in the MUSIC rather than in the speech.
# A model that calls say() in a loop must not be able to do that, and nothing
# else here stops it: the per-key lock only serialises the SAME sentence.
#
# Two and not one: one would make a second speaker wait out a whole render for
# no reason, and two still leaves half the box for audio.
RENDER_CONCURRENCY = 2

# How long a call waits for one of those slots before giving up. Two slots at a
# couple of seconds a render is a queue about twenty sentences deep, far more
# than a room ever asks for at once. Past it the caller is TOLD the box is busy
# rather than left holding a request open, because a sentence that arrives a
# minute late has stopped being information.
RENDER_QUEUE_WAIT = 20.0

# A canonical wav header is 44 bytes, so a file no bigger than that has a header
# and NO AUDIO. See _is_usable for why this is checked by size.
WAV_HEADER_BYTES = 44

# How many rendered clips are kept. This number is bounded by RAM, not by disk,
# and that is the part worth writing down: every clip in the sfx directory is
# DECODED INTO MEMORY by the mixer's EffectCache on the next reload, and stays
# there. At 48000:16:2 that is 192 kB per second of speech, and a spoken
# sentence runs four to six seconds, so 60 clips is roughly 60 MB resident on a
# box that is also running Music Assistant. Three hundred would have been about
# 300 MB, which is the kind of number that turns into an OOM kill halfway
# through an event with nothing in the log that names speech as the cause.
#
# Sixty is also far more than an evening repeats: the same handful of
# checkpoint sentences are said over and over, and each of those is one file.
CACHE_MAX_FILES = 60

# THERE IS NO MINIMUM AGE, and the version of this that had one is worth
# recording. It kept any file younger than ten minutes, so that MA could not
# have a clip deleted out from under the fetch it makes a moment after say()
# returns. Measured: rendering eighty different sentences in a row left EIGHTY
# files on disk against a CACHE_MAX_FILES of sixty, because none of them were
# ten minutes old yet. A model calling say() in a loop reaches several hundred
# inside that window, the mixer decodes every one of them into RAM on its next
# reload, and the sixty megabyte budget computed above turns into an OOM kill
# mid-event. The bound has to hold at the moment the files are created or it is
# not a bound.
#
# The age check was also redundant with the rule that replaced it. Files are
# pruned oldest first and the newest CACHE_MAX_FILES are always kept, so a clip
# MA is about to fetch is by definition among the newest and cannot be selected:
# it would take sixty newer renders, minutes of them, to push it out. The file
# just written is protected explicitly on top of that, for the case where
# CACHE_MAX_FILES is ever turned down.
#
# What this does NOT do is keep the sentences an evening repeats. Eviction is by
# mtime and a cache HIT does not touch the file, so it is oldest-first and not
# least-recently-used. Deliberate: the mixer keys its decoded PCM on
# (name, size, mtime_ns), so touching a clip on every hit would invalidate the
# mixer's disk cache and make it re-run ffmpeg on the next reload. The cost of
# getting this wrong is one re-render of a sentence, which is a second.

# What a duration past this means: see the module docstring. Reported, not
# enforced, because the text cap above is the real guard and a voice that is
# simply slow should still speak.
LONG_CLIP_SECONDS = 25.0


class TTSError(Exception):
    """Speech did not happen, and the message is the whole explanation.

    Every raise site writes a full sentence for a model to read out or act on.
    Callers turn this into an answer, never into a 500.
    """


@dataclass
class Rendered:
    path: Path
    cached: bool
    duration: float | None


class TTS:
    """piper, wrapped so the rest of the app never thinks about processes."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._locks: dict[str, asyncio.Lock] = {}
        # Constructed here and not lazily: since Python 3.10 an asyncio
        # primitive no longer captures a loop when it is built, so this is safe
        # to make in create_app, outside any running loop. See
        # RENDER_CONCURRENCY for what it is protecting, which is the music.
        self._slots = asyncio.Semaphore(RENDER_CONCURRENCY)
        # Set the first time a render fails for a reason that is not going to
        # change on its own (a missing binary, a broken voice file). /health
        # reports it so an operator sees the real reason without reading logs.
        self.last_error: str | None = None

    # ── configuration ─────────────────────────────────────────────────────────

    @property
    def cache_dir(self) -> Path:
        # The sfx directory unless someone overrode MUSICBOX_TTS_CACHE_DIR.
        # Never None: Config.__post_init__ derives it from sfx_dir.
        return self._config.tts_cache_dir  # type: ignore[return-value]

    @property
    def voice(self) -> Path | None:
        return Path(self._config.piper_voice) if self._config.piper_voice else None

    @property
    def voice_name(self) -> str | None:
        """The voice as a person would name it, "pt_BR-faber-medium".

        The stem and not the full path, because this goes in /health and in
        every say() answer, and the full nix store path is 60 characters of
        hash that tell a reader nothing they wanted to know. The full path is
        still reported once, as tts_voice_path.
        """
        voice = self.voice
        return voice.stem if voice else None

    def unavailable_reason(self) -> str | None:
        """Why say() cannot work right now, or None when it can.

        Checked at call time rather than at startup on purpose: the voice model
        is a nix store path that appears when the unit is rebuilt, and a box
        whose TTS was configured while it was running should start speaking
        without a restart.
        """
        if not self._config.piper_bin:
            return (
                "Text to speech is not configured on this box: MUSICBOX_PIPER_BIN is "
                "unset, so there is no piper to render speech with."
            )
        binary = Path(self._config.piper_bin)
        if not binary.exists():
            return (
                f"The piper executable at {binary} does not exist, so speech cannot be "
                "rendered. MUSICBOX_PIPER_BIN points at nothing."
            )
        if not os.access(binary, os.X_OK):
            return f"The piper executable at {binary} is not executable, so speech cannot be rendered."
        if not self._config.piper_voice:
            return (
                "Text to speech has no voice model: MUSICBOX_PIPER_VOICE is unset, so "
                "piper has nothing to speak with."
            )
        voice = Path(self._config.piper_voice)
        if not voice.exists():
            return (
                f"The piper voice model at {voice} does not exist, so speech cannot be "
                "rendered."
            )
        # piper needs TWO files and only ever names one of them. The .onnx.json
        # beside the model carries the sample rate, the phoneme id map and the
        # espeak voice; piper builds its path by appending ".json" to the model
        # path and never says so. Without this check a missing json is a piper
        # traceback about a file the caller never typed, which every reader
        # takes for "the model is missing" while the model sits right there.
        # nix/module.nix pins the pair together for exactly this reason, and
        # this is the same trap caught on the side that has to explain it to a
        # person. Checked here rather than at render time so it shows up in
        # /health as tts_available false, before anyone tries to speak.
        companion = voice.with_name(voice.name + ".json")
        if not companion.exists():
            return (
                f"The piper voice model at {voice} has no {companion.name} beside it. "
                "piper reads that file for the sample rate and the phoneme map and "
                "fails without it, so speech cannot be rendered. The model itself is "
                "there; it is the companion json that is missing."
            )
        return None

    @property
    def available(self) -> bool:
        return self.unavailable_reason() is None

    def status(self) -> dict[str, Any]:
        """What /health says about speech. Always the truth, never a guess."""
        reason = self.unavailable_reason()
        cached = None
        with contextlib.suppress(OSError):
            cached = len([p for p in self.cache_dir.iterdir() if is_say_file(p)])
        return {
            "tts_available": reason is None,
            "tts_reason": reason,
            "tts_voice": self.voice_name,
            "tts_voice_path": self._config.piper_voice or None,
            "tts_cache_dir": str(self.cache_dir),
            "tts_cached_clips": cached,
            "tts_last_error": self.last_error,
        }

    # ── rendering ─────────────────────────────────────────────────────────────

    def clean(self, text: str) -> str:
        """The text piper will actually be given, or a TTSError explaining why not.

        Newlines are collapsed because they are sent to piper on stdin and a
        newline there is a clip boundary: piper would render only the first line
        and the room would hear half the message with nothing to indicate it.
        """
        # Control characters go before the whitespace collapse. str.split()
        # already handles the ones that are whitespace (\\n, \\t, \\x1c to \\x1f,
        # \\x85), and what is left is the ones that are not: a NUL, an escape, a
        # bell. None of them are pronounceable and one of them is a hazard. The
        # C++ piper reads the line into a C string, so an embedded NUL TRUNCATES
        # the sentence there, and the room hears the first half of a message
        # with nothing to say it was cut. It cannot escape the command line
        # (the text goes in on stdin and never touches argv, which is checked in
        # the tests), so this is about the sentence arriving whole, not about
        # injection.
        raw = "".join(ch for ch in (text or "") if ch.isprintable() or ch.isspace())
        value = " ".join(raw.split())
        if not value:
            raise TTSError("There was no text to say, so nothing was spoken.")
        if len(value) > SAY_TEXT_MAX:
            raise TTSError(
                f"That text is {len(value)} characters, longer than the {SAY_TEXT_MAX} "
                "character limit for one announcement. Music Assistant only serialises "
                "announcements shorter than 30 seconds, so say it in two or three "
                "shorter calls instead. Nothing was spoken."
            )
        return value

    def cache_path(self, text: str, voice: Path) -> Path:
        """Where the clip for this exact (text, voice) pair lives.

        sha256 over both fields with a separator that cannot occur in either, so
        changing the voice re-renders instead of silently returning the old
        voice's audio. Truncated to 32 hex characters: this is a cache key, not
        a security boundary, and a shorter name keeps the URL readable in a log.
        """
        digest = hashlib.sha256(f"{voice}\x00{text}".encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{SAY_PREFIX}{digest}.wav"

    async def render(self, text: str) -> Rendered:
        """Text in, a playable wav on disk out. Never blocks the event loop."""
        reason = self.unavailable_reason()
        if reason is not None:
            raise TTSError(reason + " Nothing was spoken.")
        cleaned = self.clean(text)
        voice = self.voice
        assert voice is not None  # unavailable_reason already proved it is set
        target = self.cache_path(cleaned, voice)

        if _is_usable(target):
            return Rendered(path=target, cached=True, duration=_duration(target))

        # One lock per cache key, not one global lock. Two different sentences
        # can render at once (piper is a separate process either way), but the
        # SAME sentence arriving twice, which is exactly what a countdown does,
        # must not run piper twice and must not have one call read a file the
        # other is still writing.
        lock = self._locks.setdefault(target.name, asyncio.Lock())
        try:
            async with lock:
                if _is_usable(target):
                    return Rendered(path=target, cached=True, duration=_duration(target))
                await self._render_in_a_slot(cleaned, voice, target)
        finally:
            # In a finally, and that is the fix rather than the tidy-up: this
            # used to run only after a SUCCESSFUL render, so every failure left
            # its lock behind. Measured: fifty failed renders, fifty Lock
            # objects held forever. A box whose voice model is broken fails
            # every call, and every call is a different sentence.
            #
            # Popped outside the lock so a long-running box does not accumulate
            # one lock object per distinct sentence ever spoken. A caller that
            # arrives between the release and the pop builds a second lock and
            # finds the finished file on its own re-check, which is why the
            # re-check inside the lock is not optional.
            self._locks.pop(target.name, None)

        duration = _duration(target)
        if duration is not None and duration > LONG_CLIP_SECONDS:
            logs.warn("tts_long_clip", seconds=duration, chars=len(cleaned))
        _prune(self.cache_dir, keep=target)
        return Rendered(path=target, cached=False, duration=duration)

    async def _render_in_a_slot(self, text: str, voice: Path, target: Path) -> None:
        """Wait for one of the RENDER_CONCURRENCY slots, then run piper in it.

        The wait is BOUNDED and the timeout is an answer, not an exception the
        caller has to guess at. An unbounded wait here would turn a model
        looping on say() into a pile of HTTP requests all held open, and the
        room would get the whole backlog at once several minutes later.
        """
        try:
            await asyncio.wait_for(self._slots.acquire(), RENDER_QUEUE_WAIT)
        except asyncio.TimeoutError:
            logs.warn("tts_busy", waited=RENDER_QUEUE_WAIT, chars=len(text))
            raise TTSError(
                f"The box is already rendering as much speech as it can and this "
                f"sentence waited {RENDER_QUEUE_WAIT:.0f} seconds for a turn, so "
                "nothing was spoken. Say fewer things at once. The music was not "
                "interrupted."
            ) from None
        try:
            await self._run_piper(text, voice, target)
        finally:
            self._slots.release()

    async def _run_piper(self, text: str, voice: Path, target: Path) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.last_error = f"cache dir: {exc}"
            raise TTSError(
                f"The speech cache directory {self.cache_dir} could not be created "
                f"({exc}), so nothing was spoken."
            ) from None

        # Written to a temp name in the SAME directory and renamed into place.
        # os.replace is atomic within a filesystem, so /tts/file/<name> either
        # serves a complete wav or 404s. A half-written file served to MA fails
        # with "Announcement duration could not be determined", which reads like
        # a musicbox bug and is not one.
        tmp = target.with_name(target.name + f".{os.getpid()}.part")

        # The argv shape, and why each piece: `-m` takes a path to the .onnx
        # model, which stops piper from trying to DOWNLOAD a voice by name at
        # speech time (there is no internet contract for this box, and a
        # download inside an announcement would be a disaster). `-f` is the
        # output wav. The text goes in on STDIN rather than as an argument, so a
        # sentence that happens to start with a dash cannot be read as a flag,
        # and so a long line is not subject to any argv limit.
        argv = [self._config.piper_bin, "-m", str(voice), "-f", str(tmp)]

        # The service environment plus a cap on how much of the box piper is
        # allowed to take. onnxruntime and the BLAS under it size their thread
        # pools from the core count, so an unconstrained piper opens four
        # compute threads on a four core Pi that is also mixing audio in real
        # time, and RENDER_CONCURRENCY lets two of those exist at once. Both
        # variables are the standard spellings, ignored by any build that does
        # not use that runtime, so this costs nothing where it is not needed.
        #
        # Two and not one: a render that takes twice as long is a sentence that
        # arrives late, and this is a box whose job is to be heard on time.
        # Inherited env and not a fresh one: piper is a store path with a
        # wrapper that needs its own PATH and library variables, and a service
        # this box runs does not get to guess at those.
        env = dict(os.environ, OMP_NUM_THREADS="2", ORT_NUM_THREADS="2")
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            self.last_error = f"spawn: {exc}"
            raise TTSError(
                f"piper could not be started ({exc}), so nothing was spoken."
            ) from None

        try:
            _, err = await asyncio.wait_for(
                proc.communicate((text + "\n").encode("utf-8")), RENDER_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            _unlink(tmp)
            self.last_error = "render timed out"
            raise TTSError(
                f"piper did not finish rendering within {RENDER_TIMEOUT:.0f} seconds and "
                "was killed, so nothing was spoken."
            ) from None

        elapsed = time.monotonic() - started
        if proc.returncode != 0:
            _unlink(tmp)
            detail = (err or b"").decode("utf-8", "replace").strip().splitlines()
            # The LAST line, not the first: piper's traceback puts the reason at
            # the bottom, and the first line is always the same "Traceback".
            message = detail[-1] if detail else f"exit code {proc.returncode}"
            self.last_error = message
            logs.warn("tts_failed", code=proc.returncode, detail=message, dur_ms=elapsed * 1000)
            raise TTSError(
                f"piper failed to render the speech ({message}), so nothing was spoken. "
                "The music was not interrupted."
            )

        if not _is_usable(tmp):
            _unlink(tmp)
            self.last_error = "piper produced no audio"
            raise TTSError(
                "piper exited successfully but produced no audio, so nothing was spoken."
            )

        # Size is not enough, and this is THE failure this box is worst at.
        # piper exits 0 and writes a valid 44 byte wav with zero frames for any
        # text that phonemises to nothing: punctuation on its own, an emoji, a
        # zero width character, a word espeak-ng cannot pronounce in pt-br. The
        # size check above passes it, the clip is cached, and every later call
        # is a cache HIT on silence. What comes back is ok: true, spoken: true,
        # the mixer reports a voice, /health is green, and the room hears
        # nothing. Forever, because the cache never re-renders it.
        #
        # Reproduced with a piper stub that writes a header and no frames:
        # render returned ok with duration 0.0, and the second call came back
        # cached. Now it is refused, and the caller is told what to change.
        seconds = _duration(tmp)
        if seconds is not None and seconds <= 0.0:
            _unlink(tmp)
            self.last_error = "piper rendered a clip with no audio in it"
            logs.warn("tts_empty_clip", chars=len(text))
            raise TTSError(
                "piper produced a clip with no audio in it, so nothing was spoken. "
                "That happens when the text is punctuation, symbols or emoji with no "
                "word in it for the voice to pronounce. Send words."
            )
        if seconds is None:
            # Not fatal. `wave` refusing the header does not prove the file is
            # useless: the mixer decodes through ffmpeg, which reads far more
            # than the stdlib does, and MA re-encodes on its own path. Logged
            # because if speech ever goes quiet with everything green again,
            # this is the line that says the render stopped looking like a wav.
            logs.warn("tts_unreadable_header", file=tmp.name, chars=len(text))

        # Readable by everyone, on purpose, and this is load bearing.
        #
        # The mixer runs as its OWN user (musicbox-mixer), while musicbox runs
        # under DynamicUser. The wav is handed over by leaving it in the shared
        # sfx dir, and the mixer only ever opens it as that other uid. piper
        # creates this file itself, under the service UMask, and the NixOS unit
        # sets UMask=0077 — so it lands 0600 and os.replace keeps that mode
        # rather than taking the target's. Unreadable to the only process that
        # can play it.
        #
        # What that failure looks like is worth naming, because nothing errors:
        # piper renders fine, `tts_rendered` is logged, /health stays green with
        # tts_available true, POST /say answers 200 — and the mixer's scan
        # silently skips the file it cannot open, so `play` comes back
        # `not_found` and speech falls through to the announcement path, which
        # needs MA and stops the music. Measured on the pi5: mixer cache count
        # 27 with the clip at 0600, 28 with the same clip at 0644.
        #
        # Fixed here and not with UMask= in the unit, because the mode this file
        # needs is a property of the handoff, not of the host wiring it.
        # The directory is already shared; there is no secret in a party wav.
        try:
            os.chmod(tmp, 0o644)
        except OSError as exc:
            # Not fatal by itself: if the mode was already right (a umask that
            # allows it, or a future non-mkstemp path) the clip still plays.
            # Logged rather than raised so a chmod refusal on some exotic
            # filesystem does not take speech down entirely.
            logs.warn("tts_chmod_failed", file=tmp.name, error=str(exc))

        try:
            os.replace(tmp, target)
        except OSError as exc:
            _unlink(tmp)
            self.last_error = f"rename: {exc}"
            raise TTSError(
                f"The rendered speech could not be saved ({exc}), so nothing was spoken."
            ) from None

        self.last_error = None
        logs.log("tts_rendered", chars=len(text), file=target.name, dur_ms=elapsed * 1000)


# ── module helpers ────────────────────────────────────────────────────────────


def is_say_file(path: Path) -> bool:
    """A rendered speech clip, as opposed to a sound effect a human put here.

    Public because app.list_sfx needs exactly this predicate to keep speech off
    the soundboard, and two copies of the naming rule in two modules is how the
    soundboard ends up showing `say-3f8c...` to a room.
    """
    return path.name.startswith(SAY_PREFIX) and path.suffix.lower() == ".wav"


def _is_usable(path: Path) -> bool:
    """A file that exists and has AUDIO in it, judged by size alone.

    Size and not just existence: an interrupted render leaves a zero byte file
    behind, and treating that as a cache hit means the same silent announcement
    for as long as the box runs.

    WAV_HEADER_BYTES and not zero, which is the same bug one step along. A wav
    with a complete header and no frames is 44 bytes, piper writes exactly that
    for text it cannot phonemise, and it is the shape of "everything green and
    the room hears nothing". _run_piper rejects those at render time by reading
    the frame count, but this is the CACHE HIT path and it also has to reject
    the ones a previous version of this file already wrote to disk.

    Still one stat and no open, which is the promise the module docstring makes
    about a cache hit. The frame count costs an open and a parse and is only
    worth it once, on the render.
    """
    try:
        return path.stat().st_size > WAV_HEADER_BYTES
    except OSError:
        return False


def _duration(path: Path) -> float | None:
    """Seconds of audio, read from the wav header. None when it cannot be read.

    Only ever used for reporting and for the long-clip warning, so anything
    unreadable is None rather than an error.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if not rate:
                return None
            return handle.getnframes() / float(rate)
    except Exception:  # noqa: BLE001 - a duration is never worth failing over
        return None


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _prune(cache_dir: Path, keep: Path | None = None) -> None:
    """Keep the newest CACHE_MAX_FILES clips and delete the rest.

    is_say_file and NOT a *.wav glob. This directory is shared with the
    operator's sound effects (see the module docstring), and a prune that
    matched on the extension alone would quietly delete airhorn.wav the first
    time the box had said three hundred different sentences. There is no
    version of that bug that is easy to diagnose from the room.

    `keep` is the clip that was just rendered, protected by name whatever its
    mtime says. Redundant while CACHE_MAX_FILES is sixty, since the newest file
    is never in the tail, and cheap insurance for whoever turns that number
    down to two while debugging and wonders why announcements 404.

    Nothing is exempted by age. There used to be a ten minute exemption; the
    note above CACHE_MAX_FILES records what it did to the bound and why the
    rule that replaced it covers the case the exemption was written for.
    """
    try:
        entries = [p for p in cache_dir.iterdir() if p.is_file() and is_say_file(p)]
    except OSError:
        return
    if len(entries) <= CACHE_MAX_FILES:
        return

    # The mtime is read ONCE per file, here, rather than inside a sort key that
    # can raise partway through. The previous version sorted under a suppressed
    # OSError, so a file deleted by anything else mid-sort left the list in
    # whatever half-sorted order it had reached and the tail that got deleted
    # was then arbitrary: it could take the newest clips and leave the oldest.
    # A file that has vanished sorts as ancient and is skipped on unlink.
    def age(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    entries.sort(key=age, reverse=True)
    protected = keep.name if keep is not None else None
    for path in entries[CACHE_MAX_FILES:]:
        if path.name == protected:
            continue
        _unlink(path)
