"""Runtime configuration, read once from the environment at startup.

The first six names below are the frozen interface contract and must not be
renamed. Everything after them is an addition, and each one carries the reason
it had to exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099
DEFAULT_MA_URL = "http://127.0.0.1:8095"
DEFAULT_SFX_DIR = "/var/lib/musicbox/sfx"
DEFAULT_CACHE_DIR = "/var/lib/musicbox/cache"


def _read_secret(env: Mapping[str, str], inline: str, from_file: str) -> str:
    """Prefer the inline value, fall back to a file holding it.

    The file form exists so a NixOS unit can point at a path outside the nix
    store (LoadCredential or a plain 0600 file) instead of interpolating a
    secret into a world readable store path.
    """
    value = env.get(inline, "").strip()
    if value:
        return value
    path = env.get(from_file, "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        # Deliberately not fatal. A missing token file must degrade to "no
        # auth configured" or "MA auth will fail loudly", never to a crash
        # loop on a box nobody is watching.
        return ""


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(env, key: str, default: bool) -> bool:
    """Read a flag the way a person would write it.

    "0", "false", "no" and "off" are all off, case insensitive. Anything else
    that is set counts as on. A typo therefore errs towards the feature being
    enabled, which is the right direction here: prefetch protects playback, so
    accidentally leaving it on costs disk, while accidentally turning it off
    costs silence in a room.
    """
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _octal(env: Mapping[str, str], name: str, default: int) -> int:
    """A file mode, written the way a person writes one: 0660, 660, 0o660.

    int(raw, 8) and not int(raw): "660" read as decimal is 0o1224, which is a
    mode with the setuid bit in it and no obvious complaint from chmod.
    """
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw, 8)
    except ValueError:
        return default


@dataclass
class MixerConfig:
    """Settings for musicbox-mixer, the process that layers effects over music.

    Every one of these is inert by default, and `enable` is the switch the rest
    of the code reads. With it off, musicbox behaves exactly as it did before
    the mixer existed: POST /sfx goes to Music Assistant's announcement path
    and nothing in the deployed chain changes. That is deliberate. The box this
    ships to is playing music to a room right now.

    The settings live here, in one dataclass, rather than being parsed inside
    mixer.py, so that `musicbox-server --check` and `musicbox-mixer --check`
    resolve them the same way, and so app.py can read `enable` and `socket`
    without importing anything that needs numpy or a sound card.

    NOTE FOR THE NIXOS WIRING: the mixer runs as its own unit and has to READ
    the sfx directory musicbox owns. musicbox uses DynamicUser plus
    StateDirectory, so those files really live under /var/lib/private/musicbox
    and /var/lib/private is 0700 root. A separate static user cannot read
    through that however friendly the file modes look. Either give the mixer
    the same StateDirectory, so systemd bind-mounts it into view, or point
    sfxDir somewhere both units can see. If this is wrong the mixer comes up
    green with an empty effect list and plays nothing, which is the failure
    mode this project fears most.
    """

    # ── the switch ────────────────────────────────────────────────────────────
    enable: bool = False

    # ── how musicbox talks to it ──────────────────────────────────────────────
    # A unix socket, not a second HTTP port: one network control surface on
    # this box is already enough to authenticate, firewall and explain.
    socket: Path | None = field(default_factory=lambda: Path("/run/musicbox/mixer.sock"))
    # 0660 plus a shared group, because musicbox and the mixer are different
    # users. 0600 would make every effect fall back to the announcement path
    # over a permission error nobody is reading during an event.
    socket_mode: int = 0o660
    # Generous for a unix socket round trip that should be sub-millisecond.
    # This is a bound on how long a person waits before the fallback fires, not
    # an expected duration.
    timeout: float = 2.0

    # ── the audio path ────────────────────────────────────────────────────────
    # The fifo snapclient writes into with --player file:filename=<path>. The
    # mixer must own it BEFORE snapclient starts: snapcast's FilePlayer calls
    # fopen(path, "wb") inside its constructor, and that blocks on a fifo until
    # a reader exists. With no reader snapclient hangs, the unit still reports
    # active, and the room is silent. Unit ordering is not optional.
    fifo: Path | None = field(default_factory=lambda: Path("/run/musicbox/snapfifo"))
    # In production the bluealsa PCM with the speaker's MAC in it. There is no
    # discovering this: alsaaudio.pcms() lists no bluealsa entry at all,
    # because bluez-alsa ships no ALSA namehints.
    device: str = ""
    sfx_dir: Path = field(default_factory=lambda: Path(DEFAULT_SFX_DIR))
    # Decoded PCM cache, separate from the download cache: different contents
    # (raw s16le keyed by source mtime), and clearing one should never mean
    # clearing the other.
    cache_dir: Path = field(default_factory=lambda: Path(DEFAULT_CACHE_DIR) / "pcm")

    # ── buffering, where this box has already been burned twice ───────────────
    # 960 frames at 10 periods measures out as exactly 200 ms of ALSA buffer,
    # which is the ONE depth proven to work here: 80 ms stutters (484 XRUNs in
    # 5 minutes) and 500 ms stops playing entirely while everything reports
    # healthy. The only change from today's snapclient is 20 ms periods instead
    # of 25 ms, which is the safe direction. If you want the effects tighter,
    # walk `periods` down towards 6 (120 ms) WHILE LISTENING. Not blind.
    periodsize: int = 960
    periods: int = 10
    # The default pipe on this kernel is 262144 bytes, which is 1365 ms of
    # audio: a latency bomb that never drains back down once it fills. The
    # Pi 5 here uses 16384 byte pages, so F_SETPIPE_SZ only offers 16384
    # (85 ms) or 65536 (341 ms). There is nothing in between.
    pipe_bytes: int = 16384
    poll_ms: int = 10
    # Two clocks drift: snapclient paces the fifo off CLOCK_MONOTONIC while the
    # ALSA write is paced by the A2DP clock. Past this much backlog one period
    # of MUSIC is dropped to catch up. Never corrected by growing a buffer.
    #
    # 4 and not 2, and the arithmetic is the whole reason. snapclient does not
    # trickle bytes into the fifo, it writes one 9600 byte chunk every 50 ms
    # (hard coded in file_player.cpp), which at the default 3840 byte period is
    # 2.5 periods arriving at once. The reader also needs about one period of
    # cushion to survive the gaps between those chunks. So a healthy chain sits
    # at a backlog that swings up to roughly 3.5 periods, every cycle, forever.
    # A threshold of 2 would therefore fire on a chain with nothing wrong with
    # it, drop a period of music, starve on the next read, and do it again 20
    # times a second: a permanent stutter that looks like a correction working.
    # The floor in FifoSource.limit_bytes enforces this whatever is set here.
    max_backlog_periods: float = 4.0

    # ── the sound of it ───────────────────────────────────────────────────────
    # How far the music is pushed down while an effect plays. -12 dB is a
    # quarter of the amplitude: clearly under the effect, still obviously
    # playing. In dB and not as a ratio because snapclient's own software
    # volume has already scaled the music before it reaches us, so a fixed
    # subtraction is the only thing that means the same at every volume.
    duck_db: float = -12.0
    fade_in_ms: float = 60.0
    # Slower coming back than going down, which is what an ear expects from a
    # duck. A fast release sounds like the music jumping back in.
    fade_out_ms: float = 250.0
    gain_db: float = 0.0
    # Room for rapid fire on one key plus a couple of other effects on top.
    # Four voices at a 20 ms period cost 51 us on the Pi, so this limit is
    # about taste and headroom, not CPU.
    polyphony: int = 8
    log_every: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MixerConfig":
        env = os.environ if env is None else env
        defaults = cls()

        def path_or_none(name: str, default: Path | None) -> Path | None:
            raw = env.get(name)
            if raw is None:
                return default
            raw = raw.strip()
            # An explicitly EMPTY value means "no fifo" or "no socket". That is
            # how an offline render and a headless test turn them off without
            # needing a second flag for each.
            return Path(raw) if raw else None

        return cls(
            enable=_bool(env, "MUSICBOX_MIXER", False),
            socket=path_or_none("MUSICBOX_MIXER_SOCKET", defaults.socket),
            socket_mode=_octal(env, "MUSICBOX_MIXER_SOCKET_MODE", defaults.socket_mode),
            timeout=_float(env, "MUSICBOX_MIXER_TIMEOUT", defaults.timeout),
            fifo=path_or_none("MUSICBOX_MIXER_FIFO", defaults.fifo),
            device=env.get("MUSICBOX_MIXER_DEVICE", "").strip(),
            # Shared with musicbox on purpose: one directory of sounds, one
            # name for each of them, whichever surface plays it.
            sfx_dir=Path(env.get("MUSICBOX_SFX_DIR", DEFAULT_SFX_DIR).strip() or DEFAULT_SFX_DIR),
            cache_dir=Path(
                env.get("MUSICBOX_MIXER_CACHE_DIR", "").strip() or str(defaults.cache_dir)
            ),
            periodsize=_int(env, "MUSICBOX_MIXER_PERIODSIZE", defaults.periodsize),
            periods=_int(env, "MUSICBOX_MIXER_PERIODS", defaults.periods),
            pipe_bytes=_int(env, "MUSICBOX_MIXER_PIPE_BYTES", defaults.pipe_bytes),
            poll_ms=_int(env, "MUSICBOX_MIXER_POLL_MS", defaults.poll_ms),
            max_backlog_periods=_float(
                env, "MUSICBOX_MIXER_MAX_BACKLOG_PERIODS", defaults.max_backlog_periods
            ),
            duck_db=_float(env, "MUSICBOX_MIXER_DUCK_DB", defaults.duck_db),
            fade_in_ms=_float(env, "MUSICBOX_MIXER_FADE_IN_MS", defaults.fade_in_ms),
            fade_out_ms=_float(env, "MUSICBOX_MIXER_FADE_OUT_MS", defaults.fade_out_ms),
            gain_db=_float(env, "MUSICBOX_MIXER_GAIN_DB", defaults.gain_db),
            polyphony=_int(env, "MUSICBOX_MIXER_VOICES", defaults.polyphony),
            log_every=_float(env, "MUSICBOX_MIXER_LOG_EVERY", defaults.log_every),
        )

    def describe(self) -> dict[str, Any]:
        """Flat and loggable. Both --check paths print exactly this."""
        return {
            "enable": self.enable,
            "socket": str(self.socket) if self.socket else None,
            "fifo": str(self.fifo) if self.fifo else None,
            "device": self.device or None,
            "sfx_dir": str(self.sfx_dir),
            "cache_dir": str(self.cache_dir),
            "periodsize": self.periodsize,
            "periods": self.periods,
            # 48 frames per ms and 192 bytes per ms, at 48000:16:2. Printed
            # because "periodsize=960 periods=10" tells nobody that this is the
            # 200 ms that works and 500 ms is the one that kills playback.
            "period_ms": round(self.periodsize / 48.0, 1),
            "buffer_ms": round(self.periodsize * self.periods / 48.0, 1),
            "pipe_bytes": self.pipe_bytes,
            "pipe_ms": round(self.pipe_bytes / 192.0, 1),
            "duck_db": self.duck_db,
            "fade_in_ms": self.fade_in_ms,
            "fade_out_ms": self.fade_out_ms,
            "gain_db": self.gain_db,
            "polyphony": self.polyphony,
        }


@dataclass
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    ma_url: str = DEFAULT_MA_URL
    player: str = ""
    sfx_dir: Path = field(default_factory=lambda: Path(DEFAULT_SFX_DIR))
    token: str = ""

    # ── Baixar antes de tocar ─────────────────────────────────────────────────
    # Where downloaded audio lands, and whether to download at all. On by
    # default because streaming a remote URL over a bad link is how the box
    # fails in front of people: MA answers "Timeout waiting for audio data" and
    # nothing plays. See musicbox/prefetch.py for the measurements.
    cache_dir: Path = field(default_factory=lambda: Path(DEFAULT_CACHE_DIR))
    prefetch: bool = True
    prefetch_timeout: float = 300.0
    prefetch_max_bytes: int = 100 * 1024 * 1024

    # Music Assistant 2.9 authenticates every command except the four auth ones,
    # so a token is not optional in practice. It is provisioned once (POST
    # /setup, auth/login, auth/token/create) and handed to us in a file.
    ma_token: str = ""

    # The URL MA must use to fetch our sfx files. See app.sfx_base_url for why
    # this cannot simply be our own listen address.
    sfx_base_url: str = ""

    # Ordinary commands are sub second. The generous default is for the case
    # where MA is busy resolving a Spotify item on a cold cache.
    command_timeout: float = 20.0

    # play_announcement blocks for the full duration of the audio plus the
    # snapserver buffer, so its timeout has to exceed the longest sfx.
    announce_timeout: float = 300.0

    # Reconnect backoff bounds for the MA websocket.
    backoff_initial: float = 1.0
    backoff_max: float = 30.0

    # ── musicbox-mixer ────────────────────────────────────────────────────────
    # Nested rather than flattened into a dozen more top level fields, because
    # only three of them (enable, socket, timeout) are any of this process's
    # business; the rest belong to the mixer process and are here so that both
    # sides resolve them identically. Defaults to a MixerConfig with
    # enable=False, so a Config built by hand in a test is a box with no mixer.
    mixer: MixerConfig = field(default_factory=MixerConfig)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            host=env.get("MUSICBOX_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST,
            port=_int(env, "MUSICBOX_PORT", DEFAULT_PORT),
            ma_url=(env.get("MUSICBOX_MA_URL", DEFAULT_MA_URL).strip() or DEFAULT_MA_URL).rstrip("/"),
            player=env.get("MUSICBOX_PLAYER", "").strip(),
            sfx_dir=Path(env.get("MUSICBOX_SFX_DIR", DEFAULT_SFX_DIR).strip() or DEFAULT_SFX_DIR),
            cache_dir=Path(env.get("MUSICBOX_CACHE_DIR", DEFAULT_CACHE_DIR).strip() or DEFAULT_CACHE_DIR),
            prefetch=_bool(env, "MUSICBOX_PREFETCH", True),
            prefetch_timeout=_float(env, "MUSICBOX_PREFETCH_TIMEOUT", 300.0),
            prefetch_max_bytes=_int(env, "MUSICBOX_PREFETCH_MAX_BYTES", 100 * 1024 * 1024),
            token=_read_secret(env, "MUSICBOX_TOKEN", "MUSICBOX_TOKEN_FILE"),
            ma_token=_read_secret(env, "MUSICBOX_MA_TOKEN", "MUSICBOX_MA_TOKEN_FILE"),
            sfx_base_url=env.get("MUSICBOX_SFX_BASE_URL", "").strip().rstrip("/"),
            command_timeout=_float(env, "MUSICBOX_COMMAND_TIMEOUT", 20.0),
            announce_timeout=_float(env, "MUSICBOX_ANNOUNCE_TIMEOUT", 300.0),
            backoff_initial=_float(env, "MUSICBOX_BACKOFF_INITIAL", 1.0),
            backoff_max=_float(env, "MUSICBOX_BACKOFF_MAX", 30.0),
            mixer=MixerConfig.from_env(env),
        )
