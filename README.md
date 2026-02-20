# claude-profile

Launch Claude Code with isolated config directories per profile. Each profile stores
its own credentials and session history, so you can maintain multiple Claude accounts
without re-authenticating.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — Python package manager used to install and run `claude-profile`
- [claude](https://claude.ai/code) — Claude Code CLI must be installed and on your `PATH`

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
claude-profile add <name>        # create a new profile
claude-profile list              # list all profiles and auth status
claude-profile remove <name>     # delete a profile
claude-profile <name> [args...]  # launch claude with the given profile
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

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_PROFILE_PROFILES_BASE` | `~/.claude-profiles` | Directory where profiles are stored |
| `CLAUDE_PROFILE_CLAUDE_BIN` | `claude` | Path to the claude binary |

## License

[MIT](LICENSE)
