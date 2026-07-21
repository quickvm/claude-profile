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
fi

# Seed a fresh GNUPGHOME with the host's public keys; signing uses the forwarded
# gpg-agent (the secret keys/card stay on the host).
if [ -n "$CLAUDE_SANDBOX_GPG_PUBKEYS" ]; then
  mkdir -p "${GNUPGHOME:-$HOME/.gnupg}"
  chmod 700 "${GNUPGHOME:-$HOME/.gnupg}"
  printf '%s' "$CLAUDE_SANDBOX_GPG_PUBKEYS" | base64 -d | gpg --batch --import 2>/dev/null || true
fi

exec "$@"
