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

## Conventions

- Follow the global standards in `~/.claude/CLAUDE.md`.
- ≤100 lines per function, ≤8 cyclomatic complexity, 100-char line length.
- No relative imports.
- Fail fast with clear messages; never swallow exceptions.
- No speculative features — only implement what is explicitly requested.
