#!/usr/bin/env bash
# Host side of the sandbox Claude-in-Chrome browser-open bridge
# (claude_profile._start_browser_open_host_bridge).
#
# socat execs one copy of this per guest connection, wiring the guest socket to our
# stdin/stdout. The sandboxed Claude Code shells out to a `google-chrome` shim to open
# its extension connect/reconnect page (which wakes the host extension); the shim
# relays the URL here. We open it in the host's Chrome — but ONLY Anthropic's
# clau.de/claude.ai chrome URLs, so a misbehaving sandbox cannot open arbitrary pages
# in the host's logged-in browser.
set -euo pipefail

IFS= read -r url || exit 0

case "$url" in
https://clau.de/chrome* | https://claude.ai/chrome*) ;;
*)
  printf 'NO\n'
  exit 0
  ;;
esac

chrome="$(command -v google-chrome 2>/dev/null || command -v google-chrome-stable 2>/dev/null || true)"
[ -n "$chrome" ] || chrome="/opt/google/chrome/chrome"
if [ -x "$chrome" ]; then
  setsid "$chrome" "$url" >/dev/null 2>&1 &
  printf 'OK\n'
else
  printf 'NO\n'
fi
