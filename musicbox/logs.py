"""Structured, greppable log lines.

Named logs.py and not logging.py on purpose: a module called logging.py inside
the package is legal under absolute imports but makes every stack trace and
every `import logging` read ambiguously, which is the last thing you want while
debugging at a live event.

Format is flat key=value, one line per event, no nesting and no JSON. At a
hackathon the tool you have is `journalctl -u musicbox | grep`, and key=value
survives grep, cut and awk. JSON does not, unless jq happens to be installed.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

_LOCK = threading.Lock()

# Anything outside this set gets quoted. Quoting everything would make the
# common case (event=request path=/health) noisier to read at a glance.
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./:@+")


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.3f}"
    else:
        text = str(value)
    if text == "":
        return '""'
    if all(ch in _SAFE for ch in text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def log(event: str, level: str = "info", **fields: Any) -> None:
    """Emit one log line. Field order is insertion order, so callers control it."""
    parts = [
        f"ts={time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())}Z",
        f"level={level}",
        f"event={event}",
    ]
    parts.extend(f"{key}={_fmt(value)}" for key, value in fields.items())
    line = " ".join(parts)
    # Writes from the request path and from the MA reader task interleave, and
    # a torn line is worse than a slow one.
    with _LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def warn(event: str, **fields: Any) -> None:
    log(event, level="warn", **fields)


def error(event: str, **fields: Any) -> None:
    log(event, level="error", **fields)
