"""claude-profile - Launch Claude Code with an isolated config directory."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import typer
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from rich.console import Console
from rich.table import Table


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAUDE_PROFILE_")

    profiles_base: Path = Field(default_factory=lambda: Path.home() / ".claude-profiles")
    claude_bin: str = Field(default="claude")


settings = Settings()
console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="claude-profile",
    help="Launch Claude Code with isolated config directories per profile.",
    no_args_is_help=True,
)


@app.command("list")
def list_profiles() -> None:
    """List all profiles and their authentication status."""
    base = settings.profiles_base
    if not base.exists():
        console.print("No profiles found. Create one with: claude-profile add <name>")
        return

    dirs = sorted(p for p in base.iterdir() if p.is_dir())
    if not dirs:
        console.print("No profiles found. Create one with: claude-profile add <name>")
        return

    table = Table(title="Claude Code Profiles")
    table.add_column("Profile", style="cyan")
    table.add_column("Status")

    for p in dirs:
        creds = p / ".credentials.json"
        if creds.exists():
            status = "[green]✓ authenticated[/green]"
        else:
            status = (
                f"[red]✗ not authenticated[/red] (run: claude-profile {p.name} /login)"
            )
        table.add_row(p.name, status)

    console.print(table)


@app.command("add")
def add_profile(name: str = typer.Argument(..., help="Profile name to create")) -> None:
    """Create a new profile."""
    d = settings.profiles_base / name
    if d.exists():
        err_console.print(f"[yellow]Profile '{name}' already exists at {d}[/yellow]")
        raise typer.Exit(code=1)
    d.mkdir(parents=True)
    claude_dir = Path.home() / ".claude"
    for fname in ("statusline.sh", "settings.json"):
        src = claude_dir / fname
        dst = d / fname
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
    claude_md = claude_dir / "CLAUDE.md"
    dst_md = d / "CLAUDE.md"
    if claude_md.exists() and not dst_md.exists():
        shutil.copy2(claude_md, dst_md)
    console.print(f"[green]Created profile '{name}'[/green] at {d}")
    console.print(f"Authenticate with: claude-profile {name} /login")


@app.command("remove")
def remove_profile(
    name: str = typer.Argument(..., help="Profile name to remove"),
) -> None:
    """Remove a profile and all its data."""
    d = settings.profiles_base / name
    if not d.exists():
        err_console.print(f"[red]Profile '{name}' does not exist.[/red]")
        raise typer.Exit(code=1)
    confirm = typer.confirm(f"Delete profile '{name}' and all its credentials/history?")
    if not confirm:
        console.print("Aborted.")
        raise typer.Exit()
    shutil.rmtree(d)
    console.print(f"[green]Removed profile '{name}'.[/green]")


def _launch_profile(name: str, claude_args: list[str]) -> None:
    d = settings.profiles_base / name
    if not d.exists():
        err_console.print(
            f"[red]Profile '{name}' does not exist.[/red] "
            f"Create it with: claude-profile add {name}"
        )
        sys.exit(1)
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(d)
    # exec replaces this process - no wrapper in between, which matters for
    # claude's TUI (raw terminal mode, signal handling, etc.)
    os.execvpe(settings.claude_bin, [settings.claude_bin] + claude_args, env)


def main() -> None:
    _KNOWN_COMMANDS = {"list", "add", "remove"}
    args = sys.argv[1:]
    if args and args[0] not in _KNOWN_COMMANDS and not args[0].startswith("-"):
        _launch_profile(args[0], args[1:])
    else:
        app()
