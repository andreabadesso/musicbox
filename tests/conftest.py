"""Test helpers.

Async tests run through an explicit asyncio.run wrapper rather than
pytest-asyncio, so the suite has no plugin dependency and runs anywhere the app
itself runs. Every async test is wrapped in a timeout, because the failure mode
worth catching here is "hangs forever waiting for a reply", and a hung suite
tells you nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musicbox.config import Config  # noqa: E402
from musicbox.ma_client import MAClient  # noqa: E402

from fake_ma import FakeMA  # noqa: E402


def async_test(timeout: float = 15.0):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), timeout))

        return wrapper

    return decorate


@contextlib.asynccontextmanager
async def running_ma(token: str | None = "good-token"):
    server = FakeMA(token=token)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def config_for(server: FakeMA, **overrides) -> Config:
    values = dict(
        ma_url=server.url,
        player="musicbox",
        ma_token="good-token",
        command_timeout=5.0,
        announce_timeout=5.0,
        backoff_initial=0.05,
        backoff_max=0.2,
        # Desligado por padrao nos testes que NAO sao sobre prefetch. Ligado, a
        # suite tentaria baixar de verdade as URLs de exemplo (https://x/y.mp3)
        # e ficaria refem da rede da maquina que roda o teste. Os testes de
        # prefetch ligam explicitamente e apontam para um servidor local.
        prefetch=False,
    )
    values.update(overrides)
    return Config(**values)


@contextlib.asynccontextmanager
async def connected_client(server: FakeMA, **overrides):
    client = MAClient(config_for(server, **overrides))
    await client.start()
    try:
        yield client
    finally:
        await client.stop()
