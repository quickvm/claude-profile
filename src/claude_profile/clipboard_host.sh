#!/usr/bin/env bash
# Host side of the sandbox clipboard bridge (claude_profile._start_clipboard_host_bridge).
#
# socat execs one copy of this per guest connection, wiring the socket to our
# stdin/stdout. We read a single request line — the wl-paste arguments the sandbox
# asked for — allow only read-only invocations, then stream the real clipboard
# bytes back. Anything outside the whitelist yields an empty response, so a
# misbehaving sandbox can read the clipboard but cannot run other host commands.
set -euo pipefail

IFS= read -r request || exit 0
read -r -a args <<<"$request"

for arg in "${args[@]}"; do
  case "$arg" in
  -l | --list-types | -t | --type | -n | --no-newline) ;;
  image/* | text/*) ;;
  *) exit 0 ;;
  esac
done

exec wl-paste "${args[@]}"
