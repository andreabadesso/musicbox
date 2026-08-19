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
# ── And with services.musicbox.mixer.enable, one link changes ───────────────
#
#   ... MA's snapserver -> snapclient --player file:filename=<fifo>
#                                        |
#                                   musicbox-mixer  <- effects arrive here, over
#                                        |             a unix socket from musicbox
#                                   bluez-alsa -> Bluetooth speaker
#
# snapclient stops opening the ALSA device and becomes a producer of raw PCM;
# musicbox-mixer owns the speaker and sums sound effects on top of the music,
# ducking the music while they play. That is the only place a second sound can
# be added on this box: A2DP is one exclusive stream and bluez-alsa will not mix
# it, and Music Assistant's announcement path silences the music instead of
# layering, and serialises announcements on top of that (measured: four presses
# 0.35s apart came out about 3s apart).
#
# It is OFF by default and everything below assumes it stays that way until
# someone turns it on deliberately, having listened to it first.
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

  # ── The mixer's paths and package ───────────────────────────────────────────
  # One runtime directory holds both the FIFO snapclient writes into and the
  # control socket musicbox talks to. systemd creates it at start and removes it
  # at stop, which is exactly the lifetime both files want: a FIFO surviving the
  # process that owned it is how you get snapclient writing into a pipe nobody
  # is draining.
  #
  # It is owned by the mixer's user with group `audio` and mode 0750, so the two
  # processes that have business here (snapclient, which is in `audio`, and
  # musicbox, which this module puts in `audio` when the mixer is on) can
  # traverse it and nothing else can.
  mixerRuntimeDir = "/run/musicbox-mixer";

  # Where the mixer sees the sound effects. NOT cfg.sfxDir, and that difference
  # is the whole reason this exists.
  #
  # musicbox runs under DynamicUser, so its StateDirectory really lives at
  # /var/lib/private/musicbox and /var/lib/musicbox is a symlink systemd makes.
  # /var/lib/private is mode 0700 root, with no group and no exception, so ANY
  # other user opening /var/lib/musicbox/sfx is denied at the traversal, not at
  # the file. Mounting the directory somewhere that does not go through
  # /var/lib/private is the only fix that does not involve taking musicbox off
  # DynamicUser and migrating its state on a box that is mid-event.
  #
  # PID 1 does the mount as root before dropping privileges, so the 0700 is not
  # in the way there.
  mixerSfxDir = "${mixerRuntimeDir}/sfx";
  mixerSfxSource = "/var/lib/private/${sfxStateDir}";

  # The mixer opens the ALSA device that snapclient used to open, so by default
  # it is the same device string, built the same way.
  mixerDevice =
    if cfg.mixer.device != null then cfg.mixer.device else bluealsaPcm;

  # numpy and pyalsaaudio are an opt-in of the package build (nix/package.nix,
  # `withMixer`) rather than an unconditional dependency, so that a deployment
  # with the mixer off keeps byte for byte the closure it has today. Flipping it
  # here means nobody has to know the argument exists.
  #
  # The `? override` guard is for the case where someone points `package` at a
  # derivation that did not come from callPackage. Rather than fail evaluation
  # for a case that may well be deliberate (a locally patched build that already
  # has both libraries), fall through and warn: the failure then is a loud
  # ImportError on the mixer's first start, not a broken rebuild.
  mixerPackage =
    if cfg.package ? override
    then cfg.package.override { withMixer = true; }
    else cfg.package;

  # Runs as the mixer's own user inside its RuntimeDirectory, before the mixer
  # itself. Two jobs, and the first one is the important one.
  #
  # snapclient's file player opens its target with fopen(path, "wb"), which is
  # O_WRONLY|O_CREAT|O_TRUNC. If the path is missing when snapclient gets there,
  # it does not fail: it CREATES A REGULAR FILE and writes 9600 bytes into it
  # every 50 ms, forever, into a tmpfs, with the unit green and the room silent.
  # Creating the FIFO from the unit rather than from the application closes that
  # window completely, because the node exists as a FIFO before snapclient is
  # allowed to start at all.
  #
  # `mkfifo -m` applies the mode as chmod does, so the umask is not involved and
  # the 0660 is exact: owner is the mixer, group is `audio`, and snapclient is
  # in `audio`. That is what lets snapclient open it for writing.
  #
  # AN EXISTING FIFO IS LEFT ALONE, which together with
  # RuntimeDirectoryPreserve = "restart" below is what makes a mixer restart
  # survivable for snapclient. snapclient holds a file descriptor on this
  # inode; unlinking it and making a new node does not close that descriptor,
  # it orphans it, and snapclient's file player has no error handling on its
  # fwrite. It would then write 9600 bytes every 50 ms into a deleted inode
  # forever, with its unit active and the room silent. Keeping the same node
  # means the mixer can crash and come back and snapclient never notices more
  # than a gap.
  #
  # Second job: clear a stale socket. That one IS unlinked every time, because
  # a socket left behind by a crash makes bind() fail with EADDRINUSE, and a
  # mixer that will not start is a mixer that hangs snapclient.
  #
  # Third job: make sure the sfx mount point exists even when the mount was
  # skipped, which happens on a box where musicbox has never started and so has
  # no state directory yet. That turns "the effects directory is missing" into
  # "the effects directory is empty", and the difference matters more than it
  # looks: a mixer that crash-loops takes the FIFO's only reader with it, and
  # snapclient then blocks in fwrite and stops reading the network too. A mixer
  # that comes up with no effects still passes the music through.
  mixerPrepare = pkgs.writeShellScript "musicbox-mixer-prepare" ''
    set -euo pipefail
    ${pkgs.coreutils}/bin/rm -f ${lib.escapeShellArg cfg.mixer.socket}
    if [ ! -p ${lib.escapeShellArg cfg.mixer.fifo} ]; then
      # Not a fifo: either missing, or a regular file something created by
      # opening the path for writing before we got here. Both have to become a
      # fifo, and the second one is exactly the failure this whole script
      # exists to prevent, so it is loud.
      if [ -e ${lib.escapeShellArg cfg.mixer.fifo} ]; then
        echo "musicbox-mixer: ${cfg.mixer.fifo} existed and was not a fifo, replacing it" >&2
      fi
      ${pkgs.coreutils}/bin/rm -f ${lib.escapeShellArg cfg.mixer.fifo}
      ${pkgs.coreutils}/bin/mkfifo -m 0660 ${lib.escapeShellArg cfg.mixer.fifo}
    else
      # Preserved across a restart. Re-assert the mode rather than trust it.
      ${pkgs.coreutils}/bin/chmod 0660 ${lib.escapeShellArg cfg.mixer.fifo}
    fi
    ${pkgs.coreutils}/bin/mkdir -p ${lib.escapeShellArg mixerSfxDir}
  '';

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

      alsaBufferMs = mkOption {
        type = types.int;
        default = 200;
        description = ''
          Tamanho do buffer do dispositivo ALSA, em milissegundos, passado ao
          snapclient como `--player alsa:buffer_time=`.

          Este e o buffer que de fato sofre underrun, e o default do snapclient
          e 80ms. Num link A2DP com a sala cheia isso e pouco: 484 XRUNs em 5
          minutos.

          Mas existe teto, e ele morde de um jeito pior que o underrun. Com
          500ms o snapclient tenta encher o buffer inteiro de uma vez e pede
          ~118ms de audio, enquanto o snapserver do Music Assistant entrega em
          pedacos de 26ms. Ele nunca monta um periodo, e o resultado e

            Exception: Not enough frames available, requested frames: 5677,
            available: 1152
            Failed to get chunk

          repetido para sempre, com o Music Assistant reportando "playing" e a
          caixa em silencio. Silencio com tudo verde e pior que engasgo.
          Aumentar o numero de fragmentos NAO resolve: o tamanho do pedido vem
          do espaco vazio no buffer, nao do periodo.

          200ms e onde os dois convivem: grande o bastante para o jitter do
          A2DP, pequeno o bastante para o stream conseguir preencher. Medido em
          2026-08-18: 0 XRUNs e 0 falhas de chunk numa janela de 5 minutos.

          Nao confunda com `latencyMs`: aquele desloca o instante da
          reproducao para sincronizar clientes, este dimensiona o buffer. Foram
          horas ate essa distincao ficar clara, porque subir o latencyMs
          "ajudava" o suficiente para parecer a solucao.
        '';
      };

      alsaFragments = mkOption {
        type = types.int;
        default = 8;
        description = ''
          Numero de fragmentos do buffer ALSA. O default do snapclient e 4.
          Mais fragmentos significam despertares mais frequentes e menores, o
          que ajuda quando a fonte entrega em rajadas, que e o caso do A2DP.
        '';
      };

      latencyMs = mkOption {
        type = types.int;
        default = 500;
        description = ''
          Folga de buffer do snapclient, em milissegundos.

          Nao mexa para baixo sem medir. Com 0 o audio engasga em caixa
          Bluetooth: o A2DP tem jitter proprio e o ALSA acusa
          "XRUN while waiting for PCM". Numa sala cheia ate 200 foi pouco.
          Ver o comentario no ExecStart do snapclient para os numeros.
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

    # ── The software mixer ────────────────────────────────────────────────────
    # Everything here is off by default and has to stay that way. The box this
    # runs on is playing music to a room; the mixer takes over the one process
    # that owns the ALSA device, so switching it on is a change to the audio
    # path itself, not a feature flag on top of it.
    mixer = {
      enable = mkEnableOption ''
        the software mixer, which layers sound effects over the music instead of
        interrupting it.

        What it changes: snapclient stops opening the ALSA device and writes raw
        PCM into a FIFO instead (`--player file:filename=`), and a new
        musicbox-mixer service reads that FIFO, mixes effect voices on top of
        the music, ducks the music while a voice plays, and writes the result to
        the Bluetooth speaker. musicbox then triggers effects over a unix socket
        rather than through Music Assistant's announcement path.

        Why it exists: MA's announcement path silences the music, plays the
        effect and resumes, and MA serialises announcements. Measured on this
        box, four presses of the same effect 0.35s apart came out roughly 3s
        apart, because each one waits for the previous to finish through the
        snapserver and the A2DP buffers. MA has no ducking option (every
        announce_* key was checked) and bluez-alsa will not mix a second stream
        into a single A2DP transport, so there was nowhere else for the sound to
        go. Doing the mix ourselves is the only place it can happen.

        With this off, every unit in the chain is exactly what it was before this
        option existed, including the musicbox package's own closure: the two
        extra Python libraries the mixer needs are an argument of the package
        build (nix/package.nix, `withMixer`) that only this option flips
      '';

      duckDb = mkOption {
        type = types.float;
        default = -12.0;
        example = -9.0;
        description = ''
          How far the music is attenuated while an effect voice is playing, in
          decibels. Negative, because it is an attenuation: -12 dB is a quarter
          of the amplitude, which is enough for a voice to sit clearly on top
          without the music appearing to stop.

          Expressed in dB and not as a ratio on purpose. snapclient keeps
          `--mixer software` even when it writes to the FIFO, so the music
          arriving at the mixer has ALREADY been scaled by whatever volume MA is
          set to. A duck expressed as "multiply by 0.25" is the same relative
          change at any volume; a duck expressed as "bring it down to level X"
          would be wrong at every volume but one.

          0.0 disables the duck and simply sums the effect on top. That is
          louder than it sounds and is the setting most likely to clip, which
          the mixer handles by clipping properly rather than by wrapping, but
          clipping still sounds like clipping.
        '';
      };

      periodMs = mkOption {
        type = types.int;
        default = 20;
        description = ''
          The mixer's block size, in milliseconds. It is the unit of work for
          the whole loop: read this much music from the FIFO, mix this much of
          every active voice, write this much to ALSA.

          20 ms is 960 frames at 48000:16:2, and that number was chosen against
          measurements on this box rather than by feel:

          - It is the mixer's own contribution to trigger latency. An effect
            starts at the next block boundary, so this is at most 20 ms of the
            roughly 220 to 420 ms it takes a press to become audible, almost all
            of which is the 200 ms ALSA buffer and the opaque A2DP buffer that
            are already in today's path.
          - It divides snapclient's producer chunk cleanly. The file player
            writes exactly 9600 bytes (50 ms) every 50 ms and that 50 is a
            compile-time constant with no option for it, so 20 ms gives a 1:2.5
            ratio instead of an awkward one.
          - The work per block is 28 to 51 us measured with one to four voices,
            against a 20000 us budget. That is a safety factor of roughly 400,
            which is the right margin on a box that has wedged itself twice.

          Going smaller buys you a few milliseconds of trigger latency and costs
          you wakeups: 5 ms is 200 wakeups a second and 200 chances to be late.
          Going larger is the wrong direction for a feature whose entire point is
          that the effect lands when the key is pressed.
        '';
      };

      periods = mkOption {
        type = types.int;
        default = 10;
        example = 6;
        description = ''
          Number of `periodMs` periods in the ALSA ring buffer, so the buffer
          depth is periodMs * periods. The default 20 * 10 is 200 ms, and 200 ms
          is not a round number someone liked: it is the one depth this box has
          been shown to survive.

          80 ms, which is snapclient's own default, stutters: 484 XRUNs in five
          minutes. 500 ms stops playing entirely, because the client then asks
          for more audio than the source has ready and logs "Not enough frames
          available ... Failed to get chunk" forever while every service reports
          healthy. That second failure is the one to be afraid of, and it is the
          reason this option is not the first knob to reach for.

          It is nevertheless the only real lever on trigger latency: the ALSA
          buffer is the largest term in the roughly 220 to 420 ms between a key
          press and a sound. If you want the effects tighter, walk this down
          towards 6 (120 ms) WHILE LISTENING, one step at a time. Do not walk it
          down blind, and do not walk it down during an event.
        '';
      };

      voices = mkOption {
        type = types.int;
        default = 8;
        description = ''
          Maximum number of effect voices sounding at once. Past this the oldest
          voice is dropped rather than the newest refused, so hammering a key
          never stops responding.

          Eight is about taste, not CPU: four voices at a 20 ms period measured
          51 us of a 20000 us budget on this Pi, which is 0.26% of one core of
          four. The number that matters is that rapid fire on one key adds
          voices rather than restarting one, so pressing repeatedly gives the
          stutter effect people expect from a sampler instead of a single sound
          that keeps starting over.
        '';
      };

      gainDb = mkOption {
        type = types.float;
        default = 0.0;
        example = 3.0;
        description = ''
          Gain applied to every effect voice, in decibels, before it is summed
          with the ducked music. This is the "effects at their own volume" knob,
          and it is separate from `duckDb` on purpose: duck moves the music,
          gain moves the effect, and an event usually wants one of the two
          rather than both.

          Positive values are allowed and are the normal case for a quiet sample,
          but they are also the fastest way to clip. The mixer sums in float and
          clips properly, so the result is honest clipping rather than the int16
          wraparound that sounds like tearing, and clipping still sounds bad.
        '';
      };

      pipeBytes = mkOption {
        type = types.int;
        default = 16384;
        example = 65536;
        description = ''
          Capacity of the FIFO between snapclient and the mixer, in bytes, set
          with F_SETPIPE_SZ. At 48000:16:2 there are 192 bytes per millisecond,
          so 16384 is 85 ms.

          The kernel default on this box is 262144 bytes, which is 1365 ms of
          audio: if the pipe ever fills, that is latency added invisibly and it
          never drains back down. Capping it turns a mixer stall into immediate
          backpressure on snapclient instead of a growing delay nobody can see.

          The granularity is coarse and not a choice: this Pi 5 kernel uses
          16384 byte pages, so F_SETPIPE_SZ rounds to a page. Asking for 4096 or
          16384 both give 16384 (85 ms); asking for 38400 or 65536 both give
          65536 (341 ms). There is nothing in between, so this option really has
          two useful values. 65536 is the more forgiving one if the FIFO turns
          out to run dry over a long evening.
        '';
      };

      device = mkOption {
        type = types.nullOr types.str;
        default = null;
        defaultText = lib.literalExpression ''"bluealsa:DEV=''${speakerMac},PROFILE=a2dp"'';
        example = "default";
        description = ''
          ALSA PCM the mixer writes to, handed straight to snd_pcm_open. Null
          means the same bluealsa PCM snapclient opens today, built from
          `bluetoothAudio.speakerMac`, which is what you want in production.

          It has to be configuration and cannot be discovered: bluez-alsa ships
          no ALSA namehints, so `alsaaudio.pcms()` on this box returns no entry
          containing "bluealsa" at all, with or without ALSA_PLUGIN_DIR. Set it
          to something like "default" or "null" only to test the mixer without
          taking the speaker.
        '';
      };

      fifo = mkOption {
        type = types.str;
        default = "/run/musicbox-mixer/snapfifo";
        description = ''
          The FIFO snapclient writes raw PCM into and the mixer reads music out
          of. Must live in /run/musicbox-mixer: that is the mixer unit's
          RuntimeDirectory, which is the only directory systemd creates with the
          ownership and mode both processes need, and cleans up afterwards.

          The unit creates the node itself with `mkfifo -m 0660` before the mixer
          starts, and that ordering is not cosmetic. snapclient's file player
          opens its target with fopen(..., "wb"), so a missing path becomes a
          REGULAR FILE that quietly accumulates 9600 bytes every 50 ms in a
          tmpfs while every unit reports healthy and the room stays silent.
        '';
      };

      socket = mkOption {
        type = types.str;
        default = "/run/musicbox-mixer/control.sock";
        description = ''
          Unix socket the mixer listens on for trigger commands, and the only
          thing musicbox needs in order to use the mixer: MUSICBOX_MIXER_SOCKET
          is set on the musicbox unit when this is enabled and unset when it is
          not, so the fallback to Music Assistant's announcement path is decided
          by one environment variable being present.

          A unix socket rather than a second HTTP port because there is no
          second port to defend, no token to plumb, and the filesystem
          permissions are the whole access control: mode 0660 owned by the mixer
          with group `audio`, in a 0750 directory.

          Must live in /run/musicbox-mixer, for the same RuntimeDirectory reason
          as `fifo`.
        '';
      };

      sampleFormat = mkOption {
        type = types.str;
        default = "48000:16:2";
        example = "";
        description = ''
          Passed to snapclient as `--sampleformat` when the mixer is enabled, so
          the FIFO is guaranteed to carry the format the mixer is about to
          interpret. Empty string means do not pass it at all.

          This exists because of a failure with no error message anywhere. The
          FIFO carries headerless PCM: there is no format in the bytes. The
          mixer is compiled around 48000:16:2 (the live stream today logs
          "Codec: flac, sampleformat: 48000:16:2"), and if Music Assistant ever
          changes what its snapserver produces, the same bytes get reinterpreted
          at the wrong rate and everything plays at the wrong pitch while every
          service reports healthy. Pinning it here moves that failure into
          snapclient's resampler, where it is at worst audible and at best
          logged, instead of leaving it in the mixer where it is neither.

          Setting it to the format the stream already has should be a no-op in
          snapclient. If you ever suspect otherwise, this is the option to empty
          out first, because it is the only argument here that touches the audio
          data rather than routing it.
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
        }
        # MUSICBOX_MIXER is the switch and MUSICBOX_MIXER_SOCKET is where to
        # knock. musicbox uses the mixer for POST /sfx and the MCP sfx tool when
        # the switch is on and the socket answers, and falls back to Music
        # Assistant's announcement path when it does not, with the response
        # saying which path it took.
        #
        # Both unset when the mixer is off, so there is no way for this unit to
        # be pointed at a socket that this module did not create.
        //
        lib.optionalAttrs cfg.mixer.enable {
          MUSICBOX_MIXER = "1";
          MUSICBOX_MIXER_SOCKET = cfg.mixer.socket;
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
        }
        # ── What talking to the mixer costs this unit ────────────────────────
        # Two additions, both only when the mixer is on, and both are about
        # reaching one unix socket.
        //
        lib.optionalAttrs cfg.mixer.enable {
          # The socket is mode 0660 owned by the mixer with group `audio`, so
          # connect() needs this. It also hands musicbox the ability to talk to
          # bluez-alsa over D-Bus, which it has no code to do; that is the price
          # of reusing the group snapclient and the mixer already share rather
          # than inventing a third one for a single socket.
          #
          # This is additive to the existing DynamicUser identity, not a
          # replacement for it: musicbox keeps its transient uid.
          SupplementaryGroups = [ "audio" ];

          # ProtectSystem = "strict" mounts the whole hierarchy read-only, /run
          # included. connect() on a unix socket should not need the mount to be
          # writable, since it modifies no inode, but "should not" is not a thing
          # to find out on a live box, and the cost of being sure is one line.
          #
          # The `-` prefix makes it non-fatal when the path is missing, which is
          # the normal state before the mixer has ever started: without it,
          # musicbox would refuse to start whenever the mixer was down, and
          # taking the HTTP API out with the mixer is the opposite of a fallback.
          ReadWritePaths = [ "-${mixerRuntimeDir}" ];
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
          ++ lib.optional cfg.musicAssistant.enable "podman-music-assistant.service"
          # The mixer must own the FIFO before snapclient reaches for it, and
          # `after` is the load-bearing half of that. snapcast's FilePlayer does
          # its fopen(..., "wb") in the CONSTRUCTOR, and glibc's "w" on a FIFO
          # blocks until a reader exists. Start snapclient first and it does not
          # crash and does not restart-loop: it hangs inside the constructor with
          # the unit reporting active and the room silent, which is this box's
          # worst failure mode.
          ++ lib.optional cfg.mixer.enable "musicbox-mixer.service";
        wants = [ "bluealsa.service" "musicbox-bt-connect.service" ];
        # An open PCM does not survive a bluealsa restart; cycle with it.
        #
        # The mixer gets the same treatment for the mirror-image reason: with the
        # mixer gone the FIFO has no reader, and snapclient's fwrite has no error
        # handling and runs on its single io_context, so it blocks there and stops
        # reading the network too. Better to cycle it deliberately than to leave a
        # process wedged on a pipe.
        partOf = [ "bluealsa.service" ]
          ++ lib.optional cfg.mixer.enable "musicbox-mixer.service";
        # partOf propagates restart, bindsTo propagates "the thing I depend on
        # went away". Both, because both happen: a rebuild restarts the mixer and
        # a crash stops it.
        bindsTo = lib.optional cfg.mixer.enable "musicbox-mixer.service";

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
          ExecStart = lib.escapeShellArgs ([
            "${snapcast}/bin/snapclient"
            # Positional URL, not -h/--host: those are deprecated as of 0.35.
            # Passing it explicitly also avoids snapclient falling back to mDNS
            # discovery (tcp://_snapcast._tcp) for a server on loopback, which
            # would make this depend on avahi for no reason.
            "tcp://127.0.0.1:1704"
            # Explicito mesmo sendo o default, para que uma mudanca futura de
            # default falhe alto em vez de escolher pipewire em silencio.
            #
            # O buffer_time aqui e o que resolve engasgo em caixa Bluetooth,
            # e tem teto dos dois lados: 80ms (o default) engasga, 500ms para
            # de tocar. Ver alsaBufferMs, que conta a historia inteira com os
            # numeros.
            #
            # With the mixer enabled this becomes the file player instead, and
            # snapclient stops opening ALSA entirely: it writes the raw stream
            # into a FIFO and musicbox-mixer owns the device from there. The
            # option set was read off the running binary
            # (`snapclient --player 'file:?'`) rather than guessed: filename and
            # mode=[w|a] are the only two, and mode=w is the default, spelled out
            # here so an upstream default change cannot silently start appending
            # to a pipe.
            #
            # Nothing about buffer_time or fragments applies on this branch. The
            # ALSA buffer they size is the mixer's problem now, and it reproduces
            # the same 200 ms depth this box measured its way to.
            "--player"
            (
              if cfg.mixer.enable
              then "file:filename=${cfg.mixer.fifo},mode=w"
              else "alsa:buffer_time=${toString cfg.bluetoothAudio.alsaBufferMs},fragments=${toString cfg.bluetoothAudio.alsaFragments}"
            )
          ]
          # Raw ALSA PCM name, handed straight to snd_pcm_open. Dropped when the
          # mixer is on, because snapclient no longer opens a device and leaving
          # a soundcard argument on a process that ignores it is how the next
          # person spends an hour looking at the wrong end of the chain.
          ++ lib.optionals (!cfg.mixer.enable) [
            "--soundcard"
            bluealsaPcm
          ]
          # Pin the FIFO's format so a change in what MA's snapserver produces
          # fails in snapclient's resampler instead of silently reinterpreting
          # headerless PCM at the wrong rate downstream. See mixer.sampleFormat.
          # O campo de canais vai como `*`, e nao como o numero.
          #
          # snapclient recusa `48000:16:2` com
          #   [Fatal] Exception: sampleformat channels must be * (= same as the source)
          # e sai com status 1, num laco de restart. Ele aceita fixar taxa e
          # profundidade, mas exige que o numero de canais acompanhe a fonte.
          # Como o mixer so entende 2 canais, o que protege de verdade e a
          # checagem que ele faz ao abrir o dispositivo, nao este argumento.
          # Descoberto ligando o mixer pela primeira vez, em 2026-08-19.
          ++ lib.optionals (cfg.mixer.enable && cfg.mixer.sampleFormat != "") [
            "--sampleformat"
            (builtins.replaceStrings [ ":2" ] [ ":*" ] cfg.mixer.sampleFormat)
          ]
          ++ [
            # Mandatory with bluez-alsa. The hardware mixer path goes through
            # bluealsa's ctl plugin and is broken in this combination
            # (snapcast issue #1317). `software` is the current default; pinned
            # so a default change cannot break the box mid-event.
            "--mixer"
            "software"
            # 500ms de folga, e nao zero.
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
            #
            # Subiu de 200 para 500 no dia seguinte, com a sala cheia: as mesmas
            # duas medidas deram 338 XRUNs em 10 minutos com 200, e 0 em 2
            # minutos com 500. O link Bluetooth piora com gente no meio e com
            # mais radio na sala, entao o valor que bastava numa sala vazia nao
            # basta num evento. Meio segundo continua imperceptivel para musica
            # ambiente, e o custo aparece so em comando: pause e skip demoram
            # esse tanto para virar silencio.
            "--latency"
            (toString cfg.bluetoothAudio.latencyMs)
            # Default is the MAC address of whichever interface snapclient
            # picks, which means the player's name in MA changes if the network
            # hardware does, and MUSICBOX_PLAYER points at that name. Pin it.
            "--hostID"
            "musicbox"
            "--logsink"
            "stdout"
          ]);
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

    # ── The software mixer ────────────────────────────────────────────────────
    # The chain with this on:
    #
    #   MA -> snapserver -> snapclient --player file:filename=<fifo>
    #                                     |
    #                                musicbox-mixer   <- effects triggered here
    #                                     |
    #                                ALSA (bluealsa) -> speaker
    #
    # snapclient stops being the process that owns the speaker and becomes a
    # producer of raw PCM. Everything downstream of the FIFO is ours, which is
    # the only place a second sound can be added to a single A2DP stream: bluez
    # -alsa will not mix one, and MA has no ducking to offer.
    (mkIf cfg.mixer.enable {
      assertions = [
        {
          assertion = cfg.bluetoothAudio.enable;
          message =
            "services.musicbox.mixer.enable requires bluetoothAudio.enable. The mixer "
            + "replaces snapclient's ALSA output and opens the bluealsa PCM itself, so "
            + "without that chain there is no snapclient to reroute, no bluealsa to open "
            + "and no speaker MAC to derive a device from.";
        }
        {
          assertion = lib.hasPrefix "${mixerRuntimeDir}/" cfg.mixer.fifo;
          message =
            "services.musicbox.mixer.fifo must live under ${mixerRuntimeDir} (got "
            + "${cfg.mixer.fifo}). That directory is the unit's RuntimeDirectory: systemd "
            + "creates it owned by the mixer with group audio and mode 0750, which is what "
            + "lets snapclient open the FIFO for writing, and removes it on stop so a FIFO "
            + "never outlives the process that was draining it.";
        }
        {
          assertion = lib.hasPrefix "${mixerRuntimeDir}/" cfg.mixer.socket;
          message =
            "services.musicbox.mixer.socket must live under ${mixerRuntimeDir} (got "
            + "${cfg.mixer.socket}). Same reason as mixer.fifo: it is the one directory "
            + "whose ownership and lifetime systemd manages for this unit.";
        }
        {
          # 5 ms is 240 frames and 200 wakeups a second, which is a lot of
          # chances to be late for a few milliseconds of trigger latency. 50 ms
          # is snapclient's own producer chunk, past which the mixer's block
          # size starts to dominate the very latency it exists to reduce.
          assertion = cfg.mixer.periodMs >= 5 && cfg.mixer.periodMs <= 50;
          message =
            "services.musicbox.mixer.periodMs must be between 5 and 50 (got "
            + "${toString cfg.mixer.periodMs}). Below 5 the wakeup rate buys nothing an "
            + "A2DP link can deliver; above 50 the mixer's own block size becomes the "
            + "largest term in trigger latency, which is the thing it exists to shrink.";
        }
        {
          # The two numbers in this message are measurements from this box, not
          # margins of taste. 80 ms stuttered 484 times in 5 minutes; 500 ms
          # stopped producing sound at all while every unit stayed green. The
          # window between them is where this has to live.
          assertion =
            let ms = cfg.mixer.periodMs * cfg.mixer.periods; in
            ms >= 100 && ms <= 400;
          message =
            "services.musicbox.mixer periodMs * periods is the ALSA buffer depth in "
            + "milliseconds and must land between 100 and 400 (got "
            + "${toString cfg.mixer.periodMs} * ${toString cfg.mixer.periods} = "
            + "${toString (cfg.mixer.periodMs * cfg.mixer.periods)}). Measured on this "
            + "box: 80ms stutters, 484 XRUNs in 5 minutes. 500ms stops playing entirely, "
            + "with 'Not enough frames available ... Failed to get chunk' repeating "
            + "forever while every service reports healthy. 200ms is what works.";
        }
        {
          assertion = cfg.mixer.duckDb <= 0.0;
          message =
            "services.musicbox.mixer.duckDb must be zero or negative (got "
            + "${toString cfg.mixer.duckDb}). It is an attenuation applied to the music "
            + "while an effect plays. A positive value would BOOST the music under the "
            + "effect and clip, which is the opposite of ducking.";
        }
      ];

      warnings = lib.optional (!(cfg.package ? override)) ''
        services.musicbox.mixer is enabled but services.musicbox.package cannot be
        overridden, so numpy and pyalsaaudio were not added to its build. If that
        package does not already carry them, musicbox-mixer will fail at startup
        with an ImportError. Build it with `withMixer = true` (see
        nix/package.nix) or add both libraries yourself.
      '';

      # A static user, deliberately, and not DynamicUser. Two reasons, and the
      # second one is the one that bites.
      #
      # 1. bluez-alsa's D-Bus policy grants send_destination=org.bluealsa to
      #    user root and to GROUP audio, with no other exemption. Anything that
      #    opens a bluealsa PCM has to be in `audio` or the open fails with
      #    "Rejected send message ... destination=org.bluealsa", which alsa-lib
      #    surfaces as a generic device error that reads like the speaker is not
      #    connected. Measured directly: a user in users/wheel/networkmanager but
      #    not audio gets exactly that denial. snapclient's unit is the model to
      #    copy here, not musicbox's, which runs DynamicUser with no groups.
      # 2. The FIFO and the socket live in a directory two other users have to
      #    reach. Group ownership is the whole access control, and a uid that
      #    changes on every boot makes that story harder to reason about for no
      #    gain: this service owns no persistent state that needs the isolation.
      users.users.musicbox-mixer = {
        description = "musicbox software mixer";
        isSystemUser = true;
        # Primary group, the same way the bluealsa daemon's own user is set up.
        # It means every file the mixer creates, the control socket included, is
        # group audio without anyone having to remember to chgrp it.
        group = "audio";
      };

      systemd.services.musicbox-mixer = {
        description = "musicbox software mixer feeding the Bluetooth speaker";
        wantedBy = [ "multi-user.target" ];
        after = [ "bluealsa.service" "musicbox-bt-connect.service" ];
        wants = [ "bluealsa.service" "musicbox-bt-connect.service" ];
        # An open PCM does not survive a bluealsa restart, exactly as for
        # snapclient. The mixer catches ALSA errors and reopens on its own, but
        # a supervisor that already knows the device went away is cheaper and
        # more predictable than a retry loop discovering it.
        partOf = [ "bluealsa.service" ];

        # ── The decoders, and why this line is load bearing ────────────────────
        # The mixer pre-decodes every sound effect to raw PCM at startup by
        # spawning one of ffmpeg, mpg123 or sox, found with shutil.which(). A
        # systemd service on this box does NOT inherit the system PATH: the
        # default is coreutils, findutils, gnugrep, gnused and systemd, and
        # nothing else (read straight off the running musicbox unit with
        # `systemctl show musicbox -p Environment`). Without this line
        # which() returns None three times, every effect fails to decode, the
        # mixer comes up green with an empty effect list, and every single
        # press falls back to the Music Assistant announcement path. The mixer
        # would be doing all of the work and none of the good.
        #
        # mkForce is required rather than defensive, for the same reason it is
        # on musicbox-bt-connect: the systemd module always defines PATH from
        # the default unit dependencies, so a plain assignment is a conflicting
        # definition and evaluation fails. It REPLACES the default list, which
        # is why coreutils is spelled out again here.
        #
        # ffmpeg is first in the mixer's own decoder list because it is the
        # only one of the three that is exact on every input measured: mpg123
        # emits ZERO BYTES for a wav or an ogg and says nothing about it, and
        # sox leaves the mp3 decoder delay in. All three are in the binary
        # cache for aarch64-linux on the pinned nixpkgs (checked: 200 from
        # cache.nixos.org for each), which matters because this box boots off
        # an SD card and has wedged itself twice compiling from source.
        path = lib.mkForce [
          pkgs.ffmpeg-headless
          pkgs.mpg123
          pkgs.sox
          pkgs.coreutils
        ];

        environment = {
          # The mixer process does not read this one: it is running, which
          # settles the question. It is set so its own start line, and anything
          # run by hand in this unit's environment (`musicbox-mixer --check`),
          # does not report "enable=false" from inside the enabled mixer. That
          # is a small lie of exactly the kind that costs an hour later.
          MUSICBOX_MIXER = "1";
          MUSICBOX_MIXER_FIFO = cfg.mixer.fifo;
          MUSICBOX_MIXER_SOCKET = cfg.mixer.socket;
          MUSICBOX_MIXER_DEVICE = mixerDevice;
          MUSICBOX_MIXER_DUCK_DB = toString cfg.mixer.duckDb;
          MUSICBOX_MIXER_GAIN_DB = toString cfg.mixer.gainDb;
          MUSICBOX_MIXER_VOICES = toString cfg.mixer.voices;

          # periodMs is the option because milliseconds are what a person
          # reasons about; frames are what ALSA takes. 48 frames per ms at
          # 48000 Hz, so 20 ms is 960 frames, which with 10 periods measures out
          # as exactly the 200 ms ALSA buffer this box already proved is the one
          # depth that works.
          MUSICBOX_MIXER_PERIODSIZE = toString (cfg.mixer.periodMs * 48);
          MUSICBOX_MIXER_PERIODS = toString cfg.mixer.periods;
          MUSICBOX_MIXER_PIPE_BYTES = toString cfg.mixer.pipeBytes;

          # The mixer chmods its socket to this after bind(). Set explicitly
          # rather than left to the application default, because it is half of
          # how musicbox reaches it: 0660 plus group `audio`, which musicbox is
          # put in when this is enabled. If it ever came out 0600, every effect
          # would silently fall back to the announcement path over a permission
          # error nobody reads during an event.
          MUSICBOX_MIXER_SOCKET_MODE = "0660";

          # The mixer's view of the sfx directory, which is NOT cfg.sfxDir. See
          # mixerSfxDir in the let block: musicbox's state really lives under
          # /var/lib/private, which is 0700 root and denies any other user at the
          # traversal, so the directory is bind mounted somewhere reachable
          # instead.
          MUSICBOX_SFX_DIR = mixerSfxDir;

          # Where decoded PCM is cached. The sfx directory itself is mounted read
          # only and is owned by musicbox's transient uid anyway, so the cache
          # cannot live beside the mp3s. Offered under two names because a
          # decode cache is exactly the kind of thing that reaches for
          # XDG_CACHE_HOME, and the default of that (~/.cache) is a home
          # directory this user does not have.
          MUSICBOX_MIXER_CACHE_DIR = "/var/cache/musicbox-mixer";
          XDG_CACHE_HOME = "/var/cache/musicbox-mixer";

          # alsa-lib resolves `type bluealsa` by dlopening
          # libasound_module_pcm_bluealsa.so out of $ALSA_PLUGIN_DIR. This is the
          # same variable snapclient's unit sets and for the same reason, but it
          # is worth restating because of how it fails: without it the error is
          # "No such device or address", which reads as "the speaker is not
          # connected" and sends you to bluetoothctl. The real cause is only
          # visible in the alsa-lib dlmisc.c line on stderr, which is why this
          # unit's output has to reach the journal. Measured both ways on this
          # box: unset gives ENXIO, set gives a D-Bus level error, which is proof
          # the plugin loaded.
          ALSA_PLUGIN_DIR = "${pkgs.bluez-alsa}/lib/alsa-lib";

          # Unbuffered, so a traceback and every warn line about a dry FIFO or an
          # XRUN reach the journal as they happen. This service's whole safety
          # story is that a fault is loud; a fault sitting in a pipe buffer is
          # the silent-with-everything-green failure again.
          PYTHONUNBUFFERED = "1";
        };

        serviceConfig = {
          Type = "simple";

          # Creates the FIFO before the mixer, and before snapclient can possibly
          # reach it. See mixerPrepare in the let block for why a missing FIFO is
          # worse than a broken one.
          ExecStartPre = mixerPrepare;

          # Not lib.getExe: mainProgram is musicbox-server. This is the other
          # console script in the same package.
          ExecStart = "${mixerPackage}/bin/musicbox-mixer";

          User = "musicbox-mixer";
          Group = "audio";

          # Belt to the socket mode's braces. The mixer chmods its socket after
          # bind (MUSICBOX_MIXER_SOCKET_MODE above), so this is not what makes
          # the socket reachable; it is what makes everything ELSE the process
          # writes, the decoded PCM cache in particular, group readable and
          # world closed by default. The FIFO does not depend on it either, since
          # mkfifo -m applies its mode the way chmod does.
          UMask = "0007";

          # 0750, not 0755: group audio can traverse, which covers snapclient
          # reaching the FIFO and musicbox reaching the socket, and nothing else
          # can. Both files live here so that stopping the unit removes both.
          #
          # The mode does one more thing that is worth stating because it is
          # easy to "tidy" away: group has no WRITE bit, so snapclient cannot
          # create a file in here. snapcast's file player opens its target with
          # fopen(..., "wb"), which CREATES a regular file when the path is
          # missing, and it would then happily write 9600 bytes every 50 ms into
          # a regular file in a tmpfs with every unit green and the room silent.
          # With 0750 that open fails with EACCES instead, which is loud.
          RuntimeDirectory = "musicbox-mixer";
          RuntimeDirectoryMode = "0750";

          # Keep the directory, and therefore the FIFO inode, across a restart
          # of THIS unit. Removed on a real stop, as usual.
          #
          # Without it, every mixer restart destroys the node snapclient has
          # open. snapclient does not reopen it and does not check its writes,
          # so depending on whether it takes the SIGPIPE it either dies (fine,
          # it comes back) or keeps running while writing into a deleted inode
          # forever (not fine: unit active, no music, nothing in any log). The
          # mixer opens the fifo O_RDWR and so is its own writer; the same trick
          # from the other side is this line, and between them a restart of
          # either process is a gap in the sound rather than an outage.
          RuntimeDirectoryPreserve = "restart";

          # /var/cache/musicbox-mixer, for the decoded PCM. Effects are decoded
          # once at startup and cached, so that triggering one costs a memory
          # copy and not an ffmpeg spawn: decoding the current 12 file, 862 KB
          # sfx directory takes well under a second of CPU and about 11 MB.
          CacheDirectory = "musicbox-mixer";

          # Read only on purpose, and it could not usefully be otherwise: the
          # directory belongs to musicbox's transient uid, so the mixer could not
          # write into it even if the mount allowed it. The `-` prefix makes the
          # mount non-fatal when the source does not exist yet, which is the
          # state of a box where musicbox has never started. The mixer then finds
          # an empty sfx directory and says so, which is a better failure than a
          # unit that refuses to start.
          BindReadOnlyPaths = [ "-${mixerSfxSource}:${mixerSfxDir}" ];

          # `always`, not `on-failure`, and the same reasoning as snapclient's:
          # the normal state between boot and the speaker reconnecting is that
          # there is no A2DP transport to open, and a mixer that gives up then is
          # a mixer that needs a human on a headless box.
          Restart = "always";
          RestartSec = "5s";

          # ── Hardening, kept deliberately thin ────────────────────────────────
          # snapclient, which does the same job against the same device, runs
          # with none of this. The failure mode of over-sandboxing here is a
          # process that starts, reports healthy and produces no sound, which is
          # the one failure this box must not have. So: the cheap options that
          # cannot interfere with ALSA or D-Bus, and nothing else.
          #
          # Specifically NOT set, each for a reason:
          #   PrivateDevices  removes /dev/snd. bluez-alsa needs D-Bus rather
          #                   than a device node, so this would probably be fine,
          #                   and "probably fine" is not a good enough reason to
          #                   put a sandbox between this process and its output.
          #   ProtectSystem=strict  would need the runtime, cache and mount paths
          #                   listing by hand for no benefit over "full" here.
          #   RestrictAddressFamilies  the process needs AF_UNIX for both the
          #                   control socket and the system bus; getting the list
          #                   wrong reads as bluealsa refusing the connection.
          #   Nice / RT priority  the mix costs 51 us of a 20000 us budget with
          #                   four voices, 0.26% of one core. If XRUNs ever show
          #                   up, a small negative Nice is the first thing to try
          #                   and a bigger buffer is the last: this box has been
          #                   burned twice by the second one.
          NoNewPrivileges = true;
          ProtectSystem = "full";
          ProtectHome = true;
          PrivateTmp = true;
        };

        # A speaker power cycle produces a burst of failed starts, the same as it
        # does for snapclient, and systemd's default start limit would then wedge
        # the unit permanently. A wedged mixer takes snapclient down with it now,
        # so this matters more here than it does there.
        unitConfig.StartLimitIntervalSec = 0;
      };
    })
  ]);
}
