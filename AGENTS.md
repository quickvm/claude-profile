# claude-profile — Agent Instructions

## Project overview

`claude-profile` is a small CLI tool that launches Claude Code with an isolated
`CLAUDE_CONFIG_DIR` per named profile. Each profile stores its own credentials,
session history, and settings, letting users maintain multiple Claude accounts
without re-authenticating.

The entire implementation lives in `src/claude_profile/__init__.py`. Keep it
small and focused — this is not a framework.

## Dev setup

```sh
uv venv
uv pip install -e ".[dev]"
```

## Toolchain

| purpose | command |
|---------|---------|
| lint | `uv run ruff check src/ tests/` |
| format | `uv run ruff format src/ tests/` |
| type check | `uv run ty check` |
| tests | `uv run pytest -q` |

Run all checks before committing:

```sh
uv run ruff check src/ && uv run ruff format --check src/ && uv run ty check && uv run pytest -q
```

## Architecture

- **Entry point:** `main()` in `src/claude_profile/__init__.py` — dispatches to
  typer subcommands or calls `_launch_profile()` directly for unknown first args. The subcommand
  names live in the module-level `KNOWN_COMMANDS`; every `@app.command` must be listed there or it
  is shadowed by the profile-launch path (a test enforces the two stay in sync).
- **Profile isolation:** `CLAUDE_CONFIG_DIR` env var is set to the profile
  directory before `os.execvpe()` replaces the process with `claude`. No wrapper
  process remains — this is intentional for correct TUI behavior.
- **Settings:** `pydantic-settings` reads `CLAUDE_PROFILE_*` env vars. No config
  file on disk.
- **Linkable dirs:** `LINKABLE_DIRS = ("commands", "skills")` centralises which
  directories can be symlinked to `~/.claude`. The `add` command prompts once per
  dir at creation time. The `links` subcommand manages them afterwards.
  `_launch_profile()` never creates symlinks implicitly.
- **`_setup_dir_link(profile_dir, dir_name, link)`:** helper used by `add` — either
  creates a symlink to the global dir (warns and skips if global dir is absent) or
  creates an empty isolated directory.
- **`links` subcommand:** accepts an optional positional `DIR` argument (one of
  `LINKABLE_DIRS`) and `--link` / `--unlink` bool flags. When `DIR` is omitted the
  operation applies to all linkable dirs. Shows a status table when neither flag is
  given.
- **`env` subcommand:** manages per-profile environment variables stored in
  `<profile_dir>/.env`. Supports `--set KEY=VALUE` (repeatable) and `--unset KEY`
  (repeatable). With no flags, displays a table of current variables.
  `_launch_profile()` loads `.env` into the environment before `execvpe`.
- **Sandbox mode (microVM):** a profile marked with a `.sandbox` file (created by
  `add --sandbox`) launches inside a podman + `krun` microVM instead of on the host.
  `_launch_profile()` checks the marker and routes to `_launch_sandbox()`, which
  `os.execvpe`s `podman run --annotation run.oci.handler=krun …` in place of `claude`.
- **`build` command:** builds the sandbox image (`settings.sandbox_image`) from the
  packaged build context `src/claude_profile/sandbox/` (`Containerfile` +
  `entrypoint.sh`, shipped as wheel data, located via `importlib.resources`).
- **Sandbox launch (`_build_sandbox_argv`):** mounts the CWD at its real host path
  (`-v $PWD:$PWD`, so each `wt` worktree keeps a distinct claude session key), the git
  common dir when it lives outside the CWD (so a worktree's `git`/`wt` work), and the
  profile dir at the in-VM config dir. VM sizing uses `krun.ram_mib`/`krun.cpus`
  annotations, not `--memory`/`--cpus`. `.env` vars plus `TERM`/`COLORTERM` are
  forwarded with `-e`; the rest of the host environment is not.
- **Linked commands/skills:** `_linked_dir_mounts()` bind-mounts the real target of any
  symlinked `LINKABLE_DIRS` (e.g. `skills` → `~/.claude/skills`) read-only at the link's
  path, so the symlink resolves inside the VM without exposing the global dir writable.
- **Non-root entrypoint is mandatory:** krun boots the VM as root and ignores the
  image `USER`; claude refuses `--dangerously-skip-permissions` as root, so
  `entrypoint.sh` drops to a host-UID user (`runuser`) before exec'ing claude. Sandbox
  mode auto-appends that flag unless `sandbox_skip_permissions` is false or the user
  already passed it.
- **Sandbox settings:** `podman_bin`, `sandbox_image`, `sandbox_ram_mib`, `sandbox_cpus`,
  `sandbox_skip_permissions`, `sandbox_ssh_agent`, `sandbox_gpg_agent`, `sandbox_clipboard`,
  `sandbox_chrome`, `sandbox_gh`, `sandbox_infisical`, `sandbox_pulumi`, `sandbox_forward_env`
  (all read from `CLAUDE_PROFILE_*`).
- **Agent self-provisioning:** the image bakes common dev tools (`uv`, jq/yq,
  python+pyyaml/jinja2, make/openssl/trash) plus `dnf`-scoped passwordless sudo, so the
  agent installs missing tools ad-hoc (`sudo dnf install`, `uv tool install`; ephemeral).
  `_build_sandbox_argv` appends `SANDBOX_BRIEFING` via `--append-system-prompt` so the
  agent knows it is sandboxed. The `sandbox-skill` command generates the `sandbox-tools`
  SKILL.md from `SANDBOX_SKILL_TOOLS` filtered by what the image actually has (introspected
  via `podman run … command -v`); `--check` detects drift. Template lives at
  `src/claude_profile/sandbox_skill_template.md`.
- **Nested containers (podman):** the image installs `podman` + `fuse-overlayfs`, scopes
  passwordless sudo to `/usr/bin/podman` (with the `SETENV` tag), and ships
  `/etc/containers/storage.conf` pinning fuse-overlayfs. Containers run **rootful**: a `podman`
  wrapper at `/usr/local/bin/podman` (ahead of `/usr/bin` on PATH) execs `sudo -E /usr/bin/podman`,
  so bare `podman` — including `podman run …` MCP servers — works without config changes; `sudo -E`
  (needing SETENV) preserves the environment so those servers' pass-through `-e VAR` tokens survive.
  The krun guest kernel sees uid 0 as real root, so layer unpack works; rootless podman can't map
  the nested user namespace (`newuidmap … Operation not permitted`) and native overlay isn't
  backable on the virtiofs root.
  `run`/`build`/`pull`, TCP egress, external DNS, and container-to-container name resolution all
  work, because `_build_sandbox_argv` runs the microVM with `krun.use_passt=1` (a real virtio-net
  guest netstack). Without it libkrun defaults to TSI socket impersonation, whose stubbed
  `setsockopt` reads `SO_REUSEADDR` back as 0 (aborting gRPC) and whose AF_INET interception breaks
  container DNS — see the passt-networking note on `_build_sandbox_argv`.
- **Sandbox settings overlay:** the profile's `settings.json` is copied from the host and carries
  host-oriented deny rules; `deny` wins even under `--dangerously-skip-permissions`, so a blanket
  `Bash(sudo *)` deny blocks the sandbox's own scoped `sudo dnf`/`sudo podman`.
  `_sandbox_settings_overlay` writes `settings.sandbox.json` (the profile settings with any deny
  matching `SANDBOX_STRIP_DENY_PREFIXES` — currently `Bash(sudo` — removed) and `_sandbox_mounts`
  bind-mounts it over `settings.json` **inside the VM only**, read-write so in-VM setting writes
  hit the throwaway overlay (regenerated each launch), not the real profile settings. Host launches
  are untouched and keep the sudo deny.
- **SSH agent forwarding (`sandbox_ssh_agent`):** a microVM can't bind-mount the agent
  socket (separate kernel), so `_ssh_agent_sockets()` probes the candidates (the active
  `SSH_AUTH_SOCK` agent + 1Password) with `ssh-add -l`, drops dead ones (a stale socket
  whose agent doesn't answer would surface in-VM as "communication with agent failed") and
  orders keyed agents first so the VM's `SSH_AUTH_SOCK` holds keys. `_start_host_bridge()`
  runs a host `socat` (TCP on 127.0.0.1 → the agent socket), and
  `--network=pasta:--map-host-loopback,…` lets the guest reach it; `entrypoint.sh` starts a
  guest `socat` per forward and sets `SSH_AUTH_SOCK`. Because the bridges need teardown,
  this path supervises podman (`_run_sandbox_supervised`) instead of `execvpe`.
- **known_hosts persistence:** `_sandbox_mounts` mounts the host's `~/.ssh/known_hosts`
  read-only as the VM's *global* known_hosts (`/etc/ssh/ssh_known_hosts`, for verification)
  and a per-profile writable `known_hosts` (`_sandbox_known_hosts`) as the *user* file, so
  ssh records newly accepted host keys there and they persist across launches — the host's
  real file is never written by the sandbox.
- **GPG agent forwarding (`sandbox_gpg_agent`):** same bridge, forwarding the host
  gpg-agent restricted socket (`gpgconf --list-dirs agent-extra-socket`) to the in-VM
  `GNUPGHOME/S.gpg-agent` and seeding a fresh GNUPGHOME with the host's public keys
  (`gpg --export`, base64 via env, imported by `entrypoint.sh`). Signing runs on the host,
  so secret keys/smartcard never enter the VM. `_sandbox_mounts` also bind-mounts
  `~/.gitconfig` read-only so signing config applies. `_Forwarding` carries
  `(host_socket, guest_path, port)` tuples shared by both SSH and GPG plus a
  `clipboard_port` for the clipboard bridge; `active()` reports whether any host bridge is
  needed. The in-VM env (`CLAUDE_SANDBOX_FORWARDS`, `SSH_AUTH_SOCK`, `GNUPGHOME`,
  `CLAUDE_SANDBOX_GPG_PUBKEYS`, `CLAUDE_SANDBOX_CLIPBOARD_PORT`) is built by `_forwarding_env`.
- **Clipboard bridge (`sandbox_clipboard`):** Claude Code reads a pasted image on Linux by
  shelling out to `wl-paste`/`xclip`, but the microVM has no display. Rather than forward the
  whole Wayland compositor (waypipe would also hand the sandbox screen capture and keystroke
  injection — a practical escape), a scoped bridge carries only clipboard bytes: an in-VM
  `wl-paste` shim (`src/claude_profile/sandbox/wl-paste`) relays its args over pasta to a host
  `socat` that execs `clipboard_host.sh`, which whitelists read-only `wl-paste` invocations and
  streams the bytes back. `_build_forwarding` allocates the port, `_forwarding_env` exports it as
  `CLAUDE_SANDBOX_CLIPBOARD_PORT`, and `_start_clipboard_host_bridge` (started/torn down by
  `_run_sandbox_supervised`, which also verifies the host has `wl-paste`) serves it. Read-only:
  the sandbox reads the clipboard but cannot write it or reach any other Wayland protocol. No
  entrypoint or image-package changes — the shim is inert unless the port env is set.
- **Claude in Chrome bridge (`sandbox_chrome`):** the `mcp__claude-in-chrome__*` tools drive a
  browser through a native host Chrome spawns on the host (`claude --chrome-native-host`, wired via
  the `com.anthropic.claude_code_browser_extension` native-messaging manifest). That native host
  **binds** a Unix socket at `/tmp/claude-mcp-browser-bridge-<user>/<pid>.sock`; the interactive
  claude session is the client — it scans that dir, connects out, and (per its `validateSocketSecurity`)
  requires the dir be mode `0700` owned by the current uid AND the socket itself be mode `0600` (it
  throws "Insecure socket permissions (expected 0600)" and reports the extension as "Not detected"
  otherwise — which is why the real native host binds its socket `srw-------`). The microVM has its own
  kernel, so it can't reach the host socket directly. Discovery is inverted vs. the ssh/gpg bridges
  (guest connects, not the host), so the entrypoint presents a guest-side socket: it creates
  `/tmp/claude-mcp-browser-bridge-$(id -un)` (the VM user is `appuser`) at mode `0700` and
  `socat UNIX-LISTEN … host.sock,perm=0600` → `TCP:host.containers.internal:$port`
  over pasta, waiting for the socket to bind before exec'ing claude (claude scans at startup). On the
  host, `_start_browser_host_bridge` runs `socat TCP-LISTEN:$port … EXEC:python3 browser_bridge_host.py`;
  the proxy picks the **newest** live native-host socket per connection and relays the framed messages,
  so the bridge follows Chrome across native-host restarts (its pid changes each spawn) and claude's
  reconnect loop self-heals if Chrome starts after the VM. `_build_forwarding` allocates the port
  (warning if no native host is currently listening), `_forwarding_env` exports
  `CLAUDE_SANDBOX_BROWSER_BRIDGE_PORT`, and `_run_sandbox_supervised` starts/tears down the host bridge.
  Nothing but the framed native-messaging relay crosses the boundary — the host filesystem and other
  browser state stay out of the VM.
- **Service-worker keepalive (`browser_bridge_host.py`):** Chrome's MV3 service worker goes idle after
  ~30s, which closes the native-messaging port and kills the native host, so browser tools break after
  any idle gap (upstream anthropics/claude-code #16350, #61347 — the keepalive fix requests were
  stale-closed unfixed). The bridge is a Python proxy rather than a dumb socat relay so it can work
  around this: the native host is a transparent bridge to the extension service worker (reading the
  extension's `service-worker.ts` shows every native message hits its `onMessage` handler — an
  `execute_tool` runs a real browser tool, any other method round-trips as
  `{"result":{"content":"Unknown method: X"}}` **from the service worker**), and processing an event
  resets the MV3 idle timer. So during idle gaps (>20s of no traffic) the proxy injects a keepalive
  whose method name is distinctive; the service worker echoes that name back in its "Unknown method"
  reply, which lets the proxy swallow its own keepalive responses so the in-VM claude never sees them.
  Verified end-to-end: the native host survives 100s+ of idle through the proxy (vs ~30s bare). This
  makes the sandbox's browser connection *more* reliable than a plain host session, which has no
  keepalive.
  The native host only exists while Chrome's extension holds its native-messaging port; that spawn is
  triggered CLI-side by Claude Code opening a connect page (`clau.de/chrome/reconnect`) in a browser,
  and the extension's service worker idles (dropping the link) — both upstream behaviors the socket
  bridge can't fix on its own, which is what the browser-open shim below addresses.
- **Browser-open shim (part of `sandbox_chrome`):** the socket bridge is useless if nothing spawns the
  host native host, and the VM has no browser to open the connect/reconnect page that wakes the
  extension. Claude Code detects a browser with `which google-chrome` and opens URLs by running
  `google-chrome <url>`, so the image ships a `google-chrome` shim (+ `google-chrome-stable` symlink)
  at `/usr/local/bin` (`src/claude_profile/sandbox/google-chrome`) that relays the URL over pasta
  (bash `/dev/tcp`, no guest socat) to a second host bridge. `_start_browser_open_host_bridge` runs
  `socat TCP-LISTEN:$port … EXEC:bash browser_open_host.sh`, which opens the URL in the host's real
  Chrome **only** for Anthropic's `clau.de`/`claude.ai` `/chrome` URLs (so a misbehaving sandbox can't
  open arbitrary pages in the host's logged-in browser), and acks `OK`/`NO`. `sandbox_chrome` allocates
  both ports; `_forwarding_env` exports `CLAUDE_SANDBOX_BROWSER_OPEN_PORT` (the shim is inert without
  it, so browser detection stays harmless when disabled). Net effect: `/chrome` → "Reconnect extension"
  inside the sandbox opens the page in host Chrome, waking the extension so it spawns the native host
  the socket bridge then relays to.
- **Chrome enablement gates (why it says "Disabled"):** claude gates Claude in Chrome behind
  several checks, in this order: an **OAuth scope** check (`KYn()` — the token must carry one of
  `user:profile`/`user:office`/`user:ccr_inference`), then the `--chrome` flag, then
  `CLAUDE_CODE_ENABLE_CFC`, then `dn()` (`!isInteractive`), and only then the profile's
  `claudeInChromeDefaultEnabled`. Two of those bite the sandbox: a profile authenticated with a
  **setup-token gets `user:inference` only**, so Chrome reports "Disabled" no matter what the
  bridge does (fix: `CLAUDE_PROFILE_SANDBOX=0 claude-profile <name> /login` for a full OAuth
  login — `_warn_missing_chrome_scope` checks this at launch and points at the fix); and the
  sandbox launch trips `dn()`, which sits ahead of the config default, so `_build_sandbox_argv`
  auto-appends `--chrome` (checked before `dn()`) whenever `sandbox_chrome` is set. claude separately reports
  **Extension: Installed** by `readdir`-ing `<chrome-user-data>/<profile>/Extensions/<ext-id>`
  (*not* the native-messaging manifest), which a VM with no Chrome install always fails — so
  `/chrome` showed "Not detected" even with the bridge working. `_chrome_extension_guest_path`
  locates the extension in a host browser profile and passes the equivalent in-VM path as
  `CLAUDE_SANDBOX_CHROME_EXT_PATH`; the entrypoint creates just that directory (only its
  existence is checked). It returns None when the extension really is absent, so the status stays
  honest, and the host's Chrome profile — cookies, history, passwords — is never mounted into the
  VM. Browser tools work through the bridge either way; this only fixes the reported status.
- **Protecting the host's native-host wrapper:** the profile is mounted as the in-VM config dir,
  so an in-VM "Install Chrome extension" rewrites `<profile>/chrome/chrome-native-host` to an
  in-VM path (`/home/appuser/…`). Chrome's manifest on the **host** points at that same wrapper,
  so the in-VM install silently breaks the host's Chrome integration — Chrome can no longer spawn
  the native host. `_sandbox_chrome_overlay` masks the dir with a per-launch throwaway
  (`<profile>/chrome.sandbox`) mounted over `<config>/chrome`, keeping in-VM installs in the VM.
- **GitHub CLI (`sandbox_gh`):** the image bakes `gh`, and enabling `sandbox_gh` forwards your
  GitHub login into the VM. `gh` keeps its token in the system keyring (or `hosts.yml`), which a
  microVM can't reach, so `_with_gh_token` reads it on the host via `gh auth token` and injects it
  as `GH_TOKEN` (the env var gh reads natively) into the sandbox env — no config mount. A
  missing/failed token warns and continues (gh stays unauthenticated). The token grants the
  sandbox whatever the login's scopes allow, so it is opt-in.
- **Infisical (`sandbox_infisical`):** the image bakes the `infisical` CLI; `sandbox_infisical` is
  an allowlist (comma-separated emails or domain substrings) of infisical logins to forward. The
  CLI stores each login as a JSON `UserCredentials` blob in the OS keyring (service `infisical-cli`,
  keyed by email), which a microVM can't reach. `_infisical_logins()` matches the allowlist against
  the host's `~/.infisical/infisical-config.json` `loggedInUsers`, reads each match's token from the
  keyring via `secret-tool`, and drops any whose access JWT is expired (the CLI can't refresh, so an
  expired login is dead until `infisical login`). `_with_infisical_env` forwards the primary (active
  login if allowlisted, else first) as `INFISICAL_TOKEN` + `INFISICAL_API_URL`/`INFISICAL_DOMAIN` (so
  zero-flag use works) and all matches as `CLAUDE_SANDBOX_INFISICAL` (JSON) for `--token`/`--domain`
  targeting; `_infisical_briefing` appends a usage note to the system prompt. Env-only, keyring
  untouched. `INFISICAL_API_URL` (not the newer `INFISICAL_DOMAIN`, unsupported on older CLIs) is the
  reliable domain override, and it beats a repo's `.infisical.json`, so non-primary orgs need an
  explicit `--domain`. Each token grants full access to that login's secrets, so it is opt-in via
  explicit allowlist.
- **Pulumi (`sandbox_pulumi`):** the image bakes the `pulumi` CLI; enabling `sandbox_pulumi` forwards
  your Pulumi Cloud token into the VM. `pulumi` keeps the token in `~/.pulumi/credentials.json` (a
  plaintext file, not a keyring), which a microVM can't reach, so `_pulumi_token` reads it on the
  host — the `accessTokens[current]` entry, only when the current backend is an https (Pulumi Cloud)
  URL; self-managed backends (`s3://`, `file://`) have no token — and `_with_pulumi_token` injects it
  as `PULUMI_ACCESS_TOKEN` (the env var pulumi reads natively). A missing token warns and continues.
  The token grants full access to the account's stacks, so it is opt-in.
- **MCP servers & host env forwarding (`sandbox_forward_env`):** the profile's `.claude.json` is
  mounted, so claude in the VM sees the configured MCP servers. HTTP servers work over the VM's
  egress; `podman run …` servers work via the podman wrapper above. Servers that pass a secret
  through as `-e VAR` need that host var inside the VM: `sandbox_forward_env` is a comma-separated
  list of env var names, and `_with_forwarded_env` copies each one present in the host environment
  into the sandbox (missing names warn and skip). Servers that bake values into the config's `env`
  block (e.g. the victoria* servers) already travel with the mounted config. See the image-cache
  bullet to avoid re-pulling container MCP images each launch.
- **MCP image cache (`sandbox-cache`):** container MCP images would be re-pulled every launch (the
  VM is ephemeral). `claude-profile sandbox-cache <name>` discovers the podman/docker MCP images from
  the profile's `.claude.json` (`_mcp_container_images`), pulls them into a shared host store
  (`~/.local/share/claude-profile/image-store`, overlay+fuse-overlayfs to match the VM) and
  `podman unshare chmod -R a+rX`s it so the VM's mapped root can read it. When the store is
  populated, `_image_cache_mounts` bind-mounts it read-only at `SANDBOX_IMAGE_STORE` plus a
  generated storage.conf overlay with `additionalimagestores`, so in-VM podman finds images locally
  (no pull); an empty/absent store leaves the pull-on-demand default. Read-only and shared, so
  parallel worktree sandboxes can't corrupt it.
- **`sandbox` subcommand & override:** `sandbox <name> --on/--off` toggles the `.sandbox`
  marker on an existing profile (shows status when no flag). `_sandbox_enabled()` decides
  per launch: the `CLAUDE_PROFILE_SANDBOX` override (`settings.sandbox`, a tri-state
  `Optional[bool]`) wins when set, otherwise the marker.

## Conventions

- Follow the global standards in `~/.claude/CLAUDE.md`.
- ≤100 lines per function, ≤8 cyclomatic complexity, 100-char line length.
- No relative imports.
- Fail fast with clear messages; never swallow exceptions.
- No speculative features — only implement what is explicitly requested.
