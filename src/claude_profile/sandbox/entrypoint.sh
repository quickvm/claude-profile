#!/bin/bash
# krun boots the microVM as root and ignores the image USER. Drop to a non-root
# user before exec'ing the command: claude refuses --dangerously-skip-permissions
# as root, and this keeps files on the bind mounts owned by the host user.
set -e

if [ "$(id -u)" = "0" ]; then
  HOST_UID="${HOST_UID:-1000}"
  HOST_GID="${HOST_GID:-1000}"
  # Files the agent writes to the mounts get appuser's IDs, so a failed remap would give
  # them the wrong owner on the host. Skip when already right (both tools complain
  # about "no changes" otherwise).
  if [ "$(id -g appuser)" != "$HOST_GID" ]; then
    groupmod -o -g "$HOST_GID" appuser ||
      echo "warning: could not give appuser GID $HOST_GID;" \
        "files written to the mounts get the wrong group" >&2
  fi
  if [ "$(id -u appuser)" != "$HOST_UID" ]; then
    usermod -o -u "$HOST_UID" appuser ||
      echo "warning: could not give appuser UID $HOST_UID;" \
        "files written to the mounts get the wrong owner" >&2
  fi
  export HOME=/home/appuser
  # claude-profile mounts the host's own claude binary at /opt/claude-host/claude, so the
  # VM runs the version the host is on rather than the one baked into the image at build
  # time. Point the PATH entry at it. Absent for non-native host installs, in which case
  # the image's claude stays in place.
  if [ -x /opt/claude-host/claude ]; then
    ln -sf /opt/claude-host/claude /home/appuser/.local/bin/claude
  fi
  # claude-profile bind-mounts the host's custom CA anchors over the image's (empty)
  # anchor dir. Anchors are only source material: nothing reads them until
  # update-ca-trust regenerates the extracted bundles that curl/git/openssl consume.
  if [ -n "$(ls -A /etc/pki/ca-trust/source/anchors 2>/dev/null)" ]; then
    update-ca-trust extract ||
      echo "warning: update-ca-trust failed; the host's CAs are not trusted" >&2
  fi
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

# claude reports "Extension: Installed" by readdir'ing
# <chrome-user-data>/<profile>/Extensions/<extension-id>. A VM with no Chrome install always
# fails that, so /chrome shows "Extension: Not detected" even though the bridge works and the
# extension really is installed — on the host, where Chrome runs. claude-profile passes the
# path to create only when it verified the extension on the host, so the status stays honest.
# Only the directory's existence is checked, so an empty dir suffices; the host's Chrome
# profile (cookies, history, passwords) is deliberately never exposed to the VM.
if [ -n "$CLAUDE_SANDBOX_CHROME_EXT_PATH" ]; then
  mkdir -p "$CLAUDE_SANDBOX_CHROME_EXT_PATH"
fi

# Node and Python ship their own CA bundles and ignore the system trust store, so the
# anchors extracted above would still leave `npx` MCP servers and agent scripts failing
# TLS against internal hosts. Point each at the extracted bundle, which carries the
# image's public CAs plus the host's. An explicitly forwarded value wins.
_ca_bundle=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
if [ -n "$(ls -A /etc/pki/ca-trust/source/anchors 2>/dev/null)" ] && [ -f "$_ca_bundle" ]; then
  export NODE_EXTRA_CA_CERTS="${NODE_EXTRA_CA_CERTS:-$_ca_bundle}"
  export SSL_CERT_FILE="${SSL_CERT_FILE:-$_ca_bundle}"
  export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$_ca_bundle}"
fi

# Seed a fresh GNUPGHOME with the host's public keys; signing uses the forwarded
# gpg-agent (the secret keys/card stay on the host). The keyring arrives as a mounted
# file: a whole keyring can outgrow the 128 KiB Linux allows a single env variable.
if [ -n "$CLAUDE_SANDBOX_GPG_PUBKEYS_FILE" ]; then
  _gnupghome="${GNUPGHOME:-$HOME/.gnupg}"
  mkdir -p "$_gnupghome"
  chmod 700 "$_gnupghome"
  # S.gpg-agent here is the bridge socket, not a real agent. Whenever the host end is
  # briefly unreachable — the host gpg-agent restarting is enough — a request over it
  # returns EOF, and gpg's default autostart answers that by launching a local agent,
  # which unlinks the bridge socket and binds its own. The bridge keeps listening on an
  # orphaned inode, so every later gpg call silently reaches the local keyless agent and
  # signing stays broken for the rest of the session even once the host recovers.
  # no-autostart makes that outage a plain transient failure the next call recovers from.
  # It also stops dirmngr autostarting (keyserver lookups need an explicit
  # `gpgconf --launch dirmngr`), which the forwarded-agent setup does not rely on.
  printf 'no-autostart\n' >"$_gnupghome/gpg.conf"
  gpg --batch --import "$CLAUDE_SANDBOX_GPG_PUBKEYS_FILE" 2>/dev/null ||
    echo "warning: could not import the host's GPG public keys; signing may fail" >&2
fi

exec "$@"
