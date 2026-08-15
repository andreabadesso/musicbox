# musicbox

A thin HTTP API in front of [Music Assistant](https://music-assistant.io/). It turns a
Raspberry Pi 5 and a Bluetooth speaker into something another program can shout at:
queue a track, play a track, drop an airhorn over whatever is playing, skip, pause,
set the volume.

## What this is

- A small FastAPI service, `musicbox-server`, that speaks to Music Assistant over its
  websocket API and exposes a handful of event-shaped JSON endpoints.
- The only thing your other system has to talk to. It never has to learn the Music
  Assistant protocol, the queue model, or the difference between a player id and a
  queue id.
- An MCP server on the same port, so an agent controls the music as tools rather
  than as curl commands. See [MCP server](#mcp-server).
- A NixOS module that brings up the whole chain: the Music Assistant container,
  the Bluetooth stack, the snapcast client, and musicbox itself.

## What this is not

- Not a music library, not a player, not a Spotify client. Music Assistant does all
  of the actual work. musicbox is a translation layer with opinions.
- Not a general purpose Music Assistant proxy. It exposes the endpoints in the list
  below and nothing else. If you need the full command set, talk to Music Assistant
  on port 8095 directly.
- Not multi-room. It controls exactly one player, named by `MUSICBOX_PLAYER`.
- Not authenticated by default. Set `MUSICBOX_TOKEN` if you want a bearer token, and
  bind it to a private interface either way. `/mcp` is unauthenticated even when
  `MUSICBOX_TOKEN` is set, deliberately; see [MCP server](#mcp-server).
- Not audited, not hardened, not stable. It was written for a hackathon.

## Architecture

```
  your program
       |
       |  HTTP + JSON  (port 8099)
       v
  +--------------------+
  |     musicbox       |   resolves the player, holds one websocket,
  |  musicbox-server   |   serves files from MUSICBOX_SFX_DIR over HTTP
  +--------------------+
       |
       |  websocket API  (port 8095)
       v
  +--------------------+
  |  Music Assistant   |   providers (Spotify, plain URLs), the queue,
  |    (container)     |   the stream server (port 8097),
  |                    |   and a built-in snapserver
  +--------------------+
       |
       |  snapcast protocol  (ports 1704 / 1705)
       v
  +--------------------+
  |     snapclient     |   native on the host, not in the container
  +--------------------+
       |
       |  ALSA PCM  bluealsa:DEV=<mac>,PROFILE=a2dp
       v
  +--------------------+
  |     bluez-alsa     |   A2DP encoder, talks to bluetoothd over D-Bus
  +--------------------+
       |
       |  Bluetooth A2DP
       v
  +--------------------+
  |  Bluetooth speaker |
  +--------------------+
```

The split lands at snapcast on purpose. Music Assistant runs in a container and never
touches audio hardware: it produces a snapcast stream and stops there, so the container
needs no `/dev/snd`, no D-Bus socket, no Bluetooth capabilities, and no privileged flags.
Everything that needs real hardware access lives on the host as ordinary systemd units.
That also means you can restart Music Assistant without disturbing the Bluetooth link,
and restart the Bluetooth stack without disturbing the queue.

## Quick start, no Pi involved

You can run the whole thing on a laptop first. Nothing below needs Bluetooth: Music
Assistant will happily drive its own local player, and every musicbox endpoint works
against it.

1. Start Music Assistant. Host networking keeps the port mapping honest and is what
   Music Assistant's own docs assume.

   ```sh
   mkdir -p ~/ma-data
   docker run -d --name music-assistant \
     --network=host \
     -v ~/ma-data:/data \
     ghcr.io/music-assistant/server:2.9.13
   ```

   Pin the tag. `latest` can be retagged under you halfway through an event.

2. Open http://127.0.0.1:8095 and complete the first-run setup. Music Assistant 2.9
   requires a user before anything answers, including the websocket, so this step
   cannot be skipped. Create the user, then add providers (Spotify if you want it,
   and note that the built-in "URL" provider needs no setup at all).

3. Add a player. The web UI has a built-in web player. Note its name.

4. Mint a long-lived token for musicbox. Log in first, then trade that for a token
   that does not expire in 30 days.

   ```sh
   JWT=$(curl -sS http://127.0.0.1:8095/api \
     -H 'Content-Type: application/json' \
     -d '{"command":"auth/login","args":{"username":"admin","password":"CHANGEME","device_name":"musicbox"}}' \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

   curl -sS http://127.0.0.1:8095/api \
     -H "Authorization: Bearer $JWT" \
     -H 'Content-Type: application/json' \
     -d '{"command":"auth/token/create","args":{"name":"musicbox"}}'
   ```

   The second call returns a bare JWT string. That is the long-lived token. Write it
   to a file, do not paste it into a config that gets committed.

   ```sh
   mkdir -p ~/.local/share/musicbox
   ( umask 077; printf '%s' '<the-jwt>' > ~/.local/share/musicbox/token )
   ```

5. Run musicbox, with the environment it wants.

   ```sh
   export MUSICBOX_MA_URL=http://127.0.0.1:8095
   export MUSICBOX_MA_TOKEN_FILE=$HOME/.local/share/musicbox/token
   export MUSICBOX_PLAYER="Web Player"
   export MUSICBOX_SFX_DIR=$PWD/sfx

   nix run github:andreabadesso/musicbox
   # or, in a checkout:  nix develop  then  python -m musicbox
   ```

   `--check` prints the resolved configuration and exits without binding a port, which
   is the fastest way to confirm the token file was actually found:

   ```sh
   nix run github:andreabadesso/musicbox -- --check
   ```

   Without the token from step 4 musicbox still starts and still connects: the socket
   is fine, it is the commands that get refused. `/health` then shows
   `ma_connected: true` with `ma_authenticated: false`. That is the expected failure,
   not a bug, and it is a different failure from Music Assistant being down.

6. Check it.

   ```sh
   curl -sS http://127.0.0.1:8099/health
   ```

   You want `ok: true`, `ma_connected: true`, `ma_authenticated: true`, and your player
   name echoed back in `player_name`. `ma_connected: false` means musicbox could not
   reach Music Assistant at all; `ma_authenticated: false` means it got there and the
   token was refused. Either way every other endpoint will fail until it is fixed.

## Configuration

All configuration is environment variables. There is no config file.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MUSICBOX_HOST` | `127.0.0.1` | Listen address. |
| `MUSICBOX_PORT` | `8099` | Listen port. |
| `MUSICBOX_MA_URL` | `http://127.0.0.1:8095` | Music Assistant base URL. Plain http URL, not the ws one. |
| `MUSICBOX_PLAYER` | none, required at runtime | Music Assistant player id or player name. Name is resolved to an id at connect time. |
| `MUSICBOX_SFX_DIR` | `/var/lib/musicbox/sfx` | Directory of preloaded audio files served by `POST /sfx/{name}`. |
| `MUSICBOX_TOKEN` | unset | If set, every request to musicbox must carry `Authorization: Bearer <token>`. This is musicbox's own front door, not the Music Assistant token. |

Those six names are the frozen interface. The rest are additions, each of which exists
for a reason worth knowing about.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MUSICBOX_MA_TOKEN` | unset | Music Assistant token. Not optional in practice, see below. |
| `MUSICBOX_MA_TOKEN_FILE` | unset | Path to a file holding the above. |
| `MUSICBOX_TOKEN_FILE` | unset | Path to a file holding `MUSICBOX_TOKEN`. |
| `MUSICBOX_SFX_BASE_URL` | unset | The base URL Music Assistant should use to fetch sfx files from musicbox. |
| `MUSICBOX_COMMAND_TIMEOUT` | `20` | Seconds to wait for an ordinary Music Assistant command. |
| `MUSICBOX_ANNOUNCE_TIMEOUT` | `300` | Seconds to wait for a drop. Must exceed your longest sfx. |
| `MUSICBOX_BACKOFF_INITIAL` | `1` | First reconnect delay, seconds. |
| `MUSICBOX_BACKOFF_MAX` | `30` | Reconnect delay ceiling, seconds. |
| `MUSICBOX_URL` | `http://127.0.0.1:8099` | Read by the `musicbox-mcp` stdio server only, never by the service itself. Which musicbox that stdio server controls. |

For each token, the inline variable wins and the `_FILE` form is the fallback. The file
form exists so a systemd unit can point at a path outside the nix store instead of
interpolating a secret into a world readable store path.

There are two different tokens here and they are easy to confuse.

- `MUSICBOX_TOKEN` protects musicbox's own front door. You invent it. It is optional,
  and if it is unset musicbox does not check authorization at all.
- `MUSICBOX_MA_TOKEN` is what musicbox uses to authenticate outbound to Music Assistant.
  You mint it from Music Assistant, see step 4 of the quick start. Music Assistant 2.9
  authenticates every command except the four auth ones, so without it musicbox connects
  successfully and then has every single command refused with `error_code 20`. `/health`
  reports that as `ma_connected: true` with `ma_authenticated: false`. A missing token
  file is deliberately not fatal, so the symptom is a running service that cannot do
  anything rather than a crash loop, which does mean it is easy to leave unset. On NixOS
  set `services.musicbox.maTokenFile`; the module warns at build time if you do not.

`MUSICBOX_SFX_BASE_URL` is the one that will bite you on the Pi. Music Assistant refuses
local paths for announcements, so musicbox serves `MUSICBOX_SFX_DIR` over HTTP itself and
hands Music Assistant a URL. That URL has to be reachable from inside the Music Assistant
container, and `127.0.0.1` inside a container is the container, not the host.

The NixOS module always sets this, to `http://127.0.0.1:<port>` when musicbox binds a
wildcard address and to `http://<host>:<port>` otherwise, because the Music Assistant
container runs on the host network namespace and loopback therefore means the same thing
on both sides. Running musicbox by hand, leave it unset and musicbox falls back to the
`Host` header of the incoming request. That fallback is good enough on a laptop and is
the wrong default on the Pi: `curl http://pi5:8099/...` would hand Music Assistant the
bare name `pi5`, and the container resolves names through its own `/etc/resolv.conf`,
not through your shell's. If Music Assistant is on a bridge network instead of the host
one, set this to an address the container can actually reach, usually the podman
gateway.

## Deploying on a Pi 5 running NixOS

Add the flake input:

```nix
# musicbox: thin HTTP API in front of Music Assistant.
# follows nixpkgs on purpose. The Pi rebuilds locally over its own link, and a
# second nixpkgs pin would mean fetching an entire extra nixpkgs tarball at eval
# time for no benefit. musicbox only uses cached python3Packages.
musicbox = {
  url = "github:andreabadesso/musicbox";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

Import `musicbox.nixosModules.musicbox` in the host, then paste this block:

```nix
services.musicbox = {
  enable = true;

  # Reachable over the tailnet. The firewall block below is what actually
  # limits exposure; binding to 0.0.0.0 alone would not.
  host = "0.0.0.0";
  port = 8099;

  # Player name as it appears in the Music Assistant UI. snapclient is started
  # with --hostID musicbox, so this is stable across hardware changes.
  player = "musicbox";

  maUrl = "http://127.0.0.1:8095";
  sfxDir = "/var/lib/musicbox/sfx";

  # The bearer token for musicbox's own API. Read at runtime by the unit
  # through systemd's LoadCredential, never interpolated into the nix store.
  # This is not the Music Assistant token, see the two-tokens note under
  # Configuration.
  #
  # Quote it. `tokenFile = /etc/musicbox/token` without the quotes is a nix
  # path value, which nix copies into the world-readable store when the unit
  # is built. The module asserts against that, but it is worth knowing why.
  tokenFile = "/etc/musicbox/token";

  # The long-lived Music Assistant JWT. Provisioned by hand once, see below.
  # Not optional in practice: Music Assistant 2.9 authenticates every command,
  # so leaving this out gives you a service that starts, connects, and has
  # every single command refused. The module warns if it is unset.
  maTokenFile = "/etc/musicbox/ma-token";

  openFirewall = true;
  firewallInterfaces = [ "tailscale0" ];

  musicAssistant = {
    enable = true;
    dataDir = "/var/lib/music-assistant";
    # Pinned, not latest. A mid-event retag would move the API under you.
    image = "ghcr.io/music-assistant/server:2.9.13";
  };

  bluetoothAudio = {
    enable = true;
    speakerMac = "AA:BB:CC:DD:EE:FF";
  };
};
```

Then deploy the way this host is normally deployed:

```sh
rsync -a --delete --exclude .git ~/.config/nixcfg/ andre@pi5:/tmp/nixcfg/
ssh andre@pi5 'sudo nixos-rebuild switch --flake /tmp/nixcfg#pi5'
```

### One-time imperative setup

Three things cannot be declared and have to be done by hand once. All survive reboots
and rebuilds, none survives a reimage, so back them up before you need them.

**1. The Music Assistant token.** A fresh container has no users, so its websocket
closes with "Setup required" until you create one. Do the first-run setup in a browser
at `http://pi5:8095`, then mint a long-lived token with the two curl calls from the
quick start above (pointing at the Pi), and write it out. This is the file
`maTokenFile` points at, named to stay visibly distinct from musicbox's own bearer
token.

```sh
ssh andre@pi5
sudo install -d -m 0700 /etc/musicbox
sudo sh -c 'umask 077; printf "%s" "<the-jwt>" > /etc/musicbox/ma-token'
sudo systemctl restart musicbox
curl -sS http://127.0.0.1:8099/health   # ma_authenticated should now be true
```

`/etc/musicbox` and not `/var/lib/musicbox` on purpose. The service runs under
`DynamicUser`, so `/var/lib/musicbox` is a symlink into `/var/lib/private` that systemd
creates on first start and may migrate and chown underneath you. `/etc` is plain,
root-owned, and there before the service ever runs.

**2. musicbox's own bearer token,** if you set `tokenFile`. Any random string will do.

```sh
sudo sh -c 'umask 077; head -c 24 /dev/urandom | base64 | tr -d "\n" > /etc/musicbox/token'
sudo systemctl restart musicbox
```

**3. Pairing the speaker.** The pairing keys live in `/var/lib/bluetooth/`, which is
imperative state. Put the speaker into pairing mode first, its advertising window is
often only 60 seconds.

```sh
ssh andre@pi5
systemctl status bluetooth        # must be active, and `bluetoothctl list` must show a controller
sudo bluetoothctl
```

Inside `bluetoothctl`:

```
power on
agent on
default-agent
scan on
```

Wait for a line naming your speaker, note the MAC, then:

```
scan off
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
info AA:BB:CC:DD:EE:FF
quit
```

Leave the scan running and it competes with the connection attempt on the same radio,
which makes pairing flaky. `trust` is the step people skip and it is not optional: it
marks the device as pre-authorized, and without it, when the speaker powers back on and
initiates the reconnect itself, bluetoothd asks an agent to authorize the incoming A2DP
service. There is no agent on a headless box, so the request is rejected and the link
silently never comes back.

`info` should show `Connected: yes`, `Trusted: yes`, `Paired: yes` and a UUID line
containing `Audio Sink`. Then put the MAC into `bluetoothAudio.speakerMac` and rebuild.

Copy `/var/lib/bluetooth` somewhere safe once it works.

## Endpoints

Every endpoint takes and returns JSON. If `MUSICBOX_TOKEN` is set, add
`-H "Authorization: Bearer $MUSICBOX_TOKEN"` to every call below except `GET /health`
and `GET /sfx/file/{filename}`, which are deliberately open (a watchdog should not need
a credential, and Music Assistant cannot present one when it fetches an sfx file). The
examples assume:

```sh
BOX=http://pi5:8099
```

FastAPI's own generated docs are at `$BOX/docs`, which is the fastest way to confirm
what the running build actually accepts. They are unauthenticated like `/health`, and
expose nothing but the route shapes.

### GET /health

Liveness, and whether the Music Assistant connection is actually up. This is the first
thing to call when anything looks wrong.

```sh
curl -sS "$BOX/health"
```

```json
{
  "ok": true,
  "ma_connected": true,
  "player": "snapcast_musicbox",
  "version": "0.1.0",
  "ma_authenticated": true,
  "player_name": "musicbox",
  "player_error": null,
  "ma_version": "2.9.13",
  "ma_schema_version": 28,
  "ma_url": "http://127.0.0.1:8095",
  "announcement_supported": true,
  "connect_attempts": 1,
  "connected_since": 1786600000.0,
  "last_error": null,
  "sfx_count": 3,
  "auth_required": true
}
```

`/health` never requires the bearer token and always answers 200, so a watchdog can
poll it and a colleague can debug the box without a credential. Read three fields
together: `ma_connected` is the socket, `ma_authenticated` is the Music Assistant
token, and `player_error` is whether the player was found. `ma_connected: true` with
`ma_authenticated: false` means the Music Assistant token is missing or expired, which
is a different problem from the server being down.

### GET /now

What is playing, where in the track, how loud, and how many items are left in the queue.
Position is computed live, not read from a stale snapshot, so polling this is safe.

```sh
curl -sS "$BOX/now"
```

```json
{
  "ok": true,
  "player": "snapcast_musicbox",
  "player_name": "musicbox",
  "state": "playing",
  "track": {
    "title": "Song Title",
    "artist": "Some Artist",
    "album": "Some Album",
    "uri": "spotify://track/4cOdK2wGLETKBW3PvgPWqT",
    "duration": 214
  },
  "position": 37.4,
  "volume": 60,
  "muted": false,
  "queue_id": "snapcast_musicbox",
  "queue_length": 12,
  "queue_index": 3,
  "shuffle": false,
  "repeat": "off"
}
```

Tolerate nulls. `track` is null when nothing is loaded, and radio streams and plain
URLs often have no duration, in which case `track.duration` is null and `position` is
not meaningful.

### POST /queue

Append to the end of the queue. Takes either `uri` (a Music Assistant URI, for example
a Spotify one) or `url` (a plain http or https audio URL).

```sh
curl -sS -X POST "$BOX/queue" \
  -H 'Content-Type: application/json' \
  -d '{"uri": "spotify://track/4cOdK2wGLETKBW3PvgPWqT"}'
```

```sh
curl -sS -X POST "$BOX/queue" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/audio/track.mp3"}'
```

### POST /play

Play now, replacing the queue. Same body as `/queue`. A playlist URI works here and is
the usual way to start an event.

```sh
curl -sS -X POST "$BOX/play" \
  -H 'Content-Type: application/json' \
  -d '{"uri": "spotify://playlist/37i9dQZF1DXcBWIGoYBM5M"}'
```

```sh
curl -sS -X POST "$BOX/play" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/audio/intro.mp3"}'
```

### POST /drop

One-shot audio from a URL, on top of whatever is happening.

`mode: "cut"` pauses the queue, plays the drop, and resumes where it stopped. This is
the one that behaves the way you expect and it is the one to use if you care.

`mode: "over"` hands the file to Music Assistant as an announcement and does not touch
the queue. Read the note under it before you rely on it.

```sh
curl -sS -X POST "$BOX/drop" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/audio/airhorn.mp3", "mode": "cut"}'
```

```sh
curl -sS -X POST "$BOX/drop" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/audio/airhorn.mp3", "mode": "over"}'
```

The response tells you which mode actually ran, which is not always the one you asked
for. `over` falls back to `cut` if the player does not advertise `play_announcement`, or
if Music Assistant refuses the announcement anyway. Read `mode_used`, not `mode_requested`.

```json
{
  "ok": true,
  "mode_requested": "over",
  "mode_used": "cut",
  "fell_back": true,
  "reason": "the MA player does not advertise play_announcement",
  "resumed": true,
  "url": "https://example.com/audio/airhorn.mp3"
}
```

When `over` does run as asked, the response carries a `note` field spelling out the
caveat below instead of `reason`/`resumed`.

Honest note about `over`. Music Assistant 2.9 has no ducking anywhere, on any provider.
On the snapcast player, an announcement swaps the group's audio source to the
announcement stream, plays it alone, and swaps back. The music is not audible underneath
and the queue is never paused, so you rejoin the track a few seconds further along than
you left it. `over` is therefore "an announcement without the pause and resume wrapper",
not "music ducked under a voice". If what you wanted was for the music to survive
intact, use `cut`.

Both modes block for roughly the length of the audio, plus buffering. A2DP through this
chain adds a couple of hundred milliseconds and snapclient adds about a second on top,
so a drop is not instantaneous. Test the timing early if it matters to your demo.

The URL must be http or https. Music Assistant rejects `file://` and local paths for
announcements outright.

### POST /sfx/{name}

The same thing as `/drop`, from a file that is already sitting in `MUSICBOX_SFX_DIR`.
musicbox serves the file itself over HTTP and hands Music Assistant a URL it can reach,
which is what makes local files work at all here.

Default mode is `over`, same as `/drop`. The mode can go in the body or the query
string; both work, and the body wins if you send both.

```sh
curl -sS -X POST "$BOX/sfx/airhorn"
```

```sh
curl -sS -X POST "$BOX/sfx/airhorn" \
  -H 'Content-Type: application/json' \
  -d '{"mode": "cut"}'
```

```sh
curl -sS -X POST "$BOX/sfx/airhorn?mode=cut"
```

The response is the `/drop` response with the sfx name added:

```json
{
  "ok": true,
  "mode_requested": "cut",
  "mode_used": "cut",
  "fell_back": false,
  "reason": null,
  "resumed": true,
  "url": "http://127.0.0.1:8099/sfx/file/airhorn.mp3",
  "sfx": "airhorn"
}
```

The name is the filename without its extension. `airhorn.mp3` in the sfx directory is
`POST /sfx/airhorn`. The full filename works too, and matching is case insensitive.

### GET /sfx

List the names that `POST /sfx/{name}` will accept, with the URL Music Assistant will
be given for each.

```sh
curl -sS "$BOX/sfx"
```

```json
{
  "ok": true,
  "dir": "/var/lib/musicbox/sfx",
  "count": 3,
  "sfx": [
    {"name": "airhorn", "file": "airhorn.mp3", "bytes": 18342, "url": "http://127.0.0.1:8099/sfx/file/airhorn.mp3"},
    {"name": "applause", "file": "applause.mp3", "bytes": 91020, "url": "http://127.0.0.1:8099/sfx/file/applause.mp3"},
    {"name": "sadtrombone", "file": "sadtrombone.mp3", "bytes": 40118, "url": "http://127.0.0.1:8099/sfx/file/sadtrombone.mp3"}
  ]
}
```

Only files with an audio extension are listed: `.mp3 .wav .flac .ogg .m4a .aac .opus`.
A stray `.txt` in the directory is not addressable.

### GET /sfx/file/{filename}

Serves one file out of `MUSICBOX_SFX_DIR`. You will not normally call this: it exists
because Music Assistant refuses local paths for announcements, so musicbox has to hand
it an http URL and then answer the fetch itself.

It is deliberately **not** behind `MUSICBOX_TOKEN`. Music Assistant fetches the URL on
its own and has no way to present a bearer token. It only ever serves files an operator
put in the sfx directory by hand, and it is bound to a private interface.

```sh
curl -sS -o /tmp/airhorn.mp3 "$BOX/sfx/file/airhorn.mp3"
```

Note the path is the full filename here, not the bare name.

### POST /skip

Next track.

```sh
curl -sS -X POST "$BOX/skip"
```

### POST /pause

```sh
curl -sS -X POST "$BOX/pause"
```

The snapcast player does not implement pause, so Music Assistant converts this into a
stop. That is fine and expected: the queue position is recorded before it stops, which
is what makes `/resume` land in the right place. Do not leave it paused indefinitely
though, Music Assistant's own watchdog converts a 30 second pause into a real stop.

### POST /resume

```sh
curl -sS -X POST "$BOX/resume"
```

### POST /volume

Level is an integer percentage, 0 to 100.

```sh
curl -sS -X POST "$BOX/volume" \
  -H 'Content-Type: application/json' \
  -d '{"level": 45}'
```

### POST /reconnect

Tears down the Music Assistant websocket and redoes the whole sequence: connect,
authenticate, re-resolve the player. Reach for this when `/health` says
`ma_connected: false`, or after restarting the Music Assistant container. It logs the
resolved player state afterwards, so check the journal if the response looks wrong.

It does not reread configuration. The Music Assistant token is loaded once at startup,
so if you changed the token file, restart the service instead.

The call waits up to 10 seconds for the connection to come back and always answers 200;
read `ok` for whether it worked.

```sh
curl -sS -X POST "$BOX/reconnect"
```

```json
{
  "ok": true,
  "ma_connected": true,
  "player": "snapcast_musicbox",
  "player_name": "musicbox",
  "player_error": null,
  "last_error": null
}
```

## MCP server

The same box, as tools an agent can call instead of curl. It is mounted on the running
service at `/mcp`, streamable HTTP, same process and same port as everything else.

```
http://pi5:8099/mcp
```

Connect Claude Code to it over the tailnet:

```sh
claude mcp add --transport http musicbox http://pi5:8099/mcp
```

Nothing else. No SSH, no wrapper process, no token. Use the Pi's tailnet name or its
`100.x` address, whichever your machine resolves.

### The tools

| Tool | Arguments | What it does |
| --- | --- | --- |
| `play` | `uri_or_url` | Starts playing now, clearing the queue. Takes a provider URI or an http(s) URL. |
| `queue` | `uri_or_url` | Appends to the queue without interrupting. |
| `drop` | `url`, `mode` | Plays a one shot sound from an http(s) URL. `mode` is `over` or `cut`. |
| `sfx` | `name`, `mode` | The same, from a sound effect preloaded in `MUSICBOX_SFX_DIR`. |
| `list_sfx` | none | The names `sfx` accepts. |
| `now_playing` | none | Track, position, volume, queue length. |
| `skip` | none | Next track. |
| `pause` | none | Pause. |
| `resume` | none | Carry on from where pause stopped. |
| `set_volume` | `level` | 0 to 100. |
| `reconnect` | none | Re-establish the Music Assistant connection. |

`list_sfx` is also exposed as a resource, `musicbox://sfx`, so a client can see which
sounds exist without spending a tool call on it.

Every tool answers with a sentence or a small object, including when it fails. Music
Assistant being down comes back as "Music Assistant is not reachable, so nothing
happened", not as an exception. A tool that raises is a tool that derails an agent
mid-event, which is the one moment nobody has attention to spare for a stack trace.

The tools call the same internal functions the HTTP endpoints call. They do not make
HTTP requests back into their own process, so nothing here goes through the socket or
the bearer token check to reach a function that is already imported.

### `/mcp` is unauthenticated on purpose

**Set `MUSICBOX_TOKEN` and `/mcp` still takes no credentials.** Every other endpoint
starts refusing requests without `Authorization: Bearer <token>`; `/mcp` keeps answering
anyone who asks.

That is a deliberate decision, not an oversight. MCP clients handle custom headers badly
enough that a token turns a one line `claude mcp add` into a support problem, and the
port is bound to the tailnet.

Say the consequence out loud: **anyone who can reach port 8099 can play audio, skip
tracks and change the volume with no token at all.** Your tailnet ACL and
`services.musicbox.openFirewall` with `firewallInterfaces = [ "tailscale0" ]` are the
only things limiting that. Do not bind musicbox to a public interface, and if you do,
understand that you have published a speaker.

### The stdio entrypoint

The package also installs `musicbox-mcp`, a stdio MCP server presenting the identical
tool set. It does not start a musicbox: it controls one over HTTP at `MUSICBOX_URL`.
Use it only for a client that will not speak HTTP. The mounted `/mcp` endpoint is better
for anything on the tailnet, because it is one process with one connection to Music
Assistant rather than a subprocess per client.

```sh
claude mcp add musicbox-stdio --env MUSICBOX_URL=http://pi5:8099 -- musicbox-mcp
```

Unlike `/mcp`, this one goes through the front door and does need the token when one is
set. It reads `MUSICBOX_TOKEN`, or `MUSICBOX_TOKEN_FILE` if that is unset.

### When it does not connect

- `421 Misdirected Request` means the DNS rebinding protection in the MCP SDK rejected
  your `Host` header. musicbox turns that off deliberately, so seeing it means the
  running service is older than this feature. Check `curl http://pi5:8099/health` for
  the version.
- `404` on `/mcp` means the same thing: an older build with no MCP server in it.
- A client that hangs on connect is usually pointed at `http://pi5:8099` with no `/mcp`
  on the end.
- Everything else is a musicbox problem rather than an MCP one, and `GET /health`
  answers it faster than any tool will.

## Troubleshooting

### Check each link in the chain independently

Work down the chain. Do not debug Bluetooth because the API returned an error, and do
not debug the API because the speaker is silent.

```sh
# 1. musicbox itself
curl -sS http://127.0.0.1:8099/health
journalctl -u musicbox -n 50

# 2. Music Assistant is alive. /info needs no token, so this isolates
#    "server is down" from "our token is wrong".
curl -sS http://127.0.0.1:8095/info

# 3. Our token works. This is the authoritative list of commands and their
#    real argument names on whatever image actually got pulled.
curl -sS http://127.0.0.1:8095/api \
  -H "Authorization: Bearer $(sudo cat /etc/musicbox/ma-token)" \
  -H 'Content-Type: application/json' \
  -d '{"command":"players/all","args":{}}'

# 4. The snapserver inside Music Assistant is actually listening
ss -lntp | grep -E '1704|1705'
podman exec music-assistant snapserver -v

# 5. snapclient connected to it and opened its ALSA device
systemctl status snapclient
journalctl -u snapclient -n 50

# 6. bluealsa has a transport for the speaker
systemctl status bluealsa
journalctl -u bluealsa -n 50
busctl --system list | grep bluealsa

# 7. The Bluetooth link
bluetoothctl info AA:BB:CC:DD:EE:FF
```

To test audio without any of the network chain, play a file straight at the speaker.
Run it under the service user with an explicit environment, not from your own shell: a
plain `sudo -u` inherits your PATH and your groups and will give you a false all clear.

Take `ALSA_PLUGIN_DIR` from the running unit rather than from `nix eval`. The registry's
`nixpkgs#bluez-alsa` is a different store path from the one the system was built with,
and testing against a plugin the system is not using is exactly the kind of false result
this command exists to avoid.

```sh
PLUGIN_DIR=$(systemctl show snapclient -p Environment --value \
  | tr ' ' '\n' | sed -n 's/^ALSA_PLUGIN_DIR=//p')
echo "$PLUGIN_DIR"    # must be non-empty

# env -i needs an explicit PATH: with the environment cleared, execvp falls back
# to /bin:/usr/bin, which on NixOS contains almost nothing.
sudo -u snapclient env -i \
  PATH=/run/current-system/sw/bin \
  ALSA_PLUGIN_DIR="$PLUGIN_DIR" \
  mpg123 -a 'bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp' /var/lib/musicbox/sfx/airhorn.mp3
```

If that plays, the speaker, Bluetooth, and ALSA are all fine and the problem is above
them.

### Spotify refuses the account

Spotify's provider needs Premium. Free accounts do not work at all. Separately, Spotify
has been blocking third party clients on accounts created from roughly 2024 onwards,
and there is no workaround: it fails at login or plays nothing. An old Premium account
is your best chance.

Test it before you build anything on it. Add the provider in the Music Assistant UI and
play one track from the UI itself. If that fails, it will fail through musicbox too, and
the failure will show up as an error from `/play` or `/queue` rather than as a musicbox
bug.

Plan a fallback either way. Plain http and https mp3 URLs go through Music Assistant's
built-in provider and need no account, no login, and no provider setup. Everything in
`examples/` works with URLs alone.

### The speaker falls asleep and cuts the start of tracks

This is the single most common failure in this stack, and it is measurable, not
mysterious. snapclient closes its ALSA device after 5 seconds with no audio (the
timeout is hardcoded in snapcast, it is not configurable). Closing the PCM tears down
the A2DP transport, the speaker sees no stream, and it powers down its amplifier. The
next thing you play loses its first second or two while the speaker wakes up, or the
link drops entirely.

The fix is bluealsa's `--keep-alive`, which holds the Bluetooth transport open after the
PCM closes. The NixOS module sets `--keep-alive=86400`, which effectively means never.
If you are running this outside the module, that flag is the highest value thing in the
whole setup.

If it still happens, check that the flag actually made it into the running process:

```sh
systemctl show bluealsa -p ExecStart
```

### The speaker never reconnects after being switched off and on

Check `bluetoothctl info <mac>` for `Trusted: yes`. If it says no, run
`bluetoothctl trust <mac>`, see the pairing section above for why.

If it is trusted and still does not come back, BlueZ is the reason: its reconnect policy
only fires on link loss, not on a clean disconnect, and gives up after about two minutes
regardless. The module ships a `musicbox-bt-connect` watchdog that polls every 15
seconds and reissues the connect. Check it is running:

```sh
systemctl status musicbox-bt-connect
```

### Music Assistant does not see the snapcast player

In order:

1. Is the built-in snapserver even running? Music Assistant only offers it if the
   bundled binary passes a version check. Verify with
   `podman exec music-assistant snapserver -v`. If that fails, the Snapcast provider
   is expecting an external snapserver and this whole design assumed one it does not
   have.
2. Is it reachable from the host? `ss -lntp | grep 1704`. If the container is not on
   host networking, ports 1704, 1705 and 4953 to 5153 have to be published, and the
   simplest fix is host networking.
3. Is snapclient actually connecting? `journalctl -u snapclient -n 50`. It exits when
   it cannot open its ALSA device, which is normal during the window before the speaker
   reconnects, and the unit restarts it every 5 seconds. A permanent loop there means
   the speaker or bluealsa is the problem, not snapcast.
4. Is the name right? snapclient runs with `--hostID musicbox`, so the player should
   appear in Music Assistant as `musicbox`. `MUSICBOX_PLAYER` has to match a player id
   or a player name exactly, though the name match is case insensitive.

### Audio sounds like a telephone

The speaker negotiated HSP or HFP instead of A2DP, which is 8 kHz mono. bluealsa must
run with `-p a2dp-source` only. If no headset endpoint is registered, the speaker cannot
choose one. Also make sure the PCM string carries `PROFILE=a2dp` explicitly rather than
relying on a default.

### Audio stutters under load

The Pi 5's WiFi and Bluetooth share the same 2.4 GHz front end. This box is on ethernet,
so blocking the WiFi radio costs nothing and is the cheapest stability win available:

```sh
sudo rfkill block wifi
```

### Everything returns 401

`MUSICBOX_TOKEN` is set and your calls are not carrying it. Add
`-H "Authorization: Bearer $MUSICBOX_TOKEN"`. `/health` still answers 200 without it,
so if that works and everything else 401s, this is what it is. `/sfx/file/{filename}`
and `/mcp` are open too, for their own reasons: Music Assistant fetches sfx files itself
and cannot present our token, and `/mcp` is a deliberate hole.

If instead `/health` reports `ma_authenticated: false`, it is the other token, the
Music Assistant one, that is missing, wrong, or expired. Short-lived Music Assistant
tokens expire after 30 days with a hard 90 day cap, which is why the quick start trades
one for a long-lived token. Mint a new one and write it to the file `maTokenFile` points
at.

Then restart, do not `POST /reconnect`. The token is read once at startup, so
`/reconnect` reopens the socket with the same expired token and changes nothing.
`/reconnect` is for a socket that dropped, not for a credential that changed.

```sh
sudo systemctl restart musicbox
```

## Examples

`examples/` has two runnable scripts. Both are plain shell and curl, both take the
musicbox base URL as their first argument.

- `examples/event-flow.sh` is a full realistic sequence: start a playlist, let it run,
  drop an airhorn over it, cut to an mp3, restore the music, fade the volume back up.
- `examples/sfx-drop.sh` is the minimal version: check health, list the available sfx,
  fire one of them. Third argument is the mode, `cut` by default.

```sh
./examples/event-flow.sh http://pi5:8099
./examples/sfx-drop.sh http://pi5:8099 airhorn
```

## License

MIT. See [LICENSE](LICENSE).
