# The musicbox server itself: a small FastAPI app that talks to Music Assistant
# over its websocket API and exposes the event-shaped endpoints the rest of the
# installation calls.
#
# Deliberately tiny dependency set. Every package below was checked against
# cache.nixos.org for aarch64-linux before being added, because this thing gets
# built on a Raspberry Pi 5 off an SD card: one uncached Python package with a
# C extension is an hour we do not have on hackathon morning. fastapi, uvicorn
# and aiohttp are all cached, as are websockets and orjson if a later version
# wants them.
#
# `music-assistant-client` is cached too and is NOT used: musicbox speaks the
# MA websocket protocol itself (musicbox/ma_client.py) rather than pulling in a
# client whose version has to be kept in step with whatever container tag the
# Pi happens to have. That also means the `websockets` library is unused:
# aiohttp's ClientSession carries the socket, which is the same transport
# fastapi and uvicorn are already dragging in, so it costs nothing extra.
{ lib
, python3Packages
}:

python3Packages.buildPythonApplication {
  pname = "musicbox";
  version = "0.1.0";

  # `pyproject = true` is mandatory now, not stylistic: nixpkgs'
  # mk-python-derivation throws if a derivation sets neither `format` nor
  # `pyproject`. This is the modern spelling of the old `format = "pyproject"`.
  pyproject = true;

  # Filtered rather than `./..` so that editing this file, the module, or the
  # docs does not invalidate the app build. cleanSourceFilter already drops
  # .git, editor droppings and nix build results; the rest is ours.
  #
  # The build/dist/egg-info exclusions are not tidiness. A local `pip install
  # -e .` or `python -m build` leaves `musicbox.egg-info/SOURCES.txt` behind,
  # setuptools reuses it instead of rescanning, and the wheel then ships
  # whatever the developer's tree happened to look like at that moment. That
  # makes the Nix build depend on untracked local state, which is the one
  # property it exists to not have.
  src = lib.cleanSourceWith {
    name = "musicbox-src";
    src = ../.;
    filter = path: type:
      let base = baseNameOf (toString path);
      in lib.cleanSourceFilter path type
        && !(type == "directory" && builtins.elem base [
          "nix"
          "examples"
          ".github"
          "build"
          "dist"
          "__pycache__"
          ".pytest_cache"
          ".venv"
          "venv"
        ])
        && !(lib.hasSuffix ".egg-info" base)
        && base != "flake.nix"
        && base != "flake.lock"
        && base != ".gitignore";
  };

  # Matches `[build-system] build-backend = "setuptools.build_meta"` in
  # ../pyproject.toml. If that ever moves to hatchling, add hatchling here; the
  # failure is a loud "No module named hatchling" at the pypaBuildPhase, not
  # anything subtle.
  build-system = with python3Packages; [
    setuptools
    wheel
  ];

  # Must stay in step with [project].dependencies in ../pyproject.toml. They are
  # two lists in two languages describing one fact, and nixpkgs' runtime deps
  # check is what catches them drifting apart.
  dependencies = with python3Packages; [
    fastapi
    uvicorn
    aiohttp
    # The MCP server. python3Packages.mcp is the official SDK and ships
    # FastMCP, so nothing here needs the separate `fastmcp` package. Checked
    # against cache.nixos.org for aarch64-linux like everything else in this
    # list: it is pure Python on top of pydantic, httpx, starlette and anyio,
    # all of which fastapi already drags in.
    mcp
  ];

  # nixpkgs' runtime-deps hook fails the build when a pyproject pin falls
  # outside what nixpkgs ships, and nixpkgs moves faster than any pin we would
  # write. Relaxing is the right call for an app we build from source against
  # exactly one package set: the version we get is the version we tested.
  pythonRelaxDeps = true;

  # Catches a renamed module or a broken import at build time on the build
  # machine, instead of as a crash-looping unit on the Pi. mcp_server is listed
  # separately because musicbox/__init__.py does not import it, so a missing
  # `mcp` in the list above would sail through a check of "musicbox" alone and
  # only surface as a failed start.
  pythonImportsCheck = [ "musicbox" "musicbox.mcp_server" ];

  # No test suite wired into the build. pytest lives in the devShell instead:
  # running tests here would mean the Pi runs them too on every rebuild, and
  # they want a live MA to be worth anything.
  doCheck = false;

  meta = {
    description = "Event-shaped HTTP API in front of Music Assistant";
    homepage = "https://github.com/andreabadesso/musicbox";
    license = lib.licenses.mit;
    # unix, not linux: the app half is pure Python and builds and runs on the
    # Mac for development. Everything Linux-only (bluez-alsa, snapcast, podman)
    # lives in nix/module.nix, which is never evaluated on darwin.
    platforms = lib.platforms.unix;
    mainProgram = "musicbox-server";
  };
}
