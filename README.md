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

`add` copies `settings.json`, `statusline.sh`, and `CLAUDE.md` from `~/.claude` into the new
profile directory, then prompts whether to symlink `commands/` and `skills/` to your global
`~/.claude` directories (default: yes). Decline to keep those directories isolated per profile.

```sh
claude-profile add work
# Link 'commands' to ~/.claude/commands? [Y/n]:
# Link 'skills' to ~/.claude/skills? [Y/n]:
```

### Managing directory links

Use `links` to inspect or change how `commands/` and `skills/` are connected after a profile
is created:

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

### Tools & installing more

On launch the agent is told (via the system prompt) that it's in the sandbox, and the
bundled `sandbox-tools` skill explains how to self-provision. Inside the VM it can:

- `sudo dnf install -y <pkg>` — passwordless sudo is scoped to `dnf`, so it can't `sudo` a
  write into your mounted repo
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

The launcher mounts the current directory at its real path, plus the repo's git common
dir when it lives outside the worktree, so `git` and `wt` work inside the VM and each
worktree keeps its own session history. A profile's credentials and config are shared
across its VMs, just as they already are across host terminals. If the profile links
`commands`/`skills` to your global `~/.claude`, those targets are mounted **read-only**
into the VM so the links resolve there too, as is your `~/.gitconfig` (so git identity and
signing config apply).

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
and `git push` work there. To switch to 1Password inside the VM,
`export SSH_AUTH_SOCK=~/.1password/agent.sock`.

A microVM has its own kernel, so the socket can't be bind-mounted; a `socat` bridge relays
the agent over pasta networking, with the host end bound to `127.0.0.1`. Managing that
bridge means an SSH-agent launch runs the VM as a child process instead of exec'ing it (the
TUI is unchanged). Requires `socat` on the host (`dnf install socat`).

**Security:** while the VM runs, the agent is reachable on a host-local port — from your
host and this VM only, not the network. Code in the sandbox can *use* your keys to
authenticate (it cannot read them). Leave this off for untrusted work.

### GPG (signed commits)

If you sign commits with GPG, enable gpg-agent forwarding:

```sh
export CLAUDE_PROFILE_SANDBOX_GPG_AGENT=1
claude-profile personal
```

The sandbox gets a fresh GNUPGHOME seeded with your **public keys** (`gpg --export`) plus a
bridge to your gpg-agent's restricted `S.gpg-agent.extra` socket. Signing happens on the
host, so your secret keys (or smartcard) never enter the VM and you get the usual PIN/touch
prompt. Your `~/.gitconfig` is mounted read-only too, so `commit.gpgsign` and
`user.signingkey` apply. Note: `gpg --list-secret-keys` looks empty inside the VM (the
restricted socket hides key listing) — that's expected; signing still works.

### Security

A microVM raises the bar considerably but is not a perfect boundary. Networking stays open
(claude needs the API), so the sandbox limits **filesystem and process** blast radius — not
network egress — and the agent can read the profile credentials mounted into the VM. For
genuinely untrusted code, prefer a full or cloud VM. This is based on the Fedora Magazine
article [Sandbox AI coding agents with microVMs on Fedora Linux](https://fedoramagazine.org/sandbox-ai-coding-agents-with-microvms-on-fedora-linux/).

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_PROFILE_PROFILES_BASE` | `~/.claude-profiles` | Directory where profiles are stored |
| `CLAUDE_PROFILE_CLAUDE_BIN` | `claude` | Path to the claude binary |
| `CLAUDE_PROFILE_PODMAN_BIN` | `podman` | Path to the podman binary (sandbox mode) |
| `CLAUDE_PROFILE_SANDBOX_IMAGE` | `claude-profile-sandbox:latest` | Image used for sandbox launches |
| `CLAUDE_PROFILE_SANDBOX_RAM_MIB` | `4096` | microVM memory in MiB |
| `CLAUDE_PROFILE_SANDBOX_CPUS` | `4` | microVM vCPU count |
| `CLAUDE_PROFILE_SANDBOX_SKIP_PERMISSIONS` | `true` | Auto-add `--dangerously-skip-permissions` in sandbox mode |
| `CLAUDE_PROFILE_SANDBOX_SSH_AGENT` | `false` | Forward your SSH agent(s) into the VM via a socat/pasta bridge |
| `CLAUDE_PROFILE_SANDBOX_GPG_AGENT` | `false` | Forward your gpg-agent (signing) into the VM; seeds public keys, mounts `~/.gitconfig` |
| `CLAUDE_PROFILE_SANDBOX` | _(unset)_ | Per-launch override: `1` forces microVM, `0` forces host; unset uses the profile's setting |

## License

[MIT](LICENSE)
