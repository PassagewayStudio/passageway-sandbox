#!/bin/bash
set -e

# Home is a tmpfs, so seed it fresh every session
cp -a /opt/skel/. "$HOME"/

# -W writable terminal. BASE_PATH (e.g. /term) is set by the gateway so the
# terminal works behind a URL prefix. The session ends when the gateway removes
# the container (timeout or "End session"), so a page refresh doesn't kill it.
ARGS=(-W -p 7681 -t fontSize=16 -t "titleFixed=Passageway Sandbox")
if [ -n "${BASE_PATH:-}" ]; then
  ARGS+=(-b "$BASE_PATH")
fi
exec ttyd "${ARGS[@]}" bash -l
