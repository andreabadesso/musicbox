# musicbox: event-shaped HTTP API in front of Music Assistant, plus the audio
# chain that actually gets sound out of the box.
#
# The whole stack, top to bottom:
#
#   your system  ->  musicbox (:8099)  ->  Music Assistant (:8095, podman)
#                                              |
#                                              +-- MA's built-in snapserver (:1704)
#                                                       |
#                                                  snapclient (host, native)
#                                                       |
#                                                  bluez-alsa (ALSA A2DP)
#                                                       |
#                                                  Bluetooth speaker
#
# Music Assistant never touches audio hardware. It produces a PCM stream, hands
# it to its own snapserver, and snapclient on the host is the only thing that
# opens an ALSA device. That split is what lets MA stay in a container with no
# device passthrough at all.
#
# ── Why Music Assistant in podman, and not native ───────────────────────────
# Because on this host the native path is measured in hours. The Pi 5 runs the
# raspberry-pi-nix overlay, which replaces libcamera and libpisp; that forces a
# rebuild of pipewire, and pipewire is a build input of half the Linux audio
# world. On this host's overlaid package set, `pipewire`, `wireplumber`, `mpv`,
# `mopidy`, `ffmpeg`, `pulseaudioFull` and `alsa-utils` are all cache MISSES.
# Every one of them is a from-source aarch64 compile on a machine that boots
# off an SD card and has already wedged itself once. `ghcr.io/music-assistant/
# server` has a linux/arm64 manifest, ships its own ffmpeg and its own
# snapserver, and pulls in about a minute. This is not the elegant choice, it
# is the one that is running before lunch.
#
# ── Why bluez-alsa, and not pipewire or pulseaudio ──────────────────────────
# Same reason, from the other direction: `bluez-alsa` IS in the binary cache
# here, and `pulseaudioFull` is NOT. bluez-alsa is also simply less machine:
# one daemon that registers an A2DP media endpoint with bluetoothd and exposes
# it as an ALSA PCM. snapclient opens that PCM directly. No session bus, no
# per-user daemon, no policy engine, nothing that needs a logged-in user on a
# headless box.
#
# ── Why snapcast, and not squeezelite or plain mpd ──────────────────────────
# Two things this project needs and the alternatives do not give:
#   1. MA's Snapcast player provider advertises PlayerFeature.PLAY_ANNOUNCEMENT,
#      so `POST /drop` takes MA's *native* announcement path instead of the
#      generic stop-play-restore fallback. That is what makes a sound effect
#      land in a fraction of a second instead of after a full queue teardown.
#   2. A snapserver stream keeps flowing while the queue is idle. That matters
#      more than it sounds: snapclient hard-closes the ALSA device after 5000ms
#      with no chunks (`No chunk received for 5000ms. Closing ALSA.` in
#      client/player/alsa_player.cpp, and the 5000 is a compile-time constant).
#      Closing the PCM tears down the A2DP transport, the speaker sees no
#      stream, and it powers its amplifier down. The next event then loses its
#      first second or two while the speaker wakes up. See the --keep-alive
#      note on bluealsa below, which is the belt to snapcast's braces.
#
# ── Bootstrap (imperative, once, on the box) ────────────────────────────────
#
# 1. Pair the speaker. Put it in pairing mode FIRST; the advertising window is
#    often only 60 seconds.
#
#      sudo bluetoothctl
#        power on
#        agent on
#        default-agent          # headless boxes run no agent; bluetoothctl
#                               # provides one only for the life of the session,
#                               # and without it pairing fails with
#                               # AuthenticationCanceled
#        scan on                # wait for the speaker's name, note the MAC
#        scan off               # leave it on and the scan competes with the
#                               # connect on the same radio; pairing goes flaky
#        pair AA:BB:CC:DD:EE:FF
#        trust AA:BB:CC:DD:EE:FF
#        connect AA:BB:CC:DD:EE:FF
#        info AA:BB:CC:DD:EE:FF   # want Paired: yes, Trusted: yes,
#                                 # Connected: yes, and a UUID line reading
#                                 # "Audio Sink (0000110b-...)"
#        quit
#
#    `trust` is the step everyone skips and it is not optional. Pairing alone
#    covers the case where the Pi initiates. When the SPEAKER powers back on
#    and initiates, bluetoothd asks an agent to authorize the incoming A2DP
#    service; on a headless box there is no agent, the request is refused, and
#    the link silently never returns. Trust marks it pre-authorized.
#
#    The pairing keys live in /var/lib/bluetooth/<adapter>/<device>/. That is
#    imperative state: it survives rebuilds and reboots but NOT a reimage.
#    Back that directory up before the event.
#
# 2. Set `services.musicbox.bluetoothAudio.speakerMac` to the MAC and rebuild.
#
# 3. Prove the audio chain before involving the network chain:
#
#      PLUGIN_DIR=$(systemctl show snapclient -p Environment --value \
#        | tr ' ' '\n' | sed -n 's/^ALSA_PLUGIN_DIR=//p')
#      sudo env ALSA_PLUGIN_DIR="$PLUGIN_DIR" \
#        mpg123 -a 'bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp' /some/test.mp3
#
#    Read the plugin dir off the running unit, not from `nix eval nixpkgs#...`:
#    the registry's bluez-alsa is a different store path from the one this
#    system was built with, so that would test a plugin nothing is using.
#
#    (mpg123 is cached on this host; alsa-utils is not, which is why this is
#    not `aplay`.) Run it as root or as a member of group `audio`; see the
#    D-Bus note on the snapclient unit.
#
# 4. Create the Music Assistant admin user and mint musicbox's long-lived
#    token. A fresh MA container has NO users: its websocket closes with
#    503 "Setup required" and POST /api returns 503 until one exists. So this
#    cannot be skipped, and the box cannot come up fully unattended. Without
#    this token musicbox comes up, connects, and every command it sends comes
#    back error_code 20; /health then reads ma_connected: true with
#    ma_authenticated: false, which is the signature of exactly this step
#    having been skipped.
#
#      sudo install -d -m 0700 /etc/musicbox
#
#      curl -s -X POST http://127.0.0.1:8095/setup \
#        -H 'content-type: application/json' \
#        -d '{"username":"admin","password":"<pick one>"}'
#      # -> {"success":true,"token":"<short-lived jwt>", ...}
#
#      # Trade the short-lived token for a 365-day one. Short-lived tokens are
#      # 30 days sliding with a hard 90 day cap, which is a bad thing to
#      # discover on a Monday.
#      curl -s -X POST http://127.0.0.1:8095/api \
#        -H "authorization: Bearer <short-lived jwt>" \
#        -H 'content-type: application/json' \
#        -d '{"command":"auth/token/create","args":{"name":"musicbox"}}'
#      # -> the raw JWT string, quoted
#
#      ( umask 077; printf '%s' '<the long-lived jwt>' > /etc/musicbox/ma-token )
#
#    Then set `services.musicbox.maTokenFile = "/etc/musicbox/ma-token"` and
#    rebuild. /etc/musicbox rather than /var/lib/musicbox on purpose: the
#    service's state directory is a DynamicUser StateDirectory, so it really
#    lives at /var/lib/private/musicbox with /var/lib/musicbox as a symlink
#    systemd creates on first start. Writing secrets into a path that does not
#    exist yet, and that systemd may later migrate and chown, is a needless way
#    to lose a token. /etc is plain, root-owned, and there before first boot.
#
#    Then add the Spotify provider in MA's own UI at http://<host>:8095. It
#    needs Premium and it refuses a lot of accounts created after 2024;
#    musicbox is expected to degrade to plain http(s) URLs if it fails.
#
# 5. If musicbox itself should require a bearer token, generate that separately
#    and point `tokenFile` at it. Two different tokens: this one is the front
#    door, the one above is what we present to MA.
#
#      ( umask 077; head -c 24 /dev/urandom | base64 | tr -d '\n' \
#          > /etc/musicbox/token )
#
# 6. Drop sound effects in. The service runs under DynamicUser, so its
#    StateDirectory really lives at /var/lib/private/musicbox and
#    /var/lib/musicbox is a symlink to it. /var/lib/private is 0700 root, so
#    the copy has to go through root even though the sfx dir itself is
#    world-readable:
#
#      sudo cp airhorn.mp3 /var/lib/musicbox/sfx/
#      curl http://127.0.0.1:8099/sfx     # confirm it is listed
#
# ── A trap worth naming: MA fetches your sfx URL itself ─────────────────────
# `players/cmd/play_announcement` refuses anything that is not http(s) (there
# is a literal `if not url.startswith("http")` in MA's player controller), so a
# path under MUSICBOX_SFX_DIR can never be handed to MA directly. musicbox has
# to serve the file over HTTP and pass its own URL, and that URL is resolved
# from inside MA. Running the MA container on the host network namespace (see
# below) is what makes `http://127.0.0.1:8099/...` mean the same thing to MA
# and to musicbox. With a bridge network it would mean the container itself,
# and the announcement would 404 in a way that looks like a musicbox bug.
#
# This unit therefore sets MUSICBOX_SFX_BASE_URL explicitly rather than letting
# musicbox guess from the request's Host header. The guess is a reasonable
# fallback but a bad default here: a caller reaching the box as
# `http://pi5:8099` would hand MA the bare name `pi5`, and MA resolves names
# through the CONTAINER's /etc/resolv.conf, not through your shell's. Loopback
# needs no resolver at all and cannot be steered by a forged Host header.
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.musicbox;

  inherit (lib) mkIf mkOption mkEnableOption mkDefault types;

  # ── snapcast, minus the reason it is uncached ────────────────────────────────
  # nixpkgs defaults `pipewireSupport ? stdenv.hostPlatform.isLinux`, so a plain
  # `pkgs.snapcast` build-depends on pipewire. On the Pi that is fatal: the
  # raspberry-pi-nix libcamera/libpisp overlay forces pipewire to rebuild from
  # source, and snapcast inherits the whole multi-hour tree. Turning both audio
  # server backends off leaves only cached dependencies (alsa-lib, boost, asio,
  # avahi, flac, opus, soxr, openssl) and reduces this to one modest C++
  # compile, which the Pi 5 does in a few minutes.
  #
  # Nothing in this chain wants pipewire or pulse anyway: snapclient talks
  # straight to ALSA, and ALSA reaches the speaker through bluez-alsa.
  #
  # The override changes snapcast's own derivation hash, so snapcast itself is
  # always a local build here regardless. Build it deliberately, ahead of time.
  snapcast = pkgs.snapcast.override {
    pipewireSupport = false;
    pulseaudioSupport = false;
  };

  # The full parameter form, not the positional one. Positional
  # (`bluealsa:AA:BB:...,a2dp`) works but is order sensitive.
  #
  # DEV is pinned on purpose. Its default is 00:00:00:00:00:00, which means
  # "whatever connected most recently". On a box with phones in the room, that
  # is a great way to play the demo into a stranger's headphones.
  #
  # The shipped definition is a `type plug` wrapping `type bluealsa`, so rate
  # and format conversion to whatever A2DP negotiated is handled for us and
  # snapclient can be left at the server's native 48000:16:2.
  bluealsaPcm = "bluealsa:DEV=${cfg.bluetoothAudio.speakerMac},PROFILE=a2dp";

  # DynamicUser + StateDirectory is the whole ownership story for the sfx dir:
  # systemd creates it, chowns it to the transient uid, and keeps it across
  # restarts even though the uid is allocated fresh each boot. That only works
  # for paths under /var/lib, which is why sfxDir is asserted into that shape
  # rather than quietly falling back to a tmpfiles rule that would fight the
  # dynamic uid.
  sfxStateDir = lib.removePrefix "/var/lib/" cfg.sfxDir;
  cacheStateDir = lib.removePrefix "/var/lib/" cfg.cacheDir;

  # The address MA has to use to fetch our sfx files. See the trap note at the
  # top of this file for why this is pinned rather than inferred per request.
  # It matters more than it used to: the `sfx` MCP tool has no incoming request
  # to fall back to a Host header from, so for that path this value IS the
  # whole resolution. Unset, the tool would hand MA our listen address and hope.
  # A wildcard bind covers loopback, so 127.0.0.1 is correct in that case and
  # needs no name resolution inside the container; a specific bind address has
  # to be used verbatim, because loopback would then not be listening.
  sfxHost =
    if builtins.elem cfg.host [ "0.0.0.0" "::" "" ] then "127.0.0.1"
    # An IPv6 literal has to be bracketed in a URL or the port separator is
    # ambiguous. Cheap to get right here, miserable to diagnose from MA's log.
    else if lib.hasInfix ":" cfg.host then "[${cfg.host}]"
    else cfg.host;
  sfxBaseUrl = "http://${sfxHost}:${toString cfg.port}";

  # MUSICBOX_TOKEN has to reach the process as an environment variable (frozen
  # contract), but the secret must never be in the store and should not be
  # world-readable in /proc either. LoadCredential copies the file into a
  # per-service tmpfs at $CREDENTIALS_DIRECTORY, readable only by this unit,
  # and this small wrapper moves it into the environment at exec time.
  #
  # Why not `EnvironmentFile = cfg.tokenFile`? Because that requires the file
  # to be KEY=VALUE, and every sane way of generating a token
  # (`head -c 24 /dev/urandom | base64`) produces a bare secret. Making the
  # user hand-write `MUSICBOX_TOKEN=` into a file is exactly the kind of step
  # that gets fat-fingered at 2am. LoadCredential takes the raw file.
  #
  # The Music Assistant token goes the same way. It could have been passed as
  # MUSICBOX_MA_TOKEN_FILE pointing straight at cfg.maTokenFile, but then the
  # service would need read access to a root-owned 0600 file, which under
  # DynamicUser it does not have. LoadCredential is read by PID 1 before
  # privileges are dropped, so it works with the file mode you actually want.
  startScript = pkgs.writeShellScript "musicbox-start" ''
    set -euo pipefail
    ${lib.optionalString (cfg.tokenFile != null) ''
      MUSICBOX_TOKEN="$(cat "$CREDENTIALS_DIRECTORY/token")"
      export MUSICBOX_TOKEN
    ''}
    ${lib.optionalString (cfg.maTokenFile != null) ''
      MUSICBOX_MA_TOKEN="$(cat "$CREDENTIALS_DIRECTORY/ma-token")"
      export MUSICBOX_MA_TOKEN
    ''}
    exec ${lib.getExe cfg.package}
  '';

  # Refuse to accept a secret as a nix PATH VALUE rather than a string.
  #
  # This distinction is the whole trap and it is invisible. `tokenFile =
  # "/etc/musicbox/token"` is a string and stays one. `tokenFile =
  # /etc/musicbox/token` (no quotes) or `./token` is a path value, and the
  # moment it is interpolated into the LoadCredential string nix COPIES the
  # file into the store and substitutes the store path. Both forms typecheck as
  # types.path, both look right in review, and the second one publishes the
  # token to every user on the machine with no error and no warning.
  #
  # Checking `toString` does not catch it: that yields the original path, and
  # the copy only happens at interpolation. The type of the value is the only
  # thing that tells them apart. The hasPrefix arm catches the other spelling,
  # someone writing "${./token}" and handing us the store path as a string.
  notInStore = name: value:
    {
      assertion = value == null
        || (builtins.isString value && !(lib.hasPrefix builtins.storeDir value));
      message =
        "services.musicbox.${name} must be a quoted string path, not a nix path "
        + "value (got ${builtins.typeOf value}: ${toString value}). A path value is "
        + "copied into the world-readable nix store when the unit is built, which "
        + "publishes the secret to every user on this machine. Write it as "
        + "${name} = \"/etc/musicbox/...\" and create the file imperatively with "
        + "umask 077.";
    };
in
{
  options.services.musicbox = {
    enable = mkEnableOption "musicbox, an event-shaped HTTP API in front of Music Assistant";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.musicbox;
      defaultText = lib.literalExpression "musicbox.packages.\${system}.musicbox";
      description = "The musicbox package to run.";
    };

    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
      example = "0.0.0.0";
      description = ''
        Address musicbox binds to. The default keeps it host-local. Set it to
        0.0.0.0 to reach it over the tailnet and let `openFirewall` plus
        `firewallInterfaces` be what actually limits exposure, which is the
        same shape the other services on this box use.
      '';
    };

    port = mkOption {
      type = types.port;
      default = 8099;
      description = "TCP port musicbox listens on.";
    };

    player = mkOption {
      type = types.str;
      example = "ma_musicbox";
      description = ''
        The Music Assistant player musicbox controls. Either a player id or the
        player's display name; musicbox resolves a name to an id at connect
        time.

        With the audio chain below, the value you want is `ma_musicbox`. Note
        the prefix: Music Assistant does NOT use snapclient's hostID verbatim.
        It registers the client as

          Player (type player) registered: ma_musicbox/pi5

        so the id is `ma_` plus the hostID, and the display name is the
        client's HOSTNAME, not the hostID at all. Verified against MA 2.9.13.

        Prefer the id over the name. The name is the machine's hostname and
        changes with it, while the id derives from the hostID this module pins
        (snapclient otherwise names itself after the MAC address of whatever
        interface it picks, which is worse than either).

        If you get this wrong the service starts, connects, authenticates, and
        reports `player_error: no MA player matches ...` in GET /health while
        every playback endpoint returns 503. Check /health first.

        Required. There is no sensible default, and guessing one would mean the
        service starts happily and controls nothing.
      '';
    };

    maUrl = mkOption {
      type = types.str;
      default = "http://127.0.0.1:8095";
      description = ''
        Base URL of the Music Assistant server. Plain http, and musicbox
        derives the websocket URL from it. Loopback works because the container
        runs on the host network namespace.
      '';
    };

    sfxDir = mkOption {
      type = types.str;
      default = "/var/lib/musicbox/sfx";
      description = ''
        Directory of preloaded sound effects served by `GET /sfx` and played by
        `POST /sfx/{name}`.

        Must live under /var/lib: it is created as a systemd StateDirectory,
        which is the only mechanism that gets ownership right under DynamicUser.
      '';
    };

    cacheDir = mkOption {
      type = types.str;
      default = "/var/lib/musicbox/cache";
      description = ''
        Onde o audio baixado de URLs remotas fica guardado.

        O musicbox baixa antes de tocar em vez de deixar o Music Assistant
        fazer streaming ao vivo. Isso existe por medida, nao por gosto: na rede
        do evento a banda oscilou entre 811 kB/s e 2,5 kB/s na mesma hora, e o
        MA responde "Timeout waiting for audio data" quando a origem nao
        acompanha a reproducao. Baixado uma vez, o arquivo toca perfeito pelo
        resto do dia, mesmo se a internet cair.

        Precisa ficar sob /var/lib pelo mesmo motivo do sfxDir: e um
        StateDirectory de systemd, que e o unico mecanismo que acerta a posse
        do diretorio sob DynamicUser.
      '';
    };

    prefetch = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Baixar audio remoto antes de tocar. Desligue apenas se o link for
        rapido e estavel e o espaco em disco for mais precioso que a
        confiabilidade da reproducao.
      '';
    };

    tokenFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/etc/musicbox/token";
      description = ''
        Path to a file containing musicbox's own bearer token, as raw bytes with
        no KEY=VALUE wrapper. When set, every request must carry
        `Authorization: Bearer <token>`.

        Three exceptions, all deliberate: `/health`, `/sfx/file/{name}` (Music
        Assistant fetches that URL itself and cannot present our token) and
        `/mcp`. That last one is the surprising one. The MCP server stays
        unauthenticated even with this set, because MCP clients handle custom
        headers badly, which means anyone who can reach `port` can play audio
        and change the volume with no token. `openFirewall` and
        `firewallInterfaces` are what actually limit that; do not open this
        port to anything wider than the tailnet.

        Read at runtime through systemd's LoadCredential. Give this a path
        outside the flake; a `builtins.readFile` here would put the secret in
        the world-readable nix store, which is the trap this option exists to
        avoid.
      '';
    };

    maTokenFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/etc/musicbox/ma-token";
      description = ''
        Path to a file containing the long-lived Music Assistant JWT that
        musicbox presents when it connects, as raw bytes with no KEY=VALUE
        wrapper. Mint it once with `auth/token/create`; see the bootstrap block
        at the top of this file.

        Not optional in practice, despite the null default. Music Assistant 2.9
        authenticates every command except the four auth ones, so without this
        musicbox connects successfully and then has every single command
        refused with error_code 20. That failure is quiet by design (the
        service stays up and `GET /health` reports it) which makes it very easy
        to leave unset and discover on the day: the tell is
        `ma_connected: true` together with `ma_authenticated: false`.

        The default is null rather than a path because the token is imperative
        state that has to exist before it can be referenced, and a default
        pointing at a file nobody created would fail the unit at start with a
        credential error instead of degrading.

        Read at runtime through LoadCredential, never interpolated into the
        store. This is a different token from `tokenFile`: that one is
        musicbox's own front door, this one is what musicbox presents to Music
        Assistant.
      '';
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Open `port` on `firewallInterfaces` only. Deliberately not a blanket
        `allowedTCPPorts`: musicbox can queue audio and, if MA has Spotify
        wired up, spend someone's Premium account. It has no business being
        reachable from the LAN.
      '';
    };

    firewallInterfaces = mkOption {
      type = types.listOf types.str;
      default = [ "tailscale0" ];
      description = ''
        Interfaces `openFirewall` opens the port on. Tailnet only by default,
        matching the convention every other private service on this box uses.
      '';
    };

    musicAssistant = {
      enable = mkEnableOption "the Music Assistant server container";

      image = mkOption {
        type = types.str;
        default = "ghcr.io/music-assistant/server:2.9.13";
        description = ''
          Container image, pinned by tag on purpose. `:latest` means a retag
          upstream can move the API out from under a running installation
          between one `podman pull` and the next, and MA's auth model in
          particular changed shape in 2.9. Bump this deliberately, and re-check
          `GET /api-docs/commands.json` on the box afterwards, which is the
          ground truth for argument names on whatever image actually landed.
        '';
      };

      dataDir = mkOption {
        # types.str, not types.path, and for the same reason the token files
        # are asserted against store paths: a nix path value here would be
        # copied into the store and then bind-mounted from it, which is
        # read-only. MA's first write would fail somewhere deep in its config
        # writer, and the config would look completely correct.
        type = types.str;
        default = "/var/lib/music-assistant";
        description = ''
          Host directory bind-mounted at /data in the container, as a quoted
          absolute path. Holds MA's config, its user database, the provider
          credentials and the media cache, so it must survive container
          recreation.
        '';
      };
    };

    bluetoothAudio = {
      enable = mkEnableOption "the bluez-alsa plus snapclient chain to a Bluetooth speaker";

      latencyMs = mkOption {
        type = types.int;
        default = 200;
        description = ''
          Folga de buffer do snapclient, em milissegundos.

          Nao mexa para baixo sem medir. Com 0 o audio engasga em caixa
          Bluetooth: o A2DP tem jitter proprio e o ALSA acusa
          "XRUN while waiting for PCM". Ver o comentario no ExecStart do
          snapclient para os numeros.
        '';
      };

      speakerMac = mkOption {
        type = types.str;
        default = "";
        example = "AA:BB:CC:DD:EE:FF";
        description = ''
          MAC address of the paired Bluetooth speaker. Pairing itself stays
          imperative (see the bootstrap block at the top of this file); this
          option only tells snapclient and the reconnect watchdog which device
          to use.
        '';
      };
    };
  };

  config = mkIf cfg.enable (lib.mkMerge [

    # ── musicbox itself ───────────────────────────────────────────────────────
    {
      assertions = [
        {
          assertion = cfg.player != "";
          message = "services.musicbox.player must name a Music Assistant player id or display name.";
        }
        {
          assertion = lib.hasPrefix "/var/lib/" cfg.sfxDir;
          message =
            "services.musicbox.sfxDir must live under /var/lib (got ${cfg.sfxDir}). "
            + "It is created as a systemd StateDirectory, which is what gets ownership "
            + "right under DynamicUser; anywhere else and the directory ends up owned by "
            + "a uid the service no longer has.";
        }
        {
          assertion = cfg.bluetoothAudio.enable -> cfg.bluetoothAudio.speakerMac != "";
          message = "services.musicbox.bluetoothAudio.speakerMac must be set when bluetoothAudio is enabled.";
        }
        (notInStore "tokenFile" cfg.tokenFile)
        (notInStore "maTokenFile" cfg.maTokenFile)
      ];

      warnings = lib.optional (cfg.maTokenFile == null) ''
        services.musicbox.maTokenFile is not set. Music Assistant 2.9 requires a
        token for every command, so musicbox will connect and then have every
        command refused (GET /health will show ma_connected: true with
        ma_authenticated: false). Mint a long-lived token with auth/token/create
        and point maTokenFile at it.
      '';

      systemd.services.musicbox = {
        description = "musicbox HTTP API for Music Assistant";
        wantedBy = [ "multi-user.target" ];
        after = [ "network-online.target" ]
          ++ lib.optional cfg.musicAssistant.enable "podman-music-assistant.service";
        wants = [ "network-online.target" ];

        environment = {
          MUSICBOX_HOST = cfg.host;
          MUSICBOX_PORT = toString cfg.port;
          MUSICBOX_MA_URL = cfg.maUrl;
          MUSICBOX_PLAYER = cfg.player;
          MUSICBOX_SFX_DIR = cfg.sfxDir;
          MUSICBOX_CACHE_DIR = cfg.cacheDir;
          MUSICBOX_PREFETCH = if cfg.prefetch then "1" else "0";
          MUSICBOX_SFX_BASE_URL = sfxBaseUrl;
          # Unbuffered, so a crash traceback is in the journal rather than lost
          # in a pipe buffer that never got flushed.
          PYTHONUNBUFFERED = "1";
        };

        serviceConfig = {
          Type = "simple";
          ExecStart = startScript;

          # No `-` prefix and no `Type=exec` subtleties: musicbox is expected to
          # come up even when MA is not there yet (GET /health is supposed to
          # report ma_connected: false rather than refuse to start), so a
          # restart loop here means a real bug, not a cold start.
          Restart = "on-failure";
          RestartSec = "5";

          # Transient uid allocated at start, released at stop. Nothing on this
          # box needs a persistent `musicbox` account: the service owns exactly
          # one directory and systemd carries that across the uid change.
          DynamicUser = true;
          # Os dois diretorios, e nao so o de sfx. O cache de download tambem e
          # escrito pelo servico sob DynamicUser, e sem declarar aqui ele
          # nasceria fora do alcance do uid transitorio: o download falharia com
          # permissao negada e a caixa voltaria a tocar em streaming sem que
          # ninguem entendesse por que.
          StateDirectory = [ sfxStateDir cacheStateDir ];
          # 0755 so a human can read what they dropped in. The dir sits inside
          # /var/lib/private, which is 0700 root, so this is not an exposure:
          # it just means root can `cp` files in without a chown dance.
          StateDirectoryMode = "0755";

          # Ports below 1024 need the capability; above them, nothing does.
          # Granting it unconditionally would hand a network service a
          # capability it has no use for.
          AmbientCapabilities = lib.optional (cfg.port < 1024) "CAP_NET_BIND_SERVICE";
          CapabilityBoundingSet = lib.optional (cfg.port < 1024) "CAP_NET_BIND_SERVICE";

          LoadCredential =
            lib.optional (cfg.tokenFile != null) "token:${cfg.tokenFile}"
            ++ lib.optional (cfg.maTokenFile != null) "ma-token:${cfg.maTokenFile}";

          # ── Hardening ────────────────────────────────────────────────────
          NoNewPrivileges = true;
          PrivateTmp = true;
          PrivateDevices = true;
          PrivateUsers = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          ProtectClock = true;
          ProtectHostname = true;
          ProtectKernelLogs = true;
          ProtectKernelModules = true;
          ProtectKernelTunables = true;
          ProtectControlGroups = true;
          ProtectProc = "invisible";
          ProcSubset = "pid";
          RestrictNamespaces = true;
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          LockPersonality = true;
          RemoveIPC = true;
          UMask = "0077";
          SystemCallArchitectures = "native";
          SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
          SystemCallErrorNumber = "EPERM";
          # AF_UNIX is not obviously needed but CPython opens unix sockets for
          # DNS resolution paths and socketpair(); dropping it produces a
          # failure that looks like a network outage. AF_NETLINK is here for
          # the same class of reason: glibc's getaddrinfo enumerates local
          # interfaces over netlink for AI_ADDRCONFIG, so resolving a hostname
          # in MUSICBOX_MA_URL fails without it. Loopback and a bare IP work
          # either way, which is exactly why this would be missed in testing
          # and only bite the one deployment that used a name.
          RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" "AF_NETLINK" ];

          # MemoryDenyWriteExecute is deliberately NOT set. Several cached
          # Python wheels use cffi or ctypes trampolines, which need W|X pages,
          # and the resulting crash surfaces as an opaque SIGSEGV inside libffi
          # rather than as a policy denial. Not worth the debugging time for a
          # service that is already unprivileged, namespaced and syscall
          # filtered.
        };
      };

      # NixOS merges this attribute with the other modules' entries, so opening
      # a port here does not close anyone else's.
      networking.firewall.interfaces = mkIf cfg.openFirewall
        (lib.genAttrs cfg.firewallInterfaces (_: { allowedTCPPorts = [ cfg.port ]; }));
    }

    # ── Music Assistant, in a container ───────────────────────────────────────
    (mkIf cfg.musicAssistant.enable {
      # mkDefault, not a plain assignment: podman is very likely already
      # enabled host-wide with its own dockerCompat and DNS settings, and this
      # module has no business overriding those.
      virtualisation.podman.enable = mkDefault true;
      virtualisation.oci-containers.backend = mkDefault "podman";

      virtualisation.oci-containers.containers.music-assistant = {
        image = cfg.musicAssistant.image;
        autoStart = true;

        volumes = [
          "${cfg.musicAssistant.dataDir}:/data"
        ];

        environment = {
          # MA timestamps its logs and schedules with this; without it the
          # container is UTC and every log line is an hour of mental arithmetic
          # away from the host's journal. `time.timeZone` is nullOr str and
          # genuinely defaults to null, so the fallback is not decoration.
          TZ = if config.time.timeZone == null then "UTC" else config.time.timeZone;
        };

        # ── Why host networking rather than published ports ────────────────
        # Four separate reasons, any one of which is sufficient:
        #
        # 1. Port count. MA's built-in snapserver wants 1704, 1705 AND the
        #    whole 4953-5153 range. That last one is 201 published ports, each
        #    of which podman implements with its own userspace proxy process on
        #    the rootful bridge. On a Pi that is real memory for no benefit.
        #
        # 2. mDNS. MA discovers Chromecast, AirPlay, Sonos and friends by
        #    multicast. Multicast does not cross podman's bridge, so on a
        #    published-ports setup MA simply finds nothing and the failure is
        #    silent: an empty player list that looks like a configuration
        #    mistake.
        #
        # 3. Announcement URLs. `POST /sfx/{name}` makes musicbox hand MA an
        #    http URL pointing back at musicbox, and MA fetches it itself. On a
        #    bridge network `127.0.0.1:8099` inside the container is the
        #    container, so that fetch fails. Sharing the host netns makes
        #    loopback mean the same thing on both sides, which is also why
        #    `maUrl` can default to 127.0.0.1.
        #
        # 4. snapclient runs on the host and dials 127.0.0.1:1704. With host
        #    networking snapserver is genuinely listening there. Verify with
        #    `ss -lntp | grep 1704` before blaming Bluetooth for silence.
        #
        # The cost is that MA has no network isolation from the host. Accepted:
        # it is a single-purpose box on a tailnet.
        extraOptions = [ "--network=host" ];
      };

      # The bind mount source has to exist before podman starts, or podman
      # creates it as a root-owned directory with the wrong mode and MA's first
      # run fails somewhere deep in its config writer.
      systemd.tmpfiles.rules = [
        "d ${cfg.musicAssistant.dataDir} 0750 root root -"
      ];
    })

    # ── Bluetooth speaker: bluez -> bluez-alsa -> snapclient ──────────────────
    (mkIf cfg.bluetoothAudio.enable {
      hardware.bluetooth = {
        enable = true;
        powerOnBoot = true;
        # bluetoothd's SAP (SIM Access Profile) plugin cannot initialise on a
        # Pi and retries forever, filling the journal with
        # "sap-driver: Operation not permitted". Nothing here wants SIM access,
        # and on some builds the failing plugin takes adapter setup down with
        # it.
        disabledPlugins = [ "sap" ];
        settings.General = {
          Name = "musicbox";
          # Announce ourselves as an audio device. Some speakers accept a
          # reconnect noticeably faster from a peer that looks like audio gear.
          Class = "0x00040c";
        };
        # The [Policy] defaults are already right: AutoEnable=true,
        # ReconnectAttempts=7, ReconnectIntervals=1,2,4,8,16,32,64, and
        # ReconnectUUIDs including 0000110b (Audio Sink). In particular do NOT
        # set ReconnectUUIDs to an empty list "to clean it up": that disables
        # reconnect outright.
      };

      # bluez-alsa ships three things that each have to be pulled in by hand.
      # The D-Bus policy is the one that gets forgotten, and its failure mode is
      # deceptive: the packaged unit is Type=dbus with BusName=org.bluealsa, so
      # with no policy it never acquires the name and systemd reports a start
      # TIMEOUT rather than an error. It reads as a slow boot, not a broken
      # configuration. Check with `busctl --system list | grep bluealsa`.
      systemd.packages = [ pkgs.bluez-alsa ];
      services.dbus.packages = [ pkgs.bluez-alsa ];

      # mpg123 and sox are here as the cached debugging kit. alsa-utils is NOT
      # in the binary cache on this host, so `aplay`/`amixer` are off the table;
      # `bluealsa-aplay -L` covers PCM enumeration and mpg123 covers playback.
      environment.systemPackages = [ pkgs.bluez pkgs.bluez-alsa pkgs.mpg123 snapcast ];

      # The packaged unit is compiled against this exact user name
      # (--with-bluealsauser=bluealsa) and its D-Bus policy is written for it.
      users.users.bluealsa = {
        description = "BlueALSA daemon";
        isSystemUser = true;
        group = "audio";
      };

      # alsa-lib's stock alsa.conf already includes /etc/alsa/conf.d with
      # errors=false, so dropping the file here is the whole job. No
      # /etc/asound.conf and no ALSA_CONFIG_PATH are needed, and setting
      # ALSA_CONFIG_PATH would actively break things, because it REPLACES the
      # top-level alsa.conf rather than adding to it.
      #
      # Note that this repo deliberately does not use nixpkgs' `hardware.alsa`
      # module, which would do the same five lines: its non-bluetooth branch
      # unconditionally adds pkgs.alsa-utils to systemPackages and there is no
      # way to get the bluetooth half without it. That is an uncached source
      # build for tooling this box does not use.
      environment.etc."alsa/conf.d/20-bluealsa.conf".source =
        "${pkgs.bluez-alsa}/etc/alsa/conf.d/20-bluealsa.conf";

      systemd.services.bluealsa = {
        wantedBy = [ "bluetooth.target" ];
        after = [ "bluetooth.service" ];
        # A bluetoothd restart drops every registered media endpoint, and
        # bluealsa does not re-register on its own, so audio then stays dead
        # until reboot. The packaged unit only has After=, which orders startup
        # and propagates nothing. partOf makes systemd cycle us whenever
        # bluetoothd cycles.
        partOf = [ "bluetooth.service" ];

        serviceConfig = {
          # bluealsa keeps per device state, most usefully the last volume, in a
          # storage directory, and the packaged unit declares nowhere for it to
          # live. Without this it logs
          #   bluealsa: W: Couldn't create storage directory: No such file or directory
          # on every start and silently forgets the speaker's volume across
          # restarts, so the first track after a reboot comes back at whatever
          # the transport negotiates instead of where you left it. Observed on a
          # pi5 on 2026-08-15. Harmless enough to ship unnoticed, which is
          # exactly why it is worth naming.
          StateDirectory = "bluealsa";

          # systemd needs the empty string first to clear the packaged
          # ExecStart before a new one can be set.
          ExecStart = [
            ""
            (lib.escapeShellArgs [
              "${pkgs.bluez-alsa}/bin/bluealsa"
              # The packaged default passes -S, which sends logs to syslog.
              # Dropping it leaves them on stderr where `journalctl -u bluealsa`
              # can see them, which is where you will be looking.
              #
              # a2dp-source ONLY. The packaged default also registers
              # a2dp-sink, and the direction is the thing people get backwards:
              # source = we stream TO the speaker, sink = we BECOME a speaker.
              # Registering the sink endpoint invites any phone in range to
              # pair and take the box over. Leaving HSP/HFP unregistered is
              # also deliberate: if no headset endpoint exists, the speaker
              # cannot negotiate it, and HSP is 8 kHz mono and sounds broken.
              "-p"
              "a2dp-source"
              # The highest-value flag in this entire file. snapclient closes
              # the ALSA device after 5000ms without a chunk; that close tears
              # down the A2DP transport and the speaker powers its amp down,
              # eating the start of whatever plays next. --keep-alive holds the
              # transport open for that many seconds after the PCM closes. The
              # value is parsed as float seconds, so a day is fine and
              # effectively means "never let go".
              "--keep-alive=86400"
              # snapclient does its own software attenuation; start the
              # transport at unity so the two do not stack and throw away bits.
              "--initial-volume=100"
            ])
          ];
          # Not on-failure. bluealsa exiting because bluetoothd was not ready
          # yet is a normal cold-boot race, not a fault.
          Restart = "always";
          RestartSec = "2s";
        };

        # Same reasoning as snapclient's: a cold-boot race or a speaker power
        # cycle can produce a burst of restarts, and the default start limit
        # (5 in 10s) would then wedge the unit permanently. On a headless box
        # a wedged audio daemon needs a human, which is the one thing that is
        # not available mid-event.
        unitConfig.StartLimitIntervalSec = 0;
      };

      # ── Keep the speaker connected ──────────────────────────────────────────
      # BlueZ's own [Policy] ReconnectAttempts only fires on link LOSS, i.e. a
      # supervision timeout. A speaker that is switched off cleanly sends a
      # proper disconnect, which that policy does not retry at all, and even
      # for link loss it gives up after roughly two minutes. So after someone
      # power-cycles the speaker mid-event, nothing on the box is trying any
      # more. This loop is crude and it is correct.
      systemd.services.musicbox-bt-connect = {
        description = "Keep the Bluetooth speaker connected";
        wantedBy = [ "multi-user.target" ];
        after = [ "bluetooth.target" "bluealsa.service" ];
        wants = [ "bluetooth.target" ];
        # mkForce is required, not defensive: the systemd module always defines
        # PATH from the default unit dependencies, so a plain assignment is a
        # conflicting definition and evaluation fails.
        path = lib.mkForce [ pkgs.bluez pkgs.gnugrep pkgs.coreutils ];
        script = ''
          while true; do
            if ! bluetoothctl info ${cfg.bluetoothAudio.speakerMac} | grep -q "Connected: yes"; then
              bluetoothctl connect ${cfg.bluetoothAudio.speakerMac} || true
            fi
            sleep 15
          done
        '';
        serviceConfig = {
          Restart = "always";
          RestartSec = "10s";
        };
        # A long-running loop keeps its own state and cannot pile up. A
        # systemd timer doing the same poll can overlap with itself while
        # bluetoothctl is blocked on a connect attempt.
      };

      users.groups.snapclient = { };
      users.users.snapclient = {
        isSystemUser = true;
        group = "snapclient";
        # bluez-alsa's D-Bus policy opens org.bluealsa to user `bluealsa` and
        # to group `audio`, and there is NO root exemption. Anything that opens
        # a bluealsa PCM must be in `audio` or the open fails with a D-Bus
        # denial that ALSA surfaces as a generic device error, which reads as
        # "speaker not connected".
        extraGroups = [ "audio" ];
      };

      systemd.services.snapclient = {
        description = "Snapcast client feeding the Bluetooth speaker";
        wantedBy = [ "multi-user.target" ];
        after = [ "bluealsa.service" "musicbox-bt-connect.service" ]
          ++ lib.optional cfg.musicAssistant.enable "podman-music-assistant.service";
        wants = [ "bluealsa.service" "musicbox-bt-connect.service" ];
        # An open PCM does not survive a bluealsa restart; cycle with it.
        partOf = [ "bluealsa.service" ];

        environment = {
          # alsa-lib resolves `type bluealsa` by dlopening
          # libasound_module_pcm_bluealsa.so out of $ALSA_PLUGIN_DIR, falling
          # back to the directory compiled into alsa-lib, which is alsa-lib's
          # OWN store path and can never contain bluez-alsa's plugin. Without
          # this the PCM is reported as simply unknown, which looks exactly
          # like a missing config file and sends you debugging the wrong thing.
          # Invisible on a normal distro where everything lands in
          # /usr/lib/alsa-lib, which is why no tutorial mentions it.
          ALSA_PLUGIN_DIR = "${pkgs.bluez-alsa}/lib/alsa-lib";
        };

        serviceConfig = {
          Type = "simple";
          ExecStart = lib.escapeShellArgs [
            "${snapcast}/bin/snapclient"
            # Positional URL, not -h/--host: those are deprecated as of 0.35.
            # Passing it explicitly also avoids snapclient falling back to mDNS
            # discovery (tcp://_snapcast._tcp) for a server on loopback, which
            # would make this depend on avahi for no reason.
            "tcp://127.0.0.1:1704"
            # Explicit even though it is the default, so that a future default
            # change fails loudly instead of quietly selecting pipewire.
            "--player"
            "alsa"
            # Raw ALSA PCM name, handed straight to snd_pcm_open.
            "--soundcard"
            bluealsaPcm
            # Mandatory with bluez-alsa. The hardware mixer path goes through
            # bluealsa's ctl plugin and is broken in this combination
            # (snapcast issue #1317). `software` is the current default; pinned
            # so a default change cannot break the box mid-event.
            "--mixer"
            "software"
            # 200ms de folga, e nao zero.
            #
            # O valor era 0, justificado com "cliente unico, nada para
            # sincronizar". Isso estava errado, e o erro so aparece com uma
            # caixa Bluetooth real tocando: o A2DP tem jitter proprio, e sem
            # folga o snapclient esvazia o buffer e o ALSA responde
            #
            #   [Error] (Alsa) XRUN while waiting for PCM: Broken pipe
            #
            # que sai como engasgo no som. Medido em 2026-08-17, numa janela de
            # 2 minutos com musica tocando: 15 XRUNs com latency 0 (e o WiFi ja
            # desligado, entao nao era interferencia), 0 com 200. Duzentos
            # milissegundos sao imperceptiveis para musica ambiente e absorvem
            # a variacao do link.
            "--latency"
            (toString cfg.bluetoothAudio.latencyMs)
            # Default is the MAC address of whichever interface snapclient
            # picks, which means the player's name in MA changes if the network
            # hardware does, and MUSICBOX_PLAYER points at that name. Pin it.
            "--hostID"
            "musicbox"
            "--logsink"
            "stdout"
          ];
          User = "snapclient";
          Group = "snapclient";
          SupplementaryGroups = [ "audio" ];

          # `always`, not `on-failure`. snapclient exits non-zero when
          # initAlsa() throws, and that is the NORMAL state during the window
          # between boot and the speaker reconnecting: there is no A2DP
          # transport to open yet. With a 5s backoff this becomes a retry loop
          # instead of a dead unit that needs a human.
          Restart = "always";
          RestartSec = "5s";
        };

        # A speaker power cycle produces a burst of failed starts. systemd's
        # default start limit (5 starts in 10s) would trip and wedge the unit
        # permanently, which is the exact opposite of what a headless box
        # wants. StartLimitIntervalSec belongs in [Unit], hence unitConfig.
        unitConfig.StartLimitIntervalSec = 0;
      };

      # ── Radio coexistence ───────────────────────────────────────────────────
      # The Pi 5's WiFi and Bluetooth share the 2.4 GHz front end, and A2DP
      # stutters under WiFi load. This box is on ethernet, so blocking the WiFi
      # radio costs nothing and is the single cheapest stability win available.
      # Not made an option because the frozen interface contract does not have
      # one; run it by hand or add a local oneshot:
      #   systemd.services.rfkill-wifi = { ... ExecStart = "rfkill block wifi"; };
    })
  ]);
}
