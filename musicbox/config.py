"""Runtime configuration, read once from the environment at startup.

The first six names below are the frozen interface contract and must not be
renamed. Everything after them is an addition, and each one carries the reason
it had to exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

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
        )
