"""claude-profile - Launch Claude Code with an isolated config directory."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

import typer
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from rich.console import Console
from rich.table import Table


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAUDE_PROFILE_")

    profiles_base: Path = Field(
        default_factory=lambda: Path.home() / ".claude-profiles"
    )
    claude_bin: str = Field(default="claude")


settings = Settings()
console = Console()
err_console = Console(stderr=True)

LINKABLE_DIRS: tuple[str, ...] = ("commands", "skills")

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


def _setup_dir_link(profile_dir: Path, dir_name: str, link: bool) -> None:
    """Create a symlink or isolated directory for dir_name inside profile_dir.

    Args:
        profile_dir: The profile directory path.
        dir_name: Name of the directory to set up (e.g. "commands", "skills").
        link: If True, create a symlink to the global dir. If False, create a local dir.
    """
    global_dir = Path.home() / ".claude" / dir_name
    profile_path = profile_dir / dir_name

    if link:
        if not global_dir.exists():
            err_console.print(
                f"[yellow]Warning: ~/.claude/{dir_name} does not exist, skipping symlink.[/yellow]"
            )
            return
        if not profile_path.exists():
            profile_path.symlink_to(global_dir)
    else:
        profile_path.mkdir(exist_ok=True)


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

    for dir_name in LINKABLE_DIRS:
        link = typer.confirm(
            f"Link '{dir_name}' to ~/.claude/{dir_name}?", default=True
        )
        _setup_dir_link(d, dir_name, link)

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


@app.command("links")
def manage_links(
    name: str = typer.Argument(..., help="Profile name"),
    dir_name: Optional[str] = typer.Argument(
        None,
        metavar="DIR",
        help=f"Directory to operate on. One of: {', '.join(LINKABLE_DIRS)}. Omit to apply to all.",
    ),
    link: bool = typer.Option(
        False, "--link", help="Symlink to global ~/.claude/<dir>."
    ),
    unlink: bool = typer.Option(
        False, "--unlink", help="Replace symlink with an isolated directory."
    ),
) -> None:
    """Inspect or change symlinks for a profile's directories."""
    if link and unlink:
        err_console.print(
            "[red]Error: --link and --unlink are mutually exclusive.[/red]"
        )
        raise typer.Exit(code=1)

    d = settings.profiles_base / name
    if not d.exists():
        err_console.print(f"[red]Profile '{name}' does not exist.[/red]")
        raise typer.Exit(code=1)

    if dir_name is not None and dir_name not in LINKABLE_DIRS:
        err_console.print(
            f"[red]Error: '{dir_name}' is not a linkable directory. "
            f"Choose from: {', '.join(LINKABLE_DIRS)}[/red]"
        )
        raise typer.Exit(code=1)

    dirs: tuple[str, ...] = (dir_name,) if dir_name is not None else LINKABLE_DIRS

    if not link and not unlink:
        _show_links_table(name, d, dirs)
        return

    if link:
        for dn in dirs:
            _do_link(d, dn)
    else:
        for dn in dirs:
            _do_unlink(d, dn)


def _show_links_table(name: str, profile_dir: Path, dirs: tuple[str, ...]) -> None:
    table = Table(title=f"Links for profile '{name}'")
    table.add_column("Directory", style="cyan")
    table.add_column("Status")

    for dn in dirs:
        path = profile_dir / dn
        if path.is_symlink():
            target = path.resolve()
            status = f"linked → {target}"
        elif path.is_dir():
            status = "isolated"
        else:
            status = "not configured"
        table.add_row(dn, status)

    console.print(table)


def _do_link(profile_dir: Path, dir_name: str) -> None:
    path = profile_dir / dir_name
    global_dir = Path.home() / ".claude" / dir_name

    if path.is_symlink():
        console.print(f"'{dir_name}' is already linked.")
        return
    if path.is_dir():
        err_console.print(
            f"[red]Error: '{dir_name}' is an isolated directory. "
            f"Remove it manually first: rm -r {path}[/red]"
        )
        raise typer.Exit(code=1)
    if not global_dir.exists():
        err_console.print(
            f"[yellow]Warning: ~/.claude/{dir_name} does not exist, skipping.[/yellow]"
        )
        raise typer.Exit(code=1)
    path.symlink_to(global_dir)
    console.print(f"[green]Linked '{dir_name}' → {global_dir}[/green]")


def _do_unlink(profile_dir: Path, dir_name: str) -> None:
    path = profile_dir / dir_name

    if path.is_symlink():
        path.unlink()
        path.mkdir()
        console.print(
            f"[green]Unlinked '{dir_name}', created isolated directory.[/green]"
        )
    elif path.is_dir():
        console.print(f"'{dir_name}' is already isolated.")
    else:
        path.mkdir()
        console.print(f"[green]Created isolated '{dir_name}' directory.[/green]")


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
    _KNOWN_COMMANDS = {"list", "add", "remove", "links"}
    args = sys.argv[1:]
    if args and args[0] not in _KNOWN_COMMANDS and not args[0].startswith("-"):
        _launch_profile(args[0], args[1:])
    else:
        app()
