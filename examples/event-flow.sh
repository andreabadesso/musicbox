#!/usr/bin/env bash
#
# A realistic event sequence, end to end:
#
#   1. start the background playlist
#   2. let it settle, then look at what is playing
#   3. drop an airhorn over the top
#   4. cut to a full mp3 (music pauses, mp3 plays, music resumes where it was)
#   5. bring the volume back up and confirm the music really did come back
#
# Usage:
#   ./examples/event-flow.sh http://pi5:8099
#
# Environment:
#   MUSICBOX_TOKEN   bearer token, if the server was started with one
#   PLAYLIST         what to start with. Defaults to a plain URL so this script
#                    works with no Spotify account at all. A Spotify playlist
#                    URI works here too, see the two PLAYLIST lines below.
#   AIRHORN          url of the short drop
#   STINGER          url of the longer mp3 to cut to

set -euo pipefail

BOX="${1:-http://127.0.0.1:8099}"

# Default to URLs, not Spotify. Spotify needs Premium and blocks a lot of newer
# accounts, so a demo that depends on it has a failure mode you cannot fix in
# the room. Swap this for the URI line once you have confirmed Spotify works.
PLAYLIST="${PLAYLIST:-https://example.com/audio/background.mp3}"
# PLAYLIST="${PLAYLIST:-spotify://playlist/37i9dQZF1DXcBWIGoYBM5M}"

AIRHORN="${AIRHORN:-https://example.com/audio/airhorn.mp3}"
STINGER="${STINGER:-https://example.com/audio/stinger.mp3}"

auth=()
if [ -n "${MUSICBOX_TOKEN:-}" ]; then
  auth=(-H "Authorization: Bearer ${MUSICBOX_TOKEN}")
fi

say() { printf '\n== %s\n' "$1"; }

# post <path> [json-body]
post() {
  local path="$1"; shift
  if [ "$#" -gt 0 ]; then
    curl -sS --fail-with-body "${auth[@]}" \
      -X POST "$BOX$path" -H 'Content-Type: application/json' -d "$1"
  else
    curl -sS --fail-with-body "${auth[@]}" -X POST "$BOX$path"
  fi
  echo
}

get() {
  curl -sS --fail-with-body "${auth[@]}" "$BOX$1"
  echo
}

# Anything below this line is pointless if the Music Assistant connection is
# down, and the errors you get in that state point at the wrong layer. Check
# first and bail loudly.
say "health"
health=$(curl -sS --fail-with-body "${auth[@]}" "$BOX/health")
echo "$health"
case "$health" in
  *'"ma_connected": true'*|*'"ma_connected":true'*) ;;
  *)
    echo "musicbox is not connected to Music Assistant. Try: curl -X POST $BOX/reconnect" >&2
    exit 1
    ;;
esac
# Connected and authenticated are different failures, and only the second one
# lets every call below fail with a 502 that looks like a musicbox bug.
case "$health" in
  *'"ma_authenticated": true'*|*'"ma_authenticated":true'*) ;;
  *)
    echo "musicbox reached Music Assistant but its token was refused." >&2
    echo "Mint a long-lived token (auth/token/create), write it to the file" >&2
    echo "MUSICBOX_MA_TOKEN_FILE points at, and restart musicbox." >&2
    exit 1
    ;;
esac

# Choose the field name by shape. /play and /queue take either "uri" or "url",
# and a Music Assistant URI is not a URL, so it has to go in the other field.
case "$PLAYLIST" in
  http://*|https://*) start_body="{\"url\": \"$PLAYLIST\"}" ;;
  *)                  start_body="{\"uri\": \"$PLAYLIST\"}" ;;
esac

say "start the background music"
post /play "$start_body"

say "set a background level"
post /volume '{"level": 55}'

# Snapclient buffers about a second and A2DP adds a couple of hundred
# milliseconds on top, so asking /now immediately after /play will show you a
# queue that has not started moving yet. Give it a moment.
sleep 5

say "what is playing"
get /now

say "airhorn over the top"
# "over" hands this to Music Assistant as an announcement and leaves the queue
# alone. The music is NOT ducked underneath, Music Assistant 2.9 has no ducking:
# the audio source is swapped for the length of the drop and swapped back, so
# you rejoin the track a few seconds further along. Use it when a small skip
# forward is fine and you want the queue untouched.
post /drop "{\"url\": \"$AIRHORN\", \"mode\": \"over\"}"

sleep 3

say "cut to the stinger"
# "cut" is the one that preserves the music: pause, play, resume from the
# recorded position. This call blocks for the length of the stinger.
post /drop "{\"url\": \"$STINGER\", \"mode\": \"cut\"}"

say "back to the music"
# After a "cut" the queue should already be playing again. Resume is harmless
# if it is, and it is what rescues you if the resume did not take, which does
# happen because the snapcast player implements pause as a stop.
post /resume
post /volume '{"level": 70}'

sleep 3

say "confirm the music actually came back"
get /now

# Handy tail for a live demo, uncomment as needed:
#
#   post /skip
#   post /queue "{\"url\": \"$STINGER\"}"
#   post /pause
#   post /reconnect
