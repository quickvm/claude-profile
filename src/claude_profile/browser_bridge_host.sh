#!/usr/bin/env bash
# Host side of the sandbox Claude-in-Chrome bridge
# (claude_profile._start_browser_host_bridge).
#
# socat execs one copy of this per guest connection, wiring the guest socket to
# our stdin/stdout. Chrome spawns a `claude --chrome-native-host` that binds
# /tmp/claude-mcp-browser-bridge-<user>/<pid>.sock; its pid changes on every spawn
# and a crashed host leaves the socket behind, so we resolve the newest socket per
# connection and relay to it. The in-VM claude reconnects on drops, so each attempt
# re-runs this and follows the native host across Chrome restarts.
set -euo pipefail

dir="/tmp/claude-mcp-browser-bridge-$(id -un)"

newest=""
while IFS= read -r sock; do
  newest="$sock"
  break
done < <(find "$dir" -maxdepth 1 -type s -name '*.sock' -printf '%T@ %p\n' 2>/dev/null |
  sort -rn | cut -d' ' -f2-)

# No native host bound a socket (Chrome closed or the extension not connected):
# exit cleanly so the guest connection just closes and claude reconnects later.
[ -n "$newest" ] || exit 0

exec socat - "UNIX-CONNECT:$newest"
