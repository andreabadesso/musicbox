"""Entry point for the musicbox-server console script."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from . import __version__, logs
from .app import create_app
from .config import Config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="musicbox-server",
        description="Event shaped HTTP API in front of Music Assistant.",
    )
    parser.add_argument("--version", action="version", version=f"musicbox {__version__}")
    parser.add_argument("--host", default=None, help="overrides MUSICBOX_HOST")
    parser.add_argument("--port", type=int, default=None, help="overrides MUSICBOX_PORT")
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the resolved configuration and exit, without binding a port",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    config = Config.from_env()
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    if args.check:
        logs.log(
            "config",
            host=config.host,
            port=config.port,
            ma_url=config.ma_url,
            player=config.player or None,
            sfx_dir=str(config.sfx_dir),
            sfx_dir_exists=config.sfx_dir.is_dir(),
            auth=bool(config.token),
            ma_token=bool(config.ma_token),
            sfx_base_url=config.sfx_base_url or None,
        )
        return 0

    app = create_app(config)
    # log_config=None keeps uvicorn from installing its own handlers and
    # reformatting our lines. Access logging is off because the middleware in
    # app.py already emits one structured line per request, and two lines per
    # request in different formats is worse than either alone.
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
