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
  typer subcommands or calls `_launch_profile()` directly for unknown first args.
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
- **Sandbox settings:** `podman_bin`, `sandbox_image`, `sandbox_ram_mib`,
  `sandbox_cpus`, `sandbox_skip_permissions` (all read from `CLAUDE_PROFILE_*`).
- **SSH agent forwarding (`sandbox_ssh_agent`):** a microVM can't bind-mount the agent
  socket (separate kernel), so `_ssh_agent_sockets()` lists the active agent + 1Password,
  `_start_host_bridge()` runs a host `socat` (TCP on 127.0.0.1 → the agent socket), and
  `--network=pasta:--map-host-loopback,…` lets the guest reach it; `entrypoint.sh` starts a
  guest `socat` per forward and sets `SSH_AUTH_SOCK`. Because the bridges need teardown,
  this path supervises podman (`_run_sandbox_supervised`) instead of `execvpe`.
- **GPG agent forwarding (`sandbox_gpg_agent`):** same bridge, forwarding the host
  gpg-agent restricted socket (`gpgconf --list-dirs agent-extra-socket`) to the in-VM
  `GNUPGHOME/S.gpg-agent` and seeding a fresh GNUPGHOME with the host's public keys
  (`gpg --export`, base64 via env, imported by `entrypoint.sh`). Signing runs on the host,
  so secret keys/smartcard never enter the VM. `_sandbox_mounts` also bind-mounts
  `~/.gitconfig` read-only so signing config applies. `_Forwarding` carries
  `(host_socket, guest_path, port)` tuples shared by both SSH and GPG; the in-VM env
  (`CLAUDE_SANDBOX_FORWARDS`, `SSH_AUTH_SOCK`, `GNUPGHOME`, `CLAUDE_SANDBOX_GPG_PUBKEYS`)
  is built by `_forwarding_env`.
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
