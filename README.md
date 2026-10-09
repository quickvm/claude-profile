# claude-profile

Launch Claude Code with isolated config directories per profile. Each profile stores
its own credentials and session history, so you can maintain multiple Claude accounts
without re-authenticating.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — Python package manager used to install and run `claude-profile`
- [claude](https://claude.ai/code) — Claude Code CLI must be installed and on your `PATH`

[Sandbox mode](#sandbox-microvm) (optional) additionally requires [podman](https://podman.io/)
with the `krun` runtime — `sudo dnf install crun-krun` on Fedora — and `libkrun >= 1.8`.

## Install

From GitHub (no clone required):

```sh
uv tool install git+https://github.com/quickvm/claude-profile
```

From a local clone:

```sh
uv tool install .
```

## Usage

```sh
claude-profile add <name>            # create a new profile
claude-profile add <name> --sandbox  # create a profile that runs in a microVM
claude-profile list                  # list all profiles, auth status, and sandbox mode
claude-profile remove <name>         # delete a profile
claude-profile links <name>          # inspect or change directory symlinks
claude-profile sandbox <name>        # turn microVM mode on/off for a profile
claude-profile build                 # build the microVM sandbox image
claude-profile sandbox-skill         # (re)generate the in-sandbox tools skill
claude-profile <name> [args...]      # launch claude with the given profile
```

### Creating a profile

`add` copies `settings.json` and `CLAUDE.md` from `~/.claude` into the new profile directory
and symlinks `statusline.sh` to `~/.claude/statusline.sh`, so every profile, sandboxed or
not, runs the one status line script. It then prompts whether to symlink `commands/`,
`skills/` and `hooks/` to your global `~/.claude` directories (default: yes). Decline to keep
those directories isolated per profile. Link `hooks/` if your settings run hooks from
`~/.claude/hooks/`: inside the sandbox `~/.claude` is the profile directory, so an unlinked
guard hook is missing there and fails open.

```sh
claude-profile add work
# Link 'commands' to ~/.claude/commands? [Y/n]:
# Link 'skills' to ~/.claude/skills? [Y/n]:
```

### Managing directory links

Use `links` to inspect or change how `commands/`, `skills/` and `hooks/` are connected after a
profile is created:

```sh
claude-profile links work                   # show link status for all dirs
claude-profile links work commands          # show link status for commands/ only

claude-profile links work --link            # symlink all dirs to ~/.claude
claude-profile links work commands --link   # symlink just commands/

claude-profile links work --unlink          # isolate all dirs
claude-profile links work skills --unlink   # isolate just skills/
```

## Examples

```sh
claude-profile add work
claude-profile add personal
claude-profile work                   # launch claude with work profile
claude-profile personal --resume      # pass args through to claude
```

Add aliases to `~/.bashrc`:

```sh
alias claude-work='claude-profile work'
alias claude-personal='claude-profile personal'
```

## Shared settings

Settings every profile should have, such as hooks, can live in one file instead of being
copied into each profile's `settings.json`:

```sh
~/.claude-profiles/shared-settings.json
```

Every launch passes it to claude with `--settings`. claude merges it over the profile's own
settings, and hooks from both run. `{profile}` anywhere in the file becomes the profile's
name, so a hook can tell profiles apart:

```json
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "http",
      "url": "http://127.0.0.1:8080/hook?profile={profile}"}]}]
  }
}
```

In the sandbox, HTTP hooks aimed at `127.0.0.1` or `localhost` are re-pointed at the host
(inside the VM, 127.0.0.1 is the VM itself), so local hook receivers hear from sandboxed
sessions too. Passing your own `--settings` skips the shared file for that launch.

## Sandbox (microVM)

Sandbox mode runs Claude Code inside a lightweight microVM (podman + the
[`krun`](https://github.com/containers/libkrun) runtime) instead of directly on your host.
The agent gets its own kernel behind a hypervisor and sees only the directories mounted in,
so you can turn it loose with `--dangerously-skip-permissions` without it touching the rest
of your machine. That flag is added automatically in sandbox mode.

### Setup

```sh
sudo dnf install crun-krun         # Fedora: podman + krun runtime (libkrun >= 1.8)
claude-profile build               # build the sandbox image
claude-profile add work --sandbox  # mark a profile as sandboxed
```

`build` builds the image from a `Containerfile` shipped with the tool: Fedora + claude,
`uv`, git/ripgrep/fd/jq/yq, python (pyyaml/jinja2), openssl/make/trash, and `dnf`-scoped
passwordless sudo. Edit it for more baked-in tooling and rebuild; override the image name
with `CLAUDE_PROFILE_SANDBOX_IMAGE`.

### Claude Code version

You don't have to rebuild the image to keep the sandbox current. If your host's Claude Code
came from the native installer (`claude.ai/install.sh`), that binary is self-contained, so
each launch mounts the host's *current* version into the VM read-only and runs it — the
sandbox is on whatever version you are, and it follows along when Claude Code updates
itself on the host. The in-VM auto-updater is switched off, since the VM is thrown away at
exit and an update would only be discarded.

The binary is copied to `~/.local/share/claude-profile/claude/<version>` first (mounting it
would otherwise SELinux-relabel your real install), which happens once per version, not
once per launch. If your claude came from npm or a distro package instead, the VM runs the
version baked into the image and `claude-profile build` is how you update it.

### Tools & installing more

On launch the agent is told (via the system prompt) that it's in the sandbox, and the
bundled `sandbox-tools` skill explains how to self-provision. Inside the VM it can:

- `sudo dnf install -y <pkg>` — passwordless sudo covers `dnf` and `podman`. Either one
  amounts to root inside the VM, so the sudo rules are a convenience, not a boundary; the VM
  is the boundary
- `uv tool install <tool>` / `uv run --with <lib> …` — no root needed

Installs are per-session (the VM is ephemeral). For a tool you need every time, add it to
the `Containerfile` and rebuild, or build a derived image:

```dockerfile
# ~/qvm-sandbox/Containerfile
FROM claude-profile-sandbox:latest
USER root
RUN dnf install -y butane && dnf clean all
USER appuser
RUN uv tool install <your-tool>
USER root   # MUST end as root: the entrypoint starts as root to map your UID and set
            # up agent-socket dirs. A non-root final USER silently breaks SSH/GPG forwarding.
```
```sh
podman build -t qvm-sandbox ~/qvm-sandbox
export CLAUDE_PROFILE_SANDBOX_IMAGE=qvm-sandbox   # claude-profile uses it automatically
```

The skill's "already installed" list is generated from the image — after changing the
Containerfile, run `claude-profile sandbox-skill` (or `--check` to catch drift).

### Launching

A sandboxed profile launches in a microVM on every run — same command as always:

```sh
cd ~/src/myproject
claude-profile work                # boots a microVM, mounts this directory, runs claude
```

Each launch is its own throwaway microVM, so you can run many at once. This fits a
per-worktree workflow — one terminal per worktree:

```sh
wt switch feature-a && claude-profile work   # microVM #1, sees only this worktree
wt switch feature-b && claude-profile work   # microVM #2, isolated from #1
```

The launcher mounts the current directory's git work tree at its real path, read-write: the
whole repo when you start in a subdirectory (the session still starts in that directory),
and just the directory outside a repo. It refuses when that would be your home directory or
anything above it (such as `/`), which would expose your whole home directory, or would
overlap the profiles directory and its credentials. It also mounts
the repo's git common dir when it lives outside the work tree, so `git` and `wt` work inside
the VM and each
worktree keeps its own session history. A profile's credentials and config are shared
across its VMs, just as they already are across host terminals. If the profile links
`commands`, `skills` or `statusline.sh` to your global `~/.claude`, those targets are
mounted **read-only** into the VM so the links resolve there too, as is your `~/.gitconfig`
(so git identity and signing config apply).

### Turning sandbox mode on or off

Flip an existing profile between host and microVM (its credentials carry over either way):

```sh
claude-profile sandbox personal --on    # personal now launches in a microVM
claude-profile sandbox personal --off   # back to running on the host
claude-profile sandbox personal         # show current mode
```

Override per launch without changing the profile's default, via `CLAUDE_PROFILE_SANDBOX`:

```sh
CLAUDE_PROFILE_SANDBOX=0 claude-profile personal   # this run on the host
CLAUDE_PROFILE_SANDBOX=1 claude-profile personal   # this run in a microVM
```

Aliases make both modes one word:

```sh
alias claude-personal='claude-profile personal'                        # profile default
alias claude-personal-host='CLAUDE_PROFILE_SANDBOX=0 claude-profile personal'
```

### Credentials

Authenticate inside the VM the first time — `claude-profile work /login` prints a URL to
open in your host browser; the token is saved in the profile dir and reused afterward. Or
supply an API key, which is forwarded into the VM:

```sh
claude-profile env work --set ANTHROPIC_API_KEY=sk-...
```

### SSH agent access

To use your SSH keys inside the sandbox (git over SSH, commit signing, ssh to servers),
enable agent forwarding:

```sh
export CLAUDE_PROFILE_SANDBOX_SSH_AGENT=1   # globally, or set it per launch
claude-profile personal
```

This forwards your active agent (`$SSH_AUTH_SOCK`) and, if present, the 1Password agent
(`~/.1password/agent.sock`) into the VM — hardware/security keys included — so `ssh-add -l`
and `git push` work there. Dead agent sockets (e.g. a stale gnome-keyring stub) are skipped.
Inside the VM the agents appear as `/run/claude-sandbox/ssh-agent-0.sock`, `-1.sock` and so
on, agents holding keys first, and `SSH_AUTH_SOCK` points at `ssh-agent-0.sock`. To use the
other one, `export SSH_AUTH_SOCK=/run/claude-sandbox/ssh-agent-1.sock`.

Host-key checking persists per profile: your `~/.ssh/known_hosts` is mounted read-only for
verification, and host keys ssh accepts inside the VM are saved to a per-profile
`known_hosts` that carries over to the next launch — your real `~/.ssh/known_hosts` is never
modified.

A microVM has its own kernel, so the socket can't be bind-mounted. Instead `claude-profile`
relays the agent over pasta networking from a bridge it serves on the host's `127.0.0.1`,
which means an SSH-agent launch runs the VM as a child process instead of exec'ing it (the
TUI is unchanged).

**Security:** while the VM runs, the bridge listens on a TCP port on the host's `127.0.0.1`.
Every connection must start with a random token made for that launch and handed only to the
VM, so other users' processes, containers on the host network and other sandbox VMs can't use
your agent through it. Code in the sandbox can *use* your keys to authenticate (it cannot read
them). Leave this off for untrusted work.

### GPG (signed commits)

If you sign commits with GPG, enable gpg-agent forwarding:

```sh
export CLAUDE_PROFILE_SANDBOX_GPG_AGENT=1
claude-profile personal
```

The sandbox gets a fresh GNUPGHOME seeded with your **public keys** (`gpg --export`) plus a
bridge to your gpg-agent's restricted `S.gpg-agent.extra` socket. Signing happens on the
host, so your secret keys (or smartcard) never enter the VM. gpg-agent applies your usual
PIN and touch policy: with the PIN cached and no touch requirement, the sandbox can sign
without any prompt. The restricted socket also allows decryption, so the sandbox can
decrypt data encrypted to your keys. It goes through the same token-checked bridge as the
SSH agent. Your `~/.gitconfig` is mounted read-only too. Files it
pulls in with `include` or `includeIf` are not, so the launcher resolves `user.name` and
`user.email` for the working directory on the host and passes them in as global config,
together with `user.signingkey`, `commit.gpgsign` and `gpg.format` when GPG forwarding is
on and the format is openpgp. Commits in the VM get the identity and key git would use for
that directory on the host, and a repo's own config still overrides them. Note:
`gpg --list-secret-keys` looks empty
inside the VM (the restricted socket hides key listing) — that's expected; signing still
works.

### Clipboard (image paste)

Claude Code lets you paste an image (Ctrl+V) into the prompt. It reads the image by shelling
out to `wl-paste`, which needs the desktop clipboard — something the microVM, with its own
kernel and no display, can't reach. Enable a scoped clipboard bridge:

```sh
export CLAUDE_PROFILE_SANDBOX_CLIPBOARD=1
claude-profile personal
```

The bridge runs your real `wl-paste` on demand, read-only invocations only, and streams just
the clipboard bytes into the VM; an in-VM `wl-paste` shim feeds them to Claude Code. Requires
`wl-paste` on the host (`dnf install wl-clipboard`) and a Wayland session.

**Security:** deliberately *not* full Wayland forwarding. Proxying the whole compositor (e.g.
waypipe) would also hand the sandbox screen capture and keystroke injection into your focused
window — a practical escape. This bridge is **read-only clipboard**: the sandbox can read
what's on your clipboard while it runs, and nothing else. Like the agent forwards, it runs
the VM as a child process instead of exec'ing it, because `claude-profile` serves the bridge.

### GitHub CLI

The image ships `gh`. To let the agent act on your GitHub account (open PRs, comment, `gh api`),
forward your login:

```sh
export CLAUDE_PROFILE_SANDBOX_GH=1
claude-profile personal
```

`gh` stores its token in your system keyring (or `hosts.yml`), which the microVM can't reach, so
`claude-profile` reads it on the host with `gh auth token` and passes it in as `GH_TOKEN` — the
env var `gh` reads natively. No config file is mounted. If no token is found, you get a warning
and `gh` is simply unauthenticated inside the VM.

**Security:** this hands the sandbox a token with your account's scopes (yours are `repo`,
`workflow`, `read:org`, `gist`) — the agent can do anything they allow, including pushing code and
triggering workflows. It is off by default; enable it only when you want the agent working against
your real GitHub account.

### Infisical

The image ships the `infisical` CLI. To let the agent read your secrets, allowlist which of your
infisical logins to forward — by exact email, or by domain (which also covers its
subdomains), comma-separated:

```sh
export CLAUDE_PROFILE_SANDBOX_INFISICAL="corp.example,quickvm.com"
claude-profile personal
```

`infisical` keeps its login token in your system keyring, which the microVM can't reach. For each
allowlisted, non-expired login, `claude-profile` reads the token on the host and forwards it as
env: the primary (your active login if it's allowlisted, else the first match) becomes
`INFISICAL_TOKEN` + `INFISICAL_API_URL`, so `infisical secrets --projectId … --env …` works with no
extra flags; every allowlisted login is also placed in `CLAUDE_SANDBOX_INFISICAL` (JSON) so the
agent can target a non-primary org with `--token`/`--domain`. The keyring is never modified and
nothing is written to disk.

Only currently-valid logins are forwarded — the CLI can't refresh, so an expired login is skipped
with a warning until you re-run `infisical login` on the host. Forwarded tokens are good for ~10
days. The primary domain is set globally, which **overrides any repo's `.infisical.json`**, so for
a non-primary org always pass `--domain` explicitly.

**Security:** each forwarded token grants the sandbox that login's full access to your secrets. It
is off by default and takes an explicit allowlist — no login is forwarded unless you name it.

### Pulumi

The image ships the `pulumi` CLI. To let the agent run `pulumi preview`/`up` against your stacks,
forward your Pulumi Cloud token:

```sh
export CLAUDE_PROFILE_SANDBOX_PULUMI=1
claude-profile personal
```

`pulumi` stores its token in `~/.pulumi/credentials.json`, which the microVM can't reach, so
`claude-profile` reads it on the host and passes it in as `PULUMI_ACCESS_TOKEN` — the env var
`pulumi` reads natively. Only Pulumi Cloud (https) backends are forwarded; self-managed backends
(`s3://`, `file://`, …) carry no token and are skipped. If no token is found you get a warning and
`pulumi` is unauthenticated inside the VM.

**Security:** this hands the sandbox a token with full access to your Pulumi Cloud stacks — the
agent can read state and run `pulumi up`/`destroy`. It is off by default; enable it only when you
want the agent working against your real Pulumi account.

### Buildkite CLI

The image ships `bk` (the Buildkite CLI). It authenticates from `BUILDKITE_API_TOKEN` — no dedicated
toggle needed; forward that token via `CLAUDE_PROFILE_SANDBOX_FORWARD_ENV` (the same token the
buildkite MCP uses). Pass `--org <slug>` per command, or run `bk configure` in-session for a default
org.

### Nested containers (Podman)

The image includes `podman`, so the agent can build and run containers inside the VM. They run
**rootful**: plain `podman` is wrapped to `sudo` automatically, and `sudo podman` works too:

```sh
podman run --rm docker.io/library/alpine echo hi
podman build -t myimage .
```

Files a nested container writes into the mounted repo belong to root inside the VM, which is a
subuid on the host, so you can't delete them there without `podman unshare rm -rf <path>`.
Containers that write into the repo should run with `--user "$(id -u):$(id -g)"`; the agent
is told to do this.

Rootless podman doesn't work here (the nested user namespace can't be mapped), but the krun guest
kernel treats uid 0 as real root, so podman behaves like rootful podman on a normal Fedora host,
with fuse-overlayfs storage. Container networking works normally — external DNS and
container-to-container name resolution both resolve — because the sandbox runs the microVM with a
real guest network stack (`krun.use_passt=1`) instead of libkrun's default TSI socket
impersonation. (TSI silently drops socket options like `SO_REUSEADDR`, which breaks gRPC, and
intercepts container DNS.) This needs `passt` on the host and a recent crun/libkrun.

### MCP servers

Your MCP servers are configured in the profile's `.claude.json`, which is mounted into the VM, so
claude *sees* them — but servers written for the host don't all launch there:

- **HTTP servers** (e.g. `windmill`, `exa`) work as-is: the VM has network egress and any OAuth
  token travels in the mounted config.
- **Container servers** (`podman run …`, e.g. `github`, `buildkite`, the `victoria*` servers) work
  because the sandbox routes `podman` through rootful sudo automatically. A server that passes a
  token through as `-e VAR` needs that variable forwarded into the VM (servers that bake the value
  into the config's `env` block, like the `victoria*` ones, already travel with the config):

  ```sh
  export CLAUDE_PROFILE_SANDBOX_FORWARD_ENV="BUILDKITE_API_TOKEN,GITHUB_PERSONAL_ACCESS_TOKEN"
  ```

- **Host-path servers** (that run a host binary or read a host directory — e.g. an Obsidian vault
  path) won't work unless that path is mounted or the tool is installed in the VM.

Container MCP images are pulled inside the VM on each launch (it's ephemeral). To avoid that,
pre-cache them once on the host:

```sh
claude-profile sandbox-cache personal   # pull this profile's MCP images into a shared store
```

`sandbox-cache` discovers the `podman run … <image>` servers from the profile's `.claude.json`,
pulls them into `~/.local/share/claude-profile/image-store`, and makes it world-readable. The
sandbox then mounts that store read-only (a podman `additionalimagestore`) whenever it's populated,
so those images are found locally instead of pulled — no launch-time pull. It's read-only and
shared, so your parallel worktree sandboxes can't corrupt it. Re-run to update; `--clear` empties it.

### Security

A microVM raises the bar considerably but is not a perfect boundary. Networking stays open
(claude needs the API), so the sandbox limits **filesystem and process** blast radius — not
network egress — and the agent can read the profile credentials mounted into the VM. For
genuinely untrusted code, prefer a full or cloud VM. This is based on the Fedora Magazine
article [Sandbox AI coding agents with microVMs on Fedora Linux](https://fedoramagazine.org/sandbox-ai-coding-agents-with-microvms-on-fedora-linux/).

Credentials the agent in the VM can read or use:

- **Always:** the profile's Claude login (`.credentials.json` in the mounted profile
  directory), the profile's `.env` values (as environment variables), tokens in the MCP
  server entries of the profile's `.claude.json`, API keys in ancestor `.mcp.json` files
  (see below), and anything in a copy of your `~/.gitconfig`, such as a token in a remote URL.
- **When you turn them on:** your SSH agents (`CLAUDE_PROFILE_SANDBOX_SSH_AGENT`: it can
  authenticate with your keys, not read them), gpg-agent (`CLAUDE_PROFILE_SANDBOX_GPG_AGENT`:
  it can sign and decrypt, not read the secret keys), your GitHub token
  (`CLAUDE_PROFILE_SANDBOX_GH`), the allowlisted Infisical logins
  (`CLAUDE_PROFILE_SANDBOX_INFISICAL`), your Pulumi Cloud token
  (`CLAUDE_PROFILE_SANDBOX_PULUMI`), the variables named in
  `CLAUDE_PROFILE_SANDBOX_FORWARD_ENV`, your clipboard (`CLAUDE_PROFILE_SANDBOX_CLIPBOARD`,
  read-only), and your logged-in browser through the Claude extension (see below).
- **When the profile uses [Agent Vault](#agent-vault-infisical):** a session token, in
  `HTTPS_PROXY` and `HTTP_PROXY`, instead of the credentials themselves. It works only through
  the proxy, only for the bundle's services, and only until the launch ends or 24 hours pass.

Not mounted: `~/.ssh` (only a copy of `known_hosts`), your keyring, and other profiles'
directories.

What else the VM can reach:

- **Writable state the host trusts later.** The profile directory and the working tree are
  mounted read-write, so the agent could change files that run on the host afterwards. The
  ones the launcher knows about are protected:
  - The repo's `.git/config` (and each submodule's) is read-only in the VM, so the agent can't
    set something like `core.fsmonitor` that your next `git status` would run. Git commands
    that save config there can't save it: `git push -u` pushes but doesn't record the
    upstream, and `git remote add` fails.
  - `.git/hooks/`, the profile's `settings.json` and the project's
    `.claude/settings.local.json` are copies in the VM, thrown away when it exits. Hooks you
    have on the host still run in the VM.
  - The `.sandbox` marker and `.env` are read-only, so the agent can't switch the next launch
    to the host or plant variables in it.
  - On a host launch of a sandboxed profile you're warned if `statusline.sh` is no longer the
    link to `~/.claude/statusline.sh`.
  - When a session that forwards anything (SSH, GPG, clipboard) ends, you're told
    which MCP servers or `.mcp.json` approvals it added to the profile's `.claude.json`, and
    whether it created a `.claude/settings.local.json`. Other launches can't report this, so
    check `.claude.json` yourself after them.

  Everything else in the working tree, `.claude/settings.json` and `.mcp.json` included,
  is yours to review as you would any change the agent makes.
- **MCP config above the working directory.** Ancestor `.mcp.json` files (often
  `~/.mcp.json`) are mounted read-only, so any API keys in them are readable in the VM.
- **Your browser.** claude reaches the Claude Chrome extension through Anthropic's bridge,
  signed in with the profile's claude.ai login, which the VM holds. So while the extension is
  signed in to that account, anything in the VM can drive your logged-in Chrome with
  `claude --chrome`, whether or not `CLAUDE_PROFILE_SANDBOX_CHROME` is set (that setting only
  adds the flag). The sandbox runs claude with `--dangerously-skip-permissions`, so Claude Code
  doesn't ask before those actions either; the extension's site permissions are the remaining
  check. To limit what a session can reach, install the extension in a separate Chrome profile
  that isn't signed in to accounts you care about, narrow its site permissions, and turn it off
  when you aren't using it.
- **The host's loopback.** When a bridge or a loopback hook is active, the VM runs with
  `--map-host-loopback`, which reaches every service listening on the host's `127.0.0.1`,
  not only the bridges.

## Agent Vault (Infisical)

A profile can send claude's HTTPS through an
[Infisical Agent Vault](https://infisical.com/docs/documentation/platform/agent-vault/overview)
proxy, which attaches the real credential to each request for a service in an access bundle.
claude and everything it runs (`gh`, `curl`, git, SDKs, MCP servers) then work with placeholder
tokens, and the credentials stay in Infisical and on the proxy. It is off unless a profile opts
in, and it needs the `infisical` CLI on the host, logged in to the instance that holds the bundle.

Opt a profile in through its environment:

```sh
claude-profile env work \
  --set CLAUDE_PROFILE_AGENT_VAULT_BUNDLE=coding \
  --set CLAUDE_PROFILE_AGENT_VAULT_PROXY=10.0.1.5:17323 \
  --set CLAUDE_PROFILE_AGENT_VAULT_CA_FINGERPRINT=SHA256:9F:2C:... \
  --set GH_TOKEN=agent-vault
```

| Key | Required | Description |
|-----|----------|-------------|
| `CLAUDE_PROFILE_AGENT_VAULT_BUNDLE` | yes | The access bundle the session covers. Setting it turns Agent Vault on for the profile; unsetting it turns it off and keeps the other keys |
| `CLAUDE_PROFILE_AGENT_VAULT_PROXY` | with `BUNDLE` | The proxy's `host:port` |
| `CLAUDE_PROFILE_AGENT_VAULT_CA_FINGERPRINT` | with `BUNDLE` | The proxy CA's fingerprint, as the Proxies page shows it (`SHA256:...`) |
| `CLAUDE_PROFILE_AGENT_VAULT_INFISICAL_PROFILE` | no | The `infisical` login profile to mint sessions with (see `infisical profile list`), when the CLI would otherwise pick another |
| `CLAUDE_PROFILE_AGENT_VAULT_NO_PROXY` | no | Comma-separated hosts that skip the proxy, in addition to the ones listed below |

claude-profile reads these keys and claude never sees them. Every other variable in the profile's
environment reaches claude as usual, which is where placeholders go: a Bearer service replaces the
header whatever its value, so a placeholder only needs to satisfy tools that won't run without a
token, such as `gh`. These keys are per profile, not `CLAUDE_PROFILE_*` variables in your shell,
and the sandbox can't change them: the profile's `.env` is read-only in the VM.

What a launch does:

1. **Pre-flight.** It asks the proxy for its CA and compares it with the pinned fingerprint. If the
   proxy doesn't answer, serves another CA, or the `infisical` CLI is missing, the launch goes
   ahead without Agent Vault and says so in red. In the sandbox the agent is told as well, and
   `CLAUDE_SANDBOX_AGENT_VAULT` is `unavailable` (`active` otherwise).
2. **Session.** It starts itself again under `infisical agent-vault run`, which mints a 24-hour
   session over the bundle with your Infisical login and revokes it when the launch exits,
   including on SIGHUP and SIGTERM. If minting fails, the launch stops with the CLI's error; unset
   `CLAUDE_PROFILE_AGENT_VAULT_BUNDLE` to launch without it.
3. **Environment.** claude gets `HTTPS_PROXY` and `HTTP_PROXY`, which carry the session token, and
   a CA bundle with your system's roots plus the proxy's CA, so proxied and direct HTTPS both
   verify. The Claude API (`.anthropic.com`), the login and token hosts (`claude.ai`,
   `claude.com` and their subdomains) and the Claude in Chrome bridge (`.claudeusercontent.com`)
   skip the proxy, so your prompts and the profile's login never pass through it.

In the sandbox, the session token reaches the VM in the same unlinked env file as the rest of the
profile's environment, never in podman's argv or environment. The combined CA bundle is mounted
over the VM's system bundle, and the VM's `NO_PROXY` adds the host loopback, where the bridges
listen. On a host launch, `infisical agent-vault run` stays as claude's parent for the whole
session, since it revokes the session when claude exits; other launches replace claude-profile
with claude.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_PROFILE_PROFILES_BASE` | `~/.claude-profiles` | Directory where profiles are stored |
| `CLAUDE_PROFILE_CLAUDE_BIN` | `claude` | Path to the claude binary |
| `CLAUDE_PROFILE_PODMAN_BIN` | `podman` | Path to the podman binary (sandbox mode) |
| `CLAUDE_PROFILE_INFISICAL_BIN` | `infisical` | Path to the infisical CLI ([Agent Vault](#agent-vault-infisical) profiles) |
| `CLAUDE_PROFILE_SANDBOX_IMAGE` | `claude-profile-sandbox:latest` | Image used for sandbox launches |
| `CLAUDE_PROFILE_SANDBOX_RAM_MIB` | `4096` | microVM memory in MiB |
| `CLAUDE_PROFILE_SANDBOX_CPUS` | `4` | microVM vCPU count |
| `CLAUDE_PROFILE_SANDBOX_SKIP_PERMISSIONS` | `true` | Auto-add `--dangerously-skip-permissions` in sandbox mode |
| `CLAUDE_PROFILE_SANDBOX_SSH_AGENT` | `false` | Forward your SSH agent(s) into the VM through a token-checked bridge |
| `CLAUDE_PROFILE_SANDBOX_GPG_AGENT` | `false` | Forward your gpg-agent (signing) into the VM; seeds public keys, mounts `~/.gitconfig` |
| `CLAUDE_PROFILE_SANDBOX_CLIPBOARD` | `false` | Bridge your clipboard into the VM (read-only) so image paste works; needs `wl-paste` on the host |
| `CLAUDE_PROFILE_SANDBOX_CHROME` | `false` | Start the VM's claude with `--chrome`, so Claude in Chrome drives your browser through Anthropic's bridge; needs a full OAuth login, not a setup-token |
| `CLAUDE_PROFILE_SANDBOX_GH` | `false` | Forward your GitHub login into the VM as `GH_TOKEN` (read via `gh auth token`) so `gh` acts as you |
| `CLAUDE_PROFILE_SANDBOX_INFISICAL` | _(empty)_ | Allowlist (comma-separated emails/domains) of infisical logins to forward into the VM as `INFISICAL_TOKEN`/`--token` |
| `CLAUDE_PROFILE_SANDBOX_PULUMI` | `false` | Forward your Pulumi Cloud token into the VM as `PULUMI_ACCESS_TOKEN` so `pulumi` acts as you |
| `CLAUDE_PROFILE_SANDBOX_FORWARD_ENV` | _(empty)_ | Comma-separated host env var names to copy into the VM (e.g. tokens MCP servers pass through as `-e VAR`, or `HTTPS_PROXY`: the host's proxy settings aren't passed in otherwise) |
| `CLAUDE_PROFILE_SANDBOX` | _(unset)_ | Per-launch override: `1` forces microVM, `0` forces host; unset uses the profile's setting |

## License

[MIT](LICENSE)
