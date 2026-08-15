#!/usr/bin/env bash
#
# The smallest useful musicbox script: check the box is alive, then fire one
# preloaded sound effect over whatever is playing.
#
#   ./examples/sfx-drop.sh http://pi5:8099 airhorn
#   ./examples/sfx-drop.sh http://pi5:8099 airhorn cut
#
# Set MUSICBOX_TOKEN in your environment if the server was started with one.

set -euo pipefail

BOX="${1:-http://127.0.0.1:8099}"
NAME="${2:-airhorn}"
MODE="${3:-cut}"

# Every call carries the bearer header only when a token is actually set.
# Passing an empty Authorization header makes some proxies reject the request
# outright, which is a confusing thing to debug at an event.
auth=()
if [ -n "${MUSICBOX_TOKEN:-}" ]; then
  auth=(-H "Authorization: Bearer ${MUSICBOX_TOKEN}")
fi

say() { printf '\n== %s\n' "$1"; }

say "health"
# --fail-with-body so a 401 or a 500 stops the script instead of silently
# printing an error body and carrying on to play nothing.
curl -sS --fail-with-body "${auth[@]}" "$BOX/health"
echo

say "available sfx"
curl -sS --fail-with-body "${auth[@]}" "$BOX/sfx"
echo

say "dropping '$NAME' in mode '$MODE'"
# This call blocks for roughly the length of the audio. That is not the script
# hanging, it is musicbox waiting for Music Assistant to report the
# announcement finished before it resumes the queue.
#
# The mode goes in the body here, the same shape /drop takes. It also works as
# ?mode=cut in the query string; the body wins if both are given. Read
# mode_used in the response rather than assuming: "over" silently falls back to
# "cut" when the player cannot do announcements.
curl -sS --fail-with-body "${auth[@]}" \
  -X POST "$BOX/sfx/$NAME" \
  -H 'Content-Type: application/json' \
  -d "{\"mode\": \"$MODE\"}"
echo
