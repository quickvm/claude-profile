#!/bin/bash
# krun boots the microVM as root and ignores the image USER. Drop to a non-root
# user before exec'ing the command: claude refuses --dangerously-skip-permissions
# as root, and this keeps files on the bind mounts owned by the host user.
set -e

if [ "$(id -u)" = "0" ]; then
  HOST_UID="${HOST_UID:-1000}"
  HOST_GID="${HOST_GID:-1000}"
  groupmod -o -g "$HOST_GID" appuser 2>/dev/null || true
  usermod -o -u "$HOST_UID" appuser 2>/dev/null || true
  export HOME=/home/appuser
  # Agent socket paths live under root-owned trees (e.g. /run/user/..., the in-VM
  # GNUPGHOME); create and hand their parents to the host user before dropping.
  if [ -n "$CLAUDE_SANDBOX_FORWARDS" ]; then
    IFS=',' read -ra _forwards <<<"$CLAUDE_SANDBOX_FORWARDS"
    for _fwd in "${_forwards[@]}"; do
      _dir="$(dirname "${_fwd%=*}")"
      [ -n "$_dir" ] && mkdir -p "$_dir" && chown "$HOST_UID:$HOST_GID" "$_dir"
    done
  fi
  # Re-exec self as the host user so the agent bridges below run as that user.
  exec runuser -u appuser -- "$0" "$@"
fi

# Bridge each forwarded agent: a guest socket that relays to the host's socat over
# pasta (CLAUDE_SANDBOX_FORWARDS = "guest_path=port,guest_path=port").
if [ -n "$CLAUDE_SANDBOX_FORWARDS" ]; then
  IFS=',' read -ra _forwards <<<"$CLAUDE_SANDBOX_FORWARDS"
  for _fwd in "${_forwards[@]}"; do
    _path="${_fwd%=*}"
    _port="${_fwd##*=}"
    [ -n "$_path" ] && [ -n "$_port" ] || continue
    socat "UNIX-LISTEN:${_path},fork,unlink-early" \
      "TCP:host.containers.internal:${_port}" &
  done
fi

# Claude in Chrome bridge: claude scans /tmp/claude-mcp-browser-bridge-<user> for a
# native-host socket and connects out to it. Present one there that relays over pasta
# to the host bridge (claude_profile._start_browser_host_bridge). claude's
# validateSocketSecurity requires the dir be mode 0700 owned by the user AND the socket
# itself be mode 0600 (it rejects and reports "not detected" otherwise), so create the
# dir 0700 and pass perm=0600 to socat. claude scans at startup, so bind before exec.
if [ -n "$CLAUDE_SANDBOX_BROWSER_BRIDGE_PORT" ]; then
  _bdir="/tmp/claude-mcp-browser-bridge-$(id -un)"
  mkdir -p "$_bdir"
  chmod 700 "$_bdir"
  socat "UNIX-LISTEN:${_bdir}/host.sock,fork,unlink-early,perm=0600" \
    "TCP:host.containers.internal:${CLAUDE_SANDBOX_BROWSER_BRIDGE_PORT}" &
  _tries=0
  while [ ! -S "${_bdir}/host.sock" ] && [ "$_tries" -lt 50 ]; do
    sleep 0.1
    _tries=$((_tries + 1))
  done

  # claude detects the extension by looking for the native-messaging manifest on the local
  # filesystem, which a VM with no Chrome install lacks — so /chrome reports "Extension: Not
  # detected" even though the bridge works, and picking "Install Chrome extension" (which just
  # writes this file) is needed every session because the VM is ephemeral. Write it up front.
  # The wrapper it names is only read by Chrome, which isn't here; the real connection is the
  # bridged socket above. Both land in throwaway VM paths (~/.claude/chrome is masked by
  # claude_profile._sandbox_chrome_overlay), so the host's wrapper is never touched.
  _nmdir="$HOME/.config/google-chrome/NativeMessagingHosts"
  mkdir -p "$_nmdir" "$HOME/.claude/chrome"
  printf '#!/bin/sh\nexec "%s" --chrome-native-host\n' "$(command -v claude)" \
    >"$HOME/.claude/chrome/chrome-native-host"
  chmod +x "$HOME/.claude/chrome/chrome-native-host"
  cat >"$_nmdir/com.anthropic.claude_code_browser_extension.json" <<JSON
{
  "name": "com.anthropic.claude_code_browser_extension",
  "description": "Claude Code Browser Extension Native Host",
  "path": "$HOME/.claude/chrome/chrome-native-host",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://fcoeoabgfenejglbffodgkkbkcdhcgfn/"
  ]
}
JSON
fi

# Seed a fresh GNUPGHOME with the host's public keys; signing uses the forwarded
# gpg-agent (the secret keys/card stay on the host).
if [ -n "$CLAUDE_SANDBOX_GPG_PUBKEYS" ]; then
  mkdir -p "${GNUPGHOME:-$HOME/.gnupg}"
  chmod 700 "${GNUPGHOME:-$HOME/.gnupg}"
  printf '%s' "$CLAUDE_SANDBOX_GPG_PUBKEYS" | base64 -d | gpg --batch --import 2>/dev/null || true
fi

exec "$@"
