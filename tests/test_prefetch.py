"""Prefetch: baixar antes de tocar.

Os testes daqui sobem um servidor HTTP de verdade e baixam dele. Um mock de
aiohttp nao provaria a parte que interessa, que e o comportamento em cima de
uma fonte real: redirect, resposta sem content-length, corte no meio.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import web

from conftest import async_test  # noqa: E402

from musicbox.prefetch import PrefetchError, Prefetcher, _safe_name  # noqa: E402


class Source:
    """Servidor de origem, o papel de quem hospeda o mp3."""

    def __init__(self, body: bytes = b"ID3fake-audio-payload" * 100, *, stream: bool = False):
        self.body = body
        # stream=True responde sem content-length, em pedacos. E o caso que
        # importa para o limite de tamanho: sem cabecalho declarado, so da para
        # cortar contando o que chega.
        self.stream = stream
        self.hits = 0

    async def start(self):
        async def handler(request):
            self.hits += 1
            if self.stream:
                response = web.StreamResponse(headers={"content-type": "audio/mpeg"})
                await response.prepare(request)
                for _ in range(64):
                    await response.write(self.body)
                await response.write_eof()
                return response
            return web.Response(body=self.body, headers={"content-type": "audio/mpeg"})

        async def redirector(request):
            raise web.HTTPFound("/audio.mp3")

        app = web.Application()
        app.router.add_get("/audio.mp3", handler)
        app.router.add_get("/redirect", redirector)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def stop(self):
        await self.runner.cleanup()


@async_test()
async def test_downloads_once_and_serves_from_disk(tmp_path: Path):
    source = await Source().start()
    try:
        pf = Prefetcher(tmp_path / "cache", "http://box:8099")
        first = await pf.ensure_local(f"{source.url}/audio.mp3")
        assert first.cached is False
        assert first.bytes == len(source.body)
        assert (tmp_path / "cache" / first.filename).is_file()

        # A segunda chamada NAO pode tocar a origem. Num evento a mesma faixa e
        # pedida varias vezes, e rebaixar cada vez desperdicaria a banda que o
        # prefetch existe para poupar.
        second = await pf.ensure_local(f"{source.url}/audio.mp3")
        assert second.cached is True
        assert second.filename == first.filename
        assert source.hits == 1

        assert pf.local_url(first.filename) == f"http://box:8099/cache/file/{first.filename}"
    finally:
        await source.stop()


@async_test()
async def test_follows_a_redirect(tmp_path: Path):
    # A musica gerada do hackathon responde 302 para uma URL assinada do Google
    # Storage. Se o prefetch nao seguisse o redirect, gravaria a pagina de
    # redirecionamento como se fosse audio.
    source = await Source().start()
    try:
        pf = Prefetcher(tmp_path / "cache", "http://box:8099")
        result = await pf.ensure_local(f"{source.url}/redirect")
        assert result.bytes == len(source.body)
        assert (tmp_path / "cache" / result.filename).read_bytes() == source.body
    finally:
        await source.stop()


@async_test()
async def test_the_size_cap_is_enforced_during_the_download(tmp_path: Path):
    # Resposta em pedacos, sem content-length declarado. E o caso perigoso: sem
    # cabecalho para conferir antes, o unico jeito de cortar e contando o que
    # chega, e sem isso uma origem grande enche o cartao SD no meio do evento.
    source = await Source(stream=True).start()
    try:
        pf = Prefetcher(tmp_path / "cache", "http://box:8099", max_bytes=500)
        with pytest.raises(PrefetchError) as err:
            await pf.ensure_local(f"{source.url}/audio.mp3")
        assert "size limit" in str(err.value)
        # E nao pode sobrar lixo pela metade no cache.
        assert list((tmp_path / "cache").glob("*")) == []
    finally:
        await source.stop()


@async_test()
async def test_a_dead_source_raises_a_sentence_not_a_traceback(tmp_path: Path):
    pf = Prefetcher(tmp_path / "cache", "http://box:8099", timeout=2.0)
    with pytest.raises(PrefetchError) as err:
        await pf.ensure_local("http://127.0.0.1:1/nao-existe.mp3")
    assert "Could not download" in str(err.value) or "took longer" in str(err.value)


def test_handles_skips_what_is_already_local():
    pf = Prefetcher(Path("/tmp/cache"), "http://box:8099")
    assert pf.handles("https://example.com/song.mp3") is True
    # O proprio musicbox: baixar de si mesmo seria um laco que parece travamento.
    assert pf.handles("http://box:8099/sfx/file/airhorn.mp3") is False
    assert pf.handles("http://127.0.0.1:8099/cache/file/x.mp3") is False
    assert pf.handles("http://localhost/x.mp3") is False
    # Nao e URL nenhuma.
    assert pf.handles("spotify://track/abc") is False
    assert pf.handles("") is False


def test_cached_path_refuses_to_escape_the_cache_dir(tmp_path: Path):
    pf = Prefetcher(tmp_path / "cache", "http://box:8099")
    (tmp_path / "cache").mkdir()
    (tmp_path / "segredo").write_text("nao sirva isso")
    assert pf.cached_path("../segredo") is None
    assert pf.cached_path("nao-existe.mp3") is None


def test_the_cache_name_is_stable_per_url_and_safe():
    a = _safe_name("https://example.com/a/song.mp3")
    b = _safe_name("https://example.com/a/song.mp3")
    c = _safe_name("https://example.com/b/song.mp3")
    assert a == b
    # Mesmo basename, URLs diferentes: nao podem colidir, senao uma faixa toca
    # no lugar da outra.
    assert a != c
    assert "/" not in a and ".." not in a
    assert a.endswith(".mp3")


@async_test()
async def test_two_callers_asking_at_once_download_once(tmp_path: Path):
    # O caso normal numa festa: duas pessoas pedem a mesma musica quase junto.
    source = await Source().start()
    try:
        pf = Prefetcher(tmp_path / "cache", "http://box:8099")
        url = f"{source.url}/audio.mp3"
        results = await asyncio.gather(pf.ensure_local(url), pf.ensure_local(url))
        assert source.hits == 1
        assert results[0].filename == results[1].filename
    finally:
        await source.stop()
